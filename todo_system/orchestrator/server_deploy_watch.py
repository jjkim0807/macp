"""Server deploy watch — alert when a merged server change is not in production.

Why: a server fix can be merged to ``main`` and then sit there, because deploying
some apps needs the owner's approval and nothing reminded anyone. One app's server
change sat undeployed for about two days while the app kept calling the old
server. This watch notices that gap and sends ONE alert with the command to run.

What it does, per repo configured in ``<vault>/.macp/server_deploy_watch.json``:

1. Ask ``origin`` for the default branch tip (``ls-remote``), fetch it into the
   runner's checkout only when the commit is missing locally, and find the newest
   commit on that tip that touched the configured server paths (``server/**``).
2. Run the configured READ-ONLY tag command (e.g. a ``gcloud run services
   describe`` that prints the running image) and parse the short sha out of it.
3. Production is *current* when the running commit is that newest server commit
   or a descendant of it. Otherwise it is *behind*: once the oldest undeployed
   server commit is older than the grace period (default 30 min) the watch sends
   one alert naming the repo, the undeployed commits and the deploy command.
4. The alert is throttled per repo and per newest undeployed commit; once
   production runs that commit or newer, the state clears silently.

Contract — the watch NEVER deploys. ``deploy_cmd`` is a display string that only
ever goes into the alert text; no code path here executes it. The only commands
run are read-only git (``_ALLOWED_GIT``) against the runner's checkout under
``paths.worker_root()`` (never checkout/reset/clean — that tree belongs to the
runner) and the configured ``tag_cmd``. A failing tag command (network, auth)
gives at most one "cannot read production tag" alert per repo per day, carrying
the exit code only — its stdout/stderr are never logged or sent.

Config and state are derived from the ``vault_root`` the caller passes (the
orchestrator's ledger lives under it), not from an import-time path, so a tick
running against a temporary vault with no config is silent. State lives in
``<vault>/Worker/_state/server_deploy_watch.json`` (gitignored, derived). A
missing config file means ``run_once`` returns before touching state at all.

Testability: every external effect goes through the injected ``run=`` callable
(resolved from the module global ``_run`` at call time, so tests can monkeypatch
it), and ``now``/``notify``/``worker_root``/``progress`` are injectable too.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from .. import paths
from ..kakao_format import compose_alert

CONFIG_REL = Path(".macp") / "server_deploy_watch.json"
STATE_REL = Path("Worker") / "_state" / "server_deploy_watch.json"

MIN_INTERVAL_S = 600            # whole-run min-poll throttle (10 min)
DEFAULT_GRACE_MIN = 30
_GIT_TIMEOUT = 60
_FETCH_TIMEOUT = 120
_TAG_TIMEOUT = 60
_LIST_LIMIT = 9                 # undeployed commits shown in one alert
_LOG_MAX = 500                  # commits read for the undeployed list
_TAG_RE = re.compile(r"^[0-9a-f]{7,40}$")
_SHA40_RE = re.compile(r"\b[0-9a-f]{40}\b")
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# The only git subcommands this module may run. checkout/reset/clean/worktree/push
# are deliberately absent: the checkout is the runner's working tree.
_ALLOWED_GIT = frozenset({"symbolic-ref", "ls-remote", "cat-file", "fetch",
                          "log", "rev-parse", "merge-base"})

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Notify = Callable[..., None]


def _run(argv: list[str], *, cwd: str | None = None,
         timeout: int | None = None) -> "subprocess.CompletedProcess[str]":
    """Default subprocess runner — the single seam every external effect flows
    through. Never raises on a non-zero exit (callers inspect ``returncode``);
    ``TimeoutExpired``/``OSError`` propagate and are caught by the callers."""
    return subprocess.run(argv, cwd=cwd, timeout=timeout,
                          capture_output=True, text=True, check=False,
                          encoding="utf-8", errors="replace")


def _default_worker_root() -> Path:
    return paths.worker_root()


# ---------------------------------------------------------------------------
# Config (vault-side, git-tracked) + state (gitignored, derived)
# ---------------------------------------------------------------------------

def config_path(vault_root: str | Path) -> Path:
    return Path(vault_root) / CONFIG_REL


def state_path(vault_root: str | Path) -> Path:
    return Path(vault_root) / STATE_REL


def load_config(path: str | Path) -> dict[str, dict]:
    """The raw ``repos`` map, or ``{}`` when the file is missing/corrupt or
    ``repos`` is not a dict — a broken config can only ever skip, never crash."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    repos = data.get("repos") if isinstance(data, dict) else None
    return repos if isinstance(repos, dict) else {}


