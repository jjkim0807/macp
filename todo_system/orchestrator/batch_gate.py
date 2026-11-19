"""Batch verify — light tasks pass a QUICK verify and share one full gate.

Every auto-merged task used to run its repo's whole gate alone, so five small
changes to one repo paid for five full builds. With a batch gate configured for
a repo, an eligible run verifies only what it changed (QUICK mode), opens a
ready PR and waits in the ledger as ``batch_ready``; the repo's full gate then
runs ONCE on an integration branch carrying several such tasks, and the PRs are
merged in order. A task that cannot be batched, or whose batch fails, goes back
to the ordinary individual full verify.

The first half of this module has no side effects on a repo: the wire strings,
the per-repo config reader, the eligibility rule, and the two ledger-row
transitions. The second half is the integration-gate job, one per repo:

* :func:`trigger_due` / :func:`pick` — when a gate starts (two tasks waiting,
  or the oldest has waited half an hour) and which tasks ride it;
* :func:`build_integration` — a LOCAL-only ``integ/<repo>/<ts>`` branch in its
  own worktree of the shared clone, the members merged in ready order (a
  member that conflicts, moved or lost its branch is left out and goes back
  to an individual full verify);
* :class:`LocalDetachedGate` — the repo's ``full_gate`` as one detached job;
* :func:`step` — the state machine. Each call moves the repo's job one step
  and returns; the orchestrator's sweep calls it once per tick under
  :func:`repo_lock`. Everything it needs to continue is in the job record on
  disk, because a gate outlives a tick and the orchestrator restarts on a
  code change.

Git, the gate runner, the merge, ``gh`` and the clock all come in through
:class:`Env`; the ledger writes and the alerts go out through :class:`Hooks`.

Opt-in per repo, off by default. The config is vault data::

    <vault_root>/.macp/batch_gate.json
    {"enabled": true,
     "repos": {"<repo_name>": {"full_gate": [["./gradlew", "test"]],
                               "quick_hint": "optional",
                               "max_wait_min": 30, "max_batch": 5,
                               "gate_timeout_min": 120}}}

``full_gate`` is a list of argv lists (not shell strings), all of which must
exit 0. The key is ``repos.repo_name(url)``. No file, ``enabled`` not true, or
no entry for the repo: that repo behaves exactly as before.

Stdlib-only at import. Sibling modules are imported inside the functions that
need them (``tasks`` imports this module, and ``remote_gate`` pulls in
``stages``), and the vault root is always passed in, never computed here.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import re
import shlex
import shutil
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Where the config lives, relative to the vault root.
CONFIG_REL = Path(".macp") / "batch_gate.json"

# --- wire strings ------------------------------------------------------------
# All of these land in git-tracked assignment JSON or in mailbox envelopes, so
# they are defined once, here, and never re-spelled by a caller. The ledger
# state and the "returned" history type live in assignment.py (BATCH_READY,
# BATCH_RETURNED) because assignment.is_deferred reads them.

# Dispatch payload keys.
KEY_VERIFY_BATCH = "verify_batch"          # frontmatter opt-in / opt-out
KEY_VERIFY_FULL = "verify_full"            # sent back once: never batch again
KEY_RETURN_REASON = "batch_return_reason"  # why it was sent back (RETURN_*)
KEY_BATCH = "batch"                        # the batch record: ledger row + RESULT
# What dispatch() copies from the previous ledger row onto the next payload.
CARRY_KEYS = (KEY_VERIFY_FULL, KEY_RETURN_REASON)

# RESULT payload keys the runner sets.
RESULT_READY = "batch_ready"
RESULT_RETURN = "batch_return"

# Ledger history types.
HISTORY_READY = "batch-ready"
HISTORY_MERGED = "batch-merged"            # the gate passed and the PR merged

# Why a task left the batch for an individual full verify.
RETURN_NOT_ENABLED = "not-enabled"         # the repo's gate left the config
RETURN_EXPIRED = "expired"                 # no gate ran within EXPIRE_S
RETURN_NO_DEVICE = "no-device-evidence"    # needs a device run, none this run
RETURN_NO_PR = "no-pr"                     # the report opened no PR
RETURN_NO_HEAD_SHA = "no-head-sha"         # the judged sha could not be read
RETURN_QUICK_NOT_PASS = "quick-not-pass"   # the quick verify did not say PASS
RETURN_GATE_FAILED = "gate-failed"         # the shared full gate exited non-zero
RETURN_GATE_TIMEOUT = "gate-timeout"       # …or ran past its time limit
RETURN_CONFLICT = "conflict"               # does not merge onto the integration branch
RETURN_BRANCH_MOVED = "branch-moved"       # origin's branch is not the judged sha
RETURN_BRANCH_MISSING = "branch-missing"   # origin has no such branch
RETURN_NO_CHECKOUT = "no-checkout"         # this host has no shared clone of the repo
RETURN_RESTARTS = "restarts"               # the default branch kept moving
RETURN_MERGE_FAILED = "merge-failed"       # the PR would not merge after the gate

# --- numbers -----------------------------------------------------------------
# The four a repo's config entry may override are read through limits().
EXPIRE_S = 86400            # a batch_ready row older than this goes back
MIN_BATCH = 2               # start a gate once this many tasks wait…
MAX_WAIT_S = 1800           # …or once the oldest has waited this long
MAX_BATCH = 5               # tasks per integration branch
MAX_RESTARTS = 3            # rebuilds after the default branch moved
GATE_TIMEOUT_S = 7200       # one full-gate run
MERGE_ATTEMPTS = 3          # failed `gh pr merge` calls before a member goes back

# --- the integration-gate job --------------------------------------------------
# Host-local strings: they live in Worker/_state/ and in event keys, never in
# the git-tracked ledger, so they are not wire format. Still spelled once, here.
STATE_REL = Path("Worker") / "_state" / "batch_gate"   # one <repo_name>.json per job

JOB_RUNNING = "running"     # the gate job is out
JOB_PASSED = "passed"       # the gate passed; the PRs are being merged in order
JOB_IDLE = "idle"           # torn down after the default branch moved; rebuild next

GATE_RUNNING = "running"    # what a gate runner's poll() answers
GATE_PASSED = "passed"
GATE_FAILED = "failed"

NOTICE_FAILED = "failed"        # Hooks.alert kinds — passed to the hook, not stored
NOTICE_PASSED = "passed"
NOTICE_RESTARTS = "restarts"

EVENT_EXPIRED = "batch-expired"         # alert event-key prefixes
EVENT_PASSED = "batch-gate-passed"
EVENT_RESTARTS = "batch-restarts"

GATE_JOB_PREFIX = "batch-gate-"         # jobs.py slug of a repo's gate run
_INTEG_BRANCH_PREFIX = "integ/"         # integ/<repo_name>/<YYYYMMDDTHHMMSS>
_INTEG_WORKTREE_PREFIX = "integ-"       # <repo>.wt/integ-<repo_name>-<ts>

_FETCH_TIMEOUT_S = 120      # a tick must not hang on a slow remote
_LS_REMOTE_TIMEOUT_S = 30

_BATCHABLE_TIERS = ("light", "standard")


def _as_bool(v) -> bool | None:
    """True/False for a bool or a true/yes/1 · false/no/0 string, else None.
    The same rule as tasks._fm_bool, kept apart because tasks imports this
    module."""
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "yes", "1"):
            return True
        if s in ("false", "no", "0"):
            return False
    return None


def load_config(vault_root) -> dict:
    """The vault's batch-gate config, or ``{}`` when the feature is off.

    Off means any of: no file, an unreadable file, broken JSON, a non-object, or
    ``enabled`` not true. Never raises — a config that cannot be read must leave
    every run on the ordinary path."""
    try:
        data = json.loads((Path(vault_root) / CONFIG_REL).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable config = feature off
        return {}
    if not isinstance(data, dict) or _as_bool(data.get("enabled")) is not True:
        return {}
    return data


def _normalise_gate(raw) -> list[list[str]] | None:
    """``full_gate`` as a list of argv lists, or None when it is unusable.

    A single argv (``["./gradlew", "test"]``) is wrapped once. Empty lists and
    empty or non-string tokens make the whole gate unusable: a gate that cannot
    be run exactly as written is not a gate."""
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    commands = [raw] if all(isinstance(tok, str) for tok in raw) else raw
    out: list[list[str]] = []
    for argv in commands:
        if not isinstance(argv, (list, tuple)) or not argv:
            return None
        if not all(isinstance(tok, str) and tok.strip() for tok in argv):
            return None
        out.append(list(argv))
    return out


def repo_gate(cfg, repo_url) -> dict | None:
    """This repo's config entry with ``full_gate`` normalised, or None.

    None for an empty config, a task with no repo, a repo with no entry, or an
    entry whose ``full_gate`` is missing or malformed. Never raises."""
    try:
        if not cfg or not repo_url:
            return None
        from . import repos
        entries = cfg.get("repos")
        if not isinstance(entries, dict):
            return None
        entry = entries.get(repos.repo_name(str(repo_url)))
        if not isinstance(entry, dict):
            return None
        full_gate = _normalise_gate(entry.get("full_gate"))
        if not full_gate:
            return None
        return {**entry, "full_gate": full_gate}
    except Exception:  # noqa: BLE001 - a bad entry = no gate for this repo
        return None


def remote_marked(gate, checkout) -> bool:
    """Does the repo mark any full-gate command to run on the remote node?

    Such a repo is left out of batch verify for now: a command that must run
    remotely is handled by each task's own verify, and the integration gate runs
    locally. Reads ``<checkout>/.macp/gates.json`` through remote_gate.decide,
    which matches the command string exactly. Any error answers True — when the
    marking cannot be read, the conservative answer is to keep the repo out."""
    try:
        from . import remote_gate
        for argv in (gate or {}).get("full_gate") or []:
            if remote_gate.decide(shlex.join(argv), checkout) == "remote":
                return True
        return False
    except Exception:  # noqa: BLE001 - unreadable marking = exclude
        return True


def needs_device(payload) -> bool:
    """Does the task ask for an on-device run (frontmatter ``emulator: true``)?

    tasks.load_dispatchable carries it as the ``emulator`` entry of
    ``heavy_signals``; a payload built another way may carry ``emulator``
    itself."""
    p = payload if isinstance(payload, dict) else {}
    signals = p.get("heavy_signals")
    if isinstance(signals, (list, tuple)) and "emulator" in signals:
        return True
    return _as_bool(p.get("emulator")) is True


def eligible(payload, *, tier, gate, five_stage, auto_merge,
             remote_marked=False) -> tuple[bool, str]:
    """(batch this run?, the reason) — the first matching rule wins.

    ``tier`` is the run's model tier (model_select.classify_tier), ``gate`` the
    repo's config entry (repo_gate), ``five_stage`` whether the run uses the full
    research→report pipeline, ``auto_merge`` the runner's own merge decision and
    ``remote_marked`` the answer of :func:`remote_marked`."""
    p = payload if isinstance(payload, dict) else {}
    if gate is None:
        return False, "repo has no batch gate"
    if not p.get("repo"):
        return False, "vault task"
    if _as_bool(p.get(KEY_VERIFY_FULL)):
        return False, "returned to full verify"
    if not five_stage or str(p.get("worker_mode") or "").lower() != "auto":
        return False, "not the 5-stage auto pipeline"
    if not auto_merge:
        return False, "auto_merge off"
    if remote_marked:
        return False, "full gate is remote-marked"
    asked = _as_bool(p.get(KEY_VERIFY_BATCH))
    if asked is False:
        return False, f"{KEY_VERIFY_BATCH}: false"
    if asked is True:
        return True, f"{KEY_VERIFY_BATCH}: true"
    return tier in _BATCHABLE_TIERS, f"tier {tier}"


def device_ok(payload, *, device_pass, ui_hold) -> bool:
    """May a quick-verified run wait for the integration gate?

    Not when the UI confidence guard fired (a high-confidence app-UI change
    nobody saw on a device), and not when the task asked for a device run that
    left no evidence this run."""
    return not ui_hold and (not needs_device(payload) or bool(device_pass))


def mark_ready(a, result_payload, *, now_iso: str) -> bool:
    """Move a ledger row to ``batch_ready`` from a runner RESULT. True if moved.

    Needs a PR url, a repo and the sha the quick verify judged. If any is
    missing the row is left exactly as it was and False is returned, so the
    caller falls back to the ordinary review park."""
    from .assignment import BATCH_READY

    rp = result_payload if isinstance(result_payload, dict) else {}
    row = a.payload if isinstance(a.payload, dict) else {}
    batch = rp.get(KEY_BATCH)
    pr_url = str(rp.get("pr_url") or "").strip()
    repo = str(rp.get("repo") or row.get("repo") or "").strip()
    head_sha = str((batch.get("head_sha") if isinstance(batch, dict) else "")
                   or "").strip()
    if not (pr_url and repo and head_sha):
        return False
    branch = str(rp.get("branch") or row.get("branch") or f"worker/{a.task_id}")
    run_dir = str(rp.get("run_dir") or "").strip()
    if not isinstance(a.payload, dict):
        a.payload = {}
    a.state = BATCH_READY
    a.payload[KEY_BATCH] = {
        "pr_url": pr_url,
        "head_sha": head_sha,
        "branch": branch,
        "ready_ts": now_iso,
        # The run-id basename only: the ledger is git-tracked and must not carry
        # one host's absolute path (the `last_run_dir` precedent).
        "run": Path(run_dir).name if run_dir else "",
    }
    a.updated_ts = now_iso
    a.last_note = "묶음 검증 대기 — quick Verify 통과, 통합 게이트 뒤 머지"
    a.history.append({"ts": now_iso, "type": HISTORY_READY, "from": "orchestrator"})
    return True


def mark_returned(a, reason: str, *, now_iso: str) -> None:
    """Send a ledger row back to an individual full verify.

    The row becomes FAILED with `batch-returned` as its last history entry,
    which assignment.is_batch_return_pending reads: the orchestrator then treats
    it as a deferral, not a failure. ``refail_count`` and ``timeout_streak`` are
    left alone for the same reason — nothing failed. ``verify_full`` on the
    payload keeps the next run out of the batch."""
    from .assignment import BATCH_RETURNED, FAILED

    if not isinstance(a.payload, dict):
        a.payload = {}
    a.state = FAILED
    a.payload.pop(KEY_BATCH, None)
    a.payload[KEY_VERIFY_FULL] = True
    a.payload[KEY_RETURN_REASON] = reason
    a.updated_ts = now_iso
    a.capacity_limited = False
    a.job_running = False
    a.checkout_busy = False
    a.timed_out = False
    a.blocked_on = ""
    a.last_note = f"묶음 복귀 — 개별 전체 Verify로 ({reason})"[:200]
    a.history.append({"ts": now_iso, "type": BATCH_RETURNED, "reason": reason,
                      "from": "orchestrator"})


def verify_hint(gate, default_branch: str = "") -> str:
    """The sentence appended to the QUICK verify instruction for this repo: the
    full-gate commands being deferred, the command that lists the changed files,
    and the repo's own ``quick_hint`` when it has one."""
    g = gate if isinstance(gate, dict) else {}
    commands = [shlex.join(argv) for argv in g.get("full_gate") or []]
    parts: list[str] = []
    if commands:
        parts.append("Deferred full gate for this repo: "
                     + ", ".join(f"`{c}`" for c in commands) + ".")
    parts.append("Changed files: `git diff --name-only "
                 f"origin/{default_branch or '<default>'}...HEAD`.")
    hint = g.get("quick_hint")
    if isinstance(hint, str) and hint.strip():
        parts.append(hint.strip())
    return " ".join(parts)


