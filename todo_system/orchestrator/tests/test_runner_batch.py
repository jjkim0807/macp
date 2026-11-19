"""Batch verify in the staged runner — `StagedExecutor._execute`, for real.

A batch-verified run (batch_gate.py) must tell its verify stage to run in QUICK
mode and its report stage not to merge, and must hand the orchestrator a RESULT
that says "waiting for the integration gate" (`batch_ready` + the judged sha) or
"send me back to a full verify" (`batch_return`). Every other run — an
unregistered repo, a heavy task, `verify_batch: false`, `auto_merge: false` —
must be exactly what it was.

The pipeline is not mocked: branch prep, the implement checkpoint push and the
head-sha read are real git against a throwaway bare origin, and the real
`ClaudeAgentBackend.run_stage` builds each stage through `build_stage_argv`. Only
that builder is swapped, for a stub that records the keywords it was handed,
renders the REAL prompt from them, and returns a `/bin/sh` that writes the
stage's artifact. So "the instruction reached the stage" is read off the prompt
the stage would have been given, not off a mock's call list.

Nothing leaves tmp_path: the vault stand-in, the config, the origin and the clone
are all under it, and the global git config is switched off for the test.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from todo_system.orchestrator import batch_gate, beta_publish, merge_hold, repos
from todo_system.orchestrator import message as m
from todo_system.orchestrator import screen_check, stages
from todo_system.orchestrator.registry import Agent
from todo_system.orchestrator.runner import StagedExecutor

REPO_URL = "https://example.test/acme/batchrepo.git"
PR_URL = "https://example.test/acme/batchrepo/pull/7"
SLUG = "batch-task"
BRANCH = f"worker/{SLUG}"
GATE_CMD = ["./gradlew", ":app:testDebugUnitTest"]
HINT = "HINT-SENTINEL"
CONFIG = {"enabled": True,
          "repos": {"batchrepo": {"full_gate": [GATE_CMD], "quick_hint": HINT}}}
BATCH_KEYS = (batch_gate.RESULT_READY, batch_gate.KEY_BATCH, batch_gate.RESULT_RETURN)

VERIFY_PASS = "---\noverall_status: pass\nconfidence: medium\n---\n\n# verify\n"
VERIFY_SKIP = "---\noverall_status: skip\nconfidence: low\n---\n\n# verify\n"
VERIFY_FAIL = "---\noverall_status: fail\nconfidence: low\n---\n\n# verify\n"
REPORT_PASS = f"---\noverall_status: pass\nmerged: n/a\npr_url: {PR_URL}\n---\n\n# report\n"
REPORT_NO_PR = "---\noverall_status: pass\nmerged: n/a\npr_url: null\n---\n\n# report\n"
REPORT_NO_OP = ("---\noverall_status: pass\nmerged: n/a\nno_op: true\n"
                "pr_url: null\n---\n\n# report\n")
REPORT_FAIL = f"---\noverall_status: fail\nmerged: n/a\npr_url: {PR_URL}\n---\n\n# report\n"


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
        self.schedule_hold = mock.Mock()
        self.downgrade = mock.Mock(return_value=True)
        monkeypatch.setattr(stages, "merge_landed", self.merge_landed)
        monkeypatch.setattr(merge_hold, "schedule", self.schedule_hold)
        monkeypatch.setattr(stages, "downgrade_verify_confidence", self.downgrade)
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

    def write_config(self, config: dict = CONFIG) -> None:
        path = self.vault / batch_gate.CONFIG_REL
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config), encoding="utf-8")
        _git(self.vault, "add", "-A")
        _git(self.vault, "commit", "-m", "batch gate config")

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
        payload = {"repo": REPO_URL, "worker_mode": "auto", "model_tier": "standard",
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

    def batch_lines(self) -> list[str]:
        return [ln for ln in self.lines if ln.startswith("batch verify:")]

    def branch_tip(self) -> str:
        return _git(self.clone, "rev-parse", BRANCH)


@pytest.fixture
def box(tmp_path, monkeypatch) -> _Sandbox:
    return _Sandbox(tmp_path, monkeypatch)


def _assert_ordinary_run(box: _Sandbox, out) -> None:
    """The run was not batch-verified: no stage saw a batch keyword or line, and
    the RESULT carries none of the batch keys."""
    assert out.ok, out.summary
    assert box.stages_run() == list(stages.STAGES_AUTO)
    for stage, kwargs in box.calls:
        assert "batch_mode" not in kwargs, stage
        assert "batch_hint" not in kwargs, stage
        assert "Verify mode: QUICK" not in box.prompts[stage], stage
        assert "**Batch gate**" not in box.prompts[stage], stage
    for key in BATCH_KEYS:
        assert key not in out.payload, key


def _assert_batch_hold(box: _Sandbox, out) -> None:
    assert out.ok, out.summary
    assert box.stages_run() == list(stages.STAGES_AUTO)
    # only verify and report are told; the creative stages are untouched
    for stage in ("research", "plan", "implement"):
        assert "batch_mode" not in box.kw(stage), stage
        assert "batch_hint" not in box.kw(stage), stage
    assert box.kw("verify")["batch_mode"] == stages.BATCH_QUICK == "quick"
    assert box.kw("report")["batch_mode"] == stages.BATCH_QUICK
    # the instruction the verify stage actually reads
    verify_prompt = box.prompts["verify"]
    assert "Verify mode: QUICK" in verify_prompt
    assert HINT in verify_prompt
    assert f"`{shlex.join(GATE_CMD)}`" in verify_prompt          # the deferred gate
    assert "origin/main...HEAD" in verify_prompt                 # the real default branch
    assert f"origin/<default>...{BRANCH}" in verify_prompt
    # ...and the report stage: open the PR, do not merge
    assert "**Batch gate**" in box.prompts["report"]
    assert "- auto_merge: `false`" in box.prompts["report"]
    assert box.kw("report")["auto_merge"] is False
    box.merge_landed.assert_not_called()                         # nothing was merged
    # the RESULT the orchestrator turns into a batch_ready row
    assert out.payload[batch_gate.RESULT_READY] is True
    assert out.payload[batch_gate.KEY_BATCH] == {"head_sha": box.branch_tip()}
    assert out.payload["review_required"] is True                # old orchestrators park it
    assert out.payload["pr_url"] == PR_URL
    assert out.payload["repo"] == REPO_URL
    assert batch_gate.RESULT_RETURN not in out.payload
    assert "no_op" not in out.payload
    assert box.batch_lines()[-1] == "batch verify: hold for the integration gate"


def _assert_batch_return(box: _Sandbox, out, reason: str) -> None:
    assert out.ok, out.summary
    assert out.payload[batch_gate.RESULT_RETURN] == reason
    assert batch_gate.RESULT_READY not in out.payload
    assert batch_gate.KEY_BATCH not in out.payload
    assert out.payload["review_required"] is True
    assert box.kw("report")["auto_merge"] is False               # a quick-only tree never merges
    box.merge_landed.assert_not_called()
    box.schedule_hold.assert_not_called()
    box.downgrade.assert_not_called()
    assert reason in out.summary


# --- (a) the batch path -------------------------------------------------------

def test_registered_repo_standard_task_is_held_for_the_integration_gate(box):
    box.write_config()
    out = box.execute()
    _assert_batch_hold(box, out)
    assert box.batch_lines() == ["batch verify: quick (tier standard)",
                                 "batch verify: hold for the integration gate"]
    # the sha is the branch tip the quick verify judged, not the base
    assert out.payload[batch_gate.KEY_BATCH]["head_sha"] != _git(box.clone, "rev-parse",
                                                                 "origin/main")
    # verify itself still ran with the run's own merge decision; only the report
    # stage is told the run does not merge
    assert box.kw("verify")["auto_merge"] is True
    assert box.kw("verify")["batch_hint"] == box.kw("report")["batch_hint"]
    assert HINT in box.kw("verify")["batch_hint"]


def test_light_task_is_batched_too(box):
    box.write_config()
    out = box.execute(model_tier="light")
    _assert_batch_hold(box, out)
    assert box.batch_lines()[0] == "batch verify: quick (tier light)"


def test_verify_batch_true_batches_a_heavy_task(box):
    # (d) the frontmatter opt-in beats the tier
    box.write_config()
    out = box.execute(model_tier="heavy", verify_batch=True)
    _assert_batch_hold(box, out)
    assert box.batch_lines()[0] == "batch verify: quick (verify_batch: true)"


# --- (b) repos with no batch gate: nothing changes, not even a progress line ---

def test_no_config_file_is_the_ordinary_run(box):
    out = box.execute()
    _assert_ordinary_run(box, out)
    assert box.batch_lines() == []
    assert box.kw("report")["auto_merge"] is True
    box.merge_landed.assert_called_once()
    assert out.payload["review_required"] is False


def test_another_repo_registered_is_the_ordinary_run(box):
    box.write_config({"enabled": True,
                      "repos": {"otherrepo": {"full_gate": [GATE_CMD]}}})
    out = box.execute()
    _assert_ordinary_run(box, out)
    assert box.batch_lines() == []
    assert box.kw("report")["auto_merge"] is True
    box.merge_landed.assert_called_once()


def test_disabled_config_is_the_ordinary_run(box):
    box.write_config({**CONFIG, "enabled": False})
    out = box.execute()
    _assert_ordinary_run(box, out)
    assert box.batch_lines() == []


# --- (c) registered repo, task not eligible: one "off" line, same run ----------

@pytest.mark.parametrize("extra, why", [
    ({"model_tier": "heavy"}, "tier heavy"),
    ({"verify_batch": False}, "verify_batch: false"),
    ({"verify_full": True}, "returned to full verify"),
    ({"verify_full": True, "verify_batch": True}, "returned to full verify"),
])
def test_ineligible_task_in_a_registered_repo_runs_as_before(box, extra, why):
    box.write_config()
    out = box.execute(**extra)
    _assert_ordinary_run(box, out)
    assert box.batch_lines() == [f"batch verify: off ({why})"]
    assert box.kw("report")["auto_merge"] is True
    box.merge_landed.assert_called_once()
    assert out.payload["review_required"] is False


def test_auto_merge_false_task_keeps_its_review_path(box):
    box.write_config()
    out = box.execute(auto_merge=False)
    _assert_ordinary_run(box, out)
    assert box.batch_lines() == ["batch verify: off (auto_merge off)"]
    assert box.kw("report")["auto_merge"] is False
    assert out.payload["review_required"] is True              # the human review park
    box.merge_landed.assert_not_called()


def test_remote_marked_full_gate_keeps_the_repo_out(box):
    # (h) the repo itself says this command must run on the remote node
    box.publish_to_main(".macp/gates.json", json.dumps(
        {"commands": [{"cmd": shlex.join(GATE_CMD), "remote": True}]}))
    box.write_config()
    out = box.execute()
    _assert_ordinary_run(box, out)
    assert box.batch_lines() == ["batch verify: off (full gate is remote-marked)"]
    box.merge_landed.assert_called_once()


# --- (e) a device run the task asked for --------------------------------------

def test_emulator_task_without_device_evidence_goes_back_to_full_verify(box):
    box.write_config()
    out = box.execute(heavy_signals=["emulator"])
    _assert_batch_return(box, out, batch_gate.RETURN_NO_DEVICE)
    assert box.batch_lines() == [
        "batch verify: quick (tier standard)",
        "batch verify: return to full verify (no-device-evidence)"]
    # the verify stage WAS told to run quick — the return is decided after it
    assert box.kw("verify")["batch_mode"] == stages.BATCH_QUICK


def test_emulator_task_with_device_evidence_is_held(box):
    box.write_config()
    box.artifacts["verify"] = (VERIFY_PASS + "\n## 실기 화면 확인 (screen_check)\n\n"
                               f"- {screen_check.OK_NOTE_PREFIX} — screens/a.png\n")
    out = box.execute(heavy_signals=["emulator"])
    _assert_batch_hold(box, out)


def test_ui_confidence_guard_sends_the_task_back_without_a_merge_hold(box, monkeypatch):
    # The guard that would have withheld an ordinary run's merge: in a batch run
    # it returns the task instead — no confidence downgrade, no hold record.
    box.write_config()
    monkeypatch.setattr(StagedExecutor, "_ui_merge_hold_fires",
                        lambda self, *a, **k: True)
    out = box.execute()
    _assert_batch_return(box, out, batch_gate.RETURN_NO_DEVICE)
    assert "ui_merge_hold" not in out.payload


# --- (f) the quick verdict is not PASS ----------------------------------------

def test_quick_verify_skip_is_never_held(box):
    # The report stub still opens a PR, so only the verdict keeps it out.
    box.write_config()
    box.artifacts["verify"] = VERIFY_SKIP
    out = box.execute()
    _assert_batch_return(box, out, batch_gate.RETURN_QUICK_NOT_PASS)
    assert out.payload["pr_url"] == PR_URL
    assert box.batch_lines()[-1] == "batch verify: return to full verify (quick-not-pass)"


def test_quick_verify_without_a_verdict_is_never_held(box):
    box.write_config()
    box.artifacts["verify"] = "# verify\n\nno frontmatter at all\n"
    out = box.execute()
    _assert_batch_return(box, out, batch_gate.RETURN_QUICK_NOT_PASS)


def test_quick_verify_fail_is_an_ordinary_failure(box):
    # A failed quick verify is a real failure of the change: the FAIL goes out as
    # it always did (the orchestrator retries it), not as a batch return.
    box.write_config()
    box.artifacts["verify"] = VERIFY_FAIL
    out = box.execute()
    assert not out.ok
    assert out.payload["verify"] == "FAIL"
    assert box.stages_run() == ["research", "plan", "implement", "verify"]
    for key in BATCH_KEYS:
        assert key not in out.payload, key
    assert box.batch_lines() == ["batch verify: quick (tier standard)"]


# --- (g) the report opened no PR ----------------------------------------------

def test_no_pr_goes_back_to_full_verify(box):
    box.write_config()
    box.artifacts["report"] = REPORT_NO_PR
    out = box.execute()
    _assert_batch_return(box, out, batch_gate.RETURN_NO_PR)
    assert "pr_url" not in out.payload
    # the verify-stage block had chosen the hold; the missing PR overturned it
    assert box.batch_lines()[-1] == "batch verify: hold for the integration gate"


def test_proven_no_op_closes_instead_of_waiting_or_returning(box):
    # `no_op: true` AND a 0-commit branch: nothing to merge, nothing to gate.
    box.write_config()
    box.implement_commits = False
    box.artifacts["report"] = REPORT_NO_OP
    out = box.execute()
    assert out.ok, out.summary
    assert out.payload["no_op"] is True
    assert out.payload["review_required"] is False
    for key in BATCH_KEYS:
        assert key not in out.payload, key
    assert "no-op" in out.summary


def test_declared_no_op_with_commits_is_not_a_no_op(box):
    # The signal alone never closes a task: the branch carries a commit.
    box.write_config()
    box.artifacts["report"] = REPORT_NO_OP
    out = box.execute()
    _assert_batch_return(box, out, batch_gate.RETURN_NO_PR)
    assert "no_op" not in out.payload


# --- the judged sha -------------------------------------------------------------

def test_unreadable_head_sha_goes_back_to_full_verify(box, monkeypatch):
    box.write_config()
    monkeypatch.setattr(StagedExecutor, "_head_sha", staticmethod(lambda repo: ""))
    out = box.execute()
    _assert_batch_return(box, out, batch_gate.RETURN_NO_HEAD_SHA)


# --- the report's own verdict ----------------------------------------------------

def test_report_declaring_failure_is_still_a_failed_run(box):
    # The batch keys ride the payload, but the outcome is a FAIL — and the
    # orchestrator reads batch keys off a RESULT only (test_batch_ready_state).
    box.write_config()
    box.artifacts["report"] = REPORT_FAIL
    out = box.execute()
    assert not out.ok
    assert "report declared the run failed" in out.summary


# --- (i) the start path fails open ----------------------------------------------

def test_unreadable_config_is_the_ordinary_run(box, monkeypatch):
    box.write_config()

    def boom(*_a, **_k):
        raise RuntimeError("config store is down")

    monkeypatch.setattr(batch_gate, "load_config", boom)
    out = box.execute()
    _assert_ordinary_run(box, out)
    assert box.batch_lines() == []
    box.merge_landed.assert_called_once()


def test_a_fault_in_the_eligibility_rule_is_the_ordinary_run(box, monkeypatch):
    box.write_config()

    def boom(*_a, **_k):
        raise RuntimeError("rule is broken")

    monkeypatch.setattr(batch_gate, "eligible", boom)
    out = box.execute()
    _assert_ordinary_run(box, out)
    assert box.batch_lines() == []


# --- (j) what the return path leans on --------------------------------------------

def test_verify_and_report_are_never_inherited_by_a_later_run():
    # A returned task's next run must re-verify in full. That holds because a
    # resumed run inherits research/plan/implement only: the QUICK verify.md of
    # the earlier run can never stand in for the full one.
    assert "verify" not in stages.RESUMABLE
    assert "report" not in stages.RESUMABLE
    assert stages.RESUMABLE == ("research", "plan", "implement")


# --- (k) the run AFTER the integration gate sent the task back ---------------------
# dispatch() copies `verify_full` and `batch_return_reason` from the returned
# ledger row onto the next payload (test_batch_ready_state.py); these tests start
# from that payload. The first execute() is the quick-verified run that waited.

FIRST_RUN = "20200101T000000-batch-task"
SECOND_RUN = "20210101T000000-batch-task"


def _latest_run(box: _Sandbox) -> Path:
    [latest] = [p for p in box.runs.iterdir() if not p.name.startswith(("2020", "2021"))]
    return latest


def _rerun(box: _Sandbox, as_name: str, **payload):
    """Dispatch the task again. The run that just finished is filed under
    `as_name` first — two runs inside one second would share a run id — and the
    recorders are emptied, so what is read back is this run's alone."""
    _latest_run(box).rename(box.runs / as_name)
    box.calls.clear()
    box.prompts.clear()
    box.lines.clear()
    box.merge_landed.reset_mock()
    return box.execute(verify_full=True, **payload)