def validate_entry(name: str, entry: object) -> dict | None:
    """A normalized entry (``server_paths``/``tag_cmd``/``deploy_cmd``/``grace_s``)
    or ``None`` when the entry is unusable (wrong types, missing fields, or a repo
    name that is not a plain directory name)."""
    if not isinstance(name, str) or not _REPO_NAME_RE.match(name) or name in {".", ".."}:
        return None
    if not isinstance(entry, dict):
        return None
    sp = entry.get("server_paths")
    if not isinstance(sp, list) or not sp or not all(isinstance(p, str) and p.strip() for p in sp):
        return None
    tag_cmd = entry.get("tag_cmd")
    if (not isinstance(tag_cmd, list) or not tag_cmd
            or not all(isinstance(a, str) for a in tag_cmd) or not tag_cmd[0].strip()):
        return None
    deploy_cmd = entry.get("deploy_cmd")
    if not isinstance(deploy_cmd, str) or not deploy_cmd.strip():
        return None
    grace = entry.get("grace_minutes", DEFAULT_GRACE_MIN)
    if isinstance(grace, bool) or not isinstance(grace, int) or grace < 0:
        return None
    return {
        "server_paths": [p.strip() for p in sp],
        "tag_cmd": [os.path.expanduser(tag_cmd[0])] + list(tag_cmd[1:]),
        "deploy_cmd": deploy_cmd.strip(),
        "grace_s": grace * 60,
        "grace_minutes": grace,
    }


def load_state(path: str | Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"last_check_ts": None, "repos": {}}
    if not isinstance(data, dict):
        return {"last_check_ts": None, "repos": {}}
    data.setdefault("last_check_ts", None)
    if not isinstance(data.get("repos"), dict):
        data["repos"] = {}
    return data


def save_state(state: dict, path: str | Path) -> bool:
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except OSError:
        return False


def _due(state: dict, now: dt.datetime, *, min_interval_s: float = MIN_INTERVAL_S) -> bool:
    raw = state.get("last_check_ts")
    if not raw:
        return True
    try:
        last = dt.datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if last.tzinfo is not None:
        last = last.astimezone().replace(tzinfo=None)
    return (now - last).total_seconds() >= min_interval_s


# ---------------------------------------------------------------------------
# Read-only git helpers (each returns None on any failure)
# ---------------------------------------------------------------------------

def _git(co: Path, args: list[str], run: Runner, *, timeout: int = _GIT_TIMEOUT):
    """Run one allowed git subcommand in *co*; ``None`` on timeout/OSError."""
    if not args or args[0] not in _ALLOWED_GIT:
        raise ValueError(f"git subcommand not allowed: {args[:1]}")
    try:
        return run(["git", "-C", str(co), *args], timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None


def _ok(r) -> bool:
    return r is not None and getattr(r, "returncode", 1) == 0


def _out(r) -> str:
    return (getattr(r, "stdout", "") or "").strip() if r is not None else ""


def _pathspecs(server_paths: list[str]) -> list[str]:
    return [f":(glob){p}" for p in server_paths]


def _default_branch(co: Path, run: Runner) -> str:
    r = _git(co, ["symbolic-ref", "refs/remotes/origin/HEAD"], run)
    out = _out(r)
    if _ok(r) and out:
        return out.rsplit("/", 1)[-1]
    return "main"


def _remote_tip(co: Path, branch: str, run: Runner) -> str | None:
    r = _git(co, ["ls-remote", "origin", f"refs/heads/{branch}"], run)
    if not _ok(r):
        return None
    m = _SHA40_RE.search(_out(r))
    return m.group(0) if m else None


def _has_commit(co: Path, sha: str, run: Runner) -> bool:
    return _ok(_git(co, ["cat-file", "-e", f"{sha}^{{commit}}"], run))


def _ensure_commit(co: Path, sha: str, branch: str, run: Runner) -> bool:
    """True when *sha* is in the local object store, fetching ``origin <branch>``
    at most once. The fetch updates only remote-tracking refs/objects; the
    worktree, index and HEAD are never touched."""
    if _has_commit(co, sha, run):
        return True
    if not _ok(_git(co, ["fetch", "--quiet", "origin", branch], run, timeout=_FETCH_TIMEOUT)):
        return False
    return _has_commit(co, sha, run)


def _newest_server_commit(co: Path, tip: str, server_paths: list[str],
                          run: Runner) -> str | None:
    """Newest commit reachable from *tip* touching the server paths; ``None``
    on failure or when the paths were never touched."""
    r = _git(co, ["log", "-1", "--format=%H", tip, "--", *_pathspecs(server_paths)], run)
    if not _ok(r):
        return None
    m = _SHA40_RE.search(_out(r))
    return m.group(0) if m else None


def _resolve(co: Path, ref: str, run: Runner) -> str | None:
    r = _git(co, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], run)
    if not _ok(r):
        return None
    m = _SHA40_RE.search(_out(r))
    return m.group(0) if m else None


