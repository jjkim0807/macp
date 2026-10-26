"""Reclaim per-run `<shared clone>.wt/<slug>` worktrees and gradle-registry dirs.

Since the per-run worktree switchover an external run executes in its own
linked worktree `<worker root>/<repo>.wt/<slug>` (repos.ensure_worktree), and
its Gradle daemons register under `stages.GRADLE_REGISTRY_DIR / <slug>`.

* A SUCCEEDED run: the runner's `finally` removes the worktree
  (stages.remove_worktree) and then, through `remove_registry`, its registry dir.
* A failed / parked run keeps both as evidence; a retry's ensure_worktree reuses
  the worktree. Nothing else ever removed them: worker.reclaim_stale_worktrees
  only sees `<worker root>/wt-*` worktrees of the vault repo, by design.

`reclaim_parked_worktrees` closes that gap from the 5-minute doctor tick. A
worktree is removed only when ALL of these hold, and any rung that cannot be
answered holds it:

1. idle past the retention window (RETENTION_DAYS_DEFAULT, env
   RETENTION_ENV): the age is measured from the LATEST of the ledger row's
   `updated_ts` and the mtimes of the worktree's git admin files (HEAD, index,
   logs/HEAD). Unparseable timestamp → treated as now; no admin file → hold;
2. not `locked`;
3. its ledger row (Worker/assignments/<slug>.json) is not dispatched / accepted /
   in_progress and carries no `job_running`; an unreadable ledger or job file
   holds EVERY worktree (disk_guard's rule);
4. no long_job state file has its `cwd` inside it;
5. this process's cwd, any live process's cwd (/proc) and any command line
   (`pgrep -f`) do not reference it — pgrep that cannot answer counts as busy;
6. a detached worktree's HEAD is reachable from some ref;
7. `git worktree remove` WITHOUT `--force` succeeds — git refuses a tree with
   uncommitted or untracked (non-ignored) files, so that work is never lost.

Only the worktree goes; the `worker/<slug>` branch ref stays in the shared clone,
so every committed change survives, and an `awaiting_human` resume simply gets a
fresh worktree from ensure_worktree. The slug's registry dir follows the
worktree. A registry dir with no worktree left (a success run from before this
module, a `--stop` race residue) is an orphan and goes past the same window by
its newest mtime — never one named after a direct child of the worker root (a
shared clone's or `wt-*` worktree's registry), the vault / engine basename, an
active slug, or a name still present as `<x>.wt/<name>`.

TOCTOU with a parallel slot: a worktree ensure_worktree has just added for
another slot has fresh admin-file mtimes (and a `dispatched` row), so it is
inside the window and is never even evaluated for removal.

The ONLY recursive delete in this module is `shutil.rmtree` inside
`remove_registry`, confined to one plain-named, non-symlink child of the registry
dir. Worktrees go only through `git worktree remove`. Fail-open: the sweep
returns [] on any unexpected error and never raises into the doctor tick.
Silent when nothing is past the window; otherwise one `정리 완료/보류/제외`
(cleaned up / held / excluded) line per entry it judged.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from . import disk_guard, stages

RETENTION_DAYS_DEFAULT = 7
RETENTION_ENV = "MACP_PARKED_WORKTREE_RETENTION_DAYS"
MAX_ACTIONS = 5            # removals per tick, per kind (worktrees / orphan registries)
BUDGET_S = 30              # read on loop entry, like worker.STALE_WORKTREE_BUDGET_S
GIT_TIMEOUT_S = 10
ADMIN_FILES = ("HEAD", "index", "logs/HEAD")
DAY_S = 86400

# Seams, read at CALL time (todo_system/conftest.py points REGISTRY_DIR, and
# disk_guard's WORKER_ROOT / ASSIGNMENTS_DIR / JOBS_DIR, at tmp for every test).
REGISTRY_DIR = stages.GRADLE_REGISTRY_DIR
# Bound at import on purpose (disk_guard's rule): suites that patch the global
# `subprocess.run` must never see this module's git / pgrep calls.
_run = subprocess.run
_proc_cwds = disk_guard.live_process_cwds
_monotonic = time.monotonic


class _Unreadable(Exception):
    """The ledger or a job file could not be read; message = the reason."""


def _retention_s() -> int:
    raw = os.environ.get(RETENTION_ENV, "").strip()
    try:
        days = int(raw)
    except ValueError:
        days = RETENTION_DAYS_DEFAULT
    if days < 1:
        days = RETENTION_DAYS_DEFAULT
    return days * DAY_S


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return _run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                timeout=GIT_TIMEOUT_S)


def _pgrep_free(path: str | Path) -> bool:
    """True only when `pgrep -f <path>` says no match (rc 1). A match, any other
    exit code, a timeout or a missing binary all mean "in use"."""
    try:
        r = _run(["pgrep", "-f", str(path)], capture_output=True, text=True,
                 stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 1


def remove_registry(name: str, *, registry_dir: str | Path | None = None,
                    progress: Callable[[str], None] | None = None) -> bool:
    """Delete `<registry dir>/<name>`; return whether it was removed.

    Refuses (False) unless `name` is one plain path component (non-empty, no
    separator, not `.`/`..`, not dot-prefixed), the entry is not a symlink, is a
    directory, and resolves to a direct child of the registry dir. NEVER raises:
    the runner calls it from its `finally`."""
    try:
        base = Path(REGISTRY_DIR if registry_dir is None else registry_dir)
        if (not isinstance(name, str) or not name or name.startswith(".")
                or "/" in name or os.sep in name or name in (".", "..")):
            return False
        target = base / name
        if target.is_symlink() or not target.is_dir():
            return False
        if target.resolve().parent != base.resolve():
            return False
        shutil.rmtree(target)
        if progress is not None:
            progress(f"gradle-registry removed: {target}")
        return True
    except Exception:      # noqa: BLE001 — finally-called; a raise would mask the outcome
        if progress is not None:
            try:
                progress(f"gradle-registry remove errored: {name}")
            except Exception:
                pass
        return False


def _ledger_rows() -> dict[str, dict]:
    """slug → ledger row. Raises _Unreadable on a missing dir or any bad row."""
    adir = Path(disk_guard.ASSIGNMENTS_DIR)
    if not adir.is_dir():
        raise _Unreadable(f"no ledger dir {adir}")
    rows: dict[str, dict] = {}
    for f in sorted(adir.glob("*.json")):
        try:
            row = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise _Unreadable(f"{f.name}: {type(e).__name__}") from None
        if not isinstance(row, dict):
            raise _Unreadable(f"{f.name}: not a JSON object")
        rows[f.stem] = row
        task_id = row.get("task_id")
        if isinstance(task_id, str) and task_id:
            rows.setdefault(task_id, row)
    return rows


def _job_cwds() -> list[str]:
    """Realpaths of every long_job state file's `cwd` (any state)."""
    jdir = Path(disk_guard.JOBS_DIR)
    out: list[str] = []
    if not jdir.is_dir():
        return out
    for f in sorted(jdir.glob("*.json")):
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise _Unreadable(f"{f.name}: {type(e).__name__}") from None
        if not isinstance(st, dict):
            raise _Unreadable(f"{f.name}: not a JSON object")
        cwd = st.get("cwd")
        if cwd is None:
            continue
        if not isinstance(cwd, str) or not cwd:
            raise _Unreadable(f"{f.name}: bad cwd")
        out.append(disk_guard._real(cwd))
    return out


