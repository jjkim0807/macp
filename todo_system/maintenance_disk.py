"""Maintenance-tick cache prune — keep the root disk off the floor.

On 2026-11-03 the HQ host's root filesystem (96G) had 90G used and 6.4G free.
The run-start disk guard (orchestrator/disk_guard.py) only runs `gradlew clean`
in idle ~/Worker clones and only below 3 GiB, at a run start. What was actually
eating the disk were caches that Gradle and uv re-download on demand: old
`~/.gradle/caches/<version>/` trees for Gradle versions no clone uses any more,
stale `~/.gradle/caches/build-cache-1/` entries, and uv's own cache.

`check_and_prune` is a step of the 5-minute maintenance tick
(`worker.cmd_maintenance`):

* free ≥ MIN_FREE (8 GiB): nothing at all. One statvfs, no subprocess, no
  state write, no output (정상이면 무음 — and no mailbox churn).
* below it, at most once per PASS_INTERVAL_S, one prune pass:
  1. `uv cache prune` (uv's own command, under uv's own lock), then
  2. old Gradle version caches whose version nothing currently uses, then old
     build-cache entries, oldest first, until free ≥ TARGET (MIN_FREE + 2 GiB)
     or the pass budget runs out. Active runs no longer block this: only the
     version set in use is protected (see below), while build-cache entries,
     each an atomic single-file delete, always prune.
  Exactly one `disk-prune:` line then names the usage before → after, what was
  removed, what was skipped and why, and whether free space is STILL below.
* still below after the pass: one page through the caller's notifier, at most
  once per ALERT_COOLDOWN_S. The state is written BEFORE notifying, so a
  broken notifier cannot turn into one page per tick.

What may be deleted — the allowlist `_check_allowed` enforces on the realpath
of every target, and the only two deleting calls in this module sit behind it
in `_delete`:

* `<gradle home>/caches/<version>/` (a directory whose name is a Gradle
  version) when that version is in neither the ~/Worker wrappers nor any live
  Gradle daemon, or when it is but nothing at depth ≤ 2 below it changed for
  MAX_AGE_DAYS (default 30). When the protected version set cannot be read (an
  unparseable wrapper, a version-less or unreadable Gradle daemon, or no /proc),
  no version directory is a candidate — build-cache still prunes.
* `<gradle home>/caches/build-cache-1/<hash>` (a regular file with a hex name)
  unchanged for MAX_AGE_DAYS.

Never touched: `caches/modules-2`, `<gradle home>/wrapper` (every
distribution), any other `caches/*` entry, the ~/Worker clones and worktrees,
the vault, ~/.config, the browser caches under ~/.cache and the other
protected home directories listed in `_protected_roots`. Symlinks are refused,
not followed.

Protected versions (version caches only; build-cache and uv are unaffected):
the Gradle versions the ~/Worker wrappers name, unioned with the versions live
Gradle daemons in /proc are running (parsed from their dist/launcher path). A
`caches/<version>` dir is kept iff its name is in that set. The set is
recomputed before each deletion, so a daemon that starts mid-pass protects its
version from that point on. It is fail-closed: an unparseable wrapper, a Gradle
daemon whose version cannot be read, or no /proc makes the set unknown and keeps
every version dir. The ledger (`_holders`) no longer gates any deletion — an
active run by itself protects nothing.

uv lock holders: when `uv cache prune` times out or exits non-zero, the skip
reason names the live processes whose argv[0] is `uv` (uv run/sync/cache all
take uv's cache lock), e.g. `uv=timed out (90s); lock held by: uv run python
trading_loop.py`.

Fail-open by contract: the step never raises into the tick. A timeout, a
missing `uv` or an odd exit code becomes a skip reason.

Environment (read at call time):

  MACP_DISK_PRUNE                 "0" switches the step off entirely.
  MACP_DISK_PRUNE_MIN_FREE_GIB    trigger and alert threshold; default 8.
  MACP_DISK_PRUNE_TARGET_GIB      stop deleting at this much free; default
                                  trigger + 2 (never below the trigger).
  MACP_DISK_PRUNE_MAX_AGE_DAYS    N for "unused for N days"; default 30.
  MACP_DISK_PRUNE_STATE_DIR       state dir; default ~/.local/share/macp/disk.
  MACP_UV_BIN                     uv binary; default $UV, then PATH, then
                                  ~/.local/bin/uv.
  GRADLE_USER_HOME                Gradle home; default ~/.gradle.

Stdlib plus stdlib-only siblings. For a read-only look at the real host (no
deletion, no uv):

    python -m todo_system.maintenance_disk --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import ops_alerts, paths
from .kakao_format import compose_alert
from .orchestrator import disk_guard

GIB = disk_guard.GIB
DEFAULT_MIN_FREE_GIB = 8.0
TARGET_GAP_GIB = 2.0           # default target = trigger + this (hysteresis)
DEFAULT_MAX_AGE_DAYS = 30.0
PASS_INTERVAL_S = 1800         # while still low, one pass per 30 min, not per tick
ALERT_COOLDOWN_S = 24 * 3600   # check_work_starvation's cadence
UV_TIMEOUT_S = 90
BUDGET_S = 120                 # one pass, uv included
MAX_DELETIONS = 5000           # one pass, version dirs and entries together
TEXT_LIMIT = 400               # chars of the skip list in the summary line
UV_HOLDER_MAX = 5              # how many uv lock-holder cmdlines to name
UV_HOLDER_CHARS = 120          # per-holder cmdline cap
VERSION_DEPTH = 2              # how deep "last used" looks below caches/<version>
UV_ARGS = ("cache", "prune")

KILL_ENV = "MACP_DISK_PRUNE"
MIN_FREE_ENV = "MACP_DISK_PRUNE_MIN_FREE_GIB"
TARGET_ENV = "MACP_DISK_PRUNE_TARGET_GIB"
MAX_AGE_ENV = "MACP_DISK_PRUNE_MAX_AGE_DAYS"
STATE_DIR_ENV = "MACP_DISK_PRUNE_STATE_DIR"
UV_BIN_ENV = "MACP_UV_BIN"

VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?(-[0-9A-Za-z.]+)?$")
HASH_RE = re.compile(r"^[0-9a-f]{32,}$")
_VER = r"\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.]+)?"
_DIST_RE = re.compile(r"gradle-(" + _VER + r")-(?:bin|all)\.zip")
WRAPPER_GLOBS = ("*/gradle/wrapper/gradle-wrapper.properties",
                 "*.wt/*/gradle/wrapper/gradle-wrapper.properties")
GRADLE_MARKERS = (b"GradleDaemon", b"GradleWrapperMain", b"GradleWorkerMain")
# A live Gradle JVM names its version in its cmdline — the distribution dir
# (`gradle-8.7-bin`), the launcher jar (`gradle-launcher-8.7.jar`), or the
# exploded dist (`gradle-8.7/lib`). Tried in that order on the raw cmdline bytes.
_DAEMON_VERSION_RES = (
    re.compile((r"gradle-(" + _VER + r")-(?:bin|all)").encode()),
    re.compile((r"gradle-launcher-(" + _VER + r")\.jar").encode()),
    re.compile((r"gradle-(" + _VER + r")/lib").encode()),
)

# Seams, all read at CALL time — never frozen into a default argument — so the
# suite-wide fixture in todo_system/conftest.py can point every one at a tmp dir.
HOME_DIR: Path | None = None          # None → Path.home()
GRADLE_HOME_DIR: Path | None = None   # None → $GRADLE_USER_HOME, else <home>/.gradle
STATE_DIR: Path | None = None         # None → $MACP_DISK_PRUNE_STATE_DIR, else default
VAULT_ROOT: Path | None = None        # None → paths.vault_root()
PROC_DIR = "/proc"
_disk_usage = shutil.disk_usage
# Bound at import on purpose (disk_guard's rule): suites that patch the global
# `subprocess.run` must never see this step's uv call.
_run = subprocess.run
_which = shutil.which
_clock = time.monotonic
_now = time.time


class Refused(ValueError):
    """`_check_allowed` refused a deletion target; the message says why."""


# -- config ------------------------------------------------------------------

def enabled() -> bool:
    """Only an explicit "0" disables the step (disk_guard.enabled's rule)."""
    return os.environ.get(KILL_ENV, "").strip() != "0"


def _env_pos(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return value if value > 0 else default


def min_free_bytes() -> int:
    return int(_env_pos(MIN_FREE_ENV, DEFAULT_MIN_FREE_GIB) * GIB)


def target_bytes(lo: int | None = None) -> int:
    lo = min_free_bytes() if lo is None else lo
    return max(lo, int(_env_pos(TARGET_ENV, lo / GIB + TARGET_GAP_GIB) * GIB))


def max_age_days() -> float:
    return _env_pos(MAX_AGE_ENV, DEFAULT_MAX_AGE_DAYS)


def home_dir() -> Path:
    return Path(HOME_DIR) if HOME_DIR is not None else Path.home()


def gradle_home(home: Path | None = None) -> Path:
    if GRADLE_HOME_DIR is not None:
        return Path(GRADLE_HOME_DIR)
    env = os.environ.get("GRADLE_USER_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    return (home if home is not None else home_dir()) / ".gradle"


def state_dir() -> Path:
    if STATE_DIR is not None:
        return Path(STATE_DIR)
    env = os.environ.get(STATE_DIR_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    return home_dir() / ".local" / "share" / "macp" / "disk"


def state_path() -> Path:
    return state_dir() / "prune_state.json"


def vault_root() -> Path:
    return Path(VAULT_ROOT) if VAULT_ROOT is not None else paths.vault_root()


def uv_bin() -> str | None:
    for env in (UV_BIN_ENV, "UV"):          # `uv run` exports UV=<its own binary>
        value = os.environ.get(env, "").strip()
        if value:
            return value
    found = _which("uv")
    if found:
        return found
    local = home_dir() / ".local" / "bin" / "uv"
    return str(local) if os.access(local, os.X_OK) else None


# -- measurement -------------------------------------------------------------

def _gib(n: int | float) -> str:
    return disk_guard._gib(n)


def _pct(free: int, total: int) -> str:
    return f"{round(100 * (total - free) / total)}" if total > 0 else "?"


def measure(gh: Path) -> tuple[int, int] | None:
    """(free, total) of the filesystem holding the Gradle home, or None."""
    return disk_guard.measure(gh, disk_usage=_disk_usage)


# -- which versions are in use (protected) -----------------------------------

def wrapper_versions(worker_root: str | Path) -> tuple[set[str] | None, str]:
    """Gradle versions the ~/Worker clones' wrappers name — ({…}, "") — or
    (None, why) when any wrapper file cannot be read or parsed, or none exists.
    None makes every version directory a keeper (fail-closed)."""
    root = Path(worker_root)
    files = sorted({p for pattern in WRAPPER_GLOBS for p in root.glob(pattern)})
    if not files:
        return None, f"no gradle-wrapper.properties under {root}"
    found: set[str] = set()
    for f in files:
        rel = os.path.relpath(f, root)
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return None, f"{rel}: {type(e).__name__}"
        version = None
        for line in text.splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "distributionUrl":
                m = _DIST_RE.search(value)
                version = m.group(1) if m else None
        if version is None:
            return None, f"{rel}: no distributionUrl version"
        found.add(version)
    return found, ""


def _daemon_version(cmdline: bytes) -> str | None:
    """The Gradle version named in a JVM `cmdline`, or None if none parses."""
    for rx in _DAEMON_VERSION_RES:
        m = rx.search(cmdline)
        if m:
            return m.group(1).decode()
    return None


def gradle_daemon_versions() -> tuple[set[str] | None, str]:
    """Versions the live Gradle JVMs in /proc run — ({…}, "") — or (None, why)
    when /proc is unreadable or a Gradle JVM names no parseable version. None is
    fail-closed: an unknown version means we cannot tell which caches/<version>
    dir it protects, so every version dir is then kept."""
    try:
        names = os.listdir(PROC_DIR)
    except OSError:
        return None, "no /proc"
    me = os.getpid()
    versions: set[str] = set()
    for name in sorted((n for n in names if n.isdigit()), key=int):
        if int(name) == me:
            continue
        try:
            with open(os.path.join(PROC_DIR, name, "cmdline"), "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if not any(marker in data for marker in GRADLE_MARKERS):
            continue
        version = _daemon_version(data)
        if version is None:
            return None, f"gradle JVM pid {name} version unknown"
        versions.add(version)
    return versions, ""


def protected_versions() -> tuple[set[str] | None, str]:
    """Gradle versions a running build may need: the ~/Worker wrapper versions
    unioned with the live Gradle daemon versions. (None, why) when either source
    is unknown — fail-closed, every version dir is then kept."""
    wrapper, why = wrapper_versions(disk_guard.WORKER_ROOT)
    if wrapper is None:
        return None, why
    daemons, why = gradle_daemon_versions()
    if daemons is None:
        return None, why
    return wrapper | daemons, ""


# -- candidates ----------------------------------------------------------------

def _newest_mtime(path: Path, depth: int = VERSION_DEPTH) -> float:
    newest = os.lstat(path).st_mtime
    level = [path]
    for _ in range(depth):
        deeper: list[Path] = []
        for d in level:
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            st = e.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        newest = max(newest, st.st_mtime)
                        if e.is_dir(follow_symlinks=False):
                            deeper.append(Path(e.path))
            except OSError:
                continue
        level = deeper
    return newest


def classify_versions(gh: Path, current: set[str] | None, max_age_s: float,
                      now: float) -> list[tuple[Path, str | None]]:
    """Every `caches/<version>` dir with None (prune it) or why it is kept.
    Prunable ones come first: non-current before current, then oldest first."""
    caches = gh / "caches"
    try:
        entries = list(os.scandir(caches))
    except OSError:
        return []
    rows: list[tuple[int, float, str, Path, str | None]] = []
    for e in entries:
        if not VERSION_RE.match(e.name):
            continue
        p = Path(e.path)
        if e.is_symlink() or not e.is_dir(follow_symlinks=False):
            rows.append((2, 0.0, e.name, p, "not a plain directory"))
            continue
        try:
            newest = _newest_mtime(p)
        except OSError as err:
            rows.append((2, 0.0, e.name, p, f"unreadable ({type(err).__name__})"))
            continue
        age_d = (now - newest) / 86400
        if current is None:
            rows.append((2, newest, e.name, p, "wrapper set unknown"))
        elif e.name not in current:
            rows.append((0, newest, e.name, p, None))
        elif now - newest > max_age_s:
            rows.append((1, newest, e.name, p, None))
        else:
            rows.append((2, newest, e.name, p, f"current wrapper version, used {age_d:.0f}d ago"))
    rows.sort(key=lambda r: (r[0], r[1], r[2]))
    return [(p, why) for _, _, _, p, why in rows]


def version_candidates(gh: Path, current: set[str] | None, max_age_s: float,
                       now: float) -> list[Path]:
    return [p for p, why in classify_versions(gh, current, max_age_s, now) if why is None]


def build_cache_candidates(gh: Path, max_age_s: float, now: float) -> list[Path]:
    """Hex-named regular files directly in `caches/build-cache-1`, unchanged
    for longer than `max_age_s`, oldest first."""
    found: list[tuple[float, str, Path]] = []
    try:
        with os.scandir(gh / "caches" / "build-cache-1") as it:
            for e in it:
                if not HASH_RE.match(e.name) or not e.is_file(follow_symlinks=False):
                    continue
                try:
                    mtime = e.stat(follow_symlinks=False).st_mtime
                except OSError:
                    continue
                if now - mtime > max_age_s:
                    found.append((mtime, e.name, Path(e.path)))
    except OSError:
        return []
    return [p for _, _, p in sorted(found)]


# -- the allowlist and the one deleting function -------------------------------

def _protected_roots(gh: Path, home: Path, vault: Path) -> list[tuple[str, str]]:
    """(realpath, label) of every root a deletion may neither enter nor contain."""
    roots = [
        (gh / "caches" / "modules-2", "gradle modules-2"),
        (gh / "wrapper", "gradle wrapper"),
        (home / "Worker", "~/Worker"),
        (Path(disk_guard.WORKER_ROOT), "worker root"),
        (vault, "vault"),
        (home / ".config", "~/.config"),
        (home / ".cache" / "ms-playwright", "~/.cache/ms-playwright"),
        (home / ".cache" / "macp-browser", "~/.cache/macp-browser"),
    ]
    # Live-trading checkouts sit beside ~/Worker; spelt as data, not prose.
    roots += [(home / name, f"~/{name}") for name in ("Trading",)]
    return [(disk_guard._real(p), label) for p, label in roots]


def _check_allowed(path: str | Path, *, gradle_home: str | Path, home: str | Path,
                   vault_root: str | Path) -> str:
    """"version" or "entry" when `path` may be deleted; raises Refused otherwise."""
    gh_real = disk_guard._real(gradle_home)
    home_real = disk_guard._real(home)
    if gh_real == os.sep or disk_guard._inside(home_real, gh_real):
        raise Refused(f"gradle home {gh_real} is / or contains the home dir")
    lex = Path(os.path.abspath(os.fspath(path)))
    gh_lex = Path(os.path.abspath(os.fspath(gradle_home)))
    try:
        rel = lex.relative_to(gh_lex)
    except ValueError:
        raise Refused(f"{lex} is outside the gradle home") from None
    step = gh_lex
    for part in rel.parts:
        step = step / part
        if os.path.islink(step):
            raise Refused(f"symlink {step}")
    real = disk_guard._real(lex)
    parent, name = os.path.split(real)
    caches = os.path.join(gh_real, "caches")
    if parent == caches and VERSION_RE.match(name) and os.path.isdir(real):
        kind = "version"
    elif (parent == os.path.join(caches, "build-cache-1") and HASH_RE.match(name)
          and os.path.isfile(real)):
        kind = "entry"
    else:
        raise Refused(f"{real} is not a gradle version cache or build-cache entry")
    for root, label in _protected_roots(Path(gradle_home), Path(home), Path(vault_root)):
        if disk_guard._inside(real, root) or disk_guard._inside(root, real):
            raise Refused(f"{real} touches protected {label}")
    return kind


def _tree_size(path: str) -> int:
    total = 0
    stack = [path]
    while stack:
        p = stack.pop()
        try:
            st = os.lstat(p)
        except OSError:
            continue
        total += st.st_blocks * 512 if hasattr(st, "st_blocks") else st.st_size
        if os.path.isdir(p) and not os.path.islink(p):
            try:
                stack.extend(os.path.join(p, n) for n in os.listdir(p))
            except OSError:
                continue
    return total


def _delete(path: str | Path, *, gradle_home: str | Path, home: str | Path,
            vault_root: str | Path) -> int:
    """Delete one allowlisted cache path; bytes freed (best effort). The ONLY
    deleting calls in this module are the two below, behind _check_allowed."""
    kind = _check_allowed(path, gradle_home=gradle_home, home=home, vault_root=vault_root)
    real = disk_guard._real(path)
    size = _tree_size(real)
    if kind == "version":
        shutil.rmtree(real)
    else:
        os.unlink(real)
    return size


# -- uv ------------------------------------------------------------------------

def _uv_lock_holders() -> list[str]:
    """Cmdline prefixes of live processes whose argv[0] basename is `uv` — uv
    run/sync/cache all take uv's cache lock. Best-effort; fail-open on OSError."""
    try:
        names = os.listdir(PROC_DIR)
    except OSError:
        return []
    me = os.getpid()
    holders: list[str] = []
    for name in sorted((n for n in names if n.isdigit()), key=int):
        if int(name) == me:
            continue
        try:
            with open(os.path.join(PROC_DIR, name, "cmdline"), "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        parts = [p for p in data.split(b"\0") if p]
        if not parts or os.path.basename(parts[0].decode("utf-8", "replace")) != "uv":
            continue
        cmd = " ".join(p.decode("utf-8", "replace") for p in parts)
        holders.append(cmd[:UV_HOLDER_CHARS])
        if len(holders) >= UV_HOLDER_MAX:
            break
    return holders


def _with_lock_holders(reason: str) -> str:
    """Append `; lock held by: …` to a uv skip reason when any holder is live."""
    holders = _uv_lock_holders()
    return reason + "; lock held by: " + "; ".join(holders) if holders else reason


def _uv_prune(budget_left: float) -> str | None:
    """`uv cache prune`: None on success, else the skip reason (naming the uv
    lock holders on a timeout or a non-zero exit)."""
    uv = uv_bin()
    if not uv:
        return "uv=not found"
    timeout = max(1.0, min(UV_TIMEOUT_S, budget_left))
    try:
        r = _run([uv, *UV_ARGS], stdin=subprocess.DEVNULL, capture_output=True,
                 text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return _with_lock_holders(f"uv=timed out ({int(timeout)}s)")
    except OSError as e:
        return f"uv={type(e).__name__}"
    if r.returncode == 0:
        return None
    return _with_lock_holders(f"uv=rc {r.returncode}")


# -- state ---------------------------------------------------------------------

def _read_state() -> dict:
    state = {"last_pass_ts": 0.0, "last_alert_ts": 0.0}
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if isinstance(data, dict):
        for key in state:
            try:
                state[key] = float(data.get(key, 0.0))
            except (TypeError, ValueError):
                pass
    return state


def _write_state(state: dict) -> str | None:
    """None on success, else a short reason (the line carries it)."""
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        p = state_path()
        tmp = p.with_name(p.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"last_pass_ts": state["last_pass_ts"],
                       "last_alert_ts": state["last_alert_ts"]}, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except OSError as e:
        return f"state write failed ({type(e).__name__})"
    return None


def _cooling(now: float, last: float, window: float) -> bool:
    """Whether the previous notice is recent enough to suppress this one.

    ``last <= 0`` means "never notified" and never suppresses — spelt out rather
    than left to ``now - 0.0`` exceeding the window, which only holds because
    real clocks return ~1.8e9. Under an injected clock its first notice would
    otherwise be swallowed. A stamp in the future (the clock stepped back) does
    not suppress either."""
    if last <= 0:
        return False
    return 0 <= now - last < window


# -- one pass ------------------------------------------------------------------

def _clip(items: list[str], limit: int = TEXT_LIMIT) -> str:
    return disk_guard._clip(items, limit)


def prune_pass(*, gh: Path, home: Path, vault: Path, free: int, total: int,
               lo: int, target: int, now: float) -> dict:
    """uv first, then non-protected Gradle version caches and stale build-cache
    entries until free ≥ target. Never raises for one bad target; the caller
    catches anything else."""
    res: dict = {"free_before": free, "free_after": free, "total": total,
                 "threshold": lo, "target": target, "uv_ok": False,
                 "versions": [], "entries": 0, "skipped": [], "stopped": None, "notes": []}
    start = _clock()

    def left() -> float:
        return BUDGET_S - (_clock() - start)

    def remeasure() -> None:
        m = measure(gh)
        if m is not None:
            res["free_after"] = m[0]

    why = _uv_prune(left())
    if why is None:
        res["uv_ok"] = True
    else:
        res["skipped"].append(why)
    remeasure()
    if res["free_after"] >= target:
        return res
    max_age_s = max_age_days() * 86400
    current, why = protected_versions()
    if current is None:
        res["skipped"].append(f"versions=protected set unknown ({why})")
    deletions = 0
    kw = {"gradle_home": gh, "home": home, "vault_root": vault}

    def can_go_on() -> bool:
        if res["free_after"] >= target:
            return False
        if left() <= 0:
            res["stopped"] = "budget exhausted"
            return False
        if deletions >= MAX_DELETIONS:
            res["stopped"] = "deletion cap"
            return False
        return True

    for v in version_candidates(gh, current, max_age_s, now):
        if not can_go_on():
            break
        live, _ = protected_versions()  # a daemon for v can start mid-pass
        if live is None or v.name in live:
            res["skipped"].append(f"{v.name}=now protected")
            continue
        try:
            _delete(v, **kw)
        except (Refused, OSError) as e:
            res["skipped"].append(f"{v.name}={type(e).__name__}"
                                  + (f" ({e})" if isinstance(e, Refused) else ""))
            continue
        deletions += 1
        res["versions"].append(v.name)
        remeasure()
    if res["stopped"] or res["free_after"] >= target:
        return res
    errors: dict[str, int] = {}
    for entry in build_cache_candidates(gh, max_age_s, now):
        if not can_go_on():
            break
        try:
            _delete(entry, **kw)
        except (Refused, OSError) as e:
            errors[type(e).__name__] = errors.get(type(e).__name__, 0) + 1
            continue
        deletions += 1
        res["entries"] += 1
        remeasure()
    if errors:
        res["skipped"].append("build-cache=" + ", ".join(f"{n} {k}" for k, n in sorted(errors.items())))
    return res


def summarize(res: dict) -> str:
    """The single line of a pass. Ends with `— STILL below <trigger>` iff so."""
    lo, total = res["threshold"], res["total"]
    fb, fa = res["free_before"], res["free_after"]
    line = (f"disk-prune: free {_gib(fb)} → {_gib(fa)} of {_gib(total)} "
            f"({_pct(fb, total)}% → {_pct(fa, total)}% used, trigger <{_gib(lo)}, "
            f"target {_gib(res['target'])}) — ")
    parts: list[str] = []
    if res["uv_ok"]:
        parts.append("uv cache prune ok")
    if res["versions"]:
        parts.append("removed versions " + _clip(list(res["versions"])))
    if res["entries"]:
        parts.append(f"removed {res['entries']} build-cache entries")
    if not parts:
        parts.append("removed nothing")
    if res["skipped"]:
        parts.append("skipped " + _clip(list(res["skipped"])))
    if res["stopped"]:
        parts.append(f"stopped: {res['stopped']}")
    parts.extend(res["notes"])
    line += "; ".join(parts)
    if fa < lo:
        line += f" — STILL below {_gib(lo)}"
    return line


def compose_low_disk_alert(res: dict) -> str:
    total = res["total"]
    return compose_alert("💾⚠️ 본부 루트 디스크 여유 부족 — 캐시 정리 후에도 기준 미만", lines=[
        f"여유 {_gib(res['free_after'])} / {_gib(total)} "
        f"(사용 {_pct(res['free_after'], total)}%, 기준 {_gib(res['threshold'])})",
        f"정리: uv {'ok' if res['uv_ok'] else '건너뜀'}, gradle 버전 {len(res['versions'])}개, "
        f"build-cache {res['entries']}개",
        "다음: df -h / 와 du -sh ~/.gradle ~/.cache ~/Worker 로 큰 디렉토리 확인",
    ])


# -- entry point ---------------------------------------------------------------

def check_and_prune(*, notify: Callable[[str], object] | None = None,
                    now: float | None = None) -> str | None:
    """The maintenance step. Returns the line to print, or None (healthy,
    disabled, or a pass ran less than PASS_INTERVAL_S ago). Never raises."""
    try:
        if not enabled():
            return None
        home = home_dir()
        gh = gradle_home(home)
        lo = min_free_bytes()
        target = target_bytes(lo)
        m = measure(gh)
        if m is None:
            return f"disk-prune: free space unreadable under {gh} — skipped"
        free, total = m
        if free >= lo:
            # healthy: silent — no subprocess, no state write. resolve_alert is a
            # one-stat no-op unless a disk ops task is still open, in which case
            # closing it IS the recovery (the only write this path ever makes).
            ops_alerts.resolve_alert("hq-disk", "여유 회복")
            return None
        t = _now() if now is None else now
        state = _read_state()
        if _cooling(t, state["last_pass_ts"], PASS_INTERVAL_S):
            return None
        state["last_pass_ts"] = t
        # Stamped before acting, so a pass that dies cannot rerun every tick.
        state_err = _write_state(state)
        res = prune_pass(gh=gh, home=home, vault=vault_root(), free=free, total=total,
                         lo=lo, target=target, now=t)
        if state_err:
            res["notes"].append(state_err)
        if res["free_after"] < lo and not _cooling(t, state["last_alert_ts"], ALERT_COOLDOWN_S):
            state["last_alert_ts"] = t
            err = _write_state(state)    # BEFORE notify: a broken notifier pages once
            if err and err not in res["notes"]:
                res["notes"].append(err)
            if notify is not None:
                try:
                    notify(compose_low_disk_alert(res))
                    res["notes"].append("paged")
                except Exception as e:   # noqa: BLE001 - paging is best-effort
                    res["notes"].append(f"notify failed ({type(e).__name__})")
            # Tie the human task to the same 24h alert cadence (idempotent, fail-open).
            ops_alerts.open_alert(
                "hq-disk", "본부 루트 디스크 여유 부족",
                ["df -h / 로 여유 확인",
                 "du -sh ~/.gradle ~/.cache ~/Worker 로 큰 디렉토리 확인",
                 "필요 시 수동 정리 — 여유가 회복되면 이 태스크는 자동으로 닫힌다"],
                level="alert")
        return summarize(res)
    except Exception as e:               # noqa: BLE001 - must never break maintenance
        return f"disk-prune: error ({type(e).__name__}: {e})"[:300] + " — skipped"


def main(argv: list[str] | None = None) -> int:
    """`--dry-run`: measure, classify, print. Never deletes, never runs uv."""
    ap = argparse.ArgumentParser(
        prog="python -m todo_system.maintenance_disk",
        description="Show what the maintenance cache prune would do. Read-only.")
    ap.add_argument("--dry-run", action="store_true",
                    help="measure free space and classify every cache candidate; delete nothing")
    args = ap.parse_args(argv)
    if not args.dry_run:
        print("only --dry-run is supported: the prune itself runs inside the maintenance "
              "tick (worker.cmd_maintenance)", file=sys.stderr)
        return 2
    home = home_dir()
    gh = gradle_home(home)
    lo = min_free_bytes()
    target = target_bytes(lo)
    print(f"gradle home: {gh}")
    m = measure(gh)
    if m is None:
        print("free space: unreadable")
    else:
        free, total = m
        verdict = ("below the trigger — a tick would prune" if free < lo
                   else "healthy — a tick stays silent")
        print(f"free space: {_gib(free)} of {_gib(total)} ({_pct(free, total)}% used, "
              f"trigger <{_gib(lo)}, target {_gib(target)}) — {verdict}")
    print(f"kill switch {KILL_ENV}: "
          + ("off (prune enabled)" if enabled() else "ON — the prune is disabled"))
    uv = uv_bin()
    print(f"uv: {uv} — a pass runs `uv cache prune`" if uv else "uv: not found — skipped")
    current, why = protected_versions()
    print("protected versions: " + (", ".join(sorted(current)) if current is not None
                                     else f"UNKNOWN ({why}) — every version dir is kept"))
    days = max_age_days()
    now = _now()
    try:
        vault = vault_root()
    except Exception as e:               # noqa: BLE001 - dry run reports, never dies
        vault = None
        print(f"vault root: unresolved ({type(e).__name__}) — every deletion would be refused")
    kw = {"gradle_home": gh, "home": home, "vault_root": vault}
    for p, why in classify_versions(gh, current, days * 86400, now):
        if why is None and vault is not None:
            try:
                _check_allowed(p, **kw)
            except Refused as e:
                why = f"refused: {e}"
        print(f"  caches/{p.name}: " + ("would remove" if why is None else f"keep: {why}"))
    entries = build_cache_candidates(gh, days * 86400, now)
    print(f"  caches/build-cache-1: {len(entries)} entries unchanged for >{days:g}d "
          f"({_gib(sum(_tree_size(str(e)) for e in entries))}) would be removed oldest first")
    return 0


if __name__ == "__main__":
    sys.exit(main())
