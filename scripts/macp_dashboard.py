#!/usr/bin/env python3
"""MaestroACP live status dashboard — a tiny local web page showing what the
control plane is doing right now.

    uv run python scripts/macp_dashboard.py            # serve on :8787, open browser
    uv run python scripts/macp_dashboard.py --port 9000 --no-open
    MACP_DASHBOARD_TOKEN=... uv run python scripts/macp_dashboard.py --host 0.0.0.0

Stdlib-only HTTP server. `/` serves the HTML shell; `/api/status` serves fresh
JSON that the page polls every few seconds; `/api/run?id=<run dir>` serves one
run's detail (stage rcs, artifact markdown, run.log tail) and `/api/task?slug=`
one task's (todo fields, ledger, task body, its runs) for the tap-through view.
`POST /api/sms` is the phone's write-only SMS ingest (own `MACP_SMS_TOKEN`,
stored by `todo_system.sms`, never in the vault); a rejected ingest
(401/429/400/413) raises one notifier alert per kind per 6h
(`todo_system.sms.should_alert`). Reads the vault directly (ledger
assignments, run logs, agent liveness, todo.db) — no daemon changes.

Remote access: the vault is private (task titles, personal admin), so binding to
anything but loopback REQUIRES a shared secret in `MACP_DASHBOARD_TOKEN`. Open
`/?token=<secret>` once — it sets an HttpOnly cookie and every later request
(including the page's own `/api/status` polling) rides on it. `Authorization:
Bearer <secret>` works too. On loopback the token is optional. The intended
production shape on the HQ host is loopback + `tailscale serve` (HTTPS on the
tailnet); the token gate is the safety net for any other exposure path.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import glob
import hmac
import json
import logging
import os
import re
import sqlite3
import subprocess
import threading
import time
import webbrowser
from contextlib import contextmanager
from html import escape
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

_INSTALL_DIR = Path(__file__).resolve().parents[1]
# Run as `python scripts/macp_dashboard.py` puts scripts/ on sys.path, not the repo
# root — so `import todo_system` (registry/tasks enrichment) would silently fail.
import sys as _sys  # noqa: E402 — sys.path is set just below on purpose
if str(_INSTALL_DIR) not in _sys.path:
    _sys.path.insert(0, str(_INSTALL_DIR))
from todo_system import paths  # noqa: E402
from todo_system import sms  # noqa: E402
from todo_system import not_before as _nb  # noqa: E402 — alias: is_deferred is taken below
from todo_system.orchestrator.assignment import display_state, is_deferred  # noqa: E402
# Engine root through the resolver: dev checkout -> repo root (same value as the
# bootstrap above); wheel install -> todo_system/_engine, where scripts/ and
# .claude/commands ship in repo layout. The bootstrap only puts the engine on
# sys.path; this is the definition the rest of the module uses.
_INSTALL_DIR = paths.engine_root()
# Data root through the resolver: honours MACP_VAULT_ROOT / .macp/config.toml.
# The start= anchor is paths.vault_search_start(): the engine root in a checkout
# (byte-identical to the former Path(__file__).resolve().parent.parent when
# unset), None in an installed wheel so the cwd-upward search runs instead of
# anchoring on site-packages, which never holds a vault marker.
# When unset VAULT == _INSTALL_DIR (already on sys.path), so the guard below is a
# no-op and live behaviour is unchanged; an env redirect only adds the data root.
VAULT = paths.vault_root(start=paths.vault_search_start())
if str(VAULT) not in _sys.path:
    _sys.path.insert(0, str(VAULT))

ASSIGN = VAULT / "Worker" / "assignments"
RUNS = VAULT / "Worker" / "runs"
STATE = VAULT / "Worker" / "_state"
TODO_DB = VAULT / "Todo" / "todo.db"
ITEMS = VAULT / "Todo" / "items"
SESSIONS = VAULT / "Memory" / "sessions"
REGISTRY = VAULT / "Worker" / "agents" / "registry.yaml"

ACTIVE = ("dispatched", "in_progress")
# The default runner identity (systemd unit runs `--agent worker-default`). The
# server decides `active[].agent_is_default` from it so the web/app can drop the
# noisy "agent …" row line unless a non-default agent is running the task.
DEFAULT_RUNNER_AGENT = "worker-default"
STAGE_RE = re.compile(r"=== (\w+) \(rc=(\d+)\) ===")


# ---- collectors ------------------------------------------------------------

def _age_secs(ts: str) -> float | None:
    """Seconds since an ISO timestamp (None if unparseable)."""
    try:
        t = dt.datetime.fromisoformat(ts)
        now = dt.datetime.now(t.tzinfo) if t.tzinfo else dt.datetime.now()
        return (now - t).total_seconds()
    except (TypeError, ValueError):
        return None


def _iso_age(ts: str) -> str:
    """'3m', '2h', '1d' since an ISO timestamp (or '' if unparseable)."""
    s = _age_secs(ts)
    if s is None:
        return ""
    if s < 90:
        return f"{int(s)}s"
    if s < 5400:
        return f"{int(s / 60)}m"
    if s < 172800:
        return f"{int(s / 3600)}h"
    return f"{int(s / 86400)}d"


def _worker_running() -> list[str]:
    """One marker per claude stage process running right now (best-effort via pgrep).

    `-a`, NOT `-l`: with `-f`, procps-ng's `-l` prints only the process NAME, so a
    live Implement stage comes back as "1696592 claude" — the `"claude -p" in line`
    filter below never matched and `worker_busy` was pinned False forever. The badge
    therefore read "유휴 (idle)" even with a stage 28 minutes into its run (observed
    2026-09-20). `-a` prints the full command line, which is what the filter needs.

    The matched command lines are NOT returned: a stage's argv carries the entire
    prompt, and the caller only takes bool() of this. One sanitized marker per match
    keeps the count without dragging prompt text into /api/status.
    """
    try:
        out = subprocess.run(["pgrep", "-af", "claude -p"], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return []
    return ["claude -p" for line in out.splitlines() if "claude -p" in line]


# ---- worker-state derivation (why "idle"?) ---------------------------------
# The badge used to say only "유휴 (idle)". These helpers + the pure function
# below answer *why* it is idle. The two checkout helpers mirror
# orchestrator.py:_checkout_key / _checkout_label (2-line each) deliberately —
# importing the orchestrator here would drag the whole engine in and cross the
# engine-isolation gate; the duplication is pinned by tests instead.

_VAULT_CHECKOUT = "<vault>"

# worker_state enum, in priority order (first match wins). Kept in sync with the
# acceptance ladder: paused > hold > budget > running > post_run > waiting_serial
# > no_work.
WORKER_STATES = (
    "paused", "hold", "budget", "running",
    "post_run", "waiting_serial", "no_work",
)


def _checkout_key(payload: dict | None) -> str:
    """Which working tree a task runs in. External repos key on their clone URL
    (one checkout per repo); everything else keys on the vault. Mirror of
    orchestrator.py:_checkout_key."""
    return str((payload or {}).get("repo") or "") or _VAULT_CHECKOUT


def _checkout_label(key: str) -> str:
    """Short human name for a checkout key: the repo name of a clone URL
    (`.../some-app.git` -> `some-app`), else the key. Mirror of
    orchestrator.py:_checkout_label."""
    tail = key.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail.removesuffix(".git") or key


def determine_worker_state(
    *,
    stage_count: int,
    in_flight: list[dict],
    dispatchable_checkouts: list[str],
    paused: bool,
    usage_allowed: bool,
    runs_today: int,
    budget: int | None,
    cap: int = 1,
) -> tuple[str, str]:
    """Why is the worker in the state it is? Pure — no file/process/db I/O.

    Returns ``(worker_state, worker_state_detail)`` where ``worker_state`` is one
    of ``WORKER_STATES`` and ``worker_state_detail`` is a one-line Korean gloss
    carrying no prompt text or secrets.

    Inputs:
      - ``stage_count``: number of live claude stage processes.
      - ``in_flight``: in-flight ledger rows, each ``{"checkout", "label",
        "note"}`` (note already truncated upstream, prompt/secret-free).
      - ``dispatchable_checkouts``: checkout key of each auto-dispatchable
        candidate.
      - ``paused``: PAUSED file present.
      - ``usage_allowed``: usage-guard decision (``False`` == HOLD).
      - ``runs_today`` / ``budget``: daily_run_budget pacing; ``budget`` is
        ``None`` or ``0`` when unknown / unlimited (then ``budget`` never fires).
      - ``cap``: ``max_parallel_per_repo`` — how many runs may share ONE checkout
        at once. A checkout is "contended" (someone waits) only when its queued
        candidates EXCEED ``cap``. Defaults to 1 (serial), so an unknown profile
        keeps the historical behaviour.

    Priority ladder (first match wins): paused > hold > budget > running >
    post_run > waiting_serial > no_work.
    """
    if paused:
        return "paused", "일시정지(PAUSED) — 새 작업 디스패치 중단"
    if not usage_allowed:
        return "hold", "usage guard HOLD — 사용량 한도로 대기"
    if budget and runs_today >= budget:
        return "budget", f"일일 실행 예산 도달 ({runs_today}/{budget}) — 리셋까지 대기"
    if stage_count >= 1:
        return "running", f"워커 실행 중 — claude 스테이지 {stage_count}개"
    if in_flight:
        # stage 0 but a ledger row is still live: beta build / merge
        # post-processing finishing up. This is the salient state whether or not
        # candidates are queued behind it, so it sits above waiting_serial in the
        # ladder (real case ② scenario A: in-flight beta + same-repo candidate).
        labels = [r.get("label") or "?" for r in in_flight]
        if len(in_flight) == 1:
            note = (in_flight[0].get("note") or "")[:40]
            tail = f" — {note}" if note else ""
            return "post_run", f"{labels[0]} 후처리 중{tail}"
        uniq = sorted(set(labels))
        return "post_run", f"{len(in_flight)}건 후처리 중 ({', '.join(uniq)})"
    if dispatchable_checkouts:
        # stage 0, nothing in-flight: the orchestrator has not dispatched yet.
        # Candidates that collide on one checkout cannot all go (serial per
        # repo) — some wait on the others (real case ② scenario B: same-repo
        # candidates only). Distinct free checkouts are a pre-tick transition;
        # still waiting_serial per the 7-enum, with a detail saying dispatch is
        # imminent (see plan §design, transition case).
        waiters: dict[str, int] = {}
        for k in dispatchable_checkouts:
            waiters[_checkout_label(k)] = waiters.get(_checkout_label(k), 0) + 1
        contended = {lbl: n for lbl, n in waiters.items() if n > cap}
        if contended:
            parts = ", ".join(f"{lbl} {n}건"
                              for lbl, n in sorted(contended.items()))
            return "waiting_serial", f"직렬 대기 — {parts}가 한 체크아웃을 공유 (repo당 최대 {cap}개)"
        return "waiting_serial", f"다음 틱에 디스패치 예정 ({len(dispatchable_checkouts)}건 대기)"
    return "no_work", "디스패치 가능한 태스크 없음"


def collect_assignments() -> list[dict]:
    rows = []
    for f in glob.glob(str(ASSIGN / "*.json")):
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            continue
        ckey = _checkout_key(d.get("payload"))
        rows.append({
            "task": d.get("task_id", "?"),
            # Which working tree this row occupies — additive fields the
            # worker-state derivation reads; no existing consumer removes them.
            "checkout": ckey,
            "checkout_label": _checkout_label(ckey),
            # Display state: a FAILED row that is only waiting (checkout_busy /
            # job_running) reads "deferred", so it never lands in `attention`.
            # `deferred` repeats it as a bool for a client (the Android app)
            # that predates the new state value.
            "state": display_state(d) or "?",
            "deferred": is_deferred(d),
            "agent": d.get("agent_id", ""),
            "refail": d.get("refail_count", 0),
            "capacity_limited": bool(d.get("capacity_limited")),
            "note": (d.get("last_note") or "")[:90],
            "updated": d.get("updated_ts", ""),
            "age": _iso_age(d.get("updated_ts", "")),
        })
    rows.sort(key=lambda r: r["updated"], reverse=True)
    return rows


def _run_summary(p: Path) -> dict:
    """One run dir → {run, slug, when, stages, artifacts} (the list-row shape)."""
    name = p.name
    slug = re.sub(r"^\d{8}T\d{6}-", "", name)
    log = p / "run.log"
    stages = []
    if log.exists():
        try:
            txt = log.read_text(encoding="utf-8", errors="replace")
        except Exception:
            txt = ""
        for m in STAGE_RE.finditer(txt):
            stages.append({"name": m.group(1), "rc": int(m.group(2))})
    arts = [a.name for a in p.iterdir() if a.suffix == ".md"] if p.exists() else []
    return {"run": name, "slug": slug, "when": name[9:15], "stages": stages,
            "artifacts": arts}


def collect_runs(limit: int = 14) -> list[dict]:
    dirs = sorted(glob.glob(str(RUNS / "2026*")), reverse=True)[:limit]
    return [_run_summary(Path(d)) for d in dirs]


# A run id is exactly a Worker/runs/ directory name — anything else (path
# separators, `..`, dotfiles) is rejected before touching the filesystem.
RUN_ID_RE = re.compile(r"^\d{8}T\d{6}-[A-Za-z0-9][A-Za-z0-9._-]*$")
# Stage artifacts in pipeline order; any other *.md in the run dir follows.
ARTIFACT_ORDER = ("quick.md", "delegate.md", "requirements.md",
                  "implementation_plan.md", "verify.md", "report.md")
ARTIFACT_MAX = 60_000   # chars per artifact sent to the client
LOG_TAIL_MAX = 8_000    # chars from the END of run.log (the latest stage is last)


@contextmanager
def _ro_conn(db_path: Path):
    """Read-only sqlite connection on `db_path`; yields None if the file doesn't
    exist. Guarantees close() even if the caller's queries raise (the callers
    below used to close only on the success path, leaking a connection on any
    mid-query exception)."""
    if not db_path.exists():
        yield None
        return
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        yield con
    finally:
        con.close()


def _task_row(slug: str) -> dict:
    """The todo.db row of one task (empty dict if absent/unreadable)."""
    try:
        with _ro_conn(TODO_DB) as con:
            if con is None:
                return {}
            row = con.execute(
                "SELECT status, outcome, next_action, worker_mode, title, due, if_then, updated"
                " FROM tasks WHERE slug = ?", (slug,)).fetchone()
    except Exception:
        return {}
    if not row:
        return {}
    return {"status": row[0] or "", "outcome": row[1] or "",
            "next_action": row[2] or "", "worker_mode": row[3] or "",
            "title": row[4] or "", "due": row[5] or "", "if_then": row[6] or "",
            "updated": row[7] or ""}


def _ledger_row(slug: str) -> dict:
    """The task's worker-ledger entry (empty dict if none/unreadable)."""
    af = ASSIGN / f"{slug}.json"
    try:
        d = json.loads(af.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {"state": display_state(d), "deferred": is_deferred(d),
            "agent": d.get("agent_id", ""),
            "refail": d.get("refail_count", 0),
            "note": d.get("last_note") or "",
            "updated": d.get("updated_ts", ""),
            "age": _iso_age(d.get("updated_ts", ""))}


def collect_run_detail(run_id: str) -> dict | None:
    """Everything one run left behind — stage rcs, the stage artifacts' markdown,
    the tail of run.log, and the task/ledger context — for the tap-through detail
    view. None when the id is malformed or the run dir doesn't exist."""
    if not RUN_ID_RE.match(run_id or ""):
        return None
    p = RUNS / run_id
    if not p.is_dir():
        return None
    slug = re.sub(r"^\d{8}T\d{6}-", "", run_id)

    def _read(f: Path) -> str:
        try:
            return f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ""

    stages, log_tail, log_truncated = [], "", False
    log = p / "run.log"
    if log.exists():
        txt = _read(log)
        stages = [{"name": m.group(1), "rc": int(m.group(2))} for m in STAGE_RE.finditer(txt)]
        log_truncated = len(txt) > LOG_TAIL_MAX
        log_tail = txt[-LOG_TAIL_MAX:]

    mds = sorted((f for f in p.iterdir() if f.suffix == ".md" and f.is_file()),
                 key=lambda f: (ARTIFACT_ORDER.index(f.name)
                                if f.name in ARTIFACT_ORDER else len(ARTIFACT_ORDER), f.name))
    artifacts = []
    for f in mds:
        body = _read(f)
        artifacts.append({"name": f.name, "size": len(body),
                          "content": body[:ARTIFACT_MAX],
                          "truncated": len(body) > ARTIFACT_MAX})

    ledger = _ledger_row(slug)

    date = run_id[:8]
    return {
        "run": run_id, "slug": slug, "when": run_id[9:15],
        "date": f"{date[:4]}-{date[4:6]}-{date[6:8]}",
        "title": _title_map().get(slug, ""),
        "project": _project_map().get(slug) or _project_of(slug, "", []),
        "stages": stages,
        "task": _task_row(slug),
        "ledger": ledger,
        "artifacts": artifacts,
        "log_tail": log_tail,
        "log_truncated": log_truncated,
    }


# A task slug is a Todo/items/ file stem — no separators, no dotfiles.
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TASK_BODY_MAX = 30_000  # chars of the task file body (after frontmatter)
TASK_RUNS_MAX = 20      # newest runs listed for one task


def collect_task_detail(slug: str) -> dict | None:
    """One task for the tap-through view from the human/attention/active lists:
    todo fields, ledger entry, the task file body (frontmatter stripped), and the
    task's runs (newest first) to drill into. None for a malformed slug or a
    task nothing knows about."""
    if not SLUG_RE.match(slug or ""):
        return None
    task, ledger = _task_row(slug), _ledger_row(slug)
    body = ""
    f = ITEMS / f"{slug}.md"
    if f.is_file():
        try:
            body = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            body = ""
        m = re.match(r"^---\n.*?\n---\n?", body, re.S)
        if m:
            body = body[m.end():]
        body = body.strip()
    # `*-<slug>` also matches runs of any slug ENDING in -<slug>; keep exact ones.
    dirs = sorted((d for d in glob.glob(str(RUNS / f"*-{slug}"))
                   if re.fullmatch(r"\d{8}T\d{6}-" + re.escape(slug), Path(d).name)),
                  reverse=True)[:TASK_RUNS_MAX]
    if not (task or ledger or body or dirs):
        return None
    runs = [_run_summary(Path(d)) for d in dirs]
    return {
        "slug": slug,
        "title": task.get("title") or _title_map().get(slug, ""),
        "project": _project_map().get(slug) or _project_of(slug, "", []),
        "task": task,
        "ledger": ledger,
        "body": body[:TASK_BODY_MAX],
        "body_truncated": len(body) > TASK_BODY_MAX,
        "runs": runs,
    }


def collect_agents() -> list[dict]:
    seen = {}
    sp = STATE / "agent_seen.json"
    if sp.exists():
        try:
            seen = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            seen = {}
    agents = []
    try:
        from todo_system.orchestrator.registry import load_registry
        reg = load_registry(str(REGISTRY))
        for a in reg.agents:
            # humans are not heartbeat subjects — `agents` carries runners only, so a
            # stale `alice … 10d 전` row can't read as "did a person die?". Same rule as
            # Orchestrator.agent_liveness(); judged by is_human, never a string compare.
            if a.is_human:
                continue
            last = seen.get(a.id, "")
            age = _iso_age(last)
            secs = _age_secs(last)
            agents.append({"id": a.id, "kind": a.kind, "host": getattr(a, "host", "") or "",
                           "projects": list(getattr(a, "projects", []))[:6],
                           "last_seen": last, "age": age,
                           "alive": secs is not None and secs < 600})
    except Exception:
        # Registry unreadable (missing file, no PyYAML, bad YAML): nothing tells us who
        # is human, so fall open and expose every seen key as before — losing runner
        # liveness is worse than one stale human row. Pinned by
        # AgentsExcludeHumans.test_registry_unreadable_falls_back_to_every_seen_key.
        for aid, last in seen.items():
            age = _iso_age(last)
            secs = _age_secs(last)
            agents.append({"id": aid, "kind": "?", "host": "", "projects": [],
                           "last_seen": last, "age": age,
                           "alive": secs is not None and secs < 600})
    return agents


# every worker_mode the orchestrator dispatches on its own (orchestrator.tasks
# DISPATCHABLE_MODES) — `discover` rows are worker-run too, not a person's job.
AUTO_MODES = ("auto", "delegate", "quick", "discover")
# "사람 몫" rows due within this many days (or overdue) — plus pipeline-parked ones —
# are `urgent` and shown up top; the rest fold away under the life/dev details.
URGENT_DAYS = 3
WHY_ORDER = {"parked": 0, "owned": 1, "blocked": 2, "manual": 3, "backlog": 4}
# Fallback only (see _category): tags that mark a repo-less task as a dev/coding
# project. The primary dev signal is now a `repo:` anywhere in the ancestor chain
# (any declared coding project, no special tag needed); these catch repo-less dev
# leaves (ai-stock-6/7 · dancemirror · tennis-macro) that no ancestor declares.
DEV_TAGS = {"side-project", "sideproject", "project", "app", "bug", "game", "meta",
            "infra", "ops", "macp", "worker", "ai-stock-trading"}

# (path, mtime) → repo cache so a 3s poll re-reads a task file only when it changes.
_REPO_CACHE: dict[str, tuple[float, str | None]] = {}


def _task_repo(slug: str) -> str | None:
    """The `repo:` frontmatter of Todo/items/<slug>.md (None if absent/unreadable),
    cached per (path, mtime). Any parse/import error is swallowed → None, so the
    dashboard never breaks and classification degrades safely to the tag fallback."""
    p = ITEMS / f"{slug}.md"
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    hit = _REPO_CACHE.get(slug)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    repo = None
    try:
        from todo_system.orchestrator.repos import read_task_repo
        repo, _ = read_task_repo(p)
    except Exception:
        repo = None
    _REPO_CACHE[slug] = (mtime, repo)
    return repo


def _repo_in_chain(slug: str, parent_map: dict[str, str]) -> bool:
    """True if the task or any ancestor declares a `repo:` — the structural
    'this is a coding project' signal, so a worker-made review-* child inherits its
    project's repo. Cycle-guarded, depth-capped (parent chains are shallow)."""
    seen: set[str] = set()
    cur = slug
    for _ in range(8):
        if not cur or cur in seen:
            break
        seen.add(cur)
        if _task_repo(cur):
            return True
        cur = parent_map.get(cur, "")
    return False


def _category(tags: str, repo_present: bool = False, human_owned: bool = False) -> str:
    """dev vs 현생(life). Priority: repo-in-chain → dev (a declared coding project,
    no special tag needed); else a registry human-owner → life (personal admin —
    withdrawal / job / email); else the DEV_TAGS tag fallback (repo-less dev leaves
    stay dev, cv-* stay life)."""
    if repo_present:
        return "dev"
    if human_owned:
        return "life"
    t = {x.strip().lower() for x in (tags or "").split(",")}
    return "dev" if (t & DEV_TAGS) else "life"


def collect_deps() -> list[dict]:
    """Cross-project dependency graph — projects with a provides/depends_on/consumer
    relationship, so an upstream change's blast radius is visible."""
    try:
        from todo_system.macp import deps as D
        nodes = D.scan()
        down = D.downstream_edges(nodes)
        out = []
        for name in sorted(nodes):
            n = nodes[name]
            consumers = sorted(down.get(name, set()))
            if n.provides or n.depends_on or consumers:      # only linked projects
                out.append({"name": name, "provides": n.provides,
                            "depends_on": n.depends_on, "consumers": consumers})
        return out
    except Exception:
        return []


def _parse_date(v) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(v or "").strip()[:10])
    except ValueError:
        return None


