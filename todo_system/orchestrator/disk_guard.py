"""Run-start disk guard — free root-disk space before a run needs it.

The HQ host's root filesystem (`/dev/root`, 48G) is shared by ~/Worker, /tmp and
the vault. On 2026-09-19 it reached 160M free, and 물꼬 46's first
`:app:testDebugUnitTest` died 4/4 of ENOSPC while Robolectric copied fonts into
/tmp. The space had gone to Gradle `build/` output in sibling ~/Worker clones
nobody was building (0.6–1G each); `./gradlew clean --no-daemon` in three idle
clones gave back ~2.4G (the vault's Memory entity on the root-disk ENOSPC).
A few Android runs later it is full again, so the first aid has to be automatic.

`ensure_free_space` is that first aid, once per run, called from
`StagedExecutor._execute` after the long-job and sibling-hold defers and before
branch prep's `git fetch` and every stage build:

* free ≥ MIN_FREE_BYTES: nothing at all. One statvfs, no subprocess, no
  progress line (정상이면 무음 — and no mailbox churn).
* below it: `./gradlew clean --no-daemon` in IDLE ~/Worker clones, least
  recently used first, until free ≥ TARGET_FREE_BYTES (the 2G gap is
  hysteresis) or BUDGET_S runs out. Exactly one progress line then names what
  was cleaned, what was skipped and why, and whether free space is STILL below.

Idle is a conjunction, and a rung that cannot be answered counts as "no":

* a real clone: `.git` is a directory and the entry is not a symlink, so a
  linked worktree (`~/Worker/wt-<slug>`) is never touched;
* not this run's repo; not the repo of a ledger row that is dispatched,
  accepted or in_progress or has a long_job running; not the cwd of a long_job
  state file; not the cwd of a live process (/proc — a test JVM, a human's
  shell; this one belt is absent on a host without /proc). An unreadable ledger
  or job file refuses every clone;
* has `gradlew` and a `build/` at depth ≤ 2 — without output `clean` is a
  30–60 s no-op, repeated at every run start for as long as the disk stays low;
* on its default branch (origin/HEAD, else main — stages.default_branch's
  rule), not detached, and `git status --porcelain` prints nothing.

The only deleting act in this module is CLEAN_ARGV. There is no rm, rmtree or
`git clean` (test_disk_guard pins that), and `clean` removes only build output
the next build regenerates. Fail-open by contract, like cleanup_build_daemons:
the guard is advisory, so it never raises, and a timeout, a missing binary or
an odd exit code becomes a skip reason, never a dead run.

Stdlib plus three stdlib-only siblings, so scripts/health_report.py can read
MIN_FREE_BYTES under a bare python3. For a read-only look at the real host:

    python -m todo_system.orchestrator.disk_guard --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from .. import paths
from . import assignment, jobs, repos

GIB = 1024 ** 3
MIN_FREE_BYTES = 3 * GIB       # below this at run start, the guard cleans
TARGET_FREE_BYTES = 5 * GIB    # …and stops once free space is back up here
CLEAN_TIMEOUT_S = 300          # one clone's `gradlew clean`
# The whole guard, all clones together. It runs ahead of the first stage with no
# progress touch of its own, so it stays far inside the reaper's
# stage_timeout + stages.REAP_MARGIN window.
BUDGET_S = 600
GIT_TIMEOUT_S = 30             # each read-only git probe
KILL_ENV = "MACP_DISK_GUARD"
CLEAN_ARGV = ("./gradlew", "clean", "--no-daemon")   # the ONLY deleting act
# Ledger states that mean a run holds (or is about to hold) its repo's clone.
ACTIVE_STATES = frozenset({assignment.DISPATCHED, assignment.ACCEPTED,
                           assignment.IN_PROGRESS})
BUILD_DEPTH = 2                # `build`, `*/build`, `*/*/build`
SKIP_TEXT_LIMIT = 400          # chars of `name=reason` pairs in the progress line

# Seams, all read at CALL time — never frozen into a default argument, the trap
# todo_system/tests/conftest.py documents — so the suite-wide fixture in
# todo_system/conftest.py can point every one of them at a tmp dir.
WORKER_ROOT = paths.worker_root()
ASSIGNMENTS_DIR = jobs.REPO_ROOT / "Worker" / "assignments"   # the orchestrator's ledger
JOBS_DIR = jobs.JOBS_DIR
_disk_usage = shutil.disk_usage
# Bound at import on purpose. Several _execute suites patch the global
# `subprocess.run` and treat every call they see as a stage call; the guard's
# git probes and cleans must never land in such a fake.
_run = subprocess.run
_clock = time.monotonic


class _Refused(Exception):
    """A git probe that could not answer; its message is the skip reason."""


def enabled() -> bool:
    """The kill switch: `MACP_DISK_GUARD=0` in the runner's env turns the guard
    off entirely — not even the statvfs runs. Read at CALL time
    (stages.usage_enabled's rule); only an explicit "0" disables it."""
    return os.environ.get(KILL_ENV, "").strip() != "0"


def measure(path: str | Path | None = None, *,
            disk_usage: Callable | None = None) -> tuple[int, int] | None:
    """(free, total) bytes of the filesystem holding `path` (default: the
    worker root), or None when it cannot be read. A path that does not exist
    yet is measured at its nearest existing ancestor, the filesystem it would
    be created on. `free` is what an unprivileged process may still use
    (statvfs f_bavail) — the number ENOSPC is about."""
    usage = disk_usage or _disk_usage
    try:
        p = Path(WORKER_ROOT if path is None else path)
        while not p.exists() and p != p.parent:
            p = p.parent
        u = usage(str(p))
        return int(u.free), int(u.total)
    except Exception:
        return None


def _real(p: str | Path) -> str:
    return os.path.realpath(os.fspath(p))


def _inside(child: str, parent: str) -> bool:
    """`child` is `parent` or lies below it (both already realpath'd)."""
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _gib(n: int | float) -> str:
    return f"{n / GIB:.1f}G"


def candidates(worker_root: str | Path) -> list[Path]:
    """Direct children of the worker root that carry a `.git`, least recently
    used first. Recency is the mtime of `.git/logs/HEAD` (every checkout,
    commit, reset and pull appends to it), else of `.git` itself. Linked
    worktrees and symlinked entries are listed too, so that they are refused
    by name rather than silently; entries without `.git` are not checkouts."""
    try:
        entries = [Path(e.path) for e in os.scandir(worker_root)]
    except OSError:
        return []
    found: list[tuple[float, str, Path]] = []
    for p in entries:
        git = p / ".git"
        try:
            if not git.exists():
                continue
            head = git / "logs" / "HEAD"
            found.append(((head if head.exists() else git).stat().st_mtime, p.name, p))
        except OSError:
            continue
    return [p for _, _, p in sorted(found)]


def _holders(worker_root: str | Path, assignments_dir: str | Path,
             jobs_dir: str | Path) -> tuple[dict[str, str] | None, str]:
    """(realpath → who holds it, "") — or (None, what could not be read).

    Ledger rows map to a clone through `payload.repo`, exactly as the runner
    resolves them (repos.local_path); claims carry no repo, so they cannot.
    A long_job state file holds its `cwd` for as long as it exists, in any
    state: a finished job's output sits in the clone until the next dispatch
    consumes it. A missing jobs dir is normal (jobs.start creates it lazily);
    a missing ledger is not, because it means the busy set is unknowable."""
    root = Path(worker_root)
    adir = Path(assignments_dir)
    held: dict[str, str] = {}
    if not adir.is_dir():
        return None, f"no ledger dir {adir}"
    for f in sorted(adir.glob("*.json")):
        try:
            row = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            return None, f"{f.name}: {type(e).__name__}"
        if not isinstance(row, dict):
            return None, f"{f.name}: not a JSON object"
        state = row.get("state")
        job_running = bool(row.get("job_running"))
        if state not in ACTIVE_STATES and not job_running:
            continue
        payload = row.get("payload") or {}
        if not isinstance(payload, dict):
            return None, f"{f.name}: payload is {type(payload).__name__}"
        repo = payload.get("repo")
        if repo is None or repo == "":
            continue                     # a vault task: it holds no ~/Worker clone
        if not isinstance(repo, str):
            return None, f"{f.name}: payload.repo is {type(repo).__name__}"
        tag = state if state in ACTIVE_STATES else "job_running"
        held.setdefault(_real(repos.local_path(repo, root)),
                        f"held by {row.get('task_id') or f.stem} ({tag})")
    jdir = Path(jobs_dir)
    if jdir.is_dir():
        for f in sorted(jdir.glob("*.json")):
            try:
                st = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                return None, f"{f.name}: {type(e).__name__}"
            if not isinstance(st, dict):
                return None, f"{f.name}: not a JSON object"
            cwd = st.get("cwd")
            if cwd is None:
                continue                 # a vault job
            if not isinstance(cwd, str) or not cwd:
                return None, f"{f.name}: bad cwd"
            held.setdefault(_real(cwd), f"long_job {st.get('slug') or f.stem} cwd")
    return held, ""


def busy_checkouts(worker_root: str | Path | None = None,
                   assignments_dir: str | Path | None = None,
                   jobs_dir: str | Path | None = None) -> dict[str, str] | None:
    """Realpaths some run or long_job holds right now → who holds each. None
    when the ledger or a job file cannot be read, which refuses every clone."""
    return _holders(WORKER_ROOT if worker_root is None else worker_root,
                    ASSIGNMENTS_DIR if assignments_dir is None else assignments_dir,
                    JOBS_DIR if jobs_dir is None else jobs_dir)[0]


def live_process_cwds(proc: str | Path = "/proc") -> list[tuple[int, str]] | None:
    """(pid, cwd) of every live process this user may inspect, this one
    excepted — a test JVM, a human's shell, another run's gradle. None on a
    host without /proc (macOS): that belt is then absent and the others still
    gate. A process that exits or refuses mid-scan is simply not listed."""
    try:
        names = os.listdir(proc)
    except OSError:
        return None
    me = os.getpid()
    out: list[tuple[int, str]] = []
    for name in names:
        if not name.isdigit() or int(name) == me:
            continue
        try:
            out.append((int(name), os.readlink(os.path.join(os.fspath(proc), name, "cwd"))))
        except OSError:
            continue
    return out


_proc_cwds = live_process_cwds


def has_build_output(checkout: str | Path, depth: int = BUILD_DEPTH) -> bool:
    """A `build/` dir at depth ≤ `depth` below the checkout (`build`,
    `*/build`, `*/*/build`) — where Gradle writes module output in every clone
    here. Hidden dirs and node_modules are not walked into."""
    level = [Path(checkout)]
    for _ in range(depth + 1):
        deeper: list[Path] = []
        for d in level:
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if (e.name.startswith(".") or e.name == "node_modules"
                                or not e.is_dir(follow_symlinks=False)):
                            continue
                        if e.name == "build":
                            return True
                        deeper.append(Path(e.path))
            except OSError:
                continue
        level = deeper
    return False


def _git(run: Callable, checkout: Path, *args: str) -> subprocess.CompletedProcess:
    """One read-only git probe. `--no-optional-locks` keeps `status` from
    refreshing the index, so a probe can never block a git process that
    another run or a human has going in the same clone."""
    try:
        return run(["git", "--no-optional-locks", "-C", str(checkout), *args],
                   capture_output=True, text=True, encoding="utf-8", errors="replace",
                   stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise _Refused(f"git error: {args[0]} timed out") from None
    except OSError as e:
        raise _Refused(f"git error: {args[0]} {type(e).__name__}") from None


def idle_reason(checkout: str | Path, *, exclude: Iterable = (),
                busy: dict[str, str] | None = None,
                cwds: list[tuple[int, str]] | None = None,
                run: Callable | None = None) -> str | None:
    """Why `checkout` must not be cleaned now, or None when it is idle.

    Cheap checks first (memory, then the filesystem), the git probes last, and
    every rung that cannot be answered is a reason, never a pass. `busy` is
    busy_checkouts()'s answer, so None refuses. `cwds` None means this host has
    no /proc: that one belt is absent and the rest still gate."""
    c = Path(checkout)
    real = _real(c)
    if any(_inside(_real(x), real) for x in exclude if x):
        return "this run"
    if c.is_symlink():
        return "symlink"
    git = c / ".git"
    if not git.is_dir():
        return "linked worktree" if git.exists() else "not a git checkout"
    if busy is None:
        return "ledger unreadable"
    for held, who in busy.items():
        if _inside(held, real):
            return who
    for pid, cwd in cwds or ():
        if _inside(_real(cwd), real):
            return f"pid {pid} cwd"
    if not (c / "gradlew").is_file():
        return "no gradlew"
    if not has_build_output(c):
        return "no build output"
    run = run or _run
    try:
        r = _git(run, c, "branch", "--show-current")
        if r.returncode != 0:
            return f"git error: branch rc={r.returncode}"
        branch = (r.stdout or "").strip()
        if not branch:
            return "detached HEAD"
        r = _git(run, c, "symbolic-ref", "refs/remotes/origin/HEAD")
        ref = (r.stdout or "").strip() if r.returncode == 0 else ""
        default = ref.rsplit("/", 1)[-1] if ref else "main"
        if branch != default:
            return f"on {branch} (default {default})"
        r = _git(run, c, "status", "--porcelain")
        if r.returncode != 0:
            return f"git error: status rc={r.returncode}"
        if (r.stdout or "").strip():
            return "dirty"
    except _Refused as e:
        return str(e)
    return None


def _clean(run: Callable, checkout: Path, timeout: float) -> str | None:
    """CLEAN_ARGV in `checkout`: None on success, else the skip reason."""
    try:
        r = run(list(CLEAN_ARGV), cwd=str(checkout), stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"clean timed out ({int(timeout)}s)"
    except OSError as e:
        return f"clean {type(e).__name__}"
    return None if r.returncode == 0 else f"clean rc={r.returncode}"


def _clip(items: list[str], limit: int) -> str:
    """Join as many items as fit in `limit` chars, then `…(+N)` for the rest."""
    out: list[str] = []
    used = 0
    for i, s in enumerate(items):
        add = len(s) + (2 if out else 0)
        if out and used + add > limit:
            return ", ".join(out) + f", …(+{len(items) - i})"
        out.append(s)
        used += add
    return ", ".join(out)


def summarize(res: dict) -> str:
    """The single progress line of a triggered guard."""
    lo = res["threshold"]
    line = (f"disk guard: free {_gib(res['free_before'])} → {_gib(res['free_after'])} "
            f"(trigger <{_gib(lo)}, target {_gib(res['target'])}) — cleaned "
            + (", ".join(res["cleaned"]) or "nothing"))
    pairs = [f"{name}={why}" for name, why in res["skipped"].items()]
    if pairs:
        line += "; skipped " + _clip(pairs, SKIP_TEXT_LIMIT)
    if res.get("ledger"):
        line += f" [ledger: {res['ledger']}]"
    if res["free_after"] < lo:
        line += f" — STILL below {_gib(lo)}"
    return line


def _say(progress: Callable[[str], None] | None, line: str) -> None:
    if progress is None:
        return
    try:
        progress(line)
    except Exception:
        pass


def ensure_free_space(*, exclude: Iterable = (),
                      progress: Callable[[str], None] | None = None,
                      worker_root: str | Path | None = None,
                      assignments_dir: str | Path | None = None,
                      jobs_dir: str | Path | None = None,
                      min_free: int | None = None, target_free: int | None = None,
                      disk_usage: Callable | None = None, run: Callable | None = None,
                      proc_cwds: Callable | None = None,
                      clock: Callable[[], float] | None = None) -> dict:
    """Clean idle ~/Worker clones while free space is below `min_free`.

    `exclude` is this run's own repo. Every other argument is a seam for tests
    and defaults to the module global of the same role, read now. Returns
    {triggered, free_before, free_after, threshold, target, cleaned, skipped}
    (skipped = clone name → reason), plus `disabled`, `error`, `ledger` or
    `elapsed_s` when they apply. Zero progress lines when nothing needed doing;
    exactly one when it acted, or when it could not look. Never raises."""
    lo = MIN_FREE_BYTES if min_free is None else min_free
    res: dict = {"triggered": False, "free_before": None, "free_after": None,
                 "threshold": lo,
                 "target": max(lo, TARGET_FREE_BYTES if target_free is None else target_free),
                 "cleaned": [], "skipped": {}}
    try:
        if not enabled():
            res["disabled"] = True
            return res
        root = Path(WORKER_ROOT if worker_root is None else worker_root)
        adir = ASSIGNMENTS_DIR if assignments_dir is None else assignments_dir
        jdir = JOBS_DIR if jobs_dir is None else jobs_dir
        usage = disk_usage or _disk_usage
        run = run or _run
        scan = proc_cwds or _proc_cwds
        now = clock or _clock
        m = measure(root, disk_usage=usage)
        if m is None:
            res["error"] = "free space unreadable"
            _say(progress, f"disk guard: free space unreadable under {root} — skipped")
            return res
        free = res["free_before"] = res["free_after"] = m[0]
        if free >= lo:
            return res                   # healthy: silent, and no subprocess at all
        res["triggered"] = True
        excl = [x for x in exclude if x]
        start = now()
        for c in candidates(root):
            if free >= res["target"]:
                break                    # hysteresis reached — the rest can wait
            if BUDGET_S - (now() - start) <= 0:
                res["skipped"][c.name] = "budget exhausted"
                continue
            attempted = False
            try:
                # Re-read per clone: a clean takes a minute, and a run can be
                # dispatched onto the next clone in that minute.
                held, problem = _holders(root, adir, jdir)
                if problem:
                    res["ledger"] = problem
                why = idle_reason(c, exclude=excl, busy=held, cwds=scan(), run=run)
                if why is None:
                    left = BUDGET_S - (now() - start)
                    if left <= 0:
                        why = "budget exhausted"
                    else:
                        attempted = True
                        why = _clean(run, c, min(CLEAN_TIMEOUT_S, left))
            except Exception as e:       # one odd clone must not end the sweep
                why = f"error: {type(e).__name__}"
            if why is None:
                res["cleaned"].append(c.name)
            else:
                res["skipped"][c.name] = why
            if attempted:                # a failed clean may still have freed some
                m = measure(root, disk_usage=usage)
                if m is not None:
                    free = res["free_after"] = m[0]
        res["elapsed_s"] = round(now() - start, 1)
        _say(progress, summarize(res))
        return res
    except Exception as e:               # advisory: never raise into a run
        res["error"] = f"{type(e).__name__}: {e}"[:200]
        _say(progress, f"disk guard: error ({res['error']}) after cleaning "
                       f"{', '.join(res['cleaned']) or 'nothing'} — run proceeds")
        return res


def main(argv: list[str] | None = None) -> int:
    """`--dry-run`: measure, classify every clone, print. It never runs
    CLEAN_ARGV — only the runner does that, at a run start."""
    ap = argparse.ArgumentParser(
        prog="python -m todo_system.orchestrator.disk_guard",
        description="Show what the run-start disk guard would do. Read-only.")
    ap.add_argument("--dry-run", action="store_true",
                    help="measure free space and classify every ~/Worker clone; clean nothing")
    args = ap.parse_args(argv)
    if not args.dry_run:
        print("only --dry-run is supported: the guard itself runs inside the runner "
              "(StagedExecutor._execute)", file=sys.stderr)
        return 2
    root = Path(WORKER_ROOT)
    print(f"worker root: {root}")
    m = measure(root)
    if m is None:
        print("free space: unreadable")
    else:
        free, total = m
        verdict = ("below the trigger — a run start would clean" if free < MIN_FREE_BYTES
                   else "healthy — a run start stays silent")
        print(f"free space: {_gib(free)} of {_gib(total)} (trigger <{_gib(MIN_FREE_BYTES)}, "
              f"target {_gib(TARGET_FREE_BYTES)}) — {verdict}")
    print(f"kill switch {KILL_ENV}: "
          + ("off (guard enabled)" if enabled() else "ON — the guard is disabled"))
    print(f"ledger: {ASSIGNMENTS_DIR}")
    print(f"jobs: {JOBS_DIR}")
    held, problem = _holders(root, ASSIGNMENTS_DIR, JOBS_DIR)
    if held is None:
        print(f"busy set: UNKNOWN ({problem}) — every clone would be skipped")
    cwds = _proc_cwds()
    if cwds is None:
        print("live process cwds: unavailable (no /proc) — that belt is absent")
    for c in candidates(root):
        why = idle_reason(c, busy=held, cwds=cwds, run=_run)
        print(f"  {c.name}: " + ("would clean" if why is None else f"skip: {why}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
