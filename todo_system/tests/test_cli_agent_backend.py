"""``GeminiAgentBackend`` — the first non-claude CLI backend (agent-backends
leaf ⑤).

Pins the argv/env the backend hands a mock CLI (no real ``gemini`` binary runs —
``stages.run_stage_subprocess`` is captured): the account's ``command`` template
becomes the argv with the full prompt substituted and NO claude flags
(``--model``/``--effort``/``--session-id``/``--allowed-tools`` never appear); the
prompt is the stage prompt + a vendor-NEUTRAL run-metadata block (no claude
tool-allowlist lines); ``GEMINI_MODEL`` carries the model (account.env, or the
account's tier pin). Plus ``classify_exit`` 3-way, ``supports`` (6 of 8), and
``report_usage`` from ``--output-format json`` stats (sidecar; detailed tests in
``test_agent_backend.py``).

No personal identifier appears here (the engine tree is scanned by the
public-identifier gate).
"""

from __future__ import annotations

import contextlib
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from todo_system.macp import config as C
from todo_system.orchestrator import agent_backend as ab
from todo_system.orchestrator import message as m
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator import solar_stage
from todo_system.orchestrator import stages
from todo_system.orchestrator.registry import Agent
from todo_system.orchestrator.runner import StagedExecutor

_KW = dict(task_id="slug", run_id="RID", run_dir=Path("/rd"), branch="worker/slug")

_CLAUDE_FLAGS = ("--model", "--effort", "--session-id", "--allowed-tools",
                 "--max-turns", "--mcp-config", "--add-dir")


def _gemini_account(**over):
    kw = dict(name="gem", agent="gemini", command="gemini --yolo -p {prompt}")
    kw.update(over)
    return C.Account(**kw)


