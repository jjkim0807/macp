"""Done human blocks stay on the calendar, trimmed to the completion time (2026-11-19)."""
from __future__ import annotations

import copy
import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from todo_system import calendar_sync as CS

D = dt.datetime
PINNED_NOW = D(2026, 11, 19, 16, 53)


# --- a Google-like stateful fake ------------------------------------------------

def _merge(dst: dict, patch: dict) -> None:
    """Google's PATCH semantics: nested maps merge key by key, an explicit
    null deletes the key, anything else replaces."""
    for k, v in patch.items():
        if v is None:
            dst.pop(k, None)
        elif isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)


class _Req:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _GEvents:
    def __init__(self, existing=None):
        self.existing: list[dict] = [copy.deepcopy(e) for e in existing or []]
        self.inserted: list[dict] = []
        self.patched: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self._n = 0

    def list(self, **kw):
        prop = kw.get("privateExtendedProperty")
        if prop:
            k, v = prop.split("=", 1)
            items = [e for e in self.existing
                     if ((e.get("extendedProperties") or {}).get("private") or {}).get(k) == v]
        else:
            items = list(self.existing)
        return _Req({"items": [copy.deepcopy(e) for e in items]})

    def insert(self, calendarId, body):
        self._n += 1
        ev = copy.deepcopy(body)
        ev["id"] = f"new{self._n}"
        self.existing.append(ev)
        self.inserted.append(copy.deepcopy(body))
        return _Req(ev)

    def patch(self, calendarId, eventId, body):
        ev = self.get(eventId)
        _merge(ev, body)
        self.patched.append((eventId, copy.deepcopy(body)))
        return _Req(ev)

    def delete(self, calendarId, eventId):
        self.existing = [e for e in self.existing if e["id"] != eventId]
        self.deleted.append(eventId)
        return _Req({})

    def get(self, event_id):
        return next(e for e in self.existing if e["id"] == event_id)

    def writes(self) -> int:
        return len(self.inserted) + len(self.patched) + len(self.deleted)


class _GService:
    def __init__(self, events):
        self._events = events

    def events(self):
        return self._events


def _priv(ev: dict) -> dict:
    return (ev.get("extendedProperties") or {}).get("private") or {}


def _live_event(eid: str, slug: str, start: str, end: str, title: str = "work") -> dict:
    return {"id": eid, "summary": title,
            "start": {"dateTime": start, "timeZone": "Asia/Seoul"},
            "end": {"dateTime": end, "timeZone": "Asia/Seoul"},
            "extendedProperties": {"private": {"todo_slug": slug, "todo_source": f"{slug}.md"}}}


@pytest.fixture
def pinned(monkeypatch):
    monkeypatch.setattr(CS, "_now_local", lambda: PINNED_NOW)
    monkeypatch.setattr(CS, "_event_timezone", lambda: "Asia/Seoul")
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(CS, "_event_tzinfo", lambda: ZoneInfo("Asia/Seoul"))
    monkeypatch.setenv("MACP_RESCHEDULE_OVERDUE", "0")
    monkeypatch.delenv("MACP_CALENDAR_DONE_KEEP_DAYS", raising=False)


def _task_md(d: Path, slug: str, *, status: str = "done",
             scheduled: str = "2026-11-19 10:00~13:00", completed: str | None = None,
             extra: str = "") -> str:
    lines = ["---", f"status: {status}", f'scheduled: "{scheduled}"']
    if completed:
        lines.append(f'completed: "{completed}"')
    if extra:
        lines.append(extra)
    lines += ["---", f"# {slug}", ""]
    p = d / f"{slug}.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return str(p)


def _db(rows):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE tasks (slug TEXT, title TEXT, due TEXT, next_action TEXT, "
                "path TEXT, status TEXT, worker_mode TEXT)")
    con.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    return con


