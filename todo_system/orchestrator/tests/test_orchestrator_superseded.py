"""Orchestrator side of the superseded verdict (superseded.py): collect() drops a
park, an alert or a retry for a task that is already over, and closes the row
quietly.

Layout: a temp vault with `Worker/agents` (FileMailbox), `Worker/assignments`
(Ledger, so `ledger.root.parent.parent` is the vault root, as in production)
and `Todo/items/<slug>.md`. `notify` and `alert_route` are recorders, so "no
alert" is an observation and not a missing configuration.

Every suppression test has a control built from the same fixture with the md
left open, in which the park and the alert really go out. Without that pair a
suppression test would also pass on a fixture that could never alert.

The tests named `existing_sweep` pin behaviour that already existed before the
superseded gate (dispatch()'s md guard, reconcile_split_parents). They are
green without the new code and are not evidence for it.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system import cli
from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import message as m
from todo_system.orchestrator import orchestrator as orch_mod
from todo_system.orchestrator import superseded as sup
from todo_system.orchestrator import tasks
from todo_system.orchestrator.assignment import Assignment, Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import (
    ALERT_BLOCKED,
    ALERT_REVIEW_PR,
    ALERT_TIMEOUT_PARK,
    Orchestrator,
    Task,
)
from todo_system.orchestrator.registry import Agent, Registry

# Only names that predate the superseded gate are imported above, so this file
# still collects against an orchestrator.py without it: the RED control then
# shows which tests the gate turns green, test by test, instead of one
# collection error. The gate's own constant is read through `orch_mod` inside
# the one test that needs it.

SLUG = "sup-task"
TASK_PATH = f"Todo/items/{SLUG}.md"
PR_URL = "https://github.com/example-org/example-repo/pull/7"

# Sweeps that read live machine state (gh, the real queue file, other repos).
# The tick tests below stub them; dispatch, collect, reconcile_split_parents,
# reconcile_closed, reconcile_unblocked and renudge_due run for real.
_TICK_STUBS = (
    "reroute_remote", "reap_stale", "reconcile_unparked", "reconcile_resumes",
    "reconcile_rescope", "reconcile_merged",
    "reconcile_parents", "reconcile_human_ready", "reconcile_queue",
    "_advote_bridge_tick", "_beta_sweep_tick", "_project_stall_tick",
)


def _md(status: str) -> str:
    return "\n".join([
        "---", "tags:", "  - todo", f"status: {status}", "updated: 2026-01-01",
        'outcome: "x"', "---", "", "# sup task", "", "- [ ] step one", "",
        "## Progress Log", "", "- 2026-01-01: created", ""])


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.vault = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        self.items = self.vault / "Todo" / "items"
        self.items.mkdir(parents=True)
        self.md = self.items / f"{SLUG}.md"
        self.mb = FileMailbox(self.vault / "Worker" / "agents")
        self.ledger = Ledger(self.vault / "Worker" / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=[SLUG])])
        self.sent: list[tuple] = []
        self.routed: list[tuple] = []

        def notify(text, link=None):
            self.sent.append((text, link))

        def route(text, **kw):
            # No dedup state: every alert that reaches the router is delivered.
            self.routed.append((text, kw))
            return ("send", text)

        self.orch = Orchestrator(self.mb, self.reg, self.ledger,
                                 notify=notify, alert_route=route)
        # The slug-based readers (tasks.task_status / mark_done / md_fingerprint)
        # default to cli.ITEMS_DIR. Point them at this vault, never a real one.
        patcher = mock.patch.object(cli, "ITEMS_DIR", self.items)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(sup.KILL_ENV, None)

    # -- helpers ----------------------------------------------------------
    def _write_md(self, status: str) -> bytes:
        self.md.write_text(_md(status), encoding="utf-8")
        return self.md.read_bytes()

    def _row(self, state=A.IN_PROGRESS, **kw) -> Assignment:
        kw.setdefault("payload", {"task_path": TASK_PATH, "summary": "x"})
        a = Assignment(task_id=SLUG, agent_id="w", corr_id="C1", kind="ai-local",
                       state=state, **kw)
        self.ledger.save(a)
        return a

    def _reply(self, type_: str, corr_id: str = "C1", **payload) -> None:
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=type_, frm="w",
                                task_id=SLUG, corr_id=corr_id, payload=payload))

    def _collect(self) -> Assignment:
        self.orch.collect()
        return self.ledger.load(SLUG)

    def _assigns(self) -> list:
        return [e for e in self.mb.receive("w") if e.type == m.TASK_ASSIGN]

    def _history_types(self, a: Assignment) -> list:
        return [h.get("type") for h in a.history]

    def _assert_quiet(self):
        self.assertEqual(self.sent, [], "a human alert went out")
        self.assertEqual(self.routed, [], "an alert reached the router")

    def _assert_closed_quietly(self, a: Assignment, reason: str):
        self.assertEqual(a.state, A.DONE)
        [mark] = [h for h in a.history if h.get("type") == sup.HISTORY_TYPE]
        self.assertEqual(mark["reason"], reason)
        self.assertEqual(mark["from"], "orchestrator")
        self.assertEqual(a.last_alert_kind, "")
        self.assertIn("superseded", a.last_note)
        self.assertFalse(a.job_running)
        self.assertFalse(a.checkout_busy)
        self._assert_quiet()
        self.assertEqual(self._assigns(), [], "the task was re-dispatched")


class TimeoutParkTest(_Fixture):
    """'2회 연속 stage timeout' — the second consecutive timeout parks and pages."""

    def _second_timeout(self):
        self._row(timeout_streak=1)
        self._reply(m.FAIL, timed_out=True, timeout_stage="implement",
                    reason="stage implement: timed out after 3600s")

    def test_closed_md_drops_the_park_and_the_alert(self):
        before = self._write_md("done")
        self._second_timeout()
        a = self._collect()
        self._assert_closed_quietly(a, sup.REASON_MD_CLOSED)
        self.assertEqual(a.timeout_streak, 2)        # counted, just not acted on
        self.assertEqual(self.md.read_bytes(), before)
        self.assertEqual(a.close_fingerprint, hashlib.sha256(before).hexdigest())

    def test_control_open_md_still_parks_and_alerts(self):
        self._write_md("open")
        self._second_timeout()
        a = self._collect()
        self.assertEqual(a.state, A.AWAITING_HUMAN)
        self.assertEqual(a.last_alert_kind, ALERT_TIMEOUT_PARK)
        self.assertEqual(len(self.sent), 1, self.sent)
        self.assertEqual(len(self.routed), 1)
        self.assertNotIn(sup.HISTORY_TYPE, self._history_types(a))
        self.assertEqual(self._assigns(), [])

    def test_control_missing_md_still_parks_and_alerts(self):
        # No md to read is "cannot tell", not "closed".
        self._second_timeout()
        a = self._collect()
        self.assertEqual(a.state, A.AWAITING_HUMAN)
        self.assertEqual(a.last_alert_kind, ALERT_TIMEOUT_PARK)
        self.assertEqual(len(self.sent), 1)

    def test_cancelled_md_is_left_cancelled(self):
        for status in ("cancelled", "canceled"):
            with self.subTest(status=status):
                self.sent.clear()
                self.routed.clear()
                before = self._write_md(status)
                self._second_timeout()
                a = self._collect()
                self._assert_closed_quietly(a, sup.REASON_MD_CLOSED)
                self.assertIn(status, a.last_note)
                self.assertEqual(self.md.read_bytes(), before)
                self.assertEqual(tasks.task_status(SLUG), status)


class BlockedParkTest(_Fixture):
    """'선행 조건 미충족' — a FAIL that names a blocker parks and pages."""

    def _blocked(self):
        self._row()
        self._reply(m.FAIL, blocked_on="upstream-task (PR #3, OPEN)", verify="FAIL",
                    reason="blocked on upstream-task — not a defect")

    def test_closed_md_drops_the_park_and_the_alert(self):
        before = self._write_md("done")
        self._blocked()
        a = self._collect()
        self._assert_closed_quietly(a, sup.REASON_MD_CLOSED)
        self.assertEqual(self.md.read_bytes(), before)

    def test_control_open_md_still_parks_and_alerts(self):
        self._write_md("open")
        self._blocked()
        a = self._collect()
        self.assertEqual(a.state, A.AWAITING_HUMAN)
        self.assertEqual(a.last_alert_kind, ALERT_BLOCKED)
        self.assertEqual(len(self.sent), 1, self.sent)
        self.assertNotIn(sup.HISTORY_TYPE, self._history_types(a))


class ReviewParkTest(_Fixture):
    """A `review_required` RESULT parks the PR for a human and pages."""

    def test_closed_md_drops_the_review_park(self):
        before = self._write_md("done")
        self._row()
        self._reply(m.RESULT, review_required=True, pr_url=PR_URL,
                    summary="staged pipeline complete (5 stages) — needs review")
        a = self._collect()
        self._assert_closed_quietly(a, sup.REASON_MD_CLOSED)
        self.assertEqual(self.md.read_bytes(), before)

    def test_control_open_md_still_parks_for_review(self):
        self._write_md("open")
        self._row()
        self._reply(m.RESULT, review_required=True, pr_url=PR_URL, summary="needs review")
        a = self._collect()
        self.assertEqual(a.state, A.AWAITING_HUMAN)
        self.assertEqual(a.last_alert_kind, ALERT_REVIEW_PR)
        self.assertEqual([link for _, link in self.sent], [PR_URL])


class RunnerReportedTest(_Fixture):
    """The runner stopped at a stage boundary and said why (RESULT `superseded`)."""

    def _reported(self, reason, **extra):
        self._row()
        self._reply(m.RESULT, **{sup.RESULT_KEY: reason}, review_required=False,
                    summary="superseded — stopped before stage verify, nothing pushed",
                    branch=f"worker/{SLUG}", stages=["research", "plan", "implement"],
                    **extra)

    def test_md_closed_report_closes_the_row_and_leaves_the_md_alone(self):
        before = self._write_md("done")
        self._reported(sup.REASON_MD_CLOSED)
        with mock.patch.object(tasks, "mark_done") as mark_done:
            a = self._collect()
        mark_done.assert_not_called()
        self._assert_closed_quietly(a, sup.REASON_MD_CLOSED)
        self.assertEqual(self.md.read_bytes(), before)     # no box ticked, no trace line
        self.assertEqual(a.close_fingerprint, hashlib.sha256(before).hexdigest())

    def test_md_closed_report_on_a_cancelled_md_keeps_it_cancelled(self):
        before = self._write_md("cancelled")
        self._reported(sup.REASON_MD_CLOSED)
        a = self._collect()
        self._assert_closed_quietly(a, sup.REASON_MD_CLOSED)
        self.assertEqual(self.md.read_bytes(), before)

    def test_merged_report_closes_the_open_md_with_a_reason_line(self):
        self._write_md("open")
        self._reported(sup.REASON_MERGED)
        a = self._collect()
        self._assert_closed_quietly(a, sup.REASON_MERGED)
        self.assertEqual(tasks.task_status(SLUG), "done")
        text = self.md.read_text(encoding="utf-8")
        [trace] = [ln for ln in text.splitlines() if tasks.CLOSE_TRACE in ln]
        # Written into git-tracked task markdown: the wording is wire format.
        self.assertEqual(orch_mod._SUPERSEDED_MD_NOTE,
                         "superseded: 브랜치 변경이 이미 기본 브랜치에 반영됨")
        self.assertTrue(trace.endswith(f"— {orch_mod._SUPERSEDED_MD_NOTE}"), trace)
        self.assertIn("- [x] step one", text)
        # Evidence for reconcile_closed is the md as it was BEFORE this close.
        self.assertEqual(a.close_fingerprint,
                         hashlib.sha256(_md("open").encode("utf-8")).hexdigest())

    def test_merged_report_on_a_cancelled_md_keeps_it_cancelled(self):
        before = self._write_md("cancelled")
        self._reported(sup.REASON_MERGED)
        a = self._collect()
        self._assert_closed_quietly(a, sup.REASON_MERGED)
        self.assertEqual(self.md.read_bytes(), before)
        self.assertEqual(tasks.task_status(SLUG), "cancelled")

    def test_md_closed_report_is_not_believed_when_this_md_is_open(self):
        # The runner read ITS copy of the vault. A task is never closed on hearsay.
        before = self._write_md("open")
        self._reported(sup.REASON_MD_CLOSED)
        a = self._collect()
        self.assertEqual(a.state, A.FAILED)
        [mark] = [h for h in a.history if h.get("type") == sup.HISTORY_UNCONFIRMED]
        self.assertEqual(mark["reason"], sup.REASON_MD_CLOSED)
        self.assertNotIn(sup.HISTORY_TYPE, self._history_types(a))
        self.assertEqual(self.md.read_bytes(), before)
        self.assertEqual(a.close_fingerprint, "")
        self._assert_quiet()
        self.assertEqual(self._assigns(), [])      # requeued by the next dispatch, not here

    def test_unknown_reason_takes_the_confirmed_path(self):
        for value in (True, "something-newer"):
            with self.subTest(value=value):
                self._write_md("done")
                self._reported(value)
                self._assert_closed_quietly(self._collect(), sup.REASON_MD_CLOSED)
                self._write_md("open")
                self._reported(value)
                self.assertEqual(self._collect().state, A.FAILED)
                self._assert_quiet()

    def test_stale_corr_id_is_dropped_as_before(self):
        self._write_md("done")
        self._row()
        self._reply(m.RESULT, corr_id="OLD", **{sup.RESULT_KEY: sup.REASON_MD_CLOSED})
        a = self._collect()
        self.assertEqual(a.state, A.IN_PROGRESS)
        self.assertEqual(a.history, [])


class FailOpenTest(_Fixture):
    """When the verdict cannot be reached, collect() routes exactly as before."""

    def _second_timeout_on_a_closed_md(self):
        self._write_md("done")
        self._row(timeout_streak=1)
        self._reply(m.FAIL, timed_out=True, timeout_stage="implement")

    def _assert_parked_as_before(self, a):
        self.assertEqual(a.state, A.AWAITING_HUMAN)
        self.assertEqual(a.last_alert_kind, ALERT_TIMEOUT_PARK)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn(sup.HISTORY_TYPE, self._history_types(a))

    def test_control_the_fixture_is_suppressed_when_nothing_fails(self):
        self._second_timeout_on_a_closed_md()
        self._assert_closed_quietly(self._collect(), sup.REASON_MD_CLOSED)

    def test_unreadable_md_parks_as_before(self):
        self._second_timeout_on_a_closed_md()
        with mock.patch.object(sup, "md_closed_status", side_effect=RuntimeError("boom")):
            a = self._collect()
        self._assert_parked_as_before(a)

    def test_failing_switch_read_parks_as_before(self):
        self._second_timeout_on_a_closed_md()
        with mock.patch.object(sup, "enabled", side_effect=RuntimeError("boom")):
            a = self._collect()
        self._assert_parked_as_before(a)

    def test_kill_switch_parks_as_before(self):
        self._second_timeout_on_a_closed_md()
        os.environ[sup.KILL_ENV] = "0"
        self._assert_parked_as_before(self._collect())

    def test_kill_switch_treats_a_reported_result_as_an_ordinary_success(self):
        self._write_md("open")
        self._row()
        self._reply(m.RESULT, **{sup.RESULT_KEY: sup.REASON_MERGED}, review_required=False)
        os.environ[sup.KILL_ENV] = "off"
        a = self._collect()
        self.assertEqual(a.state, A.DONE)
        self.assertNotIn(sup.HISTORY_TYPE, self._history_types(a))
        self.assertEqual(tasks.task_status(SLUG), "done")
        self.assertNotIn("superseded", self.md.read_text(encoding="utf-8"))

    def test_a_failing_close_does_not_break_collect_or_alert(self):
        self._second_timeout_on_a_closed_md()
        with mock.patch.object(self.orch, "_close_superseded",
                               side_effect=RuntimeError("boom")):
            a = self._collect()
        # The reply is consumed, nothing is paged, and the row stays as the reply
        # left it; reconcile_split_parents tidies a FAILED row whose md is closed.
        self.assertEqual(a.state, A.FAILED)
        self._assert_quiet()
        self.assertEqual(self.mb.receive(m.ORCHESTRATOR), [])

    def test_human_rows_are_never_touched(self):
        self._write_md("done")
        a = Assignment(task_id=SLUG, agent_id="w", corr_id="C1", kind="human",
                       payload={"task_path": TASK_PATH})
        for msg in (
                m.Envelope(to=m.ORCHESTRATOR, type=m.RESULT, frm="w", task_id=SLUG,
                           payload={"review_required": True}),
                m.Envelope(to=m.ORCHESTRATOR, type=m.RESULT, frm="w", task_id=SLUG,
                           payload={sup.RESULT_KEY: sup.REASON_MERGED})):
            self.assertEqual(self.orch._superseded_reason(a, msg), "")
        a.kind = "ai-local"
        self.assertEqual(self.orch._superseded_reason(a, msg), sup.REASON_MERGED)


class UntouchedRepliesTest(_Fixture):
    """Replies outside the three alerting shapes never read the md and take the
    branches they always took, closed md or not."""

    def _spy(self):
        return mock.patch.object(sup, "md_closed_status", wraps=sup.md_closed_status)

    def test_accept_and_progress_do_not_read_the_md(self):
        self._write_md("done")
        self._row(state=A.DISPATCHED)
        self._reply(m.ACCEPT)
        self._reply(m.PROGRESS, note="stage research …")
        with self._spy() as spy:
            a = self._collect()
        spy.assert_not_called()
        self.assertEqual(a.state, A.IN_PROGRESS)
        self._assert_quiet()

    def test_plain_success_closes_as_before_without_reading_the_md(self):
        self._write_md("open")
        self._row()
        self._reply(m.RESULT, review_required=False, summary="staged pipeline complete")
        with self._spy() as spy:
            a = self._collect()
        spy.assert_not_called()
        self.assertEqual(a.state, A.DONE)
        self.assertNotIn(sup.HISTORY_TYPE, self._history_types(a))
        self.assertEqual(tasks.task_status(SLUG), "done")
        self.assertNotIn("superseded", self.md.read_text(encoding="utf-8"))

    def test_plain_failure_retries_as_before_without_reading_the_md(self):
        self._write_md("open")
        self._row()
        self._reply(m.FAIL, verify="FAIL", reason="verify FAIL — not merged")
        with self._spy() as spy:
            a = self._collect()
        spy.assert_not_called()
        self.assertEqual(a.state, A.DISPATCHED)
        self.assertEqual(a.attempts, 2)
        self.assertEqual(len(self._assigns()), 1)

    def test_first_timeout_retries_as_before(self):
        self._write_md("open")
        self._row()
        self._reply(m.FAIL, timed_out=True, timeout_stage="implement")
        with self._spy() as spy:
            a = self._collect()
        spy.assert_not_called()
        self.assertEqual(a.state, A.DISPATCHED)
        self.assertEqual(a.timeout_streak, 1)
        self.assertEqual(len(self._assigns()), 1)
        self._assert_quiet()

    def test_needs_input_parks_as_before(self):
        self._write_md("done")
        self._row()
        self._reply(m.NEEDS_INPUT, reason="which option?")
        with self._spy() as spy:
            a = self._collect()
        spy.assert_not_called()
        self.assertEqual(a.state, A.AWAITING_HUMAN)

    def test_row_without_task_path_parks_as_before(self):
        self._write_md("done")
        self._row(payload={"summary": "no task_path here"}, timeout_streak=1)
        self._reply(m.FAIL, timed_out=True)
        a = self._collect()
        self.assertEqual(a.state, A.AWAITING_HUMAN)
        self.assertEqual(a.last_alert_kind, ALERT_TIMEOUT_PARK)
        self.assertEqual(len(self.sent), 1)


class _TickFixture(_Fixture):
    """A real tick() with only the live-machine sweeps stubbed (_TICK_STUBS)."""

    def _tick(self, task_list):
        with contextlib.ExitStack() as st:
            for name in _TICK_STUBS:
                st.enter_context(mock.patch.object(self.orch, name, return_value=[]))
            st.enter_context(mock.patch.object(
                self.orch, "reconcile_batch_gate",
                return_value={"started": [], "merged": [], "returned": [], "closed": []}))
            st.enter_context(mock.patch.object(self.orch, "usage_hold", return_value=None))
            st.enter_context(mock.patch.object(self.orch, "paused", return_value=False))
            st.enter_context(mock.patch.object(self.orch, "budget_exceeded",
                                               return_value=False))
            return self.orch.tick(task_list)

    def _task(self) -> Task:
        return Task(SLUG, {"task_path": TASK_PATH, "summary": "x"})


class ExistingSweepTest(_TickFixture):
    """Behaviour that predates the superseded gate, pinned through a real tick().
    Green without the new code: NOT evidence for it."""

    def test_existing_sweep_failed_row_with_done_md_is_closed_not_redispatched(self):
        self._write_md("done")
        # Long past any fail cooldown: without the md guard this row would go out.
        self._row(state=A.FAILED, updated_ts="2026-01-01T00:00:00+00:00",
                  last_note="stage implement: timed out after 3600s")
        out = self._tick([self._task()])
        self.assertEqual(out["dispatched"], [])
        self.assertEqual(self._assigns(), [])
        self.assertEqual(self.ledger.load(SLUG).state, A.DONE)
        self._assert_quiet()

    def test_existing_sweep_control_failed_row_with_open_md_is_redispatched(self):
        self._write_md("open")
        self._row(state=A.FAILED, updated_ts="2026-01-01T00:00:00+00:00")
        out = self._tick([self._task()])
        self.assertEqual([a.task_id for a in out["dispatched"]], [SLUG])
        self.assertEqual(len(self._assigns()), 1)
        self.assertEqual(self.ledger.load(SLUG).state, A.DISPATCHED)

    def test_existing_sweep_parked_row_with_done_md_is_tidied_without_a_new_alert(self):
        self._write_md("done")
        self._row(state=A.AWAITING_HUMAN, last_alert_kind=ALERT_TIMEOUT_PARK,
                  timeout_streak=2, updated_ts="2026-01-01T00:00:00+00:00",
                  last_note="2회 연속 stage timeout — 이어가기 불가")
        out = self._tick([self._task()])
        self.assertEqual(out["dispatched"], [])
        self.assertEqual(out["renudged"], [])
        self.assertEqual(self.ledger.load(SLUG).state, A.DONE)
        self.assertEqual(self._assigns(), [])
        self._assert_quiet()

    def test_existing_sweep_control_parked_row_with_open_md_stays_parked(self):
        self._write_md("open")
        self._row(state=A.AWAITING_HUMAN, last_alert_kind=ALERT_TIMEOUT_PARK,
                  timeout_streak=2, updated_ts="2026-01-01T00:00:00+00:00")
        self._tick([self._task()])
        self.assertEqual(self.ledger.load(SLUG).state, A.AWAITING_HUMAN)
        self.assertEqual(self._assigns(), [])


class TickEndToEndTest(_TickFixture):
    """The new gate through a real tick(): the alert must not go out BEFORE the
    sweeps tidy the row (collect runs ahead of reconcile_split_parents)."""

    def test_second_timeout_on_a_done_task_is_closed_in_one_quiet_tick(self):
        self._write_md("done")
        self._row(timeout_streak=1)
        self._reply(m.FAIL, timed_out=True, timeout_stage="verify")
        out = self._tick([self._task()])
        a = self.ledger.load(SLUG)
        self._assert_closed_quietly(a, sup.REASON_MD_CLOSED)
        self.assertEqual(out["dispatched"], [])
        self.assertEqual(out["renudged"], [])

    def test_control_second_timeout_on_an_open_task_alerts_in_that_tick(self):
        self._write_md("open")
        self._row(timeout_streak=1)
        self._reply(m.FAIL, timed_out=True, timeout_stage="verify")
        self._tick([self._task()])
        a = self.ledger.load(SLUG)
        self.assertEqual(a.state, A.AWAITING_HUMAN)
        self.assertEqual(a.last_alert_kind, ALERT_TIMEOUT_PARK)
        self.assertEqual(len(self.sent), 1)

    def test_reopened_task_is_not_reclosed_by_the_backstop(self):
        # Closed quietly while the md said done; a person then reopens it. The
        # close evidence (the closed bytes) keeps reconcile_closed from undoing
        # that reopen.
        self._write_md("done")
        self._row(timeout_streak=1)
        self._reply(m.FAIL, timed_out=True)
        self._tick([])
        self.assertEqual(self.ledger.load(SLUG).state, A.DONE)
        self._write_md("open")
        self._tick([])
        self.assertEqual(tasks.task_status(SLUG), "open")
        self.assertIn("reopen-respected",
                      self._history_types(self.ledger.load(SLUG)))


if __name__ == "__main__":
    unittest.main()
