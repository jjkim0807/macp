"""Autonomous Implementation Worker (M4-a MVP).

Single-task, vault-only worker. Scans `Todo/items/**` for tasks with
`worker_mode: auto`, registers them in `Worker/queue.json`, and executes a
5-stage pipeline (Research → Plan → Implement → Verify → Report) by
invoking `claude -p` once per stage as a subprocess.

Scope of M4-a:
- vault-only (no external `repo` field handling)
- serial execution (no git worktree parallelism)
- `auto_merge` defaults to true; the Report stage opens the PR via the `gh`
  CLI and, once required CI checks pass, squash-merges it. Opt out per task
  with `auto_merge: false`.

Stdlib only. Does NOT modify `todo_system/cli.py` — frontmatter parsing
is duplicated here intentionally so worker fields can evolve without
touching the main CLI.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import hashlib

from .claude_auth import claude_env
from . import browser_watch
from . import kakao_format
from . import not_before as nb_gate
from . import paths
from .kakao_format import LEVEL_DIGEST, compose_alert
from .orchestrator import stages as _stages
from .orchestrator import worktree_sweep as _worktree_sweep
from .orchestrator.repos import DEFAULT_WORKER_ROOT
from todo_system import daemonctl

# Two roots, split the way orchestrator/human.py and stages.py split theirs.
# _ENGINE_ROOT comes from paths.engine_root() — the repo root in a development
# checkout, todo_system/_engine in an installed wheel — and is immune to
# MACP_VAULT_ROOT: it anchors the files that ship with the engine —
# .claude/commands, scripts/kakao_send.sh and scripts/health_report.py. ROOT
# resolves through the vault resolver so vault data (Todo/, Worker/, logs/) is
# read from a redirected vault. Its start= anchor is paths.vault_search_start():
# the engine root in a checkout — byte-identical to _ENGINE_ROOT when the env is
# unset (the live config) — and None in an installed wheel, where the resolver
# falls back to the cwd-upward search instead of anchoring on site-packages.
# Looking health_report up under ROOT made restart_stale_daemons() a silent
# no-op in a split install (Notes/macp-public-split-inventory.md §11.8 c-1).
_ENGINE_ROOT = paths.engine_root()
ROOT = paths.vault_root(start=paths.vault_search_start())
TODO_DIR = ROOT / "Todo"
ITEMS_DIR = TODO_DIR / "items"
WORKER_DIR = ROOT / "Worker"
COMMANDS_DIR = _ENGINE_ROOT / ".claude" / "commands"
LOGS_DIR = ROOT / "logs"

# ---------------------------------------------------------------------------
# Multi-host routing (M5)
# ---------------------------------------------------------------------------
# The primary machine (the MacBook) runs with no WORKER_HOST set and behaves
# exactly as before. A secondary machine (e.g. the Windows/WSL desktop, for
# projects that cannot run on macOS) sets WORKER_HOST=<name> and is rerouted:
#   - task source  : Worker/remote/<name>/inbox/*.md   (git-tracked)
#   - run queue    : Worker/remote/<name>/queue.json   (git-tracked)
#   - run artifacts: Worker/remote/<name>/runs/        (git-tracked; *.log ignored)
#   - idle planner : disabled (only the primary host may create tasks)
# Todo/items/ is gitignored (single-machine data), so tasks travel to remote
# hosts via the git-tracked inbox, and results travel back the same way
# (queue.json + runs/ + Memory/episodes/). The primary host's scan-queue
# sweeps remote queue files and surfaces finished runs as review todos.
DEFAULT_HOST = "default"


def _detect_worker_host() -> str:
    raw = os.environ.get("WORKER_HOST", "").strip().lower()
    if not raw or raw == DEFAULT_HOST:
        return DEFAULT_HOST
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", raw):
        raise SystemExit(
            f"invalid WORKER_HOST '{raw}' — use lowercase letters, digits, hyphens"
        )
    return raw


WORKER_HOST = _detect_worker_host()
IS_REMOTE_HOST = WORKER_HOST != DEFAULT_HOST
REMOTE_DIR = WORKER_DIR / "remote"
HOST_DIR = REMOTE_DIR / WORKER_HOST  # only meaningful when IS_REMOTE_HOST
REMOTE_INBOX_DIR = HOST_DIR / "inbox"
# Primary-host-local ledger of remote run_ids already surfaced as review todos.
REMOTE_ACK_PATH = WORKER_DIR / "remote_ack.json"

# Host-scoped runtime state: a remote host must never write the primary
# host's queue/planner files (they'd collide via git on Worker/ paths).
_STATE_DIR = HOST_DIR if IS_REMOTE_HOST else WORKER_DIR
RUNS_DIR = _STATE_DIR / "runs"
QUEUE_PATH = _STATE_DIR / "queue.json"
PLANNER_STATE_PATH = _STATE_DIR / "planner_state.json"
SWEEP_STATE_PATH = _STATE_DIR / "sweep_state.json"

# todo-sync tick heartbeat: written by cli.cmd_daemon around each tick's real
# work, read by _todo_sync_idle. Present-and-fresh = a sync is mid-write;
# absent = the daemon is between ticks and safe to kickstart. This is the fix
# _todo_sync_idle's 2026-08-18 note prescribed — the process tree carries no
# usable signal (the uv wrapper's python child is permanently resident), so the
# tick itself has to say when it is working.
TODO_SYNC_HEARTBEAT_PATH = _STATE_DIR / "todo_sync_heartbeat"
# A tick is seconds of sqlite/file writes. A heartbeat older than this is a
# corpse from a tick that died mid-write (daemon killed, exception), not work.
# Without this cutoff a single crash would leave the gate reading "busy"
# forever — the constant-False defect rebuilt from another road.
TODO_SYNC_HEARTBEAT_STALE_S = 10 * 60


def todo_sync_heartbeat_start() -> None:
    """Mark a todo-sync tick as in-flight.

    Failure is deliberately silent: a heartbeat that cannot be written must not
    stop the sync itself. The cost is one wrong 'idle' reading (absent file),
    which the restart path tolerates — kickstart between ticks is exactly the
    safe case, and mid-tick kills need the file to exist to be prevented at all.
    """
    try:
        TODO_SYNC_HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        TODO_SYNC_HEARTBEAT_PATH.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def todo_sync_heartbeat_clear() -> None:
    try:
        TODO_SYNC_HEARTBEAT_PATH.unlink(missing_ok=True)
    except OSError:
        pass


# Runner tick heartbeat: written by orchestrator.runner around each tick's real
# work and re-touched at the top of every pipeline stage, read by _runner_idle.
# Present-and-fresh = a stage is executing; absent = the runner is standing by
# and safe to kickstart. Same shape as the todo-sync heartbeat above, for the
# same reason: the ledger cannot answer this question. While the usage guard
# holds messages in the mailbox (run_forever's only_bypass path) a `dispatched`
# row sits untouched for DAYS with nothing running (2026-08-21 moksori-2).
RUNNER_HEARTBEAT_PATH = _STATE_DIR / "_state" / "runner_heartbeat"
# A todo-sync tick is seconds; a runner tick is a five-stage pipeline, so the
# cutoff cannot be "how long a tick takes". It is instead the hard bound between
# two consecutive touches: the runner re-touches this file per stage, and one
# stage cannot outlive its subprocess.run timeout. That timeout is no longer a
# flat 3600 — a heavy task's stage runs up to stages.MAX_STAGE_TIMEOUT (10800s
# since 2026-10-18; 5400 before), so the cutoff is 1.5x THAT bound (270min).
# 1.5x absorbs scheduling slop while
# still disarming a corpse — with no cutoff a single crash would freeze the gate
# at "busy" forever, rebuilding the constant-False defect from another road.
# INVARIANT (pinned by test, since stages.py is not imported here to avoid a
# cycle): stages.MAX_STAGE_TIMEOUT + stages.REAP_MARGIN < this value.
RUNNER_HEARTBEAT_STALE_S = 270 * 60


def runner_heartbeat_start() -> None:
    """Mark a runner tick (one task's pipeline) as in-flight.

    Called again at the top of each stage so the freshness window stays bounded
    by one stage timeout instead of by the whole pipeline. Failure is silent for
    the same reason as todo_sync_heartbeat_start: a heartbeat that cannot be
    written must not stop the work it only describes. The cost is one wrong
    'idle' reading, and the live `claude -p` co-signal still covers the stage.
    """
    try:
        RUNNER_HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
        RUNNER_HEARTBEAT_PATH.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def runner_heartbeat_clear() -> None:
    try:
        RUNNER_HEARTBEAT_PATH.unlink(missing_ok=True)
    except OSError:
        pass


# Default per-stage tool whitelists (used when the task does not override).
#
# The values live in orchestrator.stages.STAGE_TOOLS and are aliased here; this
# module no longer spells them out. The names stay because build_stage_command
# and Task.allowed_tools_* read them by these names.
#
# Two hand-maintained copies is what this replaces, and they had drifted: by
# 2026-08-24 the verify whitelist here was missing all ten Step 5b fallback
# runners and implement was missing pnpm/node/npx — every one of them added to
# stages.py alone, because nothing tied the lists together. That was not
# cosmetic. build_stage_command reads DEFAULT_VERIFY_TOOLS directly with no
# frontmatter override, so on this path the local-CI fallback could not run at
# all: the exact failure (`flutter analyze` answering "This command requires
# approval") that the stages.py copy had already been fixed for, twice.
#
# Importing stages keeps this module's stdlib-only contract — stages is
# stdlib-only too — and adds no cycle: orchestrator/__init__ is a docstring with
# no imports, and orchestrator/runner.py, the one module that pulls in both,
# already imports worker.
DEFAULT_RESEARCH_TOOLS = _stages.STAGE_TOOLS["research"]
DEFAULT_PLAN_TOOLS = _stages.STAGE_TOOLS["plan"]
DEFAULT_IMPLEMENT_TOOLS = _stages.STAGE_TOOLS["implement"]
DEFAULT_VERIFY_TOOLS = _stages.STAGE_TOOLS["verify"]
DEFAULT_REPORT_TOOLS = _stages.STAGE_TOOLS["report"]
# Delegate is a single-stage lightweight mode: claude -p reads task + context,
# edits the task file in place (Progress Log + body), commits on a worker branch.
DEFAULT_DELEGATE_TOOLS = _stages.STAGE_TOOLS["delegate"]

# Turn budget per stage. A single flat budget starves the implement/delegate
# stages (which do the actual multi-file editing) while wasting headroom on the
# read-only stages. research/plan/verify/report rarely need many turns; the
# implement and delegate stages do the heavy lifting and routinely exceeded the
# old flat 40 (e.g. todo-area-to-tags-refactor died at "Reached max turns (40)").
# A task may still override all stages via an explicit `max_turns` frontmatter.
DEFAULT_STAGE_MAX_TURNS = 40
STAGE_MAX_TURNS = {
    "research": 40,
    "plan": 40,
    "implement": 200,
    "verify": 50,
    "report": 40,
    "delegate": 200,
    "discover": 120,
}

# Pipeline shape per worker_mode.
# - auto: full 5-stage R-P-I-V-Rep (for code-change tasks)
# - delegate: single-stage 'delegate' (for research/update-the-task-file tasks)
STAGES_AUTO = ("research", "plan", "implement", "verify", "report")
STAGES_DELEGATE = ("delegate",)
STAGES = STAGES_AUTO  # kept for back-compat references
STAGES_DISCOVER = _stages.STAGES_DISCOVER
STAGES_BY_MODE = {
    "auto": STAGES_AUTO,
    "delegate": STAGES_DELEGATE,
    "discover": STAGES_DISCOVER,
}

# Tool patterns that must NEVER be in any stage's allowed_tools whitelist.
# Substring match against the user-supplied allowed_tools string.
FORBIDDEN_TOOL_SUBSTRINGS = (
    "Bash(rm",
    "Bash(sudo",
    # No trailing space. These two carried one until 2026-08-05, which meant they
    # matched `Bash(curl -X …)` — a spelling nobody writes — and missed
    # `Bash(curl:*)`, the colon form the vault's own tasks use. Eleven tasks
    # granted exactly that and validated clean, so the guard was blocking the
    # wrong dialect. The sibling entries never had the space and were always
    # correct, which is why this stayed invisible.
    "Bash(curl",        # arbitrary curl can exfiltrate secrets
    "Bash(scp",
    "Bash(ssh",         # ssh to other hosts
    "Bash(git push --force",
    "Bash(git push -f",
    "Bash(* --no-verify",
)

# auto_merge defaults to TRUE (opt out per task with `auto_merge: false`).
# When true, the Report stage merges (`gh pr merge --squash --delete-branch`)
# once verify.md says `overall_status: pass` — Verify ran the project's own
# checks locally. GitHub Actions is not used (2026-09-16, every project), so
# there are no PR checks to wait for unless the task opts in with `ci: actions`.
# When false, Report leaves a ready PR open for human review.
AUTO_MERGE_TRUTHY = ("true", "1", "yes", "on")

# Planner exhausted backoff: if hash matches and < this threshold, skip re-invoke.
PLANNER_EXHAUSTED_THRESHOLD_HOURS = 24

# Auto queue depth limit: if pending auto/delegate tasks >= this, skip planner invoke.
AUTO_QUEUE_DEPTH_LIMIT = 5
# Max automatic CI-fix attempts per task before surfacing a human gate.
MAX_CI_FIX_ATTEMPTS = 2

# Max new tasks the planner can create in a single invocation.
PLANNER_MAX_NEW_TASKS = 3

# Max turns for planner agent.
PLANNER_MAX_TURNS = 15

# Stage → artifact filename that must exist after the stage completes.
STAGE_ARTIFACT = {
    "research": "requirements.md",
    "plan": "implementation_plan.md",
    "implement": "run.log",
    "verify": "verify.md",
    "report": "report.md",
    "delegate": "delegate.md",
}

# Heuristic dispatcher: keywords that indicate human-only steps (skip task).
# Korean-first since the vault is primarily Korean.
HUMAN_INTERVENTION_KEYWORDS = (
    "전화", "통화", "팩스",
    "방문", "면담", "출석", "내방",
    "본인", "사용자가", "내가 ",
    "결제", "구매", "납부",
    "결정", "선택", "고민",
    "면접", "지원서 작성",
    "서명", "날인", "인감",
    "제출", "발급",
    "신청서", "신청 ",
)
# Keywords that indicate research/desk work that an AI agent can do alone.
AUTONOMOUS_KEYWORDS = (
    "조사", "탐색", "비교", "분석", "정리", "리스트업",
    "추천", "요약", "검색", "후보", "스캔", "벤치마크",
    "문서화", "초안", "리서치",
)


# ---------------------------------------------------------------------------
# Planner state & hash utilities
# ---------------------------------------------------------------------------


def compute_todo_state_hash(queue: dict[str, Any] | None = None) -> str:
    """Compute a hash reflecting current state of Todo/items/** and queue.json.

    Used for planner exhausted backoff: if todo state hasn't changed since the
    last planner invocation, skip re-invoke to save tokens.

    The hash includes:
    - For each .md file in Todo/items/: mtime + first 500 chars
    - queue.json runs list (serialized)

    Args:
        queue: pre-fetched queue dict to reuse (avoids a redundant queue read
            when the caller already has one). Fetches fresh if not given.
    """
    h = hashlib.sha256()

    # Hash todo files (flat items/ layout, tags-based classification)
    if ITEMS_DIR.exists():
        for path in sorted(ITEMS_DIR.glob("*.md")):
            try:
                stat = path.stat()
                content = path.read_text(encoding="utf-8")[:500]
                h.update(f"{path.name}:{stat.st_mtime_ns}:{content}".encode("utf-8"))
            except OSError:
                continue

    # Hash queue runs
    if queue is None:
        queue = load_queue()
    h.update(json.dumps(queue.get("runs", []), sort_keys=True).encode("utf-8"))

    return h.hexdigest()[:16]


def load_planner_state() -> dict[str, Any]:
    """Load planner state (current verdict/hash/last_run).

    Reads Worker/planner_state.json. Returns {} if missing/invalid.
    """
    if not PLANNER_STATE_PATH.exists():
        return {}
    try:
        return json.loads(PLANNER_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_planner_state(verdict: str, queue: dict[str, Any] | None = None) -> None:
    """Save planner state with current hash, timestamp, and verdict.

    Writes Worker/planner_state.json.

    Args:
        queue: pre-fetched queue dict to reuse (see compute_todo_state_hash).
    """
    todo_state_hash = compute_todo_state_hash(queue=queue)
    last_run = now_iso()

    state = {
        "todo_state_hash": todo_state_hash,
        "last_run": last_run,
        "verdict": verdict,
    }
    PLANNER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLANNER_STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def is_planner_exhausted(queue: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Check if planner is in exhausted state and todo hasn't changed.

    Args:
        queue: pre-fetched queue dict to reuse (see compute_todo_state_hash).

    Returns (is_exhausted, reason).
    """
    state = load_planner_state()
    if not state:
        return False, "no prior state"

    if state.get("verdict") != "exhausted":
        return False, f"verdict={state.get('verdict')}"

    # Check hash
    current_hash = compute_todo_state_hash(queue=queue)
    if state.get("todo_state_hash") != current_hash:
        return False, f"hash changed: {state.get('todo_state_hash')} → {current_hash}"

    # Check time
    last_run_raw = state.get("last_run")
    if not last_run_raw:
        return False, "no last_run timestamp"

    last_run = _parse_iso_timestamp(last_run_raw)
    if last_run is None:
        return False, "invalid last_run timestamp"
    age_hours = (dt.datetime.now() - last_run).total_seconds() / 3600
    if age_hours >= PLANNER_EXHAUSTED_THRESHOLD_HOURS:
        return False, f"exhausted but {age_hours:.1f}h elapsed (threshold={PLANNER_EXHAUSTED_THRESHOLD_HOURS}h)"

    return True, f"exhausted {age_hours:.1f}h ago, hash unchanged"


def count_auto_pending(queue: dict[str, Any] | None = None) -> int:
    """Count pending queue entries with mode=auto or mode=delegate.

    Args:
        queue: pre-fetched queue dict to reuse (see compute_todo_state_hash).
    """
    if queue is None:
        queue = load_queue()
    return sum(
        1
        for r in queue.get("runs", [])
        if r.get("status") == "pending" and r.get("mode") in ("auto", "delegate")
    )


def log_planner(msg: str) -> None:
    """Append a planner log line to logs/worker.out.log."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / "worker.out.log"
    timestamp = now_iso()
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] [planner] {msg}\n")


# ---------------------------------------------------------------------------
# frontmatter parsing (stdlib only; mirrors style of todo_system/cli.py)
# ---------------------------------------------------------------------------


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Return (frontmatter dict, body). Lists are returned as list[str].

    Intentionally duplicated from cli.py to avoid a tight coupling; this
    parser additionally recognises the worker-specific fields.
    """
    if not content.startswith("---\n"):
        return {}, content

    parts = content.split("\n---\n", 1)
    if len(parts) != 2:
        return {}, content

    fm_raw, body = parts
    fm_lines = fm_raw.splitlines()[1:]
    result: dict[str, Any] = {}
    idx = 0
    while idx < len(fm_lines):
        line = fm_lines[idx]
        if not line.strip():
            idx += 1
            continue
        if ":" not in line:
            idx += 1
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value == "":
            items: list[str] = []
            idx += 1
            while idx < len(fm_lines):
                sub = fm_lines[idx]
                if sub.strip().startswith("- "):
                    items.append(sub.strip()[2:].strip().strip('"').strip("'"))
                    idx += 1
                    continue
                break
            result[key] = items
            continue
        value = value.strip().strip('"').strip("'")
        result[key] = value
        idx += 1

    return result, body


# ---------------------------------------------------------------------------
# Task discovery
# ---------------------------------------------------------------------------


@dataclass
class WorkerTask:
    slug: str
    path: Path  # absolute
    rel_path: str  # relative to ROOT
    frontmatter: dict[str, Any]

    @property
    def worker_mode(self) -> str:
        return str(self.frontmatter.get("worker_mode", "off")).lower()

    @property
    def status(self) -> str:
        return str(self.frontmatter.get("status", "open")).lower()

    @property
    def max_turns(self) -> int | None:
        """Explicit per-task turn budget, or None to use the per-stage default."""
        raw = self.frontmatter.get("max_turns")
        if raw is None:
            return None
        try:
            return int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    def _resolve_allowed_tools(self, default: str) -> str:
        raw = self.frontmatter.get("allowed_tools")
        if isinstance(raw, str) and raw.strip():
            return raw.replace(",", " ").strip()
        if raw is not None:
            # A YAML list is the common mistake and it is otherwise invisible:
            # the task silently runs without whatever it listed (gh, gcloud,
            # gradlew...) and dies later on a permissions error. Say so here.
            print(
                f"[warn] {self.slug}: allowed_tools is {type(raw).__name__}, not str — "
                f"ignoring it and falling back to '{default}'. "
                "Quote it as a comma-separated string; see `todo validate`.",
                flush=True,
            )
        return default

    @property
    def allowed_tools_implement(self) -> str:
        return self._resolve_allowed_tools(DEFAULT_IMPLEMENT_TOOLS)

    @property
    def allowed_tools_delegate(self) -> str:
        return self._resolve_allowed_tools(DEFAULT_DELEGATE_TOOLS)

    @property
    def delegate_opt_out(self) -> bool:
        """frontmatter `delegate: false` blocks dispatcher auto-pickup."""
        raw = self.frontmatter.get("delegate")
        if raw is None:
            return False
        return str(raw).strip().lower() in ("false", "0", "no", "off")

    @property
    def is_parent(self) -> bool:
        raw = self.frontmatter.get("is_parent")
        return str(raw).strip().lower() == "true" if raw is not None else False

    def matches_human_intervention(self) -> bool:
        """True if task text suggests steps requiring the human user."""
        text = self._search_text()
        return any(kw in text for kw in HUMAN_INTERVENTION_KEYWORDS)

    def matches_autonomous(self) -> bool:
        """True if task text suggests AI-doable desk research/update work."""
        text = self._search_text()
        return any(kw in text for kw in AUTONOMOUS_KEYWORDS)

    def _search_text(self) -> str:
        """Concatenated frontmatter fields used by the dispatcher heuristic."""
        parts: list[str] = []
        for key in ("title", "outcome", "next_action", "if_then"):
            v = self.frontmatter.get(key)
            if isinstance(v, str):
                parts.append(v)
        tags = self.frontmatter.get("tags")
        if isinstance(tags, list):
            parts.extend(str(t) for t in tags)
        return "  ".join(parts)

    @property
    def worker_branch(self) -> str:
        raw = self.frontmatter.get("worker_branch")
        if isinstance(raw, str) and raw.strip() and "<slug>" not in raw:
            return raw.strip()
        return f"worker/{self.slug}"

    @property
    def repo(self) -> str | None:
        """Optional external git repo URL. M4-a returns None for all tasks.

        If set, worker should clone repo to working_dir and run pipeline there.
        Stub: parsed but execution still vault-only until M4-c lands.
        """
        raw = self.frontmatter.get("repo")
        if isinstance(raw, str) and raw.strip() and raw.strip() != "-":
            return raw.strip()
        return None

    @property
    def working_dir(self) -> str | None:
        """Path where external repo is checked out (M4-c).

        Stub: parsed but unused in M4-a; execution still vault-only.
        """
        raw = self.frontmatter.get("working_dir")
        if isinstance(raw, str) and raw.strip() and raw.strip() != "-":
            return raw.strip()
        return None

    @property
    def is_external_repo(self) -> bool:
        return self.repo is not None

    @property
    def auto_merge(self) -> bool:
        """When True, Report stage waits for CI green then `gh pr merge`.

        Default is TRUE and should stay that way for nearly every task: the worker
        auto-merges PRs once required CI checks pass. A per-task frontmatter
        `auto_merge: false` is DEPRECATED (the orchestrator's injected value is
        authoritative and defaults true). To genuinely require human review before
        merge — reserve this for the rare, unavoidable case (irreversible / prod)
        — set `worker_mode: review`, which the orchestrator honors and surfaces
        the ready PR for approval.
        """
        raw = str(self.frontmatter.get("auto_merge", "true")).strip().lower()
        return raw in AUTO_MERGE_TRUTHY

    @property
    def title(self) -> str:
        # Best-effort: first '# ' heading in body, else slug.
        try:
            content = self.path.read_text(encoding="utf-8")
        except OSError:
            return self.slug
        _, body = parse_frontmatter(content)
        for line in body.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
        return self.slug


def iter_worker_tasks_vault() -> Iterable[WorkerTask]:
    """Walk this host's task source and yield WorkerTask objects (flat layout).

    Primary host: Todo/items/*.md. Remote host: its git-tracked inbox
    Worker/remote/<host>/inbox/*.md (Todo/items/ is gitignored and never
    reaches other machines).
    """
    source_dir = REMOTE_INBOX_DIR if IS_REMOTE_HOST else ITEMS_DIR
    if not source_dir.exists():
        return
    for path in sorted(source_dir.glob("*.md")):
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        fm, _ = parse_frontmatter(content)
        yield WorkerTask(
            slug=path.stem,
            path=path,
            rel_path=str(path.relative_to(ROOT)),
            frontmatter=fm,
        )


def iter_worker_tasks() -> Iterable[WorkerTask]:
    """Yield WorkerTask objects from the vault markdown files."""
    yield from iter_worker_tasks_vault()


# ---------------------------------------------------------------------------
# Queue I/O
# ---------------------------------------------------------------------------


QUEUE_LOCK_PATH = _STATE_DIR / "queue.lock"


@contextlib.contextmanager
def queue_lock() -> Iterator[None]:
    """Exclusive flock on Worker/queue.lock — guards queue.json mutations.

    Multiple `run-next` processes (or run-parallel children) call this to
    safely claim distinct pending entries without races.
    """
    QUEUE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(QUEUE_LOCK_PATH, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def load_queue() -> dict[str, Any]:
    """Load the run queue.

    Reads Worker/queue.json verbatim.

    Return shape is always {"runs": [...]} so callers are unchanged.
    """
    if not QUEUE_PATH.exists():
        return {"runs": []}
    try:
        return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"runs": []}


def save_queue(queue: dict[str, Any]) -> None:
    """Persist the run queue.

    Writes Worker/queue.json verbatim.
    """
    QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    QUEUE_PATH.write_text(
        json.dumps(queue, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# Module constant (not recomputed per call) so tests can point every send at a
# recorder — or nowhere — with one monkeypatch (todo_system/conftest.py does the
# latter for the whole suite). Engine file, so _ENGINE_ROOT, not the vault.
KAKAO_SEND_SCRIPT = _ENGINE_ROOT / "scripts" / "kakao_send.sh"


def kakao_notify(message: str, link: str | None = None, *,
                 level: str | None = None, event_key: str | None = None) -> None:
    """Best-effort KakaoTalk memo. Never raises; failure is logged by the script.

    Calls scripts/kakao_send.sh which itself is non-fatal on token issues. We
    intentionally do not block the worker on notification failures. `link`, when
    given, is passed as the script's 2nd arg (the "열기" button target), so detail
    can live at a URL instead of being crammed into — and cut from — the text.

    `level` / `event_key` are the OPT-IN noise tiers (scripts/KAKAO_ALERTS.md):
    both None — every pre-existing call — keeps today's immediate send without
    touching any routing state. digest/silent are handled in-process (no spawn);
    an immediate send with an event_key may be suppressed into the window's next
    delivery ("외 N건"). Routing fails open to a plain send.
    """
    if level or event_key:
        try:
            verdict, final = kakao_format.route(message, level=level, event_key=event_key)
        except Exception:
            verdict, final = "send", message
        if verdict != "send":
            return
        message = final if final is not None else message
    if not KAKAO_SEND_SCRIPT.exists():
        return
    try:
        subprocess.run(
            ["bash", str(KAKAO_SEND_SCRIPT), message] + ([link] if link else []),
            timeout=15,
            check=False,
            capture_output=True,
        )
    except (OSError, subprocess.SubprocessError):
        pass


# -- Self-limiting composers for the worker's own kakao alerts ----------------
# Every worker alert leads with an emoji headline (what happened + what to do or
# "조치 불필요"); free-form detail (a failure message, a stall file list, an
# English planner reason) rides as a word-clipped tail so nothing is blind-cut.
# All go through compose_alert → length is <= KAKAO_TEXT_LIMIT by construction.

def _compose_worker_start(slug: str, mode: str, n_stages: int) -> str:
    return compose_alert(f"🤖 Worker 시작: {slug} (mode={mode}, {n_stages} stage) — 조치 불필요, 진행 알림")


def _compose_worker_done(slug: str, mode: str, n_stages: int) -> str:
    return compose_alert(f"✅ Worker 완료: {slug} (mode={mode}, {n_stages} stage) — 리뷰 태스크를 확인해주세요")


def _compose_worker_fail(slug: str, stage: str, message: str) -> str:
    return compose_alert(f"❌ Worker 실패: {slug} ({stage} 단계) — 런 로그 확인이 필요합니다",
                         tail=message)


def _compose_migration_drift(warning: str) -> str:
    return compose_alert("⚠️ 마이그레이션 미적용 — 확인이 필요합니다", tail=warning)


def _compose_dispatch_silence(pending_n: int, idle_h: float, threshold_h: float) -> str:
    return compose_alert(
        f"🤖⚠️ macp 정체: 대기 {pending_n}건인데 {idle_h:.1f}시간째 디스패치 없음 "
        f"(임계 {threshold_h:.0f}h, usage guard 아님) — 오케스트레이터/러너 확인이 필요합니다")


def _compose_starvation(since: str, why: str, parked_n: int) -> str:
    extra = f" (사람 대기 {parked_n}건)" if parked_n else ""
    return compose_alert(
        f"🤖💤 macp 유휴: 큐 비어있고 진행 0건 — 새 태스크를 넣어주세요{extra}",
        tail=f"플래너 exhausted({since}), 디스커버리 불가: {why}")


def _compose_worker_stall(count: int, bad_files: list[str]) -> str:
    shown = ", ".join(bad_files[:5])
    if len(bad_files) > 5:
        shown += f" 외 {len(bad_files) - 5}건"
    return compose_alert(
        f"⚠️ Worker 정지: 커밋 안 된 파일 때문에 {count}회 연속 중단 — 커밋/정리하면 자동 재개됩니다",
        tail=shown)


def _compose_daemon_restart_escalated(label: str, attempts: int) -> str:
    return compose_alert(
        f"🤖⚠️ macp 데몬 재기동 실패: {label} 이(가) {attempts}회 "
        f"재기동 후에도 구코드로 판정됩니다. 수동 확인 필요.")


def _compose_daemon_restart_deferred(label: str, stalled_s: float, deferrals: int) -> str:
    return compose_alert(
        f"🤖⚠️ macp 데몬 재기동 보류: {label} 이(가) {int(stalled_s // 3600)}시간 동안 "
        f"{deferrals}회 연속 '작업 중'으로 판정돼 재기동이 계속 연기됐습니다. "
        "구코드로 계속 도는 중이니 수동 확인 필요.")


def claim_next_pending() -> dict[str, Any] | None:
    """Atomically pop the first pending entry, set status=in_progress.

    Returns the queue entry dict or None if no pending available.

    queue_lock() flock + load/modify/save provides a disjoint-claim guarantee
    for concurrent processes on one machine.
    """
    with queue_lock():
        queue = load_queue()
        for entry in queue.get("runs", []):
            if entry.get("status") == "pending":
                entry["status"] = "in_progress"
                entry["started_at"] = now_iso()
                save_queue(queue)
                return entry
        return None


def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def make_run_id(slug: str) -> str:
    return dt.datetime.now().strftime("%Y%m%dT%H%M%S") + f"-{slug}"


# ---------------------------------------------------------------------------
# scan-queue
# ---------------------------------------------------------------------------


STUCK_IN_PROGRESS_THRESHOLD = dt.timedelta(hours=3)
# Auto-retry a failed entry once before giving up — handles transient failures
# (network/lint timing) without nagging the user. `auto_retry_count` is bumped
# on each automatic retry; >0 means doctor already retried it.
AUTO_RETRY_LIMIT = 1


# ---------------------------------------------------------------------------
# Review autoclose (stale review todos sweep)
# ---------------------------------------------------------------------------



def _extract_parent_slug_from_review(review_slug: str) -> str | None:
    """Extract parent task slug from a review todo slug.

    Patterns:
    - review-<slug>-<timestamp> (e.g., review-my-task-20260612T2010)
    - review-<slug> (fallback, no timestamp)

    Returns the parent slug or None if extraction fails.
    """
    # Pattern 1: review-<slug>-YYYYMMDDTHHMMSS (timestamp suffix)
    m = re.match(r"^review-(.+?)-\d{8}T\d{4,6}$", review_slug)
    if m:
        return m.group(1)

    # Pattern 2: review-<slug> (no timestamp)
    m = re.match(r"^review-(.+)$", review_slug)
    if m:
        return m.group(1)

    return None


def autoclose_stale_reviews_local() -> int:
    """Close open review-<slug> todos whose parent task is already done.

    Reads the vault markdown files, which are the only source of task state.
    Returns the count of closed review todos.
    """
    tasks_by_slug = {t.slug: t for t in iter_worker_tasks_vault()}

    closable: list[WorkerTask] = []
    for slug, task in tasks_by_slug.items():
        if not slug.startswith("review-") or task.status in ("done", "cancelled"):
            continue
        parent_slug = _extract_parent_slug_from_review(slug)
        if parent_slug is None:
            print(f"[review-autoclose] warning: cannot parse parent from {slug}")
            continue
        parent = tasks_by_slug.get(parent_slug)
        if parent is None or parent.status != "done":
            continue
        closable.append(task)

    if not closable:
        return 0

    # Deferred import: cli.py imports worker.py at module scope, so importing
    # cli.py at worker.py module scope would be circular.
    from . import cli as _cli

    con = _cli.db_connect(_cli.DB_PATH)
    _cli.init_db(con)
    today = dt.date.today().isoformat()
    closed_count = 0
    for task in closable:
        _cli.update_markdown_checkboxes(task.path, mode="complete")
        content = task.path.read_text(encoding="utf-8")
        content = _cli.update_frontmatter_value(content, "updated", today)
        content = _cli.update_frontmatter_value(content, "status", "done")
        task.path.write_text(content, encoding="utf-8")
        _cli.refresh_single_task(con, task.path)
        closed_count += 1

    _cli.sync_now_note(con)
    _cli.sync_focus_note(con)
    _cli.sync_dashboard_note(con)
    con.commit()
    return closed_count


# ---------------------------------------------------------------------------
# Stale worker worktree reporting (read-only)
# ---------------------------------------------------------------------------
# Live runs execute in a linked git worktree at `<DEFAULT_WORKER_ROOT>/wt-<slug>`
# so that a concurrent daemon `restore_main` cannot yank HEAD out from under a
# running stage. Nothing removes them once the PR merges, so they accumulate —
# 22 linked worktrees on this host when this was written.
#
# doctor only *reports* candidates. Removal stays a human decision: a worktree
# can hold the only copy of work that never made it into a commit, and a
# false positive that merely prints a line costs nothing.

WORKTREE_PREFIX = "wt-"
# Detail lines are capped so a 22-candidate host costs 6 log lines per 5-minute
# maintenance tick instead of 22 (same reasoning as the reconcile skip tally).
MAX_STALE_WORKTREE_ACTIONS = 5
# Bounds how many records are looked at: each one costs up to 4 git subprocesses
# (`status --porcelain`, `merge-base --is-ancestor`, `merge-tree --write-tree`,
# `rev-parse ^{tree}`).
MAX_SCANNED_WORKTREES = 50
# A count cap is not a runtime bound. 50 records x 4 subprocesses x the 10s
# per-call timeout is ~33 minutes, which swallows the 5-minute maintenance tick
# that runs doctor whole — and the `except Exception` below fails open on
# *failure*, never on *slowness*, so nothing else caps the wall clock.
#
# Not a hard ceiling: the deadline is read on loop *entry*, so the record already
# in flight can still spend 4x_GIT_TIMEOUT_S past it, on top of the two fixed
# calls made before the loop. Real worst case is ~70-90s, not 30s.
STALE_WORKTREE_BUDGET_S = 30
_GIT_TIMEOUT_S = 10
# `uv sync` resolves + links an environment; far slower than a git read, so it
# gets its own ceiling rather than the 10s git timeout.
_UV_SYNC_TIMEOUT_S = 300
# Injection seam for the budget tests. Patching `time.monotonic` itself would not
# do: `subprocess.run(timeout=…)` reads the same function, so a fake clock turns
# real git calls into bogus `TimeoutExpired` that the fail-open path swallows,
# and the test goes vacuously green on an empty result.
_monotonic = time.monotonic
_TREE_OID_RE = re.compile(r"^[0-9a-f]{40}$")


def _parse_worktree_porcelain(text: str) -> list[dict[str, str]]:
    """Split `git worktree list --porcelain` output into one dict per worktree.

    Records are separated by a blank line. Every line is either `key value`
    (`worktree`, `HEAD`, `branch`) or a bare flag (`bare`, `detached`), and
    `locked` / `prunable` come either bare or with a trailing reason — which is
    kept as the value, so presence of the key is the test, never its truthiness.
    """
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        records.append(current)
    return records


def _git_read(repo_root: Path, *args: str,
              timeout: int = _GIT_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Run one read-only git command against `repo_root`.

    `git -C <path>` rather than `cwd=` or `cd &&`: worker stages run behind a
    Bash whitelist that rejects `cd &&` and env-prefixed commands, so this is
    the form that stays runnable from inside a stage as well as from the daemon.
    """
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def pull_engine_checkout(
    *,
    engine: Path | None = None,
    vault: Path | None = None,
    run: Callable = subprocess.run,
) -> list[str]:
    """Best-effort ff-only pull of a *separate* engine checkout, inside the tick.

    When the engine runs from an installed layout — ``engine_root()`` is a git
    checkout (``~/Projects/macp``) distinct from the vault — a self-improvement
    PR merged into macp never reaches the resident HQ daemons unless that
    checkout is pulled. This ports the temporary ``ops/macp_engine_pull.sh``
    wiring into the maintenance tick so it runs right before
    :func:`restart_stale_daemons`: new code lands in the checkout and the same
    tick kickstarts the daemons onto it.

    Safety mirrors the ops script: no pull when HEAD is not ``main`` or the tree
    is dirty (excluding ``Worker/`` scratch), and ``--ff-only`` so a local commit
    fails loudly and retries next tick rather than merging/rebasing. A dependency
    change (``pyproject.toml`` / ``uv.lock``) triggers ``uv sync``.

    No-op (empty list) for the combined layout (``engine == vault``), a wheel
    install, or any non-checkout — anything without an ``engine/.git``.

    Returns the list of failure reasons: empty when nothing went wrong
    (including every skip / no-op), non-empty only when a git/uv command exited
    non-zero. The caller raises the tick rc on a non-empty list; subprocess
    failures do not raise, so a return value — not try/except alone — is what
    signals them.

    ``engine`` / ``vault`` / ``run`` are test seams; runtime calls pass nothing.
    """
    engine = Path(engine) if engine else paths.engine_root()
    vault = Path(vault) if vault else ROOT
    if engine == vault or not (engine / ".git").exists():
        return []

    def _git(*args: str) -> subprocess.CompletedProcess:
        return run(
            ["git", "-C", str(engine), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_S,
        )

    if _git("branch", "--show-current").stdout.strip() != "main":
        print("engine-pull: skip — HEAD가 main이 아님")
        return []
    if _git("status", "--porcelain", "--", ".", ":!Worker").stdout.strip():
        print("engine-pull: skip — 체크아웃 dirty")
        return []

    before = _git("rev-parse", "--short", "HEAD").stdout.strip()
    if _git("fetch", "origin", "main").returncode != 0:
        return ["engine-pull: fetch 실패"]
    if _git("pull", "--ff-only", "origin", "main").returncode != 0:
        return ["engine-pull: ff-only pull 실패 (로컬 커밋?)"]
    after = _git("rev-parse", "--short", "HEAD").stdout.strip()
    if before == after:
        return []

    # `diff --quiet` exits 0 when pyproject/uv.lock are unchanged, 1 when they
    # differ — only then does the environment need resyncing.
    if _git("diff", "--quiet", before, after, "--", "pyproject.toml", "uv.lock").returncode == 0:
        print(f"engine-pull: pulled {before} -> {after}")
        return []
    if run(
        ["uv", "sync", "-q"],
        cwd=str(engine),
        capture_output=True,
        text=True,
        check=False,
        timeout=_UV_SYNC_TIMEOUT_S,
    ).returncode != 0:
        return ["engine-pull: uv sync 실패"]
    print(f"engine-pull: pulled {before} -> {after} + uv sync")
    return []


def _resolve_base_ref(repo_root: Path) -> str | None:
    """The ref a merged branch should already be contained in.

    `origin/main` first: a local `main` can sit behind the remote for hours on
    this host, and judging "already merged" against a stale base under-reports.
    """
    for ref in ("origin/main", "main"):
        r = _git_read(repo_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if r.returncode == 0 and r.stdout.strip():
            return ref
    return None


def _is_contained_in_base(repo_root: Path, base: str, branch: str) -> bool:
    """True when `branch` contributes nothing that `base` does not already have.

    Two signals, OR'd, because worker PRs close with `gh pr merge --squash`:

    A. `merge-base --is-ancestor` — a real merge or fast-forward. On its own it
       misses every squash: the squashed commit is a *new* commit, so the branch
       tip is never an ancestor of main. Implementing only this would report 0
       of the 22 real worktrees while a synthetic-merge test went green.
    B. `merge-tree --write-tree` — merging `branch` into `base` yields base's own
       tree, i.e. the content is already there. This is what catches squashes.

    Signal B is read off the **stdout shape, not the return code**: merge-tree
    exits 1 both for a conflict and for a bad ref, so rc alone cannot tell
    "unmergeable" from "no such branch". A conflicted merge prints a non-oid
    first line, and a git older than 2.38 has no `--write-tree` at all — both
    land on the same conservative "not contained" answer.

    `--write-tree` writes loose objects into the object DB; it touches no ref,
    index, or working tree, and the objects are unreachable garbage that a
    later `gc` collects.
    """
    if _git_read(repo_root, "merge-base", "--is-ancestor", branch, base).returncode == 0:
        return True

    merged = _git_read(repo_root, "merge-tree", "--write-tree", base, branch)
    first_line = merged.stdout.split("\n", 1)[0].strip()
    if not _TREE_OID_RE.match(first_line):
        return False
    base_tree = _git_read(repo_root, "rev-parse", f"{base}^{{tree}}")
    return base_tree.returncode == 0 and base_tree.stdout.strip() == first_line


def _worktree_is_clean(path: Path) -> bool:
    """True only when the worktree has no modified, staged, or untracked files.

    Anything uncommitted means the worktree may be the only copy of that work,
    so it is not a removal candidate no matter how merged its branch is.
    """
    r = _git_read(path, "status", "--porcelain")
    return r.returncode == 0 and not r.stdout.strip()


def _removable_candidates(
    repo_root: Path, parent: Path,
    on_exclude: Callable[[str, str], None] | None = None,
) -> tuple[list[tuple[str, str]], int, int]:
    """Merged + clean `<parent>/wt-*` worktrees as `(path, branch)` pairs.

    The shared detection engine behind both `stale_worktree_actions` (report)
    and `reclaim_stale_worktrees` (remove). It applies only safety conditions
    1–2 — the branch's content is already in the base ref (real- or
    squash-merged, via `_is_contained_in_base`) and the worktree is clean
    (`_worktree_is_clean`). The two *removal-only* gates (unpushed work, live
    process) are deliberately the caller's, so the reporter keeps naming exactly
    what the reclaimer would consider before those gates.

    Bounded in count (`MAX_SCANNED_WORKTREES`) and wall-clock
    (`STALE_WORKTREE_BUDGET_S`); the over-cap / over-budget tallies are returned
    so the caller can declare what it skipped rather than let a truncated scan
    read as "nothing to clean". `parent` must already be resolved.

    Not fail-open on its own — every caller wraps it in `try/except → []`, so a
    git that breaks (missing binary, timeout, not a repo) surfaces there. The
    budget deadline is anchored on entry, ahead of the two fixed git calls
    below, so a slow `worktree list` / base lookup eats into the same budget.

    `on_exclude(path, reason)` fires for a worktree that cleared the `wt-`
    prefix + worker-root parent filter but is then dropped on cond 2 (`dirty`)
    or cond 1 (`unmerged`). The reclaimer passes it to name *why* a `wt-*` never
    reached a `정리 보류/완료` line — the previously silent gap this run closed
    (a superseded, still-unmerged worktree left no log trace at all). It fires
    only after the prefix/parent filter so unrelated worktrees (the main
    checkout, non-`wt-` siblings, out-of-root paths) never flood the tick log.
    The reporter passes no callback, so its output is unchanged.
    """
    deadline = _monotonic() + STALE_WORKTREE_BUDGET_S

    listing = _git_read(repo_root, "worktree", "list", "--porcelain")
    if listing.returncode != 0:
        return [], 0, 0
    base = _resolve_base_ref(repo_root)
    if base is None:
        return [], 0, 0

    records = _parse_worktree_porcelain(listing.stdout)
    scanned = records[:MAX_SCANNED_WORKTREES]
    over_cap = max(0, len(records) - MAX_SCANNED_WORKTREES)
    over_budget = 0

    candidates: list[tuple[str, str]] = []
    for index, record in enumerate(scanned):
        # Read on entry, ahead of the cheap filters and every git call, so an
        # exhausted budget costs nothing more. Candidates already confirmed are
        # kept: a truncated scan still names what it found.
        if _monotonic() >= deadline:
            over_budget = len(scanned) - index
            break
        raw_path = record.get("worktree", "")
        branch = record.get("branch", "")
        # `detached` has no branch to judge against; `locked` means a human is
        # holding it; `prunable` means the path is already gone (that is
        # `worktree prune` territory, out of scope here).
        if not raw_path or not branch:
            continue
        if {"bare", "detached", "locked", "prunable"} & record.keys():
            continue
        path = Path(raw_path)
        # Only `<worker root>/wt-*`. This single rule also excludes the main
        # checkout for free, wherever it happens to live.
        if path.parent.resolve() != parent or not path.name.startswith(WORKTREE_PREFIX):
            continue
        if not _worktree_is_clean(path):
            if on_exclude is not None:
                on_exclude(raw_path, "dirty")
            continue
        if not _is_contained_in_base(repo_root, base, branch):
            if on_exclude is not None:
                on_exclude(raw_path, "unmerged")
            continue
        candidates.append((raw_path, branch))

    return candidates, over_cap, over_budget


def _worktree_process_free(path: Path) -> bool:
    """True only when no live process references `path` (safety cond 4).

    `pgrep -f <path>` matches against the full command line, so a stage still
    executing inside the worktree — or a shell cd'd into it — shows up. `_pgrep`
    already collapses the three outcomes for us:
      * non-empty stdout  → a match     → in use   → False
      * empty stdout (rc 1, no match)   → idle     → True
      * None (rc≥2 / OSError / timeout) → unknown  → False (treated as in use)
    Unknown resolves to "in use" for the same reason `_runner_idle` treats every
    unreadable signal as busy: never destroy a worktree on missing information.
    """
    out = _pgrep("-f", str(path))
    return out is not None and not out.strip()


def _branch_has_no_unpushed_work(worktree_path: Path) -> bool:
    """True when the worktree's branch has no commits its upstream lacks (cond 3).

    Run inside the worktree so `@{u}` resolves to *this* branch's upstream (it is
    relative to HEAD, and a candidate worktree always has its branch — never a
    detached HEAD, filtered upstream in `_removable_candidates` — checked out).

    * Upstream resolves → require `rev-list --count @{u}..HEAD == "0"`: every
      local commit is already on the remote branch.
    * No upstream → return True. The worker never pushes the `worker/<slug>`
      branch itself; only the PR reaches origin, so a squash-merged branch has
      no upstream, and cond 1 (`_is_contained_in_base`) has already proved its
      content is in the base ref. A `--not --remotes` or a bare `@{u}..` here
      would instead treat every such branch as unpushed and re-preserve all of
      them — the vacuous no-op this gate exists to avoid.
    """
    upstream = _git_read(worktree_path, "rev-parse", "--verify", "--quiet", "@{u}")
    if upstream.returncode != 0 or not upstream.stdout.strip():
        return True
    counted = _git_read(worktree_path, "rev-list", "--count", "@{u}..HEAD")
    return counted.returncode == 0 and counted.stdout.strip() == "0"


def stale_worktree_actions(repo_root: Path = ROOT,
                           worktree_parent: Path | None = None) -> list[str]:
    """Report merged, clean `wt-*` worktrees as removal *candidates*.

    Read-only by construction: this never runs `worktree remove`, `worktree
    prune`, or `branch -d`, and never touches the queue. The returned strings
    carry the command for a human to run.

    Scope is worktrees attached to `repo_root` only. A worker task on an
    external repo puts its `wt-<slug>` on *that* clone, which this repo's
    `git worktree list` cannot see — those are out of scope here.

    Fail-open: any git failure (missing binary, timeout, not a repo) returns
    `[]`. doctor runs on the 5-minute maintenance tick and its queue self-heal
    has to complete even when this reporting hook cannot.

    Bounded in *time* as well as in count: the scan stops once
    `STALE_WORKTREE_BUDGET_S` is spent, because fail-open catches a git that
    breaks and not a git that merely crawls. Whatever the two limits skip is
    declared in the summary rather than dropped, so a truncated run never reads
    as "nothing to clean".
    """
    try:
        parent = (Path(worktree_parent) if worktree_parent is not None
                  else DEFAULT_WORKER_ROOT).resolve()
        candidates, over_cap, over_budget = _removable_candidates(repo_root, parent)
        paths = [raw_path for raw_path, _branch in candidates]

        actions = [
            f"정리 후보: {p} (git worktree remove {p})"
            for p in paths[:MAX_STALE_WORKTREE_ACTIONS]
        ]
        overflow = len(paths) - MAX_STALE_WORKTREE_ACTIONS
        if overflow > 0:
            actions.append(
                f"정리 후보 {overflow}개 더 있음 "
                f"(git worktree list --porcelain으로 확인)"
            )
        # One line, counts summed, but the parenthetical names which limit fired:
        # "the host has more worktrees than we look at" and "we ran out of time"
        # call for different fixes. The count-cap-only wording is byte-identical
        # to what it was before the budget existed.
        unscanned = over_cap + over_budget
        if unscanned > 0:
            if over_cap and over_budget:
                reason = (f"상한 {MAX_SCANNED_WORKTREES}개 + "
                          f"시간 예산 {STALE_WORKTREE_BUDGET_S}초 초과")
            elif over_budget:
                reason = f"시간 예산 {STALE_WORKTREE_BUDGET_S}초 초과"
            else:
                reason = f"상한 {MAX_SCANNED_WORKTREES}개"
            actions.append(f"worktree {unscanned}개는 검사하지 않음 ({reason})")
        return actions
    except Exception:
        # Never raise into doctor: the queue self-heal must still run.
        return []


def _delete_local_branch(repo_root: Path, branch: str) -> None:
    """Delete the local branch behind a just-removed worktree — best effort.

    `branch` is the porcelain ref (`refs/heads/worker/<slug>`); strip the prefix
    to the short name `git branch -d` wants. Try the safe `-d` first; a
    squash-merged branch is "not fully merged" in git's ancestor-only sense, so
    on that refusal fall back to `-D` — safe here because cond 1
    (`_is_contained_in_base`) has already proved the content is in the base ref.
    A failed delete only leaves a stale local ref, harmless now that the worktree
    (the disk cost this reclaim exists to recover) is already gone.
    """
    prefix = "refs/heads/"
    short = branch[len(prefix):] if branch.startswith(prefix) else branch
    if not short:
        return
    if _git_read(repo_root, "branch", "-d", short).returncode != 0:
        _git_read(repo_root, "branch", "-D", short)


def reclaim_stale_worktrees(repo_root: Path = ROOT,
                            worktree_parent: Path | None = None) -> list[str]:
    """Remove merged, clean `wt-*` worktrees and their local branches.

    The active counterpart to `stale_worktree_actions`: same detection
    (conditions 1–2 via `_removable_candidates`) plus two removal-only gates, and
    it actually runs `git worktree remove` + `git branch -d/-D` when — and only
    when — all four safety conditions hold. Any candidate failing a gate is
    logged and left completely untouched.

    All four are required to remove:
      1. content already in the base ref     — `_is_contained_in_base`  (cond 1)
      2. worktree clean, no uncommitted work  — `_worktree_is_clean`     (cond 2)
      3. no unpushed commits                  — `_branch_has_no_unpushed_work`
      4. no live process using the worktree   — `_worktree_process_free`

    Fail-open and count/time-bounded exactly like the reporter — it runs on the
    5-minute maintenance tick, so a git that breaks *or* merely crawls must never
    stall the queue self-heal. `git worktree remove` is deliberately
    `--force`-free: if the tree turned dirty between the clean check and here
    (TOCTOU) git aborts the remove and we log a hold rather than destroy the
    change. At most `MAX_STALE_WORKTREE_ACTIONS` are reclaimed per tick (each is
    several git subprocesses); the remainder are summarised and drained next
    tick, once these are gone from the listing.
    """
    try:
        parent = (Path(worktree_parent) if worktree_parent is not None
                  else DEFAULT_WORKER_ROOT).resolve()
        excluded: list[str] = []
        candidates, over_cap, over_budget = _removable_candidates(
            repo_root, parent,
            on_exclude=lambda raw, reason: excluded.append(
                f"정리 제외: {raw} ({reason})"),
        )

        # Exclusions first: a `wt-*` dropped on cond 1 (unmerged) or cond 2
        # (dirty) never reaches the gate loop below, so before this it left no
        # trace — the silent [] this run diagnosed (wt-macp-orphan-superseded,
        # a still-unmerged branch). The reporter passes no callback, so its
        # byte-identical output (PR #146 contract) is untouched.
        actions: list[str] = list(excluded)
        for raw_path, branch in candidates[:MAX_STALE_WORKTREE_ACTIONS]:
            path = Path(raw_path)
            # Cond 3 then cond 4, both removal-only gates the reporter skips.
            if not _branch_has_no_unpushed_work(path):
                actions.append(f"정리 보류: {raw_path} (unpushed)")
                continue
            if not _worktree_process_free(path):
                actions.append(f"정리 보류: {raw_path} (in-use)")
                continue
            if _git_read(repo_root, "worktree", "remove", raw_path).returncode != 0:
                # No --force, so a race that re-dirtied the tree lands here,
                # never on a destroyed change.
                actions.append(f"정리 보류: {raw_path} (remove 실패)")
                continue
            _delete_local_branch(repo_root, branch)
            actions.append(f"정리 완료: {raw_path} (worktree+branch 제거)")

        overflow = len(candidates) - MAX_STALE_WORKTREE_ACTIONS
        if overflow > 0:
            actions.append(
                f"정리 후보 {overflow}개 더 있음 "
                f"(git worktree list --porcelain으로 확인)"
            )
        unscanned = over_cap + over_budget
        if unscanned > 0:
            if over_cap and over_budget:
                reason = (f"상한 {MAX_SCANNED_WORKTREES}개 + "
                          f"시간 예산 {STALE_WORKTREE_BUDGET_S}초 초과")
            elif over_budget:
                reason = f"시간 예산 {STALE_WORKTREE_BUDGET_S}초 초과"
            else:
                reason = f"상한 {MAX_SCANNED_WORKTREES}개"
            actions.append(f"worktree {unscanned}개는 검사하지 않음 ({reason})")
        return actions
    except Exception:
        return []


def doctor_recover_queue(queue: dict[str, Any]) -> list[str]:
    """Self-heal common stuck-queue conditions before scan_queue runs.

    - in_progress entries older than STUCK_IN_PROGRESS_THRESHOLD with no
      live run-dir status.json → reset to pending (worker died mid-claim,
      typically dirty-check abort path).
    - failed entries that have not been auto-retried → reset to pending
      with auto_retry_count incremented. Tasks whose source markdown is
      cancelled / status != open get the failed entry marked `skipped`.
    - pending entries whose source task has since gone done / cancelled →
      marked `skipped`, so they stop inflating count_auto_pending() and the
      auto_queue_depth_limit check measures the live queue.
    - merged, clean `wt-*` worktrees whose branch has no unpushed work and no
      live process → the worktree + its local branch are reclaimed
      (`reclaim_stale_worktrees`); any that fail a safety gate are logged only.
      This branch touches only the filesystem, never the queue, so it cannot
      interact with the three above.
    - per-run `<repo>.wt/<slug>` worktrees of failed / parked external runs
      idle past the retention window, and no run, long_job or process holding
      them → the worktree (never its branch ref) and its gradle-registry dir
      are reclaimed, plus orphan registry dirs
      (`orchestrator.worktree_sweep.reclaim_parked_worktrees`). Filesystem only.

    Returns a list of human-readable action descriptions for logging.
    """
    actions: list[str] = []
    now = dt.datetime.now()
    # Map slug -> source task status (for cancellation detection)
    slug_to_status: dict[str, str] = {}
    for t in iter_worker_tasks():
        slug_to_status[t.slug] = t.status

    for entry in queue.get("runs", []):
        slug = entry.get("slug", "?")
        status = entry.get("status")

        if status == "in_progress":
            run_dir = RUNS_DIR / entry.get("run_id", "")
            status_file = run_dir / "status.json"
            # Orphaned-but-finished: the run actually completed (status.json has
            # finished_at) but the terminal queue update was lost (process killed
            # right after writing status.json, or a dropped queue write).
            # Reconcile the entry to its real terminal status instead of leaving
            # it stuck "running" forever.
            if status_file.exists():
                try:
                    sj = json.loads(status_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    sj = {}
                if sj.get("finished_at"):
                    final = "failed" if sj.get("failure_reason") else "completed"
                    entry["status"] = final
                    entry["finished_at"] = sj["finished_at"]
                    actions.append(
                        f"reconcile orphaned {final}: {slug} "
                        f"(status.json finished, DB stuck running)"
                    )
                    continue
            started = _parse_iso_timestamp(entry.get("started_at"))
            if started is None:
                continue
            age = now - started
            if age <= STUCK_IN_PROGRESS_THRESHOLD:
                continue
            if status_file.exists():
                # status.json present but not finished → live long-running
                # stage; don't disturb it.
                continue
            entry["status"] = "pending"
            entry["started_at"] = None
            entry["branch"] = None
            actions.append(
                f"reset in_progress→pending: {slug} (idle {int(age.total_seconds()/3600)}h, no run-dir)"
            )

        elif status == "failed":
            # If source task was cancelled or removed, mark entry skipped.
            source_status = slug_to_status.get(slug)
            if source_status in ("cancelled", "done") or source_status is None:
                entry["status"] = "skipped"
                actions.append(f"skip cancelled/removed task: {slug}")
                continue
            # Otherwise auto-retry once.
            retries = int(entry.get("auto_retry_count", 0))
            if retries >= AUTO_RETRY_LIMIT:
                continue
            entry["status"] = "pending"
            entry["auto_retry_count"] = retries + 1
            entry["started_at"] = None
            entry["finished_at"] = None
            actions.append(
                f"auto-retry failed entry: {slug} (attempt {retries + 2})"
            )

        elif status == "pending":
            # Deliberately narrower than the `failed` branch above: that one also
            # skips on `source_status is None`, this one must not. `Todo/items/`
            # is gitignored, so on a fresh clone or another host slug_to_status
            # comes back empty and "not found" would close every live row at once.
            source_status = slug_to_status.get(slug)
            if source_status in ("done", "cancelled"):
                entry["status"] = "skipped"
                actions.append(
                    f"skip stale pending: {slug} (source {source_status})"
                )

    # Outside the loop and last on purpose: it touches only the filesystem
    # (never the queue) and is fail-open internally, so it cannot change any
    # verdict reached above.
    actions.extend(reclaim_stale_worktrees())
    # Same contract, the per-run `<repo>.wt/<slug>` layout the `wt-*` sweep above
    # deliberately cannot see. Fail-open inside; the guard here is a second belt.
    try:
        actions.extend(_worktree_sweep.reclaim_parked_worktrees())
    except Exception:
        pass

    return actions


def _queue_fingerprint(queue: dict[str, Any]) -> str:
    """A stable snapshot of the queue, used to detect real changes.

    `default=str` so an unexpected non-JSON value degrades to "looks different"
    rather than raising inside the maintenance tick.
    """
    return json.dumps(queue, sort_keys=True, default=str)


def run_doctor_and_persist(queue: dict[str, Any]) -> list[str]:
    """Run doctor, log every action, and save the queue only if it changed.

    The save is gated on the queue *actually changing* rather than on "doctor
    said something", which is what the old `if doctor_actions:` meant. doctor
    now also emits stale-worktree reclaim actions, and a candidate that stays
    *held* (in-use / unpushed) keeps that list non-empty on every 5-minute
    tick — while touching no queue state; gating on "said something" would rewrite
    queue.json each such tick, for state nothing modified, widening the window
    in which a concurrent orchestrator write gets clobbered.
    """
    before = _queue_fingerprint(queue)
    actions = doctor_recover_queue(queue)
    for action in actions:
        print(f"doctor: {action}")
    if _queue_fingerprint(queue) != before:
        save_queue(queue)
    return actions


def _parse_iso_timestamp(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    try:
        ts = dt.datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    # A timestamp may arrive tz-aware, but the rest of the code uses naive
    # local time (now_iso / datetime.now()). Normalize aware → naive local so
    # subtractions against datetime.now() don't raise TypeError.
    if ts.tzinfo is not None:
        ts = ts.astimezone().replace(tzinfo=None)
    return ts


def _load_remote_ack() -> set[str]:
    if not REMOTE_ACK_PATH.exists():
        return set()
    try:
        data = json.loads(REMOTE_ACK_PATH.read_text(encoding="utf-8"))
        return set(data.get("acked", []))
    except (OSError, ValueError):
        return set()


def _save_remote_ack(acked: set[str]) -> None:
    try:
        REMOTE_ACK_PATH.write_text(
            json.dumps({"acked": sorted(acked)}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"remote-sweep: ack save failed — {exc}")


def sweep_remote_completed_runs() -> int:
    """Primary-host sweep: surface finished remote-host runs (M5).

    Remote hosts push their Worker/remote/<host>/queue.json via git; here we
    read every remote queue and, for each completed/failed run not yet
    acknowledged, create a review todo + kakao notification. Ack state is
    primary-host-local (Worker/remote_ack.json, gitignored) so we never write
    to files a remote host owns — that keeps the git sync conflict-free.
    """
    if IS_REMOTE_HOST or not REMOTE_DIR.exists():
        return 0

    acked = _load_remote_ack()
    surfaced = 0
    for qpath in sorted(REMOTE_DIR.glob("*/queue.json")):
        host = qpath.parent.name
        try:
            runs = json.loads(qpath.read_text(encoding="utf-8")).get("runs", [])
        except (OSError, ValueError) as exc:
            print(f"remote-sweep: cannot read {qpath.name} for host={host}: {exc}")
            continue
        for entry in runs:
            run_id = entry.get("run_id")
            status_v = entry.get("status")
            if not run_id or status_v not in ("completed", "failed"):
                continue
            if run_id in acked:
                continue
            slug = entry.get("slug", "?")
            task_path = qpath.parent / "inbox" / f"{slug}.md"
            run_dir = qpath.parent / "runs" / run_id

            if status_v == "completed" and task_path.exists():
                try:
                    fm, _ = parse_frontmatter(task_path.read_text(encoding="utf-8"))
                    task = WorkerTask(
                        slug=slug,
                        path=task_path,
                        rel_path=str(task_path.relative_to(ROOT)),
                        frontmatter=fm,
                    )
                    review = create_review_todo(
                        task, run_id, run_dir, entry.get("mode") or "auto"
                    )
                    if review:
                        print(
                            f"remote-sweep: review todo for {host}/{slug} → "
                            f"{review.relative_to(ROOT)}"
                        )
                except Exception as exc:
                    print(f"remote-sweep: review todo failed for {host}/{slug}: {exc}")

            # No Kakao ping for remote-run completions — it's a low-signal FYI the
            # user asked to stop (2026-07-23). Genuine review needs still surface:
            # completed runs get a review todo above, and the v2 orchestrator sends
            # its own [검토요청]/[격리] pings for tasks that actually need a human.
            acked.add(run_id)
            surfaced += 1

    if surfaced:
        _save_remote_ack(acked)
    return surfaced


def cmd_maintenance() -> int:
    """Vault upkeep that used to ride along inside scan_queue().

    scan_queue() does two unrelated jobs: legacy-queue dispatch AND vault upkeep.
    After the v2 cutover the dispatch half is retired (the orchestrator owns it —
    running both is what produced the duplicate-dispatch loop), but the upkeep half
    still has to run somewhere:

      - autoclose_stale_reviews_local: closes review-<slug> todos whose parent is
        done, so they stop piling up in the dashboard's 사람 할일 list.
      - sweep_remote_completed_runs: surfaces finished remote-host (Windows/sc1)
        runs as review todos + kakao — the only path by which remote work is
        reported back at all.
      - sync_vault_state: commits + pushes the tracked ledger/episodes/mailbox.
        This one was NOT carried over at cutover time and went unnoticed for six
        days, because losing it changes nothing locally — it only means the
        remote (and any host that takes over) is stale.
      - browser_watch.check_login_watch: hourly probe of the HQ's logged-in
        browser profile; pages on login-state transitions only.

    doctor_recover_queue is deliberately NOT here: it heals Worker/queue.json,
    which is exactly the store the cutover retires.
    """
    rc = 0
    if not IS_REMOTE_HOST:
        try:
            closed = autoclose_stale_reviews_local()
            if closed > 0:
                print(f"review-autoclose: {closed} closed")
        except Exception as exc:
            print(f"review-autoclose: error — {exc}")
            rc = 1
        try:
            surfaced = sweep_remote_completed_runs()
            if surfaced > 0:
                print(f"remote-sweep: {surfaced} remote run(s) surfaced")
        except Exception as exc:
            print(f"remote-sweep: error — {exc}")
            rc = 1
        # The third piece of scan_queue()'s upkeep, missing since the cutover.
        try:
            synced = sync_vault_state()
            if synced > 0:
                print(f"vault-sync: {synced} file(s) committed")
        except Exception as exc:
            print(f"vault-sync: error — {exc}")
            rc = 1
        try:
            for name in rotate_logs():
                print(f"log-rotate: {name} truncated")
        except Exception as exc:
            print(f"log-rotate: error — {exc}")
            rc = 1
        # Merging a migration is not applying it. Twice on Pairent a task closed
        # green with the live database untouched; only a direct query noticed.
        try:
            for warning in check_unapplied_migrations():
                print(f"migration-drift: {warning}")
                try:
                    kakao_notify(_compose_migration_drift(warning))
                except Exception:  # noqa: BLE001 - the print already surfaced it
                    pass
        except Exception as exc:
            print(f"migration-drift: error — {exc}")
            rc = 1
        # Pull macp's own merged PRs into the engine checkout *before* the
        # restart below, so the daemons kickstart onto the new code in one tick.
        try:
            for reason in pull_engine_checkout():
                print(reason)
                rc = 1
        except Exception as exc:
            print(f"engine-pull: error — {exc}")
            rc = 1
        # macp edits its own source; the daemons executing those edits are
        # resident and would otherwise never load them.
        try:
            for label in restart_stale_daemons():
                print(f"daemon-restart: {label} was running stale code — kickstarted")
        except Exception as exc:
            print(f"daemon-restart: error — {exc}")
            rc = 1
        # Everything that broke this week broke quietly. This is the one detector
        # for the task pipeline going silent with work still queued.
        try:
            msg = check_dispatch_silence()
            if msg:
                print(f"silence-check: paged — {msg}")
        except Exception as exc:
            print(f"silence-check: error — {exc}")
            rc = 1
        # ...and the mirror: the pipeline going silent with NOTHING queued and
        # no way left to find work on its own. Only a human can end that.
        try:
            msg = check_work_starvation()
            if msg:
                print(f"starvation-check: paged — {msg}")
        except Exception as exc:
            print(f"starvation-check: error — {exc}")
            rc = 1
        # The logged-in browser profile drops its session silently; find out
        # when it drops, not when a task next needs it.
        try:
            msg = browser_watch.check_login_watch(notify=kakao_notify)
            if msg:
                print(f"browser-watch: paged — {msg.splitlines()[0]}")
        except Exception as exc:
            print(f"browser-watch: error — {exc}")
            rc = 1
    return rc


def scan_queue() -> int:
    queue = load_queue()

    # Remote hosts skip all primary-host maintenance (review autoclose, parent
    # tree backfill, remote sweep) — their data source is just their inbox.
    if IS_REMOTE_HOST:
        pass
    # Review autoclose sweep: close stale review todos before other operations.
    else:
        try:
            closed = autoclose_stale_reviews_local()
            if closed > 0:
                print(f"review-autoclose: {closed} closed")
        except Exception as exc:
            print(f"review-autoclose: error — {exc}")

    # Primary host: surface finished remote-host runs (git-synced queue files)
    # as review todos + kakao notifications.
    if not IS_REMOTE_HOST:
        try:
            surfaced = sweep_remote_completed_runs()
            if surfaced > 0:
                print(f"remote-sweep: {surfaced} remote run(s) surfaced")
        except Exception as exc:
            print(f"remote-sweep: error — {exc}")

    # Self-heal: reset stuck/auto-retryable entries before the dispatcher
    # scans for new tasks. Each action is logged so users see what changed.
    run_doctor_and_persist(queue)
    existing_slugs = {entry["slug"] for entry in queue["runs"]}
    # Slugs whose latest entry is still actionable; if completed/failed, allow re-queue only if user manually clears entry.
    actionable_status = {"pending", "in_progress"}
    actionable_slugs = {
        entry["slug"]
        for entry in queue["runs"]
        if entry.get("status") in actionable_status
    }

    added = 0
    # Create runs highest-priority-first so the FIFO (created_at) claim order
    # honors task priority within a scan batch. (Cross-batch ordering would need
    # a priority column on orchestrator_runs; this covers the common case.)
    for task in sorted(
        iter_worker_tasks(),
        key=lambda t: -int(t.frontmatter.get("priority", 0) or 0),
    ):
        if task.status != "open":
            continue
        # Date gate: `not_before` defers auto-dispatch until that moment (the
        # same field NOW honors). Lets a task stay worker_mode:auto yet not fire
        # until a window closes — e.g. a paper-trading round whose results can
        # only be recorded once the run ends. Date-only = 00:00 that day;
        # `YYYY-MM-DD HH:MM` / `THH:MM[:SS]` host-local; see not_before.py.
        if nb_gate.is_deferred(task.frontmatter.get("not_before")):
            continue
        if task.slug in actionable_slugs:
            continue
        # Already had a terminal run? Skip — user must remove the old entry to re-run.
        if task.slug in existing_slugs:
            continue

        mode = task.worker_mode
        # Dispatcher: tasks without an explicit worker_mode are evaluated by
        # cheap keyword heuristics. AUTONOMOUS_KEYWORDS present and no
        # HUMAN_INTERVENTION_KEYWORDS → auto-assign 'delegate'. Parents and
        # explicit opt-outs (`delegate: false`) are never auto-picked.
        if mode == "off":
            if task.is_parent or task.delegate_opt_out:
                continue
            if task.matches_human_intervention():
                continue
            if not task.matches_autonomous():
                continue
            mode = "delegate"
        elif mode not in STAGES_BY_MODE:
            continue

        queue["runs"].append({
            "slug": task.slug,
            "run_id": make_run_id(task.slug),
            "task_path": task.rel_path,
            "mode": mode,
            "host": WORKER_HOST,
            "status": "pending",
            "created_at": now_iso(),
            "started_at": None,
            "finished_at": None,
            "branch": None,
        })
        added += 1

    save_queue(queue)
    print(f"scan-queue: {added} new run(s) added; total entries = {len(queue['runs'])}")
    return 0


# ---------------------------------------------------------------------------
# list / status
# ---------------------------------------------------------------------------


def cmd_list() -> int:
    queue = load_queue()
    runs = queue.get("runs", [])
    if not runs:
        print("no runs in queue")
        return 0
    pending = sum(1 for r in runs if r["status"] == "pending")
    in_progress = sum(1 for r in runs if r["status"] == "in_progress")
    completed = sum(1 for r in runs if r["status"] == "completed")
    failed = sum(1 for r in runs if r["status"] == "failed")
    print(
        f"queue summary: pending={pending} in_progress={in_progress} "
        f"completed={completed} failed={failed} total={len(runs)}"
    )
    for r in runs:
        print(
            f"- {r['status']:<11} {r['run_id']} | slug={r['slug']} | "
            f"branch={r.get('branch') or '-'}"
        )
    return 0


def cmd_status(run_id: str) -> int:
    status_path = RUNS_DIR / run_id / "status.json"
    if not status_path.exists():
        print(f"status not found: {status_path}")
        return 1
    print(status_path.read_text(encoding="utf-8"))
    return 0


def cmd_run_parallel(max_concurrent: int, dry_run: bool) -> int:
    """Fan out up to N parallel `run-next` subprocesses.

    Each child claims its own pending entry via flock on queue.lock. Children
    exit normally; parent waits for all and returns 0 if every child succeeded.

    Vault tasks (no `repo` field) and external-repo tasks are both supported,
    but only external-repo tasks benefit from true parallelism (independent
    working_dirs). Concurrent vault tasks would race on vault source files;
    run-parallel does NOT prevent this — keep vault `worker_mode:auto` rare or
    set max_concurrent=1 if vault tasks dominate the queue.
    """
    if max_concurrent < 1:
        print("run-parallel: --max must be >= 1")
        return 2

    with queue_lock():
        queue = load_queue()
        pending_count = sum(
            1 for r in queue.get("runs", []) if r.get("status") == "pending"
        )

    if pending_count == 0:
        print("run-parallel: no pending runs")
        return 0

    n = min(max_concurrent, pending_count)
    print(
        f"run-parallel: spawning {n} worker(s) (pending={pending_count}, "
        f"max_concurrent={max_concurrent}, dry_run={dry_run})"
    )

    base_argv = [
        sys.executable,
        "-m",
        "todo_system.worker",
        "run-next",
    ]
    if dry_run:
        base_argv.append("--dry-run")

    # Two roots (see the top of this module). cwd stays on the vault: cli,
    # session_log and friends resolve vault_root() from cwd. In a split install
    # (_ENGINE_ROOT != ROOT) cwd alone cannot serve both jobs, so the vault the
    # parent resolved is handed over explicitly. A split CHECKOUT additionally
    # puts the engine at the front of the import path — the child then imports
    # todo_system without site-packages and cannot re-resolve a different vault.
    # An installed wheel is already in the child's site-packages, and its
    # _ENGINE_ROOT is todo_system/_engine — a PYTHONPATH entry there would point
    # at site-packages, so it gets the vault only. In the combined layout env
    # stays None, so the child (and every stage it spawns) inherits exactly what
    # it did before.
    child_env = None
    if ROOT.resolve() != _ENGINE_ROOT:
        child_env = {**os.environ, "MACP_VAULT_ROOT": str(ROOT)}
        if not paths.is_installed():
            prior = os.environ.get("PYTHONPATH")
            child_env["PYTHONPATH"] = os.pathsep.join(
                p for p in (str(_ENGINE_ROOT), prior) if p)

    procs: list[subprocess.Popen[bytes]] = []
    for i in range(n):
        log_path = WORKER_DIR / f"run-parallel-{i:02d}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_f = open(log_path, "wb")
        p = subprocess.Popen(
            base_argv,
            cwd=ROOT,
            env=child_env,
            stdout=log_f,
            stderr=subprocess.STDOUT,
        )
        procs.append(p)
        print(f"  child #{i:02d} pid={p.pid} log={log_path.relative_to(ROOT)}")

    rcs = [p.wait() for p in procs]
    failed = [(i, rc) for i, rc in enumerate(rcs) if rc != 0]
    print(f"run-parallel: done — {len(rcs)} child(ren), {len(failed)} failed")
    return 0 if not failed else 1


# ---------------------------------------------------------------------------
# Safety: vault dirty-check
# ---------------------------------------------------------------------------


# Files the daemon regenerates constantly; safe to ignore in the dirty check.
ALLOWED_DIRTY_PREFIXES = (
    "Worker/",
    "Memory/episodes/",
    "Memory/sessions/",      # manual session_log checkpoints (written by a
                             # separate agent while worker keeps running)
    "Todo/Briefings/",       # daily brief output
    "Todo/Inbox/",           # cross-link review queue
    "Todo/items/",           # task md: git-tracked since 2026-09-10, auto-committed by
                             # sync_vault_state — an edit must not stall the queue
)
ALLOWED_DIRTY_EXACT = {
    "Todo/Dashboard.md",
    "Todo/NOW.md",
    "Todo/FOCUS.md",
    "Todo/Unified.md",
    "Todo/Calendar.md",
    "Memory/index.md",
}


# Vault state that is git-tracked ON PURPOSE and has to reach the remote: the
# assignment ledger (.gitignore keeps it tracked so "a cloud takeover inherits"
# it), the run episodes that are the durable record of what the worker did, the
# mailbox, whose consumed messages are deletions that otherwise pile up, and
# Worker/remote — where git is not a backup but the actual TRANSPORT: a task
# written to Worker/remote/<host>/inbox/ reaches that machine only by being
# pushed, and its results come back the same way.
#
# Worker as a whole rather than an enumeration of its children, because naming
# children is how this list goes wrong: remote/ was missed on the first cut
# (silently one-way dispatch), and planner_policy.yaml — live planner config —
# would have been missed on the second. Everything noisy under Worker/ is
# already gitignored (runs/, queue.json, planner_state.json, *_state/), so the
# parent adds exactly the tracked files and nothing else.
#
# Deliberately NOT the whole vault — Todo/items/ and the generated indices are
# gitignored, and a blanket `git add -A` would sweep in whatever a stage happens
# to have left in the working tree mid-run.
VAULT_SYNC_PATHS = ("Worker", "Memory", "Todo/items")   # Todo/items tracked since 2026-09-10 (cloud-HQ SoT decision)


def sync_vault_state(paths: tuple[str, ...] = VAULT_SYNC_PATHS, _run=None) -> int:
    """Commit + push the tracked vault state. Returns the number of files synced.

    The v2 cutover moved scan_queue()'s upkeep into cmd_maintenance and left this
    behind, so nothing committed vault state between 2026-07-30 and 2026-08-05:
    53 ledger entries and 59 run episodes lived only on this Mac, and the
    ledger's whole reason for being tracked — surviving a host handoff — was
    quietly false for six days. Local operation is unaffected, which is exactly
    why nobody noticed.

    Best-effort and non-fatal: several hosts push this repo, so a rejected push
    is rebased once and retried. That retry's result is now checked and logged in
    full — an unchecked re-push (or a failed rebase before it) used to leave main
    silently behind origin with nothing in the log — but anything still failing
    just leaves the commit local for the next tick. Nothing here may raise into
    maintenance. `_run` is a test seam; tests inject a fake to drive the
    push/rebase/re-push race without a real remote.
    """
    run = _run or subprocess.run

    def git(*args: str) -> subprocess.CompletedProcess:
        return run(["git", *args], cwd=ROOT, capture_output=True,
                   text=True, timeout=120)

    try:
        # The vault is both the repo we commit AND the working tree stages run
        # in — they `git checkout` here, which is why restore_main exists. HEAD is
        # therefore not always main, and committing regardless drops ledger
        # entries and episodes onto a task branch while `push origin main` pushes
        # a main that does not contain them. PRs #40 and #41 both swallowed vault
        # state this way (2026-08-05), survivable only because they merged; an
        # abandoned branch would take that state with it. Deferring costs one
        # tick, and the state is still on disk for the next one.
        head = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if head != "main":
            print(f"vault-sync: HEAD is {head!r}, not main — deferring")
            return 0
        existing = [p for p in paths if (ROOT / p).exists()]
        if not existing:
            return 0
        # `git add -A` stages what is on disk, but `git commit` writes the WHOLE
        # index — so an index left behind at an older tree silently commits a
        # reverse diff of everything that landed since. 2026-09-25: main was
        # advanced by a ref-only move (a reflog entry with no message) while the
        # index and working tree stayed on the previous tree, and the next
        # tick's `chore: sync vault state (16 files)` (e056a1e35) deleted the
        # DanceMirror beta-publish wiring of 7ff78bb55 with no alarm at all — a
        # person found it and restored it by hand (05b1c4053).
        #
        # Judged by --quiet's RETURN CODE, and deliberately with no pathspec:
        # what got reverted lived OUTSIDE the synced paths, so a path-limited
        # comparison would wave the same accident through. Any non-zero rc
        # defers — 1 means the index differs, anything else means git could not
        # tell us, and neither may authorise a commit. Not healed automatically:
        # a `read-tree` here would throw away a person's staging.
        if git("diff", "--cached", "--quiet").returncode != 0:
            names = git("diff", "--cached", "--name-only").stdout.strip().splitlines()
            detail = f" — staged: {', '.join(names[:3])}" if names else ""
            print("vault-sync: index is not at HEAD (ref-only move or manual "
                  f"staging) — deferring rather than committing a reverse diff{detail}")
            return 0
        git("add", "-A", *existing)
        staged = git("diff", "--cached", "--name-only").stdout.strip()
        if not staged:
            return 0
        n = len(staged.splitlines())
        if git("commit", "-m", f"chore: sync vault state ({n} files)").returncode != 0:
            return 0
        if git("push", "origin", "main").returncode != 0:
            # Another host pushed first. Rebase onto it and push again — but the
            # rebase and the SECOND push were both fire-and-forget, so a rebase
            # that failed (or a re-push still rejected) left main behind origin
            # with nothing logged. Check both, in full and non-fatally.
            # fetch + rebase onto the remote-tracking ref, not `pull --rebase`:
            # pull rebases onto FETCH_HEAD, a file every concurrent fetch on this
            # checkout rewrites, and the race surfaced ~26 times as "fatal:
            # Cannot rebase onto multiple branches" (2026-10-04/05).
            pulled = git("fetch", "origin", "main")
            if pulled.returncode == 0:
                pulled = git("rebase", "origin/main")
                if pulled.returncode != 0:
                    git("rebase", "--abort")   # never leave a half-done rebase behind
            if pulled.returncode != 0:
                detail = (pulled.stderr or pulled.stdout or "").strip()
                print(f"vault-sync: rebase before re-push failed — {detail}")
            else:
                repush = git("push", "origin", "main")
                if repush.returncode != 0:
                    detail = (repush.stderr or repush.stdout or "").strip()
                    print(f"vault-sync: push still rejected after rebase — {detail}")
        return n
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"vault-sync: error — {exc}")
        return 0


