"""Golden/snapshot of ``stages.build_stage_argv`` — the byte-identical safety net
(agent-backends leaf ①).

The claude-stage argv is the thing the agent-backend refactor (leaf ②/③) must
keep **byte-identical** (design docs/design/agent-backends.md §4). Until this
file, nothing pinned the assembled argv: ``build_stage_argv`` was only ever used
through a ``mock(return_value=["true"])`` in ``test_claude_accounts_switch.py``,
so a refactor could have silently reordered a flag or dropped ``--add-dir`` and
every test would still have been green. This snapshot catches that.

What it pins, for the representative stages and both the mcp-on/off and
usage-on/off shapes:

* the flag **skeleton and order** —
  ``claude -p <prompt> --model <m> --effort <e> --max-turns <N>``
  ``[--mcp-config <json> --strict-mcp-config]? [--session-id <uuid>]?``
  ``--allowed-tools <tools…> [--add-dir <wr>]?`` — with the environment-dependent
  values (model/effort, the session-id uuid, the mcp-config json) normalised to
  placeholders so the test pins *structure*, not a machine's resolved model;
* the full literal argv for two stages (``report``, ``implement``
  with the richest tool set + mcp), so a change to the tool list or turn budget
  is a conscious golden update, not a silent drift;
* the prompt body: the stage prompt passes through verbatim and the run-metadata
  block is appended after it (so the refactor cannot alter what claude reads).

The refactor's *direct* byte-identical guarantee — ``ClaudeAgentBackend`` emits
the same argv as ``build_stage_argv`` — is in ``test_agent_backend.py``; this
file is the independent snapshot the backend is compared against.

No ``claude`` binary runs: ``build_stage_argv`` is pure. Volatile inputs
(model/effort, worker_root existence, the mcp server path, the usage kill switch)
are fixed per test. No personal identifier appears here (the engine tree is
scanned by the public-identifier gate).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from todo_system.orchestrator import stages
from todo_system.orchestrator import rescope

# Representative stages: the richest write stage, verify's fallback-runner set,
# report (no browser mcp), the two single-stage lanes.
_STAGES = ("implement", "verify", "report", "quick", "delegate")

_MODEL = "MODEL-SENTINEL"
_EFFORT = "EFFORT-SENTINEL"


def _fix(monkeypatch, tmp_path, *, mcp: bool, usage: bool, wr_exists: bool) -> Path:
    """Pin every environment-dependent input of build_stage_argv so its argv is
    deterministic, and return the worker_root to pass in."""
    monkeypatch.setattr(stages, "stage_model", lambda stage="implement", tier=None: _MODEL)
    monkeypatch.setattr(stages, "stage_effort", lambda stage="implement", tier=None: _EFFORT)
    if mcp:
        server = tmp_path / "mcp" / "server.mjs"
        server.parent.mkdir(parents=True, exist_ok=True)
        server.write_text("// stub\n", encoding="utf-8")
        # Mirror the real browser_mcp_server gating: a configured server is still
        # only handed to BROWSER_MCP_STAGES, so report stays mcp-off even
        # with a server present.
        monkeypatch.setattr(
            stages, "browser_mcp_server",
            lambda stage: server if stage in stages.BROWSER_MCP_STAGES else None)
    else:
        monkeypatch.setattr(stages, "browser_mcp_server", lambda stage: None)
    monkeypatch.setenv(stages.USAGE_ENV, "1" if usage else "0")
    wr = tmp_path / "worker_root"
    if wr_exists:
        wr.mkdir()
    return wr


def _argv(stage: str, wr: Path, **extra) -> list[str]:
    return stages.build_stage_argv(
        stage, task_id="slug", run_id="RID", run_dir=Path("/rd"),
        branch="worker/slug", worker_root=wr, **extra)


def _normalise(argv: list[str]) -> list[str]:
    """Replace the three environment-dependent *values* (the prompt body, the
    session-id uuid, the mcp-config json) with stable placeholders. Model/effort
    are already sentinels via ``_fix``. The flag tokens themselves are untouched,
    so order and presence stay pinned."""
    out = list(argv)
    out[2] = "<PROMPT>"
    for i, tok in enumerate(out):
        if tok == stages.SESSION_ID_FLAG and i + 1 < len(out):
            out[i + 1] = "<SID>"
        elif tok == "--mcp-config" and i + 1 < len(out):
            out[i + 1] = "<MCP_CONFIG>"
    return out


def _expected(stage: str, wr: Path, *, mcp: bool, usage: bool, wr_exists: bool) -> list[str]:
    """The pinned skeleton, built from the module's own stable constants
    (max-turns, the tool list) but with the exact flag order spelled out here —
    the order is the invariant the refactor must not touch."""
    exp = ["claude", "-p", "<PROMPT>",
           "--model", _MODEL, "--effort", _EFFORT,
           "--max-turns", str(stages.STAGE_MAX_TURNS[stage])]
    if mcp:
        exp += ["--mcp-config", "<MCP_CONFIG>", "--strict-mcp-config"]
    if usage:
        exp += [stages.SESSION_ID_FLAG, "<SID>"]
    exp += ["--allowed-tools", *stages._safe_tools(stage, None, browser_mcp=mcp)]
    if wr_exists:
        exp += ["--add-dir", str(wr)]
    return exp


# ---------------------------------------------------------------------------
# Structural skeleton — every representative stage × mcp × usage.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stage", _STAGES)
@pytest.mark.parametrize("mcp", [False, True], ids=["mcp_off", "mcp_on"])
@pytest.mark.parametrize("usage", [True, False], ids=["usage_on", "usage_off"])
def test_argv_skeleton_is_pinned(monkeypatch, tmp_path, stage, mcp, usage):
    wr = _fix(monkeypatch, tmp_path, mcp=mcp, usage=usage, wr_exists=True)
    argv = _argv(stage, wr)
    # report is not a browser-mcp stage, so mcp flags never appear there
    # even when a server is "configured" — browser_mcp_server returns None for
    # it regardless of the monkeypatch, so the skeleton must reflect that.
    mcp_effective = mcp and stage in stages.BROWSER_MCP_STAGES
    assert _normalise(argv) == _expected(
        stage, wr, mcp=mcp_effective, usage=usage, wr_exists=True)


@pytest.mark.parametrize("stage", _STAGES)
def test_add_dir_omitted_when_worker_root_absent(monkeypatch, tmp_path, stage):
    wr = _fix(monkeypatch, tmp_path, mcp=False, usage=True, wr_exists=False)
    argv = _argv(stage, wr)
    assert "--add-dir" not in argv
    assert _normalise(argv) == _expected(
        stage, wr, mcp=False, usage=True, wr_exists=False)


# ---------------------------------------------------------------------------
# Full literal snapshots — a tool-list / turn-budget change is a conscious update.
# ---------------------------------------------------------------------------

def test_literal_argv_report_mcp_off_usage_on(monkeypatch, tmp_path):
    wr = _fix(monkeypatch, tmp_path, mcp=False, usage=True, wr_exists=True)
    argv = _argv("report", wr)
    assert _normalise(argv) == [
        "claude", "-p", "<PROMPT>",
        "--model", _MODEL, "--effort", _EFFORT, "--max-turns", "40",
        stages.SESSION_ID_FLAG, "<SID>",
        "--allowed-tools", "Read", "Write", "Edit", "Bash(git:*)", "Bash(gh:*)",
        "--add-dir", str(wr),
    ]


def test_literal_argv_implement_mcp_on_usage_on(monkeypatch, tmp_path):
    wr = _fix(monkeypatch, tmp_path, mcp=True, usage=True, wr_exists=True)
    argv = _argv("implement", wr)
    assert _normalise(argv) == [
        "claude", "-p", "<PROMPT>",
        "--model", _MODEL, "--effort", _EFFORT, "--max-turns", "250",
        "--mcp-config", "<MCP_CONFIG>", "--strict-mcp-config",
        stages.SESSION_ID_FLAG, "<SID>",
        "--allowed-tools",
        "Read", "Edit", "Write", "Grep", "Glob", "Bash(ls:*)", "Bash(grep:*)",
        "Bash(rg:*)", "Bash(find:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)",
        "Bash(wc:*)", "Bash(pwd:*)", "Bash(git:*)", "Bash(gradlew:*)",
        "Bash(./gradlew:*)", "Bash(uv:*)", "Bash(pytest:*)", "Bash(npm:*)",
        "Bash(pnpm:*)", "Bash(node:*)", "Bash(npx:*)", "Bash(flutter:*)",
        "Bash(dart:*)", "Bash(macp-sms:*)", "Bash(macp-browser:*)",
        "Bash(macp-remote-gate:*)", "Bash(macp-emulator:*)", "Bash(macp-mail:*)",
        "mcp__macp-browser__*",
        "--add-dir", str(wr),
    ]


# ---------------------------------------------------------------------------
# The variadic-flag ordering invariant, stated explicitly (design §4).
# ---------------------------------------------------------------------------

def test_variadic_flag_order_mcp_before_session_before_allowed_tools(monkeypatch, tmp_path):
    wr = _fix(monkeypatch, tmp_path, mcp=True, usage=True, wr_exists=True)
    argv = _argv("implement", wr)
    i_mcp = argv.index("--mcp-config")
    i_strict = argv.index("--strict-mcp-config")
    i_sid = argv.index(stages.SESSION_ID_FLAG)
    i_tools = argv.index("--allowed-tools")
    i_add = argv.index("--add-dir")
    # mcp pair → session-id → allowed-tools → add-dir, strictly increasing, and
    # --add-dir is dead last (nothing follows its single value but the path).
    assert i_mcp < i_strict < i_sid < i_tools < i_add
    assert i_add == len(argv) - 2  # --add-dir, <path>


def test_session_id_absent_and_unparseable_when_usage_off(monkeypatch, tmp_path):
    wr = _fix(monkeypatch, tmp_path, mcp=True, usage=False, wr_exists=True)
    argv = _argv("implement", wr)
    assert stages.SESSION_ID_FLAG not in argv
    assert stages.argv_session_id(argv) is None


def test_session_id_is_a_fresh_uuid_per_call(monkeypatch, tmp_path):
    wr = _fix(monkeypatch, tmp_path, mcp=False, usage=True, wr_exists=True)
    a = _argv("implement", wr)
    b = _argv("implement", wr)
    sid_a, sid_b = stages.argv_session_id(a), stages.argv_session_id(b)
    assert sid_a is not None and sid_b is not None and sid_a != sid_b
    uuid.UUID(sid_a)  # canonical uuid, else argv_session_id would have returned None


# ---------------------------------------------------------------------------
# The prompt body (argv[2]) and the mcp-config json content.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stage", _STAGES)
def test_prompt_body_is_stage_prompt_plus_metadata(monkeypatch, tmp_path, stage):
    wr = _fix(monkeypatch, tmp_path, mcp=False, usage=True, wr_exists=True)
    full = _argv(stage, wr)[2]
    assert full.startswith(stages.strip_frontmatter(stages.load_stage_prompt(stage)))
    assert "# Run metadata (injected by orchestrator)" in full
    assert "- run_id: `RID`" in full
    assert "- slug: `slug`" in full
    assert "- stage: `" + stage + "`" in full
    assert "allowed tools (authoritative, overrides any list in the prompt" in full


def test_prompt_body_carries_external_repo_metadata(monkeypatch, tmp_path):
    wr = _fix(monkeypatch, tmp_path, mcp=False, usage=True, wr_exists=True)
    full = _argv("implement", wr, repo="https://example.test/x.git",
                 working_dir="/w/d")[2]
    assert "- repo: `https://example.test/x.git`" in full
    assert "- working_dir: `/w/d`" in full
    assert "**External repo task**" in full


def test_mcp_config_json_names_server_and_run_id_owner(monkeypatch, tmp_path):
    wr = _fix(monkeypatch, tmp_path, mcp=True, usage=True, wr_exists=True)
    argv = _argv("implement", wr)
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    srv = cfg["mcpServers"][stages.BROWSER_MCP_SERVER_NAME]
    assert srv["env"][stages.BROWSER_OWNER_ENV] == "RID"
    assert srv["args"] and srv["args"][0].endswith("server.mjs")


# ---------------------------------------------------------------------------
# Implement stage (it writes the plan): the machine-readable split block (rescope.rescope_from_split_block)
# is documented on the metadata.
# ---------------------------------------------------------------------------

# The hint's own heading. Not the bare phrase "machine-readable split": the
# implement and verify prompts already use that for the rescope.json they write.
_SPLIT_MARK = "**Oversize plan → machine-readable split**"
_THREE_PHASE_PLAN = (
    "# Implementation Plan\n\n## Phases\n\n### Phase 1: first\n\n### Phase 2: second\n\n"
    "### Phase 3: third\n\n## Rollback plan\n\nDelete the branch.\n")


def test_plan_prompt_split_example_is_what_the_engine_parses(monkeypatch, tmp_path):
    # The example the implement agent is shown is fed to the engine's own reader and
    # builder, so the prompt and the parser cannot drift apart.
    wr = _fix(monkeypatch, tmp_path, mcp=False, usage=True, wr_exists=True)
    full = _argv("implement", wr)[2]
    assert _SPLIT_MARK in full
    blocks = rescope.find_plan_split_blocks(full)
    assert len(blocks) == 1                    # one example, and it reads as top-level
    example = json.loads(blocks[0])["subtasks"]
    assert set(example[0]) == {"slug", "phases", "acceptance_criteria", "touches"}
    # ... and the second one shows every optional key the rules name
    assert set(example[1]) == set(example[0]) | {
        "depends_on", "title", "outcome", "model_tier", "estimate_p50", "estimate_p90"}

    plan, why = rescope.rescope_from_split_block(
        blocks, _THREE_PHASE_PLAN, {"auto_merge": True}, parent_slug="slug", run_id="RID")
    assert plan is not None, why
    assert [s["slug"] for s in plan["subtasks"]] == ["slug-1-first-part", "slug-2-second-part"]
    assert [s["phases"] for s in plan["subtasks"]] == [[1, 2], [3]]
    assert rescope.can_auto_rescope(plan, {"auto_merge": True}).allow


def test_split_hint_names_every_key_the_builder_reads(monkeypatch, tmp_path):
    wr = _fix(monkeypatch, tmp_path, mcp=False, usage=True, wr_exists=True)
    full = _argv("implement", wr)[2]
    hint = full[full.index(_SPLIT_MARK):]
    for key in ("declares_oversize: yes", "`rescope`", "`slug`", "`phases`",
                "`acceptance_criteria`", "`touches`", "`depends_on`", "`title`",
                "`outcome`", "`model_tier`", "heavy|standard|light", "Todo/items/"):
        assert key in hint, key
    assert hint.endswith("\n")


@pytest.mark.parametrize("stage", [s for s in _STAGES if s != "implement"])
def test_split_hint_is_implement_only(monkeypatch, tmp_path, stage):
    wr = _fix(monkeypatch, tmp_path, mcp=False, usage=True, wr_exists=True)
    full = _argv(stage, wr)[2]
    assert _SPLIT_MARK not in full
    assert "```rescope" not in full


def test_split_hint_sits_in_the_metadata_block(monkeypatch, tmp_path):
    # Appended to the run metadata: the stage prompt passes through verbatim, the
    # hint comes after it (so it outranks it) and before the project memory.
    wr = _fix(monkeypatch, tmp_path, mcp=False, usage=True, wr_exists=True)
    prompt = stages.strip_frontmatter(stages.load_stage_prompt("implement"))
    full = _argv("implement", wr, project_context="PROJECT-MEMORY-SENTINEL")[2]
    assert full.startswith(prompt)
    assert _SPLIT_MARK not in prompt
    head = "# Run metadata (injected by orchestrator)"
    assert full.index(head) < full.index(_SPLIT_MARK) < full.index("PROJECT-MEMORY-SENTINEL")