def _capture(monkeypatch):
    seen = {}

    def cap(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(stages, "run_stage_subprocess", cap)
    return seen


def test_gemini_argv_is_the_account_command_with_prompt_and_no_claude_flags(monkeypatch):
    seen = _capture(monkeypatch)
    acct = _gemini_account(env={"GEMINI_MODEL": "gemini-2.5-flash"})
    r = ab.GeminiAgentBackend().run_stage(
        "quick", account=acct, working_dir="/w/d", env={"PATH": "/usr/bin"},
        timeout=60, **_KW)
    argv = seen["argv"]
    assert argv[:3] == ["gemini", "--yolo", "-p"]
    full = argv[3]
    assert full.startswith(stages.strip_frontmatter(stages.load_stage_prompt("quick")))
    for flag in _CLAUDE_FLAGS:
        assert flag not in argv, flag
    assert "claude" not in argv
    assert r.args is argv and r.returncode == 0


def test_gemini_env_merges_base_with_account_env(monkeypatch):
    seen = _capture(monkeypatch)
    acct = _gemini_account(env={"GEMINI_MODEL": "from-account-env"})
    ab.GeminiAgentBackend().run_stage(
        "quick", account=acct, env={"PATH": "/usr/bin", "GEMINI_MODEL": "base"}, **_KW)
    env = seen["kw"]["env"]
    assert env["PATH"] == "/usr/bin"                 # base preserved
    assert env["GEMINI_MODEL"] == "from-account-env"  # account.env wins over base


def test_gemini_tier_model_pin_overrides_env(monkeypatch):
    seen = _capture(monkeypatch)
    acct = _gemini_account(env={"GEMINI_MODEL": "from-account-env"},
                           models={"light": "gemini-light-pin"})
    ab.GeminiAgentBackend().run_stage("quick", account=acct, tier="light",
                                      env={}, **_KW)
    assert seen["kw"]["env"]["GEMINI_MODEL"] == "gemini-light-pin"
    # a tier the account does not pin leaves account.env's value
    seen2 = _capture(monkeypatch)
    ab.GeminiAgentBackend().run_stage("quick", account=acct, tier="heavy",
                                      env={}, **_KW)
    assert seen2["kw"]["env"]["GEMINI_MODEL"] == "from-account-env"


def test_gemini_forwards_cwd_working_dir_and_timeout(monkeypatch):
    seen = _capture(monkeypatch)
    acct = _gemini_account()
    ab.GeminiAgentBackend().run_stage("quick", account=acct, working_dir="/w/d",
                                      timeout=321, **_KW)
    assert seen["kw"]["cwd"] == "/w/d"   # falls back to working_dir when cwd unset
    assert seen["kw"]["timeout"] == 321
    seen2 = _capture(monkeypatch)
    ab.GeminiAgentBackend().run_stage("quick", account=acct, working_dir="/w/d",
                                      cwd="/explicit", **_KW)
    assert seen2["kw"]["cwd"] == "/explicit"  # explicit cwd wins


def test_gemini_prompt_has_neutral_meta_not_claude_tool_lines(monkeypatch):
    seen = _capture(monkeypatch)
    ab.GeminiAgentBackend().run_stage(
        "quick", account=_gemini_account(), repo="https://example.test/x.git",
        working_dir="/w/d", **_KW)
    full = seen["argv"][3]
    assert "# Run metadata (injected by orchestrator)" in full
    assert "- run_id: `RID`" in full and "- slug: `slug`" in full
    assert "- repo: `https://example.test/x.git`" in full
    assert "- working_dir: `/w/d`" in full
    # NO claude-specific allowlist / wrapper lines leak into a gemini prompt.
    assert "allowed tools (authoritative" not in full
    assert "macp-browser" not in full


@pytest.mark.parametrize("rc,stdout,expected", [
    (0, "edited files, done", ab.EXIT_SUCCESS),
    (1, "HTTP status 429: too many requests", ab.EXIT_EXHAUSTED),
    (1, "RESOURCE_EXHAUSTED: quota exceeded", ab.EXIT_EXHAUSTED),
    (53, "Turn limit exceeded", ab.EXIT_ERROR),   # turn cap, not a quota wall
    (1, "some other failure", ab.EXIT_ERROR),
    (0, "", ab.EXIT_SUCCESS),
])
def test_gemini_classify_exit_three_way(rc, stdout, expected):
    assert ab.GeminiAgentBackend().classify_exit(rc, stdout) == expected


def test_gemini_supports_six_of_eight():
    g = ab.GeminiAgentBackend()
    assert {c for c in ab.CAPABILITIES if g.supports(c)} == {
        "headless", "tool_allowlist", "turn_cap", "model_select",
        "usage_report", "exit_quota"}
    assert not g.supports("effort")      # claude-only concept
    assert not g.supports("add_dir")     # no confirmed --add-dir equivalent


def test_gemini_report_usage_without_json_handle_records_nothing(monkeypatch):
    # no --output-format json on the argv → no stats channel → None (inert),
    # the mirror of claude's stub argv without --session-id.
    monkeypatch.setenv(stages.USAGE_ENV, "1")
    assert ab.GeminiAgentBackend().report_usage("quick", Path("/rd"), ["gemini"]) is None


def test_gemini_report_usage_json_handle_is_never_none(monkeypatch):
    monkeypatch.setenv(stages.USAGE_ENV, "1")
    rec = ab.GeminiAgentBackend().report_usage(
        "quick", Path("/rd"), ["gemini", "-p", "x", "--output-format", "json"])
    assert rec == {"unavailable": "gemini stats not captured"}


def test_gemini_satisfies_the_protocol():
    assert isinstance(ab.GeminiAgentBackend(), ab.AgentBackend)


def test_gemini_runs_the_read_only_stage_capability_check_cleanly():
    # Gemini declares tool_allowlist + headless, so the capability gate would pass
    # it for a read-only stage too — it is leaf ⑥ routing (quick only) that limits
    # where it actually runs, not a missing capability.
    g = ab.GeminiAgentBackend()
    assert ab.missing_capabilities(g, "quick") == []
    assert ab.missing_capabilities(g, "research") == []   # has tool_allowlist


# ---- end-to-end routing: an `agent: gemini` quick task completes (AC #2) ----

def test_gemini_quick_task_routes_to_gemini_backend_and_completes(tmp_path):
    """Drive a full VAULT quick run through StagedExecutor with the dispatch
    machinery stubbed (mirrors test_solar_stage's ExecuteRoutingTest): an
    `agent: gemini` quick task runs via GeminiAgentBackend (a `gemini …` argv
    reaches the mock runner, NOT a claude argv, and solar_stage.run is untouched),
    the mock runner writes quick.md, and the run completes."""
    runs = Path(tempfile.mkdtemp(dir=tmp_path))

    def _run_dir() -> Path:
        dirs = [p for p in runs.iterdir() if p.is_dir()]
        assert len(dirs) == 1, dirs
        return dirs[0]

    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        (_run_dir() / stages.STAGE_ARTIFACT["quick"]).write_text(
            "---\noverall_status: pass\n---\n# quick\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "gemini stage stdout", "")

    solar_run = mock.Mock()
    vp = subprocess.CompletedProcess([], 0, "", "")
    vp.problem = None
    vp.orphaned_commits = None
    gemini_acct = C.Account(name="gem", agent="gemini", command="gemini --yolo -p {prompt}")
    msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="t",
                     payload={"worker_mode": "quick", "agent": "gemini", "auto_merge": True})
    with contextlib.ExitStack() as stack:
        for p in (
            mock.patch.object(stages, "restore_main"),
            mock.patch.object(stages, "install_push_guard"),
            mock.patch.object(stages, "default_branch", return_value="main"),
            mock.patch.object(stages, "prepare_vault_branch", return_value=vp),
            mock.patch.object(stages, "remote_branch_exists", return_value=False),
            mock.patch.object(stages, "RUNS_DIR", runs),
            mock.patch.object(stages, "cleanup_build_daemons",
                              return_value={"ran": False, "note": ""}),
            # a safety net: if routing WRONGLY picked claude, the captured argv
            # would start with "claude" and the assertion below fails fast (no
            # real model probe). The gemini path does not call build_stage_argv.
            mock.patch.object(stages, "build_stage_argv", return_value=["claude", "-p", "stub"]),
            mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run),
            mock.patch.object(solar_stage, "run", solar_run),
            mock.patch.object(stages, "publish_branch",
                              return_value={"pr_url": None, "note": ""}),
            mock.patch.object(stages, "merge_pr",
                              return_value={"merged": True, "note": ""}),
            mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")),
            mock.patch.object(StagedExecutor, "_solar_account", return_value=None),
            mock.patch.object(StagedExecutor, "_gemini_account", return_value=gemini_acct),
            mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
            mock.patch.object(StagedExecutor, "_acquire_checkout_lease", return_value=None),
            mock.patch.object(StagedExecutor, "_pipeline_for", return_value=("quick",)),
            mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
            mock.patch("todo_system.worker.runner_heartbeat_start"),
        ):
            stack.enter_context(p)
        out = StagedExecutor().execute(msg, _AGENT, progress=lambda _s: None)
    # the GEMINI backend ran the stage (a `gemini …` argv), not claude …
    assert captured["argv"][0] == "gemini", captured["argv"]
    assert "claude" not in captured["argv"]
    # … and the Solar HTTP path was never touched.
    solar_run.assert_not_called()
    # the quick stage completed and was recorded
    assert "quick" in out.payload.get("stages", []), out.summary
    assert (_run_dir() / "quick.md").exists()


_AGENT = Agent(id="w", kind="ai-local", capabilities=["git"], repo="~/Worker/x")


def test_neutral_run_metadata_omits_external_note_for_vault_task():
    vault = ab.neutral_run_metadata(run_id="R", task_id="t", run_dir=Path("/rd"),
                                    branch="b", stage="quick", auto_merge=True)
    assert "External repo task" not in vault and "- repo:" not in vault
    ext = ab.neutral_run_metadata(run_id="R", task_id="t", run_dir=Path("/rd"),
                                  branch="b", stage="quick", auto_merge=False,
                                  repo="r", working_dir="/w")
    assert "External repo task" in ext and "- working_dir: `/w`" in ext
    assert "- auto_merge: `false`" in ext