# Repos whose migrations must actually reach a live database, and where to find
# the connection details. The password is NOT stored here — it is read at run time
# from the file that already holds it, so this check adds no second copy of a
# credential. `pw_file`/`pw_key` describe how to pull it out of that file.
#
# Why this check exists: on Pairent the same thing happened twice. leaf 7 (schema
# + RLS) and leaf 14 (realtime publication) both merged, both closed as done, and
# neither migration was ever applied to the live project. leaf 14 in particular
# looked finished for a day while realtime chat was still dead, because the fix
# was real — it just wasn't anywhere the app could see it. Nothing in CI catches
# this: CI has no live database, and merging is not applying.
MIGRATION_TARGETS = {
    "Pairent": {
        "repo": Path.home() / "Worker" / "Pairent",
        "pw_file": Path.home() / "Documents" / "pairent-supabase" / "credentials.txt",
        "pw_key": "DB password:",
        "dsn": ("postgresql://postgres.qyxlikbhntvnrruacrzs:{pw}"
                "@aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"),
    },
}
PSQL_CANDIDATES = ("/opt/homebrew/opt/libpq/bin/psql", "/usr/local/opt/libpq/bin/psql", "psql")


def _psql() -> str | None:
    for c in PSQL_CANDIDATES:
        if c == "psql" or Path(c).exists():
            return c
    return None


