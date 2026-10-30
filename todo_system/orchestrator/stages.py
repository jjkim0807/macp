"""v2 staged execution — port of the legacy worker's 5-stage pipeline
(research → plan → implement → verify → report) into the orchestrator.

The intelligence lives in the SAME stage prompts the legacy worker uses
(.claude/commands/worker-<stage>.md): implement edits code, verify runs the
project's own checks locally (GitHub Actions is not used) and writes
overall_status, report opens the PR via gh and merges on that verdict. This
module only reproduces the light orchestration worker.py did inline — build the
per-stage `claude -p` command, run stages in order, require each stage's
artifact, and gate success on the verify verdict — so the v2 runner can own
execution with the same quality gates (not the single-shot BackgroundExecutor).

Constants mirror worker.py; kept in sync deliberately. Stdlib-only.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import shutil
import signal
import subprocess
import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from .. import paths

# Two roots, split the way beta_publish._ENGINE_ROOT / _VAULT_DATA_ROOT are.
# _ENGINE_ROOT comes from paths.engine_root() (the repo root in a development
# checkout, todo_system/_engine in an installed wheel) and is immune to
# MACP_VAULT_ROOT: it anchors .claude/commands (the stage prompts that ship with
# the engine). _VAULT_DATA_ROOT resolves through the vault resolver so a
# redirected vault is read from the right place; anchored at
# paths.vault_search_start() — the engine root in a checkout, byte-identical to
# _ENGINE_ROOT when the env is unset (the live config); None in an installed
# wheel, where the cwd-upward search replaces a site-packages anchor.
# It anchors Worker/runs — the stage artifacts, which the inventory
# (Notes/macp-public-split-inventory.md §2.1) classifies as vault-data and which
# metrics.RUNS already reads from the vault (PR #168). Routing RUNS_DIR through it
# is what closes the stages↔metrics split-brain on Worker/runs. paths is
# stdlib-only, so stages stays stdlib-only at import (model_select stays function-
# local).
_ENGINE_ROOT = paths.engine_root()
_VAULT_DATA_ROOT = paths.vault_root(start=paths.vault_search_start())
COMMANDS_DIR = _ENGINE_ROOT / ".claude" / "commands"
# Worker/runs = stage artifacts; vault-data, redirected via _VAULT_DATA_ROOT to
# match metrics.RUNS (PR #168) — NOT engine-immune. (Was pinned to the engine root
# before this leaf; that was the stages-vs-metrics split-brain the outcome closes.)
RUNS_DIR = _VAULT_DATA_ROOT / "Worker" / "runs"

# Model + effort for the code stages. Until 2026-09-10 every stage of every
# task ran the heaviest model at max effort ("ultra" coding); now the task's
# difficulty/type picks a tier (model_select.classify_tier — heavy / standard /
# light), each tier has its own model ladder and effort, and a mechanical stage
# runs one notch below the task's tier (STAGE_TIER_SHIFT). Retune there.
#
# Resolved per call, NOT at import. `runner --daemon` is a long-lived process:
# an import-time constant froze whatever model was resolved at process start, so
# a model change (or a quota that came back) took effect only after a manual
# daemon restart. Observed 2026-07-31 — the runner held an out-of-credits Fable 5
# for hours after the fallback had been fixed and merged. resolve_model() keeps
# its own 24h cache, so calling it per stage is cheap.
STAGE_FALLBACK_MODEL = "claude-opus-4-8"
STAGE_EFFORT = "max"          # fallback only; per-tier effort is model_select.TIER_EFFORT
DEFAULT_STAGE_TIER = "heavy"  # what a caller gets when it names no tier

# The stage-TYPE dimension: how many rungs below the task's tier a stage runs.
# report is push + `gh pr create` + wait for CI + write report.md — mechanical
# work that does not need the model that wrote the code. Everything else runs
# at the task's tier: research/plan/implement produce the work, verify judges it.
STAGE_TIER_SHIFT = {"report": 1}


def effective_tier(stage: str, tier: str | None = None) -> str:
    """The tier a given stage of a task on `tier` actually runs at."""
    from .model_select import shift_tier
    return shift_tier(tier or DEFAULT_STAGE_TIER, STAGE_TIER_SHIFT.get(stage, 0))


def stage_model(stage: str = "implement", tier: str | None = None) -> str:
    """The model id for a stage invocation, resolved fresh (cached 24h per
    tier). With no tier this is the heavy ladder — the pre-tier behaviour."""
    try:
        from .model_select import resolve_model
        return resolve_model(tier=effective_tier(stage, tier))
    except Exception:
        return STAGE_FALLBACK_MODEL


def stage_effort(stage: str = "implement", tier: str | None = None) -> str:
    """`--effort` for a stage invocation, per the effective tier."""
    try:
        from .model_select import TIER_EFFORT
        return TIER_EFFORT.get(effective_tier(stage, tier), STAGE_EFFORT)
    except Exception:
        return STAGE_EFFORT


# Per-stage subprocess timeout (seconds), sized to the task's difficulty tier.
# Until now every stage of every task got a flat 3600 (StagedExecutor.stage_timeout).
# A heavy task whose implement legitimately needs longer got SIGKILLed mid-stage:
# TimeoutExpired escaped the try/finally, the finally's restore_main walked HEAD
# back to main, and the NEXT dispatch's branch prep rewound the unpushed commits
# — the work was cut AND then erased (mulkko-32-dag-migration-v17, 2026-09-13).
# heavy gets more head-room; standard/light keep the historical 3600.
#
# 2026-10-18 (user decision): standard 3600→7200, heavy 5400→10800. Android
# Verify stages on the 7.7 GB HQ kept dying short of the finish line — not from
# bad code but from `lintDebug`/`assembleDebug` taking 10–30 min each under
# memory contention — and every such death cost a whole re-run. The user chose
# "finish, even if slow" over the tighter bound. light stays at 3600: a one-line
# change that needs two hours is a mis-tiered task, not a slow build.
#
# INVARIANT (pinned by tests, not by an import — worker.py mirrors constants, it
# does not import this module, and vice-versa): every value here PLUS REAP_MARGIN
# must stay below worker.RUNNER_HEARTBEAT_STALE_S, or the daemon idle-gate would
# kickstart a runner that is legitimately mid-stage; and the orchestrator's
# stale-reaper derives its per-row cutoff from these + REAP_MARGIN for the same
# reason (orchestrator._stale_bound_for). Keys are model_select.TIERS.
STAGE_TIMEOUTS = {"heavy": 10800, "standard": 7200, "light": 3600}
DEFAULT_STAGE_TIMEOUT = 3600
MAX_STAGE_TIMEOUT = max(STAGE_TIMEOUTS.values())
# Head-room between a run's stage timeout and the moment the reaper may declare
# it dead. A stage can run right up to its own timeout with no PROGRESS touch (a
# stage emits none mid-run — only the runner does, between stages), so the reaper
# must wait at least one full stage_timeout past the last touch, plus this slack.
REAP_MARGIN = 900


def stage_timeout_for(payload: dict) -> int:
    """The per-stage subprocess timeout (seconds) for a task payload.

    Priority: an explicit `stage_timeout_min` frontmatter override (minutes) >
    the task's difficulty tier (model_select.classify_tier) mapped through
    STAGE_TIMEOUTS > DEFAULT_STAGE_TIMEOUT. Best-effort: a classification error
    falls back to the default rather than raising — a timeout that cannot be
    sized must never be the reason a run dies. An operator setting
    `stage_timeout_min` above MAX_STAGE_TIMEOUT owns the STALE invariant for that
    one task; the tier ladder above is what the invariant tests fix in place."""
    p = payload or {}
    raw = p.get("stage_timeout_min")
    if raw is not None:
        try:
            mins = int(raw)
            if mins > 0:
                return mins * 60
        except (TypeError, ValueError):
            pass
    try:
        from .model_select import classify_tier
        tier, _ = classify_tier(p)
    except Exception:
        tier = DEFAULT_STAGE_TIER
    return STAGE_TIMEOUTS.get(tier, DEFAULT_STAGE_TIMEOUT)


STAGES_AUTO = ("research", "plan", "implement", "verify", "report")
STAGES_DELEGATE = ("delegate",)
STAGES_QUICK = ("quick",)   # right-sizing: one combined call for trivial changes
# Planner-fed exploration (discovery.py): clone a project into /tmp, run its own
# tests/build/smoke launch, sweep the whole tree, and write a candidate-task
# report to Memory/episodes/. Read-only towards the repo and the vault's tracked
# tree by prompt; the runner closes it on RESULT like any vault task.
STAGES_DISCOVER = ("discover",)

# The stages the Solar (upstage) harness can run: the single-stage lanes only.
# `solar_stage.run` has read/write/finish file tools and no shell — so it cannot
# do quick's self-verify + self-merge + `gh pr` or the 5-stage pipeline's
# git/build work; `delegate` ("do the work, edit files, write delegate.md") is
# exactly what it can do. quick is listed so a quick task CAN be routed once a
# key-authenticated vendor grows a verify path, but delegate is the lane opened
# now (runner._solar_account + the `use_solar` branch).
SOLAR_STAGES = STAGES_QUICK + STAGES_DELEGATE


def route_to_solar(agent: str | None, stage: str) -> bool:
    """Whether this stage runs via the Solar harness instead of `claude -p`:
    the task's resolved `agent` is upstage AND the stage is a single-stage lane.
    Pure and total — the runner ALSO gates on an actually-resolved upstage
    account, so this returning True for a task with no upstage account configured
    still never routes (and every claude task returns False here regardless)."""
    return (agent or "").lower() == "upstage" and stage in SOLAR_STAGES

# The test runners are for the Step 5b local fallback (verify.md) — the prompt
# tells verify to reproduce the repo's own CI commands when CI cannot run, and
# without these the harness gated every one of them behind an approval prompt,
# so the fallback could never actually execute. Read-only by construction:
# verify gets no Edit, and the prompt forbids weakening a check.
#
# One constant instead of two lists: verify and quick have always needed the
# same runner set (quick is a single combined call, so it owns its own
# verify+merge decision and must reproduce the same checks). Kept as two
# hand-maintained copies, "fix verify and forget quick" was a live failure mode
# — the set exists once so an addition cannot land in only one of them.
#
# pnpm/node/npx added 2026-08-18: advote (pnpm workspaces, Node >=20) parked
# at FAIL(unverified) because `pnpm --version` answered "This command requires
# approval" — the fallback could not run and 6 TS files reached a PR having
# never been compiled anywhere. One of them did not compile.
#
# flutter/dart added 2026-08-24: moksori-2-deck-order-by-recent-contact (21
# files, +1,421/-30) parked at FAIL(unverified) because bare `flutter analyze`
# answered "This command requires approval" twice — the same run's implement
# stage had flutter open, since a task's allowed_tools reach implement only
# (TASK_TOOLS_STAGES). A human then ran all three gates on the same sha
# (a2e5e3b8) and got green: no code defect, a pure observability gap that cost
# one human intervention and two worker runs.
FALLBACK_RUNNER_TOOLS = (
    "Bash(gradlew:*) Bash(./gradlew:*) Bash(uv:*) Bash(pytest:*) Bash(npm:*) "
    "Bash(pnpm:*) Bash(node:*) Bash(npx:*) Bash(flutter:*) Bash(dart:*)"
)

# The phone-SMS inbox CLI (CLAUDE.md §11, todo_system.sms) for tasks that need
# a 2FA code. The whitelist splits on spaces (_safe_tools, --allowed-tools), so
# `Bash(python3 -m todo_system.sms:*)` can never match anything; the PATH
# wrapper scripts/macp-sms gives it one space-free name. delegate — the
# admin/research stage that meets 2FA most — gets only this rule, not
# Bash(uv:*)/Bash(python3:*); research and plan stay read-only.
SMS_CLI = "macp-sms"
SMS_TOOL_RULE = f"Bash({SMS_CLI}:*)"
SMS_TOOL_STAGES = ("implement", "verify", "quick", "delegate")

# The mail-inbox CLI for tasks whose 2FA code arrives by e-mail instead of SMS:
# same space-split problem as SMS, so one space-free PATH wrapper name. The
# wrapper ships from its own repo, not this engine's scripts/, so it is
# deliberately absent from worker._WRAPPER_NAMES (that list syncs engine
# scripts/ only). delegate gets only this rule, never Bash(uv:*)/
# Bash(python3:*); research and plan stay read-only.
MAIL_CLI = "macp-mail"
MAIL_TOOL_RULE = f"Bash({MAIL_CLI}:*)"
MAIL_TOOL_STAGES = ("implement", "verify", "quick", "delegate")

# The HQ's logged-in Chrome (CLAUDE.md §12) for consoles that have no API. Same
# space-split problem as SMS, so one PATH wrapper, scripts/macp-browser. The
# wrapper — not this rule — is what narrows it to `browser.sh status|up` and the
# fixed node tools in the macp-browser folder; delegate gets only this, never
# Bash(node:*)/Bash(bash:*). The folder path lives in the wrapper's
# MACP_BROWSER_DIR default, never in engine code.
BROWSER_CLI = "macp-browser"
BROWSER_TOOL_RULE = f"Bash({BROWSER_CLI}:*)"
BROWSER_TOOL_STAGES = ("implement", "verify", "quick", "delegate")
# Who holds the logged-in Chrome's advisory lease (the wrapper's `lease`): one
# Chrome profile is shared by workers, interactive sessions and a human on VNC,
# so probe/steps/text exit 75 while someone else holds it. A run's browser
# stages get the run_id here (with_browser_owner), so implement → verify of one
# run are one owner and the stage can `lease acquire` without inventing an id.
BROWSER_OWNER_ENV = "MACP_BROWSER_OWNER"
# macp-browser v2: a long-lived stdio MCP server (`node <dir>/mcp/server.mjs`)
# that attaches to the same Chrome once and takes the lease itself on first call.
# Opt-in via paths.browser_mcp_server (env/config key, no default): only when it
# names an existing file do BROWSER_MCP_STAGES get `--mcp-config` (owner =
# run_id in the server's env, plus BROWSER_MCP_PASSTHROUGH_ENVS when set) and
# BROWSER_MCP_TOOL_RULE; otherwise argv is
# byte-identical to before. The Bash wrapper rule above stays for compatibility.
# `--strict-mcp-config` rides along so a user-scope registration of the same
# server (interactive sessions, docs/configuration.md) can't load a second,
# ownerless copy into a stage — no stage allows any other mcp__ tool anyway.
BROWSER_MCP_SERVER_NAME = "macp-browser"
BROWSER_MCP_TOOL_RULE = f"mcp__{BROWSER_MCP_SERVER_NAME}__*"
BROWSER_MCP_STAGES = BROWSER_TOOL_STAGES
# The node binary, same knob the macp-browser wrapper honours (node isn't
# always on the runner's PATH).
BROWSER_MCP_NODE_ENV = "MACP_BROWSER_NODE"
# Instance knobs the wrapper honours (scripts/macp-browser). The claude CLI
# spawns a stdio MCP server with ONLY the config's `env` block, so the stage's
# own environment never reaches it: each one set non-empty here is copied
# through verbatim (the server resolves CDP from the instance's ports.env
# itself; an explicit CDP wins there). The owner is never copied — it is
# always the run_id.
BROWSER_MCP_PASSTHROUGH_ENVS = ("MACP_BROWSER_INSTANCE", "MACP_BROWSER_DIR",
                                "MACP_BROWSER_CDP")

# The remote-gate executor (todo_system.orchestrator.remote_gate, CLAUDE delegate
# note below) for a verification command too heavy/memory-bound for one stage:
# `macp-remote-gate run -- <cmd>` ships it to a roomier node and the stage reads
# back only the result. Same space-split problem as SMS/BROWSER, so one PATH
# wrapper scripts/macp-remote-gate gets one space-free rule. The node
# project/zone live in the vault's .macp/remote_gate.json, never in engine code.
REMOTE_GATE_CLI = "macp-remote-gate"
REMOTE_GATE_TOOL_RULE = f"Bash({REMOTE_GATE_CLI}:*)"
REMOTE_GATE_TOOL_STAGES = ("implement", "verify", "quick", "delegate")

# The on-demand emulator node (via beta_smoke.run_smoke) for a pre-merge
# on-device check: `macp-emulator run --repo-dir <checkout> --sha <sha>
# --scenario <json|name> [--seed owner|none]` builds→installs→(seed)→scenario→
# logcat→always-stops and prints an aggregates-only result JSON. Same space-split
# problem as SMS/BROWSER/REMOTE_GATE, so one PATH wrapper scripts/macp-emulator
# gets one space-free rule. Node identity/zone and the owner-seed source live in
# the vault's .macp/ (remote_gate.json), never in engine code. Unlike the other
# three standing rules, delegate does NOT get it (an on-device check is a
# verify/implement concern, not an admin/research one — AC#2).
EMULATOR_CLI = "macp-emulator"
EMULATOR_TOOL_RULE = f"Bash({EMULATOR_CLI}:*)"
EMULATOR_TOOL_STAGES = ("implement", "verify", "quick")
# The emulator node lease holder (beta_smoke's shared _NODE_LOCK): a run's
# emulator stages get the run_id here (with_emulator_owner), mirroring
# BROWSER_OWNER_ENV, so a stage's take of the SAME node lock the beta sweep uses
# is attributable and the two can never co-drive the node.
EMULATOR_OWNER_ENV = "MACP_EMULATOR_OWNER"

# Rules every listed stage keeps even when a task supplies its own allowed_tools.
_STANDING_RULES = ((SMS_TOOL_RULE, SMS_TOOL_STAGES),
                   (BROWSER_TOOL_RULE, BROWSER_TOOL_STAGES),
                   (REMOTE_GATE_TOOL_RULE, REMOTE_GATE_TOOL_STAGES),
                   (EMULATOR_TOOL_RULE, EMULATOR_TOOL_STAGES),
                   (MAIL_TOOL_RULE, MAIL_TOOL_STAGES))

STAGE_TOOLS = {
    "research": "Read Write",
    "plan": "Read Write",
    "implement": "Read Edit Write Bash(git:*) Bash(uv:*) Bash(npm:*) Bash(pnpm:*) "
                 f"Bash(node:*) Bash(npx:*) Bash(pytest:*) {SMS_TOOL_RULE} "
                 f"{BROWSER_TOOL_RULE} {REMOTE_GATE_TOOL_RULE} {EMULATOR_TOOL_RULE} "
                 f"{MAIL_TOOL_RULE}",
    "verify": (f"Read Write Bash(git:*) Bash(gh:*) {FALLBACK_RUNNER_TOOLS} {SMS_TOOL_RULE} "
               f"{BROWSER_TOOL_RULE} {REMOTE_GATE_TOOL_RULE} {EMULATOR_TOOL_RULE} "
               f"{MAIL_TOOL_RULE}"),
    "report": "Read Write Edit Bash(git:*) Bash(gh:*)",
    "delegate": ("Read Write Edit WebSearch WebFetch Bash(git:*) Bash(pwd:*) Bash(ls:*) "
                 f"{SMS_TOOL_RULE} {BROWSER_TOOL_RULE} {REMOTE_GATE_TOOL_RULE} "
                 f"{MAIL_TOOL_RULE}"),
    "quick": (f"Read Edit Write Bash(git:*) Bash(gh:*) {FALLBACK_RUNNER_TOOLS} {SMS_TOOL_RULE} "
              f"{BROWSER_TOOL_RULE} {REMOTE_GATE_TOOL_RULE} {EMULATOR_TOOL_RULE} "
              f"{MAIL_TOOL_RULE}"),
    # No Edit: discover reads a throwaway clone and WRITES exactly two files (the
    # episode report + its artifact). The shell set is what "run the project"
    # needs — the CI runner set plus file inspection and a way to bound a
    # smoke launch: `timeout` where it exists (Linux/WSL) and perl's alarm on
    # macOS, which ships no timeout/gtimeout at all (found 2026-09-11 when the
    # first draft of the prompt leaned on `timeout`). curl stays forbidden
    # (FORBIDDEN_TOOL_SUBSTRINGS); a local HTTP probe goes through python3.
    "discover": ("Read Write Bash(git:*) Bash(pwd:*) Bash(ls:*) Bash(cat:*) "
                 "Bash(head:*) Bash(tail:*) Bash(wc:*) Bash(grep:*) Bash(rg:*) "
                 "Bash(find:*) Bash(timeout:*) Bash(perl:*) Bash(python3:*) "
                 f"Bash(ruff:*) Bash(make:*) {FALLBACK_RUNNER_TOOLS}"),
}
STAGE_MAX_TURNS = {
    "research": 40, "plan": 40, "implement": 200,
    "verify": 50, "report": 40, "delegate": 200, "quick": 120,
    "discover": 120,
}
STAGE_ARTIFACT = {
    "research": "requirements.md",
    "plan": "implementation_plan.md",
    "implement": "run.log",          # orchestrator-written (captured stage output)
    "verify": "verify.md",
    "report": "report.md",
    "delegate": "delegate.md",
    "quick": "quick.md",
    "discover": "discover.md",
}
# Never allow these in a stage tool whitelist (defense-in-depth, mirrors worker).
# No trailing space on curl/ssh — with one they matched `Bash(curl -X …)` and
# missed `Bash(curl:*)`, which is the form actually written. Kept byte-identical
# to worker.FORBIDDEN_TOOL_SUBSTRINGS; a test pins the two together.
FORBIDDEN_TOOL_SUBSTRINGS = (
    "Bash(rm", "Bash(sudo", "Bash(curl", "Bash(scp", "Bash(ssh",
    "Bash(git push --force", "Bash(git push -f", "Bash(* --no-verify",
)


def load_stage_prompt(stage: str) -> str:
    return (COMMANDS_DIR / f"worker-{stage}.md").read_text(encoding="utf-8")


def strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


# Stages whose tool list a task may widen. research/plan are read-only and
# verify is test-only BY DESIGN — those guarantees are what let the pipeline
# trust their artifacts, so a task must not be able to dissolve them. implement
# is the stage that actually touches the world, and the one Worker/README.md has
# always documented as taking the task's allowed_tools.
TASK_TOOLS_STAGES = ("implement",)


def browser_mcp_server(stage: str) -> Path | None:
    """The configured macp-browser MCP server script for `stage`, or None.

    None unless `stage` is a BROWSER_MCP_STAGES stage and
    paths.browser_mcp_server() names an existing file — a path left pointing at
    a removed checkout falls back to the wrapper-only argv instead of launching a
    dead server. Never raises: a broken config must not break dispatch."""
    if stage not in BROWSER_MCP_STAGES:
        return None
    try:
        server = paths.browser_mcp_server()
        return server if server is not None and server.is_file() else None
    except Exception:
        return None


def browser_mcp_config(server: Path, run_id: str) -> str:
    """The inline `--mcp-config` JSON for one stage: the macp-browser server
    with the run_id as its lease owner, plus any BROWSER_MCP_PASSTHROUGH_ENVS
    set non-empty in this process (none set → env is the owner alone, so the
    default Chrome is unchanged). Holds a path, the run_id and those values."""
    env = {k: v for k in BROWSER_MCP_PASSTHROUGH_ENVS if (v := os.environ.get(k))}
    env[BROWSER_OWNER_ENV] = run_id
    cfg = {"mcpServers": {BROWSER_MCP_SERVER_NAME: {
        "command": os.environ.get(BROWSER_MCP_NODE_ENV) or "node",
        "args": [str(server)],
        "env": env,
    }}}
    return json.dumps(cfg, sort_keys=True, separators=(",", ":"))


def _safe_tools(stage: str, task_tools: str | None = None, *,
                browser_mcp: bool = False) -> list[str]:
    """The stage's tool whitelist, optionally replaced by the task's own.

    `allowed_tools` in task frontmatter did nothing in the v2 staged pipeline:
    tasks.py never put it in the payload and build_stage_argv only ever read the
    hardcoded STAGE_TOOLS. Every task's carefully written tool list was
    decorative, and the only way to give one task a capability was to give it to
    every task. Discovered 2026-08-07 while trying to scope gcloud to a single
    Firebase task.

    The forbidden check applies to the task-supplied list too — that is the
    property that makes honouring it safe at all. A task list still keeps the
    _STANDING_RULES (SMS, BROWSER, REMOTE_GATE, EMULATOR, MAIL) on their
    stages, so a task needs no allowed_tools edit to read a 2FA code (SMS or
    mail) or use the logged-in Chrome. `browser_mcp` (the MCP
    server is configured) appends BROWSER_MCP_TOOL_RULE last on either path.
    """
    tools = STAGE_TOOLS[stage]
    if task_tools and stage in TASK_TOOLS_STAGES:
        tools = task_tools.replace(",", " ")
        # The task list replaces the default wholesale; the standing rules are
        # granted to every write stage regardless of task.
        for rule, rule_stages in _STANDING_RULES:
            if stage in rule_stages and rule not in tools.split():
                tools = f"{tools} {rule}"
    if (browser_mcp and stage in BROWSER_MCP_STAGES
            and BROWSER_MCP_TOOL_RULE not in tools.split()):
        tools = f"{tools} {BROWSER_MCP_TOOL_RULE}"
    for bad in FORBIDDEN_TOOL_SUBSTRINGS:
        if bad in tools:
            raise ValueError(f"forbidden tool in {stage} whitelist: {bad}")
    return tools.split()


# --- Stage token usage --------------------------------------------------------
# Until 2026-09-19 no token or cost figure existed anywhere in the pipeline:
# stages.json carried none and metrics.py priced a stage by its artifact bytes
# (Notes/macp-pipeline-cost-benefit.md, §계측 갭). Now every stage invocation
# gets a fresh `--session-id <uuid4>`, and once it exits the runner reads that
# session's usage back out of the CLI's own transcript into stages.json
# `usage.<stage>` (stage_usage / record_stage_usage, below record_timeout).
#
# Not `--output-format json`, the documented usage report: on a live capture
# (tests/fixtures/claude_output/, claude 2.1.277) JSON mode dropped the CLI's
# settings warnings from stderr, so invalidate_model_cache_if_unavailable would
# have read different text than before — and what the stage loop reads is the
# one thing this must not change. The session id changes none of it: rc, stdout
# and stderr came out byte-identical in the same capture.
USAGE_ENV = "MACP_STAGE_USAGE"
# A record's keys, in this order. Nothing else from the CLI (prompt, tool
# output, thinking) is ever copied into stages.json.
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens", "num_turns", "duration_ms", "model",
              "session_id", "total_cost_usd")
USAGE_TOKEN_KEYS = USAGE_KEYS[:4]
SESSION_ID_FLAG = "--session-id"


def usage_enabled() -> bool:
    """The kill switch: `MACP_STAGE_USAGE=0` in the runner's env turns usage off
    entirely — no flag, no transcript read, no stages.json write — so argv is
    byte-for-byte what it was before instrumentation. Read at CALL time, like
    usage_guard._cfg; only an explicit "0" disables it."""
    return os.environ.get(USAGE_ENV, "").strip() != "0"


def argv_session_id(argv) -> str | None:
    """The `--session-id` build_stage_argv put in `argv`, or None — kill switch
    off, or a caller's stub argv. Only a canonical UUID counts, because the value
    ends up in a filesystem glob."""
    try:
        args = list(argv)
        value = str(args[args.index(SESSION_ID_FLAG) + 1])
        return value if str(uuid.UUID(value)) == value.lower() else None
    except Exception:
        return None


def build_stage_argv(stage: str, *, task_id: str, run_id: str, run_dir: Path,
                     branch: str, repo: str | None = None,
                     working_dir: str | None = None, auto_merge: bool = False,
                     worker_root: Path | None = None,
                     project_context: str = "", retry_context: str = "",
                     allowed_tools: str | None = None,
                     tier: str | None = None) -> list[str]:
    """Reproduce worker.build_stage_command: stage prompt + run metadata, with
    the stage's tool whitelist and turn budget. `tier` is the task's model tier
    (model_select.classify_tier); None means the heavy ladder."""
    prompt = strip_frontmatter(load_stage_prompt(stage))
    mcp_server = browser_mcp_server(stage)
    mcp_on = mcp_server is not None
    meta = [
        "\n\n---\n\n# Run metadata (injected by orchestrator)\n\n",
        f"- run_id: `{run_id}`\n",
        f"- slug: `{task_id}`\n",
        f"- run_dir: `{run_dir}`\n",
        f"- branch: `{branch}`\n",
        f"- stage: `{stage}`\n",
        f"- auto_merge: `{str(auto_merge).lower()}`\n",
        # The stage prompts also list the runners in prose, and that prose goes
        # stale: worker-verify.md still named only gradlew/uv/pytest/npm long
        # after pnpm/node/npx were granted, so verify read "flutter is not
        # available" and stopped probing. This line is the decided whitelist
        # itself and comes after the prompt, so it wins any disagreement.
        f"- allowed tools (authoritative, overrides any list in the prompt "
        f"above): {' '.join(_safe_tools(stage, allowed_tools, browser_mcp=mcp_on))}\n",
    ]
    if stage in BROWSER_TOOL_STAGES:
        # The worker-*.md prompts live under .claude/**, which a headless stage
        # cannot edit, so the lease rule rides on the metadata instead.
        meta.append(
            f"- **Logged-in browser ({BROWSER_CLI}, CLAUDE.md §12)**: before "
            f"probe/steps/text run `{BROWSER_CLI} lease acquire --purpose '<why>'` "
            f"(`{BROWSER_OWNER_ENV}` is already `{run_id}` in your env) and "
            f"`{BROWSER_CLI} lease release` when done; exit 75 = someone else "
            "holds it — wait (`--wait <sec>`) or stop, never delete lease.json. "
            "Touch only tabs you opened.\n")
        meta.append(
            f"- **Login-less browser work → scratch instance ({BROWSER_CLI})**: for "
            "work that needs no logged-in profile (public page reads, consoles with "
            f"no login) prefer `{BROWSER_CLI} scratch acquire --owner "
            f'"${BROWSER_OWNER_ENV}" --purpose \'<why>\'` — a throwaway instance '
            "that never waits on the default lease — and `scratch release` when done. "
            "Reserve the default (logged-in) profile lease for work that needs the "
            "logged-in cookies.\n")
        if mcp_on:
            # Only when the MCP server is wired into this argv, so an unconfigured
            # host's prompt stays byte-identical.
            meta.append(
                f"- **Browser: MCP tools first, wrapper for compatibility "
                f"({BROWSER_MCP_SERVER_NAME} MCP server)**: prefer the "
                f"`{BROWSER_MCP_TOOL_RULE}` tools (browser_navigate / browser_snapshot "
                "with element refs / browser_click / browser_type / browser_wait_for / "
                "browser_tabs / browser_take_screenshot) over "
                f"`{BROWSER_CLI} probe|steps|text` — they take the lease as owner "
                f"`{run_id}` on first call, release it when idle or at exit, and touch "
                f"only tabs they opened. The `{BROWSER_CLI}` wrapper lines above stay "
                "for compatibility (lease status, scratch, login-check, or when an MCP "
                "tool is unavailable). A tool reporting logged-out or a lease held by "
                "someone else means stop and write `blocked_on:` — never log in, never "
                "take the lease.\n")
    if stage in REMOTE_GATE_TOOL_STAGES:
        # A gate command too heavy for one stage (overruns the cap, or won't finish
        # inside the runner cgroup's memory — e.g. a full Robolectric/Gradle suite)
        # goes to a roomier node via the metadata, same as the BROWSER line, since
        # the worker-*.md prompts under .claude/** are unwriteable by a headless stage.
        meta.append(
            f"- **Heavy / over-memory verification ({REMOTE_GATE_CLI})**: do NOT "
            "block-poll a command that would overrun this stage's cap or the host's "
            f"memory. Ask `{REMOTE_GATE_CLI} decide --repo <checkout> --task-id "
            f"{task_id} -- <cmd>`; if it prints `remote`, run `{REMOTE_GATE_CLI} "
            "run --repo <url> --sha <sha> -- <cmd>` (starts DETACHED, prints a "
            f"handle at once) and read the result back with `{REMOTE_GATE_CLI} poll "
            f"<handle>` / `{REMOTE_GATE_CLI} result <handle>` — the heavy wall-clock "
            "runs on the node, outside this stage's timeout window.\n")
    if stage in EMULATOR_TOOL_STAGES:
        # The on-demand emulator-node check, on the metadata rather than the
        # worker-*.md prompt under .claude/** (unwriteable by a headless stage),
        # same as the BROWSER/REMOTE_GATE lines above.
        meta.append(
            f"- **On-device emulator check ({EMULATOR_CLI})**: ONLY when the task's "
            "frontmatter/acceptance asks for a real on-device run (advisory "
            "`emulator: true`) — a UI/behaviour change whose correctness a unit test "
            f"cannot show. Run `{EMULATOR_CLI} run --repo-dir <checkout> --sha <sha> "
            "--scenario <json|name> [--seed owner|none]`: it takes the SHARED beta-sweep "
            "node lease (owner = your run_id, already in `$"
            f"{EMULATOR_OWNER_ENV}`), builds→installs→(seeds)→runs the scenario→scans "
            "logcat→ALWAYS stops the node, and prints an aggregates-only result JSON "
            "(rc · per-step durations · ANR/FATAL counts · max Skipped-frames · "
            "screenshot paths). SKIP it for a simple/non-visual change; for MANY "
            "changes at once leave it to the beta-sweep smoke gate rather than one "
            "scenario here. Exit 75 = node busy (someone else holds the lease — wait "
            "or stop), 76 = over the shared monthly budget. On any non-zero exit, or "
            "if the live node / gcloud / `.macp/remote_gate.json` is unreachable from "
            "this stage, write `blocked_on:` naming the missing precondition and stop "
            "— do NOT fake a result. Aggregates only: never copy the seed DB, "
            "screenshots, or dumps into run.log / the commit / the PR.\n")
    if repo:
        meta.append(f"- repo: `{repo}`\n")
        if working_dir:
            meta.append(f"- working_dir: `{working_dir}`\n")
            # The stage runs with cwd = vault root, and the whitelist entry is the
            # literal `Bash(./gradlew:*)`: `<wd>/gradlew -p <wd>` does not match it
            # and is denied, and `cd <wd> && ./gradlew` is a refused construct.
            # A standalone `cd` persists across Bash calls — Verify found that on
            # its own; Implement, told "git -C rather than cd", never tried it and
            # committed a Robolectric test it had not run once
            # (edge-to-edge-insets-pairent, 2026-09-25).
            meta.append(
                f"- **Running the project's build/tests**: issue `cd {working_dir}` "
                "as its own Bash call (no `&&`), then run `./gradlew …` / `npm …` "
                "etc. in later calls — the cwd persists. The absolute form "
                f"`{working_dir}/gradlew` is denied. Keep using `git -C` for git.\n")
        meta.append("- **External repo task**: Implement/Verify operate on the "
                    "checkout; vault run_dir artifacts are read-only references.\n")
    if project_context:
        meta.append("\n## Project memory (.macp/memory — accumulated context)\n\n"
                    + project_context + "\n")
    if retry_context:
        meta.append(
            "\n## ⚠ Previous attempt FAILED — fix these specific issues\n\n"
            "A prior run of THIS task already implemented on this branch but its "
            "verification failed for the reasons below. You are continuing that "
            "branch (its commits are present). Do NOT re-scaffold from scratch: "
            "diagnose and FIX-FORWARD the exact failures below, then commit the "
            "fix.\n\n" + retry_context + "\n")
    full = prompt + "".join(meta)
    argv = ["claude", "-p", full,
            "--model", stage_model(stage, tier), "--effort", stage_effort(stage, tier),
            "--max-turns", str(STAGE_MAX_TURNS[stage])]
    if mcp_on:
        # --mcp-config is variadic like --allowed-tools, so a --flag must follow
        # it; --strict-mcp-config is that flag (why strict: BROWSER_MCP_SERVER_NAME).
        argv += ["--mcp-config", browser_mcp_config(mcp_server, run_id),
                 "--strict-mcp-config"]
    if usage_enabled():
        # Names the transcript stage_usage reads the usage back from — fresh per
        # invocation, so one id is one stage run. Ahead of --allowed-tools, which
        # is variadic: nothing should depend on where its list ends.
        argv += [SESSION_ID_FLAG, str(uuid.uuid4())]
    argv += ["--allowed-tools", *_safe_tools(stage, allowed_tools, browser_mcp=mcp_on)]
    wr = Path(worker_root) if worker_root else paths.worker_root()
    if wr.exists():
        argv += ["--add-dir", str(wr)]
    return argv


def parse_verify_verdict(verify_md_text: str) -> str | None:
    """verify.md declares `overall_status: pass | fail | skip` in frontmatter."""
    m = re.search(r"overall_status:\s*(pass|fail|skip)", verify_md_text or "", re.I)
    return m.group(1).upper() if m else None


def parse_report_merged(report_md_text: str) -> bool | None:
    """report.md declares `merged: yes | no | n/a` — did the auto_merge actually
    land? None if unstated (treat as unknown → surface for review)."""
    m = re.search(r"merged:\s*(yes|true|no|false|n/?a)", report_md_text or "", re.I)
    if not m:
        return None
    v = m.group(1).lower()
    if v in ("yes", "true"):
        return True
    if v in ("no", "false"):
        return False
    return None   # n/a → not applicable (no auto_merge); caller decides


def parse_blocked_on(verify_md_text: str) -> str | None:
    """verify.md may declare `blocked_on: <what>` alongside a FAIL verdict.

    A FAIL has two causes the runner used to collapse into one: the code is
    broken (fix-forward, re-implement) or the task could not start at all
    because something OUTSIDE it hasn't happened. dripcoffee-bean-cards-2
    (2026-08-05) was the second — it measured origin/main, found the upstream
    PR unmerged, created no branch, and wrote `blocked_on:` saying so in
    as many words. Nothing read the field, so it was retried as a defect and
    re-bought research+plan+implement against the same missing dependency."""
    m = re.search(r"^blocked_on:\s*(.+)$", verify_md_text or "", re.M)
    if not m:
        return None
    v = m.group(1).strip().strip('"\'')
    return v or None


def parse_report_pr_url(report_md_text: str) -> str | None:
    """report.md declares `pr_url:` — the PR it opened, or null when it opened
    none. The review notification asserted a PR existed without ever reading
    this, and told a human to merge a PR that was never created."""
    m = re.search(r"^pr_url:\s*(.+)$", report_md_text or "", re.M)
    if not m:
        return None
    v = m.group(1).strip().strip('"\'')
    return v if v and v.lower() not in ("null", "none", "-", "n/a") else None


def parse_report_worker_mode(report_md_text: str) -> str | None:
    """report.md declares `worker_mode_recommendation: auto | review` — the
    agent's own verdict on whether a human must approve before this lands.

    Written by the report stage since day one but read by nothing until now, so
    the escalation was silently dropped. None = unstated."""
    m = re.search(r"worker_mode_recommendation:\s*(auto|review)", report_md_text or "", re.I)
    return m.group(1).lower() if m else None


def parse_report_rebase_conflict(report_md_text: str) -> bool:
    """report.md declares `rebase_conflict: true` when the report stage tried to
    rebase a LATER concurrent same-repo PR onto a moved origin/main, hit a
    conflict, and — per its own instructions — aborted WITHOUT auto-resolving,
    leaving the PR unmerged for a human. Absent / `false` = no conflict park."""
    return bool(re.search(r"^rebase_conflict:\s*(true|yes)\b",
                          report_md_text or "", re.I | re.M))


def review_hold(run_dir: Path, last_stage: str) -> bool:
    """Did the pipeline DELIBERATELY withhold an expected auto-merge?

    `merge_landed() is not True` has two very different causes that the runner
    used to collapse into one 'conflict/CI/perm' note: the merge was ATTEMPTED
    and failed, or the agent CHOSE not to merge because the task's own guardrails
    said a human must sign off. tennis-macro-monday-evening-slot-scan-failure
    (2026-08-05) was the latter — a green, mergeable PR held because the task's
    `if_then` said "구조적 문제면 자동 머지하지 말고 사용자 review" — and it got
    reported as an infrastructure failure, which sent a human hunting for a CI
    break that did not exist. The agent signals the hold by recommending
    `review`; anything else (including an unwritten field) stays a real failure,
    so a genuinely broken merge is never excused as intentional."""
    art = Path(run_dir) / STAGE_ARTIFACT.get(last_stage, "report.md")
    if not art.exists():
        return False
    return parse_report_worker_mode(
        art.read_text(encoding="utf-8", errors="replace")) == "review"


def rebase_conflict_park(run_dir: Path, last_stage: str) -> bool:
    """Did the report stage park a LATER concurrent same-repo PR on a rebase
    conflict? Same shape as review_hold(): read the LAST stage's artifact and
    look for the marker the agent is told to stamp.

    With max_parallel_per_repo > 1, two same-repo runs can merge around the same
    tick. The one whose report stage finds origin/main already moved must rebase
    `worker/<slug>` and re-run the local gates before merging; a rebase that
    CONFLICTS must never be auto-resolved (the overlap judge got it wrong — a
    human has to reconcile). The agent aborts the rebase, leaves the PR open, and
    writes `rebase_conflict: true`. This lets the runner stamp the FIXED park note
    '동시 실행 충돌 — 겹침 오판' — the reason string is owned by the code here, not
    by the agent's prose. Absent marker / missing artifact = not a conflict park."""
    art = Path(run_dir) / STAGE_ARTIFACT.get(last_stage, "report.md")
    if not art.exists():
        return False
    return parse_report_rebase_conflict(
        art.read_text(encoding="utf-8", errors="replace"))