def _is_urgent(why: str, due: str, today: dt.date) -> bool:
    """parked (a PR/decision the pipeline is waiting on) or due ≤ today+URGENT_DAYS.
    `blocked` is never urgent — it waits on something else (often depends_on worker
    leaves that auto-unblock it), so a near due date isn't a person's action yet."""
    if why == "blocked":
        return False
    d = _parse_date(due)
    return why == "parked" or (d is not None and d <= today + dt.timedelta(days=URGENT_DAYS))


LIVE_PROJECT = ("open", "waiting", "blocked")
# "… (부모)" / "… (parent)" → "…";  "… (KIS, parent)" → "… (KIS)"
_PARENT_TITLE_RE = re.compile(r"\s*[(（]\s*(?:부모|parent)\s*[)）]\s*$|,\s*(?:부모|parent)\s*(?=[)）]\s*$)")


def collect_projects(assignments: list[dict]) -> list[dict]:
    """Top-level projects at a glance: every live (open/waiting/blocked) task with no
    *live* parent of its own that is a parent — is_parent=1 or some task names it as
    parent_slug (e.g. advote-voting, whose kids link by `parent:` only). A parent
    whose own parent is already done/cancelled (or unknown) is a root too: a
    hand-split umbrella under a closed root (mulkko-121-block-editor under the done
    mulkko-pivot-project, 2026-10-19) used to fall through to the human list. One row per
    project with its subtree's leaf progress and how many of its tasks the worker is
    running / has parked. These rows are the umbrella, not work a person picks up, so
    collect_status passes their slugs to collect_human, which leaves them out.

    Order: a project with a task running now first, then `priority` (high first),
    then the subtree's latest activity (newest of run dir, ledger update, task md
    `updated`, linked session journal `updated`) — so hand work on a project whose
    worker runs are old still reads as recent."""
    rows = []
    try:
        with _ro_conn(TODO_DB) as con:
            if con is None:
                return rows
            con.row_factory = sqlite3.Row
            cols = {c[1] for c in con.execute("PRAGMA table_info(tasks)")}
            prio = "priority" if "priority" in cols else "NULL"   # older DBs lack it
            upd = "COALESCE(updated,'')" if "updated" in cols else "''"
            tasks = {r["slug"]: dict(r) for r in con.execute(
                f"SELECT slug, title, status, COALESCE(parent_slug,'') parent, {prio} prio, "
                f"{upd} upd, "
                "COALESCE(is_parent,0) is_parent, COALESCE(next_action,'') next_action, "
                "COALESCE(due,'') due, COALESCE(worker_mode,'') wm, "
                "COALESCE(tags,'') tags FROM tasks")}
    except Exception:
        return rows
    kids: dict[str, list[str]] = {}
    for slug, t in tasks.items():
        if t["parent"] and t["parent"] != slug:
            kids.setdefault(t["parent"], []).append(slug)
    state = {r["task"]: r for r in assignments}
    last = _last_activity(assignments)
    for s, t in tasks.items():                     # hand edits: the md's `updated`
        _bump(last, s, _activity_when(t["upd"]))
    for s, when in _session_activity().items():    # hand sessions (Memory/sessions)
        if s in tasks:
            _bump(last, s, when)
    for slug, t in tasks.items():
        if (t["status"] not in LIVE_PROJECT or _live_parent(t["parent"], tasks)
                or not (t["is_parent"] or kids.get(slug))):
            continue
        done = total = active = attn = 0
        seen, stack = {slug}, list(kids.get(slug, []))
        latest = last.get(slug)
        while stack:                               # whole subtree, cycle-guarded
            c = stack.pop()
            if c in seen:
                continue
            seen.add(c)
            if c in last and (latest is None or last[c] > latest):
                latest = last[c]
            ct = tasks.get(c) or {}
            st = ct.get("status", "")
            a = state.get(c)
            if a and st not in ("done", "cancelled"):
                if a["state"] in ACTIVE:
                    active += 1
                elif (a["state"] in ("awaiting_human", "failed")
                      and not _has_open_child(c, kids, tasks)):
                    attn += 1          # an umbrella whose kids are open is not waiting
            if kids.get(c):
                stack.extend(kids[c])
                continue                           # progress counts leaves only
            if st == "cancelled":
                continue
            total += 1
            done += st == "done"
        rows.append({"slug": slug,
                     "title": _PARENT_TITLE_RE.sub("", t["title"] or "") or slug,
                     "project": _root_label(_top_ancestor(slug, tasks)),
                     "status": t["status"], "worker_mode": t["wm"],
                     # additive: server decides open vs. held so the client can fold
                     # waiting/blocked umbrellas under a collapsed "보류" toggle.
                     "on_hold": t["status"] in ("waiting", "blocked"),
                     "action": t["next_action"][:110],
                     "due": t["due"] if _parse_date(t["due"]) else "",
                     "leaves_done": done, "leaves_total": total,
                     "active": active, "attention": attn,
                     "priority": t["prio"],
                     "last_activity": latest.isoformat(timespec="seconds") if latest else "",
                     "last_age": _iso_age(latest.isoformat()) if latest else ""})
    rows.sort(key=lambda r: r["last_activity"], reverse=True)   # newest first …
    rows.sort(key=lambda r: (-r["active"], -(r["priority"] or 0)))  # … stable under these
    return rows


def _live_parent(parent: str, tasks: dict[str, dict]) -> bool:
    """True when `parent` names a task that is itself still live (open/waiting/blocked).
    A done/cancelled or unknown parent does not make its child a mid-level node."""
    return bool(parent) and (tasks.get(parent) or {}).get("status") in LIVE_PROJECT


def _top_ancestor(slug: str, tasks: dict[str, dict]) -> str:
    """The root of `slug`'s parent chain (closed ancestors included) — the chip label
    source, so a project row under a done root keeps the tree's colour and name."""
    cur, depth = slug, 0
    while depth < 20:
        par = (tasks.get(cur) or {}).get("parent") or ""
        if not par or par == cur or par not in tasks:
            return cur
        cur, depth = par, depth + 1
    return cur


def _has_open_child(slug: str, kids: dict[str, list[str]], tasks: dict[str, dict]) -> bool:
    return any((tasks.get(k) or {}).get("status") not in ("done", "cancelled", "canceled")
               for k in kids.get(slug, []))


def _umbrellas_in_progress() -> set[str]:
    """Parents with at least one open child — umbrellas whose children carry the work.

    Such a parent's ledger row can read awaiting_human (a 0-commit run whose only
    output was the split ends `result-no-pr`), but nobody has anything to do on it
    until its children finish; the orchestrator closes it then. Same query the
    orchestrator's renudge uses, so the page and the nudges agree."""
    try:
        from todo_system.orchestrator.tasks import parents_with_open_children
        return parents_with_open_children(TODO_DB)
    except Exception:
        return set()


