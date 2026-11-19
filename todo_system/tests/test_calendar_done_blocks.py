"""Done human blocks stay on the calendar, trimmed to the completion time (2026-11-19)."""
from __future__ import annotations

import datetime as dt

import pytest

from todo_system import calendar_sync as CS

D = dt.datetime


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