def _run_sync(con, events, monkeypatch):
    monkeypatch.setattr(CS, "load_credentials", lambda *a, **k: object())
    monkeypatch.setattr(CS, "build", lambda *a, **k: _GService(events))
    return CS.sync_due_tasks_to_calendar(con=con, calendar_id="cal",
                                         client_secret_path=Path("/nope"),
                                         token_path=Path("/nope"))


# --- done_block_window: the three rules --------------------------------------

@pytest.mark.parametrize(
    "block, completed, expected",
    [
        # 1. inside the block → end = completion
        (("2026-11-19", "10:00", "13:00"), D(2026, 11, 19, 11, 20),
         (D(2026, 11, 19, 10, 0), D(2026, 11, 19, 11, 20))),
        # c == end counts as inside
        (("2026-11-19", "10:00", "13:00"), D(2026, 11, 19, 13, 0),
         (D(2026, 11, 19, 10, 0), D(2026, 11, 19, 13, 0))),
        # inside but < 15m after the start → keeps 15m
        (("2026-11-19", "10:00", "13:00"), D(2026, 11, 19, 10, 5),
         (D(2026, 11, 19, 10, 0), D(2026, 11, 19, 10, 15))),
        # 2. after the block, same day → extended to completion
        (("2026-11-19", "10:00", "13:00"), D(2026, 11, 19, 14, 10),
         (D(2026, 11, 19, 10, 0), D(2026, 11, 19, 14, 10))),
        # 3. before the start, same day → same length, ends at completion
        (("2026-11-19", "10:00", "11:00"), D(2026, 11, 19, 9, 0),
         (D(2026, 11, 19, 8, 0), D(2026, 11, 19, 9, 0))),
        # 3. a different day → same length, ends at completion
        (("2026-11-18", "10:00", "15:00"), D(2026, 11, 19, 16, 53),
         (D(2026, 11, 19, 11, 53), D(2026, 11, 19, 16, 53))),
        # 3. a 10-minute block that is moved keeps 15m
        (("2026-11-18", "10:00", "10:10"), D(2026, 11, 19, 16, 0),
         (D(2026, 11, 19, 15, 45), D(2026, 11, 19, 16, 0))),
        # midnight-spanning block, completed after midnight inside it
        (("2026-11-18", "23:00", "01:00"), D(2026, 11, 19, 0, 30),
         (D(2026, 11, 18, 23, 0), D(2026, 11, 19, 0, 30))),
    ],
)
def test_done_block_window_rules(block, completed, expected):
    s, e = CS._block_bounds(*block)
    assert CS.done_block_window(s, e, completed) == expected


# --- small helpers -------------------------------------------------------------

def test_done_title_is_idempotent():
    assert CS._done_title("x") == "✅ x"
    assert CS._done_title("✅ x") == "✅ x"
    assert CS._done_title("✅ ✅ x") == "✅ x"
    assert CS._done_title("✅x") == "✅ x"


def test_done_keep_days(monkeypatch):
    monkeypatch.delenv("MACP_CALENDAR_DONE_KEEP_DAYS", raising=False)
    assert CS._done_keep_days() == 30
    monkeypatch.setenv("MACP_CALENDAR_DONE_KEEP_DAYS", "7")
    assert CS._done_keep_days() == 7
    for junk in ("abc", "0", "-3", ""):
        monkeypatch.setenv("MACP_CALENDAR_DONE_KEEP_DAYS", junk)
        assert CS._done_keep_days() == 30


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2026-11-19 16:53", D(2026, 11, 19, 16, 53)),
        ("2026-11-19T16:53", D(2026, 11, 19, 16, 53)),
        ("2026-11-19 9:05", D(2026, 11, 19, 9, 5)),
        ('"2026-11-19 16:53"', D(2026, 11, 19, 16, 53)),
        ("", None),
        (None, None),
        ("-", None),
        ("2026-11-19", None),
        ("soon", None),
        ("2026-13-40 10:00", None),
    ],
)
def test_parse_completed(raw, expected):
    assert CS._parse_completed(raw) == expected