def _last_activity(assignments: list[dict]) -> dict[str, dt.datetime]:
    """{slug: latest local naive datetime} from run dir names (YYYYMMDDTHHMMSS-<slug>)
    and ledger updated_ts — one directory listing, no file reads."""
    out: dict[str, dt.datetime] = {}

    def bump(slug: str, when: dt.datetime | None):
        if when and (slug not in out or when > out[slug]):
            out[slug] = when
    try:
        names = os.listdir(RUNS)
    except OSError:
        names = []
    for n in names:
        ts, _, slug = n.partition("-")
        if slug:
            try:
                bump(slug, dt.datetime.strptime(ts, "%Y%m%dT%H%M%S"))
            except ValueError:
                pass
    for r in assignments:
        try:
            t = dt.datetime.fromisoformat(r.get("updated") or "")
        except ValueError:
            continue
        bump(r["task"], t.astimezone().replace(tzinfo=None) if t.tzinfo else t)
    return out


def _bump(out: dict[str, dt.datetime], slug: str, when: dt.datetime | None):
    """Raise out[slug] to `when` — never lowers an existing value."""
    if when and (slug not in out or when > out[slug]):
        out[slug] = when


def _parse_when(raw) -> dt.datetime | None:
    """Local naive datetime from an ISO date/datetime ('' / '-' / junk → None). A bare
    YYYY-MM-DD (task md `updated`) reads as that day's 00:00, so a same-day run time
    still wins over it."""
    raw = str(raw or "").strip()
    if not raw or raw == "-":
        return None
    try:
        t = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    return t.astimezone().replace(tzinfo=None) if t.tzinfo else t


def _activity_when(raw) -> dt.datetime | None:
    """_parse_when, minus future times (they would render a negative age)."""
    t = _parse_when(raw)
    return t if t is not None and t <= dt.datetime.now() else None


# path → (mtime, project, related slugs, updated) so a 3s poll re-reads a session
# journal only when it changes.
_SESSION_CACHE: dict[str, tuple[float, str, tuple[str, ...], dt.datetime | None]] = {}


def _journal_slug(ref) -> str:
    """'[[Todo/items/foo|별칭]]' / 'foo.md' / 'foo' → 'foo'."""
    s = str(ref or "").strip().strip('"').strip("'").replace("[[", "").replace("]]", "")
    return s.split("|")[0].rsplit("/", 1)[-1].removesuffix(".md").strip()


def _session_activity() -> dict[str, dt.datetime]:
    """{slug: latest journal `updated`} over Memory/sessions/*.md, keyed by each
    journal's frontmatter `project` and every `related_tasks` slug — never by file
    name (<date>-<slug>.<host>.md, <date>-<id>-<slug>.<host>.md and the legacy
    <date>-<slug>.md all occur). Fail-soft: a missing dir, an unreadable or broken
    file, or an unparseable/future `updated` is skipped."""
    out: dict[str, dt.datetime] = {}
    try:
        paths = sorted(SESSIONS.glob("*.md"))
    except OSError:
        return out
    if not paths:
        _SESSION_CACHE.clear()
        return out
    try:
        from todo_system.session_log import parse_frontmatter
    except Exception:
        return out
    now = dt.datetime.now()
    seen: set[str] = set()
    for p in paths:
        key = str(p)
        try:
            mtime = p.stat().st_mtime
            hit = _SESSION_CACHE.get(key)
            if hit is None or hit[0] != mtime:
                fm, _ = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
                rel = fm.get("related_tasks") or []
                if isinstance(rel, str):
                    rel = [rel]
                hit = (mtime, _journal_slug(fm.get("project")),
                       tuple(s for s in map(_journal_slug, rel) if s),
                       _parse_when(fm.get("updated")))
                _SESSION_CACHE[key] = hit
        except Exception:
            continue
        seen.add(key)
        _, project, related, when = hit
        if when is None or when > now:
            continue
        for s in (project, *related):
            if s:
                _bump(out, s, when)
    for key in set(_SESSION_CACHE) - seen:          # keep the cache bounded
        _SESSION_CACHE.pop(key, None)
    return out


def collect_human(assignments: list[dict], today: dt.date | None = None,
                  projects: frozenset | set = frozenset(),
                  now: dt.datetime | None = None,
                  umbrellas: frozenset | set = frozenset()) -> list[dict]:
    """Everything that needs a person, not the worker: pipeline-parked tasks
    (awaiting_human), human-owned decisions (registry human agent), and every open
    leaf todo that ISN'T auto-worked — blocked (unblock), manual (hand-dev), or
    with no worker_mode set (needs a decision / routing).

    Each row carries `urgent` (see _is_urgent) and `later` — true when its
    `not_before` is still in the future (not actionable yet; collect_status moves
    those to `human_later` so they don't crowd the list). Host-local; a time of
    day is honored (`not_before` stays `YYYY-MM-DD`, or `YYYY-MM-DDTHH:MM` when it
    has one). `today` alone means 00:00 that day; `now` pins the clock."""
    if now is None:
        now = dt.datetime.combine(today, dt.time.min) if today else dt.datetime.now()
    today = today or now.date()
    items, seen = [], set()
    reg = None
    try:
        from todo_system.orchestrator.registry import load_registry
        reg = load_registry(str(REGISTRY))
    except Exception:
        reg = None
    human_ids = {a.id for a in reg.agents if a.kind == "human"} if reg else set()

    # 1) pipeline-parked for a human (PR review / decision / poison quarantine)
    for r in assignments:
        if (r["state"] == "awaiting_human"
                and r.get("todo_status") not in ("cancelled", "done")
                and r["task"] not in umbrellas):
            items.append({"slug": r["task"], "title": r["task"], "category": "dev",
                          "action": r["note"] or "검토/승인 대기", "why": "parked",
                          "project": r.get("project", ""), "due": "", "not_before": "",
                          "urgent": True, "later": False})
            seen.add(r["task"])

    # 2) every open, non-parent todo that is NOT auto-worked → a person must act
    try:
        with _ro_conn(TODO_DB) as con:
            if con is not None:
                con.row_factory = sqlite3.Row
                # slug → parent (one light query) so a leaf can inherit an ancestor's
                # repo signal without a per-row DB hit.
                rows = list(con.execute("SELECT slug, COALESCE(parent_slug,''), "
                                        "COALESCE(tags,'') FROM tasks"))
                parent_map = {s: p for s, p, _ in rows}
                labels = _project_labels(rows)
                for r in con.execute("SELECT slug, title, next_action, tags, "
                                     "COALESCE(due,'') due, COALESCE(not_before,'') nb, "
                                     "COALESCE(parent_slug,'') parent, "
                                     "COALESCE(worker_mode,'') wm FROM tasks "
                                     "WHERE status='open' AND is_parent=0"):
                    slug, wm = r["slug"], r["wm"].lower()
                    if slug in seen or wm in AUTO_MODES:
                        continue                       # already listed, or auto-worked
                    if slug in projects:
                        continue                       # an umbrella → 프로젝트 card
                    if slug in umbrellas:
                        continue                       # a parent whose kids carry the work
                    owner = reg.for_project(slug) if reg else None
                    human_owned = bool(owner and owner.id in human_ids)
                    if wm == "blocked":
                        why = "blocked"                # waiting — beats owned: nothing to decide yet
                    elif human_owned:
                        why = "owned"                  # personal decision (withdrawal/job/email)
                    elif wm == "manual":
                        why = "manual"                 # hand-developed
                    else:
                        why = "backlog"                # no worker_mode — decide / route it
                    # structural category: repo-in-chain (dev) > human-owner (life) > tags.
                    # only human-listed rows hit the (mtime-cached) file resolver.
                    repo_present = _repo_in_chain(slug, parent_map)
                    items.append({"slug": slug, "title": r["title"] or slug,
                                  "action": (r["next_action"] or "")[:110], "why": why,
                                  "due": r["due"] if _parse_date(r["due"]) else "",
                                  "not_before": _nb.display_not_before(r["nb"]),
                                  "urgent": _is_urgent(why, r["due"], today),
                                  "later": _nb.is_deferred(r["nb"], now),
                                  "category": _category(r["tags"], repo_present, human_owned),
                                  "project": labels.get(slug, "")})
                    seen.add(slug)
    except Exception:
        pass

    items.sort(key=lambda r: (not r["urgent"], WHY_ORDER.get(r["why"], 9),
                              r["due"] or "9999", r["slug"]))
    return items


def _title_map() -> dict:
    """{slug: title} so tables can show a human-readable summary instead of the id."""
    m = {}
    try:
        with _ro_conn(TODO_DB) as con:
            if con is not None:
                for slug, title in con.execute("SELECT slug, title FROM tasks"):
                    if title:
                        m[slug] = title
    except Exception:
        pass
    return m


# tags that are NOT a project name (so the tag-fallback picks a real project tag)
_GENERIC_TAGS = {"todo", "private", "work", "side-project", "sideproject", "app",
                 "project", "bug", "game", "ai", "meta", "admin", "finance",
                 "maintenance", "career", "military", "job", "integration",
                 "macp-propagated", "doc-cleanup", "research", "infra", "paper"}
_PARENT_SUFFIXES = ("-project", "-agent", "-game-mode", "-process", "-parent", "-mvp")


def _project_of(slug: str, parent: str, tags: list[str]) -> str:
    """A human-readable project label for a task. A "project" is a grouping the task
    is a leaf of — so we only trust a parent/slug-prefix candidate if the slug
    actually starts with it (prevents a sibling-as-parent or mis-parent giving a
    wrong label). Falls back to a distinctive tag (single-task projects), else ''
    (no chip for a genuinely standalone task)."""
    cands = []
    if parent and parent != slug:
        base = parent
        for suf in _PARENT_SUFFIXES:
            if base.endswith(suf):
                base = base[: -len(suf)]
                break
        cands.append(base)
    m = re.match(r"^(.+?)-\d", slug)                 # advote-1-… → advote
    if m:
        cands.append(m.group(1))
    for c in cands:                                  # trust only a real prefix grouping
        if c and c != slug and slug.startswith(c + "-"):
            return c
    for t in tags:                                   # dancemirror-app → 'dancemirror'
        if t and t not in _GENERIC_TAGS:
            return t
    return ""                                        # standalone → no chip


def _root_label(slug: str) -> str:
    """Chip label for a top-level project: its slug minus a generic parent suffix
    (ai-stock-trading-agent → ai-stock-trading, worldgraph-project → worldgraph)."""
    for suf in _PARENT_SUFFIXES:
        if slug.endswith(suf) and len(slug) > len(suf):
            return slug[: -len(suf)]
    return slug


def _project_labels(rows) -> dict:
    """{slug: project label} from (slug, parent_slug, tags) rows. A task that sits
    under a top-level parent (no parent of its own, has children) — or is one —
    takes that root's _root_label, so the 프로젝트 card chip and every descendant's
    chip in the other sections name and colour the same project. Anything outside
    such a tree keeps the _project_of heuristic (prefix / tag)."""
    rows = list(rows)
    parent = {s: (p or "") for s, p, _ in rows}
    has_kids = {p for s, p in parent.items() if p and p != s}
    m = {}
    for slug, par, tags in rows:
        cur, depth = slug, 0
        while parent.get(cur) and parent[cur] != cur and depth < 20:
            cur, depth = parent[cur], depth + 1
        if cur in has_kids and cur in parent:          # a real root with a subtree
            m[slug] = _root_label(cur)
        else:
            m[slug] = _project_of(slug, par or "",
                                  [t for t in (tags or "").split(",") if t])
    return m


def _project_map() -> dict:
    """{slug: project label} for every task (see _project_labels)."""
    try:
        with _ro_conn(TODO_DB) as con:
            if con is not None:
                return _project_labels(con.execute(
                    "SELECT slug, COALESCE(parent_slug,''), COALESCE(tags,'') FROM tasks"))
    except Exception:
        pass
    return {}


def _group_of(row: dict) -> str:
    """The grouping key for a dashboard row — the single place the server, the web
    client, and the parity contract ([[macp-dashboard-android-4-task-grouping]]) agree
    on. A row groups under its existing `project` label; a row with no project
    ('' / missing / None) has no group and always renders standalone. Additive: this
    only reads `project`, never mutates other fields."""
    return row.get("project") or ""


def _todo_status_map() -> dict:
    """{slug: todo status} — so the dashboard can tell a genuinely-open failure
    from a stale ledger entry whose todo was already cancelled/closed."""
    m = {}
    try:
        with _ro_conn(TODO_DB) as con:
            if con is not None:
                for slug, status in con.execute("SELECT slug, status FROM tasks"):
                    m[slug] = status
    except Exception:
        pass
    return m


def collect_todo() -> dict:
    out = {"open": 0, "done": 0, "by_mode": {}, "dispatchable": 0}
    try:
        with _ro_conn(TODO_DB) as con:
            if con is None:
                return out
            con.row_factory = sqlite3.Row
            for r in con.execute("SELECT status, COUNT(*) c FROM tasks GROUP BY status"):
                if r["status"] == "open":
                    out["open"] = r["c"]
                elif r["status"] == "done":
                    out["done"] = r["c"]
            for r in con.execute("SELECT COALESCE(worker_mode,'(unset)') m, COUNT(*) c "
                                 "FROM tasks WHERE status='open' AND is_parent=0 GROUP BY m"):
                out["by_mode"][r["m"]] = r["c"]
    except Exception:
        pass
    try:
        from todo_system.orchestrator import tasks as T
        out["dispatchable"] = len(T.load_dispatchable(str(TODO_DB)))
    except Exception:
        pass
    return out


def collect_usage() -> dict | None:
    """Claude subscription usage + guard decision (todo_system/usage_guard.py).
    Reads the guard's state file only — the dashboard poll must never trigger a
    network fetch of its own."""
    try:
        st = json.loads((STATE / "usage_guard" / "latest.json").read_text())
    except Exception:
        return None
    d = st.get("decision") or {}
    wins = {w.get("name"): w for w in d.get("windows", [])}
    age = None
    try:
        age = int(dt.datetime.now().timestamp() - float(st.get("fetched_at")))
    except (TypeError, ValueError):
        pass
    now = dt.datetime.now().timestamp()

    def _epoch(v) -> float | None:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    def _in_s(epoch: float | None) -> int | None:
        return None if epoch is None else int(epoch - now)

    blocked_until = _epoch(d.get("blocked_until"))
    # Last good utilization: prefer the fresh decision window, else fall back to
    # the last stored snapshot so a 429/no-data tick still shows real numbers
    # (not blank), letting the card tell idle from paced-HOLD from blind.
    usage = st.get("usage") or {}

    def _util(win_name: str, usage_key: str):
        w = wins.get(win_name) or {}
        if w.get("utilization") is not None:
            return w.get("utilization")
        return (usage.get(usage_key) or {}).get("utilization")

    next_retry_at = _epoch(st.get("next_retry_at"))
    return {
        "session": _util("session", "five_hour"),
        "weekly": _util("weekly", "seven_day"),
        "allowed": d.get("allowed", True),
        "reason": d.get("reason"),
        "age_s": age,
        # no-data visibility: is the snapshot stale, what was the last fetch error,
        # and when does the guard retry — so the card distinguishes idle / HOLD /
        # blind instead of silently going empty on 429.
        "stale": bool(d.get("stale")),
        "last_error": st.get("error"),
        "next_retry_at": next_retry_at,
        "next_retry_in_s": _in_s(next_retry_at),
        # When the hold lifts. The guard writes an absolute epoch (either the
        # window reset or the pace catch-up time, whichever is sooner); the
        # dashboard shows "in N h M m" so a glance answers "when can it run?".
        "blocked_until": blocked_until,
        "blocked_in_s": _in_s(blocked_until),
        "session_resets_at": _epoch((wins.get("session") or {}).get("resets_at")),
        "session_resets_in_s": _in_s(_epoch((wins.get("session") or {}).get("resets_at"))),
        "weekly_resets_at": _epoch((wins.get("weekly") or {}).get("resets_at")),
        "weekly_resets_in_s": _in_s(_epoch((wins.get("weekly") or {}).get("resets_at"))),
    }


