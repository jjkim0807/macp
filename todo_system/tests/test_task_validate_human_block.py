"""Human tasks carry a timed block sized from estimate_p50 (2026-11-14)."""
from __future__ import annotations

from todo_system import task_validate as TV


def _fields(fm):
    return [i.field for i in TV.validate_frontmatter(fm)]


def test_dated_manual_task_without_a_block_warns():
    fm = {"status": "open", "worker_mode": "manual", "due": "2026-11-20",
          "estimate_p50": "30m"}
    issues = [i for i in TV.validate_frontmatter(fm) if i.field == "scheduled"]
    assert len(issues) == 1 and issues[0].level == TV.WARN
    assert "30m" in issues[0].message


def test_all_day_scheduled_still_warns():
    fm = {"status": "open", "due": "2026-11-20", "scheduled": "2026-11-20"}
    assert "scheduled" in _fields(fm)


def test_timed_block_passes():
    for sched in ("2026-11-20 10:00~10:30", "9:00~9:30"):
        fm = {"status": "open", "worker_mode": "manual", "due": "2026-11-20",
              "scheduled": sched, "estimate_p50": "30m"}
        assert "scheduled" not in _fields(fm)


def test_exempt_worker_owned_undated_parent_and_done():
    base = {"due": "2026-11-20", "estimate_p50": "30m"}
    assert "scheduled" not in _fields({**base, "status": "open", "worker_mode": "auto"})
    assert "scheduled" not in _fields({"status": "open", "worker_mode": "manual", "due": "-"})
    assert "scheduled" not in _fields({**base, "status": "open", "is_parent": "true"})
    assert "scheduled" not in _fields({**base, "status": "done"})
    assert "scheduled" not in _fields({**base, "status": "open", "tags": ["todo", "decision"]})
    assert "scheduled" not in _fields({**base, "status": "open", "needs_decision": "true"})


def test_oversized_estimate_asks_for_a_split():
    fm = {"status": "open", "worker_mode": "manual", "due": "2026-11-20",
          "scheduled": "2026-11-20 10:00~14:00", "estimate_p50": "4h"}
    assert "estimate_p50" in _fields(fm)
    fm["estimate_p50"] = "2h"
    assert "estimate_p50" not in _fields(fm)