def test_done_event_expired_uses_macp_completed_then_end(monkeypatch):
    monkeypatch.setattr(CS, "_event_tzinfo", lambda: dt.timezone(dt.timedelta(hours=9)))
    now = D(2026, 12, 30, 12, 0)
    with_stamp = {"extendedProperties": {"private": {"macp_completed": "2026-11-19 16:53"}},
                  "end": {"dateTime": "2026-12-29T10:00:00+09:00"}}
    assert CS._done_event_expired(with_stamp, now, 30) is True      # 40 days by the stamp
    assert CS._done_event_expired(with_stamp, now, 60) is False

    # Hand-detached event: no macp_completed → measured from its end.
    old = {"extendedProperties": {"private": {"todo_done_slug": "x"}},
           "end": {"dateTime": "2026-11-19T07:53:00Z"}}             # 16:53 at +09:00
    assert CS._done_event_expired(old, now, 30) is True
    assert CS._done_event_expired(old, D(2026, 12, 19, 16, 52), 30) is False
    recent = {"extendedProperties": {"private": {"todo_done_slug": "x"}},
              "end": {"dateTime": "2026-12-20T10:00:00", "timeZone": "Asia/Seoul"}}
    assert CS._done_event_expired(recent, now, 30) is False
    all_day = {"end": {"date": "2026-11-01"}}
    assert CS._done_event_expired(all_day, now, 30) is True
    assert CS._done_event_expired({"end": {}}, now, 30) is False


# --- sync_due_tasks_to_calendar with done rows ----------------------------------

def _seoul(t: D) -> str:
    return CS._canon({"dateTime": t.isoformat(), "timeZone": "Asia/Seoul"})


def test_just_done_event_is_kept_and_trimmed(tmp_path, pinned, monkeypatch):
    p = _task_md(tmp_path, "t", completed="2026-11-19 11:20")
    con = _db([("t", "work", None, "", p, "done", None)])
    events = _GEvents([_live_event("e1", "t", "2026-11-19T10:00:00", "2026-11-19T13:00:00")])
    _run_sync(con, events, monkeypatch)

    assert "e1" not in events.deleted
    assert events.inserted == []
    ev = events.get("e1")
    assert CS._canon(ev["start"]) == _seoul(D(2026, 11, 19, 10, 0))
    assert CS._canon(ev["end"]) == _seoul(D(2026, 11, 19, 11, 20))
    assert ev["summary"] == "✅ work"
    assert "todo_slug" not in _priv(ev)
    assert _priv(ev)["todo_done_slug"] == "t"
    assert _priv(ev)["macp_completed"] == "2026-11-19 11:20"

    # Idempotent: a second sync writes nothing.
    before = events.writes()
    _run_sync(con, events, monkeypatch)
    assert events.writes() == before


def test_hand_detached_event_is_reused_not_duplicated(tmp_path, pinned, monkeypatch):
    p = _task_md(tmp_path, "t", completed="2026-11-19 14:10")
    con = _db([("t", "work", None, "", p, "done", None)])
    manual = _live_event("m1", "t", "2026-11-19T10:00:00", "2026-11-19T13:00:00")
    manual["extendedProperties"]["private"] = {"todo_done_slug": "t", "todo_source": "t.md"}
    events = _GEvents([manual])
    _run_sync(con, events, monkeypatch)

    assert events.inserted == [] and events.deleted == []
    assert [eid for eid, _ in events.patched] == ["m1"]
    ev = events.get("m1")
    assert CS._canon(ev["end"]) == _seoul(D(2026, 11, 19, 14, 10))      # ran over → extended
    assert ev["summary"] == "✅ work"
    assert _priv(ev)["macp_completed"] == "2026-11-19 14:10"