def collect_account() -> dict:
    """The claude account the worker runs vault tasks as — the single source of
    truth is ``macp.config.current_account`` anchored at VAULT (the same pin +
    profile the runner resolves for a vault task): ``{active: <name|null>,
    config_dir: <str|''>, source: 'pin'|'profile'|'priority'|'none'}``.
    Fail-soft: any error reads as no account, so /api/status never breaks on it.
    ``collect_account_runtime`` adds ``accounts[]``, ``paused`` and
    ``last_switch`` on top (leaf 4); ``source`` gains the value ``override``."""
    out = {"active": None, "config_dir": "", "source": "none"}
    try:
        from todo_system.macp import accounts as _acc
        from todo_system.macp import config as _cfg
        acct, source = _cfg.current_account(VAULT)
        out["source"] = source
        if acct is not None:
            out["active"] = acct.name
            out["config_dir"] = str(_acc.config_dir_for(acct))
    except Exception:
        pass
    return out


def _pct(info) -> float | None:
    try:
        util = (info or {}).get("utilization")
        return float(util) if util is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def collect_account_runtime(base: dict) -> dict:
    """Leaf 4 fields on top of ``collect_account()``'s object — ADDED keys only
    (``accounts``, ``paused``, ``last_switch``); the three base keys are copied
    as given. Reads two state files, both resolved at call time under VAULT
    (or the same env overrides the engine honours), never the network:

    * ``Worker/_state/claude_accounts.json`` (``MACP_CLAUDE_ACCOUNTS_STATE``) —
      unavailable marks, the runtime override, the last switch;
    * ``Worker/_state/usage_guard/latest.json`` (``USAGE_GUARD_STATE_DIR``) —
      the per-account snapshots (``accounts.<name>``; the top-level usage when
      it is the recorded active account's).

    ``accounts[]``: one row per claude account of the profile anchored at VAULT
    — ``{name, active, available, reason, session_pct, weekly_pct, resets_at}``:
    ``active`` = same name as ``base.active``; ``available`` = no live
    unavailable mark; ``reason`` = the mark's reason (exhausted / auth /
    no_subscription) or null; ``session_pct`` / ``weekly_pct`` = that account's
    last snapshot or null; ``resets_at`` = the mark's ``until`` as epoch seconds,
    null when available or sticky. ``paused``: at least one claude account and
    none available. ``last_switch``: ``{from, to, reason, ts}`` or null.
    Fail-soft: any error leaves the base keys plus the empty additions."""
    out = dict(base)
    out.update({"accounts": [], "paused": False, "last_switch": None})
    try:
        from todo_system.macp import accounts as _acc
        from todo_system.macp import config as _cfg
        env_state = os.environ.get(_acc.ENV_RUNTIME_STATE)
        state_path = (Path(env_state).expanduser() if env_state
                      else Path(VAULT) / "Worker" / "_state" / "claude_accounts.json")
        runtime = _acc.load_runtime_state(path=state_path)
        env_ug = os.environ.get("USAGE_GUARD_STATE_DIR")
        ug_path = (Path(env_ug).expanduser() if env_ug
                   else Path(VAULT) / "Worker" / "_state" / "usage_guard") / "latest.json"
        try:
            ug = json.loads(ug_path.read_text(encoding="utf-8"))
        except Exception:
            ug = {}
        if not isinstance(ug, dict):
            ug = {}
        snaps = ug.get("accounts") if isinstance(ug.get("accounts"), dict) else {}
        ctx = _cfg.resolve(VAULT)
        prof = ctx.get("profile")
        cands = [a for a in (getattr(prof, "accounts", None) or [])
                 if getattr(a, "agent", "claude") == "claude"]
        now = time.time()
        rows = []
        for a in cands:
            entry = _acc.unavailable_entry(a, runtime, now)
            snap = snaps.get(a.name)
            usage = snap.get("usage") if isinstance(snap, dict) else None
            if not isinstance(usage, dict) and ug.get("account") == a.name:
                usage = ug.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            rows.append({
                "name": a.name,
                "active": a.name == base.get("active"),
                "available": entry is None,
                "reason": str(entry.get("reason") or "unavailable") if entry else None,
                "session_pct": _pct(usage.get("five_hour")),
                "weekly_pct": _pct(usage.get("seven_day")),
                "resets_at": _acc.until_epoch(entry) if entry else None,
            })
        out["accounts"] = rows
        out["paused"] = bool(rows) and not any(r["available"] for r in rows)
        last = runtime.get("last_switch")
        out["last_switch"] = last if isinstance(last, dict) else None
    except Exception:
        pass
    return out


def collect_account_slots() -> dict:
    """The runner's live parallel-slot usage this poll
    (``Worker/_state/runner_slots.json`` under VAULT, ``RUNNER_SLOTS_STATE``
    override) — the runner is the single source of truth for live slots, so the
    server only renders what it published. ``{total_used, total_max,
    per_account: [{name, used, max}]}``; the empty shape (0 / None / []) when no
    runner has reported (file absent on a host with no runner). Fail-soft: any
    error reads as the empty shape, so /api/status never breaks on it. Attached
    to the ``account`` object by :func:`collect_status` (kept OUT of
    :func:`collect_account_runtime`, whose key set is pinned by tests)."""
    out = {"total_used": 0, "total_max": None, "per_account": []}
    try:
        env_slots = os.environ.get("RUNNER_SLOTS_STATE")
        path = (Path(env_slots).expanduser() if env_slots
                else Path(VAULT) / "Worker" / "_state" / "runner_slots.json")
        snap = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(snap, dict):
            return out
        tu = snap.get("total_used")
        out["total_used"] = int(tu) if isinstance(tu, (int, float)) else 0
        tm = snap.get("total_max")
        out["total_max"] = int(tm) if isinstance(tm, (int, float)) else None
        per = snap.get("per_account") if isinstance(snap.get("per_account"), dict) else {}
        rows = []
        for name, v in per.items():
            v = v if isinstance(v, dict) else {}
            used, mx = v.get("used"), v.get("max")
            rows.append({"name": name,
                         "used": int(used) if isinstance(used, (int, float)) else 0,
                         "max": int(mx) if isinstance(mx, (int, float)) else None})
        out["per_account"] = rows
    except Exception:
        return {"total_used": 0, "total_max": None, "per_account": []}
    return out


# ---- Queue (docs/dashboard-parity.md "큐") ----------------------------------
QUEUE_KEYS = ("queue", "queue_upcoming", "queue_blocked")


def _usage_hold_of(usage: dict | None) -> str | None:
    """The tick's usage_hold verdict, read from the guard's state file decision
    (collect_usage) — never `Orchestrator.usage_hold()`, which refreshes usage
    state. Same fallback string as that method."""
    if not usage or usage.get("allowed", True):
        return None
    return usage.get("reason") or "usage guard blocked"


def _compute_queue(*, db: Path, assign: Path, registry: Path, now: float,
                   usage: dict | None) -> dict:
    """The three queue lists. Every wait verdict comes from the orchestrator's own
    gates (`Orchestrator.queue_snapshot`); this only assembles contract rows."""
    from todo_system.orchestrator import tasks as T
    from todo_system.orchestrator.assignment import Ledger
    from todo_system.orchestrator.orchestrator import Orchestrator, resolve_dispatch_limits
    from todo_system.orchestrator.registry import load_registry

    budget, cap, gradle_cap = resolve_dispatch_limits()
    orch = Orchestrator(None, load_registry(registry), Ledger(assign),
                        notify=lambda *a, **k: None, daily_run_budget=budget,
                        max_parallel_per_repo=max(1, int(cap)),
                        max_parallel_gradle_builds=max(0, int(gradle_cap)))
    snap = orch.queue_snapshot(T.load_dispatchable(db), now=now,
                               paused=orch.paused(), usage_hold=_usage_hold_of(usage),
                               budget_exceeded=orch.budget_exceeded())
    with _ro_conn(Path(db)) as con:
        labels = _project_labels(con.execute(
            "SELECT slug, COALESCE(parent_slug,''), COALESCE(tags,'') FROM tasks")) \
            if con is not None else {}
    meta = T.task_meta(db)

    def head(slug: str, title: str) -> dict:
        project = labels.get(slug) or _project_of(slug, "", [])
        return {"slug": slug, "title": title or "", "project": project,
                "group": _group_of({"project": project})}

    queue = []
    for r in snap:
        p, m = r["payload"], meta.get(r["slug"], {})
        queue.append({**head(r["slug"], p.get("summary") or ""),
                      "worker_mode": p.get("worker_mode") or "",
                      "priority": m.get("priority"),
                      "checkout": r["checkout"],
                      "wait": r["wait"],
                      "wait_text": r["wait_text"],
                      "holder": r["holder"],
                      "since": r["since"] or m.get("updated") or ""})
    upcoming = [{**head(r["slug"], r["title"]), "not_before": r["not_before"]}
                for r in T.load_upcoming(db, now=dt.datetime.fromtimestamp(now))]
    blocked = [{**head(r["slug"], r["title"]), "waiting_on": r["waiting_on"]}
               for r in T.load_blocked(db)]
    return {"queue": queue, "queue_upcoming": upcoming, "queue_blocked": blocked}


def collect_queue(*, db: Path | None = None, assign: Path | None = None,
                  registry: Path | None = None, now: float | None = None,
                  usage: dict | None = None) -> dict:
    """/api/status `queue` / `queue_upcoming` / `queue_blocked` — what is waiting,
    in tick order, and why. Read-only (no ledger/mailbox/usage writes, no
    network). Fail-soft: any error yields `[]` for all three keys. `cli queue`
    calls this same function, so the CLI and the web read identical rows."""
    try:
        return _compute_queue(db=Path(db or TODO_DB), assign=Path(assign or ASSIGN),
                              registry=Path(registry or REGISTRY),
                              now=time.time() if now is None else now, usage=usage)
    except Exception:
        return {k: [] for k in QUEUE_KEYS}


def collect_status() -> dict:
    a = collect_assignments()
    tstatus = _todo_status_map()
    titles = _title_map()
    projects = _project_map()
    for r in a:                                    # tie each ledger row to its todo
        r["todo_status"] = tstatus.get(r["task"])
        r["title"] = titles.get(r["task"], "")
        r["project"] = projects.get(r["task"]) or _project_of(r["task"], "", [])
    runs = collect_runs(30)
    for r in runs:
        r["title"] = titles.get(r["slug"], "")
        r["project"] = projects.get(r["slug"]) or _project_of(r["slug"], "", [])
    today = dt.datetime.now().strftime("%Y%m%d")
    runs_today = len([d for d in glob.glob(str(RUNS / f"{today}T*"))])
    # A failed/awaiting ledger entry whose todo is already cancelled/done is a stale
    # bookkeeping leftover, not a live issue — don't surface it as "attention".
    resolved = ("cancelled", "done")
    # Three triage sections get a `group` key (additive) computed by the single
    # _group_of helper; the web client folds same-section rows that share a non-empty
    # group. recent/runs stay unstamped — recent (a[:40]) shares dict objects with
    # active/attention, so those objects may end up carrying `group` too; harmless,
    # it is a field addition and no consumer removes or renames anything.
    active = [r for r in a if r["state"] in ACTIVE]
    for r in active:                               # additive: the server decides whether
        # the runner is the default one, so the client can hide the "agent …" line
        # unless a non-default agent is running the task. (recent shares these dict
        # objects per the note above; the extra bool riding along is harmless.)
        r["agent_is_default"] = (not r.get("agent")) or r["agent"] == DEFAULT_RUNNER_AGENT
    # "deferred" rows (display state) are deliberately absent: they are waiting,
    # not failing, and retry by themselves — nothing for a human to do.
    # An umbrella whose children are still open is in progress, not waiting on a
    # person — even when its own ledger row reads awaiting_human.
    umbrellas = _umbrellas_in_progress()
    attention = [r for r in a if r["state"] in ("awaiting_human", "failed")
                 and r.get("todo_status") not in resolved
                 and r["task"] not in umbrellas][:12]
    projects_rows = collect_projects(a)
    human_all = collect_human(a, projects={p["slug"] for p in projects_rows},
                              umbrellas=umbrellas)
    human = [r for r in human_all if not r.get("later")]
    human_later = [r for r in human_all if r.get("later")]
    for r in (*active, *attention, *human, *human_later):
        r["group"] = _group_of(r)

    # ---- worker-state derivation (why "idle"?) ----
    # Gather the pure function's inputs from the same live data the rest of the
    # page already reads, then let determine_worker_state() name the state. Each
    # source is fail-soft (the collectors above already swallow their own errors);
    # a missing input degrades the answer, it never breaks /api/status.
    running = _worker_running()
    paused = (STATE / "PAUSED").exists()
    usage = collect_usage()
    usage_allowed = bool((usage or {}).get("allowed", True))
    in_flight = [{"checkout": r.get("checkout"),
                  "label": r.get("checkout_label") or r.get("checkout") or "?",
                  "note": r.get("note") or ""}
                 for r in active]
    dispatchable_checkouts: list[str] = []
    try:
        from todo_system.orchestrator import tasks as T
        dispatchable_checkouts = [_checkout_key(t.payload)
                                  for t in T.load_dispatchable(str(TODO_DB))]
    except Exception:
        pass
    budget: int | None = None
    cap = 1                       # per-repo concurrent-run cap for the serial gloss
    try:                          # active host profile's daily_run_budget (0 = unlimited)
        from todo_system.macp import config as _cfg
        prof = _cfg.load_profile(_cfg.default_profile())
        budget = prof.daily_run_budget if prof else None
        if prof:                  # max_parallel_per_repo (leaf 5: default 2 = concurrency on)
            cap = max(1, int(getattr(prof, "max_parallel_per_repo", 1) or 1))
    except Exception:
        budget = None
    worker_state, worker_state_detail = determine_worker_state(
        stage_count=len(running),
        in_flight=in_flight,
        dispatchable_checkouts=dispatchable_checkouts,
        paused=paused,
        usage_allowed=usage_allowed,
        runs_today=runs_today,
        budget=budget,
        cap=cap,
    )

    return {
        "now": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "worker_busy": bool(running),
        "worker_state": worker_state,
        "worker_state_detail": worker_state_detail,
        "paused": paused,
        "runs_today": runs_today,
        "active": active,
        "attention": attention,
        "human": human,
        "human_later": human_later,
        "projects": projects_rows,
        "deps": collect_deps(),
        "recent": a[:40],
        "runs": runs,
        "agents": collect_agents(),
        "todo": collect_todo(),
        "usage": usage,
        # collect_account_runtime's key set is pinned by tests, so the live-slot
        # object is attached HERE (not folded into it): /api/status.account.slots.
        "account": {**collect_account_runtime(collect_account()),
                    "slots": collect_account_slots()},
        **collect_queue(usage=usage),
    }


# ---- Logo ------------------------------------------------------------------

