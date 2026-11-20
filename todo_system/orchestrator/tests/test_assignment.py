"""Dedicated contract tests for `orchestrator/assignment.py` — the runtime SoT
(the `Assignment` state machine + `Ledger` persistence). Until this file, every
line of coverage was incidental to other modules' fixtures
(`test_deferred_display.py`, `test_dashboard.py`); this file pins the state
machine's own contract directly: the `apply_reply` transition table, the
unknown-type no-op, `last_note` priority, flag mirroring/reset, the six
orchestrator-only fields `apply_reply` must never touch, `is_terminal`,
the `is_deferred`/`display_state` dict seam, `to_json`/`from_json` round trip
and unknown-key tolerance, and `Ledger` save/load/all/active + corrupt-row
tolerance. Production code is untouched by this run — any behavior recorded
here that looks surprising is asserted as-is, not fixed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import message as m
from todo_system.orchestrator.assignment import Assignment, Ledger

THIS_FILE = Path(__file__)


def _mk(**overrides) -> Assignment:
    base = dict(task_id="t", agent_id="w", corr_id="c")
    base.update(overrides)
    return Assignment(**base)


def _reply(msg_type, **payload_kw) -> m.Envelope:
    return m.Envelope(to="orchestrator", type=msg_type, frm="w", payload=payload_kw)


class TestApplyReplyTransitions(unittest.TestCase):
    TRANSITIONS = [
        (m.ACCEPT, A.ACCEPTED),
        (m.PROGRESS, A.IN_PROGRESS),
        (m.RESULT, A.DONE),
        (m.FAIL, A.FAILED),
        (m.REJECT, A.REJECTED),
        (m.NEEDS_INPUT, A.NEEDS_INPUT),
    ]

    def test_each_known_type_transitions_and_records_history(self):
        for msg_type, expected_state in self.TRANSITIONS:
            with self.subTest(msg_type=msg_type):
                a = _mk(updated_ts="2000-01-01T00:00:00+09:00")
                ok = a.apply_reply(_reply(msg_type))
                self.assertTrue(ok)
                self.assertEqual(a.state, expected_state)
                self.assertEqual(len(a.history), 1)
                self.assertEqual(a.history[-1]["type"], msg_type)
                self.assertEqual(a.history[-1]["from"], "w")
                self.assertIn("ts", a.history[-1])
                self.assertNotEqual(a.updated_ts, "2000-01-01T00:00:00+09:00")

    def test_heartbeat_leaves_state_unchanged_but_records_history(self):
        a = _mk(state=A.IN_PROGRESS, updated_ts="2000-01-01T00:00:00+09:00")
        ok = a.apply_reply(_reply(m.HEARTBEAT))
        self.assertTrue(ok)
        self.assertEqual(a.state, A.IN_PROGRESS)
        self.assertEqual(len(a.history), 1)
        self.assertEqual(a.history[-1]["type"], m.HEARTBEAT)
        self.assertNotEqual(a.updated_ts, "2000-01-01T00:00:00+09:00")


class TestApplyReplyUnknownType(unittest.TestCase):
    def _assert_noop(self, msg_type):
        a = _mk(state=A.IN_PROGRESS, history=[{"x": 1}],
                updated_ts="UNCHANGED", last_note="keep")
        pre_state, pre_history, pre_updated, pre_note = (
            a.state, list(a.history), a.updated_ts, a.last_note)
        ok = a.apply_reply(_reply(msg_type))
        self.assertFalse(ok)
        self.assertEqual(a.state, pre_state)
        self.assertEqual(a.history, pre_history)
        self.assertEqual(a.updated_ts, pre_updated)
        self.assertEqual(a.last_note, pre_note)

    def test_task_assign_is_a_noop(self):
        self._assert_noop(m.TASK_ASSIGN)

    def test_ping_is_a_noop(self):
        self._assert_noop(m.PING)


class TestLastNotePriority(unittest.TestCase):
    def test_reason_wins_over_summary_and_note(self):
        a = _mk()
        a.apply_reply(_reply(m.PROGRESS, reason="R", summary="S", note="N"))
        self.assertEqual(a.last_note, "R")

    def test_summary_wins_when_no_reason(self):
        a = _mk()
        a.apply_reply(_reply(m.PROGRESS, summary="S", note="N"))
        self.assertEqual(a.last_note, "S")

    def test_note_wins_when_only_note(self):
        a = _mk()
        a.apply_reply(_reply(m.PROGRESS, note="N"))
        self.assertEqual(a.last_note, "N")

    def test_no_note_field_keeps_old_value(self):
        a = _mk(last_note="previous")
        a.apply_reply(_reply(m.PROGRESS))
        self.assertEqual(a.last_note, "previous")

    def test_note_truncated_to_200_chars(self):
        a = _mk()
        long_reason = "x" * 250
        a.apply_reply(_reply(m.PROGRESS, reason=long_reason))
        self.assertEqual(len(a.last_note), 200)
        self.assertEqual(a.last_note, "x" * 200)


class TestFlagMirroring(unittest.TestCase):
    def test_empty_payload_resets_all_flags_and_blocked_on(self):
        a = _mk(capacity_limited=True, job_running=True, checkout_busy=True,
                timed_out=True, blocked_on="old")
        a.apply_reply(_reply(m.PROGRESS))
        self.assertFalse(a.capacity_limited)
        self.assertFalse(a.job_running)
        self.assertFalse(a.checkout_busy)
        self.assertFalse(a.timed_out)
        self.assertEqual(a.blocked_on, "")

    def test_payload_flags_mirror_true(self):
        a = _mk()
        a.apply_reply(_reply(m.FAIL, capacity_limited=True, job_running=True,
                             checkout_busy=True, timed_out=True,
                             blocked_on="upstream-pr-123"))
        self.assertTrue(a.capacity_limited)
        self.assertTrue(a.job_running)
        self.assertTrue(a.checkout_busy)
        self.assertTrue(a.timed_out)
        self.assertEqual(a.blocked_on, "upstream-pr-123")

    def test_blocked_on_none_coerces_to_empty_string(self):
        a = _mk(blocked_on="old")
        a.apply_reply(_reply(m.PROGRESS, blocked_on=None))
        self.assertEqual(a.blocked_on, "")


class TestOrchestratorOnlyFieldsPreserved(unittest.TestCase):
    SIX_FIELDS = dict(timeout_streak=2, refail_count=3, capacity_retry_count=4,
                       last_alert_kind="quarantine", close_fingerprint="abc123")

    def test_accept_then_progress_leaves_six_fields(self):
        a = _mk(**self.SIX_FIELDS)
        a.apply_reply(_reply(m.ACCEPT))
        a.apply_reply(_reply(m.PROGRESS))
        for name, value in self.SIX_FIELDS.items():
            with self.subTest(field=name):
                self.assertEqual(getattr(a, name), value)

    def test_timeout_streak_survives_three_fail_accept_fail_sequence(self):
        # apply_reply never mutates timeout_streak in either direction — the
        # increment/reset lives in the orchestrator's collect(), out of scope
        # here (assignment.py:127-134). This pins the regression the AC names:
        # an intervening ACCEPT between two timed-out FAILs must not silently
        # zero the streak, because apply_reply is a no-op on the field.
        a = _mk(timeout_streak=1)
        a.apply_reply(_reply(m.FAIL, timed_out=True))
        self.assertEqual(a.state, A.FAILED)
        self.assertTrue(a.timed_out)
        self.assertEqual(a.timeout_streak, 1)
        a.apply_reply(_reply(m.ACCEPT))
        self.assertEqual(a.timeout_streak, 1)
        a.apply_reply(_reply(m.FAIL, timed_out=True))
        self.assertEqual(a.timeout_streak, 1)


class TestIsTerminal(unittest.TestCase):
    EXPECTED = {
        A.DONE: True,
        A.FAILED: True,
        A.REJECTED: True,
        A.DISPATCHED: False,
        A.ACCEPTED: False,
        A.IN_PROGRESS: False,
        A.NEEDS_INPUT: False,
        A.AWAITING_HUMAN: False,
    }

    def test_terminal_boundary(self):
        for state, expected in self.EXPECTED.items():
            with self.subTest(state=state):
                self.assertEqual(_mk(state=state).is_terminal, expected)


class TestIsDeferredDictSeam(unittest.TestCase):
    def test_failed_job_running_is_deferred(self):
        self.assertTrue(A.is_deferred({"state": "failed", "job_running": True}))

    def test_failed_checkout_busy_is_deferred(self):
        self.assertTrue(A.is_deferred({"state": "failed", "checkout_busy": True}))

    def test_failed_row_requeued_by_scheduled_resume_is_deferred(self):
        # The sweep stores FAILED so the normal re-dispatch path picks the row
        # up. Until it does, this is a queued retry, not a failure.
        row = {"state": "failed", "history": [
            {"type": "FAIL"}, {"type": A.RESUMED_SCHEDULED}]}
        self.assertTrue(A.is_deferred(row))
        self.assertEqual(A.display_state(row), A.DEFERRED)
        row_obj = Assignment(task_id="t", agent_id="a", corr_id="c",
                             state=A.FAILED, history=row["history"])
        self.assertEqual(row_obj.display_state, A.DEFERRED)

    def test_failure_after_a_resume_reads_failed_again(self):
        row = {"state": "failed", "history": [
            {"type": A.RESUMED_SCHEDULED}, {"type": "FAIL"}]}
        self.assertFalse(A.is_deferred(row))

    def test_resume_marker_never_masks_a_non_failed_state(self):
        for st in ("rejected", "done", "awaiting_human"):
            row = {"state": st, "history": [{"type": A.RESUMED_SCHEDULED}]}
            self.assertFalse(A.is_deferred(row), st)

    def test_malformed_history_is_not_resume_pending(self):
        for h in (None, "x", [], ["resumed-scheduled"], [{"type": None}]):
            self.assertFalse(A.is_deferred({"state": "failed", "history": h}), h)

    def test_rejected_is_never_deferred(self):
        row = {"state": "rejected", "job_running": True}
        self.assertFalse(A.is_deferred(row))
        self.assertEqual(A.display_state(row), A.REJECTED)

    def test_empty_dict_display_state_is_empty_string(self):
        self.assertEqual(A.display_state({}), "")

    def test_field_getattr_fallback_returns_none(self):
        self.assertIsNone(A._field({}, "nonexistent"))
        self.assertIsNone(A._field(object(), "nonexistent"))


class TestJsonRoundTrip(unittest.TestCase):
    def _distinctive(self) -> Assignment:
        return Assignment(
            task_id="t1", agent_id="w1", corr_id="c1", state=A.IN_PROGRESS,
            kind="ai-local", attempts=2, refail_count=1,
            created_ts="2026-01-01T00:00:00+09:00",
            updated_ts="2026-01-02T00:00:00+09:00",
            last_nudge_ts="2026-01-03T00:00:00+09:00", nudge_index=1,
            last_note="한글 노트 확인", capacity_retry_count=1,
            capacity_limited=True, job_running=True, checkout_busy=True,
            blocked_on="upstream", timed_out=True, timeout_streak=1,
            last_alert_kind="quarantine", close_fingerprint="abc123",
            history=[{"ts": "x", "type": m.ACCEPT, "from": "w1"}],
            payload={"repo": "https://example.com/x.git"},
        )

    def test_round_trip_preserves_all_fields(self):
        a = self._distinctive()
        dumped = a.to_json()
        self.assertIn("한글", dumped)
        restored = Assignment.from_json(dumped)
        self.assertEqual(restored, a)

    def test_from_json_drops_unknown_key(self):
        a = self._distinctive()
        data = json.loads(a.to_json())
        data["a_field_from_a_newer_build"] = 123
        text = json.dumps(data, ensure_ascii=False)
        restored = Assignment.from_json(text)  # must not raise TypeError
        self.assertEqual(restored, a)

    def test_ledger_load_and_all_do_not_drop_row_with_unknown_key(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        a = self._distinctive()
        data = json.loads(a.to_json())
        data["a_field_from_a_newer_build"] = 123
        (tmp / "t1.json").write_text(json.dumps(data, ensure_ascii=False),
                                      encoding="utf-8")
        led = Ledger(tmp)
        loaded = led.load("t1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded, a)
        rows = led.all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].task_id, "t1")


class TestLedgerRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.led = Ledger(self.tmp)

    def test_save_creates_file_and_load_returns_equal_value(self):
        a = _mk(task_id="t1")
        self.led.save(a)
        self.assertTrue((self.tmp / "t1.json").exists())
        self.assertEqual(self.led.load("t1"), a)

    def test_load_missing_task_id_returns_none(self):
        self.assertIsNone(self.led.load("nope"))

    def test_all_on_nonexistent_root_returns_empty_list(self):
        led = Ledger(self.tmp / "does-not-exist")
        self.assertEqual(led.all(), [])

    def test_all_returns_sorted_by_filename(self):
        for task_id in ("c", "a", "b"):
            self.led.save(_mk(task_id=task_id))
        self.assertEqual([x.task_id for x in self.led.all()], ["a", "b", "c"])


class TestLedgerCorruptRowTolerance(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.led = Ledger(self.tmp)

    def test_load_and_all_skip_torn_json(self):
        self.led.save(_mk(task_id="t1"))
        (self.tmp / "t2.json").write_text('{"task_id":', encoding="utf-8")
        self.assertIsNone(self.led.load("t2"))
        rows = self.led.all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].task_id, "t1")

    def test_load_oserror_returns_none(self):
        # Point the ledger's task path at a directory instead of a file so
        # `.read_text()` raises IsADirectoryError (an OSError subclass),
        # exercising the `except (json.JSONDecodeError, OSError)` OSError arm.
        (self.tmp / "t3.json").mkdir()
        self.assertIsNone(self.led.load("t3"))

    def test_all_oserror_row_is_skipped(self):
        self.led.save(_mk(task_id="t1"))
        (self.tmp / "t2.json").mkdir()
        rows = self.led.all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].task_id, "t1")


class TestLedgerActive(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.led = Ledger(self.tmp)

    def test_active_returns_non_terminal_row_unchanged(self):
        a = _mk(task_id="t1", state=A.IN_PROGRESS)
        self.led.save(a)
        self.assertEqual(self.led.active("t1"), a)

    def test_active_returns_none_for_terminal_rows(self):
        for state in (A.DONE, A.FAILED, A.REJECTED):
            with self.subTest(state=state):
                task_id = f"terminal-{state}"
                self.led.save(_mk(task_id=task_id, state=state))
                self.assertIsNone(self.led.active(task_id))

    def test_active_returns_none_for_missing_row(self):
        self.assertIsNone(self.led.active("nope"))


class TestNoRealVaultPathUsed(unittest.TestCase):
    def test_grep_for_real_vault_path(self):
        # Built by concatenation so the literal substring never appears in
        # this file's own source (which would make this self-test trivially
        # find itself).
        pattern = "Worker/" + "assignments"
        result = subprocess.run(
            ["grep", "-n", pattern, str(THIS_FILE)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1, result.stdout)


if __name__ == "__main__":
    unittest.main()