# =============================================================================
# The integration-gate job
# =============================================================================

# --- when a gate starts, and with whom ---------------------------------------

def _epoch(ts) -> float:
    """A ledger timestamp as epoch seconds; 0.0 when it cannot be read (the
    orchestrator's own `_parse_ts` rule)."""
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        return dt.datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _record(row) -> dict:
    """The row's batch record (``payload["batch"]``), ``{}`` when it has none."""
    payload = getattr(row, "payload", None)
    record = payload.get(KEY_BATCH) if isinstance(payload, dict) else None
    return record if isinstance(record, dict) else {}


def ready_epoch(row) -> float:
    """When the row became ``batch_ready``, as epoch seconds (0.0 if unreadable)."""
    return _epoch(_record(row).get("ready_ts"))


def limits(gate) -> dict:
    """The numbers one repo's job runs under: the module constants, each
    overridden by the repo's config entry when that value is a positive number
    (``max_wait_min`` and ``gate_timeout_min`` are minutes). A bad value, or no
    entry at all, leaves the constant."""
    g = gate if isinstance(gate, dict) else {}

    def positive(key):
        v = g.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            return None
        return v

    out = {"min_batch": MIN_BATCH, "max_wait_s": MAX_WAIT_S,
           "max_batch": MAX_BATCH, "gate_timeout_s": GATE_TIMEOUT_S}
    wait = positive("max_wait_min")
    if wait is not None:
        out["max_wait_s"] = wait * 60
    batch = positive("max_batch")
    if batch is not None:
        out["max_batch"] = max(1, int(batch))
    timeout = positive("gate_timeout_min")
    if timeout is not None:
        out["gate_timeout_s"] = timeout * 60
    return out


