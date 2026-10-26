"""Detect a *quietly stalled* umbrella project and open ONE human decision task.

The orchestrator already chases individual leaves — it nudges parked
assignments, surfaces a parent whose children are all done, re-dispatches freed
work. What nothing watched was the project as a whole going quiet: every leaf
done (worldgraph-project sat `open` 18 days after its last of 61 leaves closed),
or the only leaves left are ones a worker can't touch and nobody said what the
human should do next. The pipeline keeps draining, a finished project keeps
advertising a stale `next_action`, and the one judgement that moves it forward —
"new direction? done? shelve it?" — never gets asked because no single leaf is
failing.

This module asks it. Deterministically (no LLM), the orchestrator tick classifies
each live umbrella project and, when one has gone stale by the rules below, writes
exactly one `Todo/items/<project>-next-direction.md` (`worker_mode: manual`, a
human decision task) and sends one deduped alert. Re-creation is suppressed while
a decision task is open, for 14 days after one closes (unless the project's shape
changes), and the whole sweep can be turned off with `MAESTRO_PROJECT_STALL=0`.

The design mirrors established engine precedent so it can't drift:

* ``discovery.py`` — deterministic ``render``/``create`` of a task, a gitignored
  ``Worker/_state/*.json`` state file, and an env off-switch.
* ``stale.py`` — read-only sqlite (``?mode=ro``), module-constant thresholds.
* ``reconcile_parents`` / ``_parent_seen_path`` — the dedup state-file idiom.
* ``_advote_bridge_tick`` / ``_beta_sweep_tick`` — fail-open, with a 1-hour
  min-poll throttle living *inside* the called function.

The classifier is split into a **pure function** (``classify_project``, which
touches no DB and is pinned by a 6-project regression fixture) and a **collector**
(``collect_project_views``) that re-derives the dashboard's umbrella/leaf rollup
from the same ``todo.db`` columns. Stdlib-only plus the sibling ``not_before``
gate (same import ``stale.py`` uses), so the engine-isolation gate stays green.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .. import not_before as nb_gate

# --- thresholds (the only knobs; see the AC) --------------------------------
STALL_IDLE_HOURS = 48       # (d) min idle since the last activity to call it stalled
RESUPPRESS_DAYS = 14        # don't re-open a decision task this soon after one closed …
RENOTIFY_OPEN_DAYS = 7      # … and re-ping once if a decision task sits open this long
SWEEP_MIN_INTERVAL_S = 3600  # 1h min-poll throttle for the whole sweep (run_once)

# Mirror of tasks.DISPATCHABLE_MODES (auto/delegate/quick/discover). Duplicated as a
# module constant rather than imported because tasks.py imports
# orchestrator.orchestrator and THIS module is imported BY orchestrator.orchestrator
# — importing tasks here would close that cycle. test_project_stall pins the two
# equal so the copy can never drift.
DISPATCHABLE_MODES = ("auto", "delegate", "quick", "discover")

# A leaf whose status is one of these is closed, so it is NOT an open leaf.
# `canceled` (one l) appears in the wild alongside `cancelled`; honour both, as
# tasks.child_status_rollup does.
CLOSED_STATUSES = frozenset({"done", "cancelled", "canceled"})

# Ledger states that mean a run is actively in flight (orchestrator._IN_FLIGHT).
IN_FLIGHT_STATES = frozenset({"dispatched", "accepted", "in_progress"})

# The vault frontmatter convention writes "no value" as "" or the literal "-".
_ABSENT = ("", "-")


# --- data model (module-internal; built by hand in Phase-1 tests, by the ----
#     collector in Phase 2) -------------------------------------------------

@dataclass(frozen=True)
class LeafView:
    """One leaf task in a project's subtree (a node with no children of its own)."""
    slug: str
    status: str = "open"
    worker_mode: str = ""
    not_before: str = ""
    next_action: str = ""
    if_then: str = ""
    in_flight: bool = False      # has an active ledger row (dispatched/accepted/in_progress)
    updated: str = ""            # frontmatter `updated:` (YYYY-MM-DD)
    title: str = ""