def _active_reason(row: dict | None) -> str | None:
    if not row:
        return None
    state = row.get("state")
    if state in disk_guard.ACTIVE_STATES:
        return f"active {state}"
    if row.get("job_running"):
        return "job_running"
    return None


def _ts(value) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _admin_mtime(wt: Path) -> float | None:
    """Newest mtime among the worktree's git admin files, or None."""
    try:
        text = (wt / ".git").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    gitdir = Path(text[len("gitdir:"):].strip())
    if not gitdir.is_absolute():
        gitdir = wt / gitdir
    best: float | None = None
    for name in ADMIN_FILES:
        try:
            m = (gitdir / name).stat().st_mtime
        except OSError:
            continue
        best = m if best is None else max(best, m)
    return best


def _newest_mtime(d: Path, depth: int = 2) -> float | None:
    try:
        best = d.lstat().st_mtime
    except OSError:
        return None
    level = [d]
    for _ in range(depth):
        deeper: list[Path] = []
        for p in level:
            try:
                with os.scandir(p) as it:
                    for e in it:
                        try:
                            best = max(best, e.stat(follow_symlinks=False).st_mtime)
                            if e.is_dir(follow_symlinks=False):
                                deeper.append(Path(e.path))
                        except OSError:
                            continue
            except OSError:
                continue
        level = deeper
    return best