def trigger_due(rows, now, *, min_batch=MIN_BATCH, max_wait_s=MAX_WAIT_S,
                restart_due=False) -> bool:
    """Should a gate start for these waiting rows?

    Never for no rows. Otherwise yes when a rebuild is owed (``restart_due``),
    when ``min_batch`` tasks wait, or when the oldest has waited ``max_wait_s``."""
    rows = list(rows or [])
    if not rows:
        return False
    if restart_due or len(rows) >= min_batch:
        return True
    return now - min(ready_epoch(a) for a in rows) >= max_wait_s


def pick(rows, max_batch=MAX_BATCH) -> list:
    """The rows that ride the next gate: oldest ready first (slug breaks a tie),
    at most ``max_batch`` of them."""
    ordered = sorted(rows or [], key=lambda a: (ready_epoch(a),
                                                str(getattr(a, "task_id", ""))))
    return ordered[:max(1, int(max_batch))]


def member_of(row) -> dict:
    """What the job needs to know about one task, read from the row's batch
    record ONLY. The branch is never rebuilt as ``worker/<slug>``: a run may
    have published under another name, and a guess would merge the wrong ref.
    A row with no recorded branch is left out of the build as branch-missing."""
    record = _record(row)
    return {"slug": str(getattr(row, "task_id", "") or ""),
            "branch": str(record.get("branch") or ""),
            "head_sha": str(record.get("head_sha") or ""),
            "pr_url": str(record.get("pr_url") or "")}