def _is_ancestor(co: Path, a: str, b: str, run: Runner) -> bool | None:
    """True when *a* is *b* or an ancestor of it, False when not, None on error."""
    r = _git(co, ["merge-base", "--is-ancestor", a, b], run)
    if r is None:
        return None
    rc = getattr(r, "returncode", None)
    if rc == 0:
        return True
    if rc == 1:
        return False
    return None


def _undeployed(co: Path, deployed: str, tip: str, server_paths: list[str],
                run: Runner) -> list[tuple[str, int, str]] | None:
    """Server commits in ``deployed..tip``, newest first, as (sha, ct, subject)."""
    r = _git(co, ["log", f"--max-count={_LOG_MAX}", "--format=%H%x09%ct%x09%s",
                  f"{deployed}..{tip}", "--", *_pathspecs(server_paths)], run)
    if not _ok(r):
        return None
    rows: list[tuple[str, int, str]] = []
    for line in (getattr(r, "stdout", "") or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 2 or not _SHA40_RE.fullmatch(parts[0].strip()):
            continue
        try:
            ct = int(parts[1].strip())
        except ValueError:
            continue
        rows.append((parts[0].strip(), ct, parts[2].strip() if len(parts) > 2 else ""))
    return rows


# ---------------------------------------------------------------------------
# Production tag
# ---------------------------------------------------------------------------

def parse_tag(stdout: str | None) -> str | None:
    """The short sha in the tag command's output, or ``None``.

    Accepts a bare sha (``abc1234``) or a full image ref
    (``<registry>/app/server:abc1234``, optionally ``@sha256:…``): last non-empty
    line → drop any digest → text after the last ``/`` → text after the last
    ``:``. The result must be 7–40 hex characters."""
    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    if not lines:
        return None
    s = lines[-1].split("@", 1)[0]
    s = s.rsplit("/", 1)[-1]
    if ":" in s:
        s = s.rsplit(":", 1)[-1]
    s = s.strip().lower()
    return s if _TAG_RE.match(s) else None


def read_prod_tag(entry: dict, run: Runner) -> tuple[str | None, str]:
    """Run the READ-ONLY tag command. Returns ``(tag, rc_token)``; the output is
    never returned beyond the parsed tag, so it can never reach a log or alert."""
    try:
        r = run(list(entry["tag_cmd"]), timeout=_TAG_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "oserror"
    rc = getattr(r, "returncode", None)
    if rc != 0:
        return None, str(rc)
    tag = parse_tag(getattr(r, "stdout", "") or "")
    if tag is None:
        return None, "0(unparseable)"
    return tag, "0"


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def _compose_behind_alert(name: str, entry: dict, deployed: str,
                          undeployed: list[tuple[str, int, str]]) -> str:
    newest = undeployed[0][0]
    n = len(undeployed)
    headline = (f"🚨 [서버 미배포] {name} — main의 서버 변경 {n}건이 "
                f"{entry['grace_minutes']}분 넘게 운영에 반영되지 않았어요")
    lines = [
        f"운영 태그: {deployed[:7]}",
        f"배포 명령: {entry['deploy_cmd'].replace('<sha>', newest[:7])}",
    ]
    for sha, _ct, subject in undeployed[:_LIST_LIMIT]:
        lines.append(f"{sha[:7]} {subject}".rstrip())
    if n > _LIST_LIMIT:
        lines.append(f"외 {n - _LIST_LIMIT}건")
    return compose_alert(headline, lines)


def _compose_tag_alert(name: str, rc: str) -> str:
    return compose_alert(
        f"⚠️ [서버 배포 감시] {name} 운영 태그를 읽을 수 없음 (rc={rc}) — 오늘은 더 알리지 않음")


# ---------------------------------------------------------------------------
# One repo
# ---------------------------------------------------------------------------

def check_repo(name: str, entry: dict, *, co: Path, run: Runner, now: dt.datetime,
               repo_state: dict, notify: Notify,
               progress: Callable[[str], None]) -> str | None:
    """Check one repo; mutates *repo_state*. Returns *name* when a behind alert
    was sent this call, else ``None``. Never deploys."""
    branch = _default_branch(co, run)
    tip = _remote_tip(co, branch, run)
    if tip is None:
        progress(f"server deploy watch: {name} ls-remote failed — skipped")
        return None
    if not _ensure_commit(co, tip, branch, run):
        progress(f"server deploy watch: {name} fetch of {tip[:7]} failed — skipped")
        return None
    newest = _newest_server_commit(co, tip, entry["server_paths"], run)
    if newest is None:
        return None                    # server paths never touched → nothing to watch

    tag, rc = read_prod_tag(entry, run)
    if tag is None:
        day = now.date().isoformat()
        if repo_state.get("tag_fail_day") == day:
            progress(f"server deploy watch: {name} cannot read production tag (rc={rc}) — already alerted today")
            return None
        repo_state["tag_fail_day"] = day
        progress(f"server deploy watch: {name} cannot read production tag (rc={rc})")
        notify(_compose_tag_alert(name, rc),
               event_key=f"server-tag-unreadable:{name}:{day}")
        return None

    deployed = _resolve(co, tag, run)
    if deployed is None:
        progress(f"server deploy watch: {name} production tag {tag} not in the clone — incomparable, no alert")
        return None

    current = _is_ancestor(co, newest, deployed, run)
    if current is None:
        progress(f"server deploy watch: {name} ancestry check failed — skipped")
        return None
    if current:
        if repo_state.pop("alerted_sha", None) is not None:
            repo_state.pop("alerted_ts", None)
            progress(f"server deploy watch: {name} production caught up ({deployed[:7]}) — cleared")
        return None

    undeployed = _undeployed(co, deployed, tip, entry["server_paths"], run)
    if not undeployed:
        progress(f"server deploy watch: {name} behind but undeployed list unreadable — skipped")
        return None
    newest_undeployed = undeployed[0][0]
    alerted = repo_state.get("alerted_sha")
    if alerted == newest_undeployed:
        return None                    # this commit was already alerted
    # First alert of an episode waits on the OLDEST undeployed commit; a newer
    # merge after an alert waits on its own age, so a burst gives one alert per
    # settled commit rather than one per tick.
    ref_ct = undeployed[-1][1] if alerted is None else undeployed[0][1]
    age_s = now.timestamp() - ref_ct
    if age_s < entry["grace_s"]:
        progress(f"server deploy watch: {name} behind by {len(undeployed)} server commit(s), within grace")
        return None
    notify(_compose_behind_alert(name, entry, deployed, undeployed),
           event_key=f"server-undeployed:{name}:{newest_undeployed[:12]}")
    repo_state["alerted_sha"] = newest_undeployed
    repo_state["alerted_ts"] = now.isoformat(timespec="seconds")
    progress(f"server deploy watch: {name} alerted — {len(undeployed)} undeployed server commit(s)")
    return name


# ---------------------------------------------------------------------------
# Entry point (called from Orchestrator._server_deploy_watch_tick)
# ---------------------------------------------------------------------------

def run_once(vault_root: str | Path, *, notify: Notify | None = None,
             now: dt.datetime | None = None, run: Runner | None = None,
             worker_root: str | Path | None = None,
             progress: Callable[[str], None] = print) -> list[str]:
    """Check every configured repo once (10-min min-poll). Returns the repos a
    behind alert went out for. Missing/empty config → ``[]`` with no state write."""
    cfg = config_path(vault_root)
    if not cfg.exists():
        return []
    repos = load_config(cfg)
    if not repos:
        return []
    now = now or dt.datetime.now()
    run = run or _run
    notify = notify or (lambda msg, *, event_key=None: progress(msg))
    sp = state_path(vault_root)
    state = load_state(sp)
    if not _due(state, now):
        return []
    state["last_check_ts"] = now.isoformat(timespec="seconds")
    wr = Path(worker_root) if worker_root is not None else _default_worker_root()

    alerted: list[str] = []
    for name, raw in repos.items():
        if isinstance(name, str) and name.startswith("_"):
            continue                   # `_readme`-style annotations
        entry = validate_entry(name, raw)
        if entry is None:
            progress(f"server deploy watch: invalid config entry {name!r} — skipped")
            continue
        co = wr / name
        if not (co / ".git").exists():
            progress(f"server deploy watch: {name} has no checkout under the worker root — skipped")
            continue
        repo_state = state["repos"].setdefault(name, {})
        try:
            hit = check_repo(name, entry, co=co, run=run, now=now,
                             repo_state=repo_state, notify=notify, progress=progress)
        except Exception as e:     # one repo must never stop the others
            progress(f"server deploy watch: {name} check failed (fail-open): {type(e).__name__}")
            continue
        if hit:
            alerted.append(hit)

    for gone in [k for k in state["repos"] if k not in repos]:
        del state["repos"][gone]
    save_state(state, sp)
    return alerted
