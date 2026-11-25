from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .ownership import human_owned_sql_filter, is_worker_owned
from . import paths

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# Vault root for resolving relative source paths passed to the field extractor
# (see todo_system.paths). Same repo-root value as before when no env/.macp key.
ROOT = paths.vault_root()


def _event_timezone() -> str | None:
    """The IANA timezone calendar events are written in, or None.

    A thin wrapper over :func:`paths.timezone` (``MACP_TIMEZONE`` → the
    ``timezone`` key of ``.macp/config.toml`` → the host's own zone), read at
    CALL time because the sync daemon is long-lived and should pick up a config
    edit without a restart.

    ``None`` means "say nothing about the zone": the event body omits its
    ``timeZone`` key and Google applies the target calendar's own timezone,
    which is the correct reading of "host local" for a vault that has pinned
    nothing. Replaces four hard-coded ``"Asia/Seoul"`` literals — a vault that
    wants that zone writes ``timezone = "Asia/Seoul"`` in ``.macp/config.toml``.
    """
    return paths.timezone()


def _event_tzinfo() -> dt.tzinfo | None:
    """:func:`_event_timezone` as a ``tzinfo`` for local-clock arithmetic.

    Used for the "now" that anchors the API's ``timeMin``/``timeMax`` windows
    and the rendered date headings — not for event bodies, which carry the IANA
    *name*. Falls back to the host's own local zone (``.astimezone()``) when
    nothing is configured or the name has no tzdata entry; those call sites need
    an aware datetime and have no "say nothing" option. Was a hard-coded
    ``dt.timezone(dt.timedelta(hours=9))`` in five places.
    """
    name = _event_timezone()
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            # Unknown name / no tzdata installed: the host's zone beats raising
            # inside a daemon tick.
            pass
    return dt.datetime.now().astimezone().tzinfo


def _extract_field_from_markdown(source_path: str, field: str) -> str | None:
    """Extract a frontmatter field value from a markdown file."""
    path = Path(source_path)
    if not path.is_absolute():
        path = ROOT / source_path
    if not path.exists():
        return None

    content = path.read_text(encoding="utf-8")
    match = re.search(rf'^{field}:\s*["\']?(.+?)["\']?\s*$', content, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()


def _parse_scheduled(source_path: str) -> tuple[str | None, str | None, str | None]:
    """Parse scheduled field.

    Supported formats:
      - "HH:MM~HH:MM"              → (None, start_time, end_time)
      - "YYYY-MM-DD"                → (date, None, None)
      - "YYYY-MM-DD HH:MM~HH:MM"   → (date, start_time, end_time)

    Returns (scheduled_date, start_time, end_time).
    """
    return _parse_scheduled_value(_extract_field_from_markdown(source_path, "scheduled"))


def _parse_scheduled_value(val: str | None) -> tuple[str | None, str | None, str | None]:
    """:func:`_parse_scheduled` on a raw `scheduled:` value instead of a file."""
    if not val or val == "-":
        return None, None, None

    # "YYYY-MM-DD HH:MM~HH:MM"
    m = re.match(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})~(\d{1,2}:\d{2})", val)
    if m:
        return m.group(1), m.group(2), m.group(3)

    # "YYYY-MM-DD" (date only)
    m = re.match(r"(\d{4}-\d{2}-\d{2})$", val)
    if m:
        return m.group(1), None, None

    # "HH:MM~HH:MM" (time only, uses due date)
    m = re.match(r"(\d{1,2}:\d{2})~(\d{1,2}:\d{2})", val)
    if m:
        return None, m.group(1), m.group(2)

    return None, None, None