# --- the job record ----------------------------------------------------------
# <vault_root>/Worker/_state/batch_gate/<repo_name>.json — host-local runtime
# state, one file per repo with a job. Fields: repo (url), repo_name, state
# (JOB_*), clone, branch, worktree, default_branch, base_sha, head_sha,
# expect_base (the sha origin's default branch must show before the next merge),
# members (member_of dicts, in merge order), merged (slugs), full_gate, gate
# (what the runner's start() returned), started_ts (epoch), restarts,
# restart_due, merge_attempts (slug -> failed merges). An idle record keeps
# only repo, repo_name, state, restarts and restart_due.

def state_dir(vault_root) -> Path:
    return Path(vault_root) / STATE_REL


def _safe(repo_name) -> str:
    """``repo_name`` as one safe path component (it names a state file, a lock,
    a jobs slug, a branch and a worktree). Raises ValueError for a name that
    cannot be one."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", str(repo_name or ""))
    if not name or name.startswith("."):
        raise ValueError(f"unusable repo name for a batch-gate job: {repo_name!r}")
    return name


def job_name(repo_url) -> str:
    """The name a repo's job goes by — its record, lock, gate job, branch and
    worktree: ``repos.repo_name(url)`` made path-safe. The sweep groups waiting
    rows by it, so a row and its repo's record always meet under one name."""
    from . import repos
    return _safe(repos.repo_name(str(repo_url or "")))


def job_path(vault_root, repo_name) -> Path:
    return state_dir(vault_root) / f"{_safe(repo_name)}.json"


