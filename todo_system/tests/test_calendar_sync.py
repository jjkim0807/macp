"""Calendar sync only ever puts human-owned tasks on a person's calendar."""
from __future__ import annotations

import datetime as dt
import sqlite3
import unittest
from pathlib import Path

import pytest

from todo_system import calendar_sync as CS
from todo_system import cli as CLI
from todo_system.ownership import human_owned_sql_filter, is_worker_owned


class _FakeEvents:
    """Minimal google-api-client `service.events()` stand-in."""

    def __init__(self, existing=None):
        self.existing = list(existing or [])
        self.inserted: list[dict] = []
        self.deleted: list[str] = []

    # --- the executable-request shape the real client returns ---
    class _Req:
        def __init__(self, result):
            self._result = result

        def execute(self):
            return self._result

    def list(self, **kw):
        if "privateExtendedProperty" in kw:  # per-slug lookup in _upsert_event
            slug = kw["privateExtendedProperty"].split("=", 1)[1]
            items = [e for e in self.existing if e["_slug"] == slug]
            return self._Req({"items": items})
        return self._Req({"items": list(self.existing)})  # cleanup scan

    def insert(self, calendarId, body):
        self.inserted.append(body)
        return self._Req({})

    def delete(self, calendarId, eventId):
        self.deleted.append(eventId)
        return self._Req({})


class _FakeService:
    def __init__(self, events):
        self._events = events

    def events(self):
        return self._events


