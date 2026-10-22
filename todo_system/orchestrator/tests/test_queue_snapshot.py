"""Orchestrator.queue_snapshot — the read-only queue view's wait verdicts.

The snapshot must come from the SAME gates tick() runs (hold_wait,
_checkout_gate, _ledger_gate): one fixture per `wait` value, the display
priority, the rows it leaves out, tick order, same-tick claims, and an
agreement test against a real `_dispatch_serial_per_checkout` pass.
"""

from __future__ import annotations

import datetime as dt
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import assignment as A
from todo_system.orchestrator.assignment import Assignment, Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import (
    WAIT_RANK, Orchestrator, Task, _clock, hold_wait,
)
from todo_system.orchestrator.registry import Agent, Registry

REPO = "https://github.com/example-org/app.git"
OTHER = "https://github.com/example-org/lib.git"
GRADLE_A = "https://github.com/example-org/mobile.git"
GRADLE_B = "https://github.com/example-org/tablet.git"


def _ago(seconds: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)).isoformat()


class QueueSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.orch = self._make()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, **kw):
        return Orchestrator(self.mb, self.reg, self.ledger,
                            notify=lambda *a, **k: None, **kw)

    def _save(self, slug, state, payload=None, **kw):
        self.ledger.save(Assignment(task_id=slug, agent_id="w", corr_id="c",
                                    kind="ai-local", state=state,
                                    payload=payload or {}, **kw))

    def _snap(self, tasks, **kw):
        return self.orch.queue_snapshot(tasks, now=time.time(), **kw)

    def _one(self, tasks, slug, **kw):
        rows = {r["slug"]: r for r in self._snap(tasks, **kw)}
        return rows[slug]

    # -- one fixture per wait value ---------------------------------------
    def test_paused(self):
        r = self._one([Task("alpha")], "alpha", paused=True)
        self.assertEqual(r["wait"], "paused")
        self.assertIn("PAUSED", r["wait_text"])

    def test_usage_hold(self):
        r = self._one([Task("alpha")], "alpha", usage_hold="5h window 92%")
        self.assertEqual(r["wait"], "usage_hold")
        self.assertIn("5h window 92%", r["wait_text"])

    def test_budget(self):
        r = self._one([Task("alpha")], "alpha", budget_exceeded=True)
        self.assertEqual(r["wait"], "budget")
        self.assertIn("/40)", r["wait_text"])

    def test_cooldown(self):
        self._save("alpha", A.FAILED, updated_ts=_ago(60))
        now = time.time()
        r = {x["slug"]: x for x in self.orch.queue_snapshot([Task("alpha")], now=now)}["alpha"]
        self.assertEqual(r["wait"], "cooldown")
        until = self.orch._ledger_gate(self.ledger.load("alpha"), now).until
        self.assertIn(_clock(until, now), r["wait_text"])
        self.assertIn("↻0", r["wait_text"])
        self.assertTrue(r["since"])          # ledger updated_ts, local ISO

    def test_cooldown_for_a_deferred_poll_row(self):
        self._save("alpha", A.FAILED, updated_ts=_ago(10), job_running=True)
        r = self._one([Task("alpha")], "alpha")
        self.assertEqual(r["wait"], "cooldown")
        self.assertIn("재확인 대기", r["wait_text"])

    def test_capacity(self):
        self._save("alpha", A.FAILED, updated_ts=_ago(60), capacity_limited=True)
        r = self._one([Task("alpha")], "alpha")
        self.assertEqual(r["wait"], "capacity")
        self.assertIn("capacity", r["wait_text"])

    def test_checkout_busy_names_the_holder(self):
        self._save("beta", A.DISPATCHED, {"repo": REPO})
        r = self._one([Task("alpha", {"repo": REPO})], "alpha")
        self.assertEqual(r["wait"], "checkout_busy")
        self.assertEqual(r["holder"], "beta")
        self.assertEqual(r["checkout"], "app")
        self.assertIn("`beta`", r["wait_text"])

    def test_gradle_cap(self):
        self.orch = self._make(max_parallel_gradle_builds=1)
        self._save("beta", A.IN_PROGRESS, {"repo": GRADLE_B})
        gradle = {GRADLE_A, GRADLE_B}
        with mock.patch.object(self.orch, "_is_gradle_checkout",
                               side_effect=lambda key: key in gradle):
            r = self._one([Task("alpha", {"repo": GRADLE_A})], "alpha")
        self.assertEqual(r["wait"], "gradle_cap")
        self.assertEqual(r["holder"], "")
        self.assertIn("(1 실행 중)", r["wait_text"])

    def test_next_tick(self):
        r = self._one([Task("alpha", {"repo": REPO})], "alpha")
        self.assertEqual(r["wait"], "next_tick")
        self.assertEqual((r["holder"], r["since"]), ("", ""))

    # -- priority / bypass -------------------------------------------------
    def test_paused_beats_checkout_busy(self):
        self._save("beta", A.DISPATCHED, {"repo": REPO})
        r = self._one([Task("alpha", {"repo": REPO})], "alpha", paused=True)
        self.assertEqual(r["wait"], "paused")
        self.assertEqual(r["holder"], "")

    def test_cooldown_beats_checkout_busy(self):
        self._save("beta", A.DISPATCHED, {"repo": REPO})
        self._save("alpha", A.FAILED, {"repo": REPO}, updated_ts=_ago(60))
        r = self._one([Task("alpha", {"repo": REPO})], "alpha")
        self.assertEqual(r["wait"], "cooldown")

    def test_bypass_guard_falls_through_a_usage_hold(self):
        r = self._one([Task("alpha", {"bypass_guard": True})], "alpha",
                      usage_hold="hard cap")
        self.assertEqual(r["wait"], "next_tick")

    def test_bypass_guard_does_not_beat_pause(self):
        r = self._one([Task("alpha", {"bypass_guard": True})], "alpha", paused=True)
        self.assertEqual(r["wait"], "paused")

    def test_hold_wait_order(self):
        self.assertEqual(hold_wait({}, paused=True, usage_hold="x", budget_exceeded=True),
                         "paused")
        self.assertEqual(hold_wait({}, paused=False, usage_hold="x", budget_exceeded=True),
                         "usage_hold")
        self.assertIsNone(hold_wait({}, paused=False, usage_hold=None, budget_exceeded=False))
        self.assertEqual(sorted(WAIT_RANK), sorted([
            "next_tick", "checkout_busy", "gradle_cap", "usage_hold", "paused",
            "budget", "cooldown", "capacity"]))

    # -- exclusions / order -----------------------------------------------
    def test_in_flight_parked_done_and_unroutable_are_left_out(self):
        self._save("inflight", A.IN_PROGRESS, {"repo": OTHER})
        self._save("parked", A.AWAITING_HUMAN)
        self._save("closed", A.DONE)
        self.reg = Registry([Agent(id="w", kind="ai-local",
                                   projects=["inflight", "parked", "closed", "alpha"],
                                   repo="~/x")])
        self.orch = self._make()
        snap = self._snap([Task("inflight"), Task("parked"), Task("closed"),
                           Task("stray"), Task("alpha")])
        self.assertEqual([r["slug"] for r in snap], ["alpha"])

    def test_order_is_input_order(self):
        tasks = [Task("gamma", {"repo": OTHER}), Task("alpha", {"repo": REPO}),
                 Task("beta")]
        self.assertEqual([r["slug"] for r in self._snap(tasks)],
                         ["gamma", "alpha", "beta"])

    def test_same_tick_claim_holds_the_later_task(self):
        snap = self._snap([Task("first", {"repo": REPO}), Task("second", {"repo": REPO})])
        self.assertEqual([(r["slug"], r["wait"], r["holder"]) for r in snap],
                         [("first", "next_tick", ""), ("second", "checkout_busy", "first")])

    def test_poison_row_reads_cooldown_and_is_not_quarantined(self):
        self._save("alpha", A.FAILED, refail_count=3, updated_ts=_ago(99999))
        r = self._one([Task("alpha")], "alpha")
        self.assertEqual(r["wait"], "cooldown")
        self.assertIn("격리 예정", r["wait_text"])
        self.assertEqual(self.ledger.load("alpha").state, A.FAILED)   # read-only

    def test_snapshot_writes_nothing(self):
        self._save("beta", A.DISPATCHED, {"repo": REPO})
        self._save("alpha", A.FAILED, {"repo": REPO}, updated_ts=_ago(99999),
                   checkout_busy=True, last_note="old")
        before = {p.name: p.read_text() for p in (self.tmp / "assignments").iterdir()}
        self._snap([Task("alpha", {"repo": REPO})])
        after = {p.name: p.read_text() for p in (self.tmp / "assignments").iterdir()}
        self.assertEqual(before, after)
        self.assertEqual(self.mb.receive("w"), [])

    # -- same code as tick -------------------------------------------------
    def test_next_tick_rows_are_exactly_what_dispatch_sends(self):
        self._save("busy", A.DISPATCHED, {"repo": OTHER})
        self._save("cool", A.FAILED, {"repo": "https://github.com/example-org/c.git"},
                   updated_ts=_ago(30))
        self._save("retry", A.FAILED, {"repo": "https://github.com/example-org/r.git"},
                   updated_ts=_ago(99999))
        tasks = [
            Task("a1", {"repo": REPO}),
            Task("a2", {"repo": REPO}),                      # held behind a1 (same tick)
            Task("o1", {"repo": OTHER}),                     # held behind busy
            Task("cool", {"repo": "https://github.com/example-org/c.git"}),
            Task("retry", {"repo": "https://github.com/example-org/r.git"}),
            Task("v1"),
            Task("v2"),
        ]
        snap = self.orch.queue_snapshot(tasks, now=time.time())
        expected = {r["slug"] for r in snap if r["wait"] == "next_tick"}
        dispatched = self.orch._dispatch_serial_per_checkout(tasks)
        self.assertEqual(expected, {a.task_id for a in dispatched})
        self.assertEqual(expected, {"a1", "retry", "v1"})


if __name__ == "__main__":
    unittest.main()