def _assert_full_verify_run(box: _Sandbox, out) -> None:
    """A returned task's run: never batched again, verified in full, merged."""
    assert out.ok, out.summary
    for stage, kwargs in box.calls:
        assert "batch_mode" not in kwargs, stage
        assert "Verify mode: QUICK" not in box.prompts[stage], stage
    assert box.batch_lines() == ["batch verify: off (returned to full verify)"]
    for key in BATCH_KEYS:
        assert key not in out.payload, key
    assert box.kw("report")["auto_merge"] is True
    box.merge_landed.assert_called_once()
    assert out.payload["review_required"] is False


def test_conflict_return_reruns_implement_to_merge_the_default_branch(box):
    box.write_config()
    _assert_batch_hold(box, box.execute())
    quick_tip = box.branch_tip()
    box.implement_text = "merged"          # the re-run implement makes a new commit

    out = _rerun(box, FIRST_RUN, batch_return_reason=batch_gate.RETURN_CONFLICT)

    _assert_full_verify_run(box, out)
    assert box.stages_run() == ["implement", "verify", "report"]
    assert f"resume: inheriting research+plan from {FIRST_RUN}" in box.lines
    assert ("batch return (conflict): implement re-runs to merge the default branch"
            in box.lines)
    # what the implement stage is told: merge, do not re-implement
    told = box.kw("implement")["retry_context"]
    assert "git merge origin/main" in told
    assert "Do NOT re-implement" in told and "never force-push" in told
    # …and NOT the quick verify.md, which passed and is no failure to fix
    assert "# verify" not in told and "overall_status" not in told
    assert "git merge origin/main" in box.prompts["implement"]
    assert box.kw("verify")["retry_context"] == ""
    # the commit it made is on origin before the full verify judges the branch
    assert box.branch_tip() != quick_tip
    assert _git(box.origin, "rev-parse", BRANCH) == box.branch_tip()


