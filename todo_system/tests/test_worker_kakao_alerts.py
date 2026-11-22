"""Worst-input render tests for worker.py's own kakao alerts (pure composers).

No subprocess / no real send — the composers return strings only
([[tests-reach-real-alert-sender]]). Each asserts the three invariants the task
fixes: length <= budget, the action/identity survives in the headline, and the
free-form tail (a planner reason) is
clipped rather than blind-cut. Covers the AC-named kinds:
정체(silence), 유휴(starvation).
"""

from __future__ import annotations

from todo_system import worker
from todo_system.kakao_format import KAKAO_TEXT_LIMIT

LONG = "failure detail with several English words " * 120  # ~5000 chars, over the 4000 budget so the tail always clips
LONG_SLUG = "play-store-9-ifthen-targetsdk-36"


def test_dispatch_silence_keeps_the_pending_count():
    text = worker._compose_dispatch_silence(3, 10.4, 6)
    assert len(text) <= KAKAO_TEXT_LIMIT
    assert "3" in text                          # the count the monitor test also pins
    assert "정체" in text.splitlines()[0]


def test_starvation_clips_the_discovery_reason():
    text = worker._compose_starvation("09-16 22:00", "discovery blocked: " + LONG, 2)
    assert len(text) <= KAKAO_TEXT_LIMIT
    assert "유휴" in text.splitlines()[0]


def test_daemon_restart_escalated_names_the_label_and_attempts():
    text = worker._compose_daemon_restart_escalated(LONG_SLUG, 3)
    assert len(text) <= KAKAO_TEXT_LIMIT
    assert LONG_SLUG in text
    assert "3회" in text
    assert "재기동 실패" in text.splitlines()[0]


def test_daemon_restart_deferred_names_the_label_hours_and_count():
    text = worker._compose_daemon_restart_deferred(LONG_SLUG, 7200.0, 12)
    assert len(text) <= KAKAO_TEXT_LIMIT
    assert LONG_SLUG in text
    assert "2시간" in text
    assert "12회" in text
    assert "재기동 보류" in text.splitlines()[0]