def gh_pr_merged(branch: str, repo: str | None = None) -> bool | None:
    """Ask GitHub (via `gh`) whether the PR for `branch` actually merged.

    True = merged, False = a PR exists but is not merged (open/closed-unmerged),
    None = unknown — no `gh`, no PR for the branch, not authenticated, or any
    parse/timeout error. None is deliberately NON-committal so the caller falls
    back to the artifact regex: a `gh` hiccup must never flip a merged PR to
    'not landed' (the false-negative that parked merged PRs) or vice-versa.
    Stdlib-only + best-effort; `gh` absence is a normal, silent outcome."""
    args = ["gh", "pr", "view", branch, "--json", "state,mergedAt"]
    if repo:
        args += ["--repo", repo]
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None      # gh not installed / timed out — unknown, let caller fall back
    if r.returncode != 0:
        return None      # no PR for this branch, not authed, etc. — unknown
    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return bool(data.get("mergedAt")) or str(data.get("state", "")).upper() == "MERGED"


def gh_pr_merged_at(branch: str, repo: str | None = None,
                    cwd: Path | str | None = None) -> str | None:
    """WHEN the PR for `branch` merged, as gh's ISO-8601 string, or None.

    A separate function from `gh_pr_merged` on purpose. That one answers "did
    it?" and returns `bool | None`; widening it to carry a timestamp would
    change a return type `merge_landed` and `reconcile_merged` both branch on,
    so the safe move is a second, additive query. None means UNKNOWN in exactly
    the same non-committal sense: no `gh`, no PR for the branch, unauthenticated,
    still open (`mergedAt: null`), timeout, or any parse error. Callers must
    treat None as "cannot tell", never as "no".

    `cwd` exists because `gh pr view` resolves the repository from the working
    directory (see merge_state._gh_json). A caller holding an external clone's
    path has to pass it, or gh answers about whatever repo the process happens
    to be sitting in — for the runner that is the vault, not the clone.
    """
    args = ["gh", "pr", "view", branch, "--json", "mergedAt"]
    if repo:
        args += ["--repo", repo]
    try:
        r = subprocess.run(args, capture_output=True, text=True, cwd=cwd,
                           encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None      # gh not installed / timed out — unknown
    if r.returncode != 0:
        return None      # no PR for this branch, not authed, etc. — unknown
    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return str(data["mergedAt"]) if data.get("mergedAt") else None


def merge_landed(run_dir: Path, last_stage: str,
                 branch: str | None = None, repo: str | None = None) -> bool | None:
    """Did the pipeline's auto-merge actually land?

    Ask GitHub FIRST (the ground truth): `gh pr view` on the branch. Only when
    that is unknown (None — no `gh`, no PR, error) fall back to the LAST stage's
    artifact, whose `merged:` line merely parrots what the LLM *wrote* it did.
    Trusting the artifact alone is what parked already-merged PRs as
    awaiting_human (5e-1 #11, 5e-3 #12: 'gate-bypass false-negative').

    The artifact varies by pipeline (report.md for auto, quick.md for quick,
    delegate.md for delegate) — read the RIGHT one via STAGE_ARTIFACT."""
    if branch:
        landed = gh_pr_merged(branch, repo)
        if landed is not None:
            return landed
    art = Path(run_dir) / STAGE_ARTIFACT.get(last_stage, "report.md")
    return parse_report_merged(art.read_text(encoding="utf-8", errors="replace")) \
        if art.exists() else None


# Every git call gets a wall-clock ceiling. On 2026-09-25 the runner's cgroup went
# over `memory.high`, and `remote_branch_exists`' `ls-remote` sat inside
# `git-remote-https` for 18~72 minutes — a normal one takes 0.6s. Three runner
# slots stayed occupied by threads that would never return while the dashboard
# showed idle, because nothing in this module bounded a subprocess. A stuck git
# now comes back as a failed CompletedProcess instead, and the thread is freed.
GIT_TIMEOUT_LOCAL_S = 180       # checkout -B / reset --hard / clean -fd on a big
                                # Android clone is seconds to tens of seconds
GIT_TIMEOUT_NETWORK_S = 300     # > local, per the task's acceptance criteria; also
                                # > publish_branch's proven `push` timeout of 120
GIT_TIMEOUT_RC = 124            # coreutils timeout(1)'s convention

# Subcommands that talk to a remote, i.e. the ones that hang on a wedged network
# or a stalled credential helper rather than on local disk.
_GIT_NETWORK_SUBCOMMANDS = frozenset({
    "ls-remote", "fetch", "push", "pull", "clone", "remote", "submodule",
})


def _default_git_timeout(args: tuple[str, ...] | list[str]) -> float:
    """Network ceiling if `args` names a remote-talking subcommand, else local.

    Read off the module globals at *call* time, not baked into a default argument,
    so a test can shrink them with `mock.patch.object(stages, ...)`."""
    for a in args:
        if not a.startswith("-"):
            return (GIT_TIMEOUT_NETWORK_S if a in _GIT_NETWORK_SUBCOMMANDS
                    else GIT_TIMEOUT_LOCAL_S)
    return GIT_TIMEOUT_LOCAL_S    # no subcommand found — the common path is local


def _git_stream(v: object) -> str:
    """`TimeoutExpired.stdout`/`.stderr` normalised to `str`.

    They are `None` when nothing was captured and can be `bytes` even though the
    call asked for text mode. Callers here do `(r.stderr or "").strip()[:200]`,
    which raises TypeError on bytes — so normalise once, at the source."""
    if v is None:
        return ""
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    return str(v)


def _run_git(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    """`subprocess.run(args)` where exceeding `timeout` is a return value.

    A timeout must NOT propagate: every caller in this module judges git by
    `returncode` (`remote_branch_exists` -> False, `branch_commits_ahead` -> -1,
    `default_branch` -> "main", `current_branch` -> "", `_orphaned_local_commits`
    -> None). Raising would turn a slow remote into a pipeline crash instead of
    the ordinary failure those contracts already handle."""
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as e:
        partial = _git_stream(e.stderr)
        if partial and not partial.endswith("\n"):
            partial += "\n"
        return subprocess.CompletedProcess(
            args=args, returncode=GIT_TIMEOUT_RC,
            stdout=_git_stream(e.stdout),
            stderr=partial + f"git timeout after {timeout}s: {' '.join(args)}\n")


def git_checkout(repo_dir: Path, branch: str, create: bool = False,
                 base: str | None = None,
                 timeout: float | None = None) -> subprocess.CompletedProcess:
    """`git checkout [-B] <branch> [<base>]`.

    `base` is only meaningful with `create`, where it is the commit the branch ref
    is (re)pointed at — the one way to inherit a starting point without a
    `reset --hard`, which is why `prepare_vault_branch` needs it. Ignored without
    `create`, since `checkout <branch> <path>` would mean something else entirely.

    `checkout` never touches a remote, so it takes the local ceiling directly
    rather than going through `_default_git_timeout`.
    """
    args = (["git", "-C", str(repo_dir), "checkout"] + (["-B"] if create else [])
            + [branch] + ([base] if create and base else []))
    return _run_git(args, GIT_TIMEOUT_LOCAL_S if timeout is None else timeout)


def _git(repo_dir: Path, *args: str,
         timeout: float | None = None) -> subprocess.CompletedProcess:
    """`git -C <repo_dir> <args>`, bounded by `timeout` (default: by subcommand).

    `timeout` is a trailing keyword with a default on purpose — existing tests
    stub this as `def stub(repo_dir, *args)`, and call sites here deliberately
    never pass it, letting `_default_git_timeout` cover all of them at once."""
    return _run_git(["git", "-C", str(repo_dir), *args],
                    _default_git_timeout(args) if timeout is None else timeout)


def default_branch(repo_dir: Path) -> str:
    r = _git(repo_dir, "symbolic-ref", "refs/remotes/origin/HEAD")
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().rsplit("/", 1)[-1]
    return "main"


def ff_default_branch(repo_dir: Path) -> str:
    """Fast-forward a clone sitting on its default branch to origin's tip.

    Best-effort and conservative: only when HEAD is the default branch and the
    working tree is clean, and only `--ff-only` — a clone on a worker branch or
    with local edits is left exactly as it is. Returns a one-line note for the
    run log ("" when nothing was attempted or nothing moved)."""
    repo_dir = Path(repo_dir)
    default = default_branch(repo_dir)
    if current_branch(repo_dir) != default:
        return ""
    if _git(repo_dir, "status", "--porcelain").stdout.strip():
        return f"long-job sync: {default} has local changes — not fast-forwarded"
    before = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    if _git(repo_dir, "fetch", "origin", default).returncode != 0:
        return f"long-job sync: fetch origin {default} failed — running on {before[:7]}"
    if _git(repo_dir, "merge", "--ff-only", f"origin/{default}").returncode != 0:
        return f"long-job sync: {default} cannot fast-forward — running on {before[:7]}"
    after = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    return f"long-job sync: {default} {before[:7]} -> {after[:7]}" if after != before else ""


def sync_for_long_job(repo_dir: Path) -> str:
    """Bring the tree a NEW long job will run in up to origin's default branch.

    Since per-run worktrees (`repos.ensure_worktree`), the job's tree is a
    DETACHED worktree cut from the shared clone's HEAD — and nothing advances
    that HEAD before the long-job gate (it runs before branch prep). So
    `ff_default_branch`, which only acts on a clone sitting ON its default
    branch, became a silent no-op and a job whose script landed in an earlier
    PR died "No such file" again (2026-10-27, ai-stock-trading
    financial-level-measurement-v3-resume ran on a 13-day-old c0d9052, 16
    commits behind, and `collect_financials.py` from #215 did not exist).

    Detached + clean → fetch the default branch and re-detach at its tip. On a
    branch → the old `ff_default_branch` behaviour (a worker branch or local
    edits are never touched). Returns a one-line note, "" when nothing moved."""
    repo_dir = Path(repo_dir)
    if _git(repo_dir, "symbolic-ref", "-q", "HEAD").returncode == 0:
        return ff_default_branch(repo_dir)
    default = default_branch(repo_dir)
    if _git(repo_dir, "status", "--porcelain").stdout.strip():
        return "long-job sync: detached worktree has local changes — not moved"
    before = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    if _git(repo_dir, "fetch", "origin", default).returncode != 0:
        return f"long-job sync: fetch origin {default} failed — running on {before[:7]}"
    if _git(repo_dir, "checkout", "-q", "--detach", f"origin/{default}").returncode != 0:
        return f"long-job sync: cannot move to origin/{default} — running on {before[:7]}"
    after = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    return f"long-job sync: detached {before[:7]} -> origin/{default} {after[:7]}" if after != before else ""


def current_branch(repo_dir: Path) -> str:
    """The branch HEAD is on, or `""` when there is no answer.

    Strictly read-only — it exists so a caller can ask "whose working tree is
    this?" *before* `restore_main` forces the answer to be main. Asking must not
    itself move HEAD, which is why this is a separate function rather than
    anything layered onto `restore_main`.

    `""` means two different things — a detached HEAD (`--show-current` prints
    nothing) and git failing outright — and callers deliberately collapse them
    into one "cannot tell". The caller (`StagedExecutor._live_sibling_hold`)
    reads that as fail-open: unknown proceeds, because wedging the queue on
    ignorance is a worse failure than the forced checkout it would prevent.
    """
    r = _git(repo_dir, "branch", "--show-current")
    return r.stdout.strip() if r.returncode == 0 else ""


def release_worktree_holding(repo_dir: Path, branch: str) -> str | None:
    """Free `branch` if a leftover git worktree still has it checked out.

    Agents create their own worktrees to keep work off the shared ~/Worker
    checkout — a sound instinct, since a live process can be running from it —
    but they do not always clean up. A surviving worktree owns the branch, so the
    next run's `checkout -B` dies with "already used by worktree at …" before any
    stage starts. ai-stock-trading-5n failed twice that way on 2026-08-06 and sat
    one refail from poison quarantine.

    Safe because runs are serial per repo: a worktree holding OUR branch at the
    START of a run can only be a leftover from a previous attempt. Removing it
    discards no commits — those live on the branch, which outlives the worktree —
    and the untracked residue is the gitignored caches. `prune` first so
    registrations whose directory is already gone stop blocking on their own.
    Returns the path released, or None.
    """
    _git(repo_dir, "worktree", "prune")
    out = _git(repo_dir, "worktree", "list", "--porcelain").stdout or ""
    path = None
    for block in out.split("\n\n"):
        if f"branch refs/heads/{branch}" in block:
            for line in block.splitlines():
                if line.startswith("worktree "):
                    path = line.split(" ", 1)[1].strip()
            break
    if not path or Path(path).resolve() == Path(repo_dir).resolve():
        return None      # the main checkout itself is not a leftover
    _git(repo_dir, "worktree", "remove", "--force", path)
    return path


def remove_worktree(base_dir: Path, worktree: Path,
                    progress: Callable[[str], None] | None = None) -> bool:
    """Remove a per-run linked worktree after its run SUCCEEDED; return whether it took.

    The switchover resolves an external task to its own `--detach` worktree under
    `<shared clone>.wt/<slug>` (repos.ensure_worktree). A successful run's commits
    are already on origin (the report stage's push / a stage checkpoint), so the
    worktree is pure residue and is reaped here. A failed, parked or timed-out run
    does NOT call this: its worktree is the evidence a retry's `ensure_worktree`
    reuses idempotently, and prepare_external_branch's `checkout -f -B` re-points it.

    `git worktree remove --force` (never `rm -rf` — hard rule) off `base_dir` (the
    shared clone that owns the worktree), then `git worktree prune` to clear the
    admin record. The `worker/<slug>` branch ref is deliberately left alone — not
    ours to delete here, and a success has no retry that would read it. When this
    returns True the runner removes the run's gradle-registry dir right after
    (worktree_sweep.remove_registry); the parked ones are worktree_sweep's.

    Best-effort and NEVER raises: it runs from the runner's `finally`, alongside
    `release_worktree_holding` / `cleanup_build_daemons`, where a raise would mask
    the real outcome. Any failure (missing base, already-removed worktree, timeout)
    degrades to a logged False."""
    try:
        r = _git(base_dir, "worktree", "remove", "--force", str(worktree))
        _git(base_dir, "worktree", "prune")
        ok = r.returncode == 0
        if progress is not None:
            why = (r.stderr or r.stdout or "").strip()[:120]
            progress(f"worktree {'removed' if ok else 'remove failed'}: {worktree}"
                     + ("" if ok or not why else f" ({why})"))
        return ok
    except Exception:       # noqa: BLE001 — finally-called; a raise would mask the outcome
        if progress is not None:
            progress(f"worktree remove errored: {worktree}")
        return False


# How many orphaned shas to spell out in the warning before summarising the rest.
# The point of the list is to be pasteable into `git cherry-pick`; past a couple of
# dozen the human is going to reach for the tip sha and a range anyway.
_ORPHAN_SHA_LIMIT = 20

# Same idea for the untracked paths the clean deletes. A clone that a long job or
# a sibling run filled up can hold thousands of them, and one log line that wide
# makes run.log unreadable — which costs more than the tail of the list is worth.
_CLEANED_PATH_LIMIT = 20


def _already_squash_merged(repo_dir: Path, branch: str, base: str,
                           shas: list[str]) -> bool:
    """Is every commit in `shas` work that a SQUASH merge already landed on base?

    `_orphaned_local_commits` decides "lost" by ancestry (`rev-list base..branch`),
    and a squash merge replays the branch as ONE new commit with a new sha —
    leaving the originals non-ancestors of base forever. So ancestry alone calls
    every cleanly auto-merged worker branch a loss: on 2026-08-23
    `moksori-icon-ui-refresh` warned about 2 "orphaned" commits whose content was
    byte-identical to main, with its PR #4 already merged. Repeated over every
    auto-merged task, that trains the human to ignore the one alert that is real.

    Returns True only for CONFIRMED suppression; anything unproven is False, and
    False means the caller warns exactly as before. Never returns None — a third
    value would blur the one property this has to keep: unknown must alert.
    Three independent conditions, all required:

      1. `base` is not `origin/<branch>`. If the remote branch still exists, then
         commits ahead of it were, by definition, never in any PR — no merge of
         any kind can account for them. This is logic, not a shortcut, and it is
         what stops a REUSED branch (old PR merged, new attempt committing on
         top) from being silenced.
      2. `gh` reports a parseable `mergedAt` for the branch's PR. Absent gh, no
         PR, unauthenticated, network failure, or an open PR all yield None →
         False. "Can't tell" is never "safe"; that reading is what would silence
         a real loss.
      3. EVERY orphan commit is older than that merge. One commit newer than
         `mergedAt` is post-merge work the PR cannot contain, so the whole set
         goes back to being reported — hence no `_ORPHAN_SHA_LIMIT`-style
         truncation here, the check is only sound over the complete set.

    Wrapped whole in `except Exception` on purpose: this runs inside branch prep,
    which must not acquire a new way to fail. A probe that cannot run degrades to
    "warn", the same direction every other unknown takes. Read-only, like the
    detector it guards — it creates no ref, tag or stash and repairs nothing.
    """
    try:
        if base == f"origin/{branch}":
            return False                    # ahead of a live remote branch — never merged
        merged_epoch = _iso_to_epoch(gh_pr_merged_at(branch, cwd=repo_dir))
        if merged_epoch is None:
            return False                    # no confirmed merge signal — warn
        # One call for the whole set: `git show -s --format=%cI a b c` prints
        # exactly one committer date per argument, in argument order.
        r = _git(repo_dir, "show", "-s", "--format=%cI", *shas)
        if r.returncode != 0:
            return False
        dates = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
        if len(dates) != len(shas):
            return False                    # unexpected shape — do not guess
        for d in dates:
            made = _iso_to_epoch(d)
            if made is None or made > merged_epoch:
                return False                # unreadable, or made after the merge
        return True
    except Exception:
        return False


def _orphaned_local_commits(repo_dir: Path, branch: str, base: str) -> dict | None:
    """Commits on the local `branch` that `checkout -B branch base` is about to
    discard, or None when the rewind loses nothing.

    That `checkout -B` is the ONLY step in `prepare_external_branch` that can move
    a local branch ref: the forced checkout of the default branch moves HEAD, the
    hard reset moves the *default* branch, and the untracked-file clean deletes
    working-tree files — none of them touch `refs/heads/<branch>`. So when a prior
    attempt committed here and died before its push, `base` falls back to
    `origin/<default>` and the commits go dangling with nothing written
    anywhere. Hence the check belongs immediately before the
    rewind, and applies to both base choices: unpushed commits stacked on top of a
    pushed `origin/<branch>` are lost exactly the same way.

    Read-only on purpose. Detecting the loss is in scope; undoing it is not —
    recovery stays a human's `git cherry-pick`, so this creates no ref, tag or
    stash. Any git failure degrades to None: a detection that cannot run must
    never become the reason branch prep fails.

    Ancestry alone over-reports, because a SQUASH merge lands the work under a new
    sha and leaves the originals non-ancestors of base for good. So a set that
    `_already_squash_merged` can PROVE was squash-merged (remote branch gone, the
    branch's PR merged, every commit older than that merge) is not a loss and stays
    silent. Everything it cannot prove — including every way of failing to ask — is
    still reported, so this narrows the false alarm without narrowing the alarm.
    """
    if _git(repo_dir, "rev-parse", "--verify", "--quiet",
            f"refs/heads/{branch}").returncode != 0:
        return None                    # first dispatch — no local branch to rewind
    tip = _git(repo_dir, "rev-parse", branch)
    # --reverse => oldest-first, which is the order `git cherry-pick` wants.
    ahead = _git(repo_dir, "rev-list", "--reverse", f"{base}..{branch}")
    if tip.returncode != 0 or ahead.returncode != 0:
        return None
    shas = (ahead.stdout or "").split()
    if not shas:
        return None                    # already contained in base — nothing is lost
    # Only now, with a non-empty doomed set, is it worth paying for a `gh` round
    # trip: on the overwhelmingly common path this returns above and never asks.
    if _already_squash_merged(repo_dir, branch, base, shas):
        return None                    # landed as a squash — dangling, but not lost
    return {"branch": branch, "base": base, "tip": tip.stdout.strip(),
            "count": len(shas), "shas": shas}


def prepare_external_branch(repo_dir: Path, branch: str,
                            progress: Callable[[str], None] | None = None,
                            ) -> subprocess.CompletedProcess:
    """Branch an EXTERNAL worker clone from the LATEST origin/default — never from
    a previous task's leftover HEAD.

    The clone under ~/Worker is reused serially (ensure_local doesn't pull), so
    without this the new `git checkout -B` bases the task branch on the prior
    task's branch → branches stack → PRs conflict and can't auto-merge. Here we
    fetch, then force-create the task branch at the right base (origin/<branch> on
    a retry, else origin/<default>) with a single `git checkout -f -B`, and clean
    untracked. The one checkout goes STRAIGHT to the task branch — it never names
    <default> as a checkout target — so this is safe inside a `--detach` linked
    worktree too, where checking out <default> would die with "'<default>' is
    already used by worktree at …" (the shared clone holds it). On a normal clone
    the end state is identical to the old fetch → reset-to-<default> → cut-branch.
    Safe: this is a throwaway clone (not the vault), a local reset (no force-push),
    and never touches origin/main.
    Retries: if THIS task's branch was already pushed by a prior attempt, we
    CONTINUE it (base on origin/<branch>) so the next push is a fast-forward.
    Re-cutting from origin/<default> instead would create a divergent lineage
    that can never be pushed (non-fast-forward, and force-push is forbidden) —
    a guaranteed deadlock on every retried external-repo task.
    Returns the final `checkout -B` result, carrying two report attributes.
    `orphaned_commits`: None normally, else the dict from `_orphaned_local_commits`
    naming the commits that rewind just dropped (a prior attempt committed them but
    died before pushing). `cleaned_untracked`: None normally, else
    {"count", "paths"} naming the untracked files the `clean -fd` above deleted —
    from in here a sibling run's uncommitted work is indistinguishable from a stale
    leftover, and without this the sweep leaves no trace of either. Optional
    `progress` gets each as one log line.
    All of it is report, not repair — the clean still deletes and the rewind still
    rewinds, and rc is untouched, because a non-zero rc here becomes
    ExecOutcome(False, ...) in the runner, which the orchestrator routes into
    `_retry`: a re-dispatch loop over a condition only a human with
    `git cherry-pick` can clear."""
    _git(repo_dir, "fetch", "origin", "--prune")
    release_worktree_holding(repo_dir, branch)
    default = default_branch(repo_dir)
    # Continue an unfinished prior attempt of this same task if it left a remote
    # branch; otherwise cut fresh from the latest default so we never stack on a
    # *different* task's leftover local HEAD.
    base = f"origin/{branch}" if remote_branch_exists(repo_dir, branch) else f"origin/{default}"
    install_push_guard(repo_dir)
    # Last look before the rewind: say out loud what it is about to throw away.
    # Sampled BEFORE the `checkout -f -B` below, because that single step is the
    # ONLY one that moves `refs/heads/<branch>`; after it the dropped commits are
    # reachable only by sha. Scoped to THIS task's branch only — a *different*
    # task's leftover local branch is still rewound without ceremony, which is
    # what stops branches from stacking.
    orphaned = _orphaned_local_commits(repo_dir, branch, base)
    if orphaned is not None and progress is not None:
        shown = orphaned["shas"][:_ORPHAN_SHA_LIMIT]
        more = orphaned["count"] - len(shown)
        progress(f"orphaning {orphaned['count']} local commit(s) from {branch} "
                 f"(tip {orphaned['tip'][:8]}, base {base}) — not recovered "
                 f"automatically; cherry-pick: git -C {repo_dir} cherry-pick "
                 + " ".join(shown) + (f" ... +{more} more" if more else ""))
    # Force-create <branch> at <base> and switch to it in ONE step: this resets
    # the tracked tree to <base> and discards local tracked edits (the `-f`). It
    # replaces the old `checkout -f <default>` + `reset --hard origin/<default>`
    # + final `checkout -B`, whose first step died in a `--detach` worktree
    # ("'<default>' is already used by worktree at …" — the shared clone holds
    # <default>). Going straight to <branch> never names <default>, so it is
    # worktree-safe; on a normal clone the end state (HEAD on <branch>, tree at
    # <base>, untracked cleaned) is identical.
    cp = _git(repo_dir, "checkout", "-f", "-B", branch, base)
    # Sample the doomed set HERE — after the checkout, before the clean. The tree
    # is already at <base>, so `git clean -fd` on the next line deletes exactly
    # this set (`checkout` leaves untracked files in place; `-f` only overwrites
    # any that collide with a tracked path in <base>). Reading it before the
    # checkout would fold in files the branch switch removes on its own.
    doomed, err = _untracked_paths(repo_dir)
    cleaned = {"count": len(doomed), "paths": doomed} if err is None and doomed else None
    if cleaned is not None and progress is not None:
        shown = cleaned["paths"][:_CLEANED_PATH_LIMIT]
        more = cleaned["count"] - len(shown)
        progress(f"cleaning {cleaned['count']} untracked path(s) in {repo_dir} — "
                 "not recovered automatically: " + " ".join(shown)
                 + (f" ... +{more} more" if more else ""))
    _git(repo_dir, "clean", "-fd")
    cp.orphaned_commits = orphaned
    cp.cleaned_untracked = cleaned
    return cp


def prepare_vault_branch(repo_dir: Path, branch: str,
                         progress: Callable[[str], None] | None = None,
                         ) -> subprocess.CompletedProcess:
    """Put the LIVE vault checkout on `branch`, creating it when it does not exist.

    Deliberately NOT `prepare_external_branch` with a flag. That function opens by
    hard-resetting onto origin's default branch and deleting every untracked file,
    which is correct for a throwaway ~/Worker clone and catastrophic here:
    `repo_dir` is the vault the human works in, and it holds gitignored live state
    (`Todo/items/`, `Worker/runs/`) plus whatever they have half-written. Neither
    of those two destructive steps may ever appear in this function — that is the
    entire reason it is a separate function, so any future edit that "unifies" the
    two has to delete this paragraph first.

    Without this, a vault run had no branch prep at all: `_execute`'s prep lived
    inside `if repo_url:` with no `else:`, so the only thing that ever created
    `worker/<slug>` was a *prompt* line in worker-implement.md, which silently did
    not run (2026-08-24, 2/2) and left the stages committing onto `main`.

    Base choice mirrors the external path's reason, not its value. If a prior
    attempt already pushed `origin/<branch>`, continue from it so the next push
    fast-forwards — re-cutting from local HEAD would build a lineage that can
    never be pushed (force-push is forbidden), the retry deadlock. Otherwise base
    is None, meaning current HEAD: `restore_main` has just put HEAD on the default
    branch, and the live vault's local default legitimately carries sync commits
    that were never pushed, so HEAD is a truer base here than `origin/<default>`.

    Returns the final checkout's CompletedProcess carrying three attributes, in
    the idiom `prepare_external_branch` already uses. `base`: what the branch was
    pointed at (None = HEAD). `orphaned_commits`: None normally, else the dict
    from `_orphaned_local_commits` naming commits the `-B` rewind just dropped —
    report, not repair; recovery is a human's cherry-pick. `problem`: None on
    success, else a one-line reason covering BOTH a non-zero checkout and a failed
    post-condition. The caller turns `problem` into `ExecOutcome(False, ...)`,
    because the failure mode being fixed is a run proceeding quietly on `main`.
    """
    fetched = _git(repo_dir, "fetch", "origin", "--prune")
    # rc deliberately unread: a vault clone without an `origin` (every temp-repo
    # test, and a legitimate offline run) must still get its branch. What the
    # fetch is for is the local `refs/remotes/origin/<branch>` that `checkout -B
    # <branch> origin/<branch>` resolves — `remote_branch_exists` below asks the
    # network directly and would otherwise name a ref that is not here yet.
    released = release_worktree_holding(repo_dir, branch)
    if released and progress is not None:
        progress(f"released leftover worktree holding {branch}: {released}")
    base = None
    if remote_branch_exists(repo_dir, branch):
        base = f"origin/{branch}"
        if _git(repo_dir, "rev-parse", "--verify", "--quiet", base).returncode != 0:
            # origin has the branch but the fetch did not land it locally (offline,
            # auth). Silently falling back to HEAD is the one wrong answer: it cuts
            # a lineage divergent from the pushed one, which can never fast-forward
            # — exactly the deadlock the inheritance exists to prevent. Stop before
            # `-B` moves anything, so a retry finds the branch as it was.
            fetched.base, fetched.orphaned_commits = base, None
            fetched.problem = (
                f"origin/{branch} exists on the remote but is not available "
                f"locally (fetch rc={fetched.returncode}); refusing to re-cut "
                f"{branch} from HEAD")
            return fetched
    # Last look before the rewind: `-B` moves the ref, so say out loud what that
    # drops. base=None means the ref moves to HEAD, so HEAD is what to diff from.
    orphaned = _orphaned_local_commits(repo_dir, branch, base or "HEAD")
    if orphaned is not None and progress is not None:
        shown = orphaned["shas"][:_ORPHAN_SHA_LIMIT]
        more = orphaned["count"] - len(shown)
        progress(f"orphaning {orphaned['count']} local commit(s) from {branch} "
                 f"(tip {orphaned['tip'][:8]}, base {base or 'HEAD'}) — not "
                 f"recovered automatically; cherry-pick: git -C {repo_dir} "
                 "cherry-pick " + " ".join(shown)
                 + (f" ... +{more} more" if more else ""))
    cp = git_checkout(repo_dir, branch, create=True, base=base)
    cp.base, cp.orphaned_commits = base, orphaned
    # Post-condition, not decoration. rc==0 is not the property the caller needs —
    # "the branch exists AND we are standing on it" is, and the whole bug class
    # here is a run that believed it had a branch and committed to main instead.
    # Both halves: the ref can exist while HEAD sits elsewhere (detached).
    ref_ok = _git(repo_dir, "rev-parse", "--verify", "--quiet",
                  f"refs/heads/{branch}").returncode == 0
    # An UNBORN HEAD is the one case where a missing ref is not a failure: in a
    # repo with no commits, `checkout -B` legitimately returns 0 and points HEAD
    # at a branch that cannot exist until something is committed to it. The
    # invariant being defended is "the next commit does not land on the default
    # branch", and an unborn `worker/<slug>` satisfies it exactly — the first
    # commit creates the ref there. Measured, not assumed: demanding the ref
    # unconditionally failed two existing vault tests whose fixture is a bare
    # `git init` (test_stages.py VaultPushGuardInstallTest).
    unborn = _git(repo_dir, "rev-parse", "--verify", "--quiet", "HEAD").returncode != 0
    landed = current_branch(repo_dir)
    if cp.returncode != 0:
        cp.problem = (f"git checkout -B {branch} failed (rc={cp.returncode}): "
                      + ((cp.stderr or cp.stdout or "").strip()[:200] or "no output"))
    elif not ref_ok and not unborn:
        cp.problem = f"refs/heads/{branch} does not exist after checkout -B"
    elif landed != branch:
        cp.problem = (f"checkout -B {branch} reported success but HEAD is on "
                      f"{landed or 'a detached commit'}")
    else:
        cp.problem = None
    return cp


def _iso_to_epoch(ts: str | None) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(ts)).timestamp()
    except (TypeError, ValueError):
        return None


def _untracked_paths(repo_dir: Path) -> tuple[list[str], str | None]:
    """The repo-relative paths `git clean -fd` would delete. Returns (paths, error).

    `-z` because a NUL-separated record can hold any byte a path can; the default
    format quotes and escapes non-ASCII names, so a parser reading it would hand
    back a path that does not exist. `-uall` lists the files INSIDE an untracked
    directory instead of just the directory — the set we have to move one file at
    a time. Rename/copy records spend a second record on their source path, which
    must be consumed so it is never mistaken for a status line of its own.
    """
    r = _git(repo_dir, "status", "--porcelain", "-uall", "-z")
    if r.returncode != 0:
        return [], (r.stderr or r.stdout or "git status failed").strip()[:200]
    recs = [x for x in (r.stdout or "").split("\0") if x]
    out, i = [], 0
    while i < len(recs):
        rec, i = recs[i], i + 1
        xy, path = rec[:2], rec[3:]
        if xy[:1] in ("R", "C"):
            i += 1                      # skip the rename/copy source record
        if xy == "??":
            out.append(path)
    return out, None


def snapshot_job_artifacts(repo_dir: Path, job_cwd: Path | str,
                           started_ts: str | None) -> dict:
    """Lift a just-finished `long_job`'s output OUT of an external clone so branch
    prep cannot eat it. Call BEFORE prepare_external_branch; restore after.

    The runner defers a task until its `long_job` exits and then, three lines
    later, runs `prepare_external_branch` — whose `git clean -fd` deletes exactly
    the untracked files the job spent its runtime producing. The pipeline waits
    for complete data and then starts the stages the instant after deleting it:
    rc=0, no data, nothing in the logs.

    The fix is deliberately NOT to weaken that clean. The reset/clean is what
    stops a previous task's leftovers from contaminating this run and what keeps
    task branches from stacking (see prepare_external_branch); "preserve
    everything" would trade this bug for those. Instead we move only THIS job's
    output out of the clean's reach and put it back afterwards.

    "This job's output" is an intersection of three independent facts, all
    required:

      1. git reports it `??` — precisely the set `clean -fd` removes. That clean
         carries no `-x`, so ignored files were never at risk; they are left
         alone here too rather than quietly gaining a new owner.
      2. it lives under the job's own cwd — the directory the runner handed to
         `jobs.start`.
      3. its mtime is at or after the job's `started_ts`, which is stamped before
         the job is launched and floored to the second. So it is a true lower
         bound and `>=` needs no skew constant; a leftover from a DIFFERENT task
         is older and still gets cleaned, which is the whole point of (3).

    A fresh untracked file that satisfies (1) and (3) but not (2) cannot be
    explained as another task's residue either, so it goes to `skipped_outside`
    and becomes a problem rather than being dropped on the floor — silence there
    would recreate this same bug one directory over. Nothing is moved in that
    case: the caller aborts before the clean, so the data is still on disk.

    Empty directories are invisible to git and therefore cannot be preserved; a
    job whose output is an empty tree is outside what this can do.

    Returns {"dir", "files", "skipped_outside", "problems"}. A non-empty
    `problems` means the caller must NOT proceed to branch prep.
    """
    repo_dir = Path(repo_dir)
    stash = repo_dir.parent / f".{repo_dir.name}-longjob-stash"
    snap: dict = {"dir": str(stash), "files": [], "skipped_outside": [], "problems": []}
    ref = _iso_to_epoch(started_ts)
    if ref is None:
        snap["problems"].append(
            f"job start time unreadable ({started_ts!r}) — cannot tell this job's "
            "output from another task's leftovers")
        return snap
    if stash.exists():
        # The stash only survives a FAILED restore, so this is a human's recovery
        # copy. Moving onto it would destroy the thing it was kept for.
        snap["problems"].append(
            f"a stash from an earlier failed preservation is still at {stash}")
        return snap
    paths, err = _untracked_paths(repo_dir)
    if err:
        snap["problems"].append(f"could not list untracked files: {err}")
        return snap
    # Resolve both roots (macOS hands out /var → /private/var symlinks) and then
    # compare lexically, so an untracked SYMLINK is judged by where it sits, not
    # by where it points.
    repo_root, job_root = repo_dir.resolve(), Path(job_cwd).resolve()
    inside: list[str] = []
    for rel in paths:
        try:
            if (repo_dir / rel).lstat().st_mtime < ref:
                continue                # older than the job: someone else's residue
        except OSError:
            continue                    # vanished between status and now
        try:
            (repo_root / rel).relative_to(job_root)
        except ValueError:
            snap["skipped_outside"].append(rel)
            continue
        inside.append(rel)
    if snap["skipped_outside"]:
        snap["problems"].append(
            "fresh untracked output outside the job's cwd (not preserved, not "
            "explainable as another task's leftover): "
            + ", ".join(sorted(snap["skipped_outside"])[:5]))
        return snap                     # nothing moved yet — the clone is untouched
    for rel in inside:
        dest = stash / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(repo_dir / rel), str(dest))
        except OSError as e:
            snap["problems"].append(f"could not stage {rel}: {e}")
            return snap                 # partial stash, but still nothing DELETED
        snap["files"].append(rel)
    return snap