@dataclass
class ProjectView:
    """A live umbrella project plus everything the classifier needs. Every input
    the verdict depends on lives here, so ``classify_project`` stays a pure,
    fixture-pinned function of this one object."""
    slug: str
    title: str = ""
    status: str = "open"
    tags: tuple[str, ...] = ()
    outcome: str = ""
    next_action: str = ""
    if_then: str = ""
    not_before: str = ""
    priority: float | None = None
    leaves: tuple[LeafView, ...] = ()
    last_activity: dt.datetime | None = None
    run_in_flight: bool = False
    # (e) signals — tri-state. None = no record (can't judge → judge on a–d).
    planner_exhausted: bool | None = None   # global planner verdict == 'exhausted'
    discovery_pending: bool | None = None   # discovery is exploring THIS project now
    # exclusions
    decision_gate_off: bool = False
    open_decision_task: str | None = None   # slug of an already-open <project>-next-direction
    # re-suppression (filled from the state file by run_once; DB-blind here)
    prior_decision_closed_ts: dt.datetime | None = None
    prior_decision_fingerprint: str = ""
    state_fingerprint: str = ""             # current project shape (decision task excluded)


@dataclass(frozen=True)
class StallVerdict:
    stalled: bool
    reason: str
    done_leaves: list[LeafView] = field(default_factory=list)
    open_leaves: list[LeafView] = field(default_factory=list)


# --- helpers ----------------------------------------------------------------

def _norm(s: object) -> str:
    return str(s or "").strip().lower()


def _nonempty(s: object) -> bool:
    """True when a text field carries a real value (not "", "-", whitespace)."""
    return bool(s) and str(s).strip() not in _ABSENT


def _is_open_leaf(leaf: LeafView) -> bool:
    return _norm(leaf.status) not in CLOSED_STATUSES


def _leaf_auto_dispatchable(leaf: LeafView, now: dt.datetime) -> bool:
    """Would the worker pick this leaf up on its own? Mirrors load_dispatchable:
    status open, a dispatchable worker_mode, and not deferred by `not_before`."""
    if _norm(leaf.status) != "open":
        return False
    if _norm(leaf.worker_mode) not in DISPATCHABLE_MODES:
        return False
    if nb_gate.is_deferred(leaf.not_before, now):
        return False
    return True


def _human_action_defined(view: ProjectView, open_leaves: list[LeafView]) -> bool:
    """Is a *current* human action spelled out? The umbrella's own
    next_action/if_then, or any open leaf's — this is what keeps a project a
    person is actively hand-developing (research-staff-job-search) off the list."""
    if _nonempty(view.next_action) or _nonempty(view.if_then):
        return True
    return any(_nonempty(leaf.next_action) or _nonempty(leaf.if_then)
               for leaf in open_leaves)


def _idle_hours(now: dt.datetime, last: dt.datetime | None) -> float | None:
    if last is None:
        return None
    return (now - last).total_seconds() / 3600.0


def _resuppressed(view: ProjectView, now: dt.datetime) -> bool:
    """True while a just-closed decision task should keep this project quiet: it
    closed < RESUPPRESS_DAYS ago AND the project's shape (state_fingerprint) has
    not changed since. A new leaf or a status change flips the fingerprint and
    lifts the suppression — exactly the AC's release condition."""
    if view.prior_decision_closed_ts is None:
        return False
    if (now - view.prior_decision_closed_ts) >= dt.timedelta(days=RESUPPRESS_DAYS):
        return False
    return view.prior_decision_fingerprint == view.state_fingerprint


def _e_note(view: ProjectView) -> str:
    bits = []
    if view.planner_exhausted is True:
        bits.append("planner exhausted")
    if view.discovery_pending is False:
        bits.append("discovery found nothing new")
    return (" · " + ", ".join(bits)) if bits else ""


def _not_stalled(reason: str) -> StallVerdict:
    return StallVerdict(False, reason)


# --- the pure classifier ----------------------------------------------------