def _db(rows):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE tasks (slug TEXT, title TEXT, due TEXT, next_action TEXT, "
        "path TEXT, status TEXT, worker_mode TEXT)"
    )
    con.executemany(
        "INSERT INTO tasks (slug, title, due, next_action, path, status, worker_mode) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return con


def _full_db(rows: list[dict]) -> sqlite3.Connection:
    """Build a tmp sqlite DB with the full `cli.init_db` schema that
    `render_unified_markdown` requires (priority, lane, estimate_p50, updated)."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    CLI.init_db(con)
    for i, row in enumerate(rows):
        defaults = {
            "slug": f"task-{i}",
            "title": f"Task {i}",
            "path": f"Todo/items/task-{i}.md",
            "status": "open",
            "due": None,
            "next_action": None,
            "estimate_p50": None,
            "priority": None,
            "lane": None,
            "updated": None,
            "imported_at": "2026-01-01T00:00:00",
        }
        defaults.update(row)
        con.execute(
            "INSERT INTO tasks (slug, title, path, status, due, next_action, "
            "estimate_p50, priority, lane, updated, imported_at) "
            "VALUES (:slug, :title, :path, :status, :due, :next_action, "
            ":estimate_p50, :priority, :lane, :updated, :imported_at)",
            defaults,
        )
    con.commit()
    return con


def _sync(con, events):
    """Run sync_due_tasks_to_calendar against the fakes."""
    orig_load, orig_build = CS.load_credentials, CS.build
    CS.load_credentials = lambda *a, **k: object()
    CS.build = lambda *a, **k: _FakeService(events)
    try:
        return CS.sync_due_tasks_to_calendar(
            con=con,
            calendar_id="cal",
            client_secret_path=Path("/nope"),
            token_path=Path("/nope"),
        )
    finally:
        CS.load_credentials, CS.build = orig_load, orig_build


class CalendarOwnershipTest(unittest.TestCase):
    def test_worker_owned_tasks_never_reach_the_calendar(self):
        con = _db([
            ("mine", "human task", "2026-08-10", "", "x.md", "open", None),
            ("bot", "worker task", "2026-08-10", "", "y.md", "open", "auto"),
            ("bot2", "delegated", "2026-08-10", "", "z.md", "open", "delegate"),
        ])
        events = _FakeEvents()
        created, _ = _sync(con, events)

        self.assertEqual(created, 1)
        summaries = [b["summary"] for b in events.inserted]
        self.assertEqual(summaries, ["human task"])
        self.assertEqual(
            events.inserted[0]["reminders"], {"useDefault": False, "overrides": []}
        )

    def test_manual_and_off_stay_on_the_calendar(self):
        # `manual` / `off` mean a human owns it despite living in a worker cluster.
        con = _db([
            ("m", "manual task", "2026-08-10", "", "x.md", "open", "manual"),
            ("o", "off task", "2026-08-10", "", "y.md", "open", "off"),
        ])
        events = _FakeEvents()
        created, _ = _sync(con, events)
        self.assertEqual(created, 2)

    def test_already_synced_worker_event_gets_deleted(self):
        """The regression: worker tasks that reached the calendar get cleaned up."""
        con = _db([("bot", "worker task", "2026-08-10", "", "y.md", "open", "auto")])
        events = _FakeEvents(existing=[
            {"id": "ev1", "_slug": "bot",
             "extendedProperties": {"private": {"todo_slug": "bot"}}},
        ])
        created, _ = _sync(con, events)

        self.assertEqual(created, 0)
        self.assertEqual(events.inserted, [])
        self.assertEqual(events.deleted, ["ev1"])

    def test_cleanup_ignores_non_todo_events(self):
        con = _db([("mine", "human task", "2026-08-10", "", "x.md", "open", None)])
        events = _FakeEvents(existing=[
            {"id": "personal", "_slug": None, "extendedProperties": {}},
        ])
        _sync(con, events)
        self.assertEqual(events.deleted, [])


class CalendarParentWithTimedChildrenTest(unittest.TestCase):
    def _write(self, d: Path, name: str, fm: str) -> str:
        path = d / f"{name}.md"
        path.write_text(f"---\n{fm}\n---\n# {name}\n", encoding="utf-8")
        return str(path)

    def test_parent_with_timed_open_child_is_left_off_and_its_event_removed(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            parent = self._write(d, "prep", "due: 2026-10-29")
            child = self._write(
                d, "prep-1",
                'parent: "[[prep|Prep]]"\nscheduled: "2026-10-28 10:00~12:00"',
            )
            con = _db([
                ("prep", "prep parent", "2026-10-29", "", parent, "open", "manual"),
                ("prep-1", "block one", "2026-10-29", "", child, "open", "manual"),
            ])
            events = _FakeEvents(existing=[
                {"id": "allday", "_slug": "prep",
                 "extendedProperties": {"private": {"todo_slug": "prep"}}},
            ])
            created, _ = _sync(con, events)

        self.assertEqual(created, 1)
        self.assertEqual([b["summary"] for b in events.inserted], ["block one"])
        self.assertIn("dateTime", events.inserted[0]["start"])
        self.assertEqual(events.deleted, ["allday"])

    def test_parent_with_only_dated_children_keeps_its_event(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            parent = self._write(d, "proj", "due: 2026-11-01")
            child = self._write(d, "proj-1", 'parent: "[[proj]]"\nscheduled: 2026-10-30')
            con = _db([
                ("proj", "project", "2026-11-01", "", parent, "open", None),
                ("proj-1", "step", "2026-10-30", "", child, "open", None),
            ])
            events = _FakeEvents()
            created, _ = _sync(con, events)

        self.assertEqual(created, 2)
        self.assertEqual(events.deleted, [])


def test_parent_slug_forms(tmp_path):
    for raw, want in [
        ('"[[a-b]]"', "a-b"), ('"[[a-b|Title]]"', "a-b"),
        ("Todo/items/a-b", "a-b"), ("-", None),
    ]:
        f = tmp_path / "t.md"
        f.write_text(f"---\nparent: {raw}\n---\n", encoding="utf-8")
        assert CS._parent_slug(str(f)) == want
    g = tmp_path / "none.md"
    g.write_text("---\ndue: -\n---\n", encoding="utf-8")
    assert CS._parent_slug(str(g)) is None


# Every value `test_mapping` below pins, plus the padded variants that used to
# split the two implementations (`is_worker_owned` strips, the SQL predicate
# did not). Keep in sync with `test_mapping` when either list grows.
OWNERSHIP_CASES = (
    "auto", "delegate", "blocked", "review", "quick", "AUTO",
    None, "", "  ", "-", "off", "manual", "Manual",
    "manual ", " auto",
)


class OwnershipTest(unittest.TestCase):
    def test_mapping(self):
        for mode in ("auto", "delegate", "blocked", "review", "quick", "AUTO"):
            self.assertTrue(is_worker_owned(mode), mode)
        for mode in (None, "", "  ", "-", "off", "manual", "Manual"):
            self.assertFalse(is_worker_owned(mode), mode)

    def test_sql_filter_agrees_with_is_worker_owned(self):
        """The two implementations of the mapping must never disagree.

        Settled by a real sqlite query, not by reading the predicate: the two
        sides normalize independently (`.strip().lower()` vs `LOWER(TRIM(...))`)
        and only the engine's own answer proves they land in the same place.
        The filter selects human-owned rows, so being selected is exactly the
        negation of `is_worker_owned`.
        """
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE tasks (slug TEXT, worker_mode TEXT)")
        con.executemany(
            "INSERT INTO tasks (slug, worker_mode) VALUES (?, ?)",
            [(str(i), mode) for i, mode in enumerate(OWNERSHIP_CASES)],
        )

        predicate, params = human_owned_sql_filter()
        selected = {
            row[0]
            for row in con.execute(f"SELECT slug FROM tasks WHERE {predicate}", params)
        }

        for i, mode in enumerate(OWNERSHIP_CASES):
            with self.subTest(worker_mode=mode):
                self.assertEqual(
                    str(i) in selected,
                    not is_worker_owned(mode),
                    f"SQL filter and is_worker_owned disagree on {mode!r}",
                )


def test_extract_field_missing_file_returns_none(tmp_path):
    assert CS._extract_field_from_markdown(str(tmp_path / "nope.md"), "due") is None


def test_extract_field_missing_field_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(CS, "ROOT", tmp_path)
    p = tmp_path / "task.md"
    p.write_text("---\ntitle: something\n---\n\nbody\n", encoding="utf-8")
    assert CS._extract_field_from_markdown(str(p), "due") is None


def test_extract_field_strips_quotes(tmp_path, monkeypatch):
    # 현행 동작 고정: 정규식이 앞뒤 따옴표를 벗겨서 반환한다 (큰따옴표/작은따옴표 둘 다).
    monkeypatch.setattr(CS, "ROOT", tmp_path)
    p1 = tmp_path / "double.md"
    p1.write_text('---\ndue: "2026-09-30"\n---\n', encoding="utf-8")
    assert CS._extract_field_from_markdown(str(p1), "due") == "2026-09-30"

    p2 = tmp_path / "single.md"
    p2.write_text("---\ndue: '2026-09-30'\n---\n", encoding="utf-8")
    assert CS._extract_field_from_markdown(str(p2), "due") == "2026-09-30"


def test_extract_field_uses_first_match_in_body(tmp_path, monkeypatch):
    # 현행 동작 고정: re.MULTILINE이라 frontmatter 뒤 본문에 같은 필드명이 다시
    # 나오면 오탐 가능하지만, 파일 앞쪽(frontmatter)의 첫 매치가 채택된다.
    monkeypatch.setattr(CS, "ROOT", tmp_path)
    p = tmp_path / "dup.md"
    p.write_text(
        "---\ndue: 2026-09-01\n---\n\n본문에도 같은 필드가 나옴: due: 2026-09-30\n",
        encoding="utf-8",
    )
    assert CS._extract_field_from_markdown(str(p), "due") == "2026-09-01"


def test_extract_field_relative_path_resolved_against_root(tmp_path, monkeypatch):
    monkeypatch.setattr(CS, "ROOT", tmp_path)
    (tmp_path / "sub").mkdir()
    p = tmp_path / "sub" / "task.md"
    p.write_text("---\ndue: 2026-10-01\n---\n", encoding="utf-8")
    assert CS._extract_field_from_markdown("sub/task.md", "due") == "2026-10-01"


def _write_task_md(tmp_path: Path, name: str, **fields: str) -> str:
    """Write a minimal frontmatter-only markdown file, return its path string."""
    lines = ["---"]
    for k, v in fields.items():
        lines.append(f"{k}: {v}")
    lines.append("---")
    p = tmp_path / f"{name}.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def test_parse_scheduled_date_and_time_range(tmp_path, monkeypatch):
    monkeypatch.setattr(CS, "ROOT", tmp_path)
    path = _write_task_md(tmp_path, "a", scheduled='"2026-09-30 14:00~15:30"')
    assert CS._parse_scheduled(path) == ("2026-09-30", "14:00", "15:30")


def test_parse_scheduled_date_only(tmp_path, monkeypatch):
    monkeypatch.setattr(CS, "ROOT", tmp_path)
    path = _write_task_md(tmp_path, "b", scheduled='"2026-09-30"')
    assert CS._parse_scheduled(path) == ("2026-09-30", None, None)


def test_parse_scheduled_time_only_single_digit_hour(tmp_path, monkeypatch):
    monkeypatch.setattr(CS, "ROOT", tmp_path)
    path = _write_task_md(tmp_path, "c", scheduled='"9:30~10:00"')
    assert CS._parse_scheduled(path) == (None, "9:30", "10:00")


def test_parse_scheduled_dash_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(CS, "ROOT", tmp_path)
    path = _write_task_md(tmp_path, "d", scheduled='"-"')
    assert CS._parse_scheduled(path) == (None, None, None)


def test_parse_scheduled_missing_field_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(CS, "ROOT", tmp_path)
    path = _write_task_md(tmp_path, "e", title="no scheduled field")
    assert CS._parse_scheduled(path) == (None, None, None)


def test_parse_scheduled_unrecognized_format_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(CS, "ROOT", tmp_path)
    path = _write_task_md(tmp_path, "f", scheduled='"내일 오후"')
    assert CS._parse_scheduled(path) == (None, None, None)


def test_parse_due_time_extracts_leading_time(tmp_path, monkeypatch):
    monkeypatch.setattr(CS, "ROOT", tmp_path)
    path = _write_task_md(tmp_path, "a", due_time='"18:00"')
    assert CS._parse_due_time(path) == "18:00"


def test_parse_due_time_dash_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(CS, "ROOT", tmp_path)
    path = _write_task_md(tmp_path, "b", due_time='"-"')
    assert CS._parse_due_time(path) is None


def test_parse_due_time_bad_format_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(CS, "ROOT", tmp_path)
    path = _write_task_md(tmp_path, "c", due_time='"저녁"')
    assert CS._parse_due_time(path) is None


def test_parse_due_time_trailing_text_still_matches(tmp_path, monkeypatch):
    # 현행 동작 고정: re.match는 문자열 시작점만 고정하므로 뒤에 텍스트가
    # 붙어도(마감 안내 문구 등) 앞의 시각 패턴만 추출된다.
    monkeypatch.setattr(CS, "ROOT", tmp_path)
    path = _write_task_md(tmp_path, "d", due_time='"9:05 마감"')
    assert CS._parse_due_time(path) == "9:05"


KST = dt.timezone(dt.timedelta(hours=9))


def _forbid_google_calls(monkeypatch):
    def _forbidden(*a, **k):
        raise AssertionError("load_credentials/build must not be called")

    monkeypatch.setattr(CS, "load_credentials", _forbidden)
    monkeypatch.setattr(CS, "build", _forbidden)


def test_render_calendar_markdown_no_events(monkeypatch):
    _forbid_google_calls(monkeypatch)
    monkeypatch.setattr(CS, "fetch_upcoming_events", lambda *a, **k: [])

    out = CS.render_calendar_markdown("cal", Path("/nope"), Path("/nope"))

    assert "tags:" in out
    assert "  - todo" in out
    assert "  - calendar" in out
    assert "updated:" in out
    assert "> last_synced:" in out
    assert "> calendar:" in out
    assert "> range:" in out
    assert "- 예정된 일정 없음" in out
    assert out.count("###") == 0


def test_render_calendar_markdown_groups_and_labels(monkeypatch):
    _forbid_google_calls(monkeypatch)

    fixed_now = dt.datetime(2026, 9, 28, 12, 0, 0, tzinfo=KST)
    if abs((fixed_now - fixed_now.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds()) < 5:
        pytest.skip("fixed now too close to KST midnight")

    class _FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    monkeypatch.setattr(CS.dt, "datetime", _FixedDateTime)

    today = fixed_now.date()
    tomorrow = today + dt.timedelta(days=1)
    plus3 = today + dt.timedelta(days=3)

    events = [
        {"summary": "오늘 일정", "start": today.isoformat(), "end": today.isoformat(),
         "all_day": True, "location": ""},
        {"summary": "내일 일정", "start": tomorrow.isoformat(), "end": tomorrow.isoformat(),
         "all_day": True, "location": ""},
        {"summary": "+3일 일정", "start": plus3.isoformat(), "end": plus3.isoformat(),
         "all_day": True, "location": ""},
    ]
    monkeypatch.setattr(CS, "fetch_upcoming_events", lambda *a, **k: events)

    out = CS.render_calendar_markdown("cal", Path("/nope"), Path("/nope"))

    today_label = "**오늘** ("
    tomorrow_label = "**내일** ("
    plus3_label = f"{plus3.month}/{plus3.day} ("

    assert today_label in out
    assert tomorrow_label in out
    assert plus3_label in out
    assert out.index(today_label) < out.index(tomorrow_label) < out.index(plus3_label)


def test_render_calendar_markdown_event_line_formats(monkeypatch):
    _forbid_google_calls(monkeypatch)

    fixed_now = dt.datetime(2026, 9, 28, 12, 0, 0, tzinfo=KST)

    class _FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    monkeypatch.setattr(CS.dt, "datetime", _FixedDateTime)

    day = fixed_now.date().isoformat()
    events = [
        {"summary": "종일 이벤트", "start": day, "end": day,
         "all_day": True, "location": ""},
        {"summary": "시간 이벤트", "start": f"{day}T09:00:00+09:00", "end": f"{day}T10:00:00+09:00",
         "all_day": False, "location": ""},
        {"summary": "링크 이벤트", "start": f"{day}T11:00:00+09:00", "end": f"{day}T12:00:00+09:00",
         "all_day": False, "location": "https://example.com/x"},
        {"summary": "장소 이벤트", "start": f"{day}T13:00:00+09:00", "end": f"{day}T14:00:00+09:00",
         "all_day": False, "location": "회의실 A"},
    ]
    monkeypatch.setattr(CS, "fetch_upcoming_events", lambda *a, **k: events)

    out = CS.render_calendar_markdown("cal", Path("/nope"), Path("/nope"))

    assert "- 종일 | 종일 이벤트" in out
    assert "- 09:00~10:00 | 시간 이벤트" in out
    assert "@ [링크](https://example.com/x)" in out
    assert "@ 회의실 A" in out
    for line in out.splitlines():
        if "종일 이벤트" in line:
            assert "@" not in line


def _fix_now(monkeypatch, fixed_now: dt.datetime) -> None:
    class _FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    monkeypatch.setattr(CS.dt, "datetime", _FixedDateTime)


def test_render_unified_injects_events_skips_fetch(tmp_path, monkeypatch):
    _forbid_google_calls(monkeypatch)
    monkeypatch.setattr(
        CS,
        "fetch_upcoming_events",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("fetch_upcoming_events must not be called when events= given")
        ),
    )
    monkeypatch.setattr(CS, "ROOT", tmp_path)

    fixed_now = dt.datetime(2026, 9, 28, 12, 0, 0, tzinfo=KST)
    _fix_now(monkeypatch, fixed_now)
    today = fixed_now.date().isoformat()

    con = _full_db([{"slug": "injected", "title": "주입된 태스크", "due": today}])
    events = [{"summary": "회의", "start": f"{today}T10:00:00", "end": f"{today}T11:00:00",
               "all_day": False, "location": None}]

    out = CS.render_unified_markdown("cal", Path("/nope"), Path("/nope"), con, events=events)

    assert "회의" in out
    assert "주입된 태스크" in out


def test_render_unified_priority_icon_thresholds(tmp_path, monkeypatch):
    _forbid_google_calls(monkeypatch)
    monkeypatch.setattr(CS, "ROOT", tmp_path)
    fixed_now = dt.datetime(2026, 9, 28, 12, 0, 0, tzinfo=KST)
    _fix_now(monkeypatch, fixed_now)
    today = fixed_now.date()

    rows = [
        {"slug": "red", "title": "Red", "priority": 90, "due": (today + dt.timedelta(days=1)).isoformat()},
        {"slug": "yellow", "title": "Yellow", "priority": 70, "due": (today + dt.timedelta(days=2)).isoformat()},
        {"slug": "purple", "title": "Purple", "priority": 50, "due": (today + dt.timedelta(days=3)).isoformat()},
        {"slug": "green", "title": "Green", "priority": 20, "due": (today + dt.timedelta(days=4)).isoformat()},
    ]
    con = _full_db(rows)

    out = CS.render_unified_markdown("cal", Path("/nope"), Path("/nope"), con, events=[])

    assert "🔴" in out
    assert "🟡" in out
    assert "🟣" in out
    assert "🟢" in out


def test_render_unified_overdue_vs_undated(tmp_path, monkeypatch):
    _forbid_google_calls(monkeypatch)
    monkeypatch.setattr(CS, "ROOT", tmp_path)
    fixed_now = dt.datetime(2026, 9, 28, 12, 0, 0, tzinfo=KST)
    _fix_now(monkeypatch, fixed_now)
    yesterday = (fixed_now.date() - dt.timedelta(days=1)).isoformat()

    rows = [
        {"slug": "late", "title": "Late Task", "due": yesterday},
        {"slug": "someday", "title": "Someday Task", "due": None},
    ]
    con = _full_db(rows)

    out = CS.render_unified_markdown("cal", Path("/nope"), Path("/nope"), con, events=[])

    assert "### 🚨 Overdue" in out
    overdue_block = out.split("### 🚨 Overdue", 1)[1].split("###", 1)[0]
    assert "Late Task" in overdue_block

    # Undated tasks are never placed in a date section nor the overdue block,
    # but they still show up in the overall priority view (not filtered out).
    assert "Someday Task" not in overdue_block
    assert "### 🔥 전체 우선순위 뷰" in out
    priority_view_block = out.split("### 🔥 전체 우선순위 뷰", 1)[1]
    assert "Someday Task" in priority_view_block


def test_render_unified_composite_score_orders_priority_view(tmp_path, monkeypatch):
    _forbid_google_calls(monkeypatch)
    monkeypatch.setattr(CS, "ROOT", tmp_path)
    fixed_now = dt.datetime(2026, 9, 28, 12, 0, 0, tzinfo=KST)
    _fix_now(monkeypatch, fixed_now)
    today = fixed_now.date()

    rows = [
        {"slug": "far", "title": "Far Due", "priority": 50, "due": (today + dt.timedelta(days=20)).isoformat()},
        {"slug": "near", "title": "Near Due", "priority": 50, "due": (today + dt.timedelta(days=1)).isoformat()},
    ]
    con = _full_db(rows)

    out = CS.render_unified_markdown("cal", Path("/nope"), Path("/nope"), con, events=[])

    priority_view_block = out.split("### 🔥 전체 우선순위 뷰", 1)[1]
    assert priority_view_block.index("Near Due") < priority_view_block.index("Far Due")


def test_render_unified_parent_link_and_is_parent_excluded(tmp_path, monkeypatch):
    _forbid_google_calls(monkeypatch)
    monkeypatch.setattr(CS, "ROOT", tmp_path)
    fixed_now = dt.datetime(2026, 9, 28, 12, 0, 0, tzinfo=KST)
    _fix_now(monkeypatch, fixed_now)
    today = fixed_now.date().isoformat()

    child_path = _write_task_md(tmp_path, "child", parent="[[parent-slug]]", due=f'"{today}"')
    parent_path = _write_task_md(tmp_path, "parent", is_parent="true", due=f'"{today}"')

    rows = [
        {"slug": "child", "title": "Child Task", "path": child_path, "due": today},
        {"slug": "parent-slug", "title": "Parent Task", "path": parent_path, "due": today},
    ]
    con = _full_db(rows)

    out = CS.render_unified_markdown("cal", Path("/nope"), Path("/nope"), con, events=[])

    assert "↑[[Todo/items/parent-slug|parent-slug]]" in out
    priority_view_block = out.split("### 🔥 전체 우선순위 뷰", 1)[1]
    assert "Parent Task" not in priority_view_block


def test_render_unified_calendar_and_todo_share_date_section(tmp_path, monkeypatch):
    _forbid_google_calls(monkeypatch)
    monkeypatch.setattr(CS, "ROOT", tmp_path)
    fixed_now = dt.datetime(2026, 9, 28, 12, 0, 0, tzinfo=KST)
    _fix_now(monkeypatch, fixed_now)
    target_day = (fixed_now.date() + dt.timedelta(days=3)).isoformat()

    con = _full_db([{"slug": "shared-day", "title": "공유된 날짜 태스크", "due": target_day}])
    events = [{"summary": "회의", "start": f"{target_day}T10:00:00", "end": f"{target_day}T11:00:00",
               "all_day": False, "location": None}]

    out = CS.render_unified_markdown("cal", Path("/nope"), Path("/nope"), con, events=events)

    d = dt.date.fromisoformat(target_day)
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    section_header = f"### {d.month}/{d.day} ({weekdays[d.weekday()]})"
    assert section_header in out
    section_block = out.split(section_header, 1)[1].split("###", 1)[0]
    assert "- 📅" in section_block and "회의" in section_block
    assert "공유된 날짜 태스크" in section_block


# --- AC2: the event timezone is configuration, not a hard-coded Asia/Seoul ----
# `_event_timezone()` is `paths.timezone()`: MACP_TIMEZONE → the `timezone` key
# of .macp/config.toml → the host's own IANA zone → None. None means the body
# omits `timeZone` entirely and Google applies the calendar's own zone.
#
# TRAP these cases are written around: the gate runs with
# MACP_VAULT_ROOT=vault-template, and the host's /etc/localtime usually answers,
# so clearing MACP_TIMEZONE is NOT enough to reach the None branch — the
# "no zone" cases patch the resolver itself.


def _upsert(events, **kw):
    """`_upsert_event` with everything but the timing fields pinned."""
    fields = dict(scheduled_date=None, scheduled_start=None, scheduled_end=None,
                  due_time=None)
    fields.update(kw)
    CS._upsert_event(_FakeService(events), "cal", "s", "title", "2026-08-10",
                     next_action="", source_path="x.md", **fields)
    return events.inserted[0]


def _no_timezone_configured(monkeypatch):
    monkeypatch.setattr(CS.paths, "timezone", lambda *a, **k: None)


def test_event_timezone_comes_from_the_env(monkeypatch):
    monkeypatch.setenv("MACP_TIMEZONE", "Europe/Berlin")
    body = _upsert(_FakeEvents(), scheduled_start="09:00", scheduled_end="10:00")
    assert body["start"]["timeZone"] == "Europe/Berlin"
    assert body["end"]["timeZone"] == "Europe/Berlin"
    # The dateTime half is untouched by the zone knob.
    assert body["start"]["dateTime"].startswith("2026-08-10T09:00")


def test_event_timezone_comes_from_the_config_key(monkeypatch, tmp_path):
    """The `timezone` key of the vault's .macp/config.toml, read via paths."""
    (tmp_path / ".macp").mkdir()
    (tmp_path / ".macp" / "config.toml").write_text(
        'timezone = "Europe/Berlin"\n', encoding="utf-8")
    monkeypatch.delenv("MACP_TIMEZONE", raising=False)
    monkeypatch.setenv("MACP_VAULT_ROOT", str(tmp_path))
    body = _upsert(_FakeEvents(), scheduled_start="09:00", scheduled_end="10:00")
    assert body["start"]["timeZone"] == "Europe/Berlin"


def test_due_time_event_also_takes_the_configured_timezone(monkeypatch):
    monkeypatch.setenv("MACP_TIMEZONE", "Europe/Berlin")
    body = _upsert(_FakeEvents(), due_time="14:30")
    assert body["start"]["timeZone"] == "Europe/Berlin"
    assert body["end"]["timeZone"] == "Europe/Berlin"
    assert body["start"]["dateTime"].startswith("2026-08-10T14:30")


def test_no_configured_timezone_omits_the_key_entirely(monkeypatch):
    """"No key → host local" is observed as the ABSENCE of `timeZone`: Google
    then applies the calendar's own timezone. Never a guessed region."""
    _no_timezone_configured(monkeypatch)
    body = _upsert(_FakeEvents(), scheduled_start="09:00", scheduled_end="10:00")
    assert "timeZone" not in body["start"]
    assert "timeZone" not in body["end"]
    assert body["start"]["dateTime"].startswith("2026-08-10T09:00")


def test_no_configured_timezone_omits_the_key_on_the_due_time_path(monkeypatch):
    _no_timezone_configured(monkeypatch)
    body = _upsert(_FakeEvents(), due_time="14:30")
    assert "timeZone" not in body["start"]
    assert "timeZone" not in body["end"]


def test_all_day_event_never_carried_a_timezone(monkeypatch):
    """Invariance: the all-day branch uses `date`, not `dateTime`, and the zone
    knob must not start adding a key there."""
    monkeypatch.setenv("MACP_TIMEZONE", "Europe/Berlin")
    body = _upsert(_FakeEvents())
    assert body["start"] == {"date": "2026-08-10"}
    assert body["end"] == {"date": "2026-08-11"}


def test_event_tzinfo_uses_the_configured_zone(monkeypatch):
    monkeypatch.setenv("MACP_TIMEZONE", "Europe/Berlin")
    offset = dt.datetime(2026, 1, 15, tzinfo=CS._event_tzinfo()).utcoffset()
    assert offset == dt.timedelta(hours=1)        # CET in January


def test_event_tzinfo_falls_back_to_the_host_zone_on_an_unknown_name(monkeypatch):
    """A bad name must not raise inside a daemon tick; the clock arithmetic
    call sites need an aware datetime and have no "say nothing" option."""
    monkeypatch.setenv("MACP_TIMEZONE", "Not/AZone")
    assert CS._event_tzinfo() == dt.datetime.now().astimezone().tzinfo


class _RecordingEvents(_FakeEvents):
    def __init__(self):
        super().__init__()
        self.list_kw: list[dict] = []

    def list(self, **kw):
        self.list_kw.append(kw)
        return super().list(**kw)


def _fetch(monkeypatch, events):
    monkeypatch.setattr(CS, "load_credentials", lambda *a, **k: object())
    monkeypatch.setattr(CS, "build", lambda *a, **k: _FakeService(events))
    return CS.fetch_upcoming_events("cal", Path("/nope"), Path("/nope"))


def test_fetch_passes_the_configured_timezone_to_the_api(monkeypatch):
    monkeypatch.setenv("MACP_TIMEZONE", "Europe/Berlin")
    events = _RecordingEvents()
    _fetch(monkeypatch, events)
    assert events.list_kw[0]["timeZone"] == "Europe/Berlin"


def test_fetch_omits_the_timezone_argument_when_nothing_is_configured(monkeypatch):
    _no_timezone_configured(monkeypatch)
    events = _RecordingEvents()
    _fetch(monkeypatch, events)
    assert "timeZone" not in events.list_kw[0]


if __name__ == "__main__":
    unittest.main()