def restore_job_artifacts(repo_dir: Path, snap: dict) -> list[str]:
    """Put a `snapshot_job_artifacts` stash back, and say so when it cannot.

    Call after `checkout -B`, so the tree is the fresh branch and every stashed
    path should be free. If one is not, the branch legitimately owns that path
    and dropping a job artifact over it would be an invisible edit — refuse and
    report instead. The stash is removed only when every file made it back;
    whatever is left is a recovery copy for a human, and the caller names its
    location. The original symptom of this bug was rc=0 with missing files, so
    every way this can go wrong returns a string rather than nothing.

    Returns [] on full success, else one line per file that did not make it.
    """
    stash = Path(snap.get("dir") or "")
    problems: list[str] = []
    for rel in snap.get("files") or []:
        src, dest = stash / rel, Path(repo_dir) / rel
        if not (src.exists() or src.is_symlink()):
            problems.append(f"missing from the stash: {rel}")
            continue
        if dest.exists() or dest.is_symlink():
            problems.append(f"path already taken on the branch: {rel}")
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        except OSError as e:
            problems.append(f"could not restore {rel}: {e}")
    if not problems:
        shutil.rmtree(stash, ignore_errors=True)
    return problems


# A stage's tool whitelist that grants the broad `Bash(git:*)` (this repo's own
# tasks do) passes FORBIDDEN_TOOL_SUBSTRINGS — that filter inspects the whitelist
# STRING, and `Bash(git:*)` contains no `git push --force` substring — yet lets
# the agent run `git push --force-with-lease` at RUNTIME. ai-stock-trading-13's
# verify stage used exactly that to overwrite a sibling run's lineage, turning a
# human's "which lineage do we keep" decision into a force-push. A prompt-text
# rule did not hold; this hook enforces it at the git layer, where the whitelist
# cannot reach. A fast-forward push (the normal case) is untouched. `--no-verify`
# would skip the hook, but that flag is itself blocked in FORBIDDEN_TOOL_SUBSTRINGS.
_PUSH_GUARD_HOOK = """#!/bin/sh
# Installed by MaestroACP stages.install_push_guard — reject forced /
# non-fast-forward pushes (ai-stock-trading-13 lineage-overwrite incident).
z=0000000000000000000000000000000000000000
while read -r local_ref local_sha remote_ref remote_sha; do
    if [ "$local_sha" = "$z" ]; then
        echo "pre-push: refusing to delete $remote_ref (forced deletion blocked)" >&2
        exit 1
    fi
    [ "$remote_sha" = "$z" ] && continue   # brand-new branch: always a fast-forward
    git merge-base --is-ancestor "$remote_sha" "$local_sha"
    case $? in
        0) ;;   # remote tip is an ancestor of what we push — fast-forward, allow
        1) echo "pre-push: refusing non-fast-forward push to $remote_ref" >&2
           echo "pre-push: force-push is a hard rule (ai-stock-trading-13)." >&2
           exit 1 ;;
        *) ;;   # indeterminate (e.g. remote object missing locally) — do not halt
    esac
done
exit 0
"""


