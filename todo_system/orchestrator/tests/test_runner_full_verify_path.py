"""The auto-merge path of a light or standard task — `StagedExecutor._execute`, for real.

D5 removed the per-repo quick-verify gate. What it did for a light or standard
task (judge whether the tree may merge, then merge it) is back with the path
every auto-merge run always had: an individual full Verify, then the Report
stage merges and the runner's landed gate confirms it. A vault may still hold
that gate's old config file, so the test writes one that registers this repo
and pins that it changes nothing: the verify prompt carries no QUICK mode, the
report stage is told `auto_merge: true`, the merge is checked, and the RESULT
carries no key of the deleted feature.

The pipeline is not mocked: branch prep and the implement checkpoint push are
real git against a throwaway bare origin, and the real
`ClaudeAgentBackend.run_stage` builds each stage through `build_stage_argv`.
Only that builder is swapped, for a stub that records the keywords it was
handed, renders the REAL prompt from them and returns a `/bin/sh` that writes
the stage's artifact. Nothing leaves tmp_path, and the global git config is
switched off for the test.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from todo_system.orchestrator import beta_publish, repos, stages
from todo_system.orchestrator import message as m
from todo_system.orchestrator.registry import Agent
from todo_system.orchestrator.runner import StagedExecutor

REPO_URL = "https://example.test/acme/lightrepo.git"
PR_URL = "https://example.test/acme/lightrepo/pull/7"
SLUG = "light-task"
# The deleted gate's per-repo config file, as a vault may still hold it (D5).
LEFTOVER_CONFIG_REL = Path(".macp") / "batch_gate.json"
LEFTOVER_CONFIG = {"enabled": True,
                   "repos": {"lightrepo": {"full_gate": [["./gradlew", ":app:testDebugUnitTest"]]}}}

VERIFY_PASS = "---\noverall_status: pass\nconfidence: medium\n---\n\n# verify\n"
REPORT_PASS = f"---\noverall_status: pass\nmerged: true\npr_url: {PR_URL}\n---\n\n# report\n"


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(cwd), *args],
                          check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _identify(repo: Path) -> None:
    _git(repo, "config", "user.email", "t@example.test")
    _git(repo, "config", "user.name", "t")


class _Sandbox:
    """A bare origin, a clone the run works in, and a vault stand-in — plus the
    stage stub. `execute()` runs one dispatch through the real `_execute`."""

    def __init__(self, tmp: Path, monkeypatch):
        self.tmp = tmp
        self.monkeypatch = monkeypatch
        # No global/system git config: a host-wide hooksPath or commit signing
        # must not decide whether these tests pass.
        (tmp / "gitconfig").write_text("", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp / "gitconfig"))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

        self.origin = tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        self.seed = tmp / "seed"
        subprocess.run(["git", "clone", str(self.origin), str(self.seed)],
                       check=True, capture_output=True)
        _identify(self.seed)
        self.publish_to_main("main.txt", "v1\n")
        self.clone = tmp / "clone"
        subprocess.run(["git", "clone", str(self.origin), str(self.clone)],
                       check=True, capture_output=True)
        _identify(self.clone)

        # The vault stand-in: stages run with cwd = the vault and it is
        # restore_main'd, so it has to be a git repo with a `main`.
        self.vault = tmp / "vault"
        subprocess.run(["git", "init", "-b", "main", str(self.vault)],
                       check=True, capture_output=True)
        _identify(self.vault)
        (self.vault / "seed.txt").write_text("x", encoding="utf-8")
        _git(self.vault, "add", "-A")
        _git(self.vault, "commit", "-m", "init")
        self.runs = tmp / "runs"
        self.runs.mkdir()
        monkeypatch.setattr(stages, "_VAULT_DATA_ROOT", self.vault)
        monkeypatch.setattr(stages, "RUNS_DIR", self.runs)

        # The real prompt is rendered in the stub below, so pin what it would
        # otherwise resolve from the host: the model ladder, effort and MCP server.
        monkeypatch.setattr(stages, "stage_model", lambda stage="implement", tier=None: "MODEL")
        monkeypatch.setattr(stages, "stage_effort", lambda stage="implement", tier=None: "EFFORT")
        monkeypatch.setattr(stages, "browser_mcp_server", lambda stage: None)

        self.artifacts = {"verify": VERIFY_PASS, "report": REPORT_PASS}
        self.implement_commits = True      # the implement stub commits one file
        self.implement_text = "changed"    # …with this content (a re-run needs new bytes)
        self.calls: list[tuple[str, dict]] = []
        self.prompts: dict[str, str] = {}
        self.lines: list[str] = []
        self._real_build = stages.build_stage_argv
        monkeypatch.setattr(stages, "build_stage_argv", self._stub_argv)

        self.merge_landed = mock.Mock(return_value=True)
        monkeypatch.setattr(stages, "merge_landed", self.merge_landed)
        monkeypatch.setattr(repos, "ensure_worktree", lambda *a, **k: self.clone)
        monkeypatch.setattr(stages, "remove_worktree", mock.Mock(return_value=False))
        monkeypatch.setattr(stages, "record_related", lambda *a, **k: None)
        monkeypatch.setattr(beta_publish, "repo_config", lambda *a, **k: None)
        monkeypatch.setattr(StagedExecutor, "_account_env",
                            staticmethod(lambda msg, repo, **k: (None, "")))
        monkeypatch.setattr(StagedExecutor, "_live_sibling_hold",
                            staticmethod(lambda msg, agent: None))
        # Belt and braces: the clone is not an app repo, but no test may ever
        # reach the billed emulator node.
        monkeypatch.setattr(StagedExecutor, "_app_screen_check",
                            lambda self, *a, **k: None)

    # -- setup helpers -----------------------------------------------------
    def publish_to_main(self, rel: str, text: str) -> None:
        """Commit one file on origin/main (branch prep cuts the run's branch there)."""
        path = self.seed / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-m", f"add {rel}")
        _git(self.seed, "push", "origin", "main")

    def write_leftover_config(self) -> None:
        """Commit the old config into the vault stand-in — committed, so the
        run's restore_main cannot drop it before anything reads the vault."""
        path = self.vault / LEFTOVER_CONFIG_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(LEFTOVER_CONFIG), encoding="utf-8")
        _git(self.vault, "add", "-A")
        _git(self.vault, "commit", "-m", "leftover gate config")

    # -- the stage stub ----------------------------------------------------
    def _stub_argv(self, stage, **kw):
        self.calls.append((stage, dict(kw)))
        self.prompts[stage] = self._real_build(stage, **kw)[2]
        artifact = Path(kw["run_dir"]) / stages.STAGE_ARTIFACT[stage]
        script = (f"printf '%s' {shlex.quote(self.artifacts.get(stage, 'ok'))} "
                  f"> {shlex.quote(str(artifact))}")
        if stage == "implement" and self.implement_commits:
            clone = shlex.quote(str(self.clone))
            script = (f"printf %s {shlex.quote(self.implement_text)} > {clone}/impl.txt "
                      f"&& git -C {clone} add impl.txt "
                      f"&& git -C {clone} commit -q -m implement && {script}")
        return ["/bin/sh", "-c", script]

    # -- run + read back ---------------------------------------------------
    def execute(self, **payload_extra):
        payload = {"repo": REPO_URL, "worker_mode": "auto", "auto_merge": True,
                   "task_path": "", **payload_extra}
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id=SLUG, corr_id="C",
                         payload=payload)
        agent = Agent(id="w", kind="ai-local", capabilities=["git"], repo="~/Worker/x")
        return StagedExecutor().execute(msg, agent, self.lines.append)

    def kw(self, stage: str) -> dict:
        [kwargs] = [k for s, k in self.calls if s == stage]
        return kwargs

    def stages_run(self) -> list[str]:
        return [s for s, _ in self.calls]


@pytest.fixture
def box(tmp_path, monkeypatch) -> _Sandbox:
    return _Sandbox(tmp_path, monkeypatch)


@pytest.mark.parametrize("tier", ["light", "standard"])
def test_leftover_gate_config_keeps_full_verify_and_report_merge(box, tier):
    box.write_leftover_config()
    out = box.execute(model_tier=tier)
    assert out.ok, out.summary
    assert box.stages_run() == list(stages.STAGES_AUTO)
    # an individual full Verify: no QUICK instruction reached the verify stage
    assert "Verify mode: QUICK" not in box.prompts["verify"]
    assert "**Batch gate**" not in box.prompts["report"]
    # the Report stage merges, and the runner's landed gate checks it
    assert box.kw("verify")["auto_merge"] is True
    assert box.kw("report")["auto_merge"] is True
    assert "- auto_merge: `true`" in box.prompts["report"]
    box.merge_landed.assert_called_once()
    assert out.payload["review_required"] is False
    assert out.payload["pr_url"] == PR_URL
    # the RESULT carries no key of the deleted feature
    assert [k for k in out.payload if k.startswith("batch")] == []