def classify_project(view: ProjectView, *, now: dt.datetime) -> StallVerdict:
    """Decide whether a project has quietly stalled. Pure: a function of `view`.

    Exclusions first (any one → not stalled), then the stall conjunction (all of
    a-or-b, c, d must hold; e is a non-blocking signal):

      exclude  project status != open; not_before future; decision_gate off;
               a decision task already open; re-suppressed (< 14d, shape unchanged)
      (a)/(b)  no open leaf, OR every open leaf is non-auto AND no human action
      (c)      no run in flight in the subtree
      (d)      last activity >= STALL_IDLE_HOURS ago
      (e)      positive evidence that planner/discovery just produced work for this
               project un-stalls it; absence/exhaustion does not save it.
    """
    status = _norm(view.status)
    if status != "open":
        return _not_stalled(
            f"project status is {status or 'unset'!r} (only open umbrellas are candidates)")
    if nb_gate.is_deferred(view.not_before, now):
        return _not_stalled("project not_before is still in the future")
    if view.decision_gate_off:
        return _not_stalled("decision_gate: off (opt-out)")
    if view.open_decision_task:
        return _not_stalled(f"a decision task is already open ({view.open_decision_task})")
    if _resuppressed(view, now):
        return _not_stalled(
            f"re-suppressed: a decision task closed < {RESUPPRESS_DAYS}d ago and the "
            "project shape is unchanged")

    leaves = list(view.leaves)
    open_leaves = [leaf for leaf in leaves if _is_open_leaf(leaf)]
    done_leaves = [leaf for leaf in leaves if _norm(leaf.status) == "done"]

    # (a) all leaves closed, or (b) open leaves all non-auto with no human action.
    if not open_leaves:
        if not leaves:
            return _not_stalled("project has no leaves yet (freshly created)")
        core_reason = f"all {len(leaves)} leaves are done/cancelled"
    else:
        if any(_leaf_auto_dispatchable(leaf, now) for leaf in open_leaves):
            return _not_stalled(
                "has an open, auto-dispatchable leaf — the worker still has work to do")
        if _human_action_defined(view, open_leaves):
            return _not_stalled(
                "open leaves are all human-owned and a current human action is defined")
        core_reason = (
            f"{len(open_leaves)} open leaf(s), all non-auto (manual/blocked/off/deferred), "
            "and no current human action is defined")

    # (c) a live run means the project is not stalled, it is being worked.
    if view.run_in_flight or any(leaf.in_flight for leaf in leaves):
        return _not_stalled("a run is in flight for this project")

    # (d) recent activity (leaf close / run / md edit) means not stalled yet.
    idle = _idle_hours(now, view.last_activity)
    if idle is not None and idle < STALL_IDLE_HOURS:
        return _not_stalled(f"last activity {idle:.0f}h ago (< {STALL_IDLE_HOURS}h)")

    # (e) non-blocking: only POSITIVE evidence that planner/discovery just made
    # work for this project saves it. Exhaustion / 0-candidates / no record do not
    # (they are the normal state of a project with nothing left to auto-extend).
    if view.discovery_pending is True:
        return _not_stalled("discovery is exploring this project (a fresh task source)")

    return StallVerdict(True, core_reason + _e_note(view), done_leaves, open_leaves)


# --- Phase 2: DB / ledger / state collector --------------------------------
#
# Re-derives the dashboard's umbrella/leaf rollup (scripts/macp_dashboard.py
# collect_projects) from the SAME todo.db columns, so the two definitions can't
# drift. The dashboard script can't be imported (it is a script, and UI is out
# of scope), hence the re-derivation here, column-for-column.

# An umbrella is a LIVE top-level parent, exactly as collect_projects defines it.
LIVE_PROJECT = frozenset({"open", "waiting", "blocked"})
# A run directory is `YYYYMMDDTHHMMSS-<slug>`.
_RUN_DIR_TS = "%Y%m%dT%H%M%S"


def decision_slug(project_slug: str) -> str:
    """The deterministic slug of a project's decision task."""
    return f"{project_slug}-next-direction"


