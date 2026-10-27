from __future__ import annotations

import datetime as dt
import re
import sqlite3
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .ownership import human_owned_sql_filter
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
    val = _extract_field_from_markdown(source_path, "scheduled")
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
) -> str:
    due_date = dt.date.fromisoformat(due)
    event_date = dt.date.fromisoformat(scheduled_date) if scheduled_date else due_date

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
        body["start"] = _timed(start_dt)
        body["end"] = _timed(end_dt)
    elif due_time:
        # Fallback to due_time (deadline) — show as 1h event
        start_dt = dt.datetime.fromisoformat(f"{due_date.isoformat()}T{due_time}:00")
        end_dt = start_dt + dt.timedelta(hours=1)
        body["start"] = _timed(start_dt)
        body["end"] = _timed(end_dt)
    else:
        end_date = event_date + dt.timedelta(days=1)
        body["start"] = {"date": event_date.isoformat()}
        body["end"] = {"date": end_date.isoformat()}

    # Two-way sync (2026-10-27): the event remembers what macp last wrote
    # (`macp_start`/`macp_end`, canonical form). A start/end that differs from
    # that baseline was edited in the calendar app; the old code deleted and
    # re-inserted every event on every md change, so such edits snapped back
    # within minutes. Now: calendar edited, md not → pull into the md's
    # `scheduled:`; md edited, calendar not → patch the event; both → the
    # later edit wins (event `updated` vs md mtime); neither → no API write.
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

    if not items:
        body["extendedProperties"]["private"].update(
            {"macp_start": desired[0], "macp_end": desired[1]})
        service.events().insert(calendarId=calendar_id, body=body).execute()
        return "created"

    ev = items[0]
    priv = (ev.get("extendedProperties") or {}).get("private") or {}
    actual = (_canon(ev.get("start") or {}), _canon(ev.get("end") or {}))
    base = (priv.get("macp_start"), priv.get("macp_end"))
    has_base = all(base)
    cal_changed = has_base and actual != base
    md_changed = (not has_base) or desired != base   # legacy event: md wins once

    if cal_changed and (not md_changed or _event_newer_than_md(ev, source_path)):
        sched = _scheduled_value(ev.get("start") or {}, ev.get("end") or {})
        if sched:
            _write_scheduled(source_path, sched)
        body["start"], body["end"] = ev["start"], ev["end"]
        body["extendedProperties"]["private"].update(
            {"macp_start": actual[0], "macp_end": actual[1]})
        service.events().patch(calendarId=calendar_id, eventId=ev["id"], body=body).execute()
        return "pulled"

    if (actual == desired and base == desired and ev.get("summary") == body["summary"]
            and ev.get("description") == body["description"]):
        return "unchanged"

    body["extendedProperties"]["private"].update(
        {"macp_start": desired[0], "macp_end": desired[1]})
    service.events().patch(calendarId=calendar_id, eventId=ev["id"], body=body).execute()
    return "updated"


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


def _write_scheduled(source_path: str, value: str) -> None:
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
        text = text.rstrip("\n") + f"\n- {today}: 캘린더에서 일정을 옮겨 scheduled를 {value}로 맞춤 (calendar sync)\n"
    path.write_text(text, encoding="utf-8")


def sync_due_tasks_to_calendar(
    con: sqlite3.Connection,
    calendar_id: str,
    client_secret_path: Path,
    token_path: Path,
) -> tuple[int, int]:
    creds = load_credentials(client_secret_path, token_path)
    service = build("calendar", "v3", credentials=creds)

    # Only human-owned tasks belong on a person's calendar — worker_mode
    # auto/delegate/... tasks are the pipeline's to do, and are tracked in
    # Worker/queue.json + the dashboard instead.
    human_owned, human_params = human_owned_sql_filter()
    rows = con.execute(
        f"""
        SELECT slug, title, due, next_action, path
        FROM tasks
        WHERE status = 'open'
          AND {human_owned}
        ORDER BY due, title
        """,
        human_params,
    ).fetchall()

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

    # --- Cleanup: delete stale events (slug not in current open tasks) ---
    # Lookback is generous: an event only gets deleted if it carries a
    # todo_slug we no longer sync, and events drift out of a narrow window
    # (done long ago, or newly worker-owned) faster than they get cleaned.
    open_slugs = {str(r["slug"]) for r in rows}
    now = dt.datetime.now(_event_tzinfo())
    time_min = (now - dt.timedelta(days=400)).isoformat()
    time_max = (now + dt.timedelta(days=400)).isoformat()
    deleted_stale = 0
    page_token: str | None = None
    while True:
        all_events = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                maxResults=500,
                pageToken=page_token,
            )
            .execute()
        )
        for ev in all_events.get("items", []):
            slug = ev.get("extendedProperties", {}).get("private", {}).get("todo_slug")
            if slug and slug not in open_slugs:
                try:
                    service.events().delete(calendarId=calendar_id, eventId=ev["id"]).execute()
                    deleted_stale += 1
                except Exception:
                    pass
        page_token = all_events.get("nextPageToken")
        if not page_token:
            break

    created_or_updated = 0
    skipped = 0
    for row in rows:
        due = row["due"]
        source_path = str(row["path"])
        sched_date, scheduled_start, scheduled_end = _parse_scheduled(source_path)
        due_time = _parse_due_time(source_path)

        # Skip if neither due date nor scheduled date
        has_due = due and due != "-"
        has_sched = sched_date is not None
        if not has_due and not has_sched:
            skipped += 1
            continue

        effective_due = due if has_due else sched_date
        result = _upsert_event(
            service=service,
            calendar_id=calendar_id,
            slug=str(row["slug"]),
            title=str(row["title"]),
            due=str(effective_due),
            scheduled_date=sched_date,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            due_time=due_time,
            next_action=str(row["next_action"] or ""),
            source_path=source_path,
        )
        if result != "unchanged":
            created_or_updated += 1

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