def test_title_already_marked_keeps_one_mark(tmp_path, pinned, monkeypatch):
    p = _task_md(tmp_path, "t", completed="2026-11-19 11:20")
    con = _db([("t", "✅ work", None, "", p, "done", None)])
    events = _GEvents([_live_event("e1", "t", "2026-11-19T10:00:00", "2026-11-19T13:00:00")])
    _run_sync(con, events, monkeypatch)
    assert events.get("e1")["summary"] == "✅ work"


def test_done_events_are_cleaned_after_keep_days(tmp_path, pinned, monkeypatch):
    p = _task_md(tmp_path, "old", completed="2026-10-01 10:00")              # 49 days ago
    con = _db([("old", "old work", None, "", p, "done", None)])
    stamped = _live_event("d1", "old", "2026-10-01T09:00:00", "2026-10-01T10:00:00")
    stamped["extendedProperties"]["private"] = {"todo_done_slug": "old",
                                                "macp_completed": "2026-10-01 10:00"}
    stale_manual = _live_event("m-old", "x", "2026-10-10T10:00:00", "2026-10-10T11:00:00")
    stale_manual["extendedProperties"]["private"] = {"todo_done_slug": "gone"}
    fresh_manual = _live_event("m-new", "x", "2026-11-18T10:00:00", "2026-11-18T11:00:00")
    fresh_manual["extendedProperties"]["private"] = {"todo_done_slug": "recent"}
    untagged = {"id": "own", "summary": "lunch", "start": {"dateTime": "2020-01-01T12:00:00"},
                "end": {"dateTime": "2020-01-01T13:00:00"}}
    events = _GEvents([stamped, stale_manual, fresh_manual, untagged])
    _run_sync(con, events, monkeypatch)

    assert sorted(events.deleted) == ["d1", "m-old"]
    assert events.inserted == [] and events.patched == []

    # A shorter window via env also counts.
    monkeypatch.setenv("MACP_CALENDAR_DONE_KEEP_DAYS", "1")
    _run_sync(con, events, monkeypatch)
    assert "m-new" in events.deleted
    assert "own" not in events.deleted


def test_done_without_completed_keeps_old_behaviour(tmp_path, pinned, monkeypatch):
    p = _task_md(tmp_path, "t")                                              # no completed:
    con = _db([("t", "work", None, "", p, "done", None)])
    events = _GEvents([_live_event("e1", "t", "2026-11-19T10:00:00", "2026-11-19T13:00:00")])
    _run_sync(con, events, monkeypatch)
    assert events.deleted == ["e1"]
    assert events.inserted == [] and events.patched == []


def test_worker_owned_done_task_is_not_finalized(tmp_path, pinned, monkeypatch):
    p = _task_md(tmp_path, "bot", completed="2026-11-19 11:20")
    con = _db([("bot", "worker", None, "", p, "done", "auto")])
    events = _GEvents([_live_event("e1", "bot", "2026-11-19T10:00:00", "2026-11-19T13:00:00")])
    _run_sync(con, events, monkeypatch)
    assert events.deleted == ["e1"]
    assert events.inserted == [] and events.patched == []


def test_done_task_without_event_gets_a_done_event(tmp_path, pinned, monkeypatch):
    p = _task_md(tmp_path, "t", scheduled="2026-11-18 10:00~15:00", completed="2026-11-19 16:53")
    con = _db([("t", "work", None, "", p, "done", "manual")])
    events = _GEvents()
    created, _ = _run_sync(con, events, monkeypatch)
    assert created == 1
    (body,) = events.inserted
    assert "todo_slug" not in _priv(body)
    assert _priv(body)["todo_done_slug"] == "t"
    assert body["summary"] == "✅ work"
    # Different day → moved, same 5h length, ends at completion.
    assert CS._canon(body["start"]) == _seoul(D(2026, 11, 19, 11, 53))
    assert CS._canon(body["end"]) == _seoul(D(2026, 11, 19, 16, 53))
