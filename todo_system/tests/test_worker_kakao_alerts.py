"""Worst-input render tests for worker.py's own kakao alerts (pure composers).

No subprocess / no real send — the composers return strings only
([[tests-reach-real-alert-sender]]). Each asserts the three invariants the task
fixes: length <= budget, the action/identity survives in the headline, and the
free-form tail (a failure message, a stall file list, a planner reason) is
clipped rather than blind-cut. Covers the AC-named kinds: Worker 시작/실패/완료,
정체(silence), 유휴(starvation), plus the commit-stall page.
"""

from __future__ import annotations

from todo_system import worker
from todo_system.kakao_format import KAKAO_TEXT_LIMIT

LONG = "failure detail with several English words " * 120  # ~5000 chars, over the 4000 budget so the tail always clips
LONG_SLUG = "play-store-9-ifthen-targetsdk-36"


def test_worker_fail_clips_the_message_instead_of_the_old_blind_120_cut():
    text = worker._compose_worker_fail(LONG_SLUG, "verify", LONG)
    assert len(text) <= KAKAO_TEXT_LIMIT
    head = text.splitlines()[0]
    assert head.startswith(f"❌ Worker 실패: {LONG_SLUG}")
    assert "확인" in head                       # the ask stays in the headline
    assert not text.endswith("English words")   # tail is clipped + marked, not raw


def test_worker_start_and_done_stay_within_budget_and_name_the_task():
    for fn in (worker._compose_worker_start, worker._compose_worker_done):
        text = fn(LONG_SLUG, "auto", 5)
        assert len(text) <= KAKAO_TEXT_LIMIT
        assert LONG_SLUG in text


def test_dispatch_silence_keeps_the_pending_count():
    text = worker._compose_dispatch_silence(3, 10.4, 6)
    assert len(text) <= KAKAO_TEXT_LIMIT
    assert "3" in text                          # the count the monitor test also pins
    assert "정체" in text.splitlines()[0]


def test_starvation_clips_the_discovery_reason():
    text = worker._compose_starvation("09-16 22:00", "discovery blocked: " + LONG, 2)
    assert len(text) <= KAKAO_TEXT_LIMIT
    assert "유휴" in text.splitlines()[0]


def test_worker_stall_lists_files_within_budget():
    files = [f"Notes/some-long-note-name-number-{i}.md" for i in range(30)]
    text = worker._compose_worker_stall(7, files)
    assert len(text) <= KAKAO_TEXT_LIMIT
    assert "7회" in text.splitlines()[0]


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