def check_unapplied_migrations(targets: dict | None = None) -> list[str]:
    """Migrations that exist in a repo but never reached its live database.

    Returns one human-readable line per repo that has drift; empty when everything
    is applied. Never raises — a missing password file or an unreachable database
    means "can't tell", and "can't tell" must not take down maintenance.
    """
    warnings: list[str] = []
    psql = _psql()
    if psql is None:
        return warnings
    for name, cfg in (targets or MIGRATION_TARGETS).items():
        try:
            mig_dir = Path(cfg["repo"]) / "supabase" / "migrations"
            local = sorted(p.name.split("_", 1)[0] for p in mig_dir.glob("*.sql"))
            if not local:
                continue
            pw_file = Path(cfg["pw_file"])
            if not pw_file.exists():
                continue
            pw = ""
            for line in pw_file.read_text(encoding="utf-8").splitlines():
                if line.startswith(cfg["pw_key"]):
                    pw = line.split(cfg["pw_key"], 1)[1].strip()
                    break
            if not pw:
                continue
            proc = subprocess.run(
                [psql, cfg["dsn"].format(pw=pw), "-Atc",
                 "select version from supabase_migrations.schema_migrations"],
                capture_output=True, text=True, timeout=45,
            )
            if proc.returncode != 0:
                continue  # unreachable -> can't tell, stay quiet
            applied = {v.strip() for v in proc.stdout.splitlines() if v.strip()}
            missing = [v for v in local if v not in applied]
            if missing:
                warnings.append(
                    f"{name}: {len(missing)} migration(s) merged but NOT applied to "
                    f"the live database — {', '.join(missing)}"
                )
        except Exception as exc:  # noqa: BLE001 - a check must not break maintenance
            print(f"migration-drift: {name} check failed — {exc}")
    return warnings


