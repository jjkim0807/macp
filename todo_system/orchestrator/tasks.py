"""TaskGraph — load dispatchable tasks from the local todo SQLite.

This is the orchestrator's "what work exists" input. The planner (fixed this
session to read the local DB) fills todo.db; here we read open, non-parent,
worker_mode=auto leaves and turn them into orchestrator Tasks the Router can
hand to agents. Read-only against the DB. Stdlib-only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from pathlib import Path

from .. import not_before as nb_gate
from . import repos
from .orchestrator import Task

DEFAULT_DB = "Todo/todo.db"
DEFAULT_ITEMS = "Todo/items"


DISPATCHABLE_MODES = ("auto", "delegate", "quick", "discover")  # 5-stage / single-stage / fast / exploration
# 'quick' is a valid single-stage pipeline (runner._pipeline_for → STAGES_QUICK) for
# small, safe changes; it MUST be dispatchable or quick tasks orphan forever.

# Progress Log line mark_done leaves in the SAME write as `status: done`. Without
# it a reopen can restore the pre-close bytes exactly — cmd_reopen is mark_done's
# exact inverse, so a task with no boxes whose `updated:` is already today comes
# back byte-identical — and reconcile_closed would read the reopen as a lost close.
# Lands in git-tracked task markdown: treat it as wire format.
CLOSE_TRACE = "closed by orchestrator"


def _open_leaf_rows(db_path: str | Path) -> tuple[list, bool, bool]:
    """(rows, has_auto_merge, has_not_before) — every open, non-parent task row,
    read-only. Shared by load_dispatchable and the queue-view loaders."""
    uri = f"file:{Path(db_path)}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        # auto_merge / not_before are added by import-md's migration; tolerate a
        # DB that predates them (auto_merge defaults True, not_before ungated).
        present = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
        has_am, has_nb = "auto_merge" in present, "not_before" in present
        cols = "slug, title, path, outcome, next_action, tags, worker_mode"
        if has_am:
            cols += ", auto_merge"
        if has_nb:
            cols += ", not_before"
        # Carry `priority` into the row so load_dispatchable can put it on the
        # dispatch payload (the runner fills its parallel slots highest-first).
        # Read-only here — the ORDER BY below already sorts on it; a column-less
        # DB just omits it and the payload falls back to None (lowest).
        if "priority" in present:
            cols += ", priority"
        # Dispatch order = priority, highest first (NULL last), then insertion
        # order. Until 2026-10-26 there was no ORDER BY at all, so the runner's
        # few parallel slots went to whichever rows SQLite happened to return
        # first (≈ rowid = import order): a task marked priority 95 still queued
        # behind a dozen priority-40 tasks imported before it, and the
        # frontmatter `priority` field did nothing for dispatch. Every consumer
        # of this list (tick() and the queue views) walks it in order, so this
        # one ORDER BY is the whole fix. A DB that predates the column keeps
        # the old rowid order.
        order = ("ORDER BY (priority IS NULL), priority DESC, rowid"
                 if "priority" in present else "ORDER BY rowid")
        rows = con.execute(
            f"SELECT {cols} FROM tasks WHERE status='open' AND is_parent=0 {order}"
        ).fetchall()
    finally:
        con.close()
    return rows, has_am, has_nb


def load_dispatchable(db_path: str | Path = DEFAULT_DB,
                      only_auto: bool = True, *,
                      now: dt.datetime | None = None) -> list[Task]:
    """Open, non-parent leaves safe to auto-dispatch (worker_mode auto, delegate,
    or quick). Manual / blocked / human-owned tasks are left out — humans get
    their work via the brief and explicit dispatch. The StagedExecutor picks the
    5-stage vs single-stage pipeline from worker_mode in the payload. `now`
    pins the `not_before` clock (default: host-local now)."""
    rows, has_am, has_nb = _open_leaf_rows(db_path)

    tasks: list[Task] = []
    for r in rows:
        if only_auto and (r["worker_mode"] or "") not in DISPATCHABLE_MODES:
            continue
        # Parse the task markdown once; the drift guard and every md-sourced
        # payload field below read this dict (see the note above repos'
        # `task_*_from_fm` cores). {} when unreadable, so each field is absent.
        # fm_err is non-None ONLY when the frontmatter is strict-YAML broken.
        fm, fm_err = (repos.frontmatter_with_error(r["path"]) if r["path"]
                      else ({}, None))
        # Strict-YAML gate: a frontmatter that `yaml.safe_load` rejects yields an
        # empty dict here, which silently drops `repo`/`allowed_tools`/`touches`
        # and would dispatch an external-repo task as a vault task with
        # auto_merge in force (2026-10-19 macp-gen-units-split15-ops-paths). A
        # broken payload is unsafe in ANY mode, so this is NOT gated on only_auto
        # (unlike the drift guard below). Missing file / no frontmatter / unclosed
        # / non-dict YAML stay fail-open — fm_err is None for all of those.
        if fm_err is not None:
            print(f"[orchestrator] load_dispatchable: skipping {r['slug']} — "
                  f"frontmatter YAML error ({fm_err}); not dispatched")
            continue
        # md<->DB status drift: todo.db's `open` lags a markdown already closed
        # (daemon not yet re-synced, or mark_done's DB refresh failed) — the
        # 2026-10-30 app-plugins-11 case, closed by a /focus session. A closed
        # task is never dispatchable in any mode, so this is NOT gated on
        # only_auto. Fail-open: an absent/blank status trusts the DB.
        closed = repos.task_closed_from_fm(fm)
        if closed:
            print(f"[orchestrator] load_dispatchable: skipping {r['slug']} — "
                  f"md status={closed!r} (todo.db said open; md<->db drift)")
            continue
        # md<->DB drift guard: todo.db's worker_mode lags the markdown (the
        # todo-sync daemon rebuilds it), so a task hand-edited to a
        # non-dispatchable mode still shows a stale `auto` row until the daemon
        # catches up. Re-read the markdown (source of truth) and skip when it
        # *explicitly* names a non-dispatchable mode — closing the race that
        # dispatched a blocked task as an auto payload (2026-08-19
        # parent-match-app-16-google-signin). Fail-open: task_worker_mode_from_fm
        # returns None for a missing path/file/field or a parse failure, so an
        # unreadable md never blocks dispatch — only a stated mode does. Gated on
        # only_auto to stay consistent with the DB-mode gate above.
        if only_auto and r["path"]:
            md_mode = repos.task_worker_mode_from_fm(fm)
            if md_mode is not None and md_mode not in DISPATCHABLE_MODES:
                print(f"[orchestrator] load_dispatchable: skipping {r['slug']} — "
                      f"md worker_mode={md_mode!r} not dispatchable "
                      f"(todo.db said {r['worker_mode']!r}; md<->db drift)")
                continue
        # Date gate: `not_before` defers auto-dispatch until that moment (the
        # same field the NOW view honors). Lets a task be worker_mode:auto yet
        # not fire until a window closes — e.g. an experiment whose results can
        # only be recorded after its run ends. Date-only = 00:00 that day;
        # `YYYY-MM-DD HH:MM` / `THH:MM[:SS]` host-local; see not_before.py.
        if has_nb and nb_gate.is_deferred(r["not_before"], now):
            continue
        tags = [t for t in (r["tags"] or "").split(",") if t]
        repo_url, working_dir = repos.task_repo_from_fm(fm)
        payload = {
            "summary": r["title"],
            "outcome": r["outcome"] or "",
            "next_action": r["next_action"] or "",
            "tags": tags,
            "worker_mode": r["worker_mode"],
            "branch": f"worker/{r['slug']}",
            # The task file this dispatch came from. The legacy worker's queue
            # carried it; the v2 payload dropped it, which is why an in-flight
            # claim (orchestrator/claims.py) could only record `task_path: null`
            # and could not stand in for the report stage's
            # slug + task_path + nearest-earlier-timestamp archaeology.
            "task_path": r["path"] or "",
            # frontmatter auto_merge → runner honors it (0 = park PR for human
            # review). Missing column / NULL defaults to True. See runner.py.
            "auto_merge": (r["auto_merge"] != 0) if has_am else True,
            # Dispatch priority → the runner reorders received envelopes so a
            # late high-priority TASK_ASSIGN fills a free slot ahead of earlier
            # low/none ones (runner._by_priority). Raw DB value (int/float/None);
            # None = lowest. A column-less DB (r.keys() guard) or a NULL yields
            # None, never an IndexError. Always present — both payloads must show
            # `priority` — so it is a base key, not a conditional append.
            "priority": (r["priority"] if "priority" in r.keys() else None),
        }
        long_job = repos.task_long_job_from_fm(fm)
        if long_job:            # gated on a detached job; see orchestrator/jobs.py
            payload["long_job"] = long_job
        if repos.task_bypass_guard_from_fm(fm):
            payload["bypass_guard"] = True   # run despite a cost hold; see tick()
        tools = repos.task_allowed_tools_from_fm(fm)
        if tools:            # implement-stage whitelist; see stages._safe_tools
            payload["allowed_tools"] = tools
        # Difficulty signals for the model tier (model_select.classify_tier):
        # the explicit call, and the estimate the classifier falls back to.
        tier = repos.task_model_tier_from_fm(fm)
        if tier:
            payload["model_tier"] = tier
        # Vendor routing: `agent: upstage` sends a quick/delegate stage to the
        # Solar harness (runner._solar_account + stages.route_to_solar). A
        # frontmatter field is dead unless it reaches the payload (the
        # touches/allowed_tools precedent), so wire it — but ONLY when present,
        # so a task without `agent:` yields a byte-identical payload dict and
        # every existing claude task is untouched.
        agent = repos.task_agent_from_fm(fm)
        if agent:
            payload["agent"] = agent
        p90 = repos.task_estimate_p90_from_fm(fm)
        if p90:
            payload["estimate_p90"] = p90
        if repo_url:            # external-repo task; else a vault task
            payload["repo"] = repo_url
        if working_dir:
            payload["working_dir"] = working_dir
        # Same-repo concurrency inputs (orchestrator._can_run_concurrently reads
        # both off the payload). Absent when the markdown declares none, so the
        # predicate's "nothing declared → serialise" branch stays reachable.
        touches = repos.task_touches_from_fm(fm)
        if touches:
            payload["touches"] = touches
        depends_on = repos.task_depends_on_from_fm(fm)
        if depends_on:
            payload["depends_on"] = depends_on
        tasks.append(Task(slug=r["slug"], payload=payload))
    return tasks


# --- queue-view loaders (read-only; /api/status queue_upcoming / queue_blocked) --

def load_upcoming(db_path: str | Path = DEFAULT_DB, *,
                  now: dt.datetime | None = None) -> list[dict]:
    """Tasks load_dispatchable would return but for a FUTURE `not_before`.

    Same mode / strict-YAML / md-status / md-drift guards as load_dispatchable (silently —
    it already prints the skip lines), then `not_before` still deferred.
    `{"slug", "title", "path", "not_before"}`, earliest first."""
    rows, _, has_nb = _open_leaf_rows(db_path)
    if not has_nb:
        return []
    out: list[tuple] = []
    for r in rows:
        if (r["worker_mode"] or "") not in DISPATCHABLE_MODES:
            continue
        if not nb_gate.is_deferred(r["not_before"], now):
            continue
        fm, fm_err = (repos.frontmatter_with_error(r["path"]) if r["path"]
                      else ({}, None))
        if fm_err is not None or repos.task_closed_from_fm(fm):
            continue
        md_mode = repos.task_worker_mode_from_fm(fm) if r["path"] else None
        if md_mode is not None and md_mode not in DISPATCHABLE_MODES:
            continue
        when = nb_gate.parse_not_before(r["not_before"]) or dt.datetime.max
        out.append((when, {"slug": r["slug"], "title": r["title"],
                           "path": r["path"] or "",
                           "not_before": str(r["not_before"]).strip()}))
    out.sort(key=lambda x: x[0])
    return [row for _, row in out]


def _dep_slug(v: str) -> str:
    """`[[slug|alias]]` / `slug` -> `slug` (task_deps._clean's convention)."""
    s = str(v).strip().strip("\"'").strip().strip("[]").strip()
    return s.split("|", 1)[0].strip()


def load_blocked(db_path: str | Path = DEFAULT_DB) -> list[dict]:
    """Open, non-parent `worker_mode: blocked` tasks that declare `depends_on` —
    the ones task_deps auto-unblocks once every predecessor is done.

    `waiting_on` keeps only predecessors that are not done yet, with the same
    done test as task_deps.resolve_unblocks: the DB OR the predecessor's own
    markdown says `status: done` (a missing predecessor is not done).
    `{"slug", "title", "path", "waiting_on"}` in DB order."""
    rows, _, _ = _open_leaf_rows(db_path)
    con = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    try:
        status = {s: ((st or "").lower(), p or "")
                  for s, st, p in con.execute("SELECT slug, status, path FROM tasks")}
    finally:
        con.close()

    def done(dep: str) -> bool:
        st, path = status.get(dep, ("", ""))
        if st == "done":
            return True
        if not path:
            return False
        md = repos._frontmatter(path).get("status")
        return str(md or "").strip().lower() == "done"

    out: list[dict] = []
    for r in rows:
        if (r["worker_mode"] or "") != "blocked" or not r["path"]:
            continue
        fm, fm_err = repos.frontmatter_with_error(r["path"])
        if fm_err is not None:
            continue
        md_mode = repos.task_worker_mode_from_fm(fm)
        if md_mode is not None and md_mode != "blocked":
            continue                     # md<->db drift: md already flipped
        deps = [d for d in (_dep_slug(x) for x in repos.task_depends_on_from_fm(fm)) if d]
        if not deps:
            continue
        out.append({"slug": r["slug"], "title": r["title"], "path": r["path"],
                    "waiting_on": [d for d in deps if not done(d)]})
    return out


def task_meta(db_path: str | Path = DEFAULT_DB) -> dict[str, dict]:
    """slug -> {"priority": float|None, "updated": str} for the queue rows.
    Columns a DB predates read as None / ""."""
    con = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        present = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
        cols = ["slug"] + [c for c in ("priority", "updated") if c in present]
        rows = con.execute(f"SELECT {', '.join(cols)} FROM tasks").fetchall()
    finally:
        con.close()
    out: dict[str, dict] = {}
    for r in rows:
        keys = r.keys()
        out[r["slug"]] = {
            "priority": r["priority"] if "priority" in keys else None,
            "updated": str(r["updated"] or "") if "updated" in keys else "",
        }
    return out


def parents_needing_review(db_path: str | Path = DEFAULT_DB) -> list[dict]:
    """Parent tasks still `open` although every child has finished.

    `load_dispatchable` filters on `is_parent=0`, so a parent task is invisible
    to the workers — and nothing else was watching it either, so it just sat
    there. Observed on worldgraph-project: all 22 leaves done, the parent still
    `status: open` 18 days later, still advertising a `next_action` that had
    long since been carried out.

    Relationship source is the DB (`parent_slug` + `is_parent`), not a markdown
    scan: `parent:` wikilinks are parsed into `parent_slug` at import (cli.py)
    and carry an index, so this is the same data in its queryable form. Reading
    child status from that same snapshot is safe HERE because `mark_done` writes
    the markdown BEFORE refreshing the DB — the DB can only lag, never lead, so
    lag delays a ping by a tick rather than inventing one. (task_status() reads
    markdown instead because it gates re-dispatch, where that window matters.)

    The join is INNER on purpose: a parent with no children at all would
    otherwise satisfy "all children are closed" vacuously, which is exactly the
    shape of a freshly-created project. `is_parent=1` is required rather than
    inferred from "has children" — an ordinary leaf can be the `parent:` of a
    review task, and those must not be flagged.

    Read-only; returns `{"slug", "title", "n_children"}`. Surfacing only —
    nothing here closes anything.
    """
    uri = f"file:{Path(db_path)}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT p.slug AS slug, p.title AS title, COUNT(c.slug) AS n,
                   SUM(CASE WHEN LOWER(COALESCE(c.status, '')) IN
                       ('done', 'cancelled', 'canceled') THEN 1 ELSE 0 END) AS closed
            FROM tasks p JOIN tasks c ON c.parent_slug = p.slug
            WHERE COALESCE(p.is_parent, 0) = 1
              AND LOWER(COALESCE(p.status, '')) = 'open'
            GROUP BY p.slug, p.title
            HAVING n >= 1 AND closed = n
            """
        ).fetchall()
    finally:
        con.close()
    return [{"slug": r["slug"], "title": r["title"], "n_children": r["n"]} for r in rows]


def child_status_rollup(parent_slug: str, db_path: str | Path = DEFAULT_DB) -> dict:
    """Count a parent's children and how many have finished — WITHOUT requiring the
    parent itself to be `is_parent=1`.

    reconcile_split_parents closes a parent a human split BY HAND into children
    (each child `parent: "[[<parent>]]"`), and a hand-split parent never carries
    `is_parent: true` — so `parents_needing_review`, which filters on `is_parent=1`,
    cannot see it (that is cause ① the task fixes). This reads the same
    `parent_slug` relationship from the DB but DROPS that filter: the evidence that
    the parent is a closeable bundle lives in the ledger (an oversize re-scope park),
    not in the parent md.

    Reading child status from the DB is lag-safe for the same reason
    `parents_needing_review` documents: `mark_done` writes the child markdown and
    only THEN refreshes todo.db, so the DB can only lag a just-closed child, never
    lead — a stale row delays an auto-close by one tick, it never invents one.

    Returns `{"n": <#children>, "closed": <#done/cancelled>, "all_closed": bool}`.
    `all_closed` is True only when there is at least one child AND every child is
    done/cancelled: a childless parent is never vacuously "all closed" (that shape
    is a freshly-split parent whose children have not imported yet, or an ordinary
    leaf). Read-only; never the authority on closing by itself — the caller still
    gates on the ledger park signal.
    """
    uri = f"file:{Path(db_path)}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            """
            SELECT COUNT(c.slug) AS n,
                   SUM(CASE WHEN LOWER(COALESCE(c.status, '')) IN
                       ('done', 'cancelled', 'canceled') THEN 1 ELSE 0 END) AS closed
            FROM tasks c WHERE c.parent_slug = ?
            """,
            (parent_slug,),
        ).fetchone()
    finally:
        con.close()
    n = int(row["n"] or 0)
    closed = int(row["closed"] or 0)
    return {"n": n, "closed": closed, "all_closed": n >= 1 and closed == n}


def parents_with_open_children(db_path: str | Path = DEFAULT_DB) -> set[str]:
    """Slugs that are the `parent_slug` of at least one still-open child.

    A parent whose children are still being worked is an umbrella in progress, not
    a task waiting on a person — even when its own ledger row reads AWAITING_HUMAN.
    That row shape is normal when a run's only output was the split itself: the
    0-commit run ends `result-no-pr` ("merge withheld for human review") and the
    children carry the work (mulkko-95-focus-block-implementation, 2026-10-13).
    renudge_due and the dashboard use this to keep such a parent out of "사람 대기".

    Read-only; a missing or unreadable DB is an empty set (fail open to the old
    behavior — the parent shows as parked, which is noisy but never hides work)."""
    try:
        con = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    except sqlite3.Error:
        return set()
    try:
        rows = con.execute(
            """
            SELECT DISTINCT parent_slug FROM tasks
            WHERE COALESCE(parent_slug, '') != ''
              AND LOWER(COALESCE(status, '')) NOT IN ('done', 'cancelled', 'canceled')
            """
        ).fetchall()
    except sqlite3.Error:
        return set()
    finally:
        con.close()
    return {r[0] for r in rows}


def md_fingerprint(slug: str, items_dir: str | Path | None = None) -> str:
    """sha256 of the task markdown's raw bytes, or "" when it cannot be read.

    The close evidence reconcile_closed compares against (Assignment.
    close_fingerprint). Raw bytes, not parsed fields: a lost or rewound close
    leaves the file byte-identical to what it was, and anything else — a human's
    edit, `cli reopen` — changes it. Never raises: "" means "no evidence", and a
    caller must treat it as that, never as a match. Mirrors mark_done's
    cli.ITEMS_DIR default so both agree on where a task lives.

    The cli import sits inside the try too: a tick that lands mid-code-update
    can fail that import (2026-09-17 macp-kakao-alert-2, ImportError from
    kakao_format), and _retry's DONE path calls this unguarded.
    """
    try:
        from todo_system import cli

        path = Path(items_dir or cli.ITEMS_DIR) / f"{slug}.md"
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:  # noqa: BLE001 - evidence is best-effort, never a crash
        return ""


def _with_close_trace(text: str, today: str) -> str:
    """`text` with `- <today>: closed by orchestrator` appended under its
    `## Progress Log` (created when absent) — beta_publish.append_progress_log's
    shape, on a string, so it can ride mark_done's single write."""
    entry = f"- {today}: {CLOSE_TRACE}"
    text = text.rstrip("\n")
    if "## Progress Log" in text:
        return f"{text}\n{entry}\n"
    return f"{text}\n\n## Progress Log\n\n{entry}\n"


def mark_done(slug: str, items_dir: str | Path | None = None,
              db_path: str | Path | None = None) -> bool:
    """Close a todo task after a successful pipeline (policy A): set the markdown
    status=done + check its boxes, then refresh the local DB — matching the
    legacy worker's close, so the task is not re-dispatched. No-op if the task
    markdown is missing. Reuses todo_system.cli helpers.

    Defaults to cli.ITEMS_DIR / cli.DB_PATH (absolute). The old CWD-relative
    default silently no-opped — returning False as if the task did not exist —
    whenever the caller ran from anywhere but the vault root.

    An actual close (the md did not already say done) also appends CLOSE_TRACE to
    the Progress Log, in the same write as `status: done`. That trace is what lets
    reconcile_closed tell a human reopen from a lost close: a lost or rewound
    close leaves the pre-close bytes behind exactly, while a reopen — hand edit or
    `cli reopen` — keeps the trace and so never matches them. Re-closing an md
    that already says done adds nothing (a human closed it; the line would lie).

    Status, `updated` and the trace go down in ONE write, and only then are the
    boxes checked. With the boxes first, a failure between the two writes left
    "boxes checked, status open" — bytes matching no pre-close state, which
    reconcile_closed would read as a human edit and never heal. Now the only
    partial outcome is "done, boxes unchecked", which is harmless.
    """
    from todo_system import cli

    path = Path(items_dir or cli.ITEMS_DIR) / f"{slug}.md"
    if not path.exists():
        print(f"[orchestrator] mark_done: no task markdown at {path} — not closed")
        return False
    today = dt.date.today().isoformat()
    content = path.read_text(encoding="utf-8")
    fm, _ = cli.parse_frontmatter(content)
    already_done = str(fm.get("status") or "").strip().lower() == "done"
    content = cli.update_frontmatter_value(content, "updated", today)
    content = cli.update_frontmatter_value(content, "status", "done")
    if fm and not already_done:   # no frontmatter = no status written = nothing to trace
        content = _with_close_trace(content, today)
    path.write_text(content, encoding="utf-8")
    try:
        cli.update_markdown_checkboxes(path, mode="complete")
    except Exception as exc:
        # The close itself already landed above; unchecked boxes on a done task
        # are cosmetic. Loud anyway, like the DB refresh below.
        print(f"[orchestrator] mark_done: checkbox pass failed for {slug} — "
              f"status is done, boxes left as they were ({type(exc).__name__}: {exc})")
    # Refresh the local DB immediately (avoids a daemon-lag window before the
    # markdown->db sync). MUST commit: sqlite3 opens an implicit transaction, so
    # dropping the connection rolled the refresh back — the markdown said done
    # while todo.db still said open, which is the dispatch source, so the task
    # stayed dispatchable and got re-run on an empty diff. The leaked connection
    # also kept a write lock, making the NEXT writer fail with "database is
    # locked". Both sibling closers (worker.py review-autoclose, cli.py complete)
    # already commit; this one did not.
    try:
        con = cli.db_connect(db_path or cli.DB_PATH)
        try:
            cli.init_db(con)
            cli.refresh_single_task(con, path)
            con.commit()
        finally:
            con.close()
    except Exception as exc:
        # Loud, not silent: a failed refresh means the dispatch source still says
        # `open` and the task WILL be re-dispatched. The todo-sync daemon repairs
        # it eventually, but the operator needs to know it happened.
        print(f"[orchestrator] mark_done: DB refresh failed for {slug} — "
              f"todo.db may still say open ({type(exc).__name__}: {exc})")
    return True


def task_status(slug: str, items_dir: str | Path | None = None) -> str | None:
    """Current todo status of a task, read from its markdown frontmatter and
    lowercased ('open' | 'done' | 'cancelled' | …). None if the file is missing
    or has no status.

    Reads the MARKDOWN (not todo.db) on purpose: mark_done() writes the markdown
    FIRST and only then refreshes the DB, so a just-closed task shows here before
    todo.db catches up — exactly the window a re-dispatch check must see. Mirrors
    mark_done's cli.ITEMS_DIR default (absolute), so both agree on where a task
    lives regardless of the caller's cwd.
    """
    from todo_system import cli

    path = Path(items_dir or cli.ITEMS_DIR) / f"{slug}.md"
    if not path.exists():
        return None
    try:
        fm, _ = cli.parse_frontmatter(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    status = fm.get("status")
    if isinstance(status, str) and status.strip():
        return status.strip().lower()
    return None