@pytest.mark.parametrize("reason", [
    batch_gate.RETURN_GATE_FAILED, batch_gate.RETURN_EXPIRED,
    batch_gate.RETURN_BRANCH_MOVED])
def test_other_returns_inherit_implement_and_only_verify_again(box, reason):
    box.write_config()
    box.execute()
    tip = box.branch_tip()

    out = _rerun(box, FIRST_RUN, batch_return_reason=reason)

    _assert_full_verify_run(box, out)
    assert box.stages_run() == ["verify", "report"]
    assert f"resume: inheriting research+plan+implement from {FIRST_RUN}" in box.lines
    assert not any(ln.startswith("batch return (conflict)") for ln in box.lines)
    assert box.branch_tip() == tip                       # nothing was implemented again


def test_a_failed_full_verify_after_a_conflict_return_is_what_the_next_run_fixes(box):
    # The payload still says `conflict` (dispatch carries it until the task is
    # done), but the latest verdict is now a real FAIL: that diagnosis is the
    # retry context, and nothing of the failed run is inherited.
    box.write_config()
    box.execute()
    box.implement_text = "merged"
    box.artifacts["verify"] = VERIFY_FAIL
    second = _rerun(box, FIRST_RUN, batch_return_reason=batch_gate.RETURN_CONFLICT)
    assert not second.ok and second.payload["verify"] == "FAIL"
    assert box.stages_run() == ["implement", "verify"]

    box.implement_text = "fixed"
    box.artifacts["verify"] = VERIFY_PASS
    third = _rerun(box, SECOND_RUN, batch_return_reason=batch_gate.RETURN_CONFLICT)

    _assert_full_verify_run(box, third)
    assert box.stages_run() == list(stages.STAGES_AUTO)
    told = box.kw("implement")["retry_context"]
    assert f"(from previous run `{SECOND_RUN}`)" in told
    assert "overall_status: fail" in told
    assert "git merge origin/main" not in told
    assert not any(ln.startswith("batch return (conflict)") for ln in box.lines)
    assert not any(ln.startswith("resume: inheriting") for ln in box.lines)