def load_job(vault_root, repo_name) -> dict | None:
    """The repo's job record, or None: no file, unreadable, or not an object."""
    try:
        data = json.loads(job_path(vault_root, repo_name).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - an unreadable record = no job
        return None
    return data if isinstance(data, dict) else None


def save_job(vault_root, job) -> None:
    from .atomic_io import atomic_write_json
    atomic_write_json(job_path(vault_root, job["repo_name"]), job)


def clear_job(vault_root, repo_name) -> None:
    try:
        job_path(vault_root, repo_name).unlink(missing_ok=True)
    except (OSError, ValueError):
        pass


def all_jobs(vault_root) -> list[dict]:
    """Every readable job record, in file-name order. ``repo_name`` is always
    the file's stem — the name load_job and clear_job find it by."""
    out: list[dict] = []
    try:
        files = sorted(state_dir(vault_root).glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append({**data, "repo_name": f.stem})
    return out


def running_jobs(vault_root) -> list[dict]:
    """The records whose gate job is out right now."""
    return [job for job in all_jobs(vault_root) if job.get("state") == JOB_RUNNING]


def has_jobs(vault_root) -> bool:
    """Is there any job record at all? One directory listing, no file read —
    the sweep asks this on every tick. Never raises."""
    try:
        d = state_dir(vault_root)
        return d.is_dir() and any(d.glob("*.json"))
    except Exception:  # noqa: BLE001 - unreadable state dir = nothing to tend
        return False


@contextlib.contextmanager
def repo_lock(vault_root, repo_name):
    """Hold the repo's job lock for the block. Yields True when taken and False
    when another process holds it — then the caller skips the repo this tick.
    Non-blocking: a tick never waits here."""
    import fcntl

    path = state_dir(vault_root) / f"{_safe(repo_name)}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+", encoding="utf-8")
    held = False
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = True
        except OSError:
            held = False
        yield held
    finally:
        if held:
            with contextlib.suppress(OSError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        fh.close()


# --- the integration branch --------------------------------------------------

def _git_call(git, cwd, *args, timeout=None) -> tuple[int, str, str]:
    """(returncode, stdout, last stderr line) of one injected git call. A git
    that raises counts as rc 1, so every caller judges by the code alone."""
    try:
        r = git(cwd, *args) if timeout is None else git(cwd, *args, timeout=timeout)
    except Exception as e:  # noqa: BLE001 - a git that cannot run = a failed call
        return 1, "", f"{type(e).__name__}: {e}"
    lines = str(getattr(r, "stderr", "") or "").strip().splitlines()
    return (int(getattr(r, "returncode", 1)), str(getattr(r, "stdout", "") or "").strip(),
            lines[-1].strip() if lines else "")


def remote_head(git, clone, name) -> str | None:
    """Origin's sha for branch ``name``, asked of the remote itself (ls-remote,
    not the clone's tracking ref): the sha, ``""`` when origin has no such
    branch, None when origin could not be asked."""
    rc, out, _err = _git_call(git, clone, "ls-remote", "origin", f"refs/heads/{name}",
                              timeout=_LS_REMOTE_TIMEOUT_S)
    if rc != 0:
        return None
    return out.split()[0] if out else ""


def _sweep_stale_integ(clone, repo_name, git) -> None:
    """Remove integration worktrees and branches an earlier job left behind in
    this clone. A repo has one job and this runs only while it has none
    running, so everything under these names is stale. Fail-open."""
    wt_prefix = f"{_INTEG_WORKTREE_PREFIX}{repo_name}-"
    rc, out, _err = _git_call(git, clone, "worktree", "list", "--porcelain")
    if rc == 0:
        for line in out.splitlines():
            if not line.startswith("worktree "):
                continue
            path = line[len("worktree "):].strip()
            if Path(path).name.startswith(wt_prefix):
                _git_call(git, clone, "worktree", "remove", "--force", path)
    ref_prefix = f"{_INTEG_BRANCH_PREFIX}{repo_name}/"
    rc, out, _err = _git_call(git, clone, "for-each-ref", "--format=%(refname:short)",
                              f"refs/heads/{ref_prefix}")
    if rc == 0:
        for ref in out.split():
            if ref.startswith(ref_prefix):
                _git_call(git, clone, "branch", "-D", ref)


def build_integration(clone, default, members, *, branch, worktree, git,
                      repo_name: str = "") -> dict:
    """Build the integration branch: a new worktree of ``clone`` at origin's
    ``default``, with each member's branch merged in order.

    Returns ``{"ok", "error", "base_sha", "head_sha", "included", "excluded"}``.
    ``excluded`` is ``[(slug, RETURN_*)]`` for members left out: no such branch
    on origin (branch-missing), a branch that is no longer the sha the quick
    verify judged (branch-moved), or one that does not merge cleanly (conflict —
    the merge is aborted and the next member is tried on the same tree).
    ``ok`` False means git itself could not do its part (fetch, worktree add, an
    abort that failed): nothing is decided about any member and the caller
    retries on a later tick.

    The branch is LOCAL only — nothing here pushes. ``git`` is
    ``(cwd, *args, timeout=None) -> CompletedProcess``."""
    out: dict = {"ok": False, "error": "", "base_sha": "", "head_sha": "",
                 "included": [], "excluded": []}
    rc, _out, err = _git_call(git, clone, "fetch", "origin", "--prune",
                              timeout=_FETCH_TIMEOUT_S)
    if rc != 0:
        out["error"] = f"fetch failed: {err}"[:200]
        return out
    rc, base, err = _git_call(git, clone, "rev-parse", "--verify", "--quiet",
                              f"origin/{default}^{{commit}}")
    if rc != 0 or not base:
        out["error"] = f"origin/{default} not found"
        return out
    out["base_sha"] = base
    if repo_name:
        _sweep_stale_integ(clone, repo_name, git)
    with contextlib.suppress(OSError):
        Path(worktree).parent.mkdir(parents=True, exist_ok=True)
    # Started at the sha, not at `origin/<default>`: a remote-tracking start
    # point would make the new branch track the default branch.
    rc, _out, err = _git_call(git, clone, "worktree", "add", "-B", branch,
                              str(worktree), base)
    if rc != 0:
        out["error"] = f"worktree add failed: {err}"[:200]
        return out
    for member in members:
        slug, name = member.get("slug"), member.get("branch")
        rc, sha, _err = ((1, "", "") if not name else _git_call(
            git, clone, "rev-parse", "--verify", "--quiet",
            f"refs/remotes/origin/{name}^{{commit}}"))
        if rc != 0 or not sha:
            out["excluded"].append((slug, RETURN_BRANCH_MISSING))
            continue
        if sha != member.get("head_sha"):
            out["excluded"].append((slug, RETURN_BRANCH_MOVED))
            continue
        # The identity rides the command: the merge must not depend on a git
        # identity being configured on this host, and must not use a person's.
        rc, _out, err = _git_call(
            git, worktree, "-c", "user.name=macp-batch-gate",
            "-c", "user.email=batch-gate@invalid",
            "merge", "--no-ff", "--no-edit", sha)
        if rc != 0:
            abort_rc, _o, abort_err = _git_call(git, worktree, "merge", "--abort")
            if abort_rc != 0:
                out["error"] = f"merge of {slug} failed and could not be aborted: " \
                               f"{abort_err or err}"[:200]
                return out
            out["excluded"].append((slug, RETURN_CONFLICT))
            continue
        out["included"].append(member)
    rc, head, err = _git_call(git, worktree, "rev-parse", "HEAD")
    if rc != 0 or not head:
        out["error"] = f"integration head unreadable: {err}"[:200]
        return out
    out["head_sha"] = head
    out["ok"] = True
    return out


# --- the gate runner ---------------------------------------------------------

def gate_job_slug(repo_name) -> str:
    """The jobs.py slug of a repo's gate run (one per repo, like the job)."""
    return GATE_JOB_PREFIX + _safe(repo_name)


class LocalDetachedGate:
    """Runs a job's ``full_gate`` on this host as ONE detached job (jobs.py) in
    the integration worktree. The tick only ever polls it.

    ``jobs`` and ``stages`` are looked up on their modules at call time."""

    def start(self, job) -> dict:
        from . import jobs, stages

        worktree, clone = Path(job["worktree"]), Path(job["clone"])
        # local.properties is gitignored, so a fresh worktree has none; an
        # Android build needs the shared clone's (the beta publisher's rule).
        with contextlib.suppress(OSError):
            src, dst = clone / "local.properties", worktree / "local.properties"
            if src.is_file() and not dst.exists():
                shutil.copyfile(src, dst)
        slug = gate_job_slug(job["repo_name"])
        jobs.clear(slug)
        inherited = (os.environ.get("GRADLE_OPTS") or "").strip()
        ours = stages._worker_gradle_opts(worktree)
        opts = f"{inherited} {ours}" if inherited else ours
        cmd = ("export GRADLE_OPTS=" + shlex.quote(opts) + "; "
               + " && ".join(shlex.join(argv) for argv in job["full_gate"]))
        jobs.start(slug, cmd, cwd=worktree)
        return {"job": slug, "log": f"Worker/jobs/{jobs.log_path(slug).name}"}

    def poll(self, job) -> str:
        from . import jobs

        state = jobs.poll(gate_job_slug(job["repo_name"])).get("state")
        if state == "running":
            return GATE_RUNNING
        return GATE_PASSED if state == "done" else GATE_FAILED

    def stop(self, job) -> None:
        """Terminate a gate that is still running, with everything it spawned.

        jobs.stop signals the one recorded pid — the wrapper shell — and the
        gate command is its child, which would run on as an orphan. Both sit in
        the process group of the session jobs.start made, so the group is
        signalled. Never this process's own group, never group 0 or 1, and
        only while jobs.poll still says running: after the job has ended its
        pid may belong to something else."""
        from . import jobs

        slug = gate_job_slug(job["repo_name"])
        with contextlib.suppress(OSError):
            if jobs.poll(slug).get("state") == "running":
                pid = (jobs.read(slug) or {}).get("pid")
                if isinstance(pid, int) and not isinstance(pid, bool) and pid > 1:
                    pgid = os.getpgid(pid)
                    if pgid > 1 and pgid != os.getpgrp():
                        os.killpg(pgid, signal.SIGTERM)
        with contextlib.suppress(OSError):
            jobs.stop(slug)

    def cleanup(self, job) -> None:
        """Stop the Gradle daemons the gate started in the worktree (they leave
        the gate's session by themselves) and forget the job. The log stays."""
        from . import jobs, stages

        stages.cleanup_build_daemons(Path(job["worktree"]))
        jobs.clear(gate_job_slug(job["repo_name"]))


def default_gate():
    """The gate runner a production job uses. A function so a test can swap it."""
    return LocalDetachedGate()


# --- what step() is given ----------------------------------------------------

@dataclass
class Env:
    """Everything outside this module that a job touches."""

    git: Callable             # (cwd, *args, timeout=None) -> CompletedProcess
    gate: object              # start(job) -> dict · poll(job) -> GATE_* · stop · cleanup
    merge_pr: Callable        # (clone, branch, repo_url) -> {"merged": bool, "note": str}
    pr_state: Callable        # (branch, repo_url) -> "OPEN" | "MERGED" | "CLOSED" | None
    pr_merged: Callable       # (branch, repo_url) -> True | False | None (unknown)
    merge_sha: Callable       # (clone, branch, default) -> the squash sha | None
    remote_head: Callable     # (clone, branch name) -> sha | "" (no branch) | None
    now: Callable             # () -> epoch seconds
    log: Callable             # (one line) -> None
    clone_for: Callable       # (repo_url) -> the shared clone's Path | None
    worktree_for: Callable    # (repo_url, name) -> where that worktree goes
    remove_registry: Callable  # (worktree basename) -> anything
    default_branch: Callable  # (clone) -> the default branch's name


@dataclass
class Hooks:
    """The ledger writes and alerts a job asks its caller for."""

    close: Callable           # (row, note): the PR merged — close the task
    returned: Callable        # (row, RETURN_*): back to an individual full verify
    park: Callable            # (row, note): the PR was closed unmerged — a human's
    alert: Callable           # (NOTICE_*, repo_name, slugs, detail)


def default_env(*, now=None, log=None) -> Env:
    """The production wiring. Each default finds its target on its module when
    CALLED, so swapping e.g. ``stages.merge_pr`` afterwards is honoured."""
    from . import beta_publish, repos, stages, worktree_sweep

    def git(cwd, *args, timeout=None):
        return stages._git(Path(cwd), *args, timeout=timeout)

    def merge_pr(clone, branch, repo_url):
        return stages.merge_pr(Path(clone), branch, repo=repo_url)

    def merge_sha(clone, branch, default):
        # The squash commit's oid, from gh only. resolve_merge_sha falls back
        # to the clone's `origin/<default>`, which is stale right after a
        # server-side merge — so its git half is answered "unknown" here.
        import subprocess

        def gh_only(argv, **kw):
            if argv and argv[0] == "gh":
                return beta_publish._run(argv, **kw)
            return subprocess.CompletedProcess(argv, 1, "", "")

        try:
            return beta_publish.resolve_merge_sha(clone, branch, run=gh_only,
                                                  default_branch=default)
        except Exception:  # noqa: BLE001 - unknown; the caller asks origin
            return None

    def clone_for(repo_url):
        path = repos.local_path(repo_url)
        return path if (path / ".git").exists() else None

    return Env(
        git=git,
        gate=default_gate(),
        merge_pr=merge_pr,
        pr_state=lambda branch, repo_url: stages.gh_pr_state(branch, repo_url),
        pr_merged=lambda branch, repo_url: stages.gh_pr_merged(branch, repo_url),
        merge_sha=merge_sha,
        remote_head=lambda clone, name: remote_head(git, clone, name),
        now=now or time.time,
        log=log or (lambda msg: print(f"[orchestrator] batch gate: {msg}")),
        clone_for=clone_for,
        worktree_for=lambda repo_url, name: repos.worktree_path(repo_url, name),
        remove_registry=lambda name: worktree_sweep.remove_registry(name),
        default_branch=lambda clone: stages.default_branch(Path(clone)),
    )


# --- the state machine -------------------------------------------------------

_NOTE_MERGED = "묶음 통합 게이트 통과 — squash 머지됨"
_NOTE_ALREADY_MERGED = "묶음 검증 대기 중 PR이 이미 머지됨 — 닫음"
_NOTE_PR_CLOSED = "묶음 검증 대기 중 PR이 머지 없이 닫힘"


def wants_step(vault_root, repo_name, rows, gate_cfg, now) -> bool:
    """Is there anything for :func:`step` to do for this repo on this tick?

    Yes when the repo has a job record, or its waiting rows have reached the
    trigger. No otherwise — and then the caller takes no lock, and nothing is
    written, printed or run for the repo."""
    if load_job(vault_root, repo_name) is not None:
        return True
    if gate_cfg is None:
        return False
    lim = limits(gate_cfg)
    return trigger_due(rows, now, min_batch=lim["min_batch"],
                       max_wait_s=lim["max_wait_s"])


def _teardown(job, env, *, gate=True) -> None:
    """Remove what a job left on this host: its gate job and daemons, the
    worktree, the branch and the worktree's Gradle registry. Each part is
    fail-open, and git is only asked to delete names this module made."""
    worktree, branch = str(job.get("worktree") or ""), str(job.get("branch") or "")
    if gate and worktree:
        with contextlib.suppress(Exception):
            env.gate.cleanup(job)
    clone = job.get("clone")
    if not clone:
        return
    wt_name = Path(worktree).name if worktree else ""
    ours_wt = wt_name.startswith(_INTEG_WORKTREE_PREFIX)
    ours_branch = branch.startswith(_INTEG_BRANCH_PREFIX)
    if ours_wt:
        _git_call(env.git, clone, "worktree", "remove", "--force", worktree)
    if ours_branch:
        _git_call(env.git, clone, "branch", "-D", branch)
    if ours_wt or ours_branch:
        _git_call(env.git, clone, "worktree", "prune")
    if ours_wt:
        with contextlib.suppress(Exception):
            env.remove_registry(wt_name)


def abandon(job, env, vault_root) -> None:
    """Drop a job outright: stop its gate if one is out, tear down, forget it.
    Nothing is done to any row."""
    if job.get("state") == JOB_RUNNING:
        with contextlib.suppress(Exception):
            env.gate.stop(job)
    _teardown(job, env)
    clear_job(vault_root, job["repo_name"])


def _alert(hooks, env, kind, repo_name, slugs, detail) -> None:
    try:
        hooks.alert(kind, repo_name, list(slugs), dict(detail))
    except Exception as e:  # noqa: BLE001 - a lost notice must not strand the job
        env.log(f"{repo_name}: {kind} notice not sent ({type(e).__name__})")


def _restart(job, live, env, hooks, vault_root, why, out) -> None:
    """Throw the integration branch away so the next tick builds a fresh one.

    ``live`` are the rows of the members not merged yet; they stay
    ``batch_ready``. Past MAX_RESTARTS they go back to an individual full verify
    instead, with one notice. With no such row left there is nothing to rebuild
    and the job simply ends."""
    name = job["repo_name"]
    _teardown(job, env)
    if not live:
        clear_job(vault_root, name)
        env.log(f"{name}: job ended — {why}; no member is left to gate")
        return
    restarts = int(job.get("restarts") or 0) + 1
    if restarts > MAX_RESTARTS:
        slugs = []
        for a in live:
            hooks.returned(a, RETURN_RESTARTS)
            out["returned"].append(a.task_id)
            slugs.append(a.task_id)
        clear_job(vault_root, name)
        _alert(hooks, env, NOTICE_RESTARTS, name, slugs,
               {"branch": job.get("branch") or "", "reason": why})
        env.log(f"{name}: restarted {MAX_RESTARTS} times already — {len(slugs)} task(s) "
                f"back to an individual full verify ({why})")
        return
    save_job(vault_root, {"repo": job.get("repo"), "repo_name": name, "state": JOB_IDLE,
                          "restart_due": True, "restarts": restarts})
    env.log(f"{name}: restart {restarts}/{MAX_RESTARTS} — {why}")


def _member_rows(job, rows) -> tuple[list, bool]:
    """(the rows of the members not merged yet, is any such member's row gone?).
    A row counts only while it still records the sha the job built with."""
    by_slug = {str(a.task_id): a for a in rows}
    merged = set(job.get("merged") or [])
    live, missing = [], False
    for member in job.get("members") or []:
        if member.get("slug") in merged:
            continue
        a = by_slug.get(member.get("slug"))
        if a is not None and member_of(a)["head_sha"] == member.get("head_sha"):
            live.append(a)
        else:
            missing = True
    return live, missing


def _start(repo_url, name, rows, gate_cfg, lim, idle, env, hooks, vault_root,
           paused, gradle_busy, out) -> None:
    """No job, or an idle one: start a gate if the trigger is reached."""
    now = env.now()
    restart_due = bool((idle or {}).get("restart_due"))
    due = {"min_batch": lim["min_batch"], "max_wait_s": lim["max_wait_s"],
           "restart_due": restart_due}
    if not trigger_due(rows, now, **due) or paused:
        return
    if gradle_busy is not None and gradle_busy():
        env.log(f"{name}: gate start deferred — the gradle build cap is reached")
        return
    clone = env.clone_for(repo_url)
    if clone is None:
        for a in rows:
            hooks.returned(a, RETURN_NO_CHECKOUT)
            out["returned"].append(a.task_id)
        clear_job(vault_root, name)
        env.log(f"{name}: no shared clone on this host — {len(rows)} task(s) back "
                "to an individual full verify")
        return
    # A PR a person merged or closed while it waited is not gated. Asked only
    # now, with the trigger reached, so an idle tick makes no gh call.
    kept = []
    for a in pick(rows, lim["max_batch"]):
        member = member_of(a)
        state = env.pr_state(member["branch"], repo_url) if member["branch"] else None
        if state == "MERGED":
            hooks.close(a, _NOTE_ALREADY_MERGED)
            out["closed"].append(a.task_id)
            env.log(f"{name}: {a.task_id} closed — its PR was already merged")
        elif state == "CLOSED":
            hooks.park(a, f"{_NOTE_PR_CLOSED} — {member['branch']}")
            env.log(f"{name}: {a.task_id} parked — its PR was closed without merging")
        else:
            kept.append(a)
    if not trigger_due(kept, now, **due):
        return
    default = env.default_branch(clone)
    stamp = dt.datetime.fromtimestamp(now).strftime("%Y%m%dT%H%M%S")
    branch = f"{_INTEG_BRANCH_PREFIX}{name}/{stamp}"
    worktree = env.worktree_for(repo_url, f"{_INTEG_WORKTREE_PREFIX}{name}-{stamp}")
    job = {"repo": repo_url, "repo_name": name, "state": JOB_RUNNING,
           "clone": str(clone), "branch": branch, "worktree": str(worktree),
           "default_branch": default, "full_gate": gate_cfg["full_gate"],
           "merged": [], "merge_attempts": {}, "restart_due": False,
           "restarts": int((idle or {}).get("restarts") or 0)}
    built = build_integration(clone, default, [member_of(a) for a in kept],
                              branch=branch, worktree=worktree, git=env.git,
                              repo_name=name)
    if not built["ok"]:
        # Git could not do its part (a locked shared clone, a remote that did
        # not answer). Nothing is decided: same rows, same record, next tick.
        _teardown(job, env, gate=False)
        env.log(f"{name}: integration branch not built ({built['error']}) — "
                "retrying next tick")
        return
    by_slug = {a.task_id: a for a in kept}
    for slug, reason in built["excluded"]:
        hooks.returned(by_slug[slug], reason)
        out["returned"].append(slug)
    if not built["included"]:
        _teardown(job, env, gate=False)
        clear_job(vault_root, name)
        env.log(f"{name}: no task merged onto {branch} — nothing to gate")
        return
    job.update(base_sha=built["base_sha"], head_sha=built["head_sha"],
               expect_base=built["base_sha"], members=built["included"])
    try:
        job["gate"] = env.gate.start(job)
    except Exception as e:  # noqa: BLE001 - not started = not decided; retry
        _teardown(job, env)
        env.log(f"{name}: gate did not start ({type(e).__name__}: {e}) — "
                "retrying next tick")
        return
    job["started_ts"] = now
    save_job(vault_root, job)
    slugs = [member["slug"] for member in job["members"]]
    out["started"].extend(slugs)
    env.log(f"{name}: gate started on {branch} at {built['base_sha'][:12]} — "
            f"{', '.join(slugs)}")


def _fail(job, live, reason, env, hooks, vault_root, out) -> None:
    """The gate failed or ran out of time: every member goes back to an
    individual full verify (which task broke the batch is not known), with ONE
    notice for the batch."""
    name = job["repo_name"]
    slugs = []
    for a in live:
        hooks.returned(a, reason)
        out["returned"].append(a.task_id)
        slugs.append(a.task_id)
    gate = job.get("gate") if isinstance(job.get("gate"), dict) else {}
    _alert(hooks, env, NOTICE_FAILED, name, slugs,
           {"branch": job.get("branch") or "", "log": gate.get("log") or "",
            "reason": reason})
    _teardown(job, env)
    clear_job(vault_root, name)
    env.log(f"{name}: gate {reason} on {job.get('branch')} — {len(slugs)} task(s) "
            "back to an individual full verify")


def _merge(job, rows, env, hooks, vault_root, out) -> None:
    """The gate passed: merge the members' PRs in the order they were built.

    Only the combination the gate saw is merged. Before each merge origin's
    default branch must still be the sha expected (the build's base, then each
    of our own squash commits) and the member's branch the sha that was built;
    otherwise the rest is rebuilt and gated again. One member per failed merge
    attempt: a merge that cannot be confirmed stops the loop until next tick."""
    name, repo_url, clone = job["repo_name"], job["repo"], job["clone"]
    default = job.get("default_branch") or "main"
    members = job.get("members") or []
    merged = job.setdefault("merged", [])
    attempts = job.setdefault("merge_attempts", {})
    by_slug = {str(a.task_id): a for a in rows}

    def live():
        return [by_slug[m["slug"]] for m in members
                if m["slug"] not in merged and m["slug"] in by_slug]

    for member in members:
        slug, branch = member["slug"], member["branch"]
        if slug in merged:
            continue
        a = by_slug.get(slug)
        if a is None or member_of(a)["head_sha"] != member.get("head_sha"):
            by_slug.pop(slug, None)
            _restart(job, live(), env, hooks, vault_root,
                     f"{slug} left the batch", out)
            return
        head = env.remote_head(clone, default)
        if head is None:
            env.log(f"{name}: origin/{default} could not be read — merging "
                    "continues next tick")
            return
        if head != job.get("expect_base"):
            _restart(job, live(), env, hooks, vault_root,
                     f"origin/{default} moved to {head[:12] or '(gone)'}", out)
            return
        tip = env.remote_head(clone, branch)
        if tip is None:
            env.log(f"{name}: origin/{branch} could not be read — merging "
                    "continues next tick")
            return
        if tip != member.get("head_sha"):
            reason = RETURN_BRANCH_MOVED if tip else RETURN_BRANCH_MISSING
            hooks.returned(a, reason)
            out["returned"].append(slug)
            by_slug.pop(slug, None)
            _restart(job, live(), env, hooks, vault_root, f"{slug}: {reason}", out)
            return
        result = env.merge_pr(clone, branch, repo_url)
        result = result if isinstance(result, dict) else {}
        if result.get("merged") or env.pr_merged(branch, repo_url) is True:
            hooks.close(a, _NOTE_MERGED)
            merged.append(slug)
            out["merged"].append(slug)
            by_slug.pop(slug, None)
            job["expect_base"] = (env.merge_sha(clone, branch, default)
                                  or env.remote_head(clone, default)
                                  or job.get("expect_base"))
            save_job(vault_root, job)
            env.log(f"{name}: merged {slug} ({len(merged)}/{len(members)})")
            continue
        tried = int(attempts.get(slug) or 0) + 1
        attempts[slug] = tried
        note = str(result.get("note") or "no reason given")[:160]
        if tried >= MERGE_ATTEMPTS:
            hooks.returned(a, RETURN_MERGE_FAILED)
            out["returned"].append(slug)
            by_slug.pop(slug, None)
            _restart(job, live(), env, hooks, vault_root,
                     f"{slug}: {RETURN_MERGE_FAILED} ({note})", out)
            return
        save_job(vault_root, job)
        env.log(f"{name}: merge of {slug} not confirmed ({tried}/{MERGE_ATTEMPTS}): "
                f"{note} — retrying next tick")
        return
    _teardown(job, env)
    clear_job(vault_root, name)
    _alert(hooks, env, NOTICE_PASSED, name, merged, {"branch": job.get("branch") or ""})
    env.log(f"{name}: batch done — {len(merged)} PR(s) merged from {job.get('branch')}")


def step(repo_url, rows, gate_cfg, env, hooks, *, vault_root, paused=False,
         gradle_busy=None) -> dict:
    """Move one repo's integration-gate job one step. Returns
    ``{"started", "merged", "returned", "closed"}`` — lists of slugs.

    ``rows`` are the repo's rows that are ``batch_ready`` right now, ``gate_cfg``
    its config entry (:func:`repo_gate`; None once the repo left the config).
    ``paused`` holds back a NEW gate only — a gate that is out is still judged
    and merged. ``gradle_busy`` is a callable answering "is the Gradle build cap
    reached?", asked only when a gate is about to start.

    First match wins:

    * no config entry, or a job with no waiting row → the job is dropped;
    * no job, or an idle one → nothing unless the trigger is reached; then the
      PRs' own states are checked, the integration branch built (members that
      do not fit go back to a full verify) and the gate started;
    * a running job → a member whose row is gone rebuilds the batch; a gate
      still running is only checked against its time limit; a failed or
      timed-out gate sends every member back with one notice; a passed gate
      goes on to the merge in the same call;
    * a passed job → :func:`_merge`.

    Call it under :func:`repo_lock`. A raise leaves the record as last saved,
    and the next call continues from it."""
    out: dict = {"started": [], "merged": [], "returned": [], "closed": []}
    repo_url = str(repo_url or "")
    name = job_name(repo_url)
    rows = list(rows or [])
    job = load_job(vault_root, name)
    if job is not None:
        job["repo_name"] = name
        job.setdefault("repo", repo_url)
    if gate_cfg is None or (job is not None and not rows):
        if job is not None:
            abandon(job, env, vault_root)
            env.log(f"{name}: job dropped — "
                    + ("the repo has no batch gate any more" if gate_cfg is None
                       else "no task is waiting"))
        return out
    lim = limits(gate_cfg)
    state = job.get("state") if job is not None else None
    if job is None or state == JOB_IDLE:
        _start(repo_url, name, rows, gate_cfg, lim, job, env, hooks, vault_root,
               paused, gradle_busy, out)
    elif state == JOB_RUNNING:
        live, missing = _member_rows(job, rows)
        if missing and not live:
            abandon(job, env, vault_root)
            env.log(f"{name}: job dropped — every member left the batch")
        elif missing:
            with contextlib.suppress(Exception):
                env.gate.stop(job)
            _restart(job, live, env, hooks, vault_root, "a member left the batch", out)
        else:
            verdict = env.gate.poll(job)
            if verdict == GATE_RUNNING:
                if env.now() - _epoch(job.get("started_ts")) >= lim["gate_timeout_s"]:
                    with contextlib.suppress(Exception):
                        env.gate.stop(job)
                    _fail(job, live, RETURN_GATE_TIMEOUT, env, hooks, vault_root, out)
            elif verdict == GATE_PASSED:
                job["state"] = JOB_PASSED
                save_job(vault_root, job)
                env.log(f"{name}: gate passed on {job.get('branch')} — merging "
                        f"{len(live)} PR(s) in order")
                _merge(job, rows, env, hooks, vault_root, out)
            else:
                _fail(job, live, RETURN_GATE_FAILED, env, hooks, vault_root, out)
    elif state == JOB_PASSED:
        _merge(job, rows, env, hooks, vault_root, out)
    else:
        abandon(job, env, vault_root)
        env.log(f"{name}: job dropped — unreadable state {state!r}")
    return out
