"""``GeminiAgentBackend`` — the first non-claude CLI backend (agent-backends
leaf ⑤).

Pins the argv/env the backend hands a mock CLI (no real ``gemini`` binary runs —
``stages.run_stage_subprocess`` is captured): the account's ``command`` template
becomes the argv with the full prompt substituted and NO claude flags
(``--model``/``--effort``/``--session-id``/``--allowed-tools`` never appear); the
prompt is the stage prompt + a vendor-NEUTRAL run-metadata block (no claude
tool-allowlist lines); ``GEMINI_MODEL`` carries the model (account.env, or the
account's tier pin). Plus ``classify_exit`` 3-way, ``supports`` (6 of 8), and
``report_usage`` None.

No personal identifier appears here (the engine tree is scanned by the
public-identifier gate).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from todo_system.macp import config as C
from todo_system.orchestrator import agent_backend as ab
from todo_system.orchestrator import stages

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


def test_gemini_report_usage_is_none_for_now():
    assert ab.GeminiAgentBackend().report_usage("quick", Path("/rd"), ["gemini"]) is None


def test_gemini_satisfies_the_protocol():
    assert isinstance(ab.GeminiAgentBackend(), ab.AgentBackend)


def test_gemini_runs_the_read_only_stage_capability_check_cleanly():
    # Gemini declares tool_allowlist + headless, so the capability gate would pass
    # it for a read-only stage too — it is leaf ⑥ routing (quick only) that limits
    # where it actually runs, not a missing capability.
    g = ab.GeminiAgentBackend()
    assert ab.missing_capabilities(g, "quick") == []
    assert ab.missing_capabilities(g, "research") == []   # has tool_allowlist


def test_neutral_run_metadata_omits_external_note_for_vault_task():
    vault = ab.neutral_run_metadata(run_id="R", task_id="t", run_dir=Path("/rd"),
                                    branch="b", stage="quick", auto_merge=True)
    assert "External repo task" not in vault and "- repo:" not in vault
    ext = ab.neutral_run_metadata(run_id="R", task_id="t", run_dir=Path("/rd"),
                                  branch="b", stage="quick", auto_merge=False,
                                  repo="r", working_dir="/w")
    assert "External repo task" in ext and "- working_dir: `/w`" in ext
    assert "- auto_merge: `false`" in ext