def _parent_slug(source_path: str) -> str | None:
    """Slug named by the `parent:` field (`[[slug]]`, `[[slug|title]]` or bare)."""
    raw = _extract_field_from_markdown(source_path, "parent")
    if not raw or raw == "-":
        return None
    m = re.match(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", raw.strip())
    slug = (m.group(1) if m else raw).strip()
    return slug.rsplit("/", 1)[-1] or None


def _parse_due_time(source_path: str) -> str | None:
    """Parse due_time field (deadline time, not scheduled work time)."""
    val = _extract_field_from_markdown(source_path, "due_time")
    if not val or val == "-":
        return None
    match = re.match(r"(\d{1,2}:\d{2})", val)
    if not match:
        return None
    return match.group(1)


def load_credentials(client_secret_path: Path, token_path: Path) -> Credentials:
    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if not client_secret_path.exists():
            raise FileNotFoundError(
                f"Google OAuth client secret not found: {client_secret_path}. "
                "Create an OAuth Desktop client in Google Cloud Console and save JSON there."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
        creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def _upsert_event(
    service,
    calendar_id: str,
    slug: str,
    title: str,
    due: str,
    scheduled_date: str | None,
    scheduled_start: str | None,
    scheduled_end: str | None,
    due_time: str | None,
    next_action: str,
    source_path: str,
    due_from_scheduled: bool = False,
) -> str:
    """Create, patch or pull one task's event; returns created / updated /
    pulled / unchanged.

    ``due_from_scheduled`` says ``due`` is not the md's own due date but its
    `scheduled:` date standing in for one (the task has no due) — so a pull
    that rewrites `scheduled:` also moves next tick's ``due``.
    """
    body: dict[str, object] = {
        "summary": title,
        "description": f"slug: {slug}\nnext_action: {next_action or '-'}\nsource: {source_path}",
        "extendedProperties": {
            "private": {
                "todo_slug": slug,
                "todo_source": source_path,
            }
        },
        "reminders": {"useDefault": False, "overrides": []},
    }
    body["start"], body["end"] = _event_window(
        due, scheduled_date, scheduled_start, scheduled_end, due_time)

    # Two-way sync (2026-10-27): the event remembers what macp last wrote
    # (`macp_start`/`macp_end`, canonical form). A start/end that differs from
    # that baseline was edited in the calendar app; the old code deleted and
    # re-inserted every event on every md change, so such edits snapped back
    # within minutes. Now: calendar edited, md not → pull into the md's
    # `scheduled:`; md edited, calendar not → patch the event; both → the
    # later edit wins (event `updated` vs md mtime); neither → no API write.
    #
    # Two baselines, because `scheduled:` cannot hold every calendar shape: a
    # multi-day all-day event or a midnight-spanning timed one is pulled into
    # the md lossily (its first day). `macp_start`/`macp_end` stay the CALENDAR
    # side (the event's real start/end), `macp_md_start`/`macp_md_end` the MD
    # side (what the md as last written produces). Comparing the md against the
    # calendar-side value read the lossy pull as an md edit and patched the
    # event back to one day on the next tick. A later hand edit of `scheduled:`
    # still wins and collapses the event to what the md can express.
    desired = (_canon(body["start"]), _canon(body["end"]))
    found = (
        service.events()
        .list(
            calendarId=calendar_id,
            privateExtendedProperty=f"todo_slug={slug}",
            singleEvents=True,
            maxResults=50,
        )
        .execute()
    )
    items = found.get("items", [])
    # Duplicates (race conditions) — keep the first, drop the rest.
    for item in items[1:]:
        try:
            service.events().delete(calendarId=calendar_id, eventId=item["id"]).execute()
        except Exception:
            # Event may already be deleted (HTTP 410) or otherwise unavailable
            pass

    def _baselines(cal: tuple, md: tuple) -> None:
        # Every key on every write: Google's patch merges map keys, the test
        # fake replaces the map — either way no stale baseline survives.
        body["extendedProperties"]["private"].update(
            {"macp_start": cal[0], "macp_end": cal[1],
             "macp_md_start": md[0], "macp_md_end": md[1]})

    if not items:
        _baselines(desired, desired)
        service.events().insert(calendarId=calendar_id, body=body).execute()
        return "created"

    ev = items[0]
    priv = (ev.get("extendedProperties") or {}).get("private") or {}
    actual = (_canon(ev.get("start") or {}), _canon(ev.get("end") or {}))
    base = (priv.get("macp_start"), priv.get("macp_end"))
    has_base = all(base)
    md_base = (priv.get("macp_md_start"), priv.get("macp_md_end"))
    if not all(md_base):
        md_base = base                      # pre-split event: single-baseline rule
    cal_changed = has_base and actual != base
    md_changed = (not has_base) or desired != md_base   # legacy event: md wins once

    if cal_changed and (not md_changed or _event_newer_than_md(ev, source_path)):
        sched = _scheduled_value(ev.get("start") or {}, ev.get("end") or {})
        md_proj = desired
        if sched:
            _write_scheduled(source_path, sched)
            # What next tick's `desired` will be, from the md just written.
            s_date, s_start, s_end = _parse_scheduled_value(sched)
            proj_due = s_date if (due_from_scheduled and s_date) else due
            md_proj = tuple(_canon(w) for w in _event_window(
                proj_due, s_date, s_start, s_end, due_time))
        body["start"], body["end"] = ev["start"], ev["end"]
        _baselines(actual, md_proj)
        service.events().patch(calendarId=calendar_id, eventId=ev["id"], body=body).execute()
        return "pulled"

    if not cal_changed and not md_changed:
        if (ev.get("summary") == body["summary"]
                and ev.get("description") == body["description"]):
            return "unchanged"
        # Text-only change (title, next_action, source): keep the event's own
        # start/end — re-sending `desired` would revert a pulled multi-day or
        # overnight shape the md cannot express.
        body["start"], body["end"] = ev["start"], ev["end"]
        _baselines(base, md_base)
        service.events().patch(calendarId=calendar_id, eventId=ev["id"], body=body).execute()
        return "updated"

    _baselines(desired, desired)
    body["start"], body["end"] = _clear_other_shape(body["start"]), _clear_other_shape(body["end"])
    service.events().patch(calendarId=calendar_id, eventId=ev["id"], body=body).execute()
    return "updated"


def _clear_other_shape(when: dict) -> dict:
    """``when`` plus an explicit null for the key of the OTHER event shape.

    Google's patch merges the start/end maps key by key, so moving an all-day
    event (`date`) to a time block (`dateTime`), or back, left both keys on the
    event and the API refused it with 400 "Invalid start time" on every tick
    (2026-11-03: a task whose `scheduled:` went from a date to "… 14:00~16:00").
    A null deletes the stale key; it is a no-op when the shape did not change.
    """
    if "dateTime" in when:
        return {**when, "date": None}
    return {**when, "dateTime": None, "timeZone": None}


def _event_window(
    due: str,
    scheduled_date: str | None,
    scheduled_start: str | None,
    scheduled_end: str | None,
    due_time: str | None,
) -> tuple[dict, dict]:
    """The event (start, end) the md's due/scheduled/due_time fields produce."""
    due_date = dt.date.fromisoformat(due)
    event_date = dt.date.fromisoformat(scheduled_date) if scheduled_date else due_date

    # One lookup per event, applied to both ends. An absent `tz` leaves the key
    # OUT of the body rather than writing a guess; with a `tz` the body is
    # byte-identical to the previous hard-coded form, key order included.
    tz = _event_timezone()

    def _timed(when: dt.datetime) -> dict[str, str]:
        d = {"dateTime": when.isoformat()}
        if tz:
            d["timeZone"] = tz
        return d

    if scheduled_start and scheduled_end:
        # Use scheduled work time for calendar event
        start_dt = dt.datetime.fromisoformat(f"{event_date.isoformat()}T{scheduled_start}:00")
        end_dt = dt.datetime.fromisoformat(f"{event_date.isoformat()}T{scheduled_end}:00")
        return _timed(start_dt), _timed(end_dt)
    if due_time:
        # Fallback to due_time (deadline) — show as 1h event
        start_dt = dt.datetime.fromisoformat(f"{due_date.isoformat()}T{due_time}:00")
        end_dt = start_dt + dt.timedelta(hours=1)
        return _timed(start_dt), _timed(end_dt)
    end_date = event_date + dt.timedelta(days=1)
    return {"date": event_date.isoformat()}, {"date": end_date.isoformat()}


def _canon(when: dict) -> str | None:
    """A start/end dict as a comparable string: `D:YYYY-MM-DD` for all-day,
    `T:<UTC ISO>` for timed (offsets and a bare `timeZone` resolve to the same
    instant, so a round-trip through the API never reads as an edit)."""
    if not when:
        return None
    if when.get("date"):
        return f"D:{when['date']}"
    raw = when.get("dateTime")
    if not raw:
        return None
    t = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if t.tzinfo is None:
        tz = None
        if when.get("timeZone"):
            try:
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(when["timeZone"])
            except Exception:
                tz = None
        t = t.replace(tzinfo=tz or _event_tzinfo() or dt.timezone.utc)
    return "T:" + t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M")


def _scheduled_value(start: dict, end: dict) -> str | None:
    """The md `scheduled:` value for a calendar start/end (local clock)."""
    if start.get("date"):
        return start["date"]
    if not start.get("dateTime") or not end.get("dateTime"):
        return None
    tz = _event_tzinfo()
    s = dt.datetime.fromisoformat(str(start["dateTime"]).replace("Z", "+00:00"))
    e = dt.datetime.fromisoformat(str(end["dateTime"]).replace("Z", "+00:00"))
    if tz is not None:
        s, e = (s.astimezone(tz) if s.tzinfo else s), (e.astimezone(tz) if e.tzinfo else e)
    if e.date() != s.date():
        return s.date().isoformat()          # spans midnight: keep the day, drop the times
    return f"{s.date().isoformat()} {s:%H:%M}~{e:%H:%M}"


def _md_path(source_path: str) -> Path:
    p = Path(source_path)
    return p if p.is_absolute() else ROOT / source_path


def _event_newer_than_md(ev: dict, source_path: str) -> bool:
    """Both sides changed since the last sync: the later edit wins."""
    try:
        upd = dt.datetime.fromisoformat(str(ev.get("updated", "")).replace("Z", "+00:00"))
        mtime = dt.datetime.fromtimestamp(_md_path(source_path).stat().st_mtime, dt.timezone.utc)
        return upd > mtime
    except Exception:
        return False


def _write_scheduled(source_path: str, value: str, note: str | None = None) -> None:
    """Set `scheduled:` in the task md (the source of truth) and log it. The
    todo-sync daemon then re-imports; the next sync finds md == calendar ==
    baseline and writes nothing, so there is no ping-pong."""
    from .cli import update_frontmatter_value
    path = _md_path(source_path)
    text = path.read_text(encoding="utf-8")
    today = dt.date.today().isoformat()
    text = update_frontmatter_value(text, "scheduled", f'"{value}"')
    text = update_frontmatter_value(text, "updated", today)
    if "## Progress Log" in text:
        line = f"- {today}: " + (
            note or f"캘린더에서 일정을 옮겨 scheduled를 {value}로 맞춤") + " (calendar sync)"
        # Inside the section: after its last entry, before the next `## `
        # heading (`### ` sub-headings stay inside); EOF when it is the last.
        head = re.search(r"^## Progress Log[ \t]*$", text, re.M)
        nxt = re.compile(r"^## ", re.M).search(text, head.end()) if head else None
        if nxt:
            text = text[:nxt.start()].rstrip("\n") + f"\n{line}\n\n" + text[nxt.start():]
        else:
            text = text.rstrip("\n") + f"\n{line}\n"
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Overdue time blocks → next free slot (2026-11-16)
# ---------------------------------------------------------------------------
# A human task whose timed block ended while it was still open used to stay on
# a past slot, so it vanished from the forward calendar. After each sync the
# block moves to the earliest free working slot of the same length; the move is
# written to the md and pushed on the next sync. Runs AFTER the upserts so a
# block the person dragged in the calendar is pulled into the md first and is
# never overridden. Opt out per task with `reschedule: false`.

RESCHEDULE_GRACE = dt.timedelta(minutes=30)   # a block ending this long ago is overdue
RESCHEDULE_HORIZON_DAYS = 21
_SLOT_STEP = dt.timedelta(minutes=15)


def _block_bounds(sched_date: str, start: str, end: str) -> tuple[dt.datetime, dt.datetime]:
    s = dt.datetime.fromisoformat(f"{sched_date}T{int(start.split(':')[0]):02d}:{start.split(':')[1]}")
    e = dt.datetime.fromisoformat(f"{sched_date}T{int(end.split(':')[0]):02d}:{end.split(':')[1]}")
    if e <= s:          # a midnight-spanning block
        e += dt.timedelta(days=1)
    return s, e


def _ceil_to_step(t: dt.datetime) -> dt.datetime:
    t = t.replace(second=0, microsecond=0)
    extra = t.minute % 15
    return t if extra == 0 else t + dt.timedelta(minutes=15 - extra)


def find_free_slot(duration: dt.timedelta, busy: list[tuple[dt.datetime, dt.datetime]],
                   earliest: dt.datetime, *, hours: tuple[int, int] = (10, 18),
                   weekdays: set[int] | None = None,
                   horizon_days: int = RESCHEDULE_HORIZON_DAYS) -> tuple[dt.datetime, dt.datetime] | None:
    """Earliest [start, start+duration) inside the working window that overlaps no
    busy interval. Naive local datetimes. Pure — unit-tested."""
    weekdays = set(range(5)) if weekdays is None else weekdays
    start = _ceil_to_step(earliest)
    last_day = start.date() + dt.timedelta(days=horizon_days)
    day = start.date()
    while day <= last_day:
        if day.weekday() in weekdays:
            open_ = dt.datetime.combine(day, dt.time(hours[0]))
            close = dt.datetime.combine(day, dt.time(0)) + dt.timedelta(hours=hours[1])
            t = max(open_, start)
            while t + duration <= close:
                end = t + duration
                clash = [b for b in busy if b[0] < end and t < b[1]]
                if not clash:
                    return t, end
                t = _ceil_to_step(max(b[1] for b in clash))
        day += dt.timedelta(days=1)
    return None


def _fmt_block(s: dt.datetime, e: dt.datetime) -> str:
    return f"{s.date().isoformat()} {s:%H:%M}~{e:%H:%M}"


def _not_before(source_path: str) -> dt.datetime | None:
    raw = (_extract_field_from_markdown(source_path, "not_before") or "").strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}):(\d{2}))?", raw)
    if not m:
        return None
    d = dt.date.fromisoformat(m.group(1))
    return dt.datetime(d.year, d.month, d.day, int(m.group(2) or 0), int(m.group(3) or 0))