SILENCE_THRESHOLD_S = 6 * 3600
SILENCE_STATE_FILE = ROOT / "Worker" / "_state" / "silence-alert.json"
SILENCE_COOLDOWN_S = 12 * 3600


def check_dispatch_silence(now: float | None = None,
                           threshold_s: float = SILENCE_THRESHOLD_S) -> str | None:
    """Alert when the system has work queued and has stopped doing it.

    Everything significant found in 2026-08-05/06 failed the same way: silently.
    The vault went six days unsynced, the runner ran seven commits stale, three
    paper runs died, the brief went missing — none of it turned anything red, so
    each was found only by someone going to look. The trading watchdog is the one
    silence detector that exists, and it is the only reason yesterday's paper run
    was noticed at all. This is that watchdog for the task pipeline.

    Deliberately narrow, because a false alarm here trains you to ignore it:

    - Silent with NOTHING queued is correct, not a fault.
    - Silent because the usage guard is pacing is correct too — it is the guard
      working, and it can legitimately hold for hours.
    - Only "work is waiting, the guard is not the reason, and nothing has been
      dispatched in `threshold_s`" pages.

    Returns the message sent, or None. Never raises: this runs inside the
    maintenance tick.
    """
    now = time.time() if now is None else now
    try:
        from .orchestrator import tasks as _tasks
        from .orchestrator.assignment import Ledger

        assignments = list(Ledger(ROOT / "Worker" / "assignments").all())
        parked = {a.task_id for a in assignments if a.state == "awaiting_human"}
        # load_dispatchable() is a TODO-level view: status open + worker_mode auto.
        # A task can satisfy that and still be correctly idle, because its LEDGER
        # entry is parked awaiting a human — dispatch() refuses to re-send those.
        # Counting them as "queued" made the first real firing a false positive
        # (2026-08-07: both queued tasks were parked on PRs awaiting review, and
        # the system was behaving exactly as designed). Silence while everything
        # waiting is waiting on a HUMAN is correct, not a stall.
        pending = [t for t in _tasks.load_dispatchable() if t.slug not in parked]
        if not pending:
            return None                      # nothing dispatchable: silence is correct

        try:                                 # guard pacing is a legitimate hold
            from todo_system import usage_guard
            if not usage_guard.check().get("allowed", True):
                return None
        except Exception:
            pass                             # guard unreadable -> keep checking

        from .orchestrator.orchestrator import _parse_ts
        stamps = [_parse_ts(a.updated_ts) for a in assignments if a.updated_ts]
        last = max((s for s in stamps if s), default=0.0)
        idle = now - last
        if idle < threshold_s:
            return None

        if now - _read_silence_state() < SILENCE_COOLDOWN_S:
            return None                      # already paged; do not repeat hourly
        msg = _compose_dispatch_silence(len(pending), idle / 3600, threshold_s / 3600)
        kakao_notify(msg)
        _write_silence_state(now)
        return msg
    except Exception as exc:
        print(f"silence-check: error — {exc}")
        return None