def _parse_porcelain(text: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for block in text.split("\n\n"):
        rec: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key:
                rec[key] = value
        if rec:
            records.append(rec)
    return records


def _wt_dirs(root: Path) -> list[tuple[Path, Path]]:
    """(shared clone, its `.wt` dir) for every real `<root>/<name>.wt` whose
    `<root>/<name>` is a real clone (`.git` is a directory)."""
    out: list[tuple[Path, Path]] = []
    for e in sorted(os.scandir(root), key=lambda e: e.name):
        if (not e.name.endswith(".wt") or e.name.startswith(".")
                or not e.is_dir(follow_symlinks=False)):
            continue
        clone = root / e.name[:-len(".wt")]
        if clone.is_symlink() or not (clone / ".git").is_dir():
            continue
        out.append((clone, Path(e.path)))
    return out


def _parked_worktrees(root: Path) -> list[tuple[Path, Path, dict[str, str]]]:
    """(clone, worktree, porcelain record) for each worktree registered on a
    shared clone whose path is a direct, non-dot-prefixed child of its `.wt`
    dir. `.<slug>-longjob-stash` siblings, unregistered dirs and the clone
    itself never appear; prunable (directory gone) records are left to prune."""
    found: list[tuple[Path, Path, dict[str, str]]] = []
    for clone, wtdir in _wt_dirs(root):
        r = _git(clone, "--no-optional-locks", "worktree", "list", "--porcelain")
        if r.returncode != 0:
            continue
        parent = wtdir.resolve()
        for rec in _parse_porcelain(r.stdout or ""):
            raw = rec.get("worktree", "")
            if not raw or {"bare", "prunable"} & rec.keys():
                continue
            path = Path(raw)
            if path.name.startswith(".") or path.parent.resolve() != parent:
                continue
            found.append((clone, path, rec))
    return found


def _held_by_process(path: Path) -> str | None:
    real = disk_guard._real(path)
    try:
        here = disk_guard._real(os.getcwd())
    except OSError:
        here = ""
    if here and disk_guard._inside(here, real):
        return "this process"
    for pid, cwd in _proc_cwds() or ():
        if disk_guard._inside(disk_guard._real(cwd), real):
            return f"pid {pid} cwd"
    if not _pgrep_free(path):
        return "in-use"
    return None


def _detached_unreferenced(path: Path) -> bool:
    r = _git(path, "--no-optional-locks", "for-each-ref", "--contains", "HEAD", "--count=1")
    return r.returncode != 0 or not (r.stdout or "").strip()


def _protected_registry_names(root: Path) -> set[str]:
    names = {stages._VAULT_DATA_ROOT.name, stages._ENGINE_ROOT.name}
    names.update(os.listdir(root))
    for _clone, wtdir in _wt_dirs(root):
        try:
            names.update(os.listdir(wtdir))
        except OSError:
            continue
    return names


def reclaim_parked_worktrees(now: float | None = None) -> list[str]:
    """Remove past-window parked `.wt/<slug>` worktrees with their registry
    dirs, then past-window orphan registry dirs. Returns the log lines (no
    `doctor:` prefix — run_doctor_and_persist adds it). Never raises."""
    try:
        root = Path(disk_guard.WORKER_ROOT)
        if not root.is_dir():
            return []
        now = time.time() if now is None else now
        window = _retention_s()
        deadline = _monotonic() + BUDGET_S

        # Pass 1, ledger-free: which worktrees / registry dirs are past the
        # window by their own files. Fresh entries are not candidates at all.
        wt_due: list[tuple[Path, Path, dict[str, str], float | None]] = []
        for clone, path, rec in _parked_worktrees(root):
            m = _admin_mtime(path)
            if m is None or now - m > window:
                wt_due.append((clone, path, rec, m))
        reg_base = Path(REGISTRY_DIR)
        reg_due: list[tuple[Path, float]] = []
        if reg_base.is_dir():
            protected = _protected_registry_names(root)
            for e in sorted(os.scandir(reg_base), key=lambda e: e.name):
                if (e.name.startswith(".") or e.name in protected
                        or not e.is_dir(follow_symlinks=False)):
                    continue
                m = _newest_mtime(Path(e.path))
                if m is not None and now - m > window:
                    reg_due.append((Path(e.path), m))
        if not wt_due and not reg_due:
            return []

        try:
            rows = _ledger_rows()
            job_cwds = _job_cwds()
        except _Unreadable as e:
            return [f"parked worktree 정리 전체 보류 ({e})"]

        actions: list[str] = []
        removed = 0
        overflow = 0
        unscanned = 0
        for index, (clone, path, rec, admin_m) in enumerate(wt_due):
            if _monotonic() >= deadline:
                unscanned = len(wt_due) - index
                break
            slug = path.name
            row = rows.get(slug)
            try:
                if admin_m is None:
                    actions.append(f"정리 보류: {path} (age unknown)")
                    continue
                refs = [admin_m]
                if row is not None:
                    t = _ts(row.get("updated_ts"))
                    refs.append(now if t is None else t)
                age = now - max(refs)
                if age <= window:
                    continue                 # the ledger moved more recently
                if "locked" in rec:
                    actions.append(f"정리 제외: {path} (locked)")
                    continue
                why = _active_reason(row)
                if why is None:
                    real = disk_guard._real(path)
                    if any(disk_guard._inside(c, real) for c in job_cwds):
                        why = "long_job cwd"
                if why is None:
                    why = _held_by_process(path)
                if why is None and "detached" in rec and _detached_unreferenced(path):
                    why = "detached, unreferenced HEAD"
                if why is not None:
                    actions.append(f"정리 보류: {path} ({why})")
                    continue
                if removed >= MAX_ACTIONS:
                    overflow += 1
                    continue
                if _git(clone, "worktree", "remove", str(path)).returncode != 0:
                    # No --force: uncommitted / untracked work makes git refuse.
                    actions.append(f"정리 보류: {path} (remove 실패: dirty?)")
                    continue
                _git(clone, "worktree", "prune")
                removed += 1
                registry = reg_base / slug
                if registry.is_dir() and not _pgrep_free(registry):
                    note = "registry 보류: in-use"
                else:
                    remove_registry(slug)
                    note = "registry 제거"
                actions.append(f"정리 완료: {path} (parked worktree+{note}, "
                               f"idle {int(age // DAY_S)}d)")
            except Exception as e:           # one odd worktree must not end the sweep
                actions.append(f"정리 보류: {path} (error: {type(e).__name__})")
        if overflow:
            actions.append(f"parked worktree 정리 후보 {overflow}개 더 있음 (다음 틱)")
        if unscanned:
            actions.append(f"parked worktree {unscanned}개는 검사하지 않음 "
                           f"(시간 예산 {BUDGET_S}초 초과)")

        reg_removed = 0
        reg_overflow = 0
        for index, (d, m) in enumerate(reg_due):
            if _monotonic() >= deadline:
                actions.append(f"orphan gradle-registry {len(reg_due) - index}개는 "
                               f"검사하지 않음 (시간 예산 {BUDGET_S}초 초과)")
                break
            if not d.exists():
                continue                     # its worktree's removal above took it
            why = _active_reason(rows.get(d.name))
            if why is None and not _pgrep_free(d):
                why = "in-use"
            if why is not None:
                actions.append(f"정리 보류: {d} ({why})")
                continue
            if reg_removed >= MAX_ACTIONS:
                reg_overflow += 1
                continue
            if remove_registry(d.name):
                reg_removed += 1
                actions.append(f"정리 완료: {d} (orphan gradle-registry 제거, "
                               f"idle {int((now - m) // DAY_S)}d)")
            else:
                actions.append(f"정리 보류: {d} (remove 실패)")
        if reg_overflow:
            actions.append(f"orphan gradle-registry 정리 후보 {reg_overflow}개 더 있음 (다음 틱)")
        return actions
    except Exception:
        return []
