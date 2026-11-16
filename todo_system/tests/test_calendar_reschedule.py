"""Overdue human time blocks move to the next free slot (2026-11-16)."""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

from todo_system import calendar_sync as CS

D = dt.datetime


def test_free_slot_skips_busy_and_closed_hours():
    busy = [(D(2026, 11, 16, 10, 0), D(2026, 11, 16, 11, 0))]
    s, e = CS.find_free_slot(dt.timedelta(minutes=30), busy, D(2026, 11, 16, 9, 7))
    assert (s, e) == (D(2026, 11, 16, 11, 0), D(2026, 11, 16, 11, 30))


def test_free_slot_rolls_to_next_weekday_after_hours():
    # Friday 17:50 → a 30m block no longer fits → Monday 10:00
    s, _ = CS.find_free_slot(dt.timedelta(minutes=30), [], D(2026, 11, 20, 17, 50))
    assert s == D(2026, 11, 23, 10, 0)


def test_free_slot_weekends_allowed_when_asked():
    s, _ = CS.find_free_slot(dt.timedelta(minutes=30), [], D(2026, 11, 20, 17, 50),
                             weekdays=set(range(7)))
    assert s == D(2026, 11, 21, 10, 0)


def test_plan_moves_only_overdue_and_keeps_length_and_order():
    now = D(2026, 11, 16, 13, 20)
    blocks = [
        {"slug": "low", "start": D(2026, 11, 16, 10, 0), "end": D(2026, 11, 16, 10, 30), "priority": 40},
        {"slug": "high", "start": D(2026, 11, 15, 14, 0), "end": D(2026, 11, 15, 14, 45), "priority": 80},
        {"slug": "grace", "start": D(2026, 11, 16, 12, 30), "end": D(2026, 11, 16, 13, 0)},  # ended 20m ago
        {"slug": "opt", "start": D(2026, 11, 16, 9, 0), "end": D(2026, 11, 16, 9, 30), "opt_out": True},
    ]
    busy = [(D(2026, 11, 16, 13, 30), D(2026, 11, 16, 14, 0))]  # another task's future block
    moves = CS.plan_overdue_moves(blocks, busy, now)
    assert moves == [
        ("high", "2026-11-15 14:00~14:45", "2026-11-16 14:00~14:45"),
        ("low", "2026-11-16 10:00~10:30", "2026-11-16 14:45~15:15"),
    ]


def test_not_before_is_respected():
    now = D(2026, 11, 16, 13, 20)
    blocks = [{"slug": "a", "start": D(2026, 11, 13, 10, 0), "end": D(2026, 11, 13, 11, 0),
               "not_before": D(2026, 11, 18, 0, 0)}]
    assert CS.plan_overdue_moves(blocks, [], now)[0][2] == "2026-11-18 10:00~11:00"


def _md(d: Path, slug: str, sched: str, extra: str = "") -> str:
    p = d / f"{slug}.md"
    p.write_text(f'---\nstatus: open\nscheduled: "{sched}"\n{extra}---\n# {slug}\n\n## Progress Log\n\n- x\n',
                 encoding="utf-8")
    return str(p)


class _Svc:
    def freebusy(self):
        class Q:
            def query(self, body):
                class R:
                    def execute(self_inner):
                        return {"calendars": {"cal": {"busy": [
                            {"start": "2026-11-16T14:00:00", "end": "2026-11-16T15:00:00"}]}}}
                return R()
        return Q()


def test_reschedule_writes_md_and_log(tmp_path, monkeypatch):
    monkeypatch.setattr(CS, "_event_tzinfo", lambda: None)
    monkeypatch.setattr(CS, "_event_timezone", lambda: None)
    overdue = _md(tmp_path, "late", "2026-11-16 10:00~10:30")
    future = _md(tmp_path, "soon", "2026-11-16 15:00~15:30")
    stays = _md(tmp_path, "pinned", "2026-11-16 09:00~09:30", "reschedule: false\n")
    con = sqlite3.connect(":memory:"); con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE t (slug TEXT, path TEXT)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [("late", overdue), ("soon", future), ("pinned", stays)])
    rows = con.execute("SELECT * FROM t").fetchall()
    moves = CS.reschedule_overdue_blocks(_Svc(), rows, ["cal"], now=D(2026, 11, 16, 13, 20))
    # 13:30 is free; freebusy blocks 14–15; "soon" holds 15:00–15:30
    assert moves == [("late", "2026-11-16 10:00~10:30", "2026-11-16 13:30~14:00")]
    text = Path(overdue).read_text(encoding="utf-8")
    assert 'scheduled: "2026-11-16 13:30~14:00"' in text
    assert "자동 재배치" in text
    assert 'scheduled: "2026-11-16 09:00~09:30"' in Path(stays).read_text(encoding="utf-8")


def test_host_switch_disables(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_RESCHEDULE_OVERDUE", "0")
    p = _md(tmp_path, "late", "2026-11-10 10:00~10:30")
    assert CS.reschedule_overdue_blocks(_Svc(), [{"slug": "late", "path": p}], ["cal"],
                                        now=D(2026, 11, 16, 13, 20)) == []


def test_sync_path_moves_an_overdue_block(tmp_path, monkeypatch):
    """Wiring: sync_due_tasks_to_calendar itself calls the mover after upserts."""
    from todo_system.tests.test_calendar_sync import _FakeEvents, _FakeService, _db

    monkeypatch.setattr(CS, "_event_tzinfo", lambda: None)
    monkeypatch.setattr(CS, "_event_timezone", lambda: None)
    p = _md(tmp_path, "late", "2020-01-06 10:00~10:30")   # long past
    con = _db([("late", "late task", "2020-01-06", "", p, "open", "manual")])
    events = _FakeEvents()
    monkeypatch.setattr(CS, "load_credentials", lambda *a, **k: object())
    monkeypatch.setattr(CS, "build", lambda *a, **k: _FakeService(events))
    CS.sync_due_tasks_to_calendar(con=con, calendar_id="cal",
                                  client_secret_path=Path("/nope"), token_path=Path("/nope"))
    text = Path(p).read_text(encoding="utf-8")
    assert '"2020-01-06 10:00~10:30"' not in text.split("## Progress Log")[0]
    assert "자동 재배치" in text