def install_push_guard(repo_dir: Path) -> Path:
    """Install a pre-push hook that refuses forced / non-fast-forward pushes.

    Hooks live under the git common dir (not the worktree), so `git clean -fd`
    and `reset --hard` never touch them and the guard persists across the serial
    reuse of a ~/Worker clone. `--git-path hooks` resolves correctly for both a
    plain repo and a linked worktree. Returns the hook path."""
    r = _git(repo_dir, "rev-parse", "--git-path", "hooks")
    out = (r.stdout or "").strip() if r.returncode == 0 else ""
    if not out:
        # A failed/empty rev-parse made this Path("") → the hook landed at the repo
        # ROOT as an untracked `pre-push` (seen in the live vault 2026-10-16 and then
        # committed by a broad `git add -A`). Fall back to a plain repo's .git/hooks;
        # anything else (a linked worktree's .git file) is skipped, never the root.
        if (repo_dir / ".git").is_dir():
            out = str(Path(".git") / "hooks")
        else:
            print(f"[stages] push guard not installed: cannot resolve hooks dir for {repo_dir}")
            return repo_dir / ".git" / "hooks" / "pre-push"
    hooks_dir = Path(out)
    if not hooks_dir.is_absolute():
        hooks_dir = repo_dir / hooks_dir
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-push"
    hook.write_text(_PUSH_GUARD_HOOK, encoding="utf-8")
    hook.chmod(0o755)
    return hook