# Canonical copy: docs/brand/macp-logo.svg (a test keeps the two byte-identical).
# Embedded here so the server never reads docs/ at runtime; served as a base64
# data-URI favicon so it loads with the page — no extra route, no auth exception.
LOGO_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="24" height="24">\n'
    '<rect width="24" height="24" rx="5" fill="#0d1117"/>\n'
    '<path d="M6.5 17V8.5l5.5 5.5 5.5-5.5V17" fill="none" stroke="#58a6ff" '
    'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>\n'
    '<circle cx="12" cy="6.5" r="1.5" fill="#58a6ff"/>\n'
    "</svg>\n"
)
FAVICON_HREF = "data:image/svg+xml;base64," + base64.b64encode(LOGO_SVG.encode("utf-8")).decode("ascii")


# ---- HTML ------------------------------------------------------------------

HTML = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MaestroACP · live</title>
<meta name="color-scheme" content="dark light">
<link rel="icon" type="image/svg+xml" href="__FAVICON__">
<script>/* pre-paint: apply saved theme to <html data-theme> before first paint so an
   explicit light choice never flashes the dark default. auto/unknown = no attr
   (falls back to :root dark + prefers-color-scheme light). No external ref. */
try{var _t=localStorage.getItem('mdash.theme');if(_t==='light'||_t==='dark')document.documentElement.dataset.theme=_t}catch(_e){}</script>
<style>
/* Mobile-first single column. Dark theme; body text (--fg 16:1, --mut ~7.6:1 on
   --bg) meets WCAG AA 4.5:1, base font 14px. No external URL / font / script. */