def test_a_conflict_return_cut_short_after_implement_is_not_implemented_again(box):
    box.write_config()
    box.execute()
    box.implement_text = "merged"
    _rerun(box, FIRST_RUN, batch_return_reason=batch_gate.RETURN_CONFLICT)
    merged_tip = box.branch_tip()
    # Make that run one that died in verify: implement is recorded and pushed,
    # verify left no verdict.
    second = _latest_run(box)
    progress = json.loads((second / stages.STAGE_PROGRESS).read_text(encoding="utf-8"))
    progress["completed"] = [s for s in progress["completed"]
                             if s not in ("verify", "report")]
    (second / stages.STAGE_PROGRESS).write_text(json.dumps(progress), encoding="utf-8")
    (second / stages.STAGE_ARTIFACT["verify"]).unlink()
    (second / stages.STAGE_ARTIFACT["report"]).unlink()

    third = _rerun(box, SECOND_RUN, batch_return_reason=batch_gate.RETURN_CONFLICT)

    _assert_full_verify_run(box, third)
    assert box.stages_run() == ["verify", "report"]      # the merge is not done twice
    assert f"resume: inheriting research+plan+implement from {SECOND_RUN}" in box.lines
    assert box.branch_tip() == merged_tip


def test_conflict_context_is_only_for_a_conflict_and_fails_open(box, monkeypatch):
    executor = StagedExecutor()
    conflict = {batch_gate.KEY_RETURN_REASON: batch_gate.RETURN_CONFLICT}
    note = executor._batch_conflict_context(conflict, "", box.clone)
    assert "git merge origin/main" in note and "{" not in note
    assert executor._batch_conflict_context(conflict, VERIFY_PASS, box.clone) == note
    assert executor._batch_conflict_context(conflict, VERIFY_FAIL, box.clone) == ""
    assert executor._batch_conflict_context({}, "", box.clone) == ""
    assert executor._batch_conflict_context(None, "", box.clone) == ""
    assert executor._batch_conflict_context(
        {batch_gate.KEY_RETURN_REASON: batch_gate.RETURN_GATE_FAILED}, "", box.clone) == ""

    def boom(*_a, **_k):
        raise RuntimeError("git is broken")

    monkeypatch.setattr(stages, "default_branch", boom)
    assert executor._batch_conflict_context(conflict, "", box.clone) == ""
