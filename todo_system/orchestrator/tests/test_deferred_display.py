"""A FAILED row that is only WAITING must not read as a failure anywhere.

2026-09-25: `mulkko-backup-gcp-1-server-and-client` sat as `failed` on every
view with the note "deferred — HEAD is on worker/..., held by a LIVE sibling
run" — the runner had declined to start it (`checkout_busy`), nothing was
broken. The orchestrator already treats checkout_busy / job_running as a wait
(no refail, flat cooldown, never poisons); these tests pin that the views say
so too, and that a serial-per-checkout hold explains itself in the ledger.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import dashboard, metrics, trace
from todo_system.orchestrator.assignment import Assignment, Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import Orchestrator, Task
from todo_system.orchestrator.registry import Agent, Registry


def _row(slug, state, **kw):
    return Assignment(task_id=slug, agent_id="w", corr_id="c", kind="ai-local",
                      state=state, **kw)


class DisplayStateTest(unittest.TestCase):
    def test_checkout_busy_failed_is_deferred(self):
        a = _row("t", A.FAILED, checkout_busy=True)
        self.assertTrue(a.is_deferred)
        self.assertEqual(a.display_state, A.DEFERRED)
        self.assertEqual(a.state, A.FAILED)          # stored state untouched

    def test_job_running_failed_is_deferred(self):
        self.assertEqual(A.display_state(_row("t", A.FAILED, job_running=True)),
                         "deferred")

    def test_plain_failure_stays_failed(self):
        a = _row("t", A.FAILED, last_note="boom")
        self.assertFalse(a.is_deferred)
        self.assertEqual(a.display_state, A.FAILED)

    def test_capacity_limited_is_not_deferred(self):
        # capacity has its own badge/backoff; this fix is scoped to the two
        # self-clearing waits the orchestrator lumps together as `deferred`.
        self.assertEqual(A.display_state(_row("t", A.FAILED, capacity_limited=True)),
                         A.FAILED)

    def test_only_failed_maps(self):
        self.assertEqual(A.display_state(_row("t", A.DONE, checkout_busy=True)), A.DONE)
        self.assertEqual(A.display_state(_row("t", A.REJECTED, job_running=True)),
                         A.REJECTED)
        self.assertEqual(A.display_state(_row("t", A.IN_PROGRESS)), A.IN_PROGRESS)

    def test_raw_json_dict(self):
        d = json.loads(_row("t", A.FAILED, checkout_busy=True).to_json())
        self.assertTrue(A.is_deferred(d))
        self.assertEqual(A.display_state(d), "deferred")
        self.assertEqual(A.display_state({"state": "done"}), "done")
        self.assertEqual(A.display_state({}), "")


class _LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.led = Ledger(self.tmp / "assignments")


class DashboardGroupingTest(_LedgerCase):
    def setUp(self):
        super().setUp()
        self.led.save(_row("waiting-busy", A.FAILED, checkout_busy=True,
                           last_note="held by a LIVE sibling run", refail_count=1))
        self.led.save(_row("waiting-job", A.FAILED, job_running=True))
        self.led.save(_row("broken", A.FAILED, last_note="tests red"))
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"])])

    def _section(self, md, head):
        body = md.split(head, 1)[1]
        return body.split("\n## ", 1)[0]

    def test_deferred_rows_not_in_failure_section(self):
        md = dashboard.render(self.led, self.reg)
        failed = self._section(md, "## ❌ 실패")
        self.assertIn("broken", failed)
        self.assertNotIn("waiting-busy", failed)
        self.assertNotIn("waiting-job", failed)
        waiting = self._section(md, "## 💤 대기")
        self.assertIn("waiting-busy", waiting)
        self.assertIn("체크아웃 대기", waiting)
        self.assertIn("waiting-job", waiting)
        self.assertIn("long_job 실행 중", waiting)
        self.assertNotIn("재실패", waiting)     # a deferral never counts as a retry
        self.assertIn("deferred 2", md)
        self.assertIn("failed 1", md)

    def test_scheduled_resume_waits_in_the_deferred_section(self):
        self.led.save(_row("resumed", A.FAILED, last_note="예약 재개",
                           history=[{"type": "FAIL"},
                                    {"type": A.RESUMED_SCHEDULED}]))
        md = dashboard.render(self.led, self.reg)
        self.assertNotIn("resumed", self._section(md, "## ❌ 실패"))
        waiting = self._section(md, "## 💤 대기")
        self.assertIn("resumed", waiting)
        self.assertIn("예약 재개 — 재디스패치 대기", waiting)

    def test_trace_shows_display_state(self):
        out = "\n".join(trace._ledger_section("waiting-busy", self.led))
        self.assertIn("state: **deferred**", out)
        self.assertIn("원장 저장값: failed", out)
        out = "\n".join(trace._ledger_section("broken", self.led))
        self.assertIn("state: **failed** (agent", out)

    def test_metrics_failure_count_excludes_deferred(self):
        rows = [json.loads(a.to_json()) for a in self.led.all()]
        with mock.patch.object(metrics, "_load_ledger", return_value=rows), \
                mock.patch.object(metrics, "_db_counts",
                                  return_value={"done": 0, "auto_done": 0,
                                                "cancelled": 0}), \
                mock.patch.object(metrics, "_reverts_in_vault", return_value=0), \
                mock.patch.object(metrics, "RUNS", self.tmp / "no-runs"):
            md = metrics.render()
        self.assertIn("실패: 1 ", md)


class SerialHoldNoteTest(_LedgerCase):
    REPO = "https://github.com/example-org/if-then-habits.git"

    def setUp(self):
        super().setUp()
        self.orch = Orchestrator(FileMailbox(self.tmp / "agents"),
                                 Registry([Agent(id="w", kind="ai-local",
                                                 projects=["*"], repo="~/x",
                                                 concurrency=8)]),
                                 self.led, notify=lambda *a, **k: None)
        self.led.save(_row("edge-to-edge-insets-mulkko", A.IN_PROGRESS,
                           payload={"repo": self.REPO}))

    def _hold(self, slug):
        return self.orch._dispatch_serial_per_checkout(
            [Task(slug, {"repo": self.REPO})])

    def test_deferred_row_gets_note_naming_sibling(self):
        self.led.save(_row("mulkko-backup", A.FAILED, checkout_busy=True,
                           updated_ts="2026-09-25T10:00:00",
                           last_note="deferred — HEAD is on worker/x, held by a "
                                     "LIVE sibling run"))
        self.assertEqual(self._hold("mulkko-backup"), [])
        a = self.led.load("mulkko-backup")
        # neither row declares `touches`, so the hold reason is "overlap
        # undetermined" (not a cap hit: held=1 < cap=2 default)
        self.assertEqual(a.last_note,
                         "대기 — 같은 체크아웃(if-then-habits)에서 "
                         "`edge-to-edge-insets-mulkko` 실행 중; "
                         "사유: 겹침 미판정 (touches 없음)")
        # presentation only: state, cooldown clock and history untouched
        self.assertEqual(a.state, A.FAILED)
        self.assertTrue(a.checkout_busy)
        self.assertEqual(a.updated_ts, "2026-09-25T10:00:00")
        self.assertEqual(a.history, [])

    def test_cap_reached_reason_flows_into_the_deferred_note(self):
        # two live siblings fill cap=2 (default), so a deferred third is held on
        # the cap itself, not an overlap verdict — the note must say so.
        self.led.save(_row("edge-to-edge-insets-mulkko-2", A.IN_PROGRESS,
                           payload={"repo": self.REPO}))
        self.led.save(_row("mulkko-backup", A.FAILED, checkout_busy=True))
        self.assertEqual(self._hold("mulkko-backup"), [])
        self.assertIn("사유: 상한 도달 — repo당 동시 실행 최대 2개",
                      self.led.load("mulkko-backup").last_note)

    def test_unchanged_note_is_not_rewritten(self):
        self.led.save(_row("mulkko-backup", A.FAILED, checkout_busy=True))
        self._hold("mulkko-backup")
        with mock.patch.object(self.led, "save") as save:
            self._hold("mulkko-backup")
        save.assert_not_called()

    def test_genuine_failure_keeps_its_diagnosis(self):
        self.led.save(_row("broken", A.FAILED, last_note="tests red"))
        self._hold("broken")
        self.assertEqual(self.led.load("broken").last_note, "tests red")

    def test_no_ledger_row_creates_none(self):
        self._hold("fresh")
        self.assertIsNone(self.led.load("fresh"))

    def test_owner_dispatched_this_tick_is_named(self):
        # vault tasks: the first dispatches, the second (deferred) is held by it
        self.led.save(_row("vault-b", A.FAILED, job_running=True))
        self.orch._dispatch_serial_per_checkout([Task("vault-a"), Task("vault-b")])
        self.assertIn("`vault-a`", self.led.load("vault-b").last_note)
        self.assertIn("(<vault>)", self.led.load("vault-b").last_note)


if __name__ == "__main__":
    unittest.main()