def _opted_out(source_path: str) -> bool:
    raw = (_extract_field_from_markdown(source_path, "reschedule") or "").strip().lower()
    return raw in ("false", "no", "off", "0")


def _is_decision_task(source_path: str) -> bool:
    """True when the task md carries a decision marker: the `decision` tag or
    `needs_decision: true` (:func:`task_deps._has_decision_marker`, the one
    definition of the marker).

    A decision is answered on the dashboard the moment it is asked (a card with
    buttons — the "Decision cards" contract in orchestrator/triage.py), so it
    gets no calendar block: the sync leaves it out, the stale cleanup deletes
    the event it already has, and the overdue mover never rewrites its
    `scheduled:`. Read from the md, not the DB row — `needs_decision` is not a
    DB column, and an answer takes effect on the next sync, not the next import.

    `needs_decision` counts only as a plain true / yes / 1 / on, the reading
    the dashboard uses for its decision rows, so a task never loses its block
    here without also showing up there as a decision. Anything else (a typo, a
    value with an inline comment) keeps the block.

    Imports are local: ``task_deps`` imports ``cli``, which imports this module.
    Any read or parse error answers False — the old behaviour (synced like any
    other task) beats a dead sync tick.
    """
    try:
        from . import task_deps
        from .cli import parse_frontmatter
        fm, _ = parse_frontmatter(_md_path(source_path).read_text(encoding="utf-8"))
        raw = str(fm.get("needs_decision") or "").strip().strip("'\"").lower()
        return task_deps._has_decision_marker({
            "tags": [t.strip().lower() for t in task_deps._as_list(fm.get("tags")) if t.strip()],
            "needs_decision": raw in ("true", "yes", "1", "on"),
        })
    except Exception:  # noqa: BLE001 - fail-open by contract
        return False


def plan_overdue_moves(blocks: list[dict], busy: list[tuple[dt.datetime, dt.datetime]],
                       now: dt.datetime, *, hours: tuple[int, int] = (10, 18),
                       weekdays: set[int] | None = None) -> list[tuple[str, str, str]]:
    """Pure planner. `blocks`: dicts with slug, start, end (naive local), and
    optional not_before / priority / opt_out. Returns (slug, old, new) for every
    overdue block that found a slot; each placed block becomes busy for the next.
    Future blocks of the other tasks must already be in `busy`."""
    busy = list(busy)
    overdue = [b for b in blocks
               if not b.get("opt_out") and b["end"] + RESCHEDULE_GRACE <= now]
    overdue.sort(key=lambda b: (-int(b.get("priority") or 50), b["start"]))
    moves: list[tuple[str, str, str]] = []
    for b in overdue:
        earliest = max(now, b.get("not_before") or now)
        slot = find_free_slot(b["end"] - b["start"], busy, earliest,
                              hours=hours, weekdays=weekdays)
        if slot is None:
            continue
        busy.append(slot)
        moves.append((b["slug"], _fmt_block(b["start"], b["end"]), _fmt_block(*slot)))
    return moves


def _freebusy(service, calendars: Sequence[str], start: dt.datetime,
              end: dt.datetime) -> list[tuple[dt.datetime, dt.datetime]]:
    """Busy intervals (naive local) from Google's freebusy for `calendars`.
    Any failure answers [] — the task blocks alone still keep slots apart."""
    tzinfo = _event_tzinfo()
    try:
        body = {"timeMin": start.replace(tzinfo=tzinfo).isoformat(),
                "timeMax": end.replace(tzinfo=tzinfo).isoformat(),
                "items": [{"id": c} for c in calendars]}
        tz = _event_timezone()
        if tz:
            body["timeZone"] = tz
        res = service.freebusy().query(body=body).execute()
        out = []
        for cal in (res.get("calendars") or {}).values():
            for b in cal.get("busy") or []:
                s = dt.datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
                e = dt.datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
                if tzinfo is not None:
                    s, e = s.astimezone(tzinfo), e.astimezone(tzinfo)
                out.append((s.replace(tzinfo=None), e.replace(tzinfo=None)))
        return out
    except Exception:
        return []


