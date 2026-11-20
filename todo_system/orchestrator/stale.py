"""Stale human-owned tasks — surface work that nothing else is chasing.

The orchestrator surfaces its own parked assignments, but that only reaches
tasks which already have a ledger entry. A task a human owns and
never dispatched has no ledger entry at all, so nothing ever pings it: no
`due`, no `not_before`, no assignment, no nudge — it just ages out of sight.
This module finds exactly those and hands them to the dashboard renderer, which
is what the daily brief reads.

Notify-only by construction (acceptance criterion): the connection is opened
`mode=ro`, and this module issues nothing but SELECT. Nothing auto-fills `due`
— a missing deadline is usually deliberate, and inventing one would drown the
brief's real deadline warnings in noise.

Both knobs live here and nowhere else:

`STALE_DAYS = 7` — measured 2026-08-18 against `Todo/todo.db`: 29 open leaves,
21 of them undated, 9 of those human-owned and not deferred. Those 9
candidates' idle ages were `[2, 9, 12, 25, 25, 26, 26, 29, 68]` — nothing at
all lands in 3..8 days, so every threshold from 3 to 9 selects the same 8
tasks. 7 sits mid-plateau, so it can drift +-4 days without changing what gets
surfaced. It also matches the `review_cycle: weekly` cadence CLAUDE.md's task
template prescribes (7 of those 29 open leaves carry that value), and it
catches the 8-day-idle keystore task that motivated the sweep.

`MAX_SURFACED = 5` — noise is bounded by the cap, not by the threshold, which
is exactly why STALE_DAYS can afford to be generous. The truncated count is
always printed so a capped list can never be misread as a complete one.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from .. import not_before as nb_gate
from .. import ownership

STALE_DAYS = 7
MAX_SURFACED = 5

# The vault frontmatter convention writes "no value" as the literal "-"
# (CLAUDE.md), so a bare emptiness test would read "-" as a real date.
_ABSENT = ("", "-")

# Mirrors `cli.py`'s task ordering (`COALESCE(priority, 50) DESC`): an unset
# priority ranks mid-band, not last. Kept identical so this sweep orders tasks
# the way every other surface does.
_DEFAULT_PRIORITY = 50.0


def find_stale(db_path: str | Path,
               *,
               today: str | None = None,
               now: dt.datetime | None = None,
               stale_days: int = STALE_DAYS) -> list[dict]:
    """Open, human-owned leaves with no deadline that nobody has touched in
    `stale_days` days. Read-only. Most-important-first.

    `today` (ISO date) alone means 00:00 that day for the `not_before` clock;
    `now` pins it exactly. Neither = host-local now.

    Excluded on purpose:

    - anything with a `due` — the brief's deadline sections already carry it;
    - anything with a future `not_before` — deferring a task IS attending to
      it, and re-surfacing it would punish the user for planning ahead. Same
      semantics as the dispatch gates in `worker.py` / `tasks.py`, via the
      shared `not_before.is_deferred` (date-only = 00:00 that day, a time
      honored host-local);
    - worker-owned tasks, via `ownership.human_owned_sql_filter()` reused
      rather than reimplemented, so this agrees with NOW/FOCUS and the
      calendar. That is what drops the `blocked` cluster the pipeline is
      already chasing;
    - rows whose `updated` will not parse — with no computable age, calling
      them N days stale would be a guess.

    Returns dicts of `slug`, `title`, `priority`, `updated`, `age_days`.
    """
    if now is None:
        now = (dt.datetime.combine(dt.date.fromisoformat(today), dt.time.min)
               if today else dt.datetime.now())
    today = today or now.date().isoformat()
    today_date = dt.date.fromisoformat(today)

    human_sql, human_params = ownership.human_owned_sql_filter()
    uri = f"file:{Path(db_path)}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"""
            SELECT slug, title, priority, updated, not_before
            FROM tasks
            WHERE LOWER(TRIM(COALESCE(status, ''))) = 'open'
              AND COALESCE(is_parent, 0) = 0
              AND TRIM(COALESCE(due, '')) IN (?, ?)
              AND {human_sql}
            """,
            [*_ABSENT, *human_params],
        ).fetchall()
    finally:
        con.close()

    out: list[dict] = []
    for r in rows:
        if nb_gate.is_deferred(r["not_before"], now):
            continue
        try:
            updated = dt.date.fromisoformat(str(r["updated"] or "")[:10])
        except ValueError:
            continue
        age_days = (today_date - updated).days
        if age_days < stale_days:
            continue
        priority = _DEFAULT_PRIORITY if r["priority"] is None else float(r["priority"])
        out.append({
            "slug": r["slug"],
            "title": r["title"],
            "priority": priority,
            "updated": updated.isoformat(),
            "age_days": age_days,
        })

    # Priority first (the vault's own ranking), then longest-neglected, then
    # slug so a tie never reorders between two renders of the same data.
    out.sort(key=lambda t: (-t["priority"], -t["age_days"], t["slug"]))
    return out


def render_section(rows: list[dict], limit: int = MAX_SURFACED) -> list[str]:
    """Markdown bullets for the dashboard section.

    Always states the total when the list is capped: a truncated view that
    looks complete is worse than no view, because the reader stops looking.
    """
    if not rows:
        return ["- 없음"]
    shown = rows[:limit]
    lines = [
        f"- `{r['slug']}` — {r['title']} "
        f"(우선순위 {r['priority']:g} · {r['age_days']}일 방치, updated {r['updated']})"
        for r in shown
    ]
    hidden = len(rows) - len(shown)
    if hidden:
        lines.append(f"- … 외 {hidden}건 더 (총 {len(rows)}건)")
    return lines