def remote_branch_exists(repo_dir: Path, branch: str) -> bool:
    """True if origin has `branch` (a prior attempt pushed it). After a successful
    squash-merge the branch is deleted, so its presence means work to continue."""
    r = _git(repo_dir, "ls-remote", "--heads", "origin", branch)
    return r.returncode == 0 and bool(r.stdout.strip())


def branch_commits_ahead(repo_dir: Path, branch: str, base: str = "main") -> int:
    """How many commits `branch` carries that `base` does not. -1 = UNKNOWN.

    `base` is a LOCAL branch name (`main`), never `origin/main`. In the live vault
    the local default legitimately carries `chore: sync vault state` commits the
    todo-sync daemon has not pushed yet; counting from `origin/main` would score
    those as the worker branch's own work and publish a branch that did nothing.
    Same reasoning `prepare_vault_branch` uses to prefer HEAD over origin/<default>.

    -1 rather than 0 on failure because the two must not be confused by callers:
    0 means "measured, there is nothing to publish", -1 means "no measurement" —
    and `publish_branch` refuses to push on the second."""
    r = _git(repo_dir, "rev-list", "--count", f"{base}..{branch}")
    if r.returncode != 0:
        return -1     # bad ref, not a repo, git missing — unknown, never "zero"
    try:
        return int((r.stdout or "").strip())
    except (TypeError, ValueError):
        return -1


_PR_URL_RE = re.compile(r"https?://\S*?/pull/\d+")