def reschedule_overdue_blocks(service, rows, calendars: Sequence[str],
                              now: dt.datetime | None = None) -> list[tuple[str, str, str]]:
    """Move open human tasks' overdue timed blocks to the next free slot and
    write them to the md. Returns the moves. Never raises.

    A decision task (:func:`_is_decision_task`) is skipped whole: its block is
    neither moved nor counted as busy, because it is not on the calendar."""
    try:
        tzinfo = _event_tzinfo()
        now = now or dt.datetime.now(tzinfo).replace(tzinfo=None)
        blocks, busy, paths_by_slug = [], [], {}
        for r in rows:
            src = str(r["path"])
            d, s, e = _parse_scheduled(src)
            if not (d and s and e):
                continue
            if _is_decision_task(src):       # after the block test: one md parse, only when needed
                continue
            start, end = _block_bounds(d, s, e)
            if end > now:
                busy.append((start, end))
                continue
            paths_by_slug[str(r["slug"])] = src
            pr = None
            try:
                pr = int(_extract_field_from_markdown(src, "priority") or 50)
            except ValueError:
                pr = 50
            blocks.append({"slug": str(r["slug"]), "start": start, "end": end,
                           "not_before": _not_before(src), "priority": pr,
                           "opt_out": _opted_out(src)})
        if not any(b["end"] + RESCHEDULE_GRACE <= now and not b["opt_out"] for b in blocks):
            return []
        busy += _freebusy(service, calendars, now,
                          now + dt.timedelta(days=RESCHEDULE_HORIZON_DAYS + 1))
        moves = plan_overdue_moves(blocks, busy, now)
        for slug, old, new in moves:
            _write_scheduled(paths_by_slug[slug], new,
                             note=f"지난 블록 {old}이 끝났는데 열려 있어 {new}로 자동 재배치")
            print(f"calendar: rescheduled overdue block {slug}: {old} -> {new}")
        return moves
    except Exception as exc:  # noqa: BLE001 - never break the sync
        print(f"calendar: overdue reschedule skipped — {exc}")
        return []


# ---------------------------------------------------------------------------
# Done human blocks → trimmed to the real completion time (2026-11-19)
# ---------------------------------------------------------------------------
# A human task with a timed block used to lose its event the moment its md
# went `status: done` (the stale-event cleanup deleted it), so the calendar
# kept no record of when the work actually happened. Now the todo import
# stamps `completed: "YYYY-MM-DD HH:MM"` on the open→done transition
# (:func:`record_completion_transition`) and the sync re-shapes the event to
# end at that time, titles it `✅ …`, and swaps its private `todo_slug` for
# `todo_done_slug` — so the cleanup, the calendar→md pull and the overdue mover
# all leave it alone. Done events are deleted `DONE_KEEP_DAYS` (30) after
# completion.
#
# A done task that is reopened keeps its ✅ event as history until it ages
# out; the reopened task simply gets a fresh `todo_slug` event beside it.

DONE_MIN = dt.timedelta(minutes=15)
DONE_KEEP_DAYS = 30
_DONE_MARK = "✅"


def _now_local() -> dt.datetime:
    """Local wall-clock now (naive, minute precision) for the done-block code.
    One seam so tests can pin it; older code keeps its own clocks."""
    return dt.datetime.now(_event_tzinfo()).replace(tzinfo=None, second=0, microsecond=0)