def project_fingerprint(status: str, leaves, *, exclude_slug: str | None = None) -> str:
    """A stable hash of the project's *shape* — its status plus the (slug, status)
    of every leaf EXCEPT the decision task itself. Excluding the decision task is
    what lets the 14-day suppression survive that task's own open→closed flip
    while still lifting on a genuinely new leaf or a project status change."""
    import hashlib

    rows = sorted(
        (leaf.slug, _norm(leaf.status)) for leaf in leaves
        if exclude_slug is None or leaf.slug != exclude_slug)
    payload = _norm(status) + "|" + ";".join(f"{s}={st}" for s, st in rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _parse_db_date(value: object) -> dt.datetime | None:
    try:
        return dt.datetime.combine(dt.date.fromisoformat(str(value or "")[:10]), dt.time.min)
    except ValueError:
        return None


def _parse_iso_ts(value: object) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return t.astimezone().replace(tzinfo=None) if t.tzinfo else t


def _bump(store: dict[str, dt.datetime], slug: str, when: dt.datetime | None) -> None:
    if when and (slug not in store or when > store[slug]):
        store[slug] = when


def _run_dir_activity(runs_dir: Path | None) -> dict[str, dt.datetime]:
    out: dict[str, dt.datetime] = {}
    if runs_dir is None:
        return out
    try:
        names = os.listdir(runs_dir)
    except OSError:
        return out
    for name in names:
        ts, _, slug = name.partition("-")
        if not slug:
            continue
        try:
            _bump(out, slug, dt.datetime.strptime(ts, _RUN_DIR_TS))
        except ValueError:
            continue
    return out


def _ledger_signals(ledger) -> tuple[set[str], dict[str, dt.datetime]]:
    """(slugs with a run in flight, {slug: ledger updated_ts})."""
    in_flight: set[str] = set()
    updated: dict[str, dt.datetime] = {}
    if ledger is None:
        return in_flight, updated
    try:
        rows = ledger.all()
    except Exception:
        return in_flight, updated
    for a in rows:
        slug = getattr(a, "task_id", None)
        if not slug:
            continue
        if _norm(getattr(a, "state", "")) in IN_FLIGHT_STATES:
            in_flight.add(slug)
        _bump(updated, slug, _parse_iso_ts(getattr(a, "updated_ts", "")))
    return in_flight, updated


def _planner_exhausted(planner_state_path: Path | None) -> bool | None:
    """True/False from Worker/planner_state.json's verdict; None when there is no
    record to judge (the AC's 'cannot judge')."""
    if planner_state_path is None:
        return None
    try:
        data = json.loads(Path(planner_state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    verdict = _norm(data.get("verdict") if isinstance(data, dict) else "")
    if not verdict:
        return None
    return verdict == "exhausted"


def _discovery_exploring_slugs(items_dir: Path | None) -> set[str]:
    """Slugs of tasks whose repo currently has an OPEN discovery task — the one
    clean 'planner/discovery is actively producing work for this project' signal
    (discovery.active_repos). Best-effort: any failure → empty set → None signal."""
    if items_dir is None:
        return set()
    try:
        from . import discovery
        exploring: set[str] = set()
        for info in discovery.active_repos(items_dir).values():
            if info.get("open_discovery"):
                exploring.update(info.get("tasks") or [])
        return exploring
    except Exception:
        return set()


def _decision_gate_off(fm: dict) -> bool:
    """Read `decision_gate: off` opt-out. yaml.safe_load turns a bare `off`/`no`
    into the bool False, so honour both the bool and the string spellings."""
    value = fm.get("decision_gate")
    if isinstance(value, bool):
        return value is False
    return str(value or "").strip().lower() in ("off", "false", "no", "0")


def _open_decision_from_md(items_dir: Path, dslug: str) -> str | None:
    """Is the `<project>-next-direction.md` present AND still open? Read from the
    MARKDOWN, not todo.db: run_once writes the file this tick and the DB only
    catches up when the sync daemon next imports, so a DB read would both miss a
    just-created task (→ a false re-create attempt) and lag a human's close (→ a
    false 'still open'). The file is the source of truth either way."""
    try:
        from . import repos
        path = items_dir / f"{dslug}.md"
        if not path.exists():
            return None
        fm = repos._frontmatter(path) or {}
        return dslug if _norm(fm.get("status")) == "open" else None
    except Exception:
        return None


def collect_project_views(db_path: str | Path, *, ledger=None,
                          runs_dir: str | Path | None = None,
                          items_dir: str | Path | None = None,
                          planner_state_path: str | Path | None = None,
                          ) -> list[ProjectView]:
    """Build a ProjectView for every live umbrella project in `todo.db`.

    Read-only against the DB (`?mode=ro`, stale.py's shape). The umbrella set and
    the subtree leaf rollup mirror collect_projects exactly: a LIVE (open/waiting/
    blocked) task with no parent of its own that is a parent (`is_parent=1` or
    named as some task's `parent_slug`), rolled up over its cycle-guarded subtree
    with progress counted on leaves only. `last_activity` is the newest of each
    subtree slug's db `updated`, ledger `updated_ts`, and run-dir timestamp.
    """
    uri = f"file:{Path(db_path)}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        cols = {c[1] for c in con.execute("PRAGMA table_info(tasks)")}
        prio = "priority" if "priority" in cols else "NULL"
        rows = con.execute(
            f"SELECT slug, title, status, COALESCE(parent_slug,'') parent, {prio} prio, "
            "COALESCE(is_parent,0) is_parent, COALESCE(next_action,'') next_action, "
            "COALESCE(if_then,'') if_then, COALESCE(not_before,'') not_before, "
            "COALESCE(worker_mode,'') wm, COALESCE(tags,'') tags, "
            "COALESCE(outcome,'') outcome, COALESCE(updated,'') updated, "
            "COALESCE(path,'') path FROM tasks"
        ).fetchall()
    finally:
        con.close()

    tasks = {r["slug"]: dict(r) for r in rows}
    kids: dict[str, list[str]] = {}
    for slug, t in tasks.items():
        if t["parent"] and t["parent"] != slug:
            kids.setdefault(t["parent"], []).append(slug)

    in_flight, ledger_updated = _ledger_signals(ledger)
    run_activity = _run_dir_activity(Path(runs_dir) if runs_dir else None)
    items_dir = Path(items_dir) if items_dir else None
    exploring = _discovery_exploring_slugs(items_dir)
    planner_exhausted = _planner_exhausted(
        Path(planner_state_path) if planner_state_path else None)

    def _activity(slug: str, t: dict) -> dt.datetime | None:
        best = _parse_db_date(t.get("updated"))
        for store in (ledger_updated, run_activity):
            when = store.get(slug)
            if when and (best is None or when > best):
                best = when
        return best

    views: list[ProjectView] = []
    for slug, t in tasks.items():
        if (_norm(t["status"]) not in LIVE_PROJECT or t["parent"]
                or not (t["is_parent"] or kids.get(slug))):
            continue

        leaves: list[LeafView] = []
        subtree = {slug}
        latest = _activity(slug, t)
        seen, stack = {slug}, list(kids.get(slug, []))
        while stack:                                   # whole subtree, cycle-guarded
            c = stack.pop()
            if c in seen:
                continue
            seen.add(c)
            subtree.add(c)
            ct = tasks.get(c) or {}
            when = _activity(c, ct)
            if when and (latest is None or when > latest):
                latest = when
            if kids.get(c):
                stack.extend(kids[c])
                continue                               # intermediate parent, not a leaf
            leaves.append(LeafView(
                slug=c, status=ct.get("status", ""), worker_mode=ct.get("wm", ""),
                not_before=ct.get("not_before", ""), next_action=ct.get("next_action", ""),
                if_then=ct.get("if_then", ""), in_flight=(c in in_flight),
                updated=ct.get("updated", ""), title=ct.get("title", "")))

        dslug = decision_slug(slug)
        fm = {}
        if items_dir is not None:
            # md is authoritative for both the umbrella's decision_gate and the
            # decision task's open/closed state (todo.db lags a just-created or
            # just-closed decision task).
            try:
                from . import repos
                fm = repos._frontmatter(items_dir / f"{slug}.md") or {}
            except Exception:
                fm = {}
            open_decision = _open_decision_from_md(items_dir, dslug)
        else:
            open_decision = (dslug if (dslug in tasks
                                       and _norm(tasks[dslug]["status"]) == "open") else None)
        discovery_pending = True if (subtree & exploring) else None

        views.append(ProjectView(
            slug=slug,
            title=t.get("title") or slug,
            status=t.get("status", ""),
            tags=tuple(x for x in (t.get("tags") or "").split(",") if x),
            outcome=t.get("outcome", ""),
            next_action=t.get("next_action", ""),
            if_then=t.get("if_then", ""),
            not_before=t.get("not_before", ""),
            priority=t.get("prio"),
            leaves=tuple(leaves),
            last_activity=latest,
            run_in_flight=bool(subtree & in_flight),
            planner_exhausted=planner_exhausted,
            discovery_pending=discovery_pending,
            decision_gate_off=_decision_gate_off(fm),
            open_decision_task=open_decision,
            state_fingerprint=project_fingerprint(t.get("status", ""), leaves,
                                                   exclude_slug=dslug),
        ))
    return views


# --- Phase 3: deterministic decision-task render / create + state file ------

def enabled(env: dict | None = None) -> bool:
    """`MAESTRO_PROJECT_STALL=0` turns the whole sweep off (discovery.enabled shape)."""
    e = os.environ if env is None else env
    return str(e.get("MAESTRO_PROJECT_STALL", "1")).strip().lower() not in (
        "0", "false", "no", "off")


def _fmt_date(d: dt.date) -> str:
    return d.isoformat()


def _leaf_date(leaf: LeafView) -> str:
    return str(leaf.updated or "")[:10] or "-"


def _planner_discovery_summary(view: ProjectView) -> str:
    bits = []
    if view.planner_exhausted is True:
        bits.append("planner=exhausted (만들 leaf 없음)")
    elif view.planner_exhausted is False:
        bits.append("planner=active")
    else:
        bits.append("planner=기록 없음")
    if view.discovery_pending is True:
        bits.append("discovery=탐색 중")
    elif view.discovery_pending is False:
        bits.append("discovery=후보 0")
    else:
        bits.append("discovery=기록 없음")
    return " · ".join(bits)


def render_decision_task(view: ProjectView, verdict: StallVerdict,
                         today: dt.date) -> str:
    """The `<project>-next-direction.md` markdown — built deterministically (no
    LLM). Every field the validator reads is spelled the way it wants: a known
    `worker_mode: manual`, an integer `priority`, no inline comments on scalar
    fields, no `allowed_tools`, and NO "CI"/"GitHub Actions" token in the
    outcome/next_action/if_then (which `_check_actions_dependency` scans)."""
    slug = view.slug
    due = _fmt_date(today + dt.timedelta(days=3))
    updated = _fmt_date(today)

    extra_tags = [t for t in view.tags if t not in ("todo", "decision")]
    tag_lines = "\n".join(f"  - {t}" for t in (["todo", "decision"] + extra_tags))

    done = sorted((leaf for leaf in verdict.done_leaves),
                  key=lambda leaf: _leaf_date(leaf), reverse=True)[:10]
    done_lines = "\n".join(
        f"- {leaf.title or leaf.slug} ({_leaf_date(leaf)})" for leaf in done) or "- (없음)"
    open_lines = "\n".join(
        f"- {leaf.title or leaf.slug} · worker_mode={leaf.worker_mode or '-'} "
        f"· status={leaf.status or '-'}"
        for leaf in verdict.open_leaves) or "- (없음)"

    # The project title is NOT interpolated into any frontmatter field the
    # _check_actions_dependency matcher scans (outcome/next_action/if_then): a
    # title like "CI pipeline" would trip its `(?<![A-Za-z])CI(?![A-Za-z])` rule
    # even followed by Korean. The title lives in the body heading instead.
    outcome = ("이 프로젝트의 다음 방향을 사람이 결정한다 — "
               "아래 선택지 중 하나를 고르거나 새 지시를 남긴다")
    return f"""---
tags:
{tag_lines}
status: open
worker_mode: manual
parent: "[[{slug}]]"
decision_gate_for: {slug}
priority: 60
due: {due}
updated: {updated}
outcome: "{outcome}"
next_action: "아래 판정 사유와 남은 leaf를 읽고 선택지 ①~⑤ 중 하나를 고른다"
if_then: "이 태스크를 열면 → 선택지 ①~⑤ 중 하나를 골라 프로젝트를 진행·완료·보류·취소한다"
estimate_p50: 10m
estimate_p90: 20m
review_cycle: "-"
---

# 다음 방향 결정 — {view.title or slug}

> 오케스트레이터가 이 프로젝트를 **조용히 멈춘(stalled)** 상태로 판정해 자동 생성한
> 사람 결정용 태스크다 (LLM 호출 없이 결정적으로 생성). 아래 선택지 중 하나를 고르거나
> 새 방향을 남겨라. `worker_mode: manual` 이라 워커가 자동 실행하지 않는다.

## 판정 사유

- {verdict.reason}
- 마지막 활동: {view.last_activity.isoformat(timespec='minutes') if view.last_activity else '기록 없음'}
- planner/discovery: {_planner_discovery_summary(view)}

## 프로젝트 outcome

{view.outcome or '(outcome 미기재)'}

## 최근 완료된 leaf (최대 10개)

{done_lines}

## 남은 열린/블록 leaf

{open_lines}

## 선택지 (하나를 고르거나 새 방향을 지시)

- ① **새 방향 지시** — 다음 마일스톤/방향을 적는다. 답하면 사람이 `/todo`로 새 leaf를 만든다.
- ② **완료 처리** — 프로젝트가 끝났으면 이 우산 태스크의 `status: done`.
- ③ **보류** — 지금은 아니면 우산 태스크에 `not_before: YYYY-MM-DD`를 지정한다.
- ④ **discovery 1회 강제** — 리포를 다시 훑어 후보를 찾고 싶으면 discovery를 1회 돌린다.
- ⑤ **취소** — 접을 거면 `status: cancelled`.

## Progress Log

- {updated}: 오케스트레이터 stall 감지 → 결정 태스크 자동 생성 (todo_system.orchestrator.project_stall)
"""


def create_decision_task(view: ProjectView, verdict: StallVerdict, *,
                         today: dt.date, items_dir: str | Path) -> Path | None:
    """Write the decision task. None (no write) if an OPEN decision task already
    occupies the file — that is the dedup — or the write fails.

    A CLOSED decision file at the same path IS overwritten: run_once only reaches
    here once the 14-day re-suppression has lifted (a dismissed ask whose window
    expired, or a project whose shape changed), so a fresh ask for the new
    situation is intended and must not be blocked by the stale closed file."""
    d = Path(items_dir)
    dest = d / f"{decision_slug(view.slug)}.md"
    if dest.exists():
        try:
            from . import repos
            if _norm((repos._frontmatter(dest) or {}).get("status")) == "open":
                return None            # never clobber an open ask
        except Exception:
            return None                # can't tell -> stay safe, don't clobber
    try:
        d.mkdir(parents=True, exist_ok=True)
        dest.write_text(render_decision_task(view, verdict, today), encoding="utf-8")
    except OSError:
        return None
    return dest


# --- state file (gitignored, derived) — parent_reconcile.json dedup idiom ---

def state_path(vault_root: str | Path) -> Path:
    return Path(vault_root) / "Worker" / "_state" / "project_stall.json"


def load_state(path: str | Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"last_sweep_ts": None, "projects": {}}
    if not isinstance(data, dict):
        return {"last_sweep_ts": None, "projects": {}}
    data.setdefault("last_sweep_ts", None)
    data.setdefault("projects", {})
    return data


def save_state(state: dict, path: str | Path) -> bool:
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        return True
    except OSError:
        return False


def _sweep_due(state: dict, now: dt.datetime, *, min_interval_s: float = SWEEP_MIN_INTERVAL_S) -> bool:
    """True when the 1h min-poll throttle has elapsed (or there is no prior sweep).
    The throttle lives INSIDE run_once, exactly like _advote_bridge_tick."""
    last = _parse_iso_ts(state.get("last_sweep_ts"))
    if last is None:
        return True
    return (now - last).total_seconds() >= min_interval_s


def _apply_suppression(view: ProjectView, proj_state: dict) -> None:
    """Fold the state file's close record into the view so the PURE classifier can
    see the re-suppression window without reading the filesystem itself."""
    closed = _parse_iso_ts(proj_state.get("closed_ts"))
    if closed is not None:
        view.prior_decision_closed_ts = closed
        view.prior_decision_fingerprint = str(proj_state.get("fingerprint_at_close") or "")


def _should_renotify(proj_state: dict, now: dt.datetime,
                     *, open_days: float = RENOTIFY_OPEN_DAYS) -> bool:
    """A decision task left open this long gets ONE more ping, then never again."""
    if proj_state.get("renotified"):
        return False
    surfaced = _parse_iso_ts(proj_state.get("surfaced_ts"))
    if surfaced is None:
        return False
    return (now - surfaced) >= dt.timedelta(days=open_days)


# --- Phase 4: the sweep the orchestrator tick calls -------------------------

def _compose_alert(view: ProjectView, reason: str, *, reopen: bool = False) -> str:
    title = view.title or view.slug
    if reopen:
        head = (f"🧭 프로젝트 '{title}' 결정 태스크가 {RENOTIFY_OPEN_DAYS}일째 열려 있어요 "
                "— 다음 방향을 정해주세요")
    else:
        head = f"🧭 프로젝트 '{title}'가 조용히 멈춘 것 같아요 — 다음 방향을 정해주세요"
    lines = [head]
    if reason:
        lines.append(f"사유: {reason}")
    lines.append(f"→ Todo/items/{decision_slug(view.slug)}.md 에서 ①~⑤ 중 하나를 고르세요")
    return "\n".join(lines)


def run_once(vault_root: str | Path, *, now: dt.datetime | None = None,
             notify=None, env: dict | None = None) -> list[str]:
    """One stall sweep. Returns the slugs of decision tasks created this call.

    The 1-hour min-poll throttle, the dedup state file and the whole decision live
    INSIDE here (like _advote_bridge_tick / beta_publish.spawn_sweep_if_due), so the
    orchestrator's wrapper is a one-liner. Read-only against todo.db; the only
    writes are the decision task markdown (at most one per project) and the
    gitignored state file. `notify(text, *, event_key=...)` is the routed alert
    sink; absent in tests that assert purely on files.
    """
    if not enabled(env):
        return []
    now = now or dt.datetime.now()
    vault_root = Path(vault_root)
    sp = state_path(vault_root)
    state = load_state(sp)
    if not _sweep_due(state, now):
        return []
    state["last_sweep_ts"] = now.isoformat(timespec="seconds")
    projects = state.setdefault("projects", {})

    try:
        from .assignment import Ledger
        ledger = Ledger(vault_root / "Worker" / "assignments")
    except Exception:
        ledger = None

    try:
        views = collect_project_views(
            vault_root / "Todo" / "todo.db",
            ledger=ledger,
            runs_dir=vault_root / "Worker" / "runs",
            items_dir=vault_root / "Todo" / "items",
            planner_state_path=vault_root / "Worker" / "planner_state.json",
        )
    except Exception as e:                       # fail-open: a bad DB never stalls the tick
        print(f"[project_stall] collect skipped (fail-open): {e}")
        views = []

    created: list[str] = []
    for view in views:
        proj = projects.get(view.slug, {})

        # A decision task is still open: re-ping once at 7 days, never create a
        # second one.
        if view.open_decision_task:
            if notify is not None and _should_renotify(proj, now):
                try:
                    notify(_compose_alert(view, "", reopen=True),
                           event_key=f"project-stall:{view.slug}")
                except Exception:
                    pass
                proj["renotified"] = True
                projects[view.slug] = proj
            continue

        # No open decision task. If one was created earlier and has since closed,
        # stamp the close (starts the 14-day re-suppression at the current shape).
        if proj.get("decision_task") and not proj.get("closed_ts"):
            proj["closed_ts"] = now.isoformat(timespec="seconds")
            proj["fingerprint_at_close"] = view.state_fingerprint
            projects[view.slug] = proj

        _apply_suppression(view, proj)
        verdict = classify_project(view, now=now)
        if not verdict.stalled:
            continue

        dest = create_decision_task(view, verdict, today=now.date(),
                                    items_dir=vault_root / "Todo" / "items")
        if dest is None:
            continue
        created.append(dest.stem)
        projects[view.slug] = {
            "surfaced_ts": now.isoformat(timespec="seconds"),
            "decision_task": dest.stem,
            "renotified": False,
            "closed_ts": None,
            "fingerprint_at_close": "",
        }
        if notify is not None:
            try:
                notify(_compose_alert(view, verdict.reason),
                       event_key=f"project-stall:{view.slug}")
            except Exception:
                pass

    save_state(state, sp)
    return created