def _gh_open_pr_url(repo_dir: Path, branch: str) -> str | None:
    """URL of the OPEN PR for `branch`, else None (incl. every failure mode).

    Filtered on state so a CLOSED or MERGED PR from an earlier attempt does not
    satisfy "a PR is open for this branch" — the branch has new commits or
    `publish_branch` would not be asking, and those commits need a live PR."""
    try:
        r = subprocess.run(["gh", "pr", "view", branch, "--json", "url,state"],
                           cwd=str(repo_dir), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None      # gh not installed / timed out — treat as "no PR known"
    if r.returncode != 0:
        return None      # no PR for the branch, not authenticated, etc.
    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or str(data.get("state", "")).upper() != "OPEN":
        return None
    return str(data["url"]) if data.get("url") else None


def publish_branch(repo_dir: Path, branch: str, base: str | None = None,
                   body: str = "",
                   progress: Callable[[str], None] | None = None) -> dict:
    """Push `branch` to origin and leave a PR open for it. Best-effort; NEVER raises.

    The bug this closes: a `worker_mode: delegate` vault run committed its work to
    `worker/<slug>` and then `restore_main` put HEAD back on main, abandoning the
    branch on the local disk. Nothing ever pushed it — worker-delegate.md's own
    "Never push." rule is about the *agent*, and no runner code filled the gap —
    so three runs' output sat in a branch nobody would ever look at.

    Returns `{"commits": int, "pushed": bool, "pr_url": str|None, "note": str}`.
    `commits` is `branch_commits_ahead`'s answer, so -1 there survives to the
    caller. Every subprocess is timed out and every failure is swallowed into
    `note`: publishing is a courtesy at the END of a run, and a `gh` hiccup must
    never turn an otherwise finished run into a failed one (the same fail-open
    contract `gh_pr_merged` keeps with None-means-unknown).

    Deliberately does NOT merge. There is no `gh pr merge` here and there must
    never be one: the vault's `main` is shared with a live todo-sync daemon that
    commits to it on a timer, so an auto-merge from a worker run races it. The
    deliverable is an OPEN PR; a human moves it to main.
    """
    out: dict = {"commits": 0, "pushed": False, "pr_url": None, "note": ""}
    base = base or default_branch(repo_dir)
    ahead = branch_commits_ahead(repo_dir, branch, base)
    out["commits"] = ahead
    if ahead == 0:
        out["note"] = f"{branch} has nothing {base} lacks — nothing to publish"
        return out
    if ahead < 0:
        # Unknown is NOT a reason to push. A push is the irreversible half of this
        # function (the pre-push guard blocks deleting the ref afterwards), so the
        # conservative direction on "cannot count" is to do nothing and say so.
        out["note"] = f"could not count {base}..{branch}; not pushing on an unknown"
        return out
    try:
        pr = subprocess.run(["git", "-C", str(repo_dir), "push", "origin", branch],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        out["note"] = f"push of {branch} failed: {type(e).__name__}"
        return out
    if pr.returncode != 0:
        out["note"] = (f"push of {branch} rejected: "
                       + ((pr.stderr or pr.stdout or "").strip()[:200] or "no output"))
        return out
    out["pushed"] = True
    if progress is not None:
        progress(f"pushed {branch} ({ahead} commit(s) ahead of {base})")
    existing = _gh_open_pr_url(repo_dir, branch)
    if existing:
        # Idempotent across retries: a re-run of the same slug pushes the extra
        # commits onto the SAME branch, which the open PR already tracks. Calling
        # `gh pr create` here is what would produce duplicate PRs for one task.
        out["pr_url"] = existing
        out["note"] = f"pushed {ahead} commit(s); PR already open"
        return out
    t = _git(repo_dir, "log", "-1", "--format=%s", branch)
    title = (t.stdout or "").strip() if t.returncode == 0 else ""
    try:
        cr = subprocess.run(["gh", "pr", "create", "--base", base, "--head", branch,
                             "--title", title or branch, "--body", body],
                            cwd=str(repo_dir), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        out["note"] = f"pushed {ahead} commit(s); gh pr create unavailable ({type(e).__name__})"
        return out
    if cr.returncode != 0:
        out["note"] = (f"pushed {ahead} commit(s); gh pr create failed: "
                       + ((cr.stderr or cr.stdout or "").strip()[:200] or "no output"))
        return out
    m = _PR_URL_RE.search(cr.stdout or "")
    out["pr_url"] = m.group(0) if m else None
    out["note"] = (f"pushed {ahead} commit(s); opened {out['pr_url']}" if out["pr_url"]
                   else f"pushed {ahead} commit(s); gh pr create returned no URL")
    if progress is not None:
        progress(out["note"])
    return out


def merge_pr(repo_dir: Path, branch: str, repo: str | None = None,
             progress: Callable[[str], None] | None = None) -> dict:
    """Server-side squash-merge the open PR for `branch`. Best-effort; NEVER raises.

    Returns `{"merged": bool, "note": str}`. `merged` is True ONLY on a `gh pr
    merge` that returned rc=0; every other outcome — no `gh`, not authenticated,
    timeout, a CONFLICTING PR, no permission, no PR for the branch — returns False
    with the reason in `note`. The conservative direction is the whole point: a
    delegate task whose merge confirmed is closed `done`, but one that merely
    could-not-confirm must PARK for a human rather than close `done` on a PR that
    is still open. That false close is the 2026-10-12 incident this helper exists
    to prevent (auto_merge:true delegate PRs opened 'Not auto-merged' and closed
    done without ever merging, auto-unblocking their downstream).

    Mirrors the Report stage's merge exactly (`gh pr merge <branch> --squash
    --delete-branch`, worker-report.md:240) — a server-side squash, NOT a local
    `main` push, so it never races the todo-sync daemon that owns the vault's
    `main` (the reason `publish_branch` refuses to merge there). NEVER `--admin`
    or `--auto`: these repos have no branch protection, so a refused plain merge
    is a finding to surface, not something to force through.
    """
    out: dict = {"merged": False, "note": ""}
    args = ["gh", "pr", "merge", branch, "--squash", "--delete-branch"]
    if repo:
        args += ["--repo", repo]
    try:
        r = subprocess.run(args, cwd=str(repo_dir), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        out["note"] = f"gh pr merge of {branch} unavailable ({type(e).__name__})"
        return out
    if r.returncode != 0:
        out["note"] = (f"gh pr merge of {branch} failed: "
                       + ((r.stderr or r.stdout or "").strip()[:200] or "no output"))
        return out
    out["merged"] = True
    out["note"] = f"squash-merged {branch}"
    if progress is not None:
        progress(out["note"])
    return out


def gh_pr_state(branch: str, repo: str | None = None,
                cwd: Path | str | None = None) -> str | None:
    """The PR state for `branch` as gh's upper-case string, or None.

    `"MERGED" | "OPEN" | "CLOSED"`, else None — the same non-committal UNKNOWN
    `gh_pr_merged` keeps: no `gh`, no PR for the branch, unauthenticated, timeout,
    or any parse error. Callers must read None as "cannot tell", never as a
    definite state — so a gh hiccup can never, e.g., alert on a PR as
    closed-unmerged when it is actually still open."""
    args = ["gh", "pr", "view", branch, "--json", "state"]
    if repo:
        args += ["--repo", repo]
    try:
        r = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("state"):
        return None
    return str(data["state"]).upper()


def checkpoint_push(repo_dir: Path, branch: str,
                    progress: Callable[[str], None] | None = None) -> dict:
    """Push `branch` to origin to SAVE local commits a stage left behind — a
    timed-out stage's partial work, or a just-finished `implement`. No PR,
    no merge — just get the work off the dying run's disk and onto origin/<branch>
    so the next dispatch's prepare_external_branch CONTINUES from it instead of
    rewinding it.

    The bug this closes: a heavy `implement` that hits the stage timeout has
    committed real work to the clone's worker/<branch>, but TimeoutExpired escaped
    the pipeline before any stage pushed, `restore_main` walked the clone off the
    branch, and the retry's branch prep hard-reset it away (mulkko-32,
    2026-09-13). This is the checkpoint that survives that.

    Returns `{"pushed": bool, "tip": str|None, "commits": int, "note": str}`.
    `tip` is the pushed sha and is populated ONLY when a push actually lands: a
    no-op (nothing ahead), an UNKNOWN count, a default-branch refusal or a push
    that fails all leave it None. Callers record it as `checkpoint_sha`
    (record_timeout/record_capacity), and the resume note reads a non-None sha as
    "origin/<branch> carries the partial work (tip …)" — so recording a sha for
    work that was never pushed makes the next dispatch's note claim a checkpoint
    that does not exist (defect A, 2026-09-20).
    Pushes ONLY when the branch is ahead of its checkpoint base — origin/<branch>
    if a prior checkpoint already exists, else origin/<default>; an empty push is
    a no-op worth skipping, and a `commits < 0` UNKNOWN is never a reason to push.
    Fast-forward only: install_push_guard is re-asserted and rejects a non-FF push,
    which is folded into `note`, never raised — losing a checkpoint must not turn
    into losing the run's own exception. NEVER raises. Deliberately no `gh`: unlike
    publish_branch this is not a deliverable, so it opens nothing a human must close.
    """
    out: dict = {"pushed": False, "tip": None, "commits": 0, "note": ""}
    tip = _git(repo_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    if tip.returncode != 0 or not tip.stdout.strip():
        out["note"] = f"no local {branch} to checkpoint"
        return out
    tip_sha = tip.stdout.strip()      # recorded in out["tip"] ONLY after a push lands
    default = default_branch(repo_dir)
    if branch == default:
        # A payload `branch` override can name the default branch, and an FF push
        # of it would sail past install_push_guard (which only rejects non-FF
        # pushes and deletions) straight onto origin/<default>. A checkpoint is
        # only ever meant for a worker/* branch, so refuse before touching origin.
        out["note"] = f"refusing to checkpoint default branch {branch}"
        return out
    install_push_guard(repo_dir)      # FF-only guard; idempotent
    base = (f"origin/{branch}" if remote_branch_exists(repo_dir, branch)
            else f"origin/{default}")
    ahead = branch_commits_ahead(repo_dir, branch, base)
    out["commits"] = ahead
    if ahead == 0:
        out["note"] = f"{branch} has nothing beyond {base} — no checkpoint needed"
        return out
    if ahead < 0:
        out["note"] = f"could not count {base}..{branch}; not pushing on an unknown"
        return out
    try:
        pr = subprocess.run(["git", "-C", str(repo_dir), "push", "origin", branch],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        out["note"] = f"checkpoint push of {branch} failed: {type(e).__name__}"
        return out
    if pr.returncode != 0:
        out["note"] = (f"checkpoint push of {branch} rejected: "
                       + ((pr.stderr or pr.stdout or "").strip()[:200] or "no output"))
        return out
    out["pushed"] = True
    out["tip"] = tip_sha              # only now, on a real push — every early return
                                      # above leaves it None so no false checkpoint sha
    out["note"] = (f"checkpoint pushed {ahead} commit(s) to origin/{branch} "
                   f"(tip {tip_sha[:8]})")
    if progress is not None:
        progress(out["note"])
    return out


_CAPACITY_MARKERS = ("out of usage credits", "usage limit", "weekly limit",
                     "hit your limit", "rate limit", "overloaded", "please run /login",
                     "not logged in",
                     # 2026-09-01: CLI rebrand — "You've reached your Fable 5
                     # limit. Run /usage-credits to continue or switch models
                     # with /model." doesn't contain any marker above (not
                     # "usage limit", not "hit your limit"), so is_capacity_limit
                     # returned False and mulkko-5-ladder-editor's capacity stall
                     # was misfiled as a real defect (refail_count 0→2, one more
                     # from poison quarantine). "/usage-credits" is the one
                     # substring the CLI's own suggested command guarantees.
                     "reached your", "/usage-credits")


def is_capacity_limit(text: str) -> bool:
    """True if a stage failed because the account hit a usage/rate limit or lost
    auth — a transient capacity constraint, NOT a task defect. Treating these as
    task failures wrongly burns refails and poison-quarantines healthy tasks."""
    t = (text or "").lower()
    return any(mrk in t for mrk in _CAPACITY_MARKERS)


def usage_shows_no_api_work(rec: dict | None) -> bool:
    """True iff a stage's usage record proves the invocation logged ZERO API work
    — the fingerprint of a refused call (quota/auth). A refused call leaves only
    the CLI's own `<synthetic>` notice in the transcript, so `transcript_usage`
    returns None and `stage_usage` records `{"unavailable": "no usage in
    transcript"}` (no rc suffix when rc == 0).

    The rc == 0 half of the runner's capacity gate: at rc != 0 the returncode
    alone defers, but a 2026-07-31-shape quota-cut can print the out-of-credits
    notice and still exit 0 — there this usage signature is the only reliable
    discriminator, and the runner ANDs it with `is_capacity_limit(stdout)`.

    Strictly narrower than `is_capacity_limit()` on purpose (like
    `invalidate_model_cache_if_unavailable`): a green stage that did real API work
    — even one that merely *quotes* a capacity marker, e.g. this file's own
    `_CAPACITY_MARKERS` — carries a real usage dict with token counts and no
    "unavailable" key, so it returns False and is never mis-deferred. `None` (the
    `MACP_STAGE_USAGE` kill switch off, or a stub argv), `{"unavailable":
    "timeout"}`, a CLI-crash `"...transcript not found"`, and every other
    "unavailable" reason also return False: only the exact "no usage in
    transcript" prefix — a session that ran but logged nothing — counts."""
    return (isinstance(rec, dict)
            and str(rec.get("unavailable", "")).startswith("no usage in transcript"))


def invalidate_model_cache_if_unavailable(text: str) -> bool:
    """Drop the 24h model-choice cache when stage output says the *selected
    model* is unusable, so the next dispatch re-probes instead of re-picking the
    dead model for another day.

    Strictly narrower than `is_capacity_limit()` above: that one also fires on
    lost auth and transient overload, neither of which a different model fixes.
    Deferred import + swallowed exceptions match `stage_model()` — this is a
    best-effort side effect on the hot stage path and must never break a run.
    """
    try:
        from .model_select import invalidate_cache_if_unavailable
        return invalidate_cache_if_unavailable(text)
    except Exception:
        return False


STAGE_PROGRESS = "stages.json"
# Stages worth carrying over from a crashed run. The creative prefix is where
# nearly all the cost is (research 40 + plan 40 + implement 200 max_turns);
# verify and report deliberately ALWAYS re-run, because their job is to read
# live state (CI conclusion, PR mergeability) that a stale artifact would
# misreport.
RESUMABLE = ("research", "plan", "implement")


def _update_progress(run_dir: Path, **fields) -> None:
    """Read-modify-write stages.json, touching only the named keys.

    It used to be write-only (`json.dumps({"completed": done})`), which silently
    destroyed every sibling key on the next stage completion — so anything else
    the run wanted to record here had a lifetime of one stage. Same best-effort
    contract as before: a corrupt or unwritable file costs a redundant re-run,
    never a dead run, so a damaged file is treated as `{}` rather than raised."""
    p = Path(run_dir) / STAGE_PROGRESS
    try:
        cur = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        if not isinstance(cur, dict):
            cur = {}
    except (OSError, ValueError, json.JSONDecodeError):
        cur = {}
    cur.update(fields)
    try:
        p.write_text(json.dumps(cur, indent=1), encoding="utf-8")
    except (OSError, ValueError, TypeError):
        pass


def record_stage(run_dir: Path, stage: str) -> None:
    """Note that `stage` finished and produced its artifact, so a later attempt
    can tell 'this stage really ran' from 'its artifact happens to exist'.
    `implement`'s artifact is run.log, which the runner writes for EVERY stage —
    file presence alone can never answer that question. Best-effort: a failure
    to record costs a redundant re-run, never correctness."""
    done = completed_stages(run_dir)
    if stage not in done:
        done.append(stage)
    _update_progress(run_dir, completed=done)


def record_model_tier(run_dir: Path, tier: str, reason: str) -> None:
    """Persist which model tier the run was classified into and why, so a
    surprising model in run.log can be traced to the field that chose it.
    Best-effort like every other stages.json write."""
    _update_progress(run_dir, model_tier=tier, model_tier_reason=reason)


def record_account(run_dir: Path, account: str) -> None:
    """Persist which claude account the run executed as — the structured
    counterpart to run.log's `account: <name>` line, so a reader (and the
    dashboard) can tie a run to the account that spent on it. The runner-side
    multi-account mirror of `record_model_tier`. Best-effort like every other
    stages.json write."""
    _update_progress(run_dir, account=account)


def record_orphaned_commits(run_dir: Path, orphaned: dict) -> None:
    """Persist what branch prep threw away, in the run's own structured record.

    prepare_external_branch already names the dropped commits, but only to
    `progress` — one line in run.log, a file nobody opens unless they already
    suspect something. The shas are the whole value: they stay dangling (not
    deleted) until git gc runs, so a human can `git cherry-pick` them days later
    — but only if they can still find them. Recorded verbatim for that reason.

    A report, not a repair: recovery stays a human's call (see
    Todo/items/macp-branch-prep-orphans-local-commits.md)."""
    if not orphaned:
        return                       # None = nothing was lost = nothing to record
    _update_progress(run_dir, orphaned_commits=orphaned)


def record_disk_guard(run_dir: Path, record: dict) -> None:
    """Persist what the run-start disk guard did (orchestrator/disk_guard.py):
    free space before and after, the idle clones it cleaned, the clones it
    skipped and why.

    Its one progress line says the same, but in run.log, where nobody looks
    until something has already gone wrong; a run that still died of ENOSPC is
    the moment someone asks "did the guard even fire?", and this answers it.
    The runner calls this only when the guard acted, so a run on a healthy
    disk has no key at all (the orphaned_commits rule). Best-effort like every
    other stages.json write."""
    if not record:
        return
    _update_progress(run_dir, disk_guard=record)


def record_timeout(run_dir: Path, stage: str, checkpoint_sha: str | None = None) -> None:
    """Persist that `stage` hit its subprocess timeout, plus the sha checkpoint_push
    saved to origin (None when nothing was pushed). `previous_timeout_context`
    reads this so a retry continues the partial work rather than restarting from
    scratch. Best-effort, like every other stages.json write."""
    _update_progress(run_dir, timed_out_stage=stage, checkpoint_sha=checkpoint_sha)


def record_capacity(run_dir: Path, stage: str, checkpoint_sha: str | None = None) -> None:
    """Persist that `stage` was DEFERRED on a usage/rate limit (or lost auth), plus
    the sha checkpoint_push saved to origin (None when nothing was pushed). The
    capacity-branch mirror of `record_timeout`: `previous_capacity_context` reads
    it so a retry continues the partial work rather than restarting from scratch.
    `checkpoint_sha` shares its key with the timeout path — harmless, since a run
    hits at most one of the two early returns and each reader gates on its own
    `*_stage` key. Best-effort, like every other stages.json write."""
    _update_progress(run_dir, capacity_limited_stage=stage, checkpoint_sha=checkpoint_sha)


def _argv_value(argv, flag: str) -> str | None:
    try:
        args = list(argv)
        return str(args[args.index(flag) + 1])
    except Exception:
        return None


def _default_config_dir() -> Path:
    """The config dir for a stage env that carries no CLAUDE_CONFIG_DIR: the
    active account's (macp.config.active_config_dir — project pin → profile
    active_account → priority, resolved from the cwd), else the legacy single
    dir (accounts.LEGACY_CONFIG_DIR). Function-local imports keep this module
    stdlib-only at import time; never raises."""
    try:
        from ..macp.config import active_config_dir
        return active_config_dir()
    except Exception:
        from ..macp.accounts import legacy_config_dir
        return legacy_config_dir()


def transcript_path(session_id: str, env: dict | None = None) -> Path | None:
    """Where the CLI wrote the transcript of `session_id`:
    `<config dir>/projects/<cwd slug>/<session_id>.jsonl`. The config dir is the
    stage env's CLAUDE_CONFIG_DIR — StagedExecutor._account_env sets it per
    account — else the active account's dir, else the legacy single dir
    (`_default_config_dir`). Globbed over the slug rather than re-deriving the
    CLI's cwd encoding: the id alone is unique."""
    e = os.environ if env is None else env
    cfg = e.get("CLAUDE_CONFIG_DIR")
    root = Path(cfg).expanduser() if cfg else _default_config_dir()
    hits = sorted((root / "projects").glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def transcript_usage(lines, *, model_hint: str | None = None) -> dict | None:
    """Sum the API usage in one session transcript (an iterable of its JSONL
    lines). Pure; None when it holds no assistant usage at all.

    Shape as claude 2.1.277 writes it (tests/fixtures/claude_output/):
    - tokens: the four `message.usage` counters over DISTINCT `message.id`s. The
      CLI writes one line per content block and repeats the message's usage on
      each, so a thinking + text reply is two lines with the same numbers and a
      plain sum doubles it. Per id the larger value wins, which also absorbs an
      early block that carries a partial count.
    - num_turns: distinct API messages. (The CLI's own num_turns counts a turn it
      refused too — error_max_turns reports 2 for one message.)
    - model: the most frequent `message.model`, a tie going to `model_hint` (the
      argv --model, exact or as a prefix). `<synthetic>` marks a notice the CLI
      wrote itself with zero usage — not an API message, so it is skipped.
    - total_cost_usd: `totalCostUSD` of the last `cost-state` line — the CLI's own
      API-price estimate, the figure `--output-format json` reports. None when
      the process died before writing it, and always on a CLI older than 2.1.277:
      the line is new there (none of this host's 424 `-p` transcripts from
      2.1.268–2.1.274 has one). The token counts do not depend on it."""
    msgs: dict[str, dict] = {}
    cost = None
    for line in lines:
        if "assistant" not in line and "cost-state" not in line:
            continue                 # most of a transcript is multi-KB context attachments
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict):
            continue
        if d.get("type") == "cost-state":
            c = d.get("totalCostUSD")
            if isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c):
                cost = c
            continue
        msg = d.get("message") if d.get("type") == "assistant" else None
        if not isinstance(msg, dict):
            continue
        mid, usage, model = msg.get("id"), msg.get("usage"), msg.get("model")
        if not mid or not isinstance(usage, dict) or model == "<synthetic>":
            continue
        rec = msgs.setdefault(str(mid), {**dict.fromkeys(USAGE_TOKEN_KEYS, 0),
                                         "model": model if isinstance(model, str) else None})
        for k in USAGE_TOKEN_KEYS:
            v = usage.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and v > rec[k]:
                rec[k] = v
    if not msgs:
        return None
    counts = Counter(r["model"] for r in msgs.values() if r["model"])
    hint = model_hint or ""
    ranked = sorted(counts, key=lambda m: (-counts[m], not (hint and m.startswith(hint))))
    out = {k: sum(r[k] for r in msgs.values()) for k in USAGE_TOKEN_KEYS}
    out.update(num_turns=len(msgs), model=ranked[0] if ranked else None, total_cost_usd=cost)
    return out


def stage_usage(argv, *, env: dict | None = None, returncode: int | None = None,
                duration_ms: int | None = None, timed_out: bool = False) -> dict | None:
    """One stage invocation's stages.json record: the USAGE_KEYS, or
    {"unavailable": "<why>"}. None — record nothing — when `argv` carries no
    session id (kill switch off, or a stub argv). `duration_ms` is the runner's
    own wall clock for the subprocess."""
    sid = argv_session_id(argv)
    if sid is None:
        return None
    if timed_out:
        # Killed mid-stage: the transcript stops wherever the kill landed, and a
        # sum over it would be an undercount dressed up as the stage's figure.
        return {"unavailable": "timeout"}
    failed = returncode not in (0, None)
    path = transcript_path(sid, env)
    if path is None:
        # No session file at all: with rc != 0 the CLI died before it started one.
        return {"unavailable": (f"cli crash (rc={returncode}): " if failed else "")
                + "transcript not found"}
    with path.open(encoding="utf-8", errors="replace") as f:
        got = transcript_usage(f, model_hint=_argv_value(argv, "--model"))
    if got is None:
        # A session with no API message — a refused call (quota, auth) leaves only
        # the CLI's own notice. Not a crash: the transcript is there.
        return {"unavailable": "no usage in transcript"
                + (f" (rc={returncode})" if failed else "")}
    got.update(duration_ms=duration_ms, session_id=sid)
    return {k: got.get(k) for k in USAGE_KEYS}


def record_usage(run_dir: Path, stage: str, rec: dict) -> None:
    """Merge one stage's usage record into stages.json `usage`. Additive: the
    other stages' records and every sibling key (completed, model_tier,
    orphaned_commits …) stay as they were. Never raises — a token count is never
    worth a stage."""
    try:
        p = Path(run_dir) / STAGE_PROGRESS
        try:
            cur = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except (OSError, ValueError):
            cur = {}
        usage = cur.get("usage") if isinstance(cur, dict) else None
        usage = dict(usage) if isinstance(usage, dict) else {}
        usage[stage] = rec
        _update_progress(run_dir, usage=usage)
    except Exception:
        pass


def record_stage_usage(run_dir: Path, stage: str, argv, *, env: dict | None = None,
                       returncode: int | None = None, duration_ms: int | None = None,
                       timed_out: bool = False) -> dict | None:
    """The runner's one call per stage invocation: stage_usage(), then
    record_usage(). Returns what was recorded — None when nothing was, which is
    always the case with the kill switch off. Never raises: a reader that does is
    recorded as unavailable instead, because the stage's outcome must not depend
    on whether its tokens could be counted."""
    try:
        if argv_session_id(argv) is None:
            return None
        try:
            rec = stage_usage(argv, env=env, returncode=returncode,
                              duration_ms=duration_ms, timed_out=timed_out)
        except Exception as e:
            rec = {"unavailable": f"usage reader error: {type(e).__name__}"}
        if rec is not None:
            record_usage(run_dir, stage, rec)
        return rec
    except Exception:
        return None


def completed_stages(run_dir: Path) -> list[str]:
    try:
        return list(json.loads((Path(run_dir) / STAGE_PROGRESS)
                               .read_text(encoding="utf-8")).get("completed", []))
    except (OSError, ValueError, json.JSONDecodeError):
        return []


def runs_for(slug: str, runs_dir: Path) -> list[Path]:
    """This task's run dirs, oldest first.

    Matches `<timestamp>-<slug>` EXACTLY, both ends pinned by fullmatch. A
    trailing-only match ('…-api-poc' for slug 'poc') is a DIFFERENT task's run,
    and resumable_run() handing one to a retry means inheriting a stranger's
    requirements.md and plan.md — the pipeline then implements the wrong thing
    without ever saying so.

    The single definition of that pattern: trace.runs_for() re-exports this one.
    Having the regex in two places is what let the two disagree in the first
    place. `runs_dir` is a required argument on purpose — a `= RUNS_DIR` default
    would freeze the path at import time and defeat the tests' monkeypatch of
    `stages.RUNS_DIR`, silently reading the real vault.

    Oldest first is the only order guaranteed here; resumable_run() wants newest
    first and reverses at its own call site."""
    pat = re.compile(r"\d{8}T\d{6}-" + re.escape(slug))
    try:
        return sorted((p for p in Path(runs_dir).iterdir()
                       if p.is_dir() and pat.fullmatch(p.name)), key=lambda p: p.name)
    except OSError:
        return []


def resumable_run(task_id: str, current_run_id: str) -> tuple[Path, list[str]] | None:
    """The prior run of this task whose finished stages a retry may inherit.

    Retries used to restart at `research` every time, so a run that died in
    stage 4 of 5 threw away three expensive stages and re-derived them from
    scratch — dancemirror-firebase-app-distribution (2026-08-05) re-ran
    research+plan+implement because verify exited without writing verify.md,
    with the implementation already pushed and its PR already green.

    Returns None when the retry SHOULD start over:
    - the prior run reached a verify verdict of FAIL. That is a real defect in
      the code, and fix-forward means re-implementing against the diagnosis
      (`previous_failure_context`) — reusing the implement that produced the red
      build would just re-verify the same failure.
    - nothing was recorded as completed."""
    # newest first; runs_for() yields oldest first. An unreadable runs dir comes
    # back as [] there, so the loop is skipped and control falls to the trailing
    # `return None` — same answer the old `except OSError: return None` gave.
    runs = [p for p in runs_for(task_id, RUNS_DIR) if p.name != current_run_id]
    for rd in reversed(runs):
        done = completed_stages(rd)
        if not done:
            continue
        vp = rd / STAGE_ARTIFACT["verify"]
        if vp.exists() and parse_verify_verdict(
                vp.read_text(encoding="utf-8", errors="replace")) == "FAIL":
            return None      # real defect → re-implement, don't inherit
        return rd, [s for s in done if s in RESUMABLE]
    return None


INHERIT_KEY = "inherited_from"
# run.log has no frontmatter to carry the key, so it gets a boundary line in the
# file's own `=== … ===` idiom. "above this line" is the whole point — see
# stamp_inherited.
RUN_LOG_BOUNDARY_RE = re.compile(
    r"^=== inherited from (\S+) \(above this line\) ===$", re.M)
# Artifacts the runner keeps APPENDING to after inheritance, so no whole-file
# claim about them can stay true — they get the boundary marker instead.
APPENDED_ARTIFACTS = frozenset({STAGE_ARTIFACT["implement"]})   # run.log


def stamp_inherited(path: Path, source_run_id: str, *, appended: bool = False) -> bool:
    """Mark an inherited artifact with the run it was copied from. Best-effort.

    A run dir stopped meaning "one execution" the moment inherit_artifacts()
    started copying a prior run's bytes into it, and nothing said so in the file.
    Diagnosing ai-stock-trading-23 (2026-08-17) meant reading a `verify.md` from
    run 1 next to a `requirements.md` from run 3 with no way to tell either was
    second-hand. stages.json cannot answer it — the runner calls record_stage()
    for inherited stages too — so the mark has to live in the artifact.

    Two markers, chosen by what happens to the file NEXT — not by its shape:
    - `appended=True` (run.log): append a trailing boundary line. The runner
      opens run.log in append mode AFTER inheritance (runner.py), so an inherited
      run.log is a mixed file — inherited prefix, this run's stage output below.
      A whole-file "this is inherited" header would be a lie about the tail;
      "above this line" stays true no matter what is appended later. This is why
      the flag exists rather than sniffing for frontmatter: run.log USUALLY has
      none, but a stage that wrote it with Write instead of appending leaves one
      behind, and that copy would then get the lying whole-file claim. A `---`
      block is deliberately not prepended either — strip_frontmatter() strips
      only the first, so the leftover would leak into the next stage's prompt.
    - everything else (requirements.md, implementation_plan.md), when it has
      frontmatter: insert `inherited_from: <run>` just above the closing `---`.
      Body bytes and block count are untouched, so strip_frontmatter() cuts at
      exactly the same place and the next stage's prompt reads what it always
      did. The existing `run_id:` is deliberately NOT rewritten: it names the run
      that *wrote* the file, this names the run it was *copied from*, and
      third-generation inheritance needs both. A re-stamp replaces the value
      rather than adding a second key — only the immediate hop is meaningful.
      With no frontmatter to insert into, it falls back to the boundary line.

    Returns True if the stamp landed. False — silently — when the artifact is not
    UTF-8 text or the write fails: the copy already succeeded, and losing a
    provenance mark is far cheaper than re-running the stage it belongs to. The
    view renders an unstamped artifact as "출처 불명", never as a failure.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")     # strict: non-text stays byte-exact
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    line = f"{INHERIT_KEY}: {source_run_id}"
    end = -1 if appended else (text.find("\n---", 3) if text.startswith("---") else -1)
    if end != -1:
        head, rest = text[:end], text[end:]
        head = (re.sub(rf"^{INHERIT_KEY}:.*$", line, head, count=1, flags=re.M)
                if re.search(rf"^{INHERIT_KEY}:", head, re.M) else f"{head}\n{line}")
        text = head + rest
    else:
        if not text.endswith("\n"):
            text += "\n"
        text += f"\n=== inherited from {source_run_id} (above this line) ===\n"
    try:
        p.write_text(text, encoding="utf-8")
    except OSError:
        return False
    return True


def read_provenance(path: Path) -> str | None:
    """The run this artifact was copied FROM, or None if it is this run's own.

    None also covers "copied but the stamp could not be written" (see
    stamp_inherited) — the caller must present that as unknown provenance, not as
    a fresh artifact and not as an error. For run.log the LAST boundary wins: a
    third-generation copy carries one marker per hop and only the final one names
    the run this file actually came from."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    end = text.find("\n---", 3) if text.startswith("---") else -1
    if end != -1:
        m = re.search(rf"^{INHERIT_KEY}:[ \t]*(\S+)[ \t]*$", text[:end], re.M)
        if m:
            return m.group(1)
    found = RUN_LOG_BOUNDARY_RE.findall(text)
    return found[-1] if found else None


def inherit_artifacts(prior_run: Path, new_run: Path, stages_: list[str]) -> list[str]:
    """Copy the inherited stages' artifacts into the new run dir so the later
    stages read them at the paths their prompts expect. Returns what actually
    landed — a stage whose artifact can't be copied is NOT inherited, so it
    re-runs rather than leaving a downstream prompt reading a missing file."""
    got = []
    for s in stages_:
        name = STAGE_ARTIFACT.get(s)
        src, dst = Path(prior_run) / name, Path(new_run) / name
        try:
            if src.exists() and src.stat().st_size > 0:
                dst.write_bytes(src.read_bytes())
                # Provenance is best-effort and its result is deliberately
                # ignored: the bytes are already there, and dropping the stage
                # from `got` over a failed stamp would re-buy an expensive stage
                # to gain a comment. Unstamped reads back as unknown, not fresh.
                stamp_inherited(dst, Path(prior_run).name,
                                appended=name in APPENDED_ARTIFACTS)
                got.append(s)
        except OSError:
            continue
    return got


def previous_failure_context(task_id: str, current_run_id: str, limit: int = 2500) -> str:
    """The verify diagnosis of this task's most recent PRIOR run that produced one,
    so a retry can fix-forward the exact failure instead of re-implementing blind
    (which just re-verifies the same red CI). Empty on the first attempt."""
    # Same exact-match rule as resumable_run(), and for a sharper reason: this
    # text is pasted into the retry's prompt as "the failure you must fix". A
    # suffix match would hand the retry of 'poc' the diagnosis of a run of
    # 'api-poc' — a plausible-looking, entirely unrelated bug report.
    runs = [p for p in runs_for(task_id, RUNS_DIR) if p.name != current_run_id]
    for rd in reversed(runs):
        vp = rd / STAGE_ARTIFACT["verify"]
        if vp.exists():
            txt = vp.read_text(encoding="utf-8", errors="replace").strip()
            if txt:
                return f"(from previous run `{rd.name}`)\n\n{txt[:limit]}"
    return ""


def previous_timeout_context(task_id: str, current_run_id: str) -> str:
    """A resume note for a retry whose most recent prior run TIMED OUT mid-stage.

    A stage that hits its subprocess timeout has usually committed real work:
    checkpoint_push saved it to origin/<branch> and prepare_external_branch
    physically continues from there, so the code is already on the retry's branch
    before the creative stages even run. This note only tells them NOT to
    re-derive from scratch — the actual continuity is the branch, not this text,
    so a stale or wrong note degrades to a redundant re-read, never a loss. Empty
    when no prior run recorded a timeout. Same exact-slug match as resumable_run /
    previous_failure_context: a suffix match would hand this task the resume note
    of an unrelated one."""
    runs = [p for p in runs_for(task_id, RUNS_DIR) if p.name != current_run_id]
    for rd in reversed(runs):
        try:
            prog = json.loads((rd / STAGE_PROGRESS).read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(prog, dict):
            continue
        stage = prog.get("timed_out_stage")
        if stage:
            tip = prog.get("checkpoint_sha")
            where = (f"origin/<branch> carries the partial work (tip {str(tip)[:8]})"
                     if isinstance(tip, str) and tip
                     else "the partial work is on this branch")
            return (f"(previous run `{rd.name}` TIMED OUT during `{stage}` — {where}. "
                    "CONTINUE from what is already committed; do not restart from scratch.)")
    return ""


def previous_capacity_context(task_id: str, current_run_id: str) -> str:
    """A resume note for a retry whose most recent prior run was DEFERRED mid-stage
    on a usage/rate limit (or lost auth) rather than timing out.

    Same shape as previous_timeout_context: a deferred stage has often committed
    real work, checkpoint_push saved it to origin/<branch>, and
    prepare_external_branch physically continues from there — so the code is
    already on the retry's branch before the creative stages even run. This note
    only tells them NOT to re-derive from scratch; the actual continuity is the
    branch, not this text, so a stale or wrong note degrades to a redundant re-read,
    never a loss. Empty when no prior run recorded a capacity deferral. Same
    exact-slug match as resumable_run / previous_timeout_context: a suffix match
    would hand this task the resume note of an unrelated one."""
    runs = [p for p in runs_for(task_id, RUNS_DIR) if p.name != current_run_id]
    for rd in reversed(runs):
        try:
            prog = json.loads((rd / STAGE_PROGRESS).read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(prog, dict):
            continue
        stage = prog.get("capacity_limited_stage")
        if stage:
            tip = prog.get("checkpoint_sha")
            where = (f"origin/<branch> carries the partial work (tip {str(tip)[:8]})"
                     if isinstance(tip, str) and tip
                     else "the partial work is on this branch")
            return (f"(previous run `{rd.name}` DEFERRED on a usage/rate limit during "
                    f"`{stage}` — {where}. CONTINUE from what is already committed; "
                    "do not restart from scratch.)")
    return ""


# The two git-tracked Worker runtime trees the live orchestrator writes between
# `chore: sync vault state` commits, and that restore_main's forced checkout must
# NOT roll back:
#   - Worker/assignments/<task>.json — the ledger (Ledger.save write_texts it,
#     uncommitted; .gitignore:107 keeps it tracked so a cloud takeover inherits it)
#   - Worker/agents/<agent>/inbox/<msg>.json — mailbox envelopes (ack unlinks them,
#     uncommitted; .gitignore:99-100 keeps them tracked because git IS the transport)
# When main carries a differing `sync vault state` snapshot of one of these and the
# live worktree has an uncommitted write/delete to it, the plain `checkout main`
# refuses and restore_main's `-f` fallback resets the whole worktree to main HEAD —
# reverting the ledger write and RESURRECTING an acked envelope (the runner then
# re-runs the whole pipeline on the stale payload). snapshot/restore_tracked_runtime
# capture the worktree state of exactly these prefixes and re-apply it across the
# `-f`. (Worker/agents/_state/ is gitignored, so git-status never surfaces it — it
# is out of scope by construction, not by an exclusion here.) A resurrection that
# slips past this snapshot (any other restore) is still not redelivered: Mailbox.ack
# tombstones the acked id in that gitignored cursor (mailbox.ACK_TOMBSTONE_TTL_S).
TRACKED_RUNTIME_PREFIXES = ("Worker/assignments", "Worker/agents")


def snapshot_tracked_runtime(
    repo_dir: Path, prefixes: tuple[str, ...] = TRACKED_RUNTIME_PREFIXES,
) -> dict:
    """Capture the worktree state of the DIRTY tracked-runtime files under `prefixes`.

    Returns ``{"content": {relpath: bytes}, "deleted": [relpath]}`` — modified/added
    files with their current bytes, and worktree-deleted files by path. Only DIRTY
    paths appear (git status lists nothing for a clean file), so a runtime file that
    `main` legitimately advanced via `chore: sync vault state` while the worktree
    left it untouched is never captured and therefore never reverted by restore.

    Parses `git status --porcelain -z` exactly like `_untracked_paths` (2-char XY,
    path at rec[3:], skip the R/C rename source record). Best-effort: any git/OS
    error yields the empty snapshot rather than raising, because the sole caller
    (`restore_main`) runs in a `finally` where a raise would swallow the run's real
    outcome. Pairs with `restore_tracked_runtime`.
    """
    empty: dict = {"content": {}, "deleted": []}
    try:
        r = _git(repo_dir, "status", "--porcelain", "-z", "--", *prefixes)
        if r.returncode != 0:
            return empty
        snap: dict = {"content": {}, "deleted": []}
        recs = [x for x in (r.stdout or "").split("\0") if x]
        i = 0
        while i < len(recs):
            rec, i = recs[i], i + 1
            xy, path = rec[:2], rec[3:]
            if xy[:1] in ("R", "C"):
                i += 1                  # skip the rename/copy source record
            p = Path(repo_dir) / path
            if p.exists() or p.is_symlink():
                snap["content"][path] = p.read_bytes()
            else:
                snap["deleted"].append(path)
        return snap
    except (OSError, subprocess.SubprocessError):
        return empty


def restore_tracked_runtime(repo_dir: Path, snap: dict) -> None:
    """Re-apply a `snapshot_tracked_runtime` snapshot onto the worktree.

    Rewrites each captured file's bytes (recreating parent dirs) and re-removes each
    captured deletion. Best-effort per file — a per-file OSError is swallowed so
    preservation can never turn the forced-checkout caller into a raise, matching the
    fail-open contract of `restore_main`'s `finally`. Idempotent.
    """
    repo_dir = Path(repo_dir)
    for relpath, data in (snap.get("content") or {}).items():
        dest = repo_dir / relpath
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        except OSError:
            continue
    for relpath in snap.get("deleted") or []:
        dest = repo_dir / relpath
        try:
            if dest.exists() or dest.is_symlink():
                dest.unlink()
        except OSError:
            continue


def restore_main(repo_dir: Path, main: str = "main") -> bool:
    """Return the working tree to `main`, forcing past leftover changes if a
    plain checkout is blocked. The live vault must NEVER be left on a task
    branch — stages run with cwd=vault (trusted) and may checkout there, so the
    caller restores main in a finally, matching the legacy worker.

    The `-f` fallback resets the WHOLE worktree to main's HEAD, which used to roll
    back the live orchestrator's uncommitted writes to the git-tracked runtime
    trees (`TRACKED_RUNTIME_PREFIXES`): a `Ledger.save` write to
    `Worker/assignments/<task>.json` reverted to the last `chore: sync vault state`
    snapshot, and an acked (unlinked) `Worker/agents/<agent>/inbox/<msg>.json` was
    resurrected — the runner then re-ran the whole pipeline on the stale payload.
    So the worktree state of those prefixes is snapshotted before the checkout and
    re-applied after a successful one. Preservation is best-effort (the helpers
    never raise) so it can never convert this finally-called function into a raise;
    scoping to the two runtime prefixes leaves `-f`'s job of forcing past every
    OTHER dirty file untouched."""
    snap = snapshot_tracked_runtime(repo_dir)
    r = subprocess.run(["git", "-C", str(repo_dir), "checkout", main],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=GIT_TIMEOUT_LOCAL_S)
    if r.returncode != 0:
        r = subprocess.run(["git", "-C", str(repo_dir), "checkout", "-f", main],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=GIT_TIMEOUT_LOCAL_S)
    if r.returncode == 0:
        restore_tracked_runtime(repo_dir, snap)
    return r.returncode == 0


# SIGTERM → grace → SIGKILL for a timed-out stage's process group. 10s is the
# window the claude CLI needs to flush its session transcript on SIGTERM —
# `record_stage_usage` reads that transcript back — and it is short enough that
# the timeout path still returns promptly to `checkpoint_push`.
STAGE_KILL_GRACE_S = 10.0


def _kill_process_group(proc: subprocess.Popen,
                        grace: float = STAGE_KILL_GRACE_S,
                        progress: Callable[[str], None] | None = None) -> None:
    """SIGTERM the stage's whole process group, wait `grace`, then SIGKILL it.

    The group and not just `proc.kill()`: a stage is one `claude -p`, and its Bash
    tool spawns `./gradlew assembleDebug` plus a 2~8G Gradle/Kotlin JVM. Killing
    only the direct child leaves those in the runner's cgroup, `memory.current`
    stays above `memory.high`, and the next git call sits in
    `__mem_cgroup_handle_over_high` — the 2026-09-25 freeze where three runner
    threads never came back while the dashboard read idle (the git half of that
    incident is fixed by `_run_git`'s ceilings above).

    Never raises, by the same contract as `cleanup_build_daemons`: this runs on
    the timeout path, where an exception would replace the `TimeoutExpired` that
    the runner's run.log / checkpoint / `timed_out=True` logic keys on.

    `os.killpg` only, never `os.kill(-1, …)`: a process-wide signal once wiped a
    whole interactive session from a pytest teardown, and the blast radius here
    must stay exactly one stage.
    """
    def _say(note: str) -> None:
        if progress:
            progress(f"stage kill: {note}")

    def _signal(sig: int) -> None:
        try:
            pgid = os.getpgid(proc.pid)
        except OSError:
            return                      # already reaped — nothing to signal
        try:
            own = os.getpgid(0)
        except OSError:                 # pragma: no cover — POSIX always answers
            own = -1
        if hasattr(os, "killpg") and pgid > 0 and pgid != own:
            try:
                os.killpg(pgid, sig)
            except OSError as exc:
                _say(f"killpg({pgid}, {sig}) failed ({type(exc).__name__})")
            return
        # `start_new_session` did not take (or this is not POSIX). Signalling
        # this pgid would signal the RUNNER itself, so degrade to the direct
        # child — a worse cleanup, never a suicide.
        _say(f"pgid {pgid} is the runner's own group — signalling the child only")
        try:
            proc.send_signal(sig)
        except OSError as exc:
            _say(f"send_signal({sig}) failed ({type(exc).__name__})")

    _signal(signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    except OSError as exc:              # pragma: no cover — wait() on a live pid
        _say(f"wait after SIGTERM failed ({type(exc).__name__})")
        return
    _say(f"group survived SIGTERM for {grace}s — SIGKILL")
    _signal(signal.SIGKILL)
    try:
        proc.wait(timeout=grace)
    except (OSError, subprocess.SubprocessError):
        _say("group still present after SIGKILL")


def run_stage_subprocess(argv: list[str], *, cwd: str | None = None,
                         env: dict | None = None, timeout: float | None = None,
                         grace: float = STAGE_KILL_GRACE_S,
                         progress: Callable[[str], None] | None = None,
                         ) -> subprocess.CompletedProcess:
    """Run one worker stage, reaping its whole process group if it times out.

    Drop-in for the `subprocess.run(argv, capture_output=True, text=True,
    encoding="utf-8", errors="replace", timeout=…)` this replaced: it returns a
    `CompletedProcess` on exit and re-raises `subprocess.TimeoutExpired` (carrying
    whatever the stage had printed) on timeout, so the runner's
    `except subprocess.TimeoutExpired` block is unchanged.

    Two separate things `subprocess.run(timeout=…)` does that this exists to fix:

    * it kills only the direct child, leaving grandchildren (`./gradlew`, the
      Gradle/Kotlin JVM) alive in the runner's cgroup, and
    * having killed it, it calls `communicate()` a SECOND time with **no**
      timeout — which blocks until those grandchildren drop the inherited
      stdout/stderr pipes. So the old code did not merely fail to reap the JVM;
      it waited on it, holding a runner slot for as long as the JVM lived.

    Known limit, and why this is only half the fix: a grandchild that puts itself
    in its own session — the Gradle *daemon* does exactly that — leaves this group
    and `killpg` cannot see it. `cleanup_build_daemons`' `./gradlew --stop` is the
    other half. The runner calls it after every stage, and on the timeout path
    before `checkpoint_push`; both halves together are what keeps the cgroup under
    `memory.high` when the next git call runs.
    """
    proc = subprocess.Popen(
        argv, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        # The reason this helper exists at all: the stage leads its own session,
        # so its entire tree shares one pgid that `_kill_process_group` can
        # signal in a single call (`jobs.py` spawns detached jobs the same way).
        start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(proc, grace, progress=progress)
        try:
            # The group is gone, so the pipes are closed and this returns at
            # once — with everything read before the timeout, because
            # `communicate` keeps its accumulator across calls.
            out, err = proc.communicate(timeout=grace)
        except (OSError, subprocess.SubprocessError):
            out, err = _git_stream(exc.stdout), _git_stream(exc.stderr)
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout,
                                        output=out, stderr=err) from None
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


# Per-run Gradle daemon registry base — the heart of AC (a). `./gradlew --stop`
# stops every daemon in whatever registry its build used; with Gradle's default
# `~/.gradle` registry that is EVERY daemon on the host — another concurrent run's
# or a human's interactive one (2026-10-13 measured regression). Giving each run
# its own `-Dorg.gradle.daemon.registry.base` (folded into the stage env below AND
# into `--stop`, so both resolve the IDENTICAL registry) scopes the stop to this
# run's daemons only. The base is HOST SCRATCH, not inside the worktree: a path in
# the worktree would be wiped by branch-prep's `git clean -fd` / the 5-min
# maintenance sweep ([[fresh-wt-worktree-reclaimed-by-maintenance]]). Split the same
# way beta_publish._SCRATCH_ROOT is (engine root in a checkout, the vault in an
# installed wheel) and placed under Worker/_state/ — gitignored (.gitignore:154), so
# it never enters a commit. `~/.gradle` is never written.
_GRADLE_SCRATCH_ROOT = _VAULT_DATA_ROOT if paths.is_installed() else _ENGINE_ROOT
GRADLE_REGISTRY_DIR = _GRADLE_SCRATCH_ROOT / "Worker" / "_state" / "gradle-registry"


def gradle_registry_base(repo_dir: Path | str) -> Path:
    """The per-run Gradle daemon registry base for a run whose checkout is
    `repo_dir`. Pure — derives `GRADLE_REGISTRY_DIR / <checkout-basename>` and does
    NO filesystem I/O: Gradle creates the tree on first daemon use, and `--stop`
    against a base that never existed is a harmless no-op. Keyed on the checkout
    basename — the run slug for a per-run worktree (`~/Worker/<repo>.wt/<slug>`),
    the repo/vault dir name otherwise — so the stage build and its matching
    `--stop` share one base while two live runs never collide."""
    return GRADLE_REGISTRY_DIR / Path(repo_dir).name


def cleanup_build_daemons(repo_dir: Path,
                          progress: Callable[[str], None] | None = None) -> dict:
    """Stop the Gradle build daemons this run's clone started, so they do not
    accumulate in the runner cgroup across runs and OOM the host (the runner and
    the run it was executing died together, 2026-09-14).

    Runs the clone's own ``./gradlew --stop``, which stops every daemon started
    by THAT wrapper's Gradle version. Because each run cleans up its own clone at
    the end, no single version's daemons pile up run-over-run; a stray daemon
    from a *different* wrapper version (e.g. a crashed prior run) is left to the
    worker-stage idle-timeout (see ``WORKER_GRADLE_OPTS``) as the backstop.
    ``--stop`` covers only Gradle by design: the worker stage forces
    ``kotlin.compiler.execution.strategy=in-process``, so no separate
    KotlinCompileDaemon is spawned that would need a separate stop.

    Fail-open by contract — this runs in ``_execute``'s ``finally``, where a
    raise would swallow the run's real outcome (or its exception). Every error is
    caught and reported via ``progress``; **this never raises**. It no-ops
    (``ran=False``) when the dir has no ``gradlew`` — a vault task, or any
    non-Gradle repo — so it is safe to call unconditionally on every run.
    """
    repo_dir = Path(repo_dir)
    if not (repo_dir / "gradlew").exists():
        return {"ran": False, "note": "no gradlew — nothing to stop"}
    # Point --stop at the SAME per-run registry the stage build used
    # (stage_env_with_gradle folded in the identical base), so it stops only THIS
    # run's daemons — never another run's or a human's interactive daemon. The
    # base rides GRADLE_OPTS on the subprocess env — the only way --stop learns it;
    # appended to any inherited GRADLE_OPTS with ours LAST (JVM last-wins), so a
    # host setting is preserved yet our base wins. `~/.gradle` is never touched.
    existing_opts = (os.environ.get("GRADLE_OPTS") or "").strip()
    base_opt = f"-Dorg.gradle.daemon.registry.base={gradle_registry_base(repo_dir)}"
    stop_env = {**os.environ,
                "GRADLE_OPTS": f"{existing_opts} {base_opt}" if existing_opts else base_opt}
    try:
        r = subprocess.run(["./gradlew", "--stop"], cwd=str(repo_dir),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60, env=stop_env)
        note = f"gradlew --stop rc={r.returncode}"
        if progress:
            progress(f"build daemons: {note}")
        return {"ran": True, "note": note}
    except (OSError, subprocess.SubprocessError) as exc:
        note = f"gradlew --stop skipped ({type(exc).__name__})"
        if progress:
            progress(f"build daemons: {note}")
        return {"ran": False, "note": note}


# Worker-stage Gradle limits, injected via GRADLE_OPTS into the STAGE subprocess
# env only (never ~/.gradle/gradle.properties), so a human's manual builds are
# untouched: a 60s daemon idle-timeout so a stage's Gradle daemon dies on its own
# soon after the run (backstop for anything `cleanup_build_daemons` misses, e.g. a
# stray daemon from a different wrapper version), plus the in-process Kotlin
# compiler strategy so no separate KotlinCompileDaemon is spawned at all.
WORKER_GRADLE_OPTS = (
    "-Dorg.gradle.daemon.idletimeout=60000 "
    "-Dkotlin.compiler.execution.strategy=in-process"
)


def _worker_gradle_opts(repo_dir: Path | str | None = None) -> str:
    """The worker Gradle options string folded into a stage's GRADLE_OPTS.

    `WORKER_GRADLE_OPTS` always leads (and stays a CONTIGUOUS substring, so callers
    that pin it by `assertIn` keep matching). When the run's checkout `repo_dir` is
    known, a per-run `-Dorg.gradle.daemon.registry.base` follows it (AC a — the
    stage build and `cleanup_build_daemons`' `--stop` then share one registry). The
    explicit, default-off `WORKER_GRADLE_NO_DAEMON` env flag appends
    `-Dorg.gradle.daemon=false` so a flaky daemon can't wedge a stage — no
    auto-detection, the default path is the per-run registry base. With
    `repo_dir=None` and the flag unset this is byte-identical to `WORKER_GRADLE_OPTS`
    (the cap-1 / pre-this-leaf path, AC6 bit-for-bit)."""
    parts = [WORKER_GRADLE_OPTS]
    if repo_dir is not None:
        parts.append(f"-Dorg.gradle.daemon.registry.base={gradle_registry_base(repo_dir)}")
    if os.environ.get("WORKER_GRADLE_NO_DAEMON"):
        parts.append("-Dorg.gradle.daemon=false")
    return " ".join(parts)


def stage_env_with_gradle(base: dict | None = None, *,
                          repo_dir: Path | str | None = None) -> dict:
    """Return a copy of `base` (or `os.environ` when `base` is None → the stage
    inherits the parent env) with the worker Gradle options folded into GRADLE_OPTS.

    Appends to any inherited GRADLE_OPTS rather than clobbering it, and does so
    with our options LAST: Gradle/JVM take the last value for a duplicated `-D`
    key, so a stage cannot accidentally out-set the worker limits. Always returns
    a real dict (never None), so the caller passes a concrete env to the stage
    subprocess.

    `repo_dir` (the run's checkout) folds in a per-run
    `-Dorg.gradle.daemon.registry.base` so the stage build and
    `cleanup_build_daemons`' `--stop` resolve the SAME registry (AC a); omitted →
    byte-identical to before (AC6, cap-1 bit-for-bit)."""
    env = dict(base) if base is not None else os.environ.copy()
    existing = (env.get("GRADLE_OPTS") or "").strip()
    worker_opts = _worker_gradle_opts(repo_dir)
    env["GRADLE_OPTS"] = f"{existing} {worker_opts}" if existing else worker_opts
    return env


# Background tasks off in every stage subprocess. A stage is one `claude -p`
# process: a `run_in_background` Bash call returns at once, the agent ends its
# turn "waiting for the completion notification", and the process exits with the
# job still running — nothing re-invokes the stage. Measured on the CLI in use
# (2.1.273): with this set, `run_in_background: true` runs in the foreground and
# returns the command's output; without it the same headless call ended on
# "Awaiting user input.". mulkko-39 (2026-09-17) lost its verify stage exactly
# so — `gradlew --rerun-tasks` backgrounded, no verify.md — and the retry
# re-bought research+plan+implement. The prompts forbid backgrounding; this
# makes it impossible rather than discouraged.
#
# Bash-tool command budget (2026-10-18, user decision "finish, even if slow").
# Claude Code caps one foreground Bash call at BASH_MAX_TIMEOUT_MS (CLI default
# 600 000 = 10 min) and uses BASH_DEFAULT_TIMEOUT_MS when the agent passes no
# `timeout`. With background tasks disabled above, that 10-minute cap was the
# hard ceiling on any single build: `./gradlew :app:lintDebug` /
# `assembleDebug` on the memory-starved HQ took longer, the tool killed the
# gradle CLIENT at 600 s while the daemon kept building under its lock, the next
# gradlew blocked on that lock ("Gradle wedged"), Verify wrote `skip`, Report
# opened no PR, and the task parked for a human — 33 runs in the first 17 days of
# October. One hour per command fits inside the 2 h standard stage above; the
# 20-minute default covers an agent that forgets the `timeout` parameter
# without letting a hung command eat the stage. Both are plain env strings
# (ms), read by the CLI at process start, so the stage subprocess env is the
# one place to set them.
BASH_DEFAULT_TIMEOUT_MS = 20 * 60 * 1000
BASH_MAX_TIMEOUT_MS = 60 * 60 * 1000
STAGE_ENV_OVERRIDES = {
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    "BASH_DEFAULT_TIMEOUT_MS": str(BASH_DEFAULT_TIMEOUT_MS),
    "BASH_MAX_TIMEOUT_MS": str(BASH_MAX_TIMEOUT_MS),
}


def stage_env(base: dict | None = None, *,
              repo_dir: Path | str | None = None) -> dict:
    """The env a worker stage subprocess runs with: `stage_env_with_gradle` plus
    `STAGE_ENV_OVERRIDES`. Same copy semantics — the caller's dict is never
    mutated, and None inherits `os.environ`. `repo_dir` threads through to the
    per-run Gradle daemon registry base (AC a)."""
    env = stage_env_with_gradle(base, repo_dir=repo_dir)
    env.update(STAGE_ENV_OVERRIDES)
    return env


def with_browser_owner(stage: str, env: dict, owner: str) -> dict:
    """Copy of `env` for one stage: BROWSER_TOOL_STAGES get `owner` under
    BROWSER_OWNER_ENV, every other stage gets the key removed so no stale
    owner is inherited from the runner's own env. Never mutates `env`."""
    out = dict(env)
    if stage in BROWSER_TOOL_STAGES:
        out[BROWSER_OWNER_ENV] = owner
    else:
        out.pop(BROWSER_OWNER_ENV, None)
    return out


def with_emulator_owner(stage: str, env: dict, owner: str) -> dict:
    """The emulator-node twin of :func:`with_browser_owner`: EMULATOR_TOOL_STAGES
    (implement/verify/quick — NOT delegate) get `owner` (= the run_id) under
    EMULATOR_OWNER_ENV so `macp-emulator run` takes the shared node lock as this
    run; every other stage has the key stripped so no stale owner leaks from the
    runner's own env. Never mutates `env`."""
    out = dict(env)
    if stage in EMULATOR_TOOL_STAGES:
        out[EMULATOR_OWNER_ENV] = owner
    else:
        out.pop(EMULATOR_OWNER_ENV, None)
    return out