def _parse_completed(raw: str | None) -> dt.datetime | None:
    """A `completed:` value (`YYYY-MM-DD HH:MM`, `T` or 1-digit hour ok) as a
    naive local datetime, or None for empty / `-` / junk."""
    if not raw:
        return None
    m = re.match(r"\s*['\"]?(\d{4}-\d{2}-\d{2})[ T](\d{1,2}):(\d{2})", str(raw))
    if not m:
        return None
    try:
        d = dt.date.fromisoformat(m.group(1))
        return dt.datetime(d.year, d.month, d.day, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _fmt_completed(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M")


def done_block_window(start: dt.datetime, end: dt.datetime,
                      completed: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """The (start, end) a done block's event takes, on naive local datetimes.

    Rules (``c`` = completion, ``[s, e]`` = the planned block):

    1. ``s <= c <= e`` (finished inside the block): ``[s, max(c, s + 15m)]``.
    2. ``c > e`` on the block's start day (ran over): ``[s, c]``.
    3. Otherwise (before the block, or another day): the block keeps its
       length (at least 15m) and moves to end at ``c``.

    Every rule keeps at least 15 minutes. Pure — unit-tested.
    """
    if start <= completed <= end:
        return start, max(completed, start + DONE_MIN)
    if completed > end and completed.date() == start.date():
        return start, completed
    length = max(end - start, DONE_MIN)
    return completed - length, completed


def _done_title(title: str) -> str:
    """`✅ <title>` with exactly one mark, whatever the title already had."""
    return f"{_DONE_MARK} " + re.sub(rf"^(?:\s|{_DONE_MARK})+", "", title or "")


def _timed_when(t: dt.datetime) -> dict[str, str]:
    """A naive local datetime as an event start/end (same shape as
    :func:`_event_window`'s timed form)."""
    d = {"dateTime": t.isoformat()}
    tz = _event_timezone()
    if tz:
        d["timeZone"] = tz
    return d


def _event_end_local(ev: dict) -> dt.datetime | None:
    end = ev.get("end") or {}
    if end.get("dateTime"):
        try:
            t = dt.datetime.fromisoformat(str(end["dateTime"]).replace("Z", "+00:00"))
        except ValueError:
            return None
        if t.tzinfo is not None:
            t = t.astimezone(_event_tzinfo()).replace(tzinfo=None)
        return t
    if end.get("date"):
        try:
            d = dt.date.fromisoformat(str(end["date"]))
        except ValueError:
            return None
        return dt.datetime(d.year, d.month, d.day)
    return None


def _done_event_expired(ev: dict, now: dt.datetime, keep_days: int) -> bool:
    """A done event is past its keep window: measured from private
    `macp_completed`, else (a hand-detached event) from the event's end."""
    priv = (ev.get("extendedProperties") or {}).get("private") or {}
    ref = _parse_completed(priv.get("macp_completed")) or _event_end_local(ev)
    if ref is None:
        return False
    return now - ref > dt.timedelta(days=keep_days)


def record_completion_transition(con: sqlite3.Connection, slug: str, new_status: str | None,
                                 worker_mode: str | None, md_path: Path,
                                 now: dt.datetime | None = None) -> str | None:
    """Stamp `completed: "YYYY-MM-DD HH:MM"` into a task md on its open→done.

    Called by ``cli.upsert_task`` BEFORE it overwrites the row, so the DB still
    holds the previous status. Writes only when all hold: the md says done, the
    DB row exists and is `open`, the task is human-owned, its md has a timed
    block, and the md has no `completed:` yet. A task first seen already done
    (old done) is never stamped. Returns the stamp written, else None. Never
    raises — a failure here must not break the todo import.
    """
    try:
        if (new_status or "").strip().lower() != "done":
            return None
        row = con.execute("SELECT status FROM tasks WHERE slug = ?", (slug,)).fetchone()
        if row is None or str(row[0] or "").strip().lower() != "open":
            return None
        if is_worker_owned(worker_mode):
            return None
        from .cli import parse_frontmatter, update_frontmatter_value
        path = Path(md_path)
        text = path.read_text(encoding="utf-8")
        fm, _ = parse_frontmatter(text)
        done_at = fm.get("completed")
        if isinstance(done_at, str) and done_at.strip() not in ("", "-"):
            return None
        sched = fm.get("scheduled")
        d, s, e = _parse_scheduled_value(sched if isinstance(sched, str) else None)
        if not (d and s and e):
            return None
        stamp = _fmt_completed(now or _now_local())
        path.write_text(update_frontmatter_value(text, "completed", f'"{stamp}"'), encoding="utf-8")
        print(f"calendar: recorded completed {slug} {stamp}")
        return stamp
    except Exception as exc:  # noqa: BLE001 - never break the import
        print(f"calendar: completed record skipped — {exc}")
        return None


def _finalize_done_event(service, calendar_id: str, slug: str, title: str, next_action: str,
                         source_path: str, block: tuple[dt.datetime, dt.datetime],
                         completed: dt.datetime) -> str:
    """Re-shape one done task's event: `✅ title`, trimmed/moved to end at the
    completion time, private `todo_slug` swapped for `todo_done_slug`.

    An event already marked `todo_done_slug` (a previous run, or one detached
    by hand) is reused before a live `todo_slug` one, so no duplicate appears;
    any extra copies are deleted. Returns created / updated / unchanged — a
    done event already in the desired shape costs no API write.
    """
    def _find(key: str) -> list[dict]:
        return (
            service.events()
            .list(calendarId=calendar_id, privateExtendedProperty=f"{key}={slug}",
                  singleEvents=True, maxResults=50)
            .execute()
            .get("items", [])
        )

    done_items, live_items = _find("todo_done_slug"), _find("todo_slug")
    candidates = done_items + [e for e in live_items
                               if e.get("id") not in {d.get("id") for d in done_items}]
    target = candidates[0] if candidates else None
    for item in candidates[1:]:
        try:
            service.events().delete(calendarId=calendar_id, eventId=item["id"]).execute()
        except Exception:
            # Already gone (HTTP 410) or otherwise unavailable.
            pass

    s, e = done_block_window(*block, completed)
    stamp = _fmt_completed(completed)
    private: dict[str, object] = {
        # Explicit null: Google's patch merges private keys, so only a null
        # removes the live `todo_slug` (see _clear_other_shape).
        "todo_slug": None,
        "todo_done_slug": slug,
        "todo_source": source_path,
        "macp_completed": stamp,
    }
    body: dict[str, object] = {
        "summary": _done_title(title),
        "description": f"slug: {slug}\nnext_action: {next_action or '-'}\nsource: {source_path}",
        "start": _timed_when(s),
        "end": _timed_when(e),
        "extendedProperties": {"private": private},
        "reminders": {"useDefault": False, "overrides": []},
    }

    if target is None:
        private.pop("todo_slug")
        service.events().insert(calendarId=calendar_id, body=body).execute()
        return "created"

    priv = (target.get("extendedProperties") or {}).get("private") or {}
    if (priv.get("todo_done_slug") == slug
            and not priv.get("todo_slug")
            and priv.get("macp_completed") == stamp
            and target.get("summary") == body["summary"]
            and _canon(target.get("start") or {}) == _canon(body["start"])
            and _canon(target.get("end") or {}) == _canon(body["end"])):
        return "unchanged"

    body["start"], body["end"] = _clear_other_shape(body["start"]), _clear_other_shape(body["end"])
    service.events().patch(calendarId=calendar_id, eventId=target["id"], body=body).execute()
    return "updated"


# (tag, calendarId) — one `--calendar-route TAG=CALENDAR_ID` flag.
CalendarRoute = tuple[str, str]


def parse_calendar_route(value: str) -> CalendarRoute:
    """argparse ``type=`` for ``--calendar-route TAG=CALENDAR_ID``.

    Splits on the first ``=`` (a calendar id may itself contain one); both
    halves are stripped and must be non-empty.
    """
    tag, sep, cal = value.partition("=")
    tag, cal = tag.strip(), cal.strip()
    if not sep or not tag or not cal:
        raise argparse.ArgumentTypeError(
            f"expected TAG=CALENDAR_ID, got {value!r}")
    return tag, cal


def _split_tags(raw: str | None) -> list[str]:
    """`tasks.tags` (comma-joined by the importer) as a list of stripped tags."""
    if not raw:
        return []
    return [t.strip() for t in str(raw).split(",") if t.strip()]


def route_calendar(
    tags: Sequence[str] | None,
    routes: Sequence[CalendarRoute] | None,
    default: str,
) -> str:
    """The calendar a task with these tags is synced to.

    Walks the TASK's tags in their own order and returns the route of the
    first tag that has one — so a task's tag order decides between two routed
    tags, not the order of the flags. When the same tag is routed twice, the
    first flag wins. No routed tag → ``default`` (``--calendar-id``).
    """
    by_tag: dict[str, str] = {}
    for tag, cal in routes or ():
        by_tag.setdefault(tag, cal)
    for tag in tags or ():
        cal = by_tag.get(tag.strip())
        if cal:
            return cal
    return default


def managed_calendars(
    calendar_id: str, routes: Sequence[CalendarRoute] | None
) -> list[str]:
    """Every calendar the sync owns todo events in: ``calendar_id`` first, then
    each route calendar, each listed once."""
    return list(dict.fromkeys([calendar_id, *(cal for _, cal in routes or ())]))


def sync_due_tasks_to_calendar(
    con: sqlite3.Connection,
    calendar_id: str,
    client_secret_path: Path,
    token_path: Path,
    calendar_routes: Sequence[CalendarRoute] | None = None,
    timed_only: bool = True,
) -> tuple[int, int]:
    """Upsert open human-owned tasks as events and delete stale todo events.

    ``timed_only`` (the default) puts only tasks with a timed work block
    (`scheduled: DATE HH:MM~HH:MM`) on the calendar: the calendar is for work a
    person sits down to at a set time. A task with just a due date or an
    all-day `scheduled:` stays on the dashboard only, and an event it got
    earlier is deleted by the cleanup. ``timed_only=False``
    (``--calendar-all-dated``) is the old rule: every dated task gets an event.

    ``calendar_routes`` (``--calendar-route TAG=CALENDAR_ID``) sends a task to
    the calendar of its first routed tag (see :func:`route_calendar`); an
    unrouted task goes to ``calendar_id``. Cleanup covers every managed
    calendar (``calendar_id`` plus each route calendar). No routes → only
    ``calendar_id``, exactly as before routing existed.

    Done human tasks with a timed block and a `completed:` stamp (written by
    :func:`record_completion_transition`) keep their event as a `✅` record
    trimmed to the completion time (:func:`_finalize_done_event`); done events
    are deleted ``DONE_KEEP_DAYS`` (30) after completion.
    A done task without `completed:` (done before 2026-11-19) loses its event
    as before. A reopened task's ✅ event stays as history until it ages out.

    A decision task (the `decision` tag or `needs_decision: true`, see
    :func:`_is_decision_task`) never gets an event, whatever its `scheduled:`
    says: it is answered on the dashboard, not sat down to at a set time. It is
    dropped before anything else is computed, so the cleanup deletes the event
    it already has, a decision child does not turn its parent into a container,
    a done decision keeps no ✅ record, and the overdue mover never sees it.
    """
    creds = load_credentials(client_secret_path, token_path)
    service = build("calendar", "v3", credentials=creds)

    # Only human-owned tasks belong on a person's calendar — worker_mode
    # auto/delegate/... tasks are the pipeline's to do, and are tracked in
    # Worker/queue.json + the dashboard instead.
    human_owned, human_params = human_owned_sql_filter()
    # `tags` is read only when routing needs it, so the no-route query is the
    # same text as before.
    cols = "slug, title, due, next_action, path" + (", tags" if calendar_routes else "")
    rows = con.execute(
        f"""
        SELECT {cols}
        FROM tasks
        WHERE status = 'open'
          AND {human_owned}
        ORDER BY due, title
        """,
        human_params,
    ).fetchall()

    # A decision is answered on the dashboard, so it has no place on the
    # calendar. Dropped FIRST: before covered_parents (a decision child carries
    # no block that could stand in for its parent's), before target_by_slug
    # (so the cleanup below deletes an event it already has) and before the
    # overdue mover, which is handed these same `rows`.
    rows = [r for r in rows if not _is_decision_task(str(r["path"]))]

    # A task whose open children carry timed blocks (`scheduled: DATE HH:MM~HH:MM`)
    # is a container: the children are the calendar entries, and the parent's
    # own all-day event would only duplicate them. Dropping it here also lets
    # the cleanup below remove an all-day event it already has.
    covered_parents = set()
    for r in rows:
        _, child_start, child_end = _parse_scheduled(str(r["path"]))
        if child_start and child_end:
            parent = _parent_slug(str(r["path"]))
            if parent:
                covered_parents.add(parent)
    rows = [r for r in rows if str(r["slug"]) not in covered_parents]

    # A task with neither `due` nor a scheduled date has no place on the
    # calendar. Leaving it out of target_by_slug lets the cleanup below delete
    # the event it got while it was still dated (a task put on hold by
    # clearing both fields used to keep its old event forever).
    def _is_dated(r: sqlite3.Row) -> bool:
        due = r["due"]
        return bool(due and due != "-") or _parse_scheduled(str(r["path"]))[0] is not None

    # Timed-only (the default): a due date or an all-day `scheduled:` is a
    # deadline, not a block of focused work, and fills the calendar with small
    # chores (2026-11-14: a 5-minute re-login showed up as an all-day event).
    def _is_timed(r: sqlite3.Row) -> bool:
        _, start, end = _parse_scheduled(str(r["path"]))
        return bool(start and end)

    keep = _is_timed if timed_only else _is_dated
    skipped = sum(1 for r in rows if not keep(r))
    rows = [r for r in rows if keep(r)]

    # Each synced slug → the one calendar its event belongs in.
    target_by_slug = {
        str(r["slug"]): (
            route_calendar(_split_tags(r["tags"]), calendar_routes, calendar_id)
            if calendar_routes else calendar_id
        )
        for r in rows
    }

    # Done human blocks that keep a ✅ event (2026-11-19). Collected BEFORE the
    # cleanup: a task that just went done still has its live `todo_slug` event,
    # which the cleanup would otherwise delete before it can be trimmed.
    now_local = _now_local()
    keep_days = DONE_KEEP_DAYS
    done_targets: list[tuple[sqlite3.Row, str, tuple[dt.datetime, dt.datetime], dt.datetime]] = []
    try:
        done_rows = con.execute(
            f"""
            SELECT {cols}
            FROM tasks
            WHERE status = 'done'
              AND {human_owned}
            ORDER BY title
            """,
            human_params,
        ).fetchall()
        for r in done_rows:
            src = str(r["path"])
            d, s, e = _parse_scheduled(src)
            if not (d and s and e):
                continue
            completed = _parse_completed(_extract_field_from_markdown(src, "completed"))
            if completed is None or now_local - completed > dt.timedelta(days=keep_days):
                continue
            # Last, so the md is parsed only for the few done rows that would get an
            # event: a decision never had a block worth a ✅ record.
            if _is_decision_task(src):
                continue
            cal = (route_calendar(_split_tags(r["tags"]), calendar_routes, calendar_id)
                   if calendar_routes else calendar_id)
            done_targets.append((r, cal, _block_bounds(d, s, e), completed))
    except Exception as exc:  # noqa: BLE001 - old behaviour beats a dead tick
        print(f"calendar: done blocks skipped — {exc}")
        done_targets = []
    done_cal_by_slug = {str(r["slug"]): cal for r, cal, _, _ in done_targets}

    # --- Cleanup: delete stale events in every managed calendar ---
    # Stale = the event's todo_slug is not synced any more, or is synced to a
    # different calendar (the task moved: it is inserted fresh in its new
    # calendar from the md, so edits made to the old event in the calendar app
    # are dropped). A just-done task's `todo_slug` event is kept for
    # _finalize_done_event below. Done events (`todo_done_slug`, no
    # `todo_slug`) are kept until DONE_KEEP_DAYS after
    # completion. Events with neither key are never touched.
    # Lookback is generous: an event only gets deleted if it carries a
    # todo_slug we no longer sync here, and events drift out of a narrow window
    # (done long ago, or newly worker-owned) faster than they get cleaned.
    now = dt.datetime.now(_event_tzinfo())
    time_min = (now - dt.timedelta(days=400)).isoformat()
    time_max = (now + dt.timedelta(days=400)).isoformat()
    deleted_stale = 0
    for cal in managed_calendars(calendar_id, calendar_routes):
        page_token: str | None = None
        while True:
            all_events = (
                service.events()
                .list(
                    calendarId=cal,
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    maxResults=500,
                    pageToken=page_token,
                )
                .execute()
            )
            for ev in all_events.get("items", []):
                priv = (ev.get("extendedProperties") or {}).get("private") or {}
                slug = priv.get("todo_slug")
                done_slug = priv.get("todo_done_slug")
                if slug:
                    stale = (target_by_slug.get(slug) != cal
                             and done_cal_by_slug.get(slug) != cal)
                else:
                    stale = bool(done_slug
                                 and done_cal_by_slug.get(done_slug) != cal
                                 and _done_event_expired(ev, now_local, keep_days))
                if stale:
                    try:
                        service.events().delete(calendarId=cal, eventId=ev["id"]).execute()
                        deleted_stale += 1
                    except Exception:
                        pass
            page_token = all_events.get("nextPageToken")
            if not page_token:
                break

    created_or_updated = 0
    for row in rows:
        due = row["due"]
        source_path = str(row["path"])
        sched_date, scheduled_start, scheduled_end = _parse_scheduled(source_path)
        due_time = _parse_due_time(source_path)
        has_due = bool(due and due != "-")

        effective_due = due if has_due else sched_date
        result = _upsert_event(
            service=service,
            calendar_id=target_by_slug[str(row["slug"])],
            slug=str(row["slug"]),
            title=str(row["title"]),
            due=str(effective_due),
            scheduled_date=sched_date,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            due_time=due_time,
            next_action=str(row["next_action"] or ""),
            source_path=source_path,
            due_from_scheduled=not has_due,
        )
        if result != "unchanged":
            created_or_updated += 1

    # Done blocks → ✅ events ending at the completion time. They carry no
    # `todo_slug`, so the upserts above never pull them into the md, and the
    # overdue mover below only sees open `rows`.
    for r, cal, block, completed in done_targets:
        try:
            result = _finalize_done_event(
                service, cal, str(r["slug"]), str(r["title"]), str(r["next_action"] or ""),
                str(r["path"]), block, completed)
        except Exception as exc:  # noqa: BLE001 - one bad event must not stop the sync
            print(f"calendar: done block {r['slug']} skipped — {exc}")
            continue
        if result != "unchanged":
            created_or_updated += 1

    # After the upserts, so a block the person moved in the calendar has already
    # been pulled into the md. The md writes reach the calendar on the next sync.
    reschedule_overdue_blocks(service, rows, managed_calendars(calendar_id, calendar_routes))

    return created_or_updated, skipped


def fetch_upcoming_events(
    calendar_id: str,
    client_secret_path: Path,
    token_path: Path,
    days: int = 7,
) -> list[dict[str, str]]:
    """Fetch upcoming events from a Google Calendar."""
    creds = load_credentials(client_secret_path, token_path)
    service = build("calendar", "v3", credentials=creds)

    tzinfo = _event_tzinfo()
    now = dt.datetime.now(tzinfo)
    # Start from today's midnight so today's earlier events still show
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    time_min = today_start.isoformat()
    time_max = (today_start + dt.timedelta(days=days)).isoformat()

    list_kw: dict[str, object] = {
        "calendarId": calendar_id,
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": True,
        "orderBy": "startTime",
        "maxResults": 100,
    }
    # Same rule as the event body: no configured zone means no `timeZone`
    # argument, and Google answers in the calendar's own timezone.
    tz_name = _event_timezone()
    if tz_name:
        list_kw["timeZone"] = tz_name
    result = service.events().list(**list_kw).execute()

    events: list[dict[str, str]] = []
    for item in result.get("items", []):
        raw_start = item["start"].get("dateTime") or item["start"].get("date", "")
        raw_end = item["end"].get("dateTime") or item["end"].get("date", "")
        all_day = "date" in item["start"]

        if not all_day and raw_start:
            start_dt_parsed = dt.datetime.fromisoformat(raw_start).astimezone(tzinfo)
            end_dt_parsed = dt.datetime.fromisoformat(raw_end).astimezone(tzinfo)
            start = start_dt_parsed.isoformat()
            end = end_dt_parsed.isoformat()
        else:
            start = raw_start
            end = raw_end

        events.append({
            "summary": item.get("summary", "(제목 없음)"),
            "start": start,
            "end": end,
            "all_day": all_day,
            "location": item.get("location", ""),
        })

    return events


def render_calendar_markdown(
    calendar_id: str,
    client_secret_path: Path,
    token_path: Path,
    days: int = 7,
) -> str:
    """Render upcoming Google Calendar events as markdown."""
    events = fetch_upcoming_events(calendar_id, client_secret_path, token_path, days)

    now = dt.datetime.now(_event_tzinfo())
    synced = now.strftime("%Y-%m-%dT%H:%M:%S")

    lines = [
        "---",
        "tags:",
        "  - todo",
        "  - calendar",
        f"updated: {now.strftime('%Y-%m-%d')}",
        "---",
        "",
        "# 📆 Google Calendar 일정",
        "",
        f"> last_synced: {synced}",
        f"> calendar: {calendar_id}",
        f"> range: {days}일",
        "",
    ]

    if not events:
        lines.append("- 예정된 일정 없음")
        lines.append("")
        return "\n".join(lines)

    # Group by date
    grouped: dict[str, list[dict[str, str]]] = {}
    for ev in events:
        start_str = ev["start"]
        if "T" in start_str:
            date_key = start_str[:10]
        else:
            date_key = start_str
        grouped.setdefault(date_key, []).append(ev)

    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + dt.timedelta(days=1)).strftime("%Y-%m-%d")
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]

    for date_key in sorted(grouped):
        d = dt.date.fromisoformat(date_key)
        wd = weekdays[d.weekday()]
        if date_key == today_str:
            label = f"**오늘** ({d.month}/{d.day} {wd})"
        elif date_key == tomorrow_str:
            label = f"**내일** ({d.month}/{d.day} {wd})"
        else:
            label = f"{d.month}/{d.day} ({wd})"

        lines.append(f"### {label}")
        lines.append("")

        for ev in grouped[date_key]:
            summary = ev["summary"]
            if ev["all_day"]:
                time_str = "종일"
            else:
                start_t = ev["start"][11:16]
                end_t = ev["end"][11:16]
                time_str = f"{start_t}~{end_t}"
            loc = ev["location"]
            if loc and re.match(r"https?://", loc):
                location = f" @ [링크]({loc})"
            elif loc:
                location = f" @ {loc}"
            else:
                location = ""
            lines.append(f"- {time_str} | {summary}{location}")

        lines.append("")

    return "\n".join(lines)


def render_unified_markdown(
    calendar_id: str,
    client_secret_path: Path,
    token_path: Path,
    con: sqlite3.Connection,
    days: int = 7,
    events: list[dict] | None = None,
) -> str:
    """Render a unified view: calendar events + open todos by date."""
    if events is None:
        events = fetch_upcoming_events(calendar_id, client_secret_path, token_path, days)

    now = dt.datetime.now(_event_tzinfo())
    synced = now.strftime("%Y-%m-%dT%H:%M:%S")
    today_str = now.strftime("%Y-%m-%d")
    tomorrow_str = (now + dt.timedelta(days=1)).strftime("%Y-%m-%d")
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]

    # --- Collect all dates with events ---
    date_events: dict[str, list[dict]] = {}
    for ev in events:
        start_str = ev["start"]
        date_key = start_str[:10] if "T" in start_str else start_str
        date_events.setdefault(date_key, []).append(ev)

    # --- Collect open tasks ---
    rows = con.execute(
        """
        SELECT slug, title, due, next_action, path, estimate_p50, status,
               COALESCE(priority, 50) AS priority,
               CASE lane
                   WHEN 'urgent' THEN 1
                   WHEN 'this_week' THEN 2
                   WHEN 'long_project' THEN 3
                   WHEN 'backlog' THEN 4
                   ELSE 5
               END AS lane_order,
               lane
        FROM tasks
        WHERE status = 'open'
        ORDER BY due, lane_order, title
        """
    ).fetchall()

    # --- Collect today's completed tasks ---
    today_done_rows = con.execute(
        """
        SELECT slug, title, due, next_action, path, estimate_p50, status,
               COALESCE(priority, 50) AS priority,
               CASE lane
                   WHEN 'urgent' THEN 1
                   WHEN 'this_week' THEN 2
                   WHEN 'long_project' THEN 3
                   WHEN 'backlog' THEN 4
                   ELSE 5
               END AS lane_order,
               lane
        FROM tasks
        WHERE status = 'done'
          AND updated = ?
        ORDER BY lane_order, title
        """,
        (today_str,),
    ).fetchall()

    date_tasks: dict[str, list[sqlite3.Row]] = {}
    undated_tasks: list[sqlite3.Row] = []
    overdue_tasks: list[sqlite3.Row] = []

    for row in rows:
        due = row["due"]
        sched_date, _, _ = _parse_scheduled(str(row["path"]))
        effective_date = sched_date if sched_date else (due if (due and due != "-") else None)
        if not effective_date:
            undated_tasks.append(row)
        elif effective_date < today_str:
            overdue_tasks.append(row)
        else:
            date_tasks.setdefault(effective_date, []).append(row)

    # --- Add today's completed tasks to today ---
    for row in today_done_rows:
        date_tasks.setdefault(today_str, []).append(row)

    # --- Merge all dates ---
    all_dates = sorted(set(list(date_events.keys()) + list(date_tasks.keys())))


    def _est(row: sqlite3.Row) -> str:
        e = row["estimate_p50"]
        return f" ({e})" if e and e != "-" else ""

    def _prio_icon(p: float) -> str:
        if p >= 80:
            return "🔴"
        if p >= 60:
            return "🟡"
        if p >= 40:
            return "🟣"
        return "🟢"

    def _prio_label(row: sqlite3.Row) -> str:
        try:
            p = float(row["priority"])
        except (KeyError, TypeError):
            p = 50.0
        return f"{_prio_icon(p)} P{int(round(p))}"

    def _parent_link(row: sqlite3.Row) -> str:
        raw = _extract_field_from_markdown(str(row["path"]), "parent")
        if not raw or raw == "-":
            return ""
        # Strip wikilink syntax [[slug]] if present
        m = re.match(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", raw.strip())
        parent_slug = m.group(1) if m else raw.strip()
        return f" ↑[[Todo/items/{parent_slug}|{parent_slug}]]"

    # --- Render ---
    lines = [
        "---",
        "tags:",
        "  - todo",
        "  - unified",
        f"updated: {now.strftime('%Y-%m-%d')}",
        "---",
        "",
        "# 📋 이번 주 통합 뷰",
        "",
        f"> last_synced: {synced}",
        f"> range: {days}일",
        "",
    ]

    # Overdue
    if overdue_tasks:
        lines.append("### 🚨 Overdue")
        lines.append("")
        for row in overdue_tasks:
            lines.append(f"- [ ] {_prio_label(row)} [[Todo/items/{row['slug']}|{row['title']}]] (due: {row['due']}){_est(row)}{_parent_link(row)}")
        lines.append("")

    # Date sections
    for date_key in all_dates:
        d = dt.date.fromisoformat(date_key)
        wd = weekdays[d.weekday()]
        if date_key == today_str:
            label = f"**오늘** ({d.month}/{d.day} {wd})"
        elif date_key == tomorrow_str:
            label = f"**내일** ({d.month}/{d.day} {wd})"
        else:
            label = f"{d.month}/{d.day} ({wd})"

        lines.append(f"### {label}")
        lines.append("")

        # Calendar events (sorted by time)
        cal_lines: list[tuple[str, str]] = []
        if date_key in date_events:
            for ev in date_events[date_key]:
                if ev["all_day"]:
                    time_str = "종일"
                    sort_key = "00:00"
                else:
                    start_t = ev["start"][11:16]
                    end_t = ev["end"][11:16]
                    time_str = f"{start_t}~{end_t}"
                    sort_key = start_t
                loc = ev["location"]
                if loc and re.match(r"https?://", loc):
                    location = f" @ [링크]({loc})"
                elif loc:
                    location = f" @ {loc}"
                else:
                    location = ""
                cal_lines.append((sort_key, f"- 📅 {time_str} | {ev['summary']}{location}"))
        cal_lines.sort(key=lambda x: x[0])
        for _, line in cal_lines:
            lines.append(line)

        # Tasks (sorted by priority desc)
        if date_key in date_tasks:
            task_lines: list[tuple[float, str]] = []
            for row in date_tasks[date_key]:
                is_done = row["status"] == "done"
                checkbox = "[x]" if is_done else "[ ]"
                _, sched_start, sched_end = _parse_scheduled(str(row["path"]))
                prio = float(row["priority"] or 50)
                if sched_start and sched_end:
                    task_lines.append((prio, f"- {checkbox} {_prio_label(row)} {sched_start}~{sched_end} | [[Todo/items/{row['slug']}|{row['title']}]]{_est(row)}{_parent_link(row)}"))
                else:
                    task_lines.append((prio, f"- {checkbox} {_prio_label(row)} [[Todo/items/{row['slug']}|{row['title']}]]{_est(row)}{_parent_link(row)}"))
            task_lines.sort(key=lambda x: -x[0])
            for _, line in task_lines:
                lines.append(line)

        lines.append("")

    # All open leaf tasks by composite score (priority + due proximity)
    def _is_parent(path: str) -> bool:
        val = _extract_field_from_markdown(path, "is_parent")
        return val == "true"

    today_date = now.date()

    def _effective_date(row: sqlite3.Row) -> dt.date | None:
        sched_date_str, _, _ = _parse_scheduled(str(row["path"]))
        if sched_date_str and sched_date_str != "-":
            try:
                return dt.date.fromisoformat(sched_date_str)
            except ValueError:
                pass
        due_raw = row["due"]
        if due_raw and due_raw != "-":
            try:
                return dt.date.fromisoformat(due_raw)
            except (ValueError, TypeError):
                pass
        return None

    def _composite_score(row: sqlite3.Row) -> float:
        p = float(row["priority"] or 50)
        eff_date = _effective_date(row)
        if eff_date is None:
            return p
        days = (eff_date - today_date).days
        # Today / overdue: +30 boost. 7d: +23. 30d: +0. >30d: +0.
        urgency = max(0.0, 30.0 - max(0, days))
        return p + urgency

    all_open_rows = [
        r for r in rows
        if r["status"] == "open" and not _is_parent(str(r["path"]))
    ]
    all_open_rows.sort(key=lambda r: (-_composite_score(r), r["title"]))

    if all_open_rows:
        lines.append("---")
        lines.append("")
        lines.append(f"### 🔥 전체 우선순위 뷰 ({len(all_open_rows)}건)")
        lines.append("")
        for row in all_open_rows:
            sched_date, sched_start, sched_end = _parse_scheduled(str(row["path"]))
            due = row["due"]

            # Build date/time annotation
            when = ""
            if sched_date:
                if sched_start and sched_end:
                    when = f" ({sched_date} {sched_start}~{sched_end})"
                else:
                    when = f" ({sched_date})"
            elif due and due != "-":
                when = f" (due: {due})"

            lines.append(f"- [ ] {_prio_label(row)} [[Todo/items/{row['slug']}|{row['title']}]]{_est(row)}{when}{_parent_link(row)}")
        lines.append("")

    # Completed tasks this week
    week_start = (now - dt.timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    done_rows = con.execute(
        """
        SELECT slug, title, updated
        FROM tasks
        WHERE status = 'done'
          AND updated >= ?
        ORDER BY updated DESC
        """,
        (week_start,),
    ).fetchall()

    if done_rows:
        lines.append("---")
        lines.append("")
        lines.append(f"### ✅ 이번 주 완료 ({len(done_rows)}건)")
        lines.append("")
        for row in done_rows:
            lines.append(f"- [x] [[Todo/items/{row['slug']}|{row['title']}]] ({row['updated']})")
        lines.append("")

    return "\n".join(lines)