def _read_silence_state() -> float:
    try:
        return float(json.loads(SILENCE_STATE_FILE.read_text(encoding="utf-8"))["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def _write_silence_state(ts: float) -> None:
    try:
        SILENCE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SILENCE_STATE_FILE.write_text(json.dumps({"ts": ts}), encoding="utf-8")
    except OSError:
        pass


STARVATION_STATE_FILE = ROOT / "Worker" / "_state" / "starvation-alert.json"
STARVATION_COOLDOWN_S = 24 * 3600
PLANNER_STATE_FILE = ROOT / "Worker" / "planner_state.json"
_IN_FLIGHT_STATES = frozenset({"dispatched", "accepted", "in_progress"})


def check_work_starvation(now: float | None = None) -> str | None:
    """Alert when the system has NO work and cannot find any on its own.

    The mirror image of `check_dispatch_silence`. That one pages when work is
    queued and nothing moves; this one pages when nothing is queued and the two
    things that feed the queue have both given up: the planner answered
    `exhausted` and discovery (its fallback) has no repo it could explore. From
    here nothing changes until a human writes a task — and an idle pipeline is
    exactly as quiet as a healthy one, so without this the only way to notice
    is to go and look.

    Fires only when ALL of these hold:

    - No dispatchable task, once ledger-parked (awaiting_human) rows are set
      aside — those are the human's queue, not the AI's.
    - Nothing in flight in the ledger. A running task will change the state
      when it finishes; let it.
    - The planner has evaluated the CURRENT state and said `exhausted`: its
      `last_run` is newer than the last ledger change. A verdict older than the
      last completion is stale — the planner tick (30 min) re-plans on every run
      completion, and paging before it had the chance is a false alarm.
    - Discovery could not create a task either (`discovery.candidate` is the
      read-only form of the gate `planner_tick.sh` runs after an exhausted plan).
    - Not paused: Worker/_state/PAUSED is a human saying "be idle".

    One page per STARVATION_COOLDOWN_S (a day). Returns the message sent, or
    None. Never raises: this runs inside the maintenance tick.
    """
    now = time.time() if now is None else now
    try:
        if (ROOT / "Worker" / "_state" / "PAUSED").exists():
            return None                      # idle on purpose

        from .orchestrator import discovery, tasks as _tasks
        from .orchestrator.assignment import Ledger
        from .orchestrator.orchestrator import _parse_ts

        assignments = list(Ledger(ROOT / "Worker" / "assignments").all())
        parked = {a.task_id for a in assignments if a.state == "awaiting_human"}
        runnable = [t for t in _tasks.load_dispatchable() if t.slug not in parked]
        if runnable:
            return None                      # there is work; silence is not the issue
        in_flight = [a for a in assignments if a.state in _IN_FLIGHT_STATES]
        if in_flight:
            return None                      # something is running; wait for it

        planner = _read_planner_state()
        verdict = str(planner.get("verdict") or "").lower()
        if verdict != "exhausted":
            return None                      # planner may still create work
        planned_at = _parse_ts(planner.get("last_run") or "")
        stamps = [_parse_ts(a.updated_ts) for a in assignments if a.updated_ts]
        last_change = max((s for s in stamps if s), default=0.0)
        if not planned_at or planned_at < last_change:
            return None                      # verdict predates the latest change

        url, why = discovery.candidate(now=now)
        if url is not None:
            return None                      # discovery still has a project to explore

        if now - _read_starvation_state() < STARVATION_COOLDOWN_S:
            return None                      # already paged today

        since = dt.datetime.fromtimestamp(planned_at).strftime("%m-%d %H:%M")
        msg = _compose_starvation(since, why or "-", len(parked))
        kakao_notify(msg)
        _write_starvation_state(now)
        return msg
    except Exception as exc:
        print(f"starvation-check: error — {exc}")
        return None


def _read_planner_state() -> dict:
    try:
        d = json.loads(PLANNER_STATE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_starvation_state() -> float:
    try:
        return float(json.loads(STARVATION_STATE_FILE.read_text(encoding="utf-8"))["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def _write_starvation_state(ts: float) -> None:
    try:
        STARVATION_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STARVATION_STATE_FILE.write_text(json.dumps({"ts": ts}), encoding="utf-8")
    except OSError:
        pass


# Ships with the engine, so it is looked up under _ENGINE_ROOT (c-1).
HEALTH_REPORT_SCRIPT = _ENGINE_ROOT / "scripts" / "health_report.py"
_HEALTH_REPORT: Any = None
_HEALTH_REPORT_LOADED = False


def _health_report() -> Any:
    """`scripts/health_report.py` loaded by path, or None if it will not load.

    The staleness rule and the code-timestamp rule live there and are imported
    here, never re-implemented — one owner, so a fix cannot land on one copy only.

    The dependency is deliberately one-way. launchd runs health_report.py with a
    bare /usr/bin/python3 and no venv, so that module must stay stdlib-only and
    must never import todo_system; loading it by path is what keeps the arrow
    pointing this direction. A failure here is printed, not swallowed: the whole
    point of this code is that a silent regression looks exactly like health.
    """
    global _HEALTH_REPORT, _HEALTH_REPORT_LOADED
    if _HEALTH_REPORT_LOADED:
        return _HEALTH_REPORT
    _HEALTH_REPORT_LOADED = True
    try:
        import importlib.util

        src = HEALTH_REPORT_SCRIPT
        spec = importlib.util.spec_from_file_location("health_report", src)
        if spec is None or spec.loader is None:
            raise ImportError(f"no loader for {src}")
        mod = importlib.util.module_from_spec(spec)
        # Registration must precede exec: @dataclass resolves its stringified
        # (future-annotations) field types via sys.modules[cls.__module__].
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _HEALTH_REPORT = mod
    except Exception as exc:  # noqa: BLE001 - any import failure must not kill the tick
        print(f"daemon-restart: health_report unavailable — {exc}")
        _HEALTH_REPORT = None
    return _HEALTH_REPORT


def code_change_time() -> float | None:
    """Epoch seconds of the last commit touching the runtime code paths, or None
    if that cannot be read.

    The same judgment restart_stale_daemons makes, exposed under a name the
    runner can call: the runner drains itself between runs when the code has
    moved past the commit it started on, so it needs the identical answer from
    the identical owner (scripts/health_report.py — CODE_TIME_PATHS + the
    `git log -1` rule live there and are never re-implemented here).

    Fails open by contract. Every failure mode — health_report will not load, no
    git, an unreadable HEAD, the rule itself raising — returns None, which the
    callers read as "no change". The alternative is a guard that can kill the
    process holding the queue, which is strictly worse than a missed restart.
    """
    try:
        hr = _health_report()
        if hr is None:
            return None
        when = hr.gather_code_time()
        if when is None:
            return None
        return float(when.timestamp())
    except Exception:  # noqa: BLE001 - see the fail-open contract above
        return None


# Daemons that stay resident, so they hold whatever code was on disk when they
# started. Every other job here respawns per tick and picks up changes for free —
# which is exactly why this gap stayed invisible: most of the system self-updates.
RUNNER_LABEL = daemonctl.SECONDBRAIN_PREFIX + "runner.worker-default"
TODO_SYNC_LABEL = daemonctl.SECONDBRAIN_PREFIX + "todo-sync"
DASHBOARD_LABEL = daemonctl.SECONDBRAIN_PREFIX + "dashboard"
LONG_LIVED_LABELS = (RUNNER_LABEL, TODO_SYNC_LABEL, DASHBOARD_LABEL)

# Same switch, same path as orchestrator.py:111 and scripts/planner_tick.sh:35.
# A pause is a person saying stop; it outranks every other signal here.
PAUSED_FILE = WORKER_DIR / "_state" / "PAUSED"

RESTART_STATE_FILE = WORKER_DIR / "_state" / "daemon-restart.json"
# Per stale window, not per lifetime: a daemon that keeps reading as stale after
# three kickstarts has something wrong that another SIGTERM will not fix, and the
# daemon in question is the one that dispatches work.
RESTART_MAX_ATTEMPTS = 3
# The maintenance tick is 300s. A restarted process needs to come up and show a
# new start time in `ps` before the next judgment means anything, so attempts are
# spaced well past one tick to stop a stale reading from firing a second kill.
RESTART_COOLDOWN_S = 15 * 60

# A deferral is not a failure, so it must not spend the restart budget — but that
# is exactly why it can go on forever in silence. Observed 2026-08-18: todo-sync
# had been deferred 169 consecutive times across four days with `attempts` still
# at 0, so the cap above never engaged and nobody was ever told. A gate that can
# only ever say "busy" (see _todo_sync_idle) makes that the steady state, not the
# edge case.
#
# Measured in TIME, not in ticks. The 169 figure is meaningless without knowing
# the launchd interval, and pinning a count here would silently change meaning
# the day that interval is retuned. Thresholds follow record_dirty_stall's
# precedent — notify once past the threshold, then re-nag on a period, because a
# one-shot alert that is missed leaves the system quietly broken for days.
DEFER_NOTIFY_AFTER_S = 6 * 3600
DEFER_RENOTIFY_EVERY_S = 24 * 3600


def restart_stale_daemons(labels: tuple[str, ...] = LONG_LIVED_LABELS,
                          now: float | None = None) -> list[str]:
    """Kickstart resident daemons whose code changed after they started.

    macp modifies its own source, and the thing that executes those tasks is a
    resident process. So a task can fix the runner, merge green, and change
    nothing: on 2026-08-06 the runner was 7 commits behind its own repo — including
    a security fix to the tool whitelist — because nothing restarts it and the
    manual restarts had simply stopped happening. Its verify stage tested the new
    code in the working tree while the daemon dispatching it ran the old.

    Both the "which paths count as code" and the "started before the code" rules
    come from scripts/health_report.py, which the weekly report already uses —
    this stage is the actuator for that judgment, not a second opinion.

    Best-effort: this runs from maintenance, which respawns per tick and therefore
    always carries current code itself.
    """
    restarted: list[str] = []
    now = time.time() if now is None else now
    if PAUSED_FILE.exists():
        print("daemon-restart: paused — Worker/_state/PAUSED present, nothing restarted")
        return restarted
    hr = _health_report()
    if hr is None:
        return restarted                  # the loader already printed the reason
    try:
        code_time = hr.gather_code_time()
        if code_time is None:
            print("daemon-restart: skipped — code timestamp unreadable")
            return restarted
        code_ts = int(code_time.timestamp())
        live = _resident_daemons(labels)
        stale = hr.stale_daemons({lb: started for lb, (_pid, started) in live.items()},
                                 code_time)

        state = _read_restart_state()
        if state is None:
            print("daemon-restart: state file unreadable — refusing to restart "
                  "(fail-closed); delete Worker/_state/daemon-restart.json to reset")
            return restarted
        # A label that is no longer stale did its job. Drop its history so the
        # next stale window starts from a full budget.
        changed = False
        for done in [lb for lb in state if lb not in stale]:
            del state[done]
            changed = True

        for label in stale:
            pid, _started = live[label]
            entry = state.get(label)
            if not isinstance(entry, dict) or entry.get("code_ts") != code_ts:
                # First sighting of this stale window: fresh budget. The three
                # defer_* fields are tracked separately from attempts on purpose
                # — see the deferral branch below.
                entry = {"attempts": 0, "last_ts": 0.0, "code_ts": code_ts,
                         "escalated": False, "deferrals": 0,
                         "first_defer_ts": 0.0, "defer_notified_ts": 0.0}
            try:
                attempts = int(entry.get("attempts", RESTART_MAX_ATTEMPTS))
                last_ts = float(entry.get("last_ts", now))
            except (TypeError, ValueError):
                attempts, last_ts = RESTART_MAX_ATTEMPTS, now   # unreadable = spent

            if attempts >= RESTART_MAX_ATTEMPTS:
                # Restarting the dispatcher in a loop stops the whole system, so
                # the budget is hard. Page once, then stay quiet: an alert that
                # repeats every tick only teaches you to ignore it.
                if not entry.get("escalated"):
                    entry["escalated"] = True
                    state[label] = entry
                    changed = True
                    print(f"daemon-restart: {label} still stale after {attempts} "
                          f"attempts — giving up, escalated")
                    try:
                        kakao_notify(_compose_daemon_restart_escalated(label, attempts))
                    except Exception as exc:  # noqa: BLE001 - print already surfaced it
                        print(f"daemon-restart: escalation notify failed — {exc}")
                continue

            if now - last_ts < RESTART_COOLDOWN_S:
                print(f"daemon-restart: {label} cooling down — "
                      f"{int(RESTART_COOLDOWN_S - (now - last_ts))}s to go")
                continue

            if not _daemon_idle(label, pid):
                # Not a failure — the code is still stale, we just refuse to kill
                # a live run for it. The next tick is five minutes away, and a
                # deferral must not spend the budget or a busy daemon would
                # exhaust its attempts without one restart ever being tried.
                #
                # `attempts` is deliberately NOT touched here. Counting deferrals
                # into the restart budget would let a merely-busy daemon burn all
                # three attempts without one restart being tried, so the streak
                # gets its own fields and its own alert.
                _note_restart_deferral(label, entry, now)
                state[label] = entry
                changed = True
                continue

            entry["attempts"] = attempts + 1
            entry["last_ts"] = now
            # A restart was actually attempted, so the deferral streak is broken.
            entry["deferrals"] = 0
            entry["first_defer_ts"] = 0.0
            entry["defer_notified_ts"] = 0.0
            state[label] = entry
            changed = True
            print(f"daemon-restart: {label} stale — kickstarting "
                  f"(attempt {attempts + 1}/{RESTART_MAX_ATTEMPTS})")
            r = daemonctl.restart(label, timeout=60)
            if r.returncode == 0:
                restarted.append(label)
            else:
                print(f"daemon-restart: {label} failed — {(r.stderr or '').strip()[:120]}")
        if changed:
            _write_restart_state(state)
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        print(f"daemon-restart: error — {exc}")
    return restarted


def _note_restart_deferral(label: str, entry: dict, now: float) -> None:
    """Record one deferral on `entry` in place, and page if the streak is old.

    Split out of restart_stale_daemons because the counters it maintains are
    deliberately NOT the restart budget: a deferral means "not now", a failed
    attempt means "broken". Merging them would let a healthy busy daemon spend
    its three attempts on days when it was simply working (criterion 5).

    The alert is keyed on elapsed time rather than a count of deferrals — the
    launchd interval is a tunable, so a count would quietly change meaning the
    day it moves. Notify once past DEFER_NOTIFY_AFTER_S, then re-nag every
    DEFER_RENOTIFY_EVERY_S, following record_dirty_stall: one alert that gets
    missed is how 169 deferrals went unseen for four days.
    """
    try:
        deferrals = int(entry.get("deferrals", 0))
        first_ts = float(entry.get("first_defer_ts", 0.0))
        notified_ts = float(entry.get("defer_notified_ts", 0.0))
    except (TypeError, ValueError):
        # Unreadable counters must not silence the alert, and must not fake one
        # either: restart the streak from this tick.
        deferrals, first_ts, notified_ts = 0, 0.0, 0.0

    deferrals += 1
    if first_ts <= 0:
        first_ts = now
    stalled_s = max(0.0, now - first_ts)

    entry["deferrals"] = deferrals
    entry["first_defer_ts"] = first_ts

    print(f"daemon-restart: {label} busy — deferred "
          f"({deferrals}x over {int(stalled_s // 60)}m)")

    if stalled_s < DEFER_NOTIFY_AFTER_S:
        return
    if notified_ts > 0 and now - notified_ts < DEFER_RENOTIFY_EVERY_S:
        return

    entry["defer_notified_ts"] = now
    try:
        kakao_notify(_compose_daemon_restart_deferred(label, stalled_s, deferrals))
    except Exception as exc:  # noqa: BLE001 - print already surfaced it
        print(f"daemon-restart: deferral notify failed — {exc}")


def _read_restart_state() -> dict | None:
    """Per-label restart bookkeeping, or None when it cannot be trusted.

    None is fail-closed and callers must not restart on it. A counter that reads
    zero because the file is corrupt would re-SIGTERM the dispatcher every tick
    forever — which is the outage this cap exists to prevent, arrived at by a
    different road.
    """
    if not RESTART_STATE_FILE.exists():
        return {}
    try:
        data = json.loads(RESTART_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_restart_state(state: dict) -> None:
    try:
        RESTART_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        RESTART_STATE_FILE.write_text(
            json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8")
    except (OSError, TypeError):
        pass


def _resident_daemons(labels: tuple[str, ...]) -> dict[str, tuple[int, dt.datetime]]:
    """label -> (pid, process start time) for the labels holding a live PID now.

    The restart target set is exactly `labels` ∩ resident. Interval jobs
    (orchestrator, brief, health, macp-monitor) re-exec per tick and therefore
    already run current code, so they are never candidates — an unreadable start
    time is treated the same way, as not-at-risk, because restarting on a guess
    is worse than missing one window.
    """
    live: dict[str, tuple[int, dt.datetime]] = {}
    for label in labels:
        pid = _daemon_pid(label)
        if pid is None:
            continue              # not resident right now; it will start fresh
        started = _process_start_ts(pid)
        if started is None:
            continue
        live[label] = (pid, dt.datetime.fromtimestamp(started, tz=dt.timezone.utc))
    return live


def _pgrep(*args: str) -> str | None:
    """stdout of `pgrep`, or None when the answer is unknown.

    rc=1 is pgrep's "no match" — a real answer, and the one that means idle.
    Anything else (rc≥2, OSError, timeout) is an absence of information.
    """
    try:
        r = subprocess.run(["pgrep", *args], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode in (0, 1) else None


def _runner_idle(pid: int) -> bool:
    """Idle iff the tick heartbeat, the ledger's job flag and the process table
    all say nothing is running.

    `kickstart -k` sends SIGTERM, so a wrong "idle" here does not delay a run, it
    kills one mid-stage. Hence the conservative AND, and hence every unreadable
    signal resolving to busy: missing a restart window costs five minutes, and the
    next tick comes around anyway.

    What this gate deliberately does NOT read is the ledger's in-flight STATE
    (dispatched/accepted/in_progress). Those rows record what the orchestrator
    DECIDED, not what is executing. While the usage guard holds, run_forever
    polls with `only_bypass=True` and leaves every non-bypass envelope queued
    and un-acked — so a `dispatched` row sits there for DAYS with no process
    behind it. On 2026-08-21 that read as busy and deferred the runner restart
    73 times in 6 hours (moksori-2), i.e. the same can-never-restart failure
    _todo_sync_idle had. Each signal kept below answers something the state
    field cannot:

    - The tick heartbeat: is a stage executing RIGHT NOW? The runner writes it
      before `handle()` and re-touches it per stage; between ticks it is absent.
      Same four branches as _todo_sync_idle, for the same reasons: absent →
      idle, fresh → busy, older than RUNNER_HEARTBEAT_STALE_S → a corpse from a
      tick that died mid-pipeline and not work (reading a corpse as busy would
      rebuild the constant-False defect from another road), unreadable → busy.
    - `job_running`: a detached long_job OUTLIVES the tick that spawned it, so
      it has neither a heartbeat nor a `claude -p` — the ledger is its only
      witness, which is why this one row flag survives the cut.
    - A live `claude -p`: covers what the heartbeat misses — a stage spawned
      outside the runner loop, or a heartbeat write that silently failed.

    Residual window: between the orchestrator writing `dispatched` and the
    runner writing the heartbeat there is up to one poll interval (15s) where
    this answers idle. A SIGTERM landing in it kills the runner BEFORE the stage
    starts, and the envelope is still queued un-acked, so the restarted runner
    picks it straight back up — the benign half of at-least-once delivery, not
    a mid-stage kill.
    """
    try:
        from .orchestrator.assignment import Ledger

        root = WORKER_DIR / "assignments"
        rows = Ledger(root).all()
        # Ledger.all() SKIPS rows it cannot parse, and a silently dropped row is
        # indistinguishable from "no work". If any file did not come back, we do
        # not know what is in flight — and the one that failed to parse could be
        # the running one.
        if root.exists() and len(rows) != len(list(root.glob("*.json"))):
            return False
        for a in rows:
            # A DONE row's flag is a leftover nothing will ever reset (the
            # orchestrator closed it without a runner reply); honouring it
            # deferred the restart 63x in 6h on 2026-09-26. Every other state
            # (FAILED-deferred, re-DISPATCHED) can still front a live long_job.
            if a.job_running and a.state != "done":
                return False
    except Exception:  # noqa: BLE001 - an unreadable ledger cannot authorise a SIGTERM
        return False
    try:
        mtime = RUNNER_HEARTBEAT_PATH.stat().st_mtime
    except FileNotFoundError:
        pass                     # between ticks — the safe kickstart window
    except OSError:
        return False             # "I cannot tell" must not authorise a SIGTERM
    else:
        if (time.time() - mtime) <= RUNNER_HEARTBEAT_STALE_S:
            return False         # a stage is mid-flight
    out = _pgrep("-fl", "claude -p")
    return out is not None and not out.strip()


def _todo_sync_idle(pid: int) -> bool:
    """Idle iff the tick heartbeat says no sync is mid-write.

    History: until 2026-08-21 this gate read the process tree and was the
    constant False — launchd's pid is the `uv` WRAPPER whose python child is
    permanently resident, so `pgrep -P` always answered. todo-sync was deferred
    169x over four days (2026-08-18), then 359x over 30 hours (2026-08-21),
    and could never be restarted automatically. The process tree carries no
    usable signal for this daemon shape (see git history of this docstring for
    the full argument), so the tick itself reports:

    - cli.cmd_daemon writes TODO_SYNC_HEARTBEAT_PATH on tick entry and unlinks
      it on tick exit. Between ticks (the `time.sleep(interval)` steady state)
      the file is absent → idle → safe to kickstart.
    - Present and fresh → a sync is mid-write → busy. Wrong-idle here SIGTERMs
      a sqlite write, so presence wins whenever it plausibly means work.
    - Present but older than TODO_SYNC_HEARTBEAT_STALE_S → a corpse from a tick
      that died mid-write, not work (a tick is seconds). Reading a corpse as
      busy would rebuild the constant-False defect from another road, so the
      cutoff treats it as idle. The subsequent kickstart also clears the state:
      the daemon restarts and the next completed tick removes the file.
    - Unreadable (OSError other than absence) → busy. "I cannot tell" must not
      authorise a SIGTERM, matching _runner_idle's fail-closed convention.

    `pid` is unused but kept: IDLE_GATES callers pass it uniformly, and a
    per-label signature fork would push complexity into _daemon_idle for
    nothing.
    """
    del pid  # the heartbeat, not the process tree, is the signal
    try:
        mtime = TODO_SYNC_HEARTBEAT_PATH.stat().st_mtime
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return (time.time() - mtime) > TODO_SYNC_HEARTBEAT_STALE_S


def _dashboard_idle(pid: int) -> bool:
    """Always idle. scripts/macp_dashboard.py is a stateless, read-only HTTP
    server: it holds no in-flight write, so a SIGTERM only costs the ~3s polling
    gap until launchd/systemd relaunches it. It still needs an explicit gate
    because _daemon_idle fails closed — a LONG_LIVED_LABELS entry with no
    IDLE_GATES entry returns False on every tick and defers forever, never
    restarting (the exact gap this label was added to close).

    `pid` is unused but kept for the uniform IDLE_GATES gate(pid) call
    convention, matching _todo_sync_idle.
    """
    del pid  # a stateless server is idle by construction; nothing to probe
    return True


# A restart is only ever authorised by an explicit per-label idle test. A label
# with no entry here is never restarted: "I do not know what this daemon is
# doing" must not be grounds for sending it a SIGTERM.
IDLE_GATES = {RUNNER_LABEL: _runner_idle, TODO_SYNC_LABEL: _todo_sync_idle,
              DASHBOARD_LABEL: _dashboard_idle}


def _daemon_idle(label: str, pid: int) -> bool:
    gate = IDLE_GATES.get(label)
    if gate is None:
        return False
    try:
        return bool(gate(pid))
    except Exception as exc:  # noqa: BLE001 - never let a probe kill the tick
        print(f"daemon-restart: {label} idle check failed — {exc}")
        return False


def _daemon_pid(label: str) -> int | None:
    """Live PID behind a daemon label — launchd on the Mac, systemd --user on the
    cloud HQ (todo_system.daemonctl)."""
    return daemonctl.main_pid(label)


def _process_start_ts(pid: int) -> int | None:
    """Unix start time of `pid`, via ps. None when it cannot be read."""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        if not out:
            return None
        return int(dt.datetime.strptime(out, "%a %b %d %H:%M:%S %Y").timestamp())
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


LOG_ROTATE_MAX_BYTES = 32 * 1024 * 1024
LOG_ROTATE_KEEP_BYTES = 4 * 1024 * 1024


def rotate_logs(log_dir: Path | None = None,
                max_bytes: int = LOG_ROTATE_MAX_BYTES,
                keep_bytes: int = LOG_ROTATE_KEEP_BYTES) -> list[str]:
    """Cap the daemon logs, which nothing was bounding (todo-sync.out.log reached
    35 MB, worker.out.log 20 MB). Returns the names rotated.

    COPY-then-TRUNCATE, not rename: launchd holds these files open via
    StandardOutPath, so a renamed file keeps receiving writes to the moved inode
    and the fresh one stays empty forever. Truncating in place keeps the inode,
    and launchd's descriptor is O_APPEND, so the next write lands at the new
    end-of-file rather than leaving a multi-megabyte hole.

    The tail is what has diagnostic value, so the last `keep_bytes` are carried
    into `<name>.1` and the rest is dropped — an archive that doubles the disk
    footprint would defeat the purpose.
    """
    d = Path(log_dir) if log_dir else ROOT / "logs"
    rotated: list[str] = []
    if not d.is_dir():
        return rotated
    for p in sorted(d.glob("*.log")):
        try:
            if p.stat().st_size <= max_bytes:
                continue
            with p.open("rb") as f:
                f.seek(-keep_bytes, os.SEEK_END)
                f.readline()           # drop the partial line at the cut point
                tail = f.read()
            p.with_suffix(".log.1").write_bytes(tail)
            os.truncate(p, 0)
            rotated.append(p.name)
        except OSError as exc:
            print(f"log-rotate: {p.name} — {exc}")
    return rotated


def vault_is_dirty_outside_worker() -> tuple[bool, list[str]]:
    """Return (dirty, file_list). file_list contains paths outside the
    allowed prefixes.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        # No git or no HEAD; treat as clean (best-effort).
        return False, []

    files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    # Also include untracked files via --others (so a half-written file blocks us).
    try:
        result_untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        files.extend(
            line.strip()
            for line in result_untracked.stdout.splitlines()
            if line.strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    bad = [
        f
        for f in files
        if not f.startswith(ALLOWED_DIRTY_PREFIXES) and f not in ALLOWED_DIRTY_EXACT
    ]
    return bool(bad), bad


# Stall marker: the dirty-check is a safety guard (stages may `git add -A`, so
# the tree must be clean to avoid sweeping user WIP into worker commits). But a
# single stray uncommitted file in a vault the user edits daily would otherwise
# stall the whole pipeline silently and indefinitely. We persist a marker so the
# stall is observable, and alert after STALL_NOTIFY_THRESHOLD consecutive aborts
# (~ threshold × launchd interval) so the user finds out the same hour. A
# one-shot alert is not enough: if it is missed, the pipeline stays silently
# down for days (observed 2026-06-17: 358 consecutive aborts, 1 alert). So we
# RE-NAG every STALL_RENOTIFY_EVERY aborts until the tree is cleaned.
STALL_MARKER = _STATE_DIR / "stall.json"
STALL_NOTIFY_THRESHOLD = 3
STALL_RENOTIFY_EVERY = 144  # ~12h at the 300s launchd interval


def record_dirty_stall(bad_files: list[str]) -> None:
    """Persist/refresh the dirty-stall marker and re-nag periodically until clean."""
    try:
        prev = json.loads(STALL_MARKER.read_text()) if STALL_MARKER.exists() else {}
    except (OSError, ValueError):
        prev = {}

    count = int(prev.get("consecutive_count", 0)) + 1
    last_notified = int(prev.get("last_notified_count", 0))
    marker = {
        "first_seen": prev.get("first_seen") or now_iso(),
        "last_seen": now_iso(),
        "consecutive_count": count,
        "files": bad_files,
        "notified": bool(prev.get("notified", False)),
        "last_notified_count": last_notified,
    }

    # Notify at the threshold, then re-nag every STALL_RENOTIFY_EVERY aborts so a
    # missed first alert can never leave the pipeline silently stalled for days.
    should_notify = count >= STALL_NOTIFY_THRESHOLD and (
        last_notified == 0 or count - last_notified >= STALL_RENOTIFY_EVERY
    )
    if should_notify:
        kakao_notify(_compose_worker_stall(count, bad_files))
        marker["notified"] = True
        marker["last_notified_count"] = count

    try:
        STALL_MARKER.write_text(json.dumps(marker, ensure_ascii=False, indent=2))
    except OSError:
        pass


def clear_dirty_stall() -> None:
    """Remove the stall marker once the vault is clean again (self-recovery)."""
    if not STALL_MARKER.exists():
        return
    try:
        prev = json.loads(STALL_MARKER.read_text())
    except (OSError, ValueError):
        prev = {}
    if prev.get("notified"):
        # Digest tier: the recovery already happened — nothing to do.
        kakao_notify("✅ Worker 재개: vault 정리됨, 파이프라인 정상 가동.",
                     level=LEVEL_DIGEST, event_key="worker-stall-resume")
    try:
        STALL_MARKER.unlink()
    except OSError:
        pass


def ensure_vault_branch_fresh(branch: str) -> tuple[bool, str]:
    """Ensure vault is on a fresh worker branch based on main.

    If the worker branch already exists (stale from a previous run), delete it
    and recreate from main HEAD. This prevents stale branches from being reused.

    Returns (ok, message).

    Every git call goes through `_prepare_git`, so the whole function is bounded
    at `300 (pull) + 180 x 5 (local) = 1200s` instead of being able to occupy a
    runner slot indefinitely on a wedged remote or a stuck index. The
    `GIT_TIMEOUT_*_S` constants live further down this file, next to
    `ensure_external_repo_ready`, which is fine because Python resolves globals
    at *call* time — that also keeps the tests' `monkeypatch.setattr(worker,
    "GIT_TIMEOUT_*", 1)` working.

    Timeout (`rc == GIT_TIMEOUT_RC`) is a fatal return on every call here, and
    each one says so in a string distinct from its ordinary-failure sibling, so
    the runner log tells "wedged" apart from "genuinely failed" after the fact.
    """
    # Check current branch
    current_branch_result = _prepare_git(
        ["git", "branch", "--show-current"],
        cwd=ROOT,
        timeout=GIT_TIMEOUT_LOCAL_S,
    )
    # A wedged ref read stops us here rather than reporting an empty stdout as
    # "not on main": if a trivial ref read is stuck, the five calls behind it are
    # very likely stuck too, and failing now saves ~20 minutes of ceilings.
    if current_branch_result.returncode == GIT_TIMEOUT_RC:
        return (False,
                f"branch --show-current timed out after {GIT_TIMEOUT_LOCAL_S}s "
                f"(wedged index/hook?)")
    # Any other non-zero rc keeps the old behaviour: read stdout and carry on.
    current_branch = current_branch_result.stdout.strip()

    # If not on main, checkout main first
    if current_branch != "main":
        checkout_main = _prepare_git(
            ["git", "checkout", "main"],
            cwd=ROOT,
            timeout=GIT_TIMEOUT_LOCAL_S,
        )
        if checkout_main.returncode == GIT_TIMEOUT_RC:
            return (False,
                    f"checkout main timed out after {GIT_TIMEOUT_LOCAL_S}s "
                    f"(wedged index/hook?): {checkout_main.stderr[:200]}")
        if checkout_main.returncode != 0:
            return False, f"checkout main failed: {checkout_main.stderr[:300]}"

    # Pull latest main (non-fatal if offline)
    pull_result = _prepare_git(
        ["git", "pull", "--ff-only", "origin", "main"],
        cwd=ROOT,
        timeout=GIT_TIMEOUT_PULL_S,
    )
    if pull_result.returncode == GIT_TIMEOUT_RC:
        # Asymmetric with `ensure_external_repo_ready`, where a pull timeout is
        # only a warning, and deliberately so: the vault is the tick's OWN live
        # checkout, so cutting a worker branch off a stale main contaminates the
        # work itself. An external checkout is a throwaway cache, which is why it
        # can afford to carry on with a stale tree.
        return (False,
                f"pull main timed out after {GIT_TIMEOUT_PULL_S}s (hung remote?): "
                f"{pull_result.stderr[:200]}")
    elif pull_result.returncode != 0:
        # Unchanged contract: an ordinary offline failure warns and carries on.
        # Making this fatal would kill every vault task on a host that cannot
        # reach origin.
        print(f"  [warn] pull main failed (offline?): {pull_result.stderr[:100]}")

    # Check if worker branch already exists
    branch_exists = _prepare_git(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=ROOT,
        timeout=GIT_TIMEOUT_LOCAL_S,
    )
    # Only the timeout is an error here. `rev-parse --verify --quiet` answers
    # rc=1 for a missing branch, which is the normal first-run case, so a
    # `rc != 0` test would fail every task. Reading a wedged rev-parse as
    # "absent" instead skips the delete below and resurfaces as a confusing
    # `branch create failed: ... already exists`.
    if branch_exists.returncode == GIT_TIMEOUT_RC:
        return (False,
                f"branch existence check timed out after {GIT_TIMEOUT_LOCAL_S}s "
                f"(wedged index/hook?)")

    # Delete stale worker branch if it exists
    if branch_exists.returncode == 0:
        del_result = _prepare_git(
            ["git", "branch", "-D", branch],
            cwd=ROOT,
            timeout=GIT_TIMEOUT_LOCAL_S,
        )
        if del_result.returncode == GIT_TIMEOUT_RC:
            return (False,
                    f"branch delete timed out after {GIT_TIMEOUT_LOCAL_S}s "
                    f"(wedged index/hook?): {del_result.stderr[:200]}")
        if del_result.returncode != 0:
            return False, f"branch delete failed: {del_result.stderr[:300]}"

    # Create fresh worker branch from main HEAD
    create_result = _prepare_git(
        ["git", "checkout", "-b", branch],
        cwd=ROOT,
        timeout=GIT_TIMEOUT_LOCAL_S,
    )
    if create_result.returncode == GIT_TIMEOUT_RC:
        return (False,
                f"branch create timed out after {GIT_TIMEOUT_LOCAL_S}s "
                f"(wedged index/hook?): {create_result.stderr[:200]}")
    if create_result.returncode != 0:
        return False, f"branch create failed: {create_result.stderr[:300]}"

    return True, f"fresh branch {branch} created from main"


# ---------------------------------------------------------------------------
# External repo support (M4-c)
# ---------------------------------------------------------------------------


DEFAULT_EXTERNAL_BASE = Path.home() / "Worker"


def resolve_working_dir(task: WorkerTask) -> Path:
    """Compute the absolute working_dir for an external repo task.

    Order:
    1. ``working_dir`` field if set (expand ~).
    2. Default: ``~/Worker/<repo-name>`` (derived from ``repo`` URL).
    """
    if task.working_dir:
        return Path(task.working_dir).expanduser().resolve()
    if task.repo is None:
        raise ValueError(f"task {task.slug} has no repo")
    # Derive repo name from URL: git@host:user/name.git or https://host/user/name.git
    repo = task.repo.rstrip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    name = repo.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return (DEFAULT_EXTERNAL_BASE / name).resolve()


# Wall-clock ceilings for the git calls in `ensure_external_repo_ready` (the
# "prepare_branch" step). On 2026-09-25 the runner's cgroup went over
# `memory.high` and `git-remote-https` sat for 18~72 minutes on calls that
# normally take under a second; nothing here bounded a subprocess, so runner
# slots stayed occupied by threads that would never return while the dashboard
# showed idle. `stages.py` got the same treatment (PR #206) for its own git
# calls; this function is the other unbounded network path.
#
# Values are graded per call, not uniform: the task body asks for "clone/pull
# generous, set-head short".
#
# WORST-CASE ELAPSED for this function, with every call wedged:
#   existing checkout: 300 (fetch) + 60 (set-head) + 300 (pull) = 660s
#   fresh clone too:   660 + 600 (clone)                        = 1260s
# plus the local ceilings below in the pathological case. Compare with the
# 18~72 minutes actually observed on 2026-09-25 — that is the point of these
# numbers. There is deliberately no whole-function deadline: a legitimately
# slow large clone must not be killed by one.
GIT_TIMEOUT_CLONE_S = 600       # generous — a fresh Android clone (DripCoffee,
                                # Pairent) takes minutes over https, and it is
                                # the heaviest network call in this function
GIT_TIMEOUT_FETCH_S = 300       # a day's delta fetch; same grade as
                                # stages.GIT_TIMEOUT_NETWORK_S
GIT_TIMEOUT_SETHEAD_S = 60      # short — one ref lookup, normally ~0.6s
                                # (stages.py:556-561's measured comment), so
                                # this is 100x headroom
GIT_TIMEOUT_PULL_S = 300        # generous, same grade as fetch
GIT_TIMEOUT_LOCAL_S = 180       # symbolic-ref / rev-parse / checkout / branch -D;
                                # same value as stages.GIT_TIMEOUT_LOCAL_S
GIT_TIMEOUT_RC = _stages.GIT_TIMEOUT_RC   # 124, coreutils timeout(1)'s
                                          # convention — one definition, not two


def _prepare_git(args: list[str], *, cwd: Path | None = None,
                 timeout: float) -> subprocess.CompletedProcess:
    """`subprocess.run(args)` where exceeding `timeout` is a return value.

    A `TimeoutExpired` must NOT escape to the caller: `ensure_external_repo_ready`
    is called from the worker tick, so a hung remote would become a tick crash
    instead of the ordinary non-zero-rc failure every call site here already
    handles. Each call site keeps judging git by `returncode`, and `GIT_TIMEOUT_RC`
    flows into its existing non-zero branch.

    `FileNotFoundError` (git itself missing) is deliberately *not* caught — the
    clone call site already turns that into its own failure string.
    """
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        partial = _stages._git_stream(exc.stderr)
        if partial and not partial.endswith("\n"):
            partial += "\n"
        return subprocess.CompletedProcess(
            args=args, returncode=GIT_TIMEOUT_RC,
            stdout=_stages._git_stream(exc.stdout),
            stderr=partial + f"git timeout after {timeout}s: {' '.join(args)}\n")


def ensure_external_repo_ready(task: WorkerTask) -> tuple[bool, str, Path | None]:
    """Clone if needed, then check out worker_branch (creating if absent).

    Returns (ok, message, working_dir). On failure, working_dir may be None.

    Every git call below goes through `_prepare_git`, so the whole function is
    bounded by the sum of the `GIT_TIMEOUT_*_S` ceilings above (660s for an
    existing checkout, 1260s if the clone hangs too) rather than being able to
    occupy a runner slot indefinitely on a wedged remote.
    """
    if not task.is_external_repo:
        return True, "not external", None

    try:
        wd = resolve_working_dir(task)
    except ValueError as exc:
        return False, str(exc), None

    wd.parent.mkdir(parents=True, exist_ok=True)

    # Step 1: clone if working_dir is missing or not a git checkout.
    git_dir = wd / ".git"
    if not git_dir.exists():
        try:
            clone = _prepare_git(
                ["git", "clone", str(task.repo), str(wd)],
                timeout=GIT_TIMEOUT_CLONE_S,
            )
        except FileNotFoundError as exc:
            return False, f"clone failed: {str(exc)[:300]}", wd
        if clone.returncode == GIT_TIMEOUT_RC:
            # Deliberately a DIFFERENT string from "clone failed": a wedged
            # remote and a bad URL / auth error need to be told apart in the
            # runner log after the fact (2026-09-25).
            return (False,
                    f"clone timeout after {GIT_TIMEOUT_CLONE_S}s (hung remote?): "
                    f"{clone.stderr[:200]}", wd)
        if clone.returncode != 0:
            return False, f"clone failed: {clone.stderr[:300]}", wd

    # Step 2: fetch latest from origin (non-fatal if offline).
    fetch_result = _prepare_git(
        ["git", "fetch", "--quiet", "origin"],
        cwd=wd,
        timeout=GIT_TIMEOUT_FETCH_S,
    )
    if fetch_result.returncode == GIT_TIMEOUT_RC:
        print(f"  [warn] fetch timeout after {GIT_TIMEOUT_FETCH_S}s (hung remote?)")
    # Refresh the local origin/HEAD pointer so a changed remote default branch
    # propagates (plain fetch does not update it). Non-fatal if offline.
    sethead_result = _prepare_git(
        ["git", "remote", "set-head", "origin", "-a"],
        cwd=wd,
        timeout=GIT_TIMEOUT_SETHEAD_S,
    )
    if sethead_result.returncode == GIT_TIMEOUT_RC:
        print(f"  [warn] remote set-head timeout after {GIT_TIMEOUT_SETHEAD_S}s (hung remote?)")

    # Step 3: determine default branch (main or master).
    branch = task.worker_branch
    default_branch = "main"
    # The remaining calls are local-only, but a cgroup under memory pressure or a
    # stalled filesystem can wedge those too, so they get the local ceiling. None
    # of the judgements below change: rc=124 simply flows into each call's
    # existing non-zero branch (symbolic-ref -> main/master fallback,
    # checkout/branch -> the fatal returns).
    sym_result = _prepare_git(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        cwd=wd,
        timeout=GIT_TIMEOUT_LOCAL_S,
    )
    if sym_result.returncode == 0:
        # e.g. "refs/remotes/origin/main" → "main".
        # Strip the full "refs/remotes/origin/" prefix rather than rsplit on
        # the last "/", so slashed default branches like
        # "claude/coffee-recipe-timer-app-Uoevu" survive intact.
        ref = sym_result.stdout.strip()
        prefix = "refs/remotes/origin/"
        default_branch = ref[len(prefix):] if ref.startswith(prefix) else ref.rsplit("/", 1)[-1]
    else:
        # Fallback: check if "main" exists, otherwise "master"
        main_check = _prepare_git(
            ["git", "rev-parse", "--verify", "--quiet", "refs/heads/main"],
            cwd=wd,
            timeout=GIT_TIMEOUT_LOCAL_S,
        )
        if main_check.returncode != 0:
            default_branch = "master"

    # Step 4: always create fresh worker branch from default branch HEAD.
    # If worker branch already exists (stale from a previous run), delete it first.
    branch_exists = _prepare_git(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=wd,
        timeout=GIT_TIMEOUT_LOCAL_S,
    )

    # Checkout default branch and pull latest.
    checkout_default = _prepare_git(
        ["git", "checkout", default_branch],
        cwd=wd,
        timeout=GIT_TIMEOUT_LOCAL_S,
    )
    if checkout_default.returncode != 0:
        return False, f"checkout {default_branch} failed: {checkout_default.stderr[:300]}", wd

    pull_result = _prepare_git(
        ["git", "pull", "--ff-only", "origin", default_branch],
        cwd=wd,
        timeout=GIT_TIMEOUT_PULL_S,
    )
    # pull failure is non-fatal (offline scenario), but log it
    if pull_result.returncode == GIT_TIMEOUT_RC:
        print(f"  [warn] pull {default_branch} timeout after {GIT_TIMEOUT_PULL_S}s "
              f"(hung remote?): {pull_result.stderr[:100]}")
    elif pull_result.returncode != 0:
        print(f"  [warn] pull {default_branch} failed (offline?): {pull_result.stderr[:100]}")

    # Delete stale worker branch if it exists.
    if branch_exists.returncode == 0:
        del_result = _prepare_git(
            ["git", "branch", "-D", branch],
            cwd=wd,
            timeout=GIT_TIMEOUT_LOCAL_S,
        )
        if del_result.returncode != 0:
            return False, f"branch delete failed: {del_result.stderr[:300]}", wd

    # Create fresh worker branch from default branch HEAD.
    cb = _prepare_git(
        ["git", "checkout", "-b", branch],
        cwd=wd,
        timeout=GIT_TIMEOUT_LOCAL_S,
    )
    if cb.returncode != 0:
        return False, f"branch create failed: {cb.stderr[:300]}", wd

    return True, f"ready at {wd} on {branch} (fresh from {default_branch})", wd


# ---------------------------------------------------------------------------
# Stage invocation
# ---------------------------------------------------------------------------


def strip_frontmatter(text: str) -> str:
    """Strip a leading YAML frontmatter block from a slash-command file."""
    if not text.startswith("---\n"):
        return text
    parts = text.split("\n---\n", 1)
    if len(parts) != 2:
        return text
    return parts[1].lstrip("\n")


def load_stage_prompt(stage: str) -> str:
    path = COMMANDS_DIR / f"worker-{stage}.md"
    text = path.read_text(encoding="utf-8")
    return strip_frontmatter(text)


def build_stage_command(
    stage: str,
    task: WorkerTask,
    run_id: str,
    run_dir: Path,
    dry_run: bool,
) -> tuple[list[str], str]:
    """Return (argv, full_prompt). argv begins with `claude`."""
    base_prompt = load_stage_prompt(stage)
    branch = task.worker_branch

    metadata_lines = [
        "\n\n---\n\n",
        "# Run metadata (injected by orchestrator)\n\n",
        f"- run_id: `{run_id}`\n",
        f"- slug: `{task.slug}`\n",
        f"- task_path: `{task.rel_path}`\n",
        f"- run_dir: `{run_dir.relative_to(ROOT)}`\n",
        f"- branch: `{branch}`\n",
        f"- vault_root: `{ROOT}`\n",
        f"- stage: `{stage}`\n",
        f"- auto_merge: `{task.auto_merge}`\n",
    ]
    if task.is_external_repo:
        wd = resolve_working_dir(task)
        metadata_lines.append(f"- repo: `{task.repo}`\n")
        metadata_lines.append(f"- working_dir: `{wd}`\n")
        metadata_lines.append(
            "- **External repo task**: Implement / Verify operate on files under "
            "`working_dir`. Use absolute paths for any edits. Vault artifacts "
            "(prior stage outputs at `run_dir`) are read-only references.\n"
        )
    metadata = "".join(metadata_lines)
    full_prompt = base_prompt + metadata

    # Per-stage tool whitelist.
    if stage == "research":
        tools = DEFAULT_RESEARCH_TOOLS
    elif stage == "plan":
        tools = DEFAULT_PLAN_TOOLS
    elif stage == "implement":
        tools = task.allowed_tools_implement
    elif stage == "verify":
        tools = DEFAULT_VERIFY_TOOLS
    elif stage == "report":
        tools = DEFAULT_REPORT_TOOLS
    elif stage == "delegate":
        tools = task.allowed_tools_delegate
    else:
        raise ValueError(f"unknown stage: {stage}")

    max_turns = task.max_turns
    if max_turns is None:
        max_turns = STAGE_MAX_TURNS.get(stage, DEFAULT_STAGE_MAX_TURNS)

    argv: list[str] = [
        "claude",
        "-p",
        full_prompt,
        "--max-turns",
        str(max_turns),
        "--allowed-tools",
        *tools.split(),
    ]
    # Allow worker stages to operate on external side-project repos under
    # ~/Worker (e.g. LucidCue, DripCoffee, Pairent). Vault root is the
    # invocation cwd; this --add-dir extends accessible filesystem scope.
    worker_extra_dir = Path.home() / "Worker"
    if worker_extra_dir.exists():
        argv += ["--add-dir", str(worker_extra_dir)]
    return argv, full_prompt


# Substrings Claude Code / the Anthropic API use for capacity conditions
# (account usage-limit reached, API rate limiting, model overloaded) — these
# are transient/time-based, not a bug in the task, so callers should retry
# rather than give up. Checked case-insensitively against stdout/stderr tail.
_CAPACITY_ERROR_MARKERS = (
    "usage limit",
    "rate_limit_error",
    "rate limit",
    "overloaded_error",
    "429 ",
    "out of extra usage",
    "out of usage",
    # Claude Code subscription message: "You've hit your session limit ·
    # resets 4:10am" — time-bound like the others (observed 2026-07-17).
    "session limit",
)


def detect_capacity_error(text: str) -> str | None:
    """Return a short reason if `text` looks like a Claude usage/rate-limit hit.

    None if no known marker is present.
    """
    lowered = text.lower()
    for marker in _CAPACITY_ERROR_MARKERS:
        if marker in lowered:
            return marker.strip()
    return None


def parse_verify_status(verify_md: Path) -> str | None:
    """Return the `overall_status` (pass/fail/skip) from a verify.md frontmatter.

    Returns the lowercased status, or None if it can't be found. Scans only the
    leading `---` frontmatter block to avoid matching prose in the body.
    """
    try:
        text = verify_md.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"\s*overall_status\s*:\s*([A-Za-z]+)", line)
        if m:
            return m.group(1).strip().lower()
    return None


def check_forbidden_tools(tools: str) -> str | None:
    """Return error string if tools whitelist contains forbidden substrings."""
    for pattern in FORBIDDEN_TOOL_SUBSTRINGS:
        if pattern in tools:
            return f"forbidden tool pattern '{pattern}' in allowed_tools='{tools}'"
    return None


def check_task_guardrails(task: WorkerTask) -> str | None:
    """Return error string if task frontmatter violates guardrails.

    auto_merge is no longer outright forbidden — it's an opt-in flag handled
    by the Report stage. The merge gate is the Verify stage's local-check verdict
    (verify.md `overall_status: pass`), enforced inside the Report stage prompt.
    """
    return None


def invoke_stage(
    stage: str,
    task: WorkerTask,
    run_id: str,
    run_dir: Path,
    dry_run: bool,
) -> tuple[bool, str]:
    """Run one stage. Returns (success, message).

    success = True iff the stage's expected artifact exists after the call.
    """
    argv, _prompt = build_stage_command(stage, task, run_id, run_dir, dry_run)
    artifact = run_dir / STAGE_ARTIFACT[stage]

    # Extract the tools whitelist from argv (positions after --allowed-tools).
    try:
        tools_idx = argv.index("--allowed-tools")
        tools_used = " ".join(
            a for a in argv[tools_idx + 1:] if not a.startswith("--")
        )
    except ValueError:
        tools_used = ""
    err = check_forbidden_tools(tools_used)
    if err:
        return False, f"guardrail: {err}"

    if dry_run:
        # Print a concise representation; don't dump the full prompt body.
        preview = " ".join(argv[:1] + ["-p", "<prompt>"] + argv[3:])
        print(f"  [dry-run] would invoke: {preview}")
        print(f"  [dry-run] expected artifact: {artifact.relative_to(ROOT)}")
        return True, "dry-run"

    if not shutil.which("claude"):
        return False, "claude CLI not found on PATH"

    log_path = run_dir / f"{stage}.stdout.log"
    err_path = run_dir / f"{stage}.stderr.log"
    try:
        with log_path.open("w", encoding="utf-8") as out_f, err_path.open(
            "w", encoding="utf-8"
        ) as err_f:
            subprocess.run(
                argv,
                cwd=ROOT,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=out_f,
                stderr=err_f,
                # 장수 automation token이 있으면 주입 — Keychain 리프레시 토큰의
                # 절대만료와 독립적으로 헤드리스 인증을 유지한다. 파일이 없으면
                # os.environ 그대로라 동작 무변경. todo_system/claude_auth.py 참고.
                env=claude_env(),
            )
    except subprocess.CalledProcessError as exc:
        combined_tail = ""
        for p in (err_path, log_path):
            try:
                combined_tail += p.read_text(encoding="utf-8")[-2000:]
            except OSError:
                pass
        capacity = detect_capacity_error(combined_tail)
        if capacity:
            return False, (
                f"claude-capacity: {capacity} (exit={exc.returncode}); "
                f"see {err_path.relative_to(ROOT)}"
            )
        return False, f"claude exit={exc.returncode}; see {err_path.relative_to(ROOT)}"
    except FileNotFoundError:
        return False, "claude CLI not found on PATH"

    if not artifact.exists():
        # `claude -p` exits 0 even when it ran out of turns (it just prints
        # "Error: Reached max turns (N)" to stdout), so the generic
        # "artifact missing" message hid the real cause. Surface it explicitly
        # so the failure is diagnosable without grepping the stdout log.
        try:
            tail = log_path.read_text(encoding="utf-8")[-2000:]
        except OSError:
            tail = ""
        m = re.search(r"Reached max turns \((\d+)\)", tail)
        if m:
            return False, (
                f"stage hit max turns ({m.group(1)}) before writing "
                f"{artifact.relative_to(ROOT)} — raise max_turns for this stage/task"
            )
        capacity = detect_capacity_error(tail)
        if capacity:
            return False, (
                f"claude-capacity: {capacity} before writing "
                f"{artifact.relative_to(ROOT)}"
            )
        return False, f"artifact missing after stage: {artifact.relative_to(ROOT)}"

    # The Verify stage writes verify.md regardless of the verdict, so artifact
    # existence alone is NOT success — that let a `fail` (e.g. red CI, or a
    # "fix" that didn't actually fix anything) sail through to a clean Report
    # and a "completed" run. Gate on the verdict: only pass/skip continue.
    if stage == "verify":
        verdict = parse_verify_status(artifact)
        if verdict == "fail":
            return False, f"verify: overall_status=fail (see {artifact.relative_to(ROOT)})"

    return True, str(artifact.relative_to(ROOT))


# ---------------------------------------------------------------------------
# Review todo auto-generation
# ---------------------------------------------------------------------------


def should_skip_review_todo(task: WorkerTask) -> bool:
    """Determine if review todo should NOT be created for this task.

    Skip conditions:
    1. Task has 'review' tag (to prevent infinite generation loops)
    2. Task has 'worker_mode: auto-review-disabled'
    """
    tags = task.frontmatter.get("tags", [])
    if isinstance(tags, list) and "review" in tags:
        return True

    worker_mode = task.frontmatter.get("worker_mode", "")
    if isinstance(worker_mode, str) and worker_mode.strip().lower() == "auto-review-disabled":
        return True

    return False


def create_review_todo(
    task: WorkerTask,
    run_id: str,
    run_dir: Path,
    mode: str,
) -> Path | None:
    """Create a review todo file for a completed worker run.

    Returns the path of the created file, or None if creation was skipped.
    """
    # Remote hosts don't own Todo/items/ (it's the primary host's local data).
    # The primary host's remote-sweep creates the review todo instead, once the
    # run's queue.json/artifacts arrive via git.
    if IS_REMOTE_HOST:
        return None
    if should_skip_review_todo(task):
        return None

    # Carry over the parent task's classification tags (e.g. work/private/domain)
    # so the review inherits its lane in the flat, tags-based layout.
    parent_tags = task.frontmatter.get("tags", [])
    if not isinstance(parent_tags, list):
        parent_tags = []
    inherited_tags = [str(t) for t in parent_tags if str(t) not in ("todo", "review")]

    # Generate run_id short form (first 13 chars, e.g., "20260602T0640")
    runid_short = run_id[:13] if len(run_id) >= 13 else run_id

    # Build file path (flat items/ layout)
    filename = f"review-{task.slug}-{runid_short}.md"
    review_path = ITEMS_DIR / filename

    # Compute dates
    today = dt.date.today()
    due_date = today + dt.timedelta(days=3)
    today_str = today.isoformat()
    due_str = due_date.isoformat()

    # Build frontmatter
    frontmatter_lines = [
        "---",
        "tags:",
        "  - todo",
        *[f"  - {t}" for t in inherited_tags],
        "  - review",
        "status: open",
        f"due: {due_str}",
        f"scheduled: {today_str}",
        f"updated: {today_str}",
        "priority: 45",
        "delegate: false",
        f"parent: '[[{task.slug}]]'",
        f'outcome: "{task.slug} worker 결과 검토 완료"',
        'next_action: "report.md/verify.md 읽고 acceptance criteria 확인"',
        'if_then: "Worker 완료 알림 보면 → review todo 열어서 결과 확인"',
        "estimate_p50: 15m",
        "estimate_p90: 30m",
        "review_cycle: '-'",
        "---",
    ]

    # Build body
    run_dir_rel = run_dir.relative_to(ROOT)
    body_lines = [
        "",
        f"# Review: {task.title}",
        "",
        f"Worker run `{run_id}` completed in mode `{mode}`.",
        "",
        "## Links",
        "",
        f"- 원본 task: [[{task.rel_path}|{task.slug}]]",
        f"- report: [[{run_dir_rel}/report.md|report.md]]",
        f"- verify: [[{run_dir_rel}/verify.md|verify.md]]",
        "",
        "## Checklist",
        "",
        "- [ ] Acceptance criteria 충족 여부 확인",
        "- [ ] 빌드/테스트 통과 여부 확인",
        "- [ ] 변경 사항 review 완료",
        "- [ ] 다음 액션 결정 (merge / 수정 요청 / close)",
        "",
        "## Progress Log",
        "",
        f"- {today_str}: Worker 완료로 자동 생성 (run_id={run_id})",
    ]

    content = "\n".join(frontmatter_lines + body_lines) + "\n"

    # Write file
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(content, encoding="utf-8")

    return review_path


def _sweep_due(key: str, min_interval_s: int) -> bool:
    """Throttle expensive maintenance sweeps run from scan_queue.

    The todo-sync daemon (KeepAlive=true) calls scan_queue in a tight loop, so
    heavy sweeps (parent-id backfill, decision gates) must not run every pass.
    Records the last run time per key in SWEEP_STATE_PATH. Returns True (and
    stamps now) only if at least `min_interval_s` have elapsed.
    """
    now = dt.datetime.now().timestamp()
    try:
        state = json.loads(SWEEP_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    if now - state.get(key, 0) < min_interval_s:
        return False
    state[key] = now
    try:
        SWEEP_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass
    return True


def _area_from_tags(tags: Any) -> str:
    """Legacy NOT NULL `area` column: derive from tags ('work' else 'private')."""
    if isinstance(tags, list) and any(str(t) == "work" for t in tags):
        return "work"
    return "private"


def _markdown_parent_links() -> dict[str, str]:
    """Map leaf_slug -> parent_slug from `parent: "[[...]]"` frontmatter on disk.

    This is the human-authored, authoritative source of the parent/leaf tree.
    """
    links: dict[str, str] = {}
    if not ITEMS_DIR.exists():
        return links
    for path in ITEMS_DIR.glob("*.md"):
        try:
            fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        praw = fm.get("parent")
        if isinstance(praw, str):
            m = re.search(r"\[\[([^\]|]+)", praw)
            if m:
                links[path.stem] = m.group(1).strip()
    return links


def _build_parent_tree(tasks: list) -> tuple[list, dict[str, list]]:
    """Group leaf tasks under their parent. Returns (parents, {parent_slug: [leaves]}).

    Parent linkage is resolved in priority order:
      1. markdown `parent:` frontmatter (authoritative, human-authored)
      2. slug-prefix against the parent slug OR its stem (parent slug with a
         trailing `-word` removed, e.g. `worldgraph-project` -> `worldgraph`),
         since some parents carry a suffix their leaves don't share.
    The DB `parent_id` column is too sparsely populated to rely on. Excludes
    review-/decision- helper todos.
    """
    parents = [t for t in tasks if getattr(t, "is_parent", False)]
    parent_slugs = {p.slug for p in parents}
    md_links = _markdown_parent_links()

    # Prefix candidates: full slug and stem, mapped to the parent slug. Longest
    # prefix wins so nested names resolve to the most specific parent.
    prefix_map: dict[str, str] = {}
    for p in parents:
        prefix_map[p.slug] = p.slug
        stem = p.slug.rsplit("-", 1)[0]
        if stem and stem != p.slug:
            prefix_map.setdefault(stem, p.slug)
    prefixes = sorted(prefix_map, key=len, reverse=True)

    tree: dict[str, list] = {p.slug: [] for p in parents}
    for t in tasks:
        if getattr(t, "is_parent", False):
            continue
        if t.slug.startswith(("review-", "decision-")):
            continue
        # 1) markdown link
        linked = md_links.get(t.slug)
        if linked in parent_slugs:
            tree[linked].append(t)
            continue
        # 2) slug-prefix / stem fallback
        for pref in prefixes:
            if t.slug.startswith(pref + "-"):
                tree[prefix_map[pref]].append(t)
                break
    return parents, tree


def _latest_run_per_slug() -> dict[str, dict]:
    """Map slug -> its latest run's status.json (by run_id timestamp)."""
    latest: dict[str, dict] = {}
    if not RUNS_DIR.exists():
        return latest
    for sp in RUNS_DIR.glob("*/status.json"):
        try:
            s = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            continue
        slug = s.get("slug")
        if not slug:
            continue
        rid = s.get("run_id") or sp.parent.name
        cur = latest.get(slug)
        if cur is None or rid > (cur.get("run_id") or ""):
            s["_run_dir"] = sp.parent
            s["run_id"] = rid
            latest[slug] = s
    return latest


def _run_is_ci_failed(status: dict) -> bool:
    """True if the run's last stage was verify and it failed because CI was red
    (overall_status=fail), as opposed to a transient claude/API error."""
    sr = status.get("stage_results") or []
    if not sr:
        return False
    last = sr[-1]
    if last.get("stage") != "verify" or last.get("status") != "failed":
        return False
    blob = f"{last.get('message') or ''} {status.get('failure_reason') or ''}"
    return "overall_status=fail" in blob


# ---------------------------------------------------------------------------
# Planner invocation (idle path)
# ---------------------------------------------------------------------------


def invoke_planner(dry_run: bool = False) -> tuple[int, str]:
    """Invoke the planner agent to create auto tasks.

    Returns (tasks_created_count, verdict_string).
    """
    planner_prompt_path = COMMANDS_DIR / "worker-planner.md"
    if not planner_prompt_path.exists():
        log_planner("error: planner prompt not found")
        return 0, "error: prompt missing"

    prompt_text = strip_frontmatter(planner_prompt_path.read_text(encoding="utf-8"))

    if dry_run:
        print(f"  [dry-run] would invoke planner with --max-turns {PLANNER_MAX_TURNS}")
        return 0, "dry-run"

    if not shutil.which("claude"):
        log_planner("error: claude CLI not found")
        return 0, "error: claude not found"

    # Prepare log file
    timestamp = dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    planner_log_path = LOGS_DIR / f"planner.{timestamp}.log"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

    argv = [
        "claude",
        "-p",
        prompt_text,
        "--max-turns",
        str(PLANNER_MAX_TURNS),
        "--allowed-tools",
        "Read", "Write",
    ]

    try:
        result = subprocess.run(
            argv,
            cwd=ROOT,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=600,  # 10 min max
        )
        # Write combined output to log
        planner_log_path.write_text(
            f"=== STDOUT ===\n{result.stdout}\n\n=== STDERR ===\n{result.stderr}\n",
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        log_planner("error: planner timed out after 600s")
        return 0, "error: timeout"
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        log_planner(f"error: {exc}")
        return 0, f"error: {exc}"

    # Parse verdict from stdout
    stdout = result.stdout
    verdict = "unknown"
    tasks_created = 0

    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("verdict:"):
            verdict = line
            # Parse tasks count: verdict: tasks_created tasks=2 reason="..."
            if "tasks=" in line:
                try:
                    tasks_part = line.split("tasks=")[1].split()[0]
                    tasks_created = int(tasks_part)
                except (IndexError, ValueError):
                    pass
            break

    if verdict == "unknown":
        capacity = detect_capacity_error(stdout + result.stderr)
        if capacity:
            verdict = f"claude-capacity: {capacity}"

    log_planner(f"{verdict} (log={planner_log_path.name})")
    return tasks_created, verdict


def _handle_idle_planner(dry_run: bool = False) -> int:
    """Handle the idle path: check conditions and invoke planner if appropriate.

    Returns 0 on success (whether planner ran or was skipped), non-zero on error.
    """
    # Remote hosts never create tasks autonomously — only the primary host
    # runs the planner. An idle remote worker simply waits for inbox pushes.
    if IS_REMOTE_HOST:
        print(
            f"[planner] disabled on remote host '{WORKER_HOST}' — "
            "idle until new tasks arrive in the inbox"
        )
        return 0

    # Fetch once and reuse below — count_auto_pending + is_planner_exhausted
    # both need the run queue; without this they'd each re-read it on every
    # idle tick.
    queue = load_queue()

    # Check 1: auto queue depth limit
    pending_count = count_auto_pending(queue=queue)
    if pending_count >= AUTO_QUEUE_DEPTH_LIMIT:
        msg = f"skip: auto queue depth={pending_count} >= limit={AUTO_QUEUE_DEPTH_LIMIT}"
        print(f"[planner] {msg}")
        log_planner(msg)
        return 0

    # Check 2: exhausted backoff
    exhausted, reason = is_planner_exhausted(queue=queue)
    if exhausted:
        msg = f"skip: planner exhausted ({reason})"
        print(f"[planner] {msg}")
        log_planner(msg)
        return 0

    # Invoke planner
    current_hash = compute_todo_state_hash()
    print(f"[planner] invoking planner agent (dry_run={dry_run}, hash={current_hash})...")
    log_planner(f"invoke: hash={current_hash}")
    tasks_created, verdict = invoke_planner(dry_run=dry_run)

    if dry_run:
        print("[planner] dry-run complete")
        return 0

    # Save state based on verdict
    if "exhausted" in verdict:
        save_planner_state("exhausted")
        print(f"[planner] {verdict}")
    elif tasks_created > 0:
        save_planner_state("tasks_created")
        print(f"[planner] {verdict}")
        # Run scan_queue to register the newly created tasks
        print("[planner] running scan-queue to register new tasks...")
        scan_queue()
    else:
        # No tasks created but not explicitly exhausted — treat as exhausted
        save_planner_state("exhausted")
        print(f"[planner] {verdict} — treating as exhausted")

    return 0


# ---------------------------------------------------------------------------
# run-next
# ---------------------------------------------------------------------------


def find_task_by_slug(slug: str) -> WorkerTask | None:
    for task in iter_worker_tasks():
        if task.slug == slug:
            return task
    return None


def write_status(run_dir: Path, status: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def update_queue_entry(run_id: str, **fields: Any) -> None:
    """Patch a single run entry by run_id.

    flock + load/modify/save.
    """
    with queue_lock():
        queue = load_queue()
        for entry in queue["runs"]:
            if entry["run_id"] == run_id:
                entry.update(fields)
                break
        save_queue(queue)


def _return_vault_to_main(own_branch: str | None = None) -> None:
    """Best-effort return vault working tree to main branch.

    Called in finally block; failures are logged but never raise.
    This ensures the user sees vault on main after worker completes,
    regardless of success/failure/abort.

    The vault is a single checkout shared by every run, so this only moves HEAD
    when it is on main or on `own_branch` (this run's own worker branch). If
    another run is working on its own worker/<slug>, leave it alone — pulling it
    down to main would silently strand that run's edits and commits on main.
    A detached or unreadable HEAD counts as "not ours" and is left alone too.
    """
    try:
        current_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        current = current_result.stdout.strip() if current_result.returncode == 0 else ""

        if current != "main" and current != own_branch:
            print(
                f"  [warn] return-to-main skipped: HEAD is "
                f"{current or '(detached)'} (another run's branch)"
            )
            return

        result = subprocess.run(
            ["git", "checkout", "main"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"  [warn] return-to-main failed: {result.stderr[:100]}")
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  [warn] return-to-main exception: {exc}")


def run_next(dry_run: bool = False) -> int:
    """Execute the first pending run in the queue.

    Wrapper that ensures vault returns to main branch on exit (finally block).
    """
    # Mutable holder: the impl records this run's own worker branch here so the
    # finally block can tell "our branch" from a concurrent run's branch.
    own: dict[str, str | None] = {"branch": None}
    try:
        return _run_next_impl(dry_run, own_branch_out=own)
    finally:
        if not dry_run:
            _return_vault_to_main(own["branch"])


def _run_next_impl(
    dry_run: bool = False,
    own_branch_out: dict[str, str | None] | None = None,
) -> int:
    # Safety: vault must not be dirty outside Worker/ and Memory/episodes/.
    # Check BEFORE claiming to avoid phantom in_progress entries on dirty-abort.
    dirty, bad_files = vault_is_dirty_outside_worker()
    if dirty:
        print(
            "run-next: aborting — vault has uncommitted changes outside "
            "Worker/ and Memory/episodes/:"
        )
        for f in bad_files[:20]:
            print(f"  - {f}")
        if len(bad_files) > 20:
            print(f"  ... and {len(bad_files) - 20} more")
        record_dirty_stall(bad_files)
        return 2

    # Vault is clean: clear any prior stall marker (self-recovery).
    clear_dirty_stall()

    # Proactive usage guard: don't START a new pipeline run (or a planner
    # invocation via the idle path below) while the Claude subscription is on
    # pace to exhaust before its refill. Entries stay pending and nothing is
    # marked failed — the guard clears by itself as the quota window advances.
    # A dry-run launches no Claude process (invoke_planner / invoke_stage
    # return before spawning), so it is not subject to pacing — and must not
    # read the live account's usage, or throwaway-vault smoke runs turn red
    # whenever the host's account is ahead of pace.
    if dry_run:
        print("  [dry-run] usage guard not evaluated (dry-run launches no Claude process)")
    else:
        try:
            from todo_system import usage_guard
            guard = usage_guard.check()
        except Exception as exc:  # a guard outage must never stall the worker
            guard = None
            print(f"run-next: usage guard unavailable (continuing): {exc}")
        if guard is not None and not guard.get("allowed", True):
            print(f"run-next: skipping tick — usage guard blocked: {guard.get('reason')}")
            return 3

    # Atomic claim: marks status=in_progress under flock so concurrent
    # run_next processes pick disjoint entries.
    if dry_run:
        # For dry-run we DON'T mutate the queue, just peek at first pending.
        queue = load_queue()
        pending = [r for r in queue.get("runs", []) if r.get("status") == "pending"]
        if not pending:
            print("run-next: no pending runs — entering planner path")
            return _handle_idle_planner(dry_run=True)
        entry = pending[0]
    else:
        entry = claim_next_pending()
        if entry is None:
            print("run-next: no pending runs — entering planner path")
            return _handle_idle_planner(dry_run=False)

    slug = entry["slug"]
    run_id = entry["run_id"]

    task = find_task_by_slug(slug)
    if task is None:
        print(f"run-next: task file disappeared for slug={slug}; marking failed")
        update_queue_entry(
            run_id,
            status="failed",
            finished_at=now_iso(),
        )
        return 1

    # Task-level guardrails (auto_merge etc.) — fail fast before claiming.
    guard_err = check_task_guardrails(task)
    if guard_err:
        print(f"run-next: guardrail rejected task={slug}: {guard_err}")
        update_queue_entry(
            run_id,
            status="failed",
            finished_at=now_iso(),
        )
        return 1

    # M4-c: external repo — clone & checkout worker branch before first stage.
    if task.is_external_repo:
        if dry_run:
            try:
                wd = resolve_working_dir(task)
                print(
                    f"  [dry-run] would prepare external repo: clone {task.repo} "
                    f"-> {wd}, checkout {task.worker_branch}"
                )
            except ValueError as exc:
                print(f"  [dry-run] working_dir resolution failed: {exc}")
        else:
            ok, msg, wd = ensure_external_repo_ready(task)
            print(f"run-next: external repo prep: {msg}")
            if not ok:
                update_queue_entry(
                    run_id,
                    status="failed",
                    finished_at=now_iso(),
                )
                return 1
    else:
        # Vault-local task — ensure fresh worker branch from main.
        if dry_run:
            print(
                f"  [dry-run] would prepare vault branch: fresh {task.worker_branch} from main"
            )
        else:
            # Claim this branch as ours BEFORE prep: if prep fails partway and
            # leaves HEAD on the worker branch, the finally block must still be
            # allowed to bring our own vault checkout back to main.
            if own_branch_out is not None:
                own_branch_out["branch"] = task.worker_branch
            ok, msg = ensure_vault_branch_fresh(task.worker_branch)
            print(f"run-next: vault branch prep: {msg}")
            if not ok:
                update_queue_entry(
                    run_id,
                    status="failed",
                    finished_at=now_iso(),
                )
                return 1

    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    status: dict[str, Any] = {
        "run_id": run_id,
        "slug": slug,
        "task_path": task.rel_path,
        "started_at": now_iso(),
        "finished_at": None,
        "current_stage": None,
        "branch": task.worker_branch,
        "stage_results": [],
        "failure_reason": None,
    }
    write_status(run_dir, status)

    if not dry_run:
        # claim_next_pending already set status=in_progress and started_at;
        # just record the branch name here.
        update_queue_entry(run_id, branch=task.worker_branch)

    # Resolve which stage pipeline to run for this entry's mode.
    # Default to 'auto' for legacy queue entries that lack `mode`.
    entry_mode = entry.get("mode") or task.worker_mode or "auto"
    if entry_mode not in STAGES_BY_MODE:
        entry_mode = "auto"
    stage_list = STAGES_BY_MODE[entry_mode]

    print(f"run-next: starting run_id={run_id} slug={slug} mode={entry_mode} dry_run={dry_run}")
    if not dry_run:
        # Digest tier: "조치 불필요" is in the message itself (KAKAO_ALERTS.md).
        kakao_notify(_compose_worker_start(slug, entry_mode, len(stage_list)),
                     level=LEVEL_DIGEST, event_key=f"worker-start:{slug}")

    for stage in stage_list:
        stage_started = now_iso()
        status["current_stage"] = stage
        write_status(run_dir, status)
        print(f"  stage={stage} started={stage_started}")

        ok, message = invoke_stage(stage, task, run_id, run_dir, dry_run)
        stage_finished = now_iso()

        stage_record = {
            "stage": stage,
            "started_at": stage_started,
            "finished_at": stage_finished,
            "status": "ok" if ok else "failed",
            "artifact_path": message if ok else None,
            "message": message,
        }
        status["stage_results"].append(stage_record)
        write_status(run_dir, status)

        if not ok:
            status["failure_reason"] = f"stage {stage} failed: {message}"
            status["finished_at"] = stage_finished
            write_status(run_dir, status)
            if message.startswith("claude-capacity:"):
                # Account usage/rate limit, not a task bug — it's time-bound and
                # will clear on its own. Requeue as pending (unlimited retries,
                # doesn't burn the AUTO_RETRY_LIMIT budget or land in the
                # give-up-forever "failed" state) and skip the alarm
                # notification so an hours-long limit window doesn't spam
                # kakao every 5 minutes with the same transient condition.
                if not dry_run:
                    update_queue_entry(
                        run_id,
                        status="pending",
                        started_at=None,
                        finished_at=None,
                    )
                print(f"  stage={stage} capacity-limited: {message}")
                print(f"run-next: requeued (pending) at stage={stage} — will retry next cycle")
                return 1
            if not dry_run:
                update_queue_entry(
                    run_id,
                    status="failed",
                    finished_at=stage_finished,
                )
                # Immediate — but the same slug re-failing inside the merge
                # window (auto-retry churn) collapses into one "외 N건" ping.
                kakao_notify(_compose_worker_fail(slug, stage, message),
                             event_key=f"worker-fail:{slug}")
            print(f"  stage={stage} FAILED: {message}")
            print(f"run-next: aborted at stage={stage}")
            return 1

        print(f"  stage={stage} ok artifact={message}")

    status["current_stage"] = None
    status["finished_at"] = now_iso()
    write_status(run_dir, status)
    if not dry_run:
        # Note: Report stage is expected to itself update queue.json to
        # status=completed/failed based on verify result. We treat absence
        # of update as success here since all 5 stages produced artifacts.
        update_queue_entry(
            run_id,
            status="completed",
            finished_at=status["finished_at"],
        )
        # Digest tier: any follow-up judgement arrives as its own review alert.
        kakao_notify(_compose_worker_done(slug, entry_mode, len(stage_list)),
                     level=LEVEL_DIGEST, event_key=f"worker-done:{slug}")

        # Create review todo for user to check the worker output.
        review_path = create_review_todo(task, run_id, run_dir, entry_mode)
        if review_path:
            # Stage and commit the review todo so Report stage won't see dirty state.
            subprocess.run(
                ["git", "add", str(review_path)],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-m",
                    f"chore(worker): add review todo for {slug}",
                    "--",
                    str(review_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            print(f"  review-todo created: {review_path.relative_to(ROOT)}")
        elif IS_REMOTE_HOST:
            print("  review-todo deferred to primary host (remote-sweep)")
        else:
            print("  review-todo skipped (task has review tag or auto-review-disabled)")

    print(f"run-next: completed run_id={run_id}")
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Autonomous Implementation Worker (M4-a MVP)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("scan-queue", help="Scan Todo/items/** and register worker_mode:auto tasks in Worker/queue.json")

    sub.add_parser(
        "maintenance",
        help="Vault upkeep only (stale review autoclose + remote-run sweep) — no dispatch. "
             "Used after the v2 cutover, when the legacy queue worker is no longer loaded.",
    )

    p_run = sub.add_parser("run-next", help="Execute the first pending run in the queue")
    p_run.add_argument("--dry-run", action="store_true", help="Print what would be invoked without calling claude")

    p_status = sub.add_parser("status", help="Print status.json of a given run-id")
    p_status.add_argument("run_id")

    sub.add_parser("list", help="List all entries in Worker/queue.json")

    p_par = sub.add_parser(
        "run-parallel",
        help="Spawn N parallel run-next subprocesses; each claims a distinct pending entry (M4-b)",
    )
    p_par.add_argument("--max", type=int, default=3, help="max concurrent workers (default 3)")
    p_par.add_argument("--dry-run", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "scan-queue":
        return scan_queue()
    if args.command == "maintenance":
        return cmd_maintenance()
    if args.command == "run-next":
        return run_next(dry_run=args.dry_run)
    if args.command == "list":
        return cmd_list()
    if args.command == "status":
        return cmd_status(args.run_id)
    if args.command == "run-parallel":
        return cmd_run_parallel(max_concurrent=args.max, dry_run=args.dry_run)
    parser.error(f"unknown command: {args.command}")
    return 2  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())
