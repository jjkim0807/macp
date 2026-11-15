"""Maintenance-tick public-repo watch — external issues/PRs become human tasks.

An external contributor's issue on the public engine repo once sat open for 17
days with nobody noticing: nothing in the engine ever looked at the repo's issue
or PR list, and GitHub's own notification mail drowned in everything else.

``watch_tick`` is a step of the 5-minute maintenance tick
(``worker.cmd_maintenance``), but it queries GitHub **once a day**:

* every watched repo's open issues and PRs are read with ``gh`` — one
  ``gh api user`` per run (the maintainer = the authenticated account) plus one
  ``gh api graphql`` per repo, so never more than 2 calls per repo;
* an item is **external** unless the maintainer opened it, or it is a PR whose
  head branch starts with ``worker/`` or lives in the same repo (not a fork —
  only collaborators can push there);
* an external item with no maintainer comment or review yet opens an
  ``ops-repo-issue-<n>`` / ``ops-repo-pr-<n>`` human task through
  ``ops_alerts.open_alert`` (the dashboard's "직접 할 일"). Title, author, link,
  created date and the first 200 chars of the body go in the task; nothing more;
* a maintainer comment or review, or the item leaving the open list (closed or
  merged), resolves the task through ``ops_alerts.resolve_alert``. A later
  comment from the outside author does **not** reopen it;
* a key whose task file already exists is never re-opened: an open task stays as
  it is (no daily ``재발생`` churn) and a task a person marked done stays done.

Fail-safe by contract: a ``gh`` failure (auth, network, timeout, bad JSON, a
GraphQL error) is one ``repo-watch: … gh 실패 …`` line and **closes nothing** —
"absent from the list" is only believed for a repo whose fetch succeeded and
whose list was complete (not cut off at the first page). A failed run is retried
after FAIL_RETRY_S, not on every tick. The step never raises into the tick;
``worker.cmd_maintenance`` wraps it in its own try/except as a second belt.

정상이면 무음: a run that opened and resolved nothing prints nothing.

Watched repos, first match wins:

  1. env MACP_REPO_WATCH_REPOS — comma-separated ``owner/name``;
  2. ``repo_watch_repos`` in the nearest ``.macp/config.toml`` (same string form);
  3. the engine checkout's ``git remote get-url origin``;
  4. the installed distribution's ``Project-URL: Repository`` metadata.

Nothing resolvable → one skip line, at most once a day. The first repo's keys are
``repo-issue-<n>`` / ``repo-pr-<n>``; any further repo's keys carry its name
(``repo-issue-<n>-<name>``) because issue numbers collide across repos.

Environment (read at call time):

  MACP_REPO_WATCH            "0" switches the step off entirely.
  MACP_REPO_WATCH_STATE_DIR  state dir; default ~/.local/share/macp/repo-watch.
  MACP_REPO_WATCH_REPOS      watched repos (see above).

Stdlib plus ``ops_alerts`` / ``paths`` only. For a look at what the next tick
would watch (no gh call, no write):

    python -m todo_system.maintenance_repo_watch --dry-run
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

from . import ops_alerts, paths

KILL_ENV = "MACP_REPO_WATCH"
STATE_DIR_ENV = "MACP_REPO_WATCH_STATE_DIR"
REPOS_ENV = "MACP_REPO_WATCH_REPOS"
CONFIG_KEY = "repo_watch_repos"
DIST_NAME = "macp"

RUN_INTERVAL_S = 24 * 3600      # one GitHub read a day
FAIL_RETRY_S = 3600             # a failed run retries after an hour, not every tick
GH_TIMEOUT_S = 60
GIT_TIMEOUT_S = 10
EXCERPT_CHARS = 200             # the body is never copied past this
TITLE_CHARS = 80                # title inside the task's heading / outcome
TEXT_LIMIT = 300                # the one line, and any free text in a step
REASON_CHARS = 120              # a gh error inside the line
PAGE = 100                      # GraphQL first:N (the API maximum)
COMMENTS_LAST = 30              # comments/reviews scanned for a maintainer reply

ACTION = "확인·답글·머지 여부 결정"

# Seams, all read at CALL time — never frozen into a default argument — so the
# suite-wide fixture in todo_system/conftest.py can point every one at tmp.
STATE_DIR: Path | None = None    # None → $MACP_REPO_WATCH_STATE_DIR, else the default
CONFIG_START: Path | None = None  # None → paths.vault_root_or_cwd()
_run = subprocess.run            # bound at import (disk_guard's rule): tests replace it
_now = time.time

_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_URL_RE = re.compile(
    r"^(?:https?://(?:[^@/\s]+@)?github\.com/|ssh://git@github\.com/|git@github\.com:)"
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
_LINK_RE = re.compile(r"https://github\.com/([^/\s]+/[^/\s]+)/(issues|pull)/(\d+)")

QUERY = (
    "query($owner:String!,$name:String!){repository(owner:$owner,name:$name){"
    f"issues(states:OPEN,first:{PAGE},orderBy:{{field:CREATED_AT,direction:DESC}})"
    "{totalCount nodes{number title url createdAt body author{login} "
    f"comments(last:{COMMENTS_LAST}){{nodes{{author{{login}}}}}}}}}}"
    f"pullRequests(states:OPEN,first:{PAGE},orderBy:{{field:CREATED_AT,direction:DESC}})"
    "{totalCount nodes{number title url createdAt body headRefName isCrossRepository "
    f"author{{login}} comments(last:{COMMENTS_LAST}){{nodes{{author{{login}}}}}} "
    f"reviews(last:{COMMENTS_LAST}){{nodes{{author{{login}}}}}}}}}}}}}}"
)

KINDS = (("issues", "issue", "이슈"), ("pullRequests", "pr", "PR"))
_LINK_KIND = {"issues": "issue", "pull": "pr"}


# -- config -------------------------------------------------------------------

def enabled() -> bool:
    """Only an explicit "0" disables the step (``maintenance_disk.enabled``'s rule)."""
    return os.environ.get(KILL_ENV, "").strip() != "0"


def state_dir() -> Path:
    if STATE_DIR is not None:
        return Path(STATE_DIR)
    env = os.environ.get(STATE_DIR_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "macp" / "repo-watch"


def state_path() -> Path:
    return state_dir() / "repo_watch_state.json"


def _parse_repo_url(url: str | None) -> str | None:
    """``owner/name`` from a GitHub remote URL (https, https with user, scp-like
    ssh, ssh://), else None."""
    m = _URL_RE.match(str(url or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _dist_repo_url() -> str | None:
    """The installed distribution's ``Project-URL: Repository`` (covers a wheel /
    uv-git install, whose engine root has no ``.git``). Never raises."""
    try:
        for entry in importlib.metadata.metadata(DIST_NAME).get_all("Project-URL") or []:
            label, _, url = str(entry).partition(",")
            if label.strip().lower() == "repository" and url.strip():
                return url.strip()
    except Exception:                    # noqa: BLE001 - metadata is a best-effort fallback
        return None
    return None


def _config_value() -> str | None:
    start = Path(CONFIG_START) if CONFIG_START is not None else paths.vault_root_or_cwd()
    base = start.resolve()
    for d in (base, *base.parents):
        cfg = d / ".macp" / "config.toml"
        if cfg.is_file():
            return paths._read_config_key(cfg, CONFIG_KEY)
    return None


def _origin_repo() -> str | None:
    try:
        cp = _run(["git", "-C", str(paths.engine_root()), "remote", "get-url", "origin"],
                  capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
    except Exception:                    # noqa: BLE001 - no git / no checkout → next rung
        return None
    if cp.returncode != 0:
        return None
    return _parse_repo_url((cp.stdout or "").strip())


def _split_repos(value: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for part in str(value).split(","):
        slug = part.strip()
        if not _SLUG_RE.match(slug) or slug.casefold() in seen:
            continue
        seen.add(slug.casefold())
        out.append(slug)
    return out


def watched_repos() -> tuple[list[str], str | None]:
    """(repos, skip reason). The reason is set only when the list is empty."""
    env = os.environ.get(REPOS_ENV, "").strip()
    if env:
        repos = _split_repos(env)
        return (repos, None) if repos else ([], f"{REPOS_ENV}에 유효한 owner/name 없음")
    cfg = _config_value()
    if cfg:
        repos = _split_repos(cfg)
        return (repos, None) if repos else ([], f"{CONFIG_KEY}에 유효한 owner/name 없음")
    for found in (_origin_repo(), _parse_repo_url(_dist_repo_url())):
        if found:
            return [found], None
    return [], "감시할 GitHub 리포를 정하지 못함 (origin·메타데이터 모두 GitHub 아님)"


# -- state --------------------------------------------------------------------

_STATE_KEYS = ("last_run_ts", "last_attempt_ts")


def _read_state() -> dict:
    state = {k: 0.0 for k in _STATE_KEYS}
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
    """None on success, else a short reason. Atomic replace, 0600 file, 0700 dir."""
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        p = state_path()
        tmp = p.with_name(p.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({k: state[k] for k in _STATE_KEYS}, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except OSError as e:
        return f"state write failed ({type(e).__name__})"
    return None


def _cooling(now: float, last: float, window: float) -> bool:
    """``maintenance_embed._cooling``: ``last <= 0`` (never) and a stamp in the
    future (the clock stepped back) never suppress."""
    if last <= 0:
        return False
    return 0 <= now - last < window


def _due(now: float, state: dict) -> bool:
    return (not _cooling(now, state["last_run_ts"], RUN_INTERVAL_S)
            and not _cooling(now, state["last_attempt_ts"], FAIL_RETRY_S))


# -- gh -----------------------------------------------------------------------

def _gh_json(args: list[str]) -> tuple[dict | None, str | None]:
    """(parsed JSON object, None) or (None, short reason). Never raises."""
    try:
        cp = _run(["gh", *args], capture_output=True, text=True, timeout=GH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None, f"timeout {GH_TIMEOUT_S}s"
    except FileNotFoundError:
        return None, "gh 없음"
    except Exception as e:               # noqa: BLE001 - any runner failure is a gh failure
        return None, f"{type(e).__name__}: {e}"
    if cp.returncode != 0:
        first = ((cp.stderr or "").strip().splitlines() or [f"rc={cp.returncode}"])[0]
        return None, first
    try:
        data = json.loads(cp.stdout or "")
    except ValueError:
        return None, "JSON 파싱 실패"
    if not isinstance(data, dict):
        return None, "JSON 형식 아님"
    return data, None


def _maintainer() -> tuple[str | None, str | None]:
    data, err = _gh_json(["api", "user"])
    if err:
        return None, err
    login = data.get("login")
    if not isinstance(login, str) or not login.strip():
        return None, "gh api user에 login 없음"
    return login.strip(), None


def _fetch(repo: str) -> tuple[dict | None, str | None]:
    owner, name = repo.split("/", 1)
    data, err = _gh_json(["api", "graphql", "-f", f"query={QUERY}",
                          "-f", f"owner={owner}", "-f", f"name={name}"])
    if err:
        return None, err
    if data.get("errors"):
        first = data["errors"][0] if isinstance(data["errors"], list) else data["errors"]
        msg = first.get("message") if isinstance(first, dict) else first
        return None, f"GraphQL 오류: {msg}"
    repository = (data.get("data") or {}).get("repository")
    if not isinstance(repository, dict):
        return None, "repository 없음"
    for field, _kind, _label in KINDS:
        conn = repository.get(field)
        if not isinstance(conn, dict) or not isinstance(conn.get("nodes"), list):
            return None, f"{field} 응답 형식 아님"
    return repository, None


# -- items --------------------------------------------------------------------

def _clean(value: object, limit: int) -> str:
    """One safe line: control chars → space, ``"`` → ``'``, ``\\`` → ``/``,
    whitespace collapsed, cut to ``limit`` chars (``…`` included)."""
    s = "" if value is None else str(value)
    s = "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c for c in s)
    s = " ".join(s.replace('"', "'").replace("\\", "/").split())
    if len(s) > limit:
        s = s[: max(limit - 1, 0)].rstrip() + "…"
    return s


def _login(node: object) -> str:
    author = node.get("author") if isinstance(node, dict) else None
    login = author.get("login") if isinstance(author, dict) else None
    return login if isinstance(login, str) and login else "ghost"


def _logins(node: dict, field: str) -> list[str]:
    conn = node.get(field)
    nodes = conn.get("nodes") if isinstance(conn, dict) else None
    return [_login(n).casefold() for n in nodes or [] if isinstance(n, dict)]


def _responded(node: dict, maintainer: str) -> bool:
    m = maintainer.casefold()
    return m in _logins(node, "comments") or m in _logins(node, "reviews")


def _excluded(node: dict, kind: str, maintainer: str) -> bool:
    if _login(node).casefold() == maintainer.casefold():
        return True
    if kind == "pr":
        head = str(node.get("headRefName") or "")
        if head.startswith("worker/") or node.get("isCrossRepository") is not True:
            return True
    return False


def _key(repo: str, kind: str, number: int, primary: bool) -> str:
    base = f"repo-{kind}-{number}"
    return base if primary else f"{base}-{ops_alerts._slug(repo.split('/', 1)[1])}"


def _steps(kind_label: str, node: dict) -> list[str]:
    n = node.get("number")
    steps = [
        f"{ACTION} — {_clean(node.get('url'), TEXT_LIMIT)}",
        f"{kind_label} #{n} 「{_clean(node.get('title'), TITLE_CHARS)}」 — "
        f"작성자 @{_clean(_login(node), 60)}, 만든 날짜 {_clean(node.get('createdAt'), 40)[:10]}",
    ]
    excerpt = _clean(node.get("body"), EXCERPT_CHARS)
    if excerpt:
        steps.append(f"본문 첫 {EXCERPT_CHARS}자: {excerpt}")
    return steps


def _task_exists(key: str) -> bool:
    return ops_alerts._path(key).exists()


def _open_repo_tasks() -> list[tuple[str, str, str, int]]:
    """(key, repo, kind, number) for every OPEN ``ops-repo-*`` task, read from the
    link in its ``next_action``. Read-only and stateless: a lost state file
    orphans nothing."""
    out = []
    items = ops_alerts.vault_root() / "Todo" / "items"
    if not items.is_dir():
        return out
    for path in sorted(items.glob("ops-repo-*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if ops_alerts._read_field(text, "status") != "open":
            continue
        m = _LINK_RE.search(ops_alerts._read_field(text, "next_action") or "")
        if not m:
            continue
        out.append((path.stem[len("ops-"):], m.group(1), _LINK_KIND[m.group(2)],
                    int(m.group(3))))
    return out


def _sync_repo(repo: str, repository: dict, maintainer: str, primary: bool,
               open_tasks: list[tuple[str, str, str, int]]) -> tuple[int, int]:
    """Open/resolve for one successfully fetched repo. (opened, resolved)."""
    opened = resolved = 0
    for field, kind, label in KINDS:
        conn = repository[field]
        nodes = [n for n in conn["nodes"] if isinstance(n, dict)]
        open_numbers = set()
        for node in nodes:
            try:
                number = int(node.get("number"))
            except (TypeError, ValueError):
                continue
            open_numbers.add(number)
            if _excluded(node, kind, maintainer):
                continue
            key = _key(repo, kind, number, primary)
            if _responded(node, maintainer):
                if ops_alerts._read_field(_read(key), "status") == "open":
                    ops_alerts.resolve_alert(key, "관리자 응답 확인")
                    resolved += 1
                continue
            if _task_exists(key):
                continue                 # open: no churn · done by a person: stays done
            title = (f"공개 리포 {repo} {label} #{number}: "
                     f"{_clean(node.get('title'), TITLE_CHARS)}")
            ops_alerts.open_alert(key, title, _steps(label, node))
            if _task_exists(key):
                opened += 1
        try:
            total = int(conn.get("totalCount"))
        except (TypeError, ValueError):
            continue                     # unknown completeness → never resolve by absence
        if total > len(nodes):
            continue                     # cut off at the first page → absence proves nothing
        for key, task_repo, task_kind, number in open_tasks:
            if (task_repo.casefold() == repo.casefold() and task_kind == kind
                    and number not in open_numbers):
                if ops_alerts._read_field(_read(key), "status") == "open":
                    ops_alerts.resolve_alert(key, "닫힘 또는 머지됨")
                    resolved += 1
    return opened, resolved


def _read(key: str) -> str:
    try:
        return ops_alerts._path(key).read_text(encoding="utf-8")
    except OSError:
        return ""


# -- the step -----------------------------------------------------------------

def watch_tick(*, now: float | None = None) -> str | None:
    """The maintenance step. Returns the line to print, or None (disabled, not
    due, or nothing changed). Never raises."""
    try:
        if not enabled():
            return None
        t = _now() if now is None else now
        state = _read_state()
        if not _due(t, state):
            return None
        # Stamped BEFORE any gh call, so a crash cannot become a call per tick.
        state["last_attempt_ts"] = t
        err = _write_state(state)
        if err:
            return f"repo-watch: {err} — 건너뜀"
        repos, reason = watched_repos()
        if reason:
            state["last_run_ts"] = t
            err = _write_state(state)
            line = f"repo-watch: {_clean(reason, TEXT_LIMIT)} — 건너뜀"
            return f"{line}; {err}" if err else line
        maintainer, err = _maintainer()
        if err:
            return (f"repo-watch: gh 실패 (gh api user: {_clean(err, REASON_CHARS)}) — "
                    "기존 태스크 유지, 1h 뒤 재시도")
        open_tasks = _open_repo_tasks()
        opened = resolved = 0
        failures: list[str] = []
        for i, repo in enumerate(repos):
            repository, err = _fetch(repo)
            if err:
                failures.append(f"{repo}: {_clean(err, REASON_CHARS)}")
                continue                 # neither open nor resolve on a failed fetch
            o, r = _sync_repo(repo, repository, maintainer, i == 0, open_tasks)
            opened += o
            resolved += r
        if not failures:
            state["last_run_ts"] = t
            err = _write_state(state)
        else:
            err = None
        parts = []
        if opened or resolved:
            parts.append(f"새 항목 {opened}건 열림, {resolved}건 해소")
        if failures:
            parts.append(f"gh 실패 {'; '.join(failures)} — 기존 태스크 유지, 1h 뒤 재시도")
        if err:
            parts.append(err)
        if not parts:
            return None
        return f"repo-watch: {' · '.join(parts)}"[: TEXT_LIMIT * 2]
    except Exception as e:               # noqa: BLE001 - must never break maintenance
        return f"repo-watch: error ({_clean(f'{type(e).__name__}: {e}', TEXT_LIMIT)}) — skipped"


# -- entry point ---------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """``--dry-run``: what the next tick would watch. No gh call, no write."""
    ap = argparse.ArgumentParser(
        prog="python -m todo_system.maintenance_repo_watch",
        description="Show what the maintenance public-repo watch would do. Read-only.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the watched repos and the schedule; call no gh, write nothing")
    args = ap.parse_args(argv)
    if not args.dry_run:
        print("only --dry-run is supported: the watch itself runs inside the maintenance "
              "tick (worker.cmd_maintenance)", file=sys.stderr)
        return 2
    print(f"kill switch {KILL_ENV}: "
          + ("off (watch enabled)" if enabled() else "ON — the watch is disabled"))
    repos, reason = watched_repos()
    if reason:
        print(f"watched repos: none — {reason}")
    for i, repo in enumerate(repos):
        print(f"watched repo: {repo}" + (" (primary: keys repo-issue-<n> / repo-pr-<n>)"
                                         if i == 0 else ""))
    state = _read_state()
    t = _now()
    print(f"state file: {state_path()}")
    for key in _STATE_KEYS:
        print(f"  {key}: {state[key]:.0f}")
    if _due(t, state):
        print("next tick: due — it would call gh")
    else:
        nxt = max(state["last_run_ts"] + RUN_INTERVAL_S if _cooling(
            t, state["last_run_ts"], RUN_INTERVAL_S) else 0,
            state["last_attempt_ts"] + FAIL_RETRY_S if _cooling(
            t, state["last_attempt_ts"], FAIL_RETRY_S) else 0)
        print(f"next tick: not due for {int(nxt - t)}s")
    open_tasks = _open_repo_tasks()
    print(f"open ops-repo tasks: {len(open_tasks)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