:root{--bg:#0d1117;--card:#161b22;--bd:#30363d;--bd2:#242b34;
--fg:#e6edf3;--mut:#9ba5b0;
--grn:#3fb950;--red:#f85149;--yel:#d29922;--blu:#58a6ff;--pur:#bc8cff}
/* Light palette (docs/dashboard-parity.md §2, 11 tokens). Applied for OS light
   under auto (no explicit data-theme), and for an explicit data-theme="light".
   A media query and an attribute selector can't share one declaration block, so
   the same token list is written twice — keep both in sync if the contract moves. */
@media (prefers-color-scheme:light){:root:not([data-theme="dark"]){
--bg:#f6f8fa;--card:#ffffff;--bd:#d0d7de;--bd2:#eaeef2;
--fg:#1f2328;--mut:#59636e;
--grn:#1a7f37;--red:#cf222e;--yel:#9a6700;--blu:#0969da;--pur:#8250df}}
:root[data-theme="light"]{
--bg:#f6f8fa;--card:#ffffff;--bd:#d0d7de;--bd2:#eaeef2;
--fg:#1f2328;--mut:#59636e;
--grn:#1a7f37;--red:#cf222e;--yel:#9a6700;--blu:#0969da;--pur:#8250df}
*{box-sizing:border-box}
html{scroll-behavior:smooth;-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
header{display:flex;align-items:center;gap:10px;padding:12px 16px;
border-bottom:1px solid var(--bd);position:sticky;top:0;background:var(--bg);z-index:5}
.htitle{flex:1;min-width:0;font-size:15px;font-weight:600;white-space:nowrap;
overflow:hidden;text-overflow:ellipsis}
.htitle .mut{font-weight:400;font-size:13px}
.dot{width:11px;height:11px;border-radius:50%;flex:none;background:var(--mut)}
.pill{padding:3px 11px;border-radius:20px;font-size:13px;font-weight:600;
border:1px solid var(--bd);white-space:nowrap}
#theme{background:transparent;color:var(--fg);font:inherit;cursor:pointer}
#theme:hover{border-color:var(--blu)}
.live{animation:pulse 1.5s infinite}@keyframes pulse{50%{opacity:.3}}
.wrap{max-width:900px;margin:0 auto;padding:14px 16px 28px;
display:flex;flex-direction:column;gap:12px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:13px 15px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--mut);
margin:0 0 9px;font-weight:600;display:flex;align-items:center;gap:8px}
.cnt{background:color-mix(in srgb,var(--card),var(--bd));color:var(--fg);border-radius:20px;padding:1px 9px;
font-size:12px;font-weight:600}
.state{font-size:20px;font-weight:700;display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.state .sub{font-size:13px;font-weight:500;color:var(--mut)}
.metrics{display:flex;flex-wrap:wrap;gap:13px 22px;margin-top:13px}
.m{display:flex;flex-direction:column;min-width:50px}
.m b{font-size:22px;font-weight:700;line-height:1.1}
.m span{color:var(--mut);font-size:12px;margin-top:3px}
.usage{margin-top:13px;border-top:1px solid var(--bd2);padding-top:11px}
.ur{display:flex;flex-wrap:wrap;gap:11px 20px;align-items:baseline}
.ur .u{display:flex;flex-direction:column;min-width:62px}
.ur .u b{font-size:17px;font-weight:700}
.ur .u span{color:var(--mut);font-size:12px}
.list{display:flex;flex-direction:column}
.row{display:flex;gap:10px;align-items:baseline;padding:8px 0;border-bottom:1px solid var(--bd2)}
.row:last-child{border-bottom:none}
.row .main{flex:1;min-width:0}
.ttl{font-weight:500;overflow-wrap:anywhere}
.note{color:var(--mut);font-size:13px;margin-top:3px;overflow-wrap:anywhere}
.rmeta{display:flex;align-items:center;justify-content:flex-end;gap:6px;
flex:0 1 auto;min-width:0;flex-wrap:wrap}
.age{color:var(--mut);font-size:12px;white-space:nowrap}
.qdeps{white-space:normal;overflow-wrap:anywhere;text-align:right}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.badge{padding:2px 8px;border-radius:6px;font-size:12px;font-weight:600;white-space:nowrap}
/* Badges: tint = semantic token at 15% over --card, text = the token darkened 25%
   toward --fg. Both derive only from the 11 tokens (no hardcoded badge hex, parity
   §2); the --fg nudge lifts the token's own ~4.9:1-on-white to ≥5.4:1 on the tint
   in both themes (yellow alone can't clear 4.5:1 undarkened — see run contrast table). */
.b-done{background:color-mix(in srgb,var(--grn) 15%,var(--card));color:color-mix(in srgb,var(--grn) 75%,var(--fg))}
.b-fail{background:color-mix(in srgb,var(--red) 15%,var(--card));color:color-mix(in srgb,var(--red) 75%,var(--fg))}
.b-run{background:color-mix(in srgb,var(--blu) 15%,var(--card));color:color-mix(in srgb,var(--blu) 75%,var(--fg))}
.b-wait{background:color-mix(in srgb,var(--yel) 15%,var(--card));color:color-mix(in srgb,var(--yel) 75%,var(--fg))}
.b-gray{background:color-mix(in srgb,var(--mut) 15%,var(--card));color:color-mix(in srgb,var(--mut) 75%,var(--fg))}
.b-def{background:color-mix(in srgb,var(--mut) 15%,var(--card));color:color-mix(in srgb,var(--mut) 75%,var(--fg))}
.b-pur{background:color-mix(in srgb,var(--pur) 15%,var(--card));color:color-mix(in srgb,var(--pur) 75%,var(--fg))}
.b-blu{background:color-mix(in srgb,var(--blu) 15%,var(--card));color:color-mix(in srgb,var(--blu) 75%,var(--fg))}
.pchip{display:inline-block;border-radius:6px;padding:1px 7px;font-size:12px;
font-weight:600;margin-right:5px;border:1px solid var(--bd)}
.cp{background:color-mix(in srgb,var(--card),var(--bd));border:1px solid var(--bd);color:var(--mut);border-radius:6px;
cursor:pointer;font-size:12px;line-height:1;padding:2px 6px;margin-left:5px;white-space:nowrap}
.cp:hover{color:var(--fg);border-color:var(--blu)}.cp.ok{color:var(--grn);border-color:var(--grn)}
.stg{display:inline-block;min-width:16px;height:16px;border-radius:4px;font-size:10px;
text-align:center;line-height:16px;font-weight:700;margin-right:2px}
.s-ok{background:color-mix(in srgb,var(--grn) 15%,var(--card));color:color-mix(in srgb,var(--grn) 75%,var(--fg))}.s-bad{background:color-mix(in srgb,var(--red) 15%,var(--card));color:color-mix(in srgb,var(--red) 75%,var(--fg))}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chips span{background:color-mix(in srgb,var(--card),var(--bd));border-radius:6px;padding:3px 9px;font-size:13px;color:var(--fg)}
details>summary{cursor:pointer;font-size:13px;font-weight:600;color:var(--fg);
list-style:none;display:flex;align-items:center;gap:8px;padding:2px 0}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:'▸';color:var(--mut);font-size:11px;flex:none}
details[open]>summary::before{content:'▾'}
details>summary .cnt{margin-left:auto}
.pbar{display:inline-block;width:56px;height:6px;border-radius:3px;background:color-mix(in srgb,var(--card),var(--bd));
overflow:hidden;vertical-align:middle}.pbar i{display:block;height:100%;background:var(--grn)}
/* project rows are one line: chip + title clipped with ellipsis (info diet). */
.prj .main>div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* Folded task groups: same-section rows sharing a project collapse under one tappable
   header row (▸ collapsed / ▾ open); kids are indented and keep their own row behaviour. */
.grphead{font-weight:600}
.gar{color:var(--mut);font-size:13px;flex:none;margin-left:auto}
.grpkids{padding-left:10px;border-left:2px solid var(--bd2)}
.more{margin-top:9px;background:color-mix(in srgb,var(--card),var(--bd));border:1px solid var(--bd);color:var(--blu);
border-radius:8px;cursor:pointer;font-size:13px;padding:7px 12px;width:100%}
.more:hover{border-color:var(--blu)}
.empty{color:var(--mut);font-size:13px;padding:5px 0}
.mut{color:var(--mut)}
footer{text-align:center;color:var(--mut);font-size:12px;padding:16px}
/* Tap-through detail: rows with data-open open a full-screen panel above the
   live page (which keeps polling underneath, untouched). */
.clk{cursor:pointer}.clk:hover{background:color-mix(in srgb,var(--fg) 5%,var(--bg))}
.lnk{background:color-mix(in srgb,var(--card),var(--bd));border:1px solid var(--bd);color:var(--blu);border-radius:6px;
cursor:pointer;font-size:13px;padding:5px 10px}
.lnk:hover{border-color:var(--blu)}
#dv{position:fixed;inset:0;background:var(--bg);z-index:20;overflow-y:auto}
.pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bg);border:1px solid var(--bd2);
border-radius:8px;padding:10px;font-size:13px;margin-top:6px}
.pre.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.kv{margin-top:9px}.kv .k{color:var(--mut);font-size:12px;font-weight:600}
.kv .v{overflow-wrap:anywhere}
#dv details{margin-top:6px}
</style></head><body>
<header>
  <span id="hdot" class="dot"></span>
  <div class="htitle">MaestroACP<span class="mut"> · 실행 현황</span></div>
  <span id="hstate" class="pill">…</span>
  <button id="theme" class="pill" type="button" aria-label="테마 전환 (자동/라이트/다크)">◐ 자동</button>
  <span id="clock" class="mut mono"></span>
</header>
<main class="wrap" id="app">
  <!-- 현재 상태 (idle/실행중/PAUSED/usage HOLD) — always visible triage -->
  <section class="card" id="c-status">
    <div class="state"><span id="state-line">…</span><span class="sub" id="state-sub"></span><span class="sub" id="state-acct"></span></div>
    <!-- first screen = status line + 3 action counts (주의·결정·진행 중). runs today·
         queue·todo open·usage·슬롯 moved into 시스템 상세 (#sys-metrics / #usage). -->
    <div class="metrics">
      <div class="m"><b id="mv-attn">–</b><span>주의</span></div>
      <div class="m"><b id="mv-human">–</b><span>결정</span></div>
      <div class="m"><b id="mv-active">–</b><span>진행 중</span></div>
    </div>
  </section>

  <!-- 진행 중인 프로젝트 (최상위 부모 태스크) -->
  <section class="card" id="c-projects">
    <h2>프로젝트 <span class="cnt" id="cnt-proj">0</span></h2>
    <div class="list" id="projects"></div>
    <button class="more" data-sec="projects" hidden>더보기</button>
    <details id="d-projhold" hidden style="margin-top:8px">
      <summary>보류 <span class="cnt" id="cnt-projhold">0</span></summary>
      <div class="list" id="projects-hold"></div>
    </details>
  </section>

  <!-- 내가 결정할 것 (사람 몫) -->
  <section class="card" id="c-human">
    <h2>결정할 것 · 🔥 급한 것 <span class="cnt" id="cnt-urgent">0</span></h2>
    <div class="list" id="human-urgent"></div>
    <details id="d-humanlife" style="margin-top:8px">
      <summary>🎓 현생 (여유 있음) <span class="cnt" id="cnt-life">0</span></summary>
      <div class="list" id="human-life"></div>
    </details>
    <details id="d-humandev" style="margin-top:8px">
      <summary>🛠 개발 프로젝트 (사람 몫) <span class="cnt" id="cnt-dev">0</span></summary>
      <div class="list" id="human-dev"></div>
    </details>
    <details id="d-humanblocked" style="margin-top:8px">
      <summary>⛔ 대기 중 (블록) <span class="cnt" id="cnt-blocked">0</span></summary>
      <div class="list" id="human-blocked"></div>
    </details>
    <details id="d-humanlater" style="margin-top:8px">
      <summary>⏳ 시작일 전 <span class="cnt" id="cnt-later">0</span></summary>
      <div class="list" id="human-later"></div>
    </details>
  </section>

  <!-- 깨진 것 (failed · 사람 대기) -->
  <section class="card" id="c-attention">
    <h2>깨진 것 · failed·사람대기 <span class="cnt" id="cnt-attn">0</span></h2>
    <div class="list" id="attention"></div>
  </section>

  <!-- 지금 실행 중 -->
  <section class="card" id="c-active">
    <h2>진행 중 <span class="cnt" id="cnt-active">0</span></h2>
    <div class="list" id="active"></div>
    <button class="more" data-sec="active" hidden>더보기</button>
  </section>

  <!-- 큐: 디스패치 대기 순서·사유 (서버 판정, 읽기 전용). 기본 접힘, 그룹 폴딩 없음. -->
  <section class="card" id="c-queue">
    <details id="d-queue">
      <summary>큐 <span class="cnt" id="cnt-queue">0</span></summary>
      <div class="list" id="queue"></div>
      <details id="d-queue-upcoming" hidden style="margin-top:8px">
        <summary>⏳ 예약됨 <span class="cnt" id="cnt-queue-upcoming">0</span></summary>
        <div class="list" id="queue-upcoming"></div>
      </details>
      <details id="d-queue-blocked" hidden style="margin-top:8px">
        <summary>🔗 선행 대기 <span class="cnt" id="cnt-queue-blocked">0</span></summary>
        <div class="list" id="queue-blocked"></div>
      </details>
    </details>
  </section>

  <!-- 이하 이력·참고·계측: 기본 접힘 '시스템 상세' 한 카드로 합침. 안의 기존
       <details>/<div id> 는 중첩으로 보존(id·cnt-* 앵커 불변). runs 는 '이력'에
       흡수되므로 cnt-runs/#runs 는 숨긴 자리표시로만 남긴다. -->
  <section class="card" id="c-recent">
    <details id="d-sys">
      <summary>시스템 상세 <span class="cnt" id="cnt-sys">0</span></summary>
      <!-- runs today/queue/todo open·슬롯·usage: moved off the first screen (same ids,
           update()/setUsage/setState keep working since the elements only moved). -->
      <div id="sys-metrics">
        <div class="metrics">
          <div class="m"><b id="mv-runs">–</b><span>runs today</span></div>
          <div class="m"><b id="mv-queue">–</b><span>queue</span></div>
          <div class="m"><b id="mv-open">–</b><span>todo open</span></div>
        </div>
        <div class="note" id="sys-slots"></div>
      </div>
      <div class="usage" id="usage" hidden></div>
      <details id="d-recent" open style="margin-top:8px">
        <summary>이력 (실행·태스크) <span class="cnt" id="cnt-recent">0</span></summary>
        <div class="list" id="recent"></div>
        <button class="more" data-sec="recent" hidden>더보기</button>
      </details>
      <details id="d-agents" style="margin-top:8px">
        <summary>에이전트 (러너) <span class="cnt" id="cnt-agents">0</span></summary>
        <div class="list" id="agents"></div>
      </details>
      <details id="d-mode" style="margin-top:8px">
        <summary>오픈 태스크 (worker_mode별) <span class="cnt" id="cnt-mode">0</span></summary>
        <div class="chips" id="mode"></div>
      </details>
      <details id="d-deps" style="margin-top:8px">
        <summary>🔗 크로스-프로젝트 의존성 <span class="cnt" id="cnt-deps">0</span></summary>
        <div class="list" id="deps"></div>
      </details>
      <!-- id 보존용 자리표시(runs 는 이력에 흡수됨) -->
      <div id="d-runs" hidden><span class="cnt" id="cnt-runs">0</span><div class="list" id="runs"></div></div>
    </details>
  </section>
</main>
<footer>자동 새로고침 3초 · <span id="upd"></span></footer>
<div id="dv" hidden>
  <header>
    <button class="lnk" id="dv-back" title="뒤로 (Esc)">←</button>
    <div class="htitle" id="dv-title">상세</div>
    <button class="lnk" id="dv-reload" title="새로고침">↻</button>
  </header>
  <main class="wrap" id="dv-body"></main>
</div>
<script>
// The shell above is static markup. tick() mounts delegated handlers ONCE, then
// each 3s poll only rewrites the leaf list bodies + metric numbers — it never
// wipes the whole #app — so open <details>, page scroll, and 더보기 stay put.
const $=id=>document.getElementById(id);
const esc=s=>(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const esca=s=>(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const badge=s=>{const m={done:'b-done',failed:'b-fail',deferred:'b-def',in_progress:'b-run',dispatched:'b-run',awaiting_human:'b-wait'};return `<span class="badge ${m[s]||'b-gray'}">${esc(s)}</span>`};
function pcolor(s){let h=0;for(const c of(s||''))h=(h*31+c.charCodeAt(0))>>>0;return h%360}
function copyBtn(id){return `<button class="cp" data-cp="${esca(id)}" title="task id 복사: ${esca(id)}">⧉ id</button>`}
function pchip(p){if(!p)return '';const h=pcolor(p);return `<span class="pchip" style="color:hsl(${h},65%,74%);border-color:hsl(${h},35%,44%)" title="프로젝트: ${esca(p)}">${esc(p)}</span>`}
function tlabel(slug,title,project){return `${pchip(project)}<span class="ttl">${esc(title||slug)}</span>`}
// List rows carry no ⧉ id button (info diet); only the task/run detail views do.
function tlabelCp(slug,title,project){return tlabel(slug,title,project)+' '+copyBtn(slug)}
function stages(st){return (st||[]).map(s=>`<span class="stg ${s.rc===0?'s-ok':'s-bad'}" title="${esc(s.name)} rc=${s.rc}">${esc((s.name[0]||'?').toUpperCase())}</span>`).join('')}
const empty=t=>`<div class="empty">${t}</div>`;
const dur=s=>{if(s==null)return '—';if(s<=0)return '지금';const h=Math.floor(s/3600),m=Math.floor((s%3600)/60);return h?`${h}시간 ${m}분`:`${m}분`};
const at=e=>e==null?'':new Date(e*1000).toLocaleString('ko-KR',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false});

const LIM=10, PROJLIM=5, ACTLIM=6, showAll={runs:false,recent:false,projects:false,active:false};
let last=null, mounted=false;

const WHY={parked:['검토대기','b-wait'],owned:['결정필요','b-pur'],blocked:['블록','b-fail'],manual:['수동','b-gray'],backlog:['미설정','b-blu']};
const whyBadge=w=>{const m=WHY[w]||['?','b-gray'];return `<span class="badge ${m[1]}">${m[0]}</span>`};

const md=s=>s?String(s).slice(5).replace('-','/').replace('T',' '):'';
const humanRow=r=>`<div class="row clk" data-open="task:${esca(r.slug)}"><div class="main"><div>${tlabel(r.slug,r.title,r.project)}</div><div class="note">${esc(r.action)||'—'}</div></div><div class="rmeta">${r.later&&r.not_before?`<span class="age">${esc(md(r.not_before))}부터</span> `:''}${r.due?`<span class="age">~${esc(md(r.due))}</span> `:''}${whyBadge(r.why)}</div></div>`;
const attnRow=r=>`<div class="row clk" data-open="task:${esca(r.task)}"><div class="main"><div>${tlabel(r.task,r.title,r.project)}</div><div class="note">${esc(r.note)||'—'}</div></div><div class="rmeta">${badge(r.state)}${r.capacity_limited?' <span class="badge b-gray">capacity</span>':''}${r.refail?` <span class="age">↻${r.refail}</span>`:''} <span class="age mono">${esc(r.age)}</span></div></div>`;
const activeRow=r=>`<div class="row clk" data-open="task:${esca(r.task)}"><div class="main"><div>${tlabel(r.task,r.title,r.project)}</div>${(r.agent&&!r.agent_is_default)?`<div class="note">agent ${esc(r.agent)}</div>`:''}</div><div class="rmeta">${badge(r.state)} <span class="age mono">${esc(r.age)}</span></div></div>`;
const PST={waiting:['대기','b-wait'],blocked:['블록','b-fail']};
const projRow=r=>{const t=r.leaves_total||0,dn=r.leaves_done||0,pc=t?Math.round(dn*100/t):0,st=PST[r.status];
  return `<div class="row clk prj" data-open="task:${esca(r.slug)}"><div class="main"><div>${tlabel(r.slug,r.title,r.project)}</div></div><div class="rmeta">${r.active?`<span class="badge b-run">● ${r.active}</span> `:''}${r.attention?`<span class="badge b-fail">⚠ ${r.attention}</span> `:''}${st?`<span class="badge ${st[1]}">${st[0]}</span> `:''}${r.last_age?`<span class="age" title="최근 활동 ${esca(r.last_activity)} (run·ledger·md·session 중 최신)">${esc(r.last_age)} 전</span> `:''}<span class="pbar" title="${dn}/${t} 완료"><i style="width:${pc}%"></i></span> <span class="age mono">${dn}/${t}</span></div></div>`};
const runRow=r=>{const w=r.when||'';return `<div class="row clk" data-open="run:${esca(r.run)}"><div class="main"><div>${tlabel(r.slug,r.title,r.project)} <span class="age mono">${esc(w.slice(0,2))}:${esc(w.slice(2,4))}</span></div><div class="note">${stages(r.stages)||'—'}${(r.artifacts||[]).length?' · '+esc((r.artifacts||[]).join(', ')):''}</div></div></div>`};
const recentRow=r=>`<div class="row clk" data-open="task:${esca(r.task)}"><div class="main"><div>${tlabel(r.task,r.title,r.project)}</div><div class="note">${esc(r.note)||'—'}</div></div><div class="rmeta">${badge(r.state)}${r.todo_status&&r.todo_status!=='open'?` <span class="age">(${esc(r.todo_status)})</span>`:''} <span class="age mono">${esc(r.age)}</span></div></div>`;
const agentRow=a=>`<div class="row"><div class="main"><div><span class="dot" style="display:inline-block;width:9px;height:9px;margin-right:6px;background:${a.alive?'var(--grn)':'var(--mut)'}"></span>${esc(a.id)} <span class="note" style="display:inline">${esc(a.kind)}</span></div><div class="note">${esc(a.host)||'—'}</div></div><span class="age mono">${a.age?esc(a.age)+' 전':'—'}</span></div>`;
const depRow=r=>`<div class="row"><div class="main"><div class="ttl mono">${esc(r.name)}</div><div class="note">${r.provides?'provides: '+esc(r.provides):'—'}${(r.depends_on||[]).length?' · ← '+r.depends_on.map(esc).join(', '):''}${(r.consumers||[]).length?' · → '+r.consumers.map(esc).join(', '):''}</div></div></div>`;

// 큐 (docs/dashboard-parity.md "큐"): the server judges `wait`; rows render in the
// order received (tick order). Existing badge classes only — no new colours.
const QW={next_tick:['다음 틱','b-run'],checkout_busy:['리포 사용중','b-def'],gradle_cap:['빌드 상한','b-def'],usage_hold:['HOLD','b-wait'],paused:['PAUSED','b-wait'],budget:['예산','b-wait'],cooldown:['재시도 대기','b-gray'],capacity:['재시도 대기','b-gray']};
const qwBadge=w=>{const m=QW[w]||[w||'?','b-gray'];return `<span class="badge ${m[1]}">${esc(m[0])}</span>`};
const queueRow=(r,i)=>`<div class="row clk" data-open="task:${esca(r.slug)}"><div class="main"><div><span class="age mono">${i+1}</span> ${tlabel(r.slug,r.title,r.project)}</div><div class="note">${[esc(r.wait_text),r.holder?'← '+esc(r.holder):'',r.since?esc(md(String(r.since).slice(0,16)))+'부터':''].filter(Boolean).join(' · ')||'—'}</div></div><div class="rmeta">${qwBadge(r.wait)}</div></div>`;
const upcomingRow=r=>`<div class="row clk" data-open="task:${esca(r.slug)}"><div class="main"><div>${tlabel(r.slug,r.title,r.project)}</div></div><div class="rmeta"><span class="age">${esc(md(r.not_before))}부터</span></div></div>`;
const blockedRow=r=>`<div class="row clk" data-open="task:${esca(r.slug)}"><div class="main"><div>${tlabel(r.slug,r.title,r.project)}</div></div><div class="rmeta"><span class="age qdeps">${(r.waiting_on||[]).length?'← '+r.waiting_on.map(esc).join(', '):'선행 완료 — 언블락 대기'}</span></div></div>`;
// Open/closed of the queue <details> survives reloads under the contract keys
// mdash.sec.queue / mdash.sec.queue-upcoming / mdash.sec.queue-blocked.
const QSEC={'d-queue':'queue','d-queue-upcoming':'queue-upcoming','d-queue-blocked':'queue-blocked'};
const secKey=s=>'mdash.sec.'+s;
function secMount(){for(const [id,s] of Object.entries(QSEC)){const el=$(id);if(!el)continue;
  try{el.open=localStorage.getItem(secKey(s))==='1'}catch(_){}
  el.addEventListener('toggle',()=>{try{localStorage.setItem(secKey(s),el.open?'1':'0')}catch(_){}})}}

const fill=(id,list,render,emptyMsg)=>{$(id).innerHTML=list.length?list.map(render).join(''):empty(emptyMsg)};
// Fold: within ONE section, rows sharing a non-empty `group` (count>=2) collapse into
// a single tappable header row placed at the group's first appearance (default
// collapsed). Open/closed is kept per section+group in localStorage so it survives both
// the 3s poll (leaf innerHTML swap) and a full reload. group='' or singletons stay as
// standalone rows, rendered in place by the section's own row renderer.
// Theme toggle: header button cycles auto→light→dark→auto. auto leaves <html>
// with no data-theme (CSS falls back to prefers-color-scheme). The pre-paint
// <head> script already set data-theme from storage before first paint; here we
// keep the button label in sync and handle clicks. Storage key = 'mdash.theme'.
const THEMES=['auto','light','dark'];
const THEME_LBL={auto:'◐ 자동',light:'☀ 라이트',dark:'🌙 다크'};
const themeGet=()=>{try{const t=localStorage.getItem('mdash.theme');return THEMES.includes(t)?t:'auto'}catch(_){return 'auto'}};
const themeApply=t=>{const el=document.documentElement;if(t==='light'||t==='dark')el.dataset.theme=t;else delete el.dataset.theme;const b=$('theme');if(b)b.textContent=THEME_LBL[t]||THEME_LBL.auto};
const themeSet=t=>{try{localStorage.setItem('mdash.theme',t)}catch(_){}themeApply(t)};

const grpKey=(sec,g)=>'mdash.grp.'+sec+'.'+g;
const grpOpen=(sec,g)=>{try{return localStorage.getItem(grpKey(sec,g))==='1'}catch(_){return false}};
const grpSet=(sec,g,v)=>{try{localStorage.setItem(grpKey(sec,g),v?'1':'0')}catch(_){}};
function foldFill(id,sec,list,render,emptyMsg){
  if(!list.length){$(id).innerHTML=empty(emptyMsg);return}
  const cnt={};for(const r of list){const g=r.group||'';if(g)cnt[g]=(cnt[g]||0)+1}
  const seen={};let h='';
  for(const r of list){
    const g=r.group||'';
    if(g&&cnt[g]>=2){
      if(seen[g])continue;                         // fold emitted at first appearance
      seen[g]=1;
      const op=grpOpen(sec,g);
      const kids=list.filter(x=>(x.group||'')===g).map(render).join('');
      h+=`<div class="grp"><div class="row grphead clk" data-gsec="${esca(sec)}" data-grp="${esca(g)}">${pchip(g)}<span class="cnt">${cnt[g]}건</span><span class="gar">${op?'▾':'▸'}</span></div><div class="grpkids"${op?'':' hidden'}>${kids}</div></div>`;
    }else h+=render(r);                             // singleton / no-group → standalone
  }
  $(id).innerHTML=h;
}
function fillMore(sec,list,render){
  const shown=showAll[sec]?list:list.slice(0,LIM);
  $(sec).innerHTML=list.length?shown.map(render).join(''):empty('없음');
  const btn=document.querySelector(`.more[data-sec="${sec}"]`);
  if(btn){if(list.length>LIM){btn.hidden=false;btn.textContent=showAll[sec]?'접기':`더보기 (+${list.length-LIM})`}else btn.hidden=true}
}
// Unified history: runs ∪ recent, deduped by slug (a run wins over a ledger-only
// recent — it carries more of "what happened"), newest first. Time key uses only
// existing fields: run `YYYYMMDDTHHMMSS-…`[:15] and recent `updated` ISO, both
// stripped to digits (YYYYMMDDHHMMSS) and compared as strings. Each row keeps its
// own click target (run→run:, task→task:) via its original renderer.
const histRow=h=>h.k==='run'?runRow(h.row):recentRow(h.row);
function renderHistory(d){
  const seen={},hist=[];
  for(const r of (d.runs||[])){const s=r.slug||'';if(s)seen[s]=1;
    hist.push({k:'run',slug:s,t:(r.run||'').slice(0,15).replace(/\\D/g,''),row:r})}
  for(const r of (d.recent||[])){const s=r.task||'';if(s&&seen[s])continue;
    hist.push({k:'task',slug:s,t:(r.updated||'').replace(/\\D/g,''),row:r})}
  hist.sort((a,b)=>b.t.localeCompare(a.t));
  $('cnt-recent').textContent=hist.length;
  const shown=showAll.recent?hist:hist.slice(0,LIM);
  $('recent').innerHTML=hist.length?shown.map(histRow).join(''):empty('없음');
  const btn=document.querySelector('.more[data-sec="recent"]');
  if(btn){if(hist.length>LIM){btn.hidden=false;btn.textContent=showAll.recent?'접기':`더보기 (+${hist.length-LIM})`}else btn.hidden=true}
}

// worker_state (from /api/status) → badge label / short tag / color / live dot.
// `label` reads in the big state line, `hs` is the compact header chip.
const WSTATE={
  paused:        {label:'⏸ PAUSED',   hs:'PAUSED', color:'var(--yel)', live:false},
  hold:          {label:'⛔ usage HOLD',hs:'HOLD',  color:'var(--red)', live:false},
  budget:        {label:'⏳ 예산 도달', hs:'budget', color:'var(--yel)', live:false},
  running:       {label:'● 워커 실행중', hs:'실행중', color:'var(--grn)', live:true},
  post_run:      {label:'◍ 후처리 중',  hs:'후처리', color:'var(--grn)', live:true},
  waiting_serial:{label:'◷ 직렬 대기',  hs:'직렬대기',color:'var(--yel)', live:false},
  no_work:       {label:'○ 유휴 (idle)',hs:'idle',  color:'var(--mut)', live:false},
};
function setState(d){
  const sl=$('state-line'),hs=$('hstate');
  // Current Claude account (/api/status.account, leaf ③): the server decides, this only renders.
  // Missing key (older server) → blank; null active → '계정: -'. Leaf ④: ' · PAUSED' when
  // every account is unavailable (a.paused); accounts[]/last_switch are not rendered here.
  // Multi-account capacity: '슬롯 used/max (name used/max, …)' from a.slots, which the
  // server (collect_account_slots) computes from the runner's snapshot — rendered only when a
  // runner has reported (total_max != null), else omitted. Server computes, client renders.
  // Off the first-screen account line (info diet) → #sys-slots inside 시스템 상세.
  const ae=$('state-acct');
  if(ae){const a=d.account;ae.textContent=a?('계정: '+(a.active?`${a.active} (${a.source})`:'-')+(a.paused?' · PAUSED':'')):'';}
  const se=$('sys-slots');
  if(se){const a=d.account,s=a&&a.slots;
    if(s&&s.total_max!=null){
      const pa=(s.per_account||[]).map(r=>`${r.name} ${r.used}/${r.max==null?'-':r.max}`).join(', ');
      se.textContent=`슬롯 ${s.total_used}/${s.total_max}`+(pa?` (${pa})`:'');}
    else se.textContent='';}
  const m=d.worker_state&&WSTATE[d.worker_state];
  if(m){
    sl.textContent=m.label;sl.style.color=m.color;
    hs.textContent=m.hs;hs.style.color=m.color;hs.style.borderColor=m.color;
    $('hdot').style.background=m.color;$('hdot').className='dot'+(m.live?' live':'');
    // The server already says *why* in one safe line; show it verbatim, but for
    // a usage HOLD prefer the live release countdown when the guard provides one.
    let sub=d.worker_state_detail||'';
    if(d.worker_state==='hold'&&d.usage){const s=d.usage.blocked_in_s;
      if(s!=null&&s>0)sub=`HOLD 해제 ${dur(s)} 뒤`}
    $('state-sub').textContent=sub;
    return;
  }
  // Fallback: an older server without worker_state — the legacy paused/hold/busy/idle logic.
  const paused=d.paused,busy=d.worker_busy,hold=d.usage&&!d.usage.allowed;
  let label,color,live=false;
  if(paused){label='⏸ PAUSED';color='var(--yel)'}
  else if(hold){label='⛔ usage HOLD';color='var(--red)'}
  else if(busy){label='● 워커 실행중';color='var(--grn)';live=true}
  else{label='○ 유휴 (idle)';color='var(--mut)'}
  sl.textContent=label;sl.style.color=color;
  hs.textContent=paused?'PAUSED':(hold?'HOLD':(busy?'실행중':'idle'));
  hs.style.color=color;hs.style.borderColor=color;
  $('hdot').style.background=color;$('hdot').className='dot'+(live?' live':'');
  let sub='';
  if(hold&&d.usage){const s=d.usage.blocked_in_s;sub=s!=null&&s>0?`HOLD 해제 ${dur(s)} 뒤`:'다음 guard 틱에 해제 예정'}
  else if(busy){sub=`${d.active.length} task 실행 중`}
  $('state-sub').textContent=sub;
}

function setUsage(u){
  const box=$('usage');
  if(!u){box.hidden=true;box.innerHTML='';return}
  box.hidden=false;
  const pc=v=>v==null?'—':v+'%';
  const col=v=>v==null?'var(--mut)':(v>=80?'var(--red)':(v>=50?'var(--yel)':'var(--grn)'));
  let h=`<div class="ur"><div class="u"><b style="color:${col(u.session)}">${pc(u.session)}</b><span>session (5h)</span></div><div class="u"><b style="color:${col(u.weekly)}">${pc(u.weekly)}</b><span>weekly (7d)</span></div><div class="u"><b style="color:${u.allowed?'var(--grn)':'var(--red)'}">${u.allowed?'OPEN':'HOLD'}</b><span>guard</span></div></div>`;
  if(!u.allowed){
    h+=`<div class="note" style="color:var(--yel);margin-top:7px">HOLD 해제 ${u.blocked_in_s!=null&&u.blocked_in_s>0?dur(u.blocked_in_s)+' 뒤':(u.blocked_in_s!=null?'예정 시각 지남 — 다음 guard 틱에 해제':'시각 미상')}${u.blocked_until?` (${at(u.blocked_until)})`:''}</div>`;
    if(u.reason)h+=`<div class="note" style="color:var(--yel)">${esc(u.reason)}</div>`;
  }
  h+=`<div class="note" style="margin-top:7px">reset · session ${at(u.session_resets_at)||'—'} (${dur(u.session_resets_in_s)} 뒤) · weekly ${at(u.weekly_resets_at)||'—'} (${dur(u.weekly_resets_in_s)} 뒤)</div>`;
  if(u.age_s!=null&&u.age_s>3600)h+=`<div class="note">usage 데이터 ${Math.round(u.age_s/60)}분 전 (stale)</div>`;
  box.innerHTML=h;
}

function update(d){
  setState(d);
  $('clock').textContent=(d.now||'').split(' ')[1]||d.now||'';
  $('upd').textContent='마지막 갱신 '+(d.now||'');
  $('mv-runs').textContent=d.runs_today;
  $('mv-active').textContent=d.active.length;
  $('mv-queue').textContent=(d.todo&&d.todo.dispatchable)||0;
  $('mv-attn').textContent=d.attention.length;
  const urgent=d.human.filter(r=>r.urgent),rest=d.human.filter(r=>!r.urgent),later=d.human_later||[];
  $('mv-human').textContent=urgent.length;
  $('mv-open').textContent=(d.todo&&d.todo.open)||0;
  setUsage(d.usage);

  const blocked=rest.filter(r=>r.why==='blocked'),open=rest.filter(r=>r.why!=='blocked');
  const life=open.filter(r=>r.category!=='dev'),dev=open.filter(r=>r.category==='dev');
  $('cnt-blocked').textContent=blocked.length;
  foldFill('human-blocked','human-blocked',blocked,humanRow,'없음');
  $('cnt-urgent').textContent=urgent.length;$('cnt-later').textContent=later.length;
  $('cnt-life').textContent=life.length;$('cnt-dev').textContent=dev.length;
  foldFill('human-urgent','human-urgent',urgent,humanRow,'급하게 결정할 일 없음 ✓');
  foldFill('human-life','human-life',life,humanRow,'없음');
  foldFill('human-dev','human-dev',dev,humanRow,'없음');
  foldFill('human-later','human-later',later,humanRow,'없음');
  // empty sub-blocks fold away (human-urgent keeps its ✓ empty line; c-human stays).
  $('d-humanlife').hidden=!life.length;
  $('d-humandev').hidden=!dev.length;
  $('d-humanblocked').hidden=!blocked.length;
  $('d-humanlater').hidden=!later.length;

  // open projects on the first screen (top PROJLIM + 더보기); waiting/blocked fold
  // under a collapsed 보류. cnt-proj stays the full open count.
  const projs=d.projects||[],openP=projs.filter(p=>!p.on_hold),holdP=projs.filter(p=>p.on_hold);
  $('cnt-proj').textContent=openP.length;
  const shownP=showAll.projects?openP:openP.slice(0,PROJLIM);
  fill('projects',shownP,projRow,'진행 중인 프로젝트 없음');
  const pbtn=document.querySelector('.more[data-sec="projects"]');
  if(pbtn){if(openP.length>PROJLIM){pbtn.hidden=false;pbtn.textContent=showAll.projects?'접기':`더보기 (+${openP.length-PROJLIM})`}else pbtn.hidden=true}
  $('cnt-projhold').textContent=holdP.length;
  $('d-projhold').hidden=!holdP.length;
  fill('projects-hold',holdP,projRow,'없음');

  $('cnt-attn').textContent=d.attention.length;
  $('c-attention').hidden=!d.attention.length;   // 깨진 것 0건이면 카드 자체 숨김
  foldFill('attention','attention',d.attention,attnRow,'주의가 필요한 항목 없음 ✓');

  // 진행 중: top ACTLIM on the first screen (folded by project), rest behind 더보기.
  // cnt-active stays the full count; foldFill keeps its grouping on the shown slice.
  $('cnt-active').textContent=d.active.length;
  const shownA=showAll.active?d.active:d.active.slice(0,ACTLIM);
  foldFill('active','active',shownA,activeRow,'지금 실행 중인 task 없음');
  const abtn=document.querySelector('.more[data-sec="active"]');
  if(abtn){if(d.active.length>ACTLIM){abtn.hidden=false;abtn.textContent=showAll.active?'접기':`더보기 (+${d.active.length-ACTLIM})`}else abtn.hidden=true}

  // 큐: server order as-is, no group folding (the order is the information).
  const q=d.queue||[],qu=d.queue_upcoming||[],qb=d.queue_blocked||[];
  $('cnt-queue').textContent=q.length;
  fill('queue',q,queueRow,'큐 비어 있음');
  $('cnt-queue-upcoming').textContent=qu.length;
  $('cnt-queue-blocked').textContent=qb.length;
  fill('queue-upcoming',qu,upcomingRow,'없음');
  fill('queue-blocked',qb,blockedRow,'없음');
  $('d-queue-upcoming').hidden=!qu.length;
  $('d-queue-blocked').hidden=!qb.length;

  renderHistory(d);
  $('cnt-runs').textContent=d.runs.length;
  // small, non-showy: how many of the detail subsections have any content (0–4).
  $('cnt-sys').textContent=[d.runs.length||d.recent.length,d.agents.length,
    (d.deps||[]).length,Object.keys((d.todo&&d.todo.by_mode)||{}).length].filter(n=>n>0).length;

  $('cnt-agents').textContent=d.agents.length;
  fill('agents',d.agents,agentRow,'없음');

  const modes=Object.entries((d.todo&&d.todo.by_mode)||{});
  $('cnt-mode').textContent=modes.length;
  $('mode').innerHTML=modes.length?modes.map(([k,v])=>`<span>${esc(k)}: ${v}</span>`).join(''):empty('없음');

  $('cnt-deps').textContent=d.deps.length;
  fill('deps',d.deps,depRow,'선언된 크로스-프로젝트 의존성 없음');
}

// ---- tap-through detail (run / task) ----
// A stack of 'run:<id>' / 'task:<slug>' refs mirrored into history, so the
// browser/phone back gesture steps back through details and finally closes.
const dstack=[];
const kv=(k,v)=>v?`<div class="kv"><div class="k">${esc(k)}</div><div class="v">${esc(String(v))}</div></div>`:'';
const pre=(t,mono)=>`<div class="pre${mono?' mono':''}">${esc(t)||'—'}</div>`;
const sz=n=>n<1000?`${n}자`:`${(n/1000).toFixed(1)}k자`;
const hm=w=>(w||'').length>=4?`${w.slice(0,2)}:${w.slice(2,4)}`:'';
const card=(inner)=>`<section class="card">${inner}</section>`;
function ctxCard(t,l){
  if(!Object.keys(t).length&&!l.state)return '';
  const meta=[l.state?badge(l.state):'',t.status?`<span class="age">todo: ${esc(t.status)}</span>`:'',
    t.worker_mode?`<span class="age">· ${esc(t.worker_mode)}</span>`:'',l.refail?`<span class="age">↻${l.refail}</span>`:'',
    t.due&&t.due!=='-'?`<span class="age">due ${esc(t.due)}</span>`:'',l.age?`<span class="age mono">${esc(l.age)}</span>`:''].filter(Boolean).join(' ');
  return card(`<h2>태스크</h2><div class="rmeta" style="justify-content:flex-start">${meta}</div>${kv('완료 조건',t.outcome)}${kv('다음 행동',t.next_action)}${kv('If-Then',t.if_then)}${kv('워커 메모',l.note)}${kv('agent',l.agent)}`);
}
function runDetail(d){
  const arts=d.artifacts||[];
  const st=(d.stages||[]).map(s=>`<span style="white-space:nowrap">${stages([s])}${esc(s.name)} rc=${s.rc}</span>`).join(' &nbsp;');
  return card(`<h2>${esc(d.date)} ${hm(d.when)}</h2><div>${tlabelCp(d.slug,d.title,d.project)}</div><div class="note mono">${esc(d.slug)}</div>${st?`<div class="note" style="margin-top:8px">${st}</div>`:''}<div style="margin-top:10px"><button class="lnk" data-open="task:${esca(d.slug)}">태스크 상세 →</button></div>`)
    +ctxCard(d.task||{},d.ledger||{})
    +card(`<h2>산출물 <span class="cnt">${arts.length}</span></h2>${arts.length?arts.map((a,i)=>`<details${i===arts.length-1?' open':''}><summary>${esc(a.name)} <span class="age">${sz(a.size||0)}</span></summary>${pre(a.content)}${a.truncated?'<div class="note">— 서버 상한에서 잘림 —</div>':''}</details>`).join(''):empty('산출물 없음')}`)
    +card(d.log_tail?`<details><summary>run.log ${d.log_truncated?'<span class="age">끝부분만</span>':''}</summary>${pre(d.log_tail,1)}</details>`:empty('run.log 없음'));
}
const taskRunRow=r=>{const n=r.run||'';return `<div class="row clk" data-open="run:${esca(n)}"><div class="main"><div class="mono">${esc(n.slice(0,4))}-${esc(n.slice(4,6))}-${esc(n.slice(6,8))} ${hm(r.when)}</div><div class="note">${stages(r.stages)||'—'}${(r.artifacts||[]).length?' · '+esc(r.artifacts.join(', ')):''}</div></div><span class="age">›</span></div>`};
function taskDetail(d){
  const runs=d.runs||[];
  return card(`<div>${tlabelCp(d.slug,d.title,d.project)}</div><div class="note mono">${esc(d.slug)}</div>`)
    +ctxCard(d.task||{},d.ledger||{})
    +card(`<h2>실행 <span class="cnt">${runs.length}</span></h2><div class="list">${runs.length?runs.map(taskRunRow).join(''):empty('실행 기록 없음')}</div>`)
    +(d.body?card(`<details open><summary>태스크 본문</summary>${pre(d.body)}${d.body_truncated?'<div class="note">— 서버 상한에서 잘림 —</div>':''}</details>`):'');
}
async function showDetail(ref){
  const i=ref.indexOf(':'),kind=ref.slice(0,i),id=ref.slice(i+1),body=$('dv-body');
  $('dv').hidden=false;document.body.style.overflow='hidden';
  $('dv-title').textContent=kind==='run'?'실행 상세':'태스크 상세';
  body.innerHTML=card(empty('불러오는 중…'));$('dv').scrollTop=0;
  try{
    const r=await fetch(kind==='run'?`/api/run?id=${encodeURIComponent(id)}`:`/api/task?slug=${encodeURIComponent(id)}`);
    if(dstack[dstack.length-1]!==ref)return;          // user already moved on
    if(r.status===404){body.innerHTML=card(empty(kind==='run'?'실행 기록을 찾을 수 없음':'태스크를 찾을 수 없음'));return}
    if(!r.ok)throw new Error(r.status);
    const d=await r.json();
    if(dstack[dstack.length-1]!==ref)return;
    body.innerHTML=kind==='run'?runDetail(d):taskDetail(d);
  }catch(e){if(dstack[dstack.length-1]===ref)body.innerHTML=card(empty('불러오기 실패 — ↻로 다시 시도'))}
}
function openDetail(ref){dstack.push(ref);history.pushState({dv:dstack.length},'');showDetail(ref)}
function closeDetail(){$('dv').hidden=true;$('dv-body').innerHTML='';document.body.style.overflow=''}
window.addEventListener('popstate',()=>{dstack.pop();if(dstack.length)showDetail(dstack[dstack.length-1]);else closeDetail()});

async function cp(el){try{await navigator.clipboard.writeText(el.dataset.cp);const o=el.innerHTML;el.innerHTML='✓ 복사됨';el.classList.add('ok');setTimeout(()=>{el.innerHTML=o;el.classList.remove('ok')},1000)}catch(_){el.innerHTML='✗'}}

function mount(){
  // delegated on document so it survives every leaf innerHTML swap
  themeApply(themeGet());                    // sync button label (pre-paint set data-theme)
  secMount();                                // restore the queue sections' open state
  document.addEventListener('click',e=>{
    const tb=e.target.closest('#theme');if(tb){const i=THEMES.indexOf(themeGet());themeSet(THEMES[(i+1)%3]);return}
    const c=e.target.closest('.cp');if(c){e.stopPropagation();cp(c);return}
    if(e.target.closest('#dv-back')){history.back();return}
    if(e.target.closest('#dv-reload')){if(dstack.length)showDetail(dstack[dstack.length-1]);return}
    const gh=e.target.closest('[data-grp]');   // group header (no data-open) → toggle fold
    if(gh){const gs=gh.dataset.gsec,g=gh.dataset.grp,now=!grpOpen(gs,g);grpSet(gs,g,now);
      const kids=gh.parentElement.querySelector('.grpkids');if(kids)kids.hidden=!now;
      const ar=gh.querySelector('.gar');if(ar)ar.textContent=now?'▾':'▸';return}
    const o=e.target.closest('[data-open]');if(o){openDetail(o.dataset.open);return}
    const m=e.target.closest('.more');if(m){const sec=m.dataset.sec;showAll[sec]=!showAll[sec];
      if(last){if(sec==='recent')renderHistory(last);else update(last)}}
  });
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&dstack.length)history.back()});
  mounted=true;
}

async function tick(){
  try{const r=await fetch('/api/status');last=await r.json();if(!mounted)mount();update(last)}
  catch(e){const hs=$('hstate');hs.textContent='연결 끊김';hs.style.color='var(--red)'}
}
tick();setInterval(tick,3000);
</script></body></html>"""
HTML = HTML.replace("__FAVICON__", FAVICON_HREF)


COOKIE = "macp_dash"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS


# Auth-failure audit log. NullHandler keeps it silent on import (tests capture it
# with assertLogs); main() calls logging.basicConfig so a running service records
# failed attempts to its journal.
logger = logging.getLogger("macp.dashboard")
logger.addHandler(logging.NullHandler())


# -- SMS ingest-failure alert ------------------------------------------------
# A rejected phone POST (401/429/400/413) raises one notifier alert per kind per
# 6h (`sms.should_alert`, file-backed so restarts can't flood). Fail-open: it runs
# on a daemon thread and swallows every error, so the HTTP reply and storage
# never change.
def _send_ingest_alert(message: str) -> None:
    # Imported here, not at module top: `worker` pulls in the orchestrator, which
    # the engine-isolation note near `_checkout_key` keeps out of this module.
    # Going through worker.kakao_notify also keeps the test conftest guard on it.
    from todo_system import worker
    worker.kakao_notify(message)


SMS_ALERT_NOTIFY = _send_ingest_alert  # seam: looked up at call time, tests patch it

_alert_threads: list[threading.Thread] = []
_alert_threads_lock = threading.Lock()


def _run_ingest_alert(kind: str, code: int) -> None:
    try:
        if sms.should_alert(kind):
            SMS_ALERT_NOTIFY(sms.compose_ingest_alert(kind, code))
    except Exception as e:
        # Type name only: an exception message could echo request data.
        logger.warning("sms ingest alert dropped kind=%s code=%d err=%s",
                       kind, code, type(e).__name__)


def _dispatch_ingest_alert(kind: str, code: int) -> None:
    try:
        t = threading.Thread(target=_run_ingest_alert, args=(kind, code),
                             name=f"sms-alert-{kind}", daemon=True)
        with _alert_threads_lock:
            _alert_threads[:] = [x for x in _alert_threads if x.is_alive()]
            _alert_threads.append(t)
        t.start()
    except Exception as e:
        logger.warning("sms ingest alert not dispatched kind=%s err=%s", kind, type(e).__name__)


def wait_ingest_alerts(timeout: float = 5.0) -> bool:
    """Join pending alert threads (tests and ops). True if all finished in time."""
    deadline = time.monotonic() + timeout
    with _alert_threads_lock:
        pending = list(_alert_threads)
    for t in pending:
        t.join(max(0.0, deadline - time.monotonic()))
    return not any(t.is_alive() for t in pending)


class Handler(BaseHTTPRequestHandler):
    # Shared secret; None = open (only allowed on loopback, see `serve`).
    token: str | None = None

    # Auth-failure rate limit. serve() injects a FRESH per-server _auth_failures /
    # _auth_lock into every BoundHandler so servers (and tests) never share state;
    # the class-level values here are only a fallback for direct instantiation.
    RL_WINDOW_S = 60
    RL_MAX_FAILS = 5
    _auth_failures: dict[str, list[float]] = {}
    _auth_lock = threading.Lock()

    def log_message(self, *a):  # quiet
        pass

    # -- auth ---------------------------------------------------------------
    def _presented(self) -> tuple[str | None, bool]:
        """(secret the client presented, came_from_query). Query wins so a fresh
        `/?token=` link can replace a stale cookie."""
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query).get("token")
        if q:
            return q[0], True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip(), False
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        if COOKIE in jar:
            return jar[COOKIE].value, False
        return None, False

    def _authorized(self) -> tuple[bool, bool]:
        """(ok, set_cookie). Constant-time compare so the token can't be guessed
        byte by byte from response timing."""
        if self.token is None:
            return True, False
        got, from_query = self._presented()
        if got is None or not hmac.compare_digest(got, self.token):
            return False, False
        return True, from_query

    def _note_auth_failure(self) -> bool:
        """Record an auth failure for this client IP and return True once it is over
        the sliding-window threshold (caller answers 429 instead of 401). Logs the
        failure with the IP and the request PATH ONLY — never the presented secret
        (a token can arrive in `?token=`, so self.path is deliberately not logged)."""
        ip = self.client_address[0]
        path = urlparse(self.path).path
        now = time.monotonic()
        with self._auth_lock:
            recent = [t for t in self._auth_failures.get(ip, ()) if now - t < self.RL_WINDOW_S]
            recent.append(now)
            self._auth_failures[ip] = recent
            over = len(recent) >= self.RL_MAX_FAILS
        logger.warning("dashboard auth failure ip=%s path=%s fails=%d%s",
                       ip, path, len(recent), " -> rate-limited (429)" if over else "")
        return over

    def _reply(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        ok, set_cookie = self._authorized()
        if not ok:
            if self._note_auth_failure():
                self._reply(429, b"429 too many failed attempts; slow down\n",
                            "text/plain; charset=utf-8",
                            {"Retry-After": str(self.RL_WINDOW_S)})
            else:
                self._reply(401, b"401 unauthorized: open /?token=<MACP_DASHBOARD_TOKEN>\n",
                            "text/plain; charset=utf-8")
            return
        extra = {}
        if set_cookie:
            # 30 days; HttpOnly keeps it out of page JS. `Secure` is deliberately
            # off: tailscale serve terminates TLS and forwards plain http here.
            extra["Set-Cookie"] = f"{COOKIE}={self.token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict"
        if urlparse(self.path).path == "/api/status":
            self._reply(200, json.dumps(collect_status()).encode("utf-8"),
                        "application/json", extra)
        elif urlparse(self.path).path == "/api/task":
            slug = (parse_qs(urlparse(self.path).query).get("slug") or [""])[0]
            detail = collect_task_detail(slug)
            if detail is None:
                self._reply(404, b'{"error": "task not found"}', "application/json", extra)
            else:
                self._reply(200, json.dumps(detail).encode("utf-8"),
                            "application/json", extra)
        elif urlparse(self.path).path == "/api/run":
            run_id = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            detail = collect_run_detail(run_id)
            if detail is None:
                self._reply(404, b'{"error": "run not found"}', "application/json", extra)
            else:
                self._reply(200, json.dumps(detail).encode("utf-8"),
                            "application/json", extra)
        elif urlparse(self.path).path == "/sms-setup":
            self._reply(200, self._sms_setup_page().encode("utf-8"),
                        "text/html; charset=utf-8", extra)
        elif urlparse(self.path).path.startswith("/api/"):
            self._reply(404, b'{"error": "not found"}', "application/json", extra)
        else:
            self._reply(200, HTML.encode("utf-8"), "text/html; charset=utf-8", extra)


    def _sms_setup_page(self) -> str:
        """One-tap phone setup: a `macpsms://setup` link carrying this front's
        ingest URL and MACP_SMS_TOKEN. Behind the dashboard token, which already
        outranks the write-only SMS token."""
        sms_token = os.environ.get("MACP_SMS_TOKEN", "").strip()
        if not sms_token:
            body = "<p>MACP_SMS_TOKEN is not set on the host.</p>"
        else:
            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host", "")
            local = host.split(":")[0] in ("127.0.0.1", "localhost", "::1", "")
            scheme = self.headers.get("X-Forwarded-Proto") or ("http" if local else "https")
            endpoint = f"{scheme}://{host}/api/sms"
            link = f"macpsms://setup?url={quote(endpoint, safe='')}&token={quote(sms_token, safe='')}"
            body = (f'<p><a style="font-size:1.4em" href="{escape(link)}">SMS 릴레이 앱에 설정 적용</a></p>'
                    f"<p>endpoint: <code>{escape(endpoint)}</code></p>")
        return ("<!doctype html><meta charset=utf-8>"
                '<meta name=viewport content="width=device-width,initial-scale=1">'
                f"<title>SMS relay setup</title><body style='font-family:sans-serif;padding:1em'>"
                f"<h2>SMS 릴레이 설정</h2>{body}</body>")

    # -- SMS ingest (phone -> todo_system.sms) --------------------------------
    # Write-only and gated by its OWN secret (MACP_SMS_TOKEN): the phone never
    # holds the dashboard token, and the SMS token can't read the vault.
    SMS_MAX_BYTES = 256 * 1024

    def do_POST(self):
        if urlparse(self.path).path != "/api/sms":
            self._reply(404, b'{"error": "not found"}', "application/json")
            return
        sms_token = os.environ.get("MACP_SMS_TOKEN", "").strip()
        if not sms_token:
            self._reply(503, b'{"error": "sms ingest disabled (MACP_SMS_TOKEN unset)"}',
                        "application/json")
            return
        auth = self.headers.get("Authorization", "")
        got = auth[7:].strip() if auth.startswith("Bearer ") else ""
        if not got or not hmac.compare_digest(got, sms_token):
            over = self._note_auth_failure()
            _dispatch_ingest_alert("auth", 429 if over else 401)
            if over:
                self._reply(429, b"429 too many failed attempts; slow down\n",
                            "text/plain; charset=utf-8",
                            {"Retry-After": str(self.RL_WINDOW_S)})
            else:
                self._reply(401, b'{"error": "unauthorized"}', "application/json")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length <= 0 or length > self.SMS_MAX_BYTES:
            code = 413 if length > 0 else 400
            _dispatch_ingest_alert("too_large" if code == 413 else "bad_payload", code)
            self._reply(code, b'{"error": "bad body size"}', "application/json")
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            raw = data.get("messages", [data]) if isinstance(data, dict) else data
            if not isinstance(raw, list):
                raise ValueError("expected an object or a list")
            msgs = [sms.normalize(m) for m in raw]
        except (ValueError, UnicodeDecodeError) as e:
            _dispatch_ingest_alert("bad_payload", 400)  # str(e) stays in the reply only
            self._reply(400, json.dumps({"error": str(e)}).encode("utf-8"),
                        "application/json")
            return
        added = sms.append(msgs)
        logger.info("sms ingest received=%d stored=%d", len(msgs), added)
        self._reply(200, json.dumps({"received": len(msgs), "stored": added}).encode("utf-8"),
                    "application/json")


def serve(host: str, port: int, token: str | None,
          public_exposure: bool = False) -> ThreadingHTTPServer:
    """Bind the server (not yet serving). Refuses to bind without a token whenever
    the vault would be reachable off this host — either a non-loopback bind, or a
    loopback bind that sits behind a public tunnel (`public_exposure=True`, set by
    main() from MACP_PUBLIC_EXPOSURE). A tunnel such as Tailscale Funnel or
    cloudflared forwards to 127.0.0.1, so the non-loopback check alone can't see it
    — without this the vault would be published to the internet with no token gate."""
    if (not _is_loopback(host) or public_exposure) and not token:
        raise SystemExit(
            f"refusing to bind {host}:{port} without MACP_DASHBOARD_TOKEN "
            "(the dashboard exposes private vault contents; set the token before "
            "exposing it on a non-loopback address or behind a public tunnel with "
            "MACP_PUBLIC_EXPOSURE=1)")
    handler = type("BoundHandler", (Handler,), {
        "token": token or None,
        "_auth_failures": {},          # per-server: no leakage across servers/tests
        "_auth_lock": threading.Lock(),
    })
    return ThreadingHTTPServer((host, port), handler)


def main() -> int:
    # Record auth failures to the service journal (audit trail for exposed fronts).
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; anything but loopback needs MACP_DASHBOARD_TOKEN")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()
    os.chdir(VAULT)
    # A tunnel (Funnel/cloudflared) forwards to loopback, so a public front must
    # declare itself here — the loopback bind alone looks private to serve().
    public = os.environ.get("MACP_PUBLIC_EXPOSURE", "").strip() not in ("", "0")
    srv = serve(args.host, args.port,
                os.environ.get("MACP_DASHBOARD_TOKEN", "").strip(),
                public_exposure=public)
    url = f"http://{args.host}:{args.port}/"
    print(f"MaestroACP dashboard → {url}  (Ctrl-C to stop)")
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
