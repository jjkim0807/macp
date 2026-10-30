"""Tests for P3 — lease, assignment ledger, orchestrator core, observable exec."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system import usage_guard, worker
from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import message as m
from todo_system.orchestrator import dashboard, repos, stages, tasks, trace
from todo_system.orchestrator.assignment import Assignment, Ledger
from todo_system.orchestrator.lease import Lease
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import (
    ALERT_BLOCKED,
    ALERT_CLOSED_UNMERGED,
    ALERT_QUARANTINE,
    ALERT_REVIEW_PR,
    ALERT_TIMEOUT_PARK,
    Orchestrator,
    Task,
    _CLOSED_UNMERGED,
    _check_divergence,
    _globs_overlap,
    _parse_ts,
    _pull_main,
    _static_prefix,
    _touches_disjoint,
    parse_duration,
)
from todo_system.orchestrator.registry import Agent, Registry
from todo_system.orchestrator.runner import BackgroundClaudeExecutor, make_executor

# Sentinel for _hermetic_tick(usage_hold=...): leave the real `usage_hold()` in
# place because the test patches something deeper down that seam.
_INHERIT = object()


def _hermetic_tick(orch, tasks, *, usage_hold=None, paused=False,
                   budget_exceeded=False, live: tuple = ()):
    """`orch.tick(tasks)` with every machine-state read **injected, not inherited**.

    tick() (orchestrator.py:884-912) mixes verdict logic with six reads of whatever
    this particular mac happens to look like right now:

      1. usage_hold()        :870-882  live Keychain token + api.anthropic.com, and
                                       state under a repo-root-fixed path -> NOT
                                       tmpdir-isolated. This is the one that bit us.
      2. paused()            :107-111  ledger.root.parent/_state/PAUSED  -> already tmp
      3. budget_exceeded()   :104-105  ledger.root.parent/runs           -> already tmp
      4. reconcile_parents() :801-827  live Todo/todo.db
      5. reconcile_queue()   :829-868  live Worker/queue.json + takes the live lock at
                                       :854, which the running daemon also holds
      6. reconcile_merged()  :639-693  live `gh`, but only if the ledger has rows
      7. reconcile_unparked()          reads each parked row's task markdown off
                                       disk (payload `task_path`, resolved against
                                       the ledger root's grandparent) -> live
                                       Todo/items/ for any fixture row that
                                       carries a real task_path

    2/3 were hermetic by luck (the tests point the ledger at a tempdir); the rest were
    not. Injecting all of them keeps a verdict a statement about the code under test.
    The guards are passed as **explicit values** rather than riding the `return_value=[]`
    used for the stubs — `[]` only worked as "no hold" by being falsy.

    Production `orchestrator.py` is deliberately untouched: this is a test-side seam.

    `live` names sweeps to leave REAL — the one a test is about — so it runs
    through tick() itself rather than being called directly; the caller then owns
    making that sweep's reads hermetic.
    """
    stubs = tuple(name for name in (
        "collect", "reconcile_merged", "reconcile_unblocked",
        "reconcile_unparked", "reconcile_closed",
        "reconcile_queue", "reconcile_parents", "renudge_due", "reap_stale")
        if name not in live)
    guards = {"paused": paused, "budget_exceeded": budget_exceeded}
    if usage_hold is not _INHERIT:
        guards["usage_hold"] = usage_hold
    with contextlib.ExitStack() as st:
        for name in stubs:
            st.enter_context(mock.patch.object(orch, name, return_value=[]))
        for name, value in guards.items():
            st.enter_context(mock.patch.object(orch, name, return_value=value))
        return orch.tick(tasks)


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "orchestrator.lock"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_single_writer(self):
        a = Lease(self.path, holder="mac", ttl=100)
        b = Lease(self.path, holder="cloud", ttl=100)
        self.assertTrue(a.acquire(now=1000))
        self.assertFalse(b.acquire(now=1010))          # held by mac, unexpired
        self.assertTrue(a.renew(now=1050))             # holder renews
        self.assertTrue(b.acquire(now=1200))           # mac's lease expired (>1100)

    def test_release_frees(self):
        a = Lease(self.path, holder="mac", ttl=100)
        b = Lease(self.path, holder="cloud", ttl=100)
        a.acquire(now=0)
        a.release()
        self.assertTrue(b.acquire(now=1))


class AssignmentTest(unittest.TestCase):
    def test_transitions(self):
        a = Assignment(task_id="t", agent_id="x", corr_id="c")
        self.assertEqual(a.state, A.DISPATCHED)
        a.apply_reply(m.Envelope(to=m.ORCHESTRATOR, type=m.ACCEPT, frm="x", task_id="t"))
        self.assertEqual(a.state, A.ACCEPTED)
        a.apply_reply(m.Envelope(to=m.ORCHESTRATOR, type=m.RESULT, frm="x", task_id="t"))
        self.assertEqual(a.state, A.DONE)
        self.assertTrue(a.is_terminal)
        self.assertEqual(len(a.history), 2)

    def test_heartbeat_is_recorded_but_no_state_change(self):
        a = Assignment(task_id="t", agent_id="x", corr_id="c", state=A.IN_PROGRESS)
        self.assertTrue(a.apply_reply(
            m.Envelope(to=m.ORCHESTRATOR, type=m.HEARTBEAT, frm="x")))
        self.assertEqual(a.state, A.IN_PROGRESS)

    def test_ledger_active_excludes_terminal(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            led = Ledger(tmp)
            led.save(Assignment(task_id="t1", agent_id="x", corr_id="c", state=A.DONE))
            led.save(Assignment(task_id="t2", agent_id="x", corr_id="c",
                                state=A.IN_PROGRESS))
            self.assertIsNone(led.active("t1"))
            self.assertIsNotNone(led.active("t2"))
            self.assertEqual(len(led.all()), 2)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_crash_during_ledger_save_preserves_prior_row(self):
        # A crash mid-write to a ledger row must not leave a torn JSON file
        # that Ledger.load/all would silently skip — the task would vanish
        # from every sweep.
        tmp = Path(tempfile.mkdtemp())
        try:
            led = Ledger(tmp)
            led.save(Assignment(task_id="t1", agent_id="x", corr_id="c",
                                 state=A.IN_PROGRESS))
            with mock.patch("todo_system.orchestrator.atomic_io.os.replace",
                             side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    led.save(Assignment(task_id="t1", agent_id="x", corr_id="c",
                                         state=A.DONE))
            reloaded = led.load("t1")
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.state, A.IN_PROGRESS)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_timed_out_flag_mirrors_the_payload_but_streak_is_untouched(self):
        a = Assignment(task_id="t", agent_id="x", corr_id="c", timeout_streak=2)
        a.apply_reply(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="x", task_id="t",
                                 payload={"timed_out": True}))
        self.assertTrue(a.timed_out)
        self.assertEqual(a.timeout_streak, 2)          # apply_reply must NOT touch it
        # a later non-timeout reply clears the flag (collect() owns the streak)
        a.apply_reply(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="x", task_id="t",
                                 payload={}))
        self.assertFalse(a.timed_out)

    def test_new_fields_round_trip_and_old_rows_default(self):
        a = Assignment(task_id="t", agent_id="x", corr_id="c",
                       timed_out=True, timeout_streak=3)
        b = Assignment.from_json(a.to_json())
        self.assertTrue(b.timed_out)
        self.assertEqual(b.timeout_streak, 3)
        # an OLD row (written before these fields existed) still loads, at defaults
        old = json.dumps({"task_id": "t", "agent_id": "x", "corr_id": "c"})
        c = Assignment.from_json(old)
        self.assertFalse(c.timed_out)
        self.assertEqual(c.timeout_streak, 0)


class ParseDurationTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_duration("1d"), 86400)
        self.assertEqual(parse_duration("30m"), 1800)
        self.assertEqual(parse_duration("2h"), 7200)
        self.assertEqual(parse_duration("45"), 45)


class OrchestratorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([
            Agent(id="armchair-commander", kind="ai-local",
                  projects=["armchair-commander"], capabilities=["godot"],
                  repo="~/x"),
            Agent(id="alice", kind="human", projects=["withdrawal-*"],
                  notify=["kakao"], renudge_backoff=["1d", "3d"]),
        ])
        self.sent: list = []
        self.orch = Orchestrator(self.mb, self.reg, self.ledger,
                                 notify=lambda t, level=None: self.sent.append(t))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_dispatch_ai_and_dedup(self):
        a = self.orch.dispatch(Task("armchair-commander", {"summary": "core loop"}))
        self.assertEqual(a.state, A.DISPATCHED)
        [inbox] = self.mb.receive("armchair-commander")
        self.assertEqual(inbox.type, m.TASK_ASSIGN)
        # dedup: active assignment blocks re-dispatch
        self.assertIsNone(self.orch.dispatch(Task("armchair-commander")))

    def test_unroutable(self):
        self.assertIsNone(self.orch.dispatch(Task("no-such-project")))

    def test_agent_liveness_from_heartbeat(self):
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, frm="armchair-commander",
                                type=m.HEARTBEAT, payload={"status": "idle"}))
        self.orch.collect()
        live = self.orch.agent_liveness()
        self.assertEqual(live["armchair-commander"]["status"], "online")
        self.assertNotIn("alice", live)   # humans aren't heartbeat-monitored

    def _seen(self) -> dict:
        p = self.ledger.root.parent / "_state" / "agent_seen.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def test_human_sender_is_not_recorded_as_a_live_agent(self):
        # A person's message is not a heartbeat: recording it left `alice … 10d 전`
        # rows in the dashboard that read as "did a person die?". Runner keys still
        # update — this must not cost us runner liveness.
        for frm in ("armchair-commander", "alice"):
            self.mb.send(m.Envelope(to=m.ORCHESTRATOR, frm=frm, type=m.HEARTBEAT,
                                    payload={"status": "idle"}))
        self.orch.collect()
        seen = self._seen()
        self.assertIn("armchair-commander", seen)
        self.assertNotIn("alice", seen)

    def test_a_human_reply_still_advances_the_ledger_without_a_seen_key(self):
        # Skipping the liveness write must not skip the reply itself: alice's RESULT
        # still closes her assignment.
        a = self.orch.dispatch(Task("withdrawal-a2-final-decision"))
        self.assertEqual(a.state, A.AWAITING_HUMAN)
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, frm="alice", type=m.RESULT,
                                task_id=a.task_id, corr_id=a.corr_id))
        with mock.patch.object(self.orch, "_close_with_evidence"):
            self.orch.collect()
        self.assertEqual(self.ledger.load(a.task_id).state, A.DONE)
        self.assertNotIn("alice", self._seen())

    def test_no_rerun_of_done_task(self):
        # a completed assignment must never re-dispatch (would make a duplicate PR),
        # even if the todo status wasn't marked done (e.g. a failed mark_done)
        a = A.Assignment(task_id="armchair-commander", agent_id="armchair-commander",
                         corr_id="c", kind="ai-local", state=A.DONE)
        self.ledger.save(a)
        self.assertIsNone(self.orch.dispatch(Task("armchair-commander")))

    def _md(self, status_line: str) -> str:
        p = self.tmp / "armchair-commander.md"
        p.write_text(f"---\n{status_line}worker_mode: auto\n---\n# t\n", encoding="utf-8")
        return str(p)

    def test_no_dispatch_of_md_closed_task(self):
        # the md is the source of truth: a closed task is never sent, whatever
        # its caller (CLI dispatch, _min_progress_dispatch) and with no ledger row
        for status in ("done", "cancelled", "canceled", '"Done"'):
            with self.subTest(status=status):
                tp = self._md(f"status: {status}\n")
                self.assertIsNone(self.orch.dispatch(
                    Task("armchair-commander", {"task_path": tp})))
                self.assertEqual(self.mb.receive("armchair-commander"), [])
                self.assertIsNone(self.ledger.load("armchair-commander"))

    def test_no_redispatch_of_md_closed_failed_row(self):
        a = A.Assignment(task_id="armchair-commander", agent_id="armchair-commander",
                         corr_id="c-old", kind="ai-local", state=A.FAILED)
        self.ledger.save(a)
        tp = self._md("status: done\n")
        future = _parse_ts(a.updated_ts) + 10 * 86400       # well past any cooldown
        self.assertIsNone(self.orch.dispatch(
            Task("armchair-commander", {"task_path": tp}), now=future))
        self.assertEqual(self.mb.receive("armchair-commander"), [])
        row = self.ledger.load("armchair-commander")
        self.assertEqual((row.state, row.corr_id), (A.FAILED, "c-old"))  # untouched

    def test_md_open_or_statusless_task_dispatches(self):
        for sent, line in enumerate(("status: open\n", ""), start=1):
            with self.subTest(status_line=line):
                tp = self._md(line)
                a = self.orch.dispatch(Task("armchair-commander", {"task_path": tp}))
                self.assertIsNotNone(a)
                self.assertEqual(len(self.mb.receive("armchair-commander")), sent)
                self.ledger.root.joinpath("armchair-commander.json").unlink()

    def test_poison_quarantine(self):
        a = A.Assignment(task_id="armchair-commander", agent_id="armchair-commander",
                         corr_id="c", kind="ai-local", state=A.FAILED, refail_count=3)
        self.ledger.save(a)
        self.assertIsNone(self.orch.dispatch(Task("armchair-commander")))  # not retried
        self.assertEqual(self.ledger.load("armchair-commander").state, A.AWAITING_HUMAN)
        self.assertTrue(self.sent)   # human was pinged

    def test_capacity_limit_never_poisons(self):
        # a usage/rate limit is not the task's fault: even at/over the poison
        # threshold it must NOT be quarantined — it re-dispatches once capacity
        # returns (past the capacity cooldown), without a human ping.
        a = A.Assignment(task_id="armchair-commander", agent_id="armchair-commander",
                         corr_id="c", kind="ai-local", state=A.FAILED, refail_count=5,
                         capacity_limited=True)
        self.ledger.save(a)
        self.assertIsNone(self.orch.dispatch(Task("armchair-commander")))  # cooling down
        self.assertEqual(self.ledger.load("armchair-commander").state, A.FAILED)  # not quarantined
        self.assertFalse(self.sent)                                        # no poison ping
        future = _parse_ts(a.updated_ts) + self.orch.capacity_cooldown_s + 1
        red = self.orch.dispatch(Task("armchair-commander"), now=future)
        self.assertIsNotNone(red)                                          # re-dispatched
        self.assertEqual(red.refail_count, 5)                              # refail NOT bumped

    def test_checkout_busy_never_poisons(self):
        # The checkout guard held the run back because a LIVE sibling owned the
        # shared working tree. Nothing about the TASK failed — no stage ran — so
        # even well past the poison threshold it must not be quarantined, and it
        # re-checks on the flat poll cooldown rather than the escalating fail
        # backoff. Otherwise a busy sibling can quarantine a healthy task.
        a = A.Assignment(task_id="armchair-commander", agent_id="armchair-commander",
                         corr_id="c", kind="ai-local", state=A.FAILED, refail_count=5,
                         checkout_busy=True)
        self.ledger.save(a)
        self.assertIsNone(self.orch.dispatch(Task("armchair-commander")))  # cooling down
        self.assertEqual(self.ledger.load("armchair-commander").state, A.FAILED)
        self.assertFalse(self.sent)                                        # no poison ping
        future = _parse_ts(a.updated_ts) + self.orch.job_poll_cooldown_s + 1
        red = self.orch.dispatch(Task("armchair-commander"), now=future)
        self.assertIsNotNone(red)                                          # re-dispatched
        self.assertEqual(red.refail_count, 5)                              # refail NOT bumped

    def test_checkout_busy_reply_sets_flag(self):
        a = self.orch.dispatch(Task("armchair-commander"))
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="armchair-commander",
                                task_id=a.task_id, corr_id=a.corr_id,
                                payload={"reason": "deferred — HEAD is on worker/other",
                                         "checkout_busy": True}))
        self.orch.collect()
        self.assertTrue(self.ledger.load("armchair-commander").checkout_busy)

    def test_a_real_failure_clears_the_checkout_busy_flag(self):
        # The flag describes the LATEST reply only. A run that was held, then ran
        # for real and failed, must go back to normal refail/poison accounting —
        # otherwise one deferral would make a task permanently unpoisonable.
        a = self.orch.dispatch(Task("armchair-commander"))
        corr = a.corr_id
        for payload in ({"reason": "held", "checkout_busy": True},
                        {"reason": "actually broke"}):
            self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL,
                                    frm="armchair-commander", task_id=a.task_id,
                                    corr_id=corr, payload=payload))
            self.orch.collect()
            got = self.ledger.load("armchair-commander")
            # collect() re-dispatches a failed ai task, which rotates corr_id —
            # replying with the stale one would be dropped as a mismatch.
            corr = got.corr_id
        self.assertFalse(got.checkout_busy)

    def test_ledger_json_without_the_flag_still_loads(self):
        # Every assignment written before this field existed lacks the key;
        # Assignment.from_json splats the dict, so a missing default would raise.
        raw = json.dumps({"task_id": "t", "agent_id": "a", "corr_id": "c",
                          "state": A.FAILED})
        self.assertFalse(A.Assignment.from_json(raw).checkout_busy)

    def test_ledger_json_with_an_unknown_key_still_loads(self):
        # The other direction of the restart window: a NEWER daemon wrote a field
        # this build doesn't know. cls(**data) would TypeError — and Ledger.load/
        # all() swallow that as corrupt, silently dropping the row from every
        # sweep. Unknown keys must be dropped, not splatted.
        raw = json.dumps({"task_id": "t", "agent_id": "a", "corr_id": "c",
                          "state": A.FAILED, "field_from_the_future": True})
        a = A.Assignment.from_json(raw)
        self.assertEqual(a.task_id, "t")
        self.assertEqual(a.state, A.FAILED)
        self.assertFalse(hasattr(a, "field_from_the_future"))
        self.assertEqual(a.last_alert_kind, "")   # new field defaults to benign-empty

    def test_capacity_limit_reply_sets_flag(self):
        a = self.orch.dispatch(Task("armchair-commander"))
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="armchair-commander",
                                task_id=a.task_id, corr_id=a.corr_id,
                                payload={"reason": "usage limit", "capacity_limited": True}))
        self.orch.collect()
        self.assertTrue(self.ledger.load(a.task_id).capacity_limited)

    def test_budget_cap(self):
        self.assertFalse(self.orch.budget_exceeded())   # no runs yet
        runs = self.ledger.root.parent / "runs"
        runs.mkdir(parents=True, exist_ok=True)
        import datetime as _dt
        day = _dt.datetime.now().strftime("%Y%m%d")
        (runs / f"{day}T010101-x").mkdir()
        self.orch.daily_run_budget = 1
        self.assertTrue(self.orch.budget_exceeded())

    def test_tick_usage_hold_blocks_dispatch(self):
        blocked = {"allowed": False, "reason": "weekly 90% >= hard cap 80%"}
        # _INHERIT deliberately leaves the real `usage_hold()` in place: this test is
        # *about* the usage_hold() -> usage_guard.check() seam (see the "hard cap"
        # assertion below), so it injects one level deeper instead of short-circuiting
        # it. The other five machine-state reads are still stubbed by the helper — the
        # old inline `heavy` tuple missed reconcile_parents(), so this test used to read
        # the live Todo/todo.db.
        with mock.patch.object(usage_guard, "check", return_value=blocked):
            r = _hermetic_tick(self.orch, [Task("armchair-commander")],
                               usage_hold=_INHERIT)
        self.assertEqual(r["dispatched"], [])            # dispatch held
        self.assertIn("hard cap", r["usage_hold"])
        self.assertFalse(self.mb.receive("armchair-commander"))  # nothing sent

    def test_usage_hold_open_and_fail_open(self):
        from todo_system import usage_guard
        with mock.patch.object(usage_guard, "check",
                               return_value={"allowed": True}):
            self.assertIsNone(self.orch.usage_hold())
        # a guard crash must not hold the tick (fail-open)
        with mock.patch.object(usage_guard, "check",
                               side_effect=RuntimeError("boom")):
            self.assertIsNone(self.orch.usage_hold())

    def test_per_agent_transport(self):
        from todo_system.orchestrator.mailbox import FileMailbox
        # an agent on the orchestrator's own transport -> reuse self.mb
        same = Agent(id="x", kind="ai-local")   # default mailbox == default_uri
        self.assertIs(self.orch._mailbox_for(same), self.orch.mb)
        # an agent that declares a different transport -> its own mailbox
        remote = Agent(id="y", kind="ai-remote", mailbox=f"file://{self.tmp}/other")
        mb = self.orch._mailbox_for(remote)
        self.assertIsNot(mb, self.orch.mb)
        self.assertIsInstance(mb, FileMailbox)

    def test_collect_advances_to_done_and_closes_task(self):
        from todo_system.orchestrator import tasks
        closed = []
        orig = tasks.mark_done
        tasks.mark_done = lambda slug, **kw: closed.append(slug)  # close-on-done
        try:
            a = self.orch.dispatch(Task("armchair-commander"))
            for t in (m.ACCEPT, m.RESULT):
                self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=t, frm="armchair-commander",
                                        task_id=a.task_id, corr_id=a.corr_id,
                                        payload={"branch": "agent/x"} if t == m.RESULT else {}))
            updated = self.orch.collect()
        finally:
            tasks.mark_done = orig
        self.assertEqual(self.ledger.load(a.task_id).state, A.DONE)
        self.assertEqual(updated[-1].state, A.DONE)
        self.assertEqual(closed, ["armchair-commander"])   # todo task closed on success

    def test_ai_fail_retries_then_gives_up(self):
        # _retry now re-confirms the todo is still open (#1) before re-dispatching;
        # patch it open so this stays hermetic (Todo/items/ is gitignored → absent
        # in CI anyway, but a live vault could otherwise perturb the retry).
        with mock.patch.object(tasks, "task_status", return_value="open"):
            a = self.orch.dispatch(Task("armchair-commander"))
            # first failure -> bounded re-dispatch (attempts 1 -> 2)
            self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="armchair-commander",
                                    task_id=a.task_id, corr_id=a.corr_id))
            self.orch.collect()
            a2 = self.ledger.load(a.task_id)
            self.assertEqual(a2.state, A.DISPATCHED)
            self.assertEqual(a2.attempts, 2)
            self.assertTrue(self.mb.receive("armchair-commander"))   # re-dispatched
            # second failure -> exhausted, stays failed
            self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="armchair-commander",
                                    task_id=a.task_id, corr_id=a2.corr_id))
            self.orch.collect()
            self.assertEqual(self.ledger.load(a.task_id).state, A.FAILED)

    def test_failed_task_cooldown(self):
        # a task that failed out must not be re-dispatched every tick (the
        # expensive re-processing loop). max_attempts=1 so one FAIL is terminal.
        orch = Orchestrator(self.mb, self.reg, self.ledger,
                            notify=lambda *a, **k: None, max_attempts=1,
                            fail_cooldown_s=3600)
        a = orch.dispatch(Task("armchair-commander"))
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="armchair-commander",
                                task_id=a.task_id, corr_id=a.corr_id))
        orch.collect()
        self.assertEqual(self.ledger.load("armchair-commander").state, A.FAILED)
        self.assertIsNone(orch.dispatch(Task("armchair-commander")))   # cooled down
        future = _parse_ts(self.ledger.load("armchair-commander").updated_ts) + 3601
        self.assertIsNotNone(orch.dispatch(Task("armchair-commander"), now=future))

    def test_ai_needs_input_escalates_to_human(self):
        a = self.orch.dispatch(Task("armchair-commander"))
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.NEEDS_INPUT,
                                frm="armchair-commander", task_id=a.task_id,
                                corr_id=a.corr_id))
        self.orch.collect()
        self.assertEqual(self.ledger.load(a.task_id).state, A.AWAITING_HUMAN)

    def test_human_dispatch_parks_and_nudges(self):
        a = self.orch.dispatch(Task("withdrawal-a2-final-decision",
                                    {"summary": "A2 결정 점검"}))
        self.assertEqual(a.state, A.AWAITING_HUMAN)
        self.assertEqual(len(self.sent), 1)             # one kakao nudge
        self.assertIn("withdrawal-a2", self.sent[0])
        # human is never auto-failed; no TASK_ASSIGN sits unanswered as failure
        self.assertFalse(self.ledger.load(a.task_id).is_terminal)

    def test_reap_stale_in_flight(self):
        orch = Orchestrator(self.mb, self.reg, self.ledger,
                            notify=lambda *a, **k: None, stale_timeout_s=1800)
        a = orch.dispatch(Task("armchair-commander"))
        # The runner must have TAKEN the work for the timeout to mean anything — a
        # message still unacked in the inbox is queued, not stalled, and is exempt
        # (see reap_stale). Ack it to model a runner that picked the task up and
        # then went silent, which is what this timeout exists to catch.
        for agent in self.reg.agents:
            mb = orch._mailbox_for(agent)
            mb.ack(agent.id, mb.receive(agent.id))
        base = _parse_ts(self.ledger.load(a.task_id).updated_ts)
        # The cutoff is DERIVED per row, not the flat stale_timeout_s: an untiered
        # task classifies as `standard` (stage_timeout 3600), so the reaper must
        # wait a full stage + REAP_MARGIN before calling a silent runner a corpse
        # — otherwise a heavy stage running its legitimate length gets reaped and
        # its unpushed commits rewound (mulkko-32). 1800 is only a floor here.
        bound = stages.STAGE_TIMEOUTS["standard"] + stages.REAP_MARGIN   # 4500
        self.assertEqual(orch.reap_stale(now=base + 60), [])          # not yet stale
        self.assertEqual(orch.reap_stale(now=base + bound - 1), [])   # within the stage bound
        reaped = orch.reap_stale(now=base + bound + 1)                # past the derived bound
        self.assertEqual([r.task_id for r in reaped], ["armchair-commander"])
        self.assertEqual(self.ledger.load(a.task_id).state, A.FAILED)

    def test_reap_leaves_humans_parked(self):
        orch = Orchestrator(self.mb, self.reg, self.ledger,
                            notify=lambda *a, **k: None, stale_timeout_s=1)
        a = orch.dispatch(Task("withdrawal-a2-final-decision"))       # human -> awaiting_human
        self.assertEqual(orch.reap_stale(now=_parse_ts(a.created_ts) + 10**6), [])

    def test_renudge_backoff(self):
        a = self.orch.dispatch(Task("withdrawal-a2-final-decision"))
        base = _parse_ts(a.last_nudge_ts)
        # backoff[nudge_index=1] == '3d' -> not due after 1h
        self.assertEqual(self.orch.renudge_due(now=base + 3600), [])
        self.assertEqual(len(self.sent), 1)
        # due after 4d
        self.assertEqual(len(self.orch.renudge_due(now=base + 4 * 86400)), 1)
        self.assertEqual(len(self.sent), 2)

    def test_renudge_reaches_a_human_when_an_ai_parks_the_work(self):
        # AWAITING_HUMAN is "human parked, OR AI escalated for input" — but this
        # loop used to require the ASSIGNEE to be human, so work an AI handed
        # back was skipped on every tick and nobody was ever told.
        # parent-match-app-16b sat 10.4 days that way, code complete and green.
        a = self.orch.dispatch(Task("armchair-commander"))   # -> AI agent
        a.state = A.AWAITING_HUMAN
        a.last_nudge_ts = None
        self.ledger.save(a)
        self.sent.clear()
        base = _parse_ts(a.created_ts)
        # alice's backoff[0] is 1d: nothing at 1h, nudged once past a day
        self.assertEqual(self.orch.renudge_due(now=base + 3600), [])
        self.assertEqual(self.sent, [])
        nudged = self.orch.renudge_due(now=base + 2 * 86400)
        self.assertEqual([n.task_id for n in nudged], ["armchair-commander"])
        self.assertEqual(len(self.sent), 1)


class BackgroundExecTest(unittest.TestCase):
    def test_make_executor_is_background(self):
        ex = make_executor("live-remote")
        self.assertIsInstance(ex, BackgroundClaudeExecutor)
        self.assertEqual(ex.mode, "live-remote")

    def test_missing_repo_guard(self):
        out = BackgroundClaudeExecutor().execute(
            m.Envelope(to="a", type=m.TASK_ASSIGN, task_id="t"),
            Agent(id="a", kind="ai-local", repo=None), progress=lambda _: None)
        self.assertFalse(out.ok)
        self.assertIn("no repo", out.summary)


class CapacityBackoffTest(unittest.TestCase):
    """Consecutive usage/rate-limit deferrals must back off, not spin.

    Regression: a capacity limit deliberately does not escalate refail_count (the
    task is not at fault) and poison quarantine is skipped for it — but the
    re-check stayed flat at capacity_cooldown_s, so a task that hit an exhausted
    weekly quota on every attempt re-dispatched ~40x/day (the daily_run_budget
    ceiling) for days, each run spending a full pipeline to rediscover the limit.
    Observed: ~170 runs of one slug over 5 days.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "mb")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.orch = Orchestrator(self.mb, self.reg, self.ledger, notify=lambda *a, **k: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_capacity_cooldown_escalates_then_caps(self):
        cds = [self.orch._capacity_cooldown(n) for n in range(5)]
        self.assertEqual(cds[0], self.orch.capacity_cooldown_s)   # first re-check unchanged
        self.assertGreater(cds[1], cds[0])                        # then backs off
        self.assertGreater(cds[2], cds[1])
        self.assertLessEqual(max(cds), self.orch.capacity_cooldown_max_s)
        self.assertEqual(cds[-1], self.orch.capacity_cooldown_max_s)

    def test_daily_redispatch_count_is_bounded(self):
        # The property that actually matters: a full day of persistent quota
        # exhaustion must cost a handful of runs, not dozens.
        elapsed, runs = 0.0, 0
        while elapsed < 86400:
            elapsed += self.orch._capacity_cooldown(runs)
            runs += 1
        self.assertLess(runs, 10)          # was 48 at the old flat 30min

    def test_capacity_streak_increments_and_resets(self):
        a = Assignment(task_id="t", agent_id="w", corr_id="c", kind="ai-local",
                       state=A.FAILED, capacity_limited=True, capacity_retry_count=2,
                       refail_count=1)
        self.ledger.save(a)
        # far enough past the (now longer) cooldown that dispatch is allowed
        a.updated_ts = m.now_iso()
        self.ledger.save(a)
        later = _parse_ts(a.updated_ts) + self.orch._capacity_cooldown(2) + 60
        got = self.orch.dispatch(Task("t", {"summary": "x"}), now=later)
        self.assertIsNotNone(got)
        self.assertEqual(got.capacity_retry_count, 3)   # streak grows
        self.assertEqual(got.refail_count, 1)           # task not blamed

    def test_real_failure_clears_the_capacity_streak(self):
        a = Assignment(task_id="t2", agent_id="w", corr_id="c", kind="ai-local",
                       state=A.FAILED, capacity_limited=False, capacity_retry_count=3,
                       refail_count=0, updated_ts=m.now_iso())
        self.ledger.save(a)
        later = _parse_ts(a.updated_ts) + self.orch._fail_cooldown(0) + 60
        got = self.orch.dispatch(Task("t2", {"summary": "x"}), now=later)
        self.assertIsNotNone(got)
        self.assertEqual(got.capacity_retry_count, 0)   # streak reset
        self.assertEqual(got.refail_count, 1)           # real failure escalates

class StaleBoundInvariantTest(unittest.TestCase):
    """The stale-reaper and the runner heartbeat idle-gate must both sit OUTSIDE
    the longest a stage can legitimately run, or they kill live work.

    stages.py owns the per-tier stage timeouts; worker.py owns the heartbeat
    cutoff; the two deliberately do not import each other (a cycle), so the
    relationship they must keep is pinned HERE rather than by construction —
    stages.STAGE_TIMEOUTS / MAX_STAGE_TIMEOUT / REAP_MARGIN vs.
    worker.RUNNER_HEARTBEAT_STALE_S."""

    def _orch(self, **kw):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        return Orchestrator(FileMailbox(tmp / "agents"), Registry([]),
                            Ledger(tmp / "assignments"),
                            notify=lambda *a, **k: None, **kw)

    def test_a_run_is_never_reaped_before_its_own_stage_timeout(self):
        orch = self._orch()
        for tier in stages.STAGE_TIMEOUTS:
            a = Assignment(task_id=f"t-{tier}", agent_id="w", corr_id="c",
                           payload={"model_tier": tier})
            self.assertLess(stages.stage_timeout_for(a.payload),
                            orch._stale_bound_for(a),
                            f"{tier}: reaper bound must exceed the stage timeout")

    def test_stale_bound_floors_at_configured_stale_timeout(self):
        # A large configured stale_timeout_s wins over the (smaller) derived bound:
        # the derived value is a floor on the reaper, never a cap.
        orch = self._orch(stale_timeout_s=99999)
        a = Assignment(task_id="t", agent_id="w", corr_id="c",
                       payload={"model_tier": "light"})
        self.assertEqual(orch._stale_bound_for(a), 99999)

    def test_heartbeat_cutoff_exceeds_the_max_stage_bound(self):
        # The idle-gate re-touches per stage, so its cutoff must clear one whole
        # heaviest stage PLUS the reaper margin, or it kickstarts a live runner.
        self.assertLess(stages.MAX_STAGE_TIMEOUT + stages.REAP_MARGIN,
                        worker.RUNNER_HEARTBEAT_STALE_S)
        self.assertLess(stages.MAX_STAGE_TIMEOUT, worker.RUNNER_HEARTBEAT_STALE_S)


class ReapQueuedExemptionTest(unittest.TestCase):
    """A dispatched-but-not-yet-picked-up task must not be reaped.

    Regression: reap_stale timed out from dispatch regardless of pickup, so with a
    serial runner the SECOND dispatch sat unacked in the inbox and was failed after
    stale_timeout_s while the first task ran normally. Observed 2026-07-29 on
    macp-cleanup-supabase-and-links.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.orch = Orchestrator(self.mb, self.reg, self.ledger,
                                 notify=lambda *a, **k: None, stale_timeout_s=1.0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_queued_task_is_not_reaped(self):
        a = self.orch.dispatch(Task("queued-behind-a-long-run"))
        self.assertIsNotNone(a)
        # Well past even the DERIVED bound (standard stage 7200 + REAP_MARGIN 900),
        # so the only thing sparing this row is the queued exemption, not the clock.
        later = _parse_ts(a.updated_ts) + 20000
        self.assertEqual(self.orch.reap_stale(now=later), [])
        self.assertFalse(self.ledger.load(a.task_id).is_terminal)

    def test_picked_up_then_silent_is_reaped(self):
        a = self.orch.dispatch(Task("taken-then-went-silent"))
        self.mb.ack("w", self.mb.receive("w"))      # runner consumed it, never replied
        # Past the derived cutoff (max(stale_timeout_s, standard 7200 + 900)); the
        # flat stale_timeout_s=1.0 is only a floor now.
        later = _parse_ts(a.updated_ts) + 9000
        reaped = self.orch.reap_stale(now=later)
        self.assertEqual([r.task_id for r in reaped], ["taken-then-went-silent"])
        self.assertEqual(self.ledger.load(a.task_id).state, A.FAILED)


class SerialPerCheckoutTest(unittest.TestCase):
    """Two tasks sharing one checkout must not be in flight at the same time.

    Regression 2026-08-14: leaf 16 and leaf 17 (both ai-stock-trading, both auto)
    were dispatched in the same tick and ran 12 concurrent `claude -p` stages
    against the single ~/Worker/ai-stock-trading clone. Each stage's branch-prep
    (`checkout -f` + `checkout -B`) and `git clean -fd` clobbered the other's:
    leaf 17's completed implementation was reset off its branch seconds after it
    landed, and leaf 16's long_job artifact was deleted. stages.py already
    documented the "runs are serial per repo" invariant — nothing enforced it.
    """

    REPO = "https://github.com/example-org/ai-stock-trading.git"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.orch = Orchestrator(self.mb, self.reg, self.ledger,
                                 notify=lambda *a, **k: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tick(self, tasks, **kw):
        return _hermetic_tick(self.orch, tasks, **kw)

    def test_same_repo_tasks_do_not_dispatch_together(self):
        t16 = Task("leaf-16", {"repo": self.REPO})
        t17 = Task("leaf-17", {"repo": self.REPO})
        r = self._tick([t16, t17])
        self.assertEqual([a.task_id for a in r["dispatched"]], ["leaf-16"])
        self.assertIsNone(self.ledger.load("leaf-17"))   # held back, still open

    def test_held_task_dispatches_once_checkout_frees(self):
        self._tick([Task("leaf-16", {"repo": self.REPO})])
        a = self.ledger.load("leaf-16")
        a.state = A.DONE                     # first run finished -> checkout free
        self.ledger.save(a)
        r = self._tick([Task("leaf-17", {"repo": self.REPO})])
        self.assertEqual([x.task_id for x in r["dispatched"]], ["leaf-17"])

    def test_different_repos_still_run_in_parallel(self):
        r = self._tick([Task("leaf-16", {"repo": self.REPO}),
                        Task("other", {"repo": "https://github.com/example-org/DripCoffee.git"})])
        self.assertEqual(sorted(a.task_id for a in r["dispatched"]), ["leaf-16", "other"])

    def test_vault_tasks_share_one_checkout(self):
        # no `repo` == runs in the vault, which is also a single working tree
        r = self._tick([Task("vault-a"), Task("vault-b")])
        self.assertEqual([a.task_id for a in r["dispatched"]], ["vault-a"])

    def test_parked_assignment_does_not_hold_the_checkout(self):
        # leaf 17 ended awaiting_human (blocked_on). It owns no processes, so it
        # must not wedge the repo for every other task indefinitely.
        self.ledger.save(A.Assignment(task_id="leaf-17", agent_id="w", corr_id="c",
                                      kind="ai-local", state=A.AWAITING_HUMAN,
                                      payload={"repo": self.REPO}))
        r = self._tick([Task("leaf-16", {"repo": self.REPO})])
        self.assertEqual([a.task_id for a in r["dispatched"]], ["leaf-16"])


class SerialPerCheckoutUnderUsageHoldTest(SerialPerCheckoutTest):
    """위 5건의 판정이 구독 quota hold 여부와 **무관**함을 고정한다.

    그 5건이 시험하는 것은 checkout 직렬화 규칙이다 — usage guard 가 hold 를 걸었는지와는
    아무 상관이 없어야 한다. 그런데 `_tick` 이 hold 를 주입하지 않던 시절엔 tick() 이 라이브
    `usage_guard.check()`(macOS Keychain 토큰 + api.anthropic.com 실네트워크)를 읽었고,
    이 맥의 구독 사용량이 hard cap 을 넘은 동안에는 `runnable=[]` 이 되어 5건이 통째로
    빨개졌다 — 코드가 아니라 **머신 상태**가 판정을 뒤집은 것이다.

    guard 를 blocked 로 강제 고정한 채 부모 5건을 상속 재실행해서 판정 불변을 증명한다.
    테스트를 복제하지 않고 상속하므로 부모가 늘면 이 클래스도 자동으로 함께 늘어난다.
    """

    def _tick(self, tasks, **kw):
        blocked = {"allowed": False, "reason": "forced hold (test)"}
        with mock.patch.object(usage_guard, "check", return_value=blocked):
            return super()._tick(tasks, **kw)


class CanRunConcurrentlyTest(unittest.TestCase):
    """The pure overlap predicate (static — no Orchestrator instance needed).

    Two same-checkout tasks are concurrency-eligible ONLY when both declare a
    `touches` glob list, the lists are disjoint, and neither depends_on the
    other. Anything else -> serialise. The predicate is dormant at the default
    cap of 1; these pin its branches directly so leaf 5 raises the cap against a
    known-good judge.
    """

    def _p(self, a_slug, a, b_slug, b):
        return Orchestrator._can_run_concurrently(a_slug, a, b_slug, b)

    def test_disjoint_touches_no_dep_is_eligible(self):
        self.assertTrue(self._p(
            "a", {"touches": ["app/src/main/**"]},
            "b", {"touches": ["app/src/test/**"]}))

    def test_overlapping_touches_serialises(self):
        # "a/x.py" lives under "a/**": nested -> overlap -> serial
        self.assertFalse(self._p(
            "a", {"touches": ["a/**"]},
            "b", {"touches": ["a/x.py"]}))

    def test_leading_wildcard_overlaps_anything(self):
        # no static prefix -> can reach any file -> never disjoint
        self.assertFalse(self._p(
            "a", {"touches": ["**/Thing.kt"]},
            "b", {"touches": ["unrelated/dir/**"]}))

    def test_missing_touches_on_either_side_serialises(self):
        self.assertFalse(self._p("a", {}, "b", {"touches": ["b/**"]}))
        self.assertFalse(self._p("a", {"touches": ["a/**"]}, "b", {}))
        self.assertFalse(self._p("a", {}, "b", {}))

    def test_empty_or_bare_string_touches_serialises(self):
        self.assertFalse(self._p(
            "a", {"touches": []}, "b", {"touches": ["b/**"]}))
        self.assertFalse(self._p(
            "a", {"touches": "a/**"}, "b", {"touches": ["b/**"]}))  # bare str

    def test_depends_on_edge_serialises_both_directions(self):
        # disjoint touches, yet a depends_on edge forbids concurrency either way
        self.assertFalse(self._p(
            "a", {"touches": ["a/**"], "depends_on": ["b"]},
            "b", {"touches": ["b/**"]}))
        self.assertFalse(self._p(
            "a", {"touches": ["a/**"]},
            "b", {"touches": ["b/**"], "depends_on": ["a"]}))

    def test_static_prefix_and_overlap_helpers(self):
        self.assertEqual(_static_prefix("a/b/*.py"), "a/b")
        self.assertEqual(_static_prefix("a/b.py"), "a/b.py")
        self.assertEqual(_static_prefix("**/x"), "")
        # a sibling name must NOT be read as an ancestor (the trailing-/ guard)
        self.assertFalse(_globs_overlap("a/b/**", "a/bc/**"))
        self.assertTrue(_globs_overlap("a/b/**", "a/b/c.py"))
        self.assertTrue(_touches_disjoint(["a/**"], ["b/**"]))
        self.assertFalse(_touches_disjoint(["a/**", "z/**"], ["z/q.py"]))


class HoldReasonTest(unittest.TestCase):
    """The pure classifier behind a deferred row's '사유: …' note. Mirrors the
    dispatch guard: cap first, else the first sibling `_can_run_concurrently`
    rejects, named in that predicate's own order (empty touches > depends_on >
    real overlap). No Orchestrator instance needed."""

    def _held(self, slug, **payload):
        return Assignment(task_id=slug, agent_id="w", corr_id="c",
                          payload=dict(payload))

    def _r(self, t_payload, held, cap):
        return Orchestrator._hold_reason("t", t_payload, held, cap)

    def test_cap_reached_names_the_cap(self):
        # len(held) >= cap wins even though both siblings are disjoint from t
        held = [self._held("a", touches=["a/**"]), self._held("b", touches=["b/**"])]
        self.assertEqual(self._r({"touches": ["c/**"]}, held, cap=2),
                         "상한 도달 — repo당 동시 실행 최대 2개")

    def test_missing_touches_either_side_is_undetermined(self):
        self.assertEqual(self._r({}, [self._held("a", touches=["a/**"])], cap=2),
                         "겹침 미판정 (touches 없음)")
        self.assertEqual(self._r({"touches": ["a/**"]}, [self._held("a")], cap=2),
                         "겹침 미판정 (touches 없음)")

    def test_depends_on_edge_is_named(self):
        held = [self._held("a", touches=["a/**"])]
        self.assertEqual(
            self._r({"touches": ["b/**"], "depends_on": ["a"]}, held, cap=2),
            "depends_on 직렬")

    def test_real_overlap_is_named(self):
        held = [self._held("a", touches=["a/**"])]
        self.assertEqual(self._r({"touches": ["a/x.py"]}, held, cap=2), "겹침 있음")

    def test_reason_comes_from_the_first_blocking_sibling(self):
        # below cap (2 < 3); a concurrency-eligible lead is skipped, the reason
        # is the first INELIGIBLE sibling's (here: no touches)
        held = [self._held("a", touches=["a/**"]),      # disjoint from t -> eligible
                self._held("b")]                         # no touches -> blocks
        self.assertEqual(self._r({"touches": ["z/**"]}, held, cap=3),
                         "겹침 미판정 (touches 없음)")


class SerialPerCheckoutCapTest(unittest.TestCase):
    """max_parallel_per_repo > 1: disjoint-touches same-repo tasks may run
    together, up to the cap; everything the predicate rejects stays serial.

    This covers the concurrency path leaf 5 will enable. The default cap of 1
    keeps it dormant (SerialPerCheckoutTest passing unmodified is that proof);
    here the cap is forced to 2 so BOTH dispatch-guard branches are exercised.
    """

    REPO = "https://github.com/example-org/ai-stock-trading.git"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.orch = Orchestrator(self.mb, self.reg, self.ledger,
                                 notify=lambda *a, **k: None,
                                 max_parallel_per_repo=2)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tick(self, tasks, **kw):
        return _hermetic_tick(self.orch, tasks, **kw)

    def _touch(self, slug, globs, **extra):
        payload = {"repo": self.REPO, "touches": globs}
        payload.update(extra)
        return Task(slug, payload)

    def test_disjoint_touches_run_concurrently_up_to_cap(self):
        r = self._tick([self._touch("leaf-a", ["a/**"]),
                        self._touch("leaf-b", ["b/**"])])
        self.assertEqual(sorted(x.task_id for x in r["dispatched"]),
                         ["leaf-a", "leaf-b"])

    def test_cap_holds_the_third_disjoint_task(self):
        r = self._tick([self._touch("leaf-a", ["a/**"]),
                        self._touch("leaf-b", ["b/**"]),
                        self._touch("leaf-c", ["c/**"])])
        self.assertEqual(sorted(x.task_id for x in r["dispatched"]),
                         ["leaf-a", "leaf-b"])
        self.assertIsNone(self.ledger.load("leaf-c"))   # cap(2) reached -> held

    def test_overlapping_touches_serialise_below_cap(self):
        # len(held)=1 < cap=2, but the globs overlap -> predicate False -> held
        r = self._tick([self._touch("leaf-a", ["a/**"]),
                        self._touch("leaf-b", ["a/x.py"])])
        self.assertEqual([x.task_id for x in r["dispatched"]], ["leaf-a"])
        self.assertIsNone(self.ledger.load("leaf-b"))

    def test_depends_on_edge_serialises_below_cap(self):
        r = self._tick([self._touch("leaf-a", ["a/**"]),
                        self._touch("leaf-b", ["b/**"], depends_on=["leaf-a"])])
        self.assertEqual([x.task_id for x in r["dispatched"]], ["leaf-a"])
        self.assertIsNone(self.ledger.load("leaf-b"))

    def test_tasks_without_touches_stay_serial_even_above_cap(self):
        # the existing payload shape (no `touches`) stays dormant even at cap 2
        r = self._tick([Task("leaf-a", {"repo": self.REPO}),
                        Task("leaf-b", {"repo": self.REPO})])
        self.assertEqual([x.task_id for x in r["dispatched"]], ["leaf-a"])
        self.assertIsNone(self.ledger.load("leaf-b"))


class ConcurrentDispatchE2ETest(unittest.TestCase):
    """leaf 5 e2e (AC4), one flow threading dispatch → hold → note → dashboard.

    Uses the DEFAULT-constructed Orchestrator (NO max_parallel_per_repo kwarg) —
    that is the switch this leaf flipped, so concurrency must be ON with no
    explicit cap. Two disjoint-`touches` same-repo `auto` tasks dispatch together
    in one tick (two in-flight rows the dashboard renders as two 진행-중 lines); a
    third same-repo task is held on the cap, its reason recorded on the deferred
    row and rendered in the deferred section. Hermetic: no real git/claude."""

    REPO = "https://github.com/example-org/ai-stock-trading.git"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.orch = Orchestrator(self.mb, self.reg, self.ledger,
                                 notify=lambda *a, **k: None)   # default cap (now 2)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, slug, globs):
        return Task(slug, {"repo": self.REPO, "touches": globs})

    def _section(self, md: str, header: str) -> str:
        return md.split(header, 1)[1].split("\n## ", 1)[0]

    def test_dispatch_hold_note_dashboard_in_one_flow(self):
        # default cap is 2 -> both disjoint same-repo tasks go in ONE tick
        r = _hermetic_tick(self.orch, [self._touch("leaf-a", ["a/**"]),
                                       self._touch("leaf-b", ["b/**"])])
        self.assertEqual(sorted(x.task_id for x in r["dispatched"]),
                         ["leaf-a", "leaf-b"])
        inflight = sorted(a.task_id for a in self.ledger.all()
                          if a.state in (A.DISPATCHED, A.ACCEPTED, A.IN_PROGRESS))
        self.assertEqual(inflight, ["leaf-a", "leaf-b"])      # two concurrent runs

        md = dashboard.render(self.ledger, self.reg)
        self.assertIn("active 2", md)
        running = self._section(md, "## 🏃 진행 중")
        self.assertIn("leaf-a", running)
        self.assertIn("leaf-b", running)

        # a third same-repo task now hits the cap. Seed its deferred row (the shape
        # a runner checkout_busy FAIL leaves) so _note_checkout_hold rewrites it.
        self.ledger.save(A.Assignment(
            task_id="leaf-c", agent_id="w", corr_id="c", kind="ai-local",
            state=A.FAILED, checkout_busy=True,
            payload={"repo": self.REPO, "touches": ["c/**"]},
            last_note="deferred — held by a LIVE sibling run"))
        self.assertEqual(
            self.orch._dispatch_serial_per_checkout([self._touch("leaf-c", ["c/**"])]),
            [])                                               # cap reached -> held
        note = self.ledger.load("leaf-c").last_note
        self.assertIn("사유: 상한 도달 — repo당 동시 실행 최대 2개", note)

        deferred = self._section(dashboard.render(self.ledger, self.reg),
                                 "## 💤 대기 (deferred")
        self.assertIn("leaf-c", deferred)
        self.assertIn("상한 도달 — repo당 동시 실행 최대 2개", deferred)


class GlobalGradleBuildCapTest(unittest.TestCase):
    """max_parallel_gradle_builds: a dispatch-time ceiling on concurrent
    gradle-capable runs across ALL checkouts — the memory guard
    ([[runner-cgroup-memhigh-freeze-orphan-gradle]]), SEPARATE from the per-repo
    cap and from usage_guard's quota slots, double-counting neither. Dormant at
    the default 0. `_is_gradle_checkout` is patched so no real clone is needed —
    the two example repos count as gradle, everything else (the vault) does not.
    """

    GRADLE_A = "https://github.com/example-org/if-then-habits.git"
    GRADLE_B = "https://github.com/example-org/DripCoffee.git"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.orch = self._make_orch(gradle_cap=0)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_orch(self, gradle_cap):
        return Orchestrator(self.mb, self.reg, self.ledger,
                            notify=lambda *a, **k: None,
                            max_parallel_gradle_builds=gradle_cap)

    def _tick(self, tasks, **kw):
        gradle = {self.GRADLE_A, self.GRADLE_B}
        with mock.patch.object(self.orch, "_is_gradle_checkout",
                               side_effect=lambda key: key in gradle):
            return _hermetic_tick(self.orch, tasks, **kw)

    def test_default_zero_lets_two_gradle_repos_run_together(self):
        # dormant: proves no regression vs today's parallel-different-repo dispatch
        r = self._tick([Task("a", {"repo": self.GRADLE_A}),
                        Task("b", {"repo": self.GRADLE_B})])
        self.assertEqual(sorted(x.task_id for x in r["dispatched"]), ["a", "b"])

    def test_cap_one_holds_the_second_gradle_repo(self):
        self.orch = self._make_orch(gradle_cap=1)
        r = self._tick([Task("a", {"repo": self.GRADLE_A}),
                        Task("b", {"repo": self.GRADLE_B})])
        self.assertEqual([x.task_id for x in r["dispatched"]], ["a"])
        self.assertIsNone(self.ledger.load("b"))       # held open, runnable next tick

    def test_cap_one_still_lets_a_non_gradle_vault_task_through(self):
        # the cap gates ONLY gradle checkouts — a vault task dispatches in the SAME
        # tick a 2nd gradle repo is held (proof it is separate from per-repo/usage)
        self.orch = self._make_orch(gradle_cap=1)
        r = self._tick([Task("a", {"repo": self.GRADLE_A}),
                        Task("b", {"repo": self.GRADLE_B}),
                        Task("vault-x")])
        self.assertEqual(sorted(x.task_id for x in r["dispatched"]), ["a", "vault-x"])
        self.assertIsNone(self.ledger.load("b"))

    def test_held_gradle_task_dispatches_once_the_first_frees(self):
        self.orch = self._make_orch(gradle_cap=1)
        self._tick([Task("a", {"repo": self.GRADLE_A})])
        done = self.ledger.load("a")
        done.state = A.DONE                            # first gradle run reaches terminal
        self.ledger.save(done)
        r = self._tick([Task("b", {"repo": self.GRADLE_B})])
        self.assertEqual([x.task_id for x in r["dispatched"]], ["b"])


class GlobalGradleBuildCapUnderUsageHoldTest(GlobalGradleBuildCapTest):
    """위 판정이 구독 quota hold 여부와 **무관**함을 고정한다 — gradle 메모리 cap 은
    usage_guard 슬롯과 별개 레이어다. guard 를 blocked 로 강제 고정한 채 부모 전건을
    상속 재실행해 판정 불변을 증명한다(SerialPerCheckoutUnderUsageHoldTest 와 같은 꼴)."""

    def _tick(self, tasks, **kw):
        blocked = {"allowed": False, "reason": "forced hold (test)"}
        with mock.patch.object(usage_guard, "check", return_value=blocked):
            return super()._tick(tasks, **kw)


class GradleCheckoutDetectTest(unittest.TestCase):
    """`_is_gradle_checkout`: vault/non-URL keys and a not-yet-cloned repo are not
    gradle (conservative); a URL whose shared clone has a `gradlew` is."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.orch = Orchestrator(FileMailbox(self.tmp / "agents"),
                                 Registry([Agent(id="w", kind="ai-local",
                                                 projects=["*"], repo="~/x")]),
                                 Ledger(self.tmp / "assignments"),
                                 notify=lambda *a, **k: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_vault_and_empty_keys_are_not_gradle(self):
        from todo_system.orchestrator.orchestrator import _VAULT_CHECKOUT
        self.assertFalse(self.orch._is_gradle_checkout(_VAULT_CHECKOUT))
        self.assertFalse(self.orch._is_gradle_checkout(""))

    def test_url_is_gradle_only_when_its_clone_has_gradlew(self):
        url = "https://github.com/example-org/some-app.git"
        clone = self.tmp / "clone"
        with mock.patch.object(repos, "local_path", return_value=clone):
            self.assertFalse(self.orch._is_gradle_checkout(url))   # not cloned yet → False
            clone.mkdir(parents=True)
            (clone / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
            self.assertTrue(self.orch._is_gradle_checkout(url))


class RetryReconfirmTest(unittest.TestCase):
    """#1 + #2 — _retry must re-confirm the todo is still open before re-dispatch,
    and send over the AGENT'S transport, not the default mailbox.

    Regressions: ai-stock-trading-5h merged (PR #19) and was closed, yet a stale
    FAIL drove _retry the next morning and re-ran a whole pipeline into a usage
    limit (dispatch's DONE guard is bypassed on the collect→_retry path). And a
    remote git:// agent's retry went to the local default mailbox and never
    arrived (dispatch used _mailbox_for; _retry used self.mb)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.orch = Orchestrator(self.mb, self.reg, self.ledger, notify=lambda *a, **k: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _failed(self, agent_id="w", kind="ai-local", **kw):
        a = Assignment(task_id="t", agent_id=agent_id, corr_id="C1",
                       state=A.FAILED, kind=kind, attempts=1, **kw)
        self.ledger.save(a)
        return a

    def _tick(self, tasks, **kw):
        return _hermetic_tick(self.orch, tasks, **kw)

    def test_retry_skips_and_closes_when_todo_done(self):
        a = self._failed()
        with mock.patch.object(tasks, "task_status", return_value="done"):
            self.orch._retry(a)
        self.assertEqual(a.state, A.DONE)            # ledger closed, not re-dispatched
        self.assertEqual(a.attempts, 1)             # attempts NOT bumped
        self.assertTrue(any(h["type"] == "retry-skipped-closed" for h in a.history))
        self.assertEqual(self.mb.receive("w"), [])   # nothing sent to the agent

    def test_bypass_guard_dispatches_through_a_usage_hold(self):
        urgent = Task("armchair-commander", payload={"bypass_guard": True})
        normal = Task("worldgraph")
        out = self._tick([urgent, normal], usage_hold="quota pacing")
        self.assertEqual([a.task_id for a in out["dispatched"]], ["armchair-commander"])

    def test_bypass_guard_does_not_override_an_explicit_pause(self):
        # a pause is a human saying stop; a task file must not countermand it
        urgent = Task("armchair-commander", payload={"bypass_guard": True})
        out = self._tick([urgent], usage_hold=None, paused=True)
        self.assertEqual(out["dispatched"], [])

    def test_without_the_flag_a_hold_still_stops_everything(self):
        out = self._tick([Task("armchair-commander")], usage_hold="quota pacing")
        self.assertEqual(out["dispatched"], [])

    def test_blocked_reply_parks_instead_of_retrying(self):
        # bean-cards-2 (2026-08-05): verify measured origin/main, found the
        # upstream PR unmerged, created no branch and said so in `blocked_on`.
        # Retrying re-bought research+plan+implement against the same gap.
        a = self.orch.dispatch(Task("armchair-commander"))
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="armchair-commander",
                                task_id=a.task_id, corr_id=a.corr_id,
                                payload={"blocked_on": "upstream-task", "verify": "FAIL"}))
        with mock.patch.object(self.orch, "_retry") as retry:
            self.orch.collect()
        retry.assert_not_called()
        reloaded = self.ledger.load(a.task_id)
        self.assertEqual(reloaded.state, A.AWAITING_HUMAN)
        self.assertIn("upstream-task", reloaded.last_note)
        # AWAITING_HUMAN is not terminal, so dispatch() will not re-send it
        self.assertIsNone(self.orch.dispatch(Task(a.task_id)))

    def test_plain_verify_failure_still_retries(self):
        # the escape hatch is the named blocker, not "any failure"
        a = self.orch.dispatch(Task("armchair-commander"))
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="armchair-commander",
                                task_id=a.task_id, corr_id=a.corr_id,
                                payload={"verify": "FAIL"}))
        with mock.patch.object(self.orch, "_retry") as retry:
            self.orch.collect()
        retry.assert_called_once()

    def _timeout_fail(self, corr_id):
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="w",
                                task_id="t", corr_id=corr_id,
                                payload={"timed_out": True, "timeout_stage": "implement"}))

    def test_first_timeout_retries_second_parks(self):
        # A heavy task that keeps hitting the stage cap must not re-dispatch into the
        # same wall forever. First timeout retries (resumes on origin/<branch>); the
        # second parks for a human. Work is safe on origin (checkpoint_push).
        a = self.orch.dispatch(Task("t"))
        self._timeout_fail(a.corr_id)
        with mock.patch.object(tasks, "task_status", return_value="open"):
            self.orch.collect()                          # attempt 1 → _retry
        after1 = self.ledger.load("t")
        self.assertEqual(after1.state, A.DISPATCHED)     # retried, not parked
        self.assertEqual(after1.timeout_streak, 1)
        self._timeout_fail(after1.corr_id)               # attempt 2 → same wall
        with mock.patch.object(self.orch, "_retry") as retry, \
                mock.patch.object(tasks, "task_status", return_value="open"):
            self.orch.collect()
        retry.assert_not_called()                        # NOT re-dispatched a 3rd time
        after2 = self.ledger.load("t")
        self.assertEqual(after2.state, A.AWAITING_HUMAN)
        self.assertEqual(after2.timeout_streak, 2)
        self.assertEqual(after2.last_alert_kind, ALERT_TIMEOUT_PARK)
        self.assertIsNone(self.orch.dispatch(Task("t")))   # park is absolute

    def test_a_non_timeout_failure_resets_the_streak(self):
        # a timeout, then an ordinary failure: the streak resets so unrelated
        # failures never accumulate into a spurious park.
        self._failed(payload={"branch": "worker/t"}, timeout_streak=1)
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="w",
                                task_id="t", corr_id="C1", payload={"verify": "FAIL"}))
        with mock.patch.object(self.orch, "_retry"), \
                mock.patch.object(tasks, "task_status", return_value="open"):
            self.orch.collect()
        self.assertEqual(self.ledger.load("t").timeout_streak, 0)

    def test_a_successful_result_resets_the_streak(self):
        self._failed(payload={"branch": "worker/t"}, timeout_streak=1)
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.RESULT, frm="w",
                                task_id="t", corr_id="C1", payload={"branch": "worker/t"}))
        with mock.patch.object(self.orch, "_close_task"):
            self.orch.collect()
        self.assertEqual(self.ledger.load("t").timeout_streak, 0)

    def test_timeout_streak_survives_a_cooldown_redispatch(self):
        # consecutive timeouts across the dispatch() cooldown path (not the in-place
        # _retry) must still accumulate toward the park.
        a = self._failed(payload={"branch": "worker/t"}, timeout_streak=1, refail_count=1)
        future = _parse_ts(a.updated_ts) + self.orch._fail_cooldown(1) + 60
        got = self.orch.dispatch(Task("t"), now=future)
        self.assertIsNotNone(got)
        self.assertEqual(got.timeout_streak, 1)          # carried forward, not reset

    def test_retry_skips_when_todo_cancelled(self):
        a = self._failed()
        with mock.patch.object(tasks, "task_status", return_value="cancelled"):
            self.orch._retry(a)
        self.assertEqual(a.state, A.DONE)
        self.assertEqual(self.mb.receive("w"), [])

    def test_retry_redispatches_when_todo_open(self):
        a = self._failed(payload={"branch": "worker/t"})
        old = a.corr_id
        with mock.patch.object(tasks, "task_status", return_value="open"):
            self.orch._retry(a)
        self.assertEqual(a.state, A.DISPATCHED)
        self.assertEqual(a.attempts, 2)
        self.assertNotEqual(a.corr_id, old)          # fresh conversation
        [env] = self.mb.receive("w")
        self.assertEqual(env.type, m.TASK_ASSIGN)
        self.assertEqual(env.corr_id, a.corr_id)

    def test_retry_fail_open_when_status_unknown(self):
        # a missing todo / None status must NOT strand a genuinely-failed task
        a = self._failed()
        with mock.patch.object(tasks, "task_status", return_value=None):
            self.orch._retry(a)
        self.assertEqual(a.state, A.DISPATCHED)
        self.assertEqual(a.attempts, 2)

    def test_retry_routes_to_agent_transport_not_default(self):
        remote_root = self.tmp / "remote"
        reg = Registry([Agent(id="sc1", kind="ai-remote", projects=["*"],
                              mailbox=f"file://{remote_root}")])
        orch = Orchestrator(self.mb, reg, self.ledger, notify=lambda *a, **k: None)
        a = Assignment(task_id="t", agent_id="sc1", corr_id="C1",
                       state=A.FAILED, kind="ai-remote", attempts=1)
        self.ledger.save(a)
        with mock.patch.object(tasks, "task_status", return_value="open"):
            orch._retry(a)
        self.assertEqual(self.mb.receive("sc1"), [])         # NOT the default mailbox
        [env] = FileMailbox(remote_root).receive("sc1")      # the agent's own transport
        self.assertEqual(env.type, m.TASK_ASSIGN)

    def test_retry_falls_back_to_default_when_agent_missing(self):
        # agent vanished from the registry: reg.get raises KeyError — must not
        # crash collect, fall back to the default mailbox.
        a = self._failed(agent_id="ghost", kind="ai-remote")
        with mock.patch.object(tasks, "task_status", return_value="open"):
            self.orch._retry(a)
        self.assertEqual(a.state, A.DISPATCHED)
        [env] = self.mb.receive("ghost")
        self.assertEqual(env.type, m.TASK_ASSIGN)


class CollectDropWarningTest(unittest.TestCase):
    """#3 — a reply that matches no assignment (or carries a stale corr_id) is
    ACKed and dropped; it must at least be logged. This is the
    reaper-then-late-runner-reply race that silently lost completed-task results."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.orch = Orchestrator(self.mb, self.reg, self.ledger, notify=lambda *a, **k: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _collect_capturing(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            updated = self.orch.collect()
        return updated, buf.getvalue()

    def test_warns_on_corr_id_mismatch_and_preserves_state(self):
        a = Assignment(task_id="t", agent_id="w", corr_id="EXPECTED",
                       state=A.IN_PROGRESS, kind="ai-local")
        self.ledger.save(a)
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, frm="w", type=m.RESULT,
                                task_id="t", corr_id="STALE",
                                payload={"summary": "late reply"}))
        updated, out = self._collect_capturing()
        self.assertEqual(updated, [])
        self.assertIn("corr_id mismatch", out)
        self.assertIn("t", out)
        self.assertEqual(self.ledger.load("t").state, A.IN_PROGRESS)   # untouched
        self.assertEqual(self.mb.receive(m.ORCHESTRATOR), [])          # acked (dropped)

    def test_warns_on_no_assignment(self):
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, frm="w", type=m.RESULT,
                                task_id="ghost", corr_id="X", payload={}))
        updated, out = self._collect_capturing()
        self.assertEqual(updated, [])
        self.assertIn("no assignment on record", out)
        self.assertIn("ghost", out)
        self.assertEqual(self.mb.receive(m.ORCHESTRATOR), [])

    def test_heartbeat_is_not_warned(self):
        # HEARTBEATs are consumed before the assignment lookup and must NOT trip
        # the drop warning (else every idle ping would spam the log).
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, frm="w", type=m.HEARTBEAT,
                                payload={"status": "idle"}))
        _, out = self._collect_capturing()
        self.assertNotIn("dropping", out)


class ReconcileMergedTest(unittest.TestCase):
    """#5 — a task whose PR merged but whose todo stayed open must be auto-closed.
    Previously every such case (4b, 5a, 5e-1/3/4, worldgraph-15 …) was fixed by
    hand."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([
            Agent(id="w", kind="ai-local", projects=["*"], repo="~/x"),
            Agent(id="me", kind="human", projects=["human-*"]),
        ])
        self.orch = Orchestrator(self.mb, self.reg, self.ledger, notify=lambda *a, **k: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _awaiting(self, task_id="t", kind="ai-local", agent_id="w", **payload):
        a = Assignment(task_id=task_id, agent_id=agent_id, corr_id="C1",
                       state=A.AWAITING_HUMAN, kind=kind, payload=payload)
        self.ledger.save(a)
        return a

    def test_closes_merged_open_task(self):
        self._awaiting(branch="worker/t", repo="o/r")
        with mock.patch.object(stages, "gh_pr_merged", return_value=True) as gh, \
             mock.patch.object(self.orch, "_close_task") as close:
            out = self.orch.reconcile_merged()
        gh.assert_called_once_with("worker/t", "o/r")
        close.assert_called_once_with("t")
        self.assertEqual([x.task_id for x in out], ["t"])
        reloaded = self.ledger.load("t")
        self.assertEqual(reloaded.state, A.DONE)
        self.assertTrue(any(h["type"] == "reconciled-merged" for h in reloaded.history))

    def test_clears_the_stale_park_note(self):
        # Orchestrator.md renders last_note verbatim, so a resolved task must not
        # keep advertising the park reason that no longer applies.
        a = self._awaiting(branch="worker/t", repo="o/r")
        a.last_note = "staged pipeline complete — auto_merge did NOT land; needs review"
        self.ledger.save(a)
        with mock.patch.object(stages, "gh_pr_merged", return_value=True), \
             mock.patch.object(self.orch, "_close_task"):
            self.orch.reconcile_merged()
        self.assertNotIn("needs review", self.ledger.load("t").last_note)

    def test_closing_clears_the_deferral_flags(self):
        # A FAILED+job_running row closed here gets no further runner reply, so a
        # flag left set sits on a DONE row forever and worker._runner_idle read it
        # as "busy": 63 deferred runner restarts in 6h (2026-09-26).
        a = Assignment(task_id="t", agent_id="w", corr_id="C1", state=A.FAILED,
                       kind="ai-local", payload={"branch": "worker/t", "repo": "o/r"})
        a.job_running = True
        a.checkout_busy = True
        self.ledger.save(a)
        with mock.patch.object(stages, "gh_pr_merged", return_value=True), \
             mock.patch.object(self.orch, "_close_task"):
            self.orch.reconcile_merged()
        reloaded = self.ledger.load("t")
        self.assertEqual(reloaded.state, A.DONE)
        self.assertFalse(reloaded.job_running)
        self.assertFalse(reloaded.checkout_busy)

    def test_closes_parked_task_whose_todo_is_already_done(self):
        # ks-kmp-5a1: shipped inside a sibling task's PR, so its own branch never
        # had one and gh answers None forever. The closed todo is the authority.
        self._awaiting(branch="worker/t", repo="o/r")
        with mock.patch.object(stages, "gh_pr_merged", return_value=None), \
             mock.patch.object(tasks, "task_status", return_value="done"), \
             mock.patch.object(self.orch, "_close_task"):
            out = self.orch.reconcile_merged()
        self.assertEqual([x.task_id for x in out], ["t"])
        self.assertEqual(self.ledger.load("t").state, A.DONE)

    def test_open_todo_without_a_merged_pr_stays_parked(self):
        self._awaiting(branch="worker/t", repo="o/r")
        with mock.patch.object(stages, "gh_pr_merged", return_value=None), \
             mock.patch.object(tasks, "task_status", return_value="open"), \
             mock.patch.object(self.orch, "_close_task") as close:
            out = self.orch.reconcile_merged()
        close.assert_not_called()
        self.assertEqual(out, [])
        self.assertEqual(self.ledger.load("t").state, A.AWAITING_HUMAN)

    def test_parked_delegate_pr_closed_unmerged_alerts_once(self):
        # A delegate PR a human CLOSED without merging will never merge, so
        # reconcile_merged would hold it AWAITING_HUMAN forever with no new signal.
        # Alert once so a person decides — and only once (park-once guard).
        self._awaiting(branch="worker/t", repo="o/r")
        with mock.patch.object(stages, "gh_pr_merged", return_value=None), \
             mock.patch.object(stages, "gh_pr_state", return_value="CLOSED"), \
             mock.patch.object(tasks, "task_status", return_value="open"), \
             mock.patch.object(self.orch, "_close_task") as close, \
             mock.patch.object(self.orch, "_notify_routed") as alert, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.orch.reconcile_merged(), [])     # parked, not closed
            self.assertEqual(self.orch.reconcile_merged(), [])     # a later tick
        close.assert_not_called()
        self.assertEqual(alert.call_count, 1)                      # not once per tick
        row = self.ledger.load("t")
        self.assertEqual(row.state, A.AWAITING_HUMAN)              # stays parked
        self.assertEqual(row.last_alert_kind, ALERT_CLOSED_UNMERGED)
        self.assertEqual(sum(h["type"] == _CLOSED_UNMERGED for h in row.history), 1)

    def test_parked_delegate_pr_still_open_is_not_alerted(self):
        # OPEN (awaiting a human's merge) is the normal parked state — no alert.
        self._awaiting(branch="worker/t", repo="o/r")
        with mock.patch.object(stages, "gh_pr_merged", return_value=False), \
             mock.patch.object(stages, "gh_pr_state", return_value="OPEN"), \
             mock.patch.object(tasks, "task_status", return_value="open"), \
             mock.patch.object(self.orch, "_notify_routed") as alert, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.orch.reconcile_merged(), [])
        alert.assert_not_called()
        self.assertEqual(self.ledger.load("t").state, A.AWAITING_HUMAN)

    def test_closed_unmerged_alert_is_silent_when_gh_state_unknown(self):
        # gh absent / unauthenticated → gh_pr_state is None: never alert on a
        # guess (the same fail-open None=unknown the rest of the sweep keeps).
        self._awaiting(branch="worker/t", repo="o/r")
        with mock.patch.object(stages, "gh_pr_merged", return_value=None), \
             mock.patch.object(stages, "gh_pr_state", return_value=None), \
             mock.patch.object(tasks, "task_status", return_value="open"), \
             mock.patch.object(self.orch, "_notify_routed") as alert, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.orch.reconcile_merged(), [])
        alert.assert_not_called()

    def _rejected(self, task_id="t"):
        a = Assignment(task_id=task_id, agent_id="w", corr_id="C1",
                       state=A.REJECTED, kind="ai-local",
                       payload={"branch": f"worker/{task_id}", "repo": "o/r"})
        self.ledger.save(a)
        return a

    def test_rejected_assignment_for_a_cancelled_todo_is_swept(self):
        # worldgraph-project-doc-sync: REJECTED is terminal and no further reply
        # ever arrives, so the one path that closes a cancelled todo (_retry, via
        # collect) can never run. It rendered as the dashboard's only failure for
        # 43 days. dispatch() and dashboard already treat REJECTED as FAILED's
        # equal — the sweep must too.
        self._rejected()
        with mock.patch.object(stages, "gh_pr_merged", return_value=None), \
             mock.patch.object(tasks, "task_status", return_value="cancelled"), \
             mock.patch.object(self.orch, "_close_task"):
            out = self.orch.reconcile_merged()
        self.assertEqual([x.task_id for x in out], ["t"])
        self.assertEqual(self.ledger.load("t").state, A.DONE)

    def test_rejected_assignment_for_an_open_todo_is_left_alone(self):
        # Widening the sweep must not start closing genuinely-unfinished work.
        self._rejected()
        with mock.patch.object(stages, "gh_pr_merged", return_value=None), \
             mock.patch.object(tasks, "task_status", return_value="open"), \
             mock.patch.object(self.orch, "_close_task") as close:
            out = self.orch.reconcile_merged()
        close.assert_not_called()
        self.assertEqual(out, [])
        self.assertEqual(self.ledger.load("t").state, A.REJECTED)

    def test_merged_pr_with_blocker_parks_failed_and_rejected(self):
        # 2026-09-17 macp-dashboard-public-access: PR #129 merged the code half
        # while the Funnel steps were still a human's, named in `blocked_on`. A
        # merge is not "the work is over" then: park where the blocker shows,
        # never close. (The incident row itself was AWAITING_HUMAN — see
        # MergedButBlockedTest; the other two swept states park the same way.)
        for state in (A.FAILED, A.REJECTED):
            with self.subTest(state=state):
                a = Assignment(task_id="t", agent_id="w", corr_id="C1", state=state,
                               kind="ai-local", payload={"branch": "worker/t", "repo": "o/r"})
                a.blocked_on = "AC4/AC5 need a human on acme-hq: sudo-enable Tailscale Funnel"
                self.ledger.save(a)
                with mock.patch.object(stages, "gh_pr_merged", return_value=True), \
                        mock.patch.object(tasks, "task_status", return_value="open"), \
                        mock.patch.object(self.orch, "_close_task") as close, \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(self.orch.reconcile_merged(), [])
                    self.assertEqual(self.orch.reconcile_merged(), [])   # a later tick
                close.assert_not_called()
                row = self.ledger.load("t")
                self.assertEqual(row.state, A.AWAITING_HUMAN)
                self.assertIn("Tailscale Funnel", row.blocked_on)   # still names what is owed
                # recorded once, not once per tick
                self.assertEqual(
                    sum(h.get("type") == dashboard._MERGED_BUT_BLOCKED
                        for h in row.history), 1)

    def _parked_on(self, blocker, task_id="t"):
        a = Assignment(task_id=task_id, agent_id="w", corr_id="C1",
                       state=A.AWAITING_HUMAN, kind="ai-local", attempts=1)
        a.blocked_on = blocker
        self.ledger.save(a)
        return a

    def test_resolved_blocker_requeues_the_parked_task(self):
        # parking stops the churn; without this it would trade churn for a stall
        self._parked_on("upstream-task (PR #22, OPEN)")
        with mock.patch.object(tasks, "task_status", return_value="done"):
            out = self.orch.reconcile_unblocked()
        self.assertEqual([x.task_id for x in out], ["t"])
        reloaded = self.ledger.load("t")
        self.assertEqual(reloaded.state, A.FAILED)   # back on the normal retry path
        self.assertEqual(reloaded.blocked_on, "")
        self.assertIn("upstream-task", reloaded.last_note)

    def test_unresolved_blocker_stays_parked(self):
        self._parked_on("upstream-task")
        with mock.patch.object(tasks, "task_status", return_value="open"):
            self.assertEqual(self.orch.reconcile_unblocked(), [])
        self.assertEqual(self.ledger.load("t").state, A.AWAITING_HUMAN)

    def test_unrecognizable_blocker_stays_parked(self):
        # free text that names no task is genuinely a human's problem; guessing
        # here would resurrect the churn parking exists to stop
        self._parked_on("Firebase console secrets")
        with mock.patch.object(tasks, "task_status", return_value=None):
            self.assertEqual(self.orch.reconcile_unblocked(), [])
        self.assertEqual(self.ledger.load("t").state, A.AWAITING_HUMAN)

    def test_self_referencing_blocker_is_never_resolvable(self):
        self._parked_on("t is stuck")
        with mock.patch.object(tasks, "task_status", return_value="done"):
            self.assertEqual(self.orch.reconcile_unblocked(), [])

    def test_parked_without_a_blocker_is_left_alone(self):
        # ordinary review parking (PR opened, awaiting approval) must not requeue
        a = Assignment(task_id="t", agent_id="w", corr_id="C1",
                       state=A.AWAITING_HUMAN, kind="ai-local", attempts=1)
        self.ledger.save(a)
        with mock.patch.object(tasks, "task_status", return_value="done"):
            self.assertEqual(self.orch.reconcile_unblocked(), [])
        self.assertEqual(self.ledger.load("t").state, A.AWAITING_HUMAN)

    def test_defaults_branch_to_worker_slug_when_payload_missing(self):
        self._awaiting()   # no branch/repo in payload
        with mock.patch.object(stages, "gh_pr_merged", return_value=True) as gh, \
             mock.patch.object(self.orch, "_close_task"):
            self.orch.reconcile_merged()
        gh.assert_called_once_with("worker/t", None)   # falls back to worker/<slug>

    def test_noop_when_gh_unknown(self):
        self._awaiting(branch="worker/t", repo="o/r")
        with mock.patch.object(stages, "gh_pr_merged", return_value=None), \
             mock.patch.object(self.orch, "_close_task") as close:
            out = self.orch.reconcile_merged()
        close.assert_not_called()
        self.assertEqual(out, [])
        self.assertEqual(self.ledger.load("t").state, A.AWAITING_HUMAN)

    def test_noop_when_pr_still_open(self):
        self._awaiting(branch="worker/t", repo="o/r")
        with mock.patch.object(stages, "gh_pr_merged", return_value=False), \
             mock.patch.object(self.orch, "_close_task") as close:
            out = self.orch.reconcile_merged()
        close.assert_not_called()
        self.assertEqual(out, [])

    def test_skips_human_parked_tasks(self):
        self._awaiting(task_id="ht", kind="human", agent_id="me", branch="worker/ht")
        with mock.patch.object(stages, "gh_pr_merged", return_value=True) as gh, \
             mock.patch.object(self.orch, "_close_task") as close:
            out = self.orch.reconcile_merged()
        gh.assert_not_called()      # human tasks never reach gh
        close.assert_not_called()
        self.assertEqual(out, [])

    def test_never_raises_into_tick(self):
        # a gh error must be swallowed — reconciliation is strictly best-effort so
        # it can never freeze the dispatch loop it runs inside.
        self._awaiting(branch="worker/t", repo="o/r")
        with mock.patch.object(stages, "gh_pr_merged", side_effect=RuntimeError("boom")):
            self.assertEqual(self.orch.reconcile_merged(), [])


class ReconcileFailedWithMergedPrTest(unittest.TestCase):
    """A FAILED assignment whose PR actually merged must be reconciled too.

    Regression: reconcile_merged scanned only AWAITING_HUMAN, but a merged task
    can end FAILED — a post-merge retry dies on an empty diff or a usage limit
    and that terminal write wins. Observed 2026-07-30 on
    dancemirror-selfcam-mirror-toggle (PR #1 merged, ledger failed(capacity)).
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.orch = Orchestrator(self.mb, self.reg, self.ledger, notify=lambda *a, **k: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, state, merged, capacity=False):
        from unittest.mock import patch
        from todo_system.orchestrator import stages
        a = Assignment(task_id="t-merged", agent_id="w", corr_id="c", kind="ai-local",
                       state=state, capacity_limited=capacity,
                       payload={"repo": "https://h/x/r.git", "branch": "worker/t-merged"})
        self.ledger.save(a)
        with patch.object(stages, "gh_pr_merged", return_value=merged), \
             patch.object(self.orch, "_close_task", lambda slug: None):
            return self.orch.reconcile_merged()

    def test_failed_with_merged_pr_is_reconciled(self):
        out = self._run(A.FAILED, merged=True, capacity=True)
        self.assertEqual([r.task_id for r in out], ["t-merged"])
        self.assertEqual(self.ledger.load("t-merged").state, A.DONE)

    def test_failed_without_merged_pr_stays_failed(self):
        out = self._run(A.FAILED, merged=False)
        self.assertEqual(out, [])
        self.assertEqual(self.ledger.load("t-merged").state, A.FAILED)

    def test_unknown_merge_state_touches_nothing(self):
        out = self._run(A.FAILED, merged=None)   # gh absent
        self.assertEqual(out, [])
        self.assertEqual(self.ledger.load("t-merged").state, A.FAILED)


class ReconcileQueueTest(unittest.TestCase):
    """#22: queue.json must follow the ledger, not accumulate forever.

    The v1 worker that drained `pending` entries was retired in the 2026-07-29
    v2 cutover, but todo-sync still fills the queue and the dashboard still
    renders it — so a merged, DONE task kept showing as `pending` work.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.orch = Orchestrator(self.mb, self.reg, self.ledger, notify=lambda *a, **k: None)
        self.queue = {"runs": []}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _reconcile(self, ledger_state, queue_status="pending", slug="t"):
        self.ledger.save(Assignment(task_id=slug, agent_id="w", corr_id="c",
                                    kind="ai-local", state=ledger_state))
        self.queue = {"runs": [{"slug": slug, "status": queue_status}]}
        from todo_system import worker as _worker
        with mock.patch.object(_worker, "load_queue", lambda: self.queue), \
             mock.patch.object(_worker, "save_queue", lambda q: None), \
             mock.patch.object(_worker, "queue_lock", contextlib.nullcontext):
            return self.orch.reconcile_queue()

    def test_done_ledger_completes_queue_entry(self):
        self.assertEqual(self._reconcile(A.DONE), ["t"])
        self.assertEqual(self.queue["runs"][0]["status"], "completed")
        self.assertTrue(self.queue["runs"][0]["finished_at"])

    def test_failed_stays_pending_so_doctor_can_retry(self):
        # a failed attempt (esp. capacity-limited) is still outstanding work;
        # mirroring it would also flip-flop against doctor_recover_queue, which
        # resets failed entries back to pending for its own auto-retry.
        self.assertEqual(self._reconcile(A.FAILED), [])
        self.assertEqual(self.queue["runs"][0]["status"], "pending")

    def test_rejected_stays_pending(self):
        self.assertEqual(self._reconcile(A.REJECTED), [])
        self.assertEqual(self.queue["runs"][0]["status"], "pending")

    def test_awaiting_human_stays_pending(self):
        # parked for a human — showing it as still-queued work is accurate
        self.assertEqual(self._reconcile(A.AWAITING_HUMAN), [])
        self.assertEqual(self.queue["runs"][0]["status"], "pending")

    def test_in_flight_ledger_leaves_queue_alone(self):
        self.assertEqual(self._reconcile(A.IN_PROGRESS), [])
        self.assertEqual(self.queue["runs"][0]["status"], "pending")

    def test_in_progress_queue_entry_for_done_task_is_closed(self):
        self.assertEqual(self._reconcile(A.DONE, queue_status="in_progress"), ["t"])
        self.assertEqual(self.queue["runs"][0]["status"], "completed")

    def test_already_terminal_queue_entry_untouched(self):
        self.assertEqual(self._reconcile(A.DONE, queue_status="skipped"), [])
        self.assertEqual(self.queue["runs"][0]["status"], "skipped")

    def test_entry_with_no_ledger_record_is_left_alone(self):
        self.queue = {"runs": [{"slug": "orphan", "status": "pending"}]}
        from todo_system import worker as _worker
        with mock.patch.object(_worker, "load_queue", lambda: self.queue), \
             mock.patch.object(_worker, "save_queue", lambda q: None), \
             mock.patch.object(_worker, "queue_lock", contextlib.nullcontext):
            self.assertEqual(self.orch.reconcile_queue(), [])
        self.assertEqual(self.queue["runs"][0]["status"], "pending")

    # -- runs nothing can ever claim -------------------------------------
    #
    # load_dispatchable only yields tasks with a dispatchable worker_mode, so a
    # task edited down to human-only drops out of every candidate set while its
    # old pending run lingers. macp_monitor then reports the leftover as a
    # routing failure. linkedin-profile-refresh sat 85h that way (2026-09-06).

    def _reconcile_orphan(self, md_body: str | None, name: str = "t.md"):
        path = self.tmp / name
        if md_body is not None:
            path.write_text(md_body)
        self.queue = {"runs": [{"slug": "t", "status": "pending",
                                "task_path": str(path)}]}
        from todo_system import worker as _worker
        with mock.patch.object(_worker, "load_queue", lambda: self.queue), \
             mock.patch.object(_worker, "save_queue", lambda q: None), \
             mock.patch.object(_worker, "queue_lock", contextlib.nullcontext):
            return self.orch.reconcile_queue()

    def test_unclaimable_run_is_skipped(self):
        # human-owned: no worker_mode at all -> no dispatcher will ever look
        self.assertEqual(self._reconcile_orphan("---\nstatus: open\n---\n"), ["t"])
        self.assertEqual(self.queue["runs"][0]["status"], "skipped")
        self.assertTrue(self.queue["runs"][0]["finished_at"])

    def test_non_dispatchable_mode_is_skipped(self):
        self.assertEqual(self._reconcile_orphan("---\nworker_mode: blocked\n---\n"), ["t"])
        self.assertEqual(self.queue["runs"][0]["status"], "skipped")

    def test_dispatchable_run_stays_pending(self):
        self.assertEqual(self._reconcile_orphan("---\nworker_mode: auto\n---\n"), [])
        self.assertEqual(self.queue["runs"][0]["status"], "pending")

    def test_unreadable_task_file_stays_pending(self):
        # fail-closed on doubt: voiding a real run because a file could not be
        # read is worse than one stale entry in a view with no consumers
        self.assertEqual(self._reconcile_orphan(None, name="gone.md"), [])
        self.assertEqual(self.queue["runs"][0]["status"], "pending")

    def _git_vault(self):
        import subprocess
        # production shape: ledger at <vault>/Worker/assignments
        root = self.tmp / "vault"
        root.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(root / "Worker" / "assignments")
        self.orch = Orchestrator(self.mb, self.reg, self.ledger, notify=lambda *a, **k: None)
        def g(*a):
            return subprocess.run(["git", "-C", str(root), *a], check=True, capture_output=True)
        g("init", "-q")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        (root / "keep.md").write_text("x\n")
        g("add", "keep.md")
        g("commit", "-qm", "base")
        return root, g

    def test_md_never_on_head_is_skipped(self):
        # 2026-10-21: md existed only on an unmerged worker branch -> the run sat
        # pending 738h with nothing able to claim it.
        root, _ = self._git_vault()
        self.queue = {"runs": [{"slug": "t", "status": "pending",
                                "task_path": str(root / "Todo" / "items" / "never-merged.md")}]}
        from todo_system import worker as _worker
        with mock.patch.object(_worker, "load_queue", lambda: self.queue), \
             mock.patch.object(_worker, "save_queue", lambda q: None), \
             mock.patch.object(_worker, "queue_lock", contextlib.nullcontext):
            self.assertEqual(self.orch.reconcile_queue(), ["t"])
        self.assertEqual(self.queue["runs"][0]["status"], "skipped")

    def test_md_on_head_but_missing_on_disk_stays_pending(self):
        # committed but momentarily absent from the work tree: not definitive
        root, g = self._git_vault()
        (root / "Todo" / "items").mkdir(parents=True)
        (root / "Todo" / "items" / "t.md").write_text("---\nworker_mode: auto\n---\n")
        g("add", "-A")
        g("commit", "-qm", "task")
        (root / "Todo" / "items" / "t.md").unlink()
        self.queue = {"runs": [{"slug": "t", "status": "pending",
                                "task_path": str(root / "Todo" / "items" / "t.md")}]}
        from todo_system import worker as _worker
        with mock.patch.object(_worker, "load_queue", lambda: self.queue), \
             mock.patch.object(_worker, "save_queue", lambda q: None), \
             mock.patch.object(_worker, "queue_lock", contextlib.nullcontext):
            self.assertEqual(self.orch.reconcile_queue(), [])
        self.assertEqual(self.queue["runs"][0]["status"], "pending")

    def test_assigned_run_is_never_voided(self):
        # live work: its reply path is what closes it, not this reconciler
        self.ledger.save(Assignment(task_id="t", agent_id="w", corr_id="c",
                                    kind="ai-local", state=A.IN_PROGRESS))
        self.assertEqual(self._reconcile_orphan("---\nstatus: open\n---\n"), [])
        self.assertEqual(self.queue["runs"][0]["status"], "pending")

    def test_never_raises_into_tick(self):
        from todo_system import worker as _worker
        with mock.patch.object(_worker, "load_queue", side_effect=RuntimeError("boom")):
            self.assertEqual(self.orch.reconcile_queue(), [])


class ReconcileCancelledBlockerTest(unittest.TestCase):
    """A blocker that was CANCELLED is not a blocker that was DONE.

    reconcile_unblocked treated both as "the wait is over" and dropped the
    downstream task to FAILED, i.e. straight back onto the re-dispatch path —
    and left `last_note` claiming "<slug> is done — requeued", which is a lie
    when the upstream was abandoned. Cancelling upstream work silently bought a
    fresh pipeline run for every task waiting on it. A cancelled blocker means
    nobody is going to deliver the precondition, so the honest move is to stay
    parked and put it in front of a human.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.pings: list[str] = []
        self.orch = Orchestrator(self.mb, self.reg, self.ledger,
                                 notify=lambda text, *a, **k: self.pings.append(text))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _parked_on(self, blocker, task_id="t"):
        a = Assignment(task_id=task_id, agent_id="w", corr_id="C1",
                       state=A.AWAITING_HUMAN, kind="ai-local", attempts=1)
        a.blocked_on = blocker
        self.ledger.save(a)
        return a

    def test_cancelled_blocker_keeps_the_task_parked(self):
        self._parked_on("upstream-task (PR #22, OPEN)")
        with mock.patch.object(tasks, "task_status", return_value="cancelled"):
            out = self.orch.reconcile_unblocked()
        # `out` means "requeued" — the tick surfaces it under the `unblocked`
        # key — so a still-parked task must not appear in it.
        self.assertEqual(out, [])
        reloaded = self.ledger.load("t")
        self.assertEqual(reloaded.state, A.AWAITING_HUMAN)
        self.assertEqual(reloaded.blocked_on, "upstream-task (PR #22, OPEN)")
        self.assertIn("upstream-task", reloaded.last_note)
        self.assertNotIn("requeued", reloaded.last_note)
        self.assertTrue(any(h["type"] == "blocker-cancelled" for h in reloaded.history))

    def test_cancelled_blocker_pings_a_human_once(self):
        self._parked_on("upstream-task")
        with mock.patch.object(tasks, "task_status", return_value="cancelled"):
            self.orch.reconcile_unblocked()
            self.assertEqual(len(self.pings), 1)
            self.assertIn("upstream-task", self.pings[0])
            # every tick re-enters this branch (the task stays parked with its
            # blocker); re-pinging on each one would be a notification loop.
            self.orch.reconcile_unblocked()
        self.assertEqual(len(self.pings), 1)

    def test_american_spelling_is_also_a_cancellation(self):
        # _retry() already reads ("done", "cancelled", "canceled"); without the
        # variant here a `canceled` upstream falls through the done branch and
        # silently re-dispatches.
        self._parked_on("upstream-task")
        with mock.patch.object(tasks, "task_status", return_value="canceled"):
            out = self.orch.reconcile_unblocked()
        self.assertEqual(out, [])
        self.assertEqual(self.ledger.load("t").state, A.AWAITING_HUMAN)

    def test_done_blocker_still_requeues(self):
        # the discriminating half: requirement 3 says the done path is untouched.
        # ReconcileMergedTest.test_resolved_blocker_requeues_the_parked_task is
        # the standing safety net; this pins done-vs-cancelled side by side.
        self._parked_on("upstream-task")
        with mock.patch.object(tasks, "task_status", return_value="done"):
            out = self.orch.reconcile_unblocked()
        self.assertEqual([x.task_id for x in out], ["t"])
        self.assertEqual(self.ledger.load("t").state, A.FAILED)
        self.assertEqual(self.pings, [])       # requeue is routine, not news


class ReconcileUnparkedTest(unittest.TestCase):
    """A blocker the human cleared must release the task without ledger surgery.

    reconcile_unblocked only resolves blockers whose first token names a vault
    task. Real ones usually are not slugs — worldgraph-26-live-eval parked on a
    missing `ANTHROPIC_API_KEY` — so those rows sat in AWAITING_HUMAN forever and
    the only exit was `human reply --status reject`, i.e. hand-operating a
    git-tracked JSON file to restate what the task markdown already said.

    The accepted signal is that markdown: a human flips `worker_mode` to
    non-dispatchable to park, and back to `auto` once the blocker is gone. Only
    that OBSERVED transition releases the row, and only for rows parked on an
    external blocker — everything else keeps waiting for the human who owes an
    answer.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # ledger.root.parent.parent is the vault root the code resolves relative
        # task_paths against; keep it inside tmp so nothing can reach the real one
        self.vault = self.tmp / "vault"
        self.items = self.vault / "Todo" / "items"
        self.items.mkdir(parents=True)
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.vault / "Worker" / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.pings: list[str] = []
        self.orch = Orchestrator(self.mb, self.reg, self.ledger,
                                 notify=lambda text, *a, **k: self.pings.append(text))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _md(self, mode, slug="t") -> Path:
        p = self.items / f"{slug}.md"
        p.write_text(f"---\nstatus: open\nworker_mode: {mode}\n---\n\n# {slug}\n",
                     encoding="utf-8")
        return p

    def _parked(self, *, md_mode="auto", stamp="blocked", blocker="ANTHROPIC_API_KEY 미설정",
                alert=ALERT_BLOCKED, state=A.AWAITING_HUMAN, slug="t",
                task_path=..., refail_count=2):
        """A row parked on an external blocker, plus the task md it points at."""
        self._md(md_mode, slug)
        if task_path is ...:
            task_path = f"Todo/items/{slug}.md"      # vault-relative, as tasks.py writes it
        a = Assignment(task_id=slug, agent_id="w", corr_id="C1", state=state,
                       kind="ai-local", attempts=1, refail_count=refail_count,
                       payload={"task_path": task_path} if task_path is not None else {})
        a.blocked_on = blocker
        a.last_alert_kind = alert
        a.parked_worker_mode = stamp
        # park it far enough in the past that the fail cooldown is spent — the
        # point of preserving updated_ts is that dispatch() can act immediately
        a.updated_ts = "2026-01-01T00:00:00"
        self.ledger.save(a)
        return a

    # --- the incident ----------------------------------------------------

    def test_blocked_to_auto_releases_the_parked_task(self):
        self._parked(stamp="blocked", md_mode="auto")
        out = self.orch.reconcile_unparked()
        self.assertEqual([x.task_id for x in out], ["t"])
        r = self.ledger.load("t")
        self.assertEqual(r.state, A.FAILED)      # back on the normal retry path
        self.assertEqual(r.blocked_on, "")
        self.assertIn("worker_mode", r.last_note)
        self.assertTrue(any(h["type"] == "unparked-worker-mode" for h in r.history))

    def test_release_neither_blames_the_task_nor_restarts_its_cooldown(self):
        # An external blocker is not a defect, so refail_count must not move; and
        # updated_ts is dispatch()'s cooldown anchor, so refreshing it would charge
        # the human's explicit "go" a fresh window before anything ran.
        self._parked(refail_count=2)
        self.assertEqual(len(self.orch.reconcile_unparked()), 1)   # it DID release
        r = self.ledger.load("t")
        self.assertEqual(r.refail_count, 2)
        self.assertEqual(r.updated_ts, "2026-01-01T00:00:00")

    def test_released_task_dispatches_on_the_same_tick(self):
        # the whole point of preserving updated_ts: no extra tick of latency
        self._parked()
        self.orch.reconcile_unparked()
        self.assertIsNotNone(self.orch.dispatch(Task(slug="t")))

    def test_tick_reconciles_before_dispatching(self):
        # end-to-end through tick(): a parked row goes out in the SAME tick.
        # reconcile_unparked is deliberately NOT stubbed here — it is the subject.
        self._parked()
        stubs = ("collect", "reconcile_merged", "reconcile_unblocked",
                 "reconcile_queue", "reconcile_parents", "renudge_due", "reap_stale")
        with contextlib.ExitStack() as st:
            for name in stubs:
                st.enter_context(mock.patch.object(self.orch, name, return_value=[]))
            for name in ("paused", "budget_exceeded", "usage_hold"):
                st.enter_context(mock.patch.object(self.orch, name, return_value=False))
            result = self.orch.tick([Task(slug="t")])
        self.assertEqual([x.task_id for x in result["unparked"]], ["t"])
        self.assertEqual([x.task_id for x in result["dispatched"]], ["t"])

    # --- false-positive prevention: normal parking keeps waiting ---------

    def test_still_blocked_md_stays_parked(self):
        self._parked(stamp="blocked", md_mode="blocked")
        self.assertEqual(self.orch.reconcile_unparked(), [])
        self.assertEqual(self.ledger.load("t").state, A.AWAITING_HUMAN)

    def test_pr_review_parking_stays_parked(self):
        # a human owes a review; no markdown edit may substitute for it
        self._parked(alert=ALERT_REVIEW_PR)
        self.assertEqual(self.orch.reconcile_unparked(), [])
        self.assertEqual(self.ledger.load("t").state, A.AWAITING_HUMAN)

    def test_poison_quarantine_stays_parked(self):
        self._parked(alert=ALERT_QUARANTINE)
        self.assertEqual(self.orch.reconcile_unparked(), [])
        self.assertEqual(self.ledger.load("t").state, A.AWAITING_HUMAN)

    def test_needs_input_stays_parked(self):
        # the AI asked a question; releasing it would drop the question
        self._parked(state=A.NEEDS_INPUT)
        self.assertEqual(self.orch.reconcile_unparked(), [])
        self.assertEqual(self.ledger.load("t").state, A.NEEDS_INPUT)

    def test_parking_with_no_blocker_stays_parked(self):
        self._parked(blocker="")
        self.assertEqual(self.orch.reconcile_unparked(), [])
        self.assertEqual(self.ledger.load("t").state, A.AWAITING_HUMAN)

    def test_legacy_row_without_a_stamp_is_only_observed(self):
        # A row parked by an older build has no observation to compare against.
        # "No evidence" is not "a transition" — record the mode, stay parked.
        self._parked(stamp="", md_mode="auto")
        self.assertEqual(self.orch.reconcile_unparked(), [])
        r = self.ledger.load("t")
        self.assertEqual(r.state, A.AWAITING_HUMAN)
        self.assertEqual(r.parked_worker_mode, "auto")   # observed, not acted on

    def test_missing_task_path_stays_parked(self):
        self._parked(task_path=None)
        self.assertEqual(self.orch.reconcile_unparked(), [])
        self.assertEqual(self.ledger.load("t").state, A.AWAITING_HUMAN)

    def test_task_file_gone_stays_parked(self):
        self._parked(task_path="Todo/items/does-not-exist.md")
        self.assertEqual(self.orch.reconcile_unparked(), [])
        self.assertEqual(self.ledger.load("t").state, A.AWAITING_HUMAN)

    def test_unreadable_worker_mode_stays_parked(self):
        # read_task_worker_mode returns None for a blank/placeholder/absent field
        self._parked(md_mode="-")
        self.assertEqual(self.orch.reconcile_unparked(), [])
        self.assertEqual(self.ledger.load("t").state, A.AWAITING_HUMAN)

    # --- loop prevention --------------------------------------------------

    def test_release_consumes_the_signal_and_does_not_repeat(self):
        # Without the re-stamp the pair (stamp=blocked, md=auto) stays true and
        # the row is released again on every single tick — an unpark loop. Re-park
        # the released row exactly as collect() would and confirm it stays put.
        self._parked(stamp="blocked", md_mode="auto")
        self.assertEqual(len(self.orch.reconcile_unparked()), 1)
        self.assertEqual(self.ledger.load("t").parked_worker_mode, "auto")
        r = self.ledger.load("t")
        r.state = A.AWAITING_HUMAN
        r.blocked_on = "ANTHROPIC_API_KEY 미설정"
        r.last_alert_kind = ALERT_BLOCKED
        self.ledger.save(r)
        self.assertEqual(self.orch.reconcile_unparked(), [])
        self.assertEqual(self.ledger.load("t").state, A.AWAITING_HUMAN)

    def test_observation_alone_never_changes_state(self):
        # an `auto` -> `blocked` edit is the human PARKING it, not releasing it
        self._parked(stamp="auto", md_mode="blocked")
        self.assertEqual(self.orch.reconcile_unparked(), [])
        r = self.ledger.load("t")
        self.assertEqual(r.state, A.AWAITING_HUMAN)
        self.assertEqual(r.parked_worker_mode, "blocked")

    def test_every_dispatchable_mode_counts_as_a_release(self):
        for mode in tasks.DISPATCHABLE_MODES:
            with self.subTest(mode=mode):
                self._parked(stamp="blocked", md_mode=mode)
                self.assertEqual(len(self.orch.reconcile_unparked()), 1)
                self.assertEqual(self.ledger.load("t").state, A.FAILED)

    # --- safety -----------------------------------------------------------

    def test_a_raising_reader_never_escapes_into_tick(self):
        # an exception here would freeze ALL dispatch, not just this row
        self._parked()
        with mock.patch.object(repos, "read_task_worker_mode",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(self.orch.reconcile_unparked(), [])
        self.assertEqual(self.ledger.load("t").state, A.AWAITING_HUMAN)

    def test_awaiting_human_is_not_terminal(self):
        # putting AWAITING_HUMAN in TERMINAL would "fix" this bug by re-dispatching
        # every parked row, including the PR reviews above
        self.assertNotIn(A.AWAITING_HUMAN, A.TERMINAL)

    def test_older_json_without_the_field_loads_with_a_default(self):
        text = json.dumps({"task_id": "t", "agent_id": "w", "corr_id": "C1",
                           "state": A.AWAITING_HUMAN})
        self.assertEqual(Assignment.from_json(text).parked_worker_mode, "")

    def test_a_reply_can_never_set_the_stamp(self):
        # the stamp is an orchestrator observation, not wire data — an agent that
        # sent one could unpark itself
        a = self._parked()
        a.apply_reply(m.Envelope(to="o", type=m.FAIL, task_id="t",
                                 payload={"parked_worker_mode": "auto"}))
        self.assertEqual(a.parked_worker_mode, "blocked")


class ReconcilePerRowIsolationTest(unittest.TestCase):
    """One bad ledger row must skip only itself, not the rows behind it.

    Both reconcile sweeps used to wrap the ENTIRE `for a in self.ledger.all()`
    loop in a single `try/except: pass`. `Ledger.all()` is filename-sorted, so
    the first row that raised — a whitespace-only `blocked_on`, a `task_path`
    that is not a string, an md that won't read — silently cancelled the
    reconcile of every row sorted after it, on every tick, forever, with nothing
    printed. The rows below use REAL raisers rather than mocks: the point is
    that these inputs occur, not that an exception can be injected.

    The outer net is deliberately kept, so `tick()` still never sees an
    exception from here — `test_*_survives_a_broken_ledger` is the only check
    that goes red if someone deletes it, since per-row handlers alone would let
    a failing `ledger.all()` propagate.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.items = self.vault / "Todo" / "items"
        self.items.mkdir(parents=True)
        self.mb = FileMailbox(self.tmp / "agents")
        # ledger.root.parent.parent must stay inside tmp — reconcile_unparked
        # resolves relative task_paths against it, and the real vault must never
        # be reachable from a test.
        self.ledger = Ledger(self.vault / "Worker" / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.orch = Orchestrator(self.mb, self.reg, self.ledger, notify=lambda *a, **k: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _row(self, slug, *, blocker, task_path=..., md_mode="auto", stamp="blocked"):
        """A parked row named `slug`; rows are processed in filename order."""
        (self.items / f"{slug}.md").write_text(
            f"---\nstatus: open\nworker_mode: {md_mode}\n---\n\n# {slug}\n",
            encoding="utf-8")
        if task_path is ...:
            task_path = f"Todo/items/{slug}.md"
        a = Assignment(task_id=slug, agent_id="w", corr_id="C1",
                       state=A.AWAITING_HUMAN, kind="ai-local", attempts=1,
                       payload={"task_path": task_path})
        a.blocked_on = blocker
        a.last_alert_kind = ALERT_BLOCKED
        a.parked_worker_mode = stamp
        a.updated_ts = "2026-01-01T00:00:00"
        self.ledger.save(a)
        return a

    @staticmethod
    def _capturing(fn):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = fn()
        return out, buf.getvalue()

    # --- the incident: row 1 raises, row 2 must still reconcile ------------

    def test_unblocked_row_that_raises_does_not_swallow_the_next_row(self):
        # "   " is truthy, so it passes the `not blocker` guard, and then
        # "   ".split()[0] raises IndexError — a real row shape, not a mock.
        self._row("aaa", blocker="   ")
        self._row("zzz", blocker="upstream-task (PR #9, MERGED)")
        with mock.patch.object(tasks, "task_status", return_value="done"):
            out, printed = self._capturing(self.orch.reconcile_unblocked)
        self.assertEqual([x.task_id for x in out], ["zzz"])       # NOT swallowed
        self.assertEqual(self.ledger.load("zzz").state, A.FAILED)  # actually saved
        self.assertEqual(self.ledger.load("aaa").state, A.AWAITING_HUMAN)  # untouched
        self.assertIn("reconcile_unblocked", printed)              # at least one line
        self.assertIn("aaa", printed)                              # naming the row

    def test_unparked_row_that_raises_does_not_swallow_the_next_row(self):
        # a non-string task_path is truthy, so it survives the `not raw` guard
        # and Path(123) raises TypeError.
        self._row("aaa", blocker="ANTHROPIC_API_KEY 미설정", task_path=123)
        self._row("zzz", blocker="ANTHROPIC_API_KEY 미설정", md_mode="auto",
                  stamp="blocked")
        out, printed = self._capturing(self.orch.reconcile_unparked)
        self.assertEqual([x.task_id for x in out], ["zzz"])
        self.assertEqual(self.ledger.load("zzz").state, A.FAILED)
        self.assertEqual(self.ledger.load("aaa").state, A.AWAITING_HUMAN)
        self.assertIn("reconcile_unparked", printed)
        self.assertIn("aaa", printed)

    # --- the contract the per-row handlers must not replace ----------------

    def test_unblocked_survives_a_broken_ledger_without_raising(self):
        with mock.patch.object(self.ledger, "all", side_effect=RuntimeError("boom")):
            out, printed = self._capturing(self.orch.reconcile_unblocked)
        self.assertEqual(out, [])                     # never raises into tick()
        self.assertIn("sweep aborted", printed)       # ...but is not silent

    def test_unparked_survives_a_broken_ledger_without_raising(self):
        with mock.patch.object(self.ledger, "all", side_effect=RuntimeError("boom")):
            out, printed = self._capturing(self.orch.reconcile_unparked)
        self.assertEqual(out, [])
        self.assertIn("sweep aborted", printed)

    # --- 같은 원인의 반복 skip은 상한 + 요약 한 줄 -------------------------
    #
    # Per-row isolation buys one printed line per swallowed exception, so a
    # SYSTEMATIC defect (task_status raising for every row) prints one line per
    # ledger row, on every 5-minute tick, burying every other signal. The rows
    # below lock the VOLUME down; none of them relaxes the isolation above.

    def test_unblocked_caps_repeated_same_cause_skip_lines(self):
        for slug in ("aaa", "bbb", "ccc", "ddd", "eee"):
            self._row(slug, blocker="   ")   # one identical IndexError, five times
        out, printed = self._capturing(self.orch.reconcile_unblocked)
        self.assertEqual(out, [])
        self.assertEqual(printed.count("skipped:"), 3)      # capped, not 5
        self.assertIn("aaa", printed)                       # first ones stay named
        self.assertNotIn("ddd", printed)                    # the rest are folded in
        self.assertNotIn("eee", printed)
        self.assertIn("같은 원인으로 2건 추가 skip", printed)
        self.assertEqual(printed.count("추가 skip"), 1)      # one summary, not one per row
        # what was suppressed is the PRINTING; every row is still skipped.
        for slug in ("aaa", "bbb", "ccc", "ddd", "eee"):
            self.assertEqual(self.ledger.load(slug).state, A.AWAITING_HUMAN)

    def test_unblocked_cap_does_not_stop_a_healthy_row_behind_it(self):
        # Both halves are asserted in ONE test on purpose, because neither
        # existing row can catch a suppression bug that gains control flow: the
        # cap test above has no healthy row to lose, and the isolation test at
        # the top has a single raiser, so the cap never fires there. Only a sweep
        # where the cap ACTUALLY fired AND a healthy row sorts behind it pins
        # "record() decides what gets PRINTED, never whether the loop keeps
        # reconciling" — the day suppression grows a `continue` or an early
        # return, this is the row that goes red.
        for slug in ("aaa", "bbb", "ccc", "ddd", "eee"):
            self._row(slug, blocker="   ")   # five identical IndexErrors
        self._row("zzz", blocker="upstream-task (PR #9, MERGED)")  # sorts last
        with mock.patch.object(tasks, "task_status", return_value="done"):
            out, printed = self._capturing(self.orch.reconcile_unblocked)
        # (1) the cap really fired — without this, half (2) proves nothing
        self.assertEqual(printed.count("skipped:"), 3)
        self.assertIn("같은 원인으로 2건 추가 skip", printed)
        # (2) ...and the healthy row queued behind it still reconciled AND saved
        self.assertEqual([x.task_id for x in out], ["zzz"])
        self.assertEqual(self.ledger.load("zzz").state, A.FAILED)
        for slug in ("aaa", "bbb", "ccc", "ddd", "eee"):
            self.assertEqual(self.ledger.load(slug).state, A.AWAITING_HUMAN)

    def test_unblocked_distinct_causes_are_tallied_separately(self):
        # Four rows raise IndexError before task_status is ever reached; four
        # more get far enough to call it and raise RuntimeError. Two causes must
        # tally apart — merged, whichever defect is rarer would vanish.
        for slug in ("aaa", "bbb", "ccc", "ddd"):
            self._row(slug, blocker="   ")
        for slug in ("raa", "rbb", "rcc", "rdd"):
            self._row(slug, blocker="upstream-task (PR #9, MERGED)")
        with mock.patch.object(tasks, "task_status", side_effect=RuntimeError("boom")):
            out, printed = self._capturing(self.orch.reconcile_unblocked)
        self.assertEqual(out, [])
        self.assertEqual(printed.count("skipped:"), 6)      # 3 per cause
        self.assertEqual(printed.count("추가 skip"), 2)      # one line per cause
        self.assertEqual(printed.count("같은 원인으로 1건 추가 skip"), 2)
        self.assertIn("IndexError", printed)
        self.assertIn("RuntimeError: boom", printed)

    def test_no_summary_line_when_nothing_is_suppressed(self):
        # Under the cap the output stays exactly what it was before this change.
        # An unconditional summary would make QUIET ticks noisier — the opposite
        # of the point — so this is the row that fails a naive always-print.
        self._row("aaa", blocker="   ")
        self._row("bbb", blocker="   ")
        out, printed = self._capturing(self.orch.reconcile_unblocked)
        self.assertEqual(printed.count("skipped:"), 2)
        self.assertNotIn("추가 skip", printed)

    def test_skip_tally_is_sweep_local(self):
        # The tally is rebuilt per sweep. One that outlived the call would stay
        # silent from the second tick onward — that does not stop the flood, it
        # stops anyone SEEING it, which is strictly worse than the flood.
        for slug in ("aaa", "bbb", "ccc", "ddd", "eee"):
            self._row(slug, blocker="   ")
        _, first = self._capturing(self.orch.reconcile_unblocked)
        _, second = self._capturing(self.orch.reconcile_unblocked)
        self.assertEqual(first.count("skipped:"), 3)
        self.assertEqual(second.count("skipped:"), 3)       # NOT 0
        self.assertIn("같은 원인으로 2건 추가 skip", second)

    def test_unparked_caps_repeated_same_cause_skip_lines(self):
        # The other sweep carries its own tally: Path(123) -> TypeError.
        for slug in ("aaa", "bbb", "ccc", "ddd", "eee"):
            self._row(slug, blocker="ANTHROPIC_API_KEY 미설정", task_path=123)
        out, printed = self._capturing(self.orch.reconcile_unparked)
        self.assertEqual(out, [])
        self.assertEqual(printed.count("skipped:"), 3)
        self.assertIn("reconcile_unparked", printed)        # named for ITS sweep
        self.assertEqual(printed.count("추가 skip"), 1)
        self.assertIn("같은 원인으로 2건 추가 skip", printed)
        self.assertNotIn("eee", printed)

    def test_summary_key_is_length_capped(self):
        # A 500-char exception message quoted in full would re-create the flood
        # the cap exists to end. Only the SUMMARY key is abridged: the first
        # three per-row lines keep the whole message, since those are the ones
        # anyone actually debugs from.
        long_msg = "x" * 500
        for slug in ("aaa", "bbb", "ccc", "ddd"):
            self._row(slug, blocker="upstream-task (PR #9, MERGED)")
        with mock.patch.object(tasks, "task_status",
                               side_effect=RuntimeError(long_msg)):
            _, printed = self._capturing(self.orch.reconcile_unblocked)
        summary = [ln for ln in printed.splitlines() if "추가 skip" in ln]
        self.assertEqual(len(summary), 1)
        self.assertIn("…", summary[0])                      # abridged...
        self.assertNotIn(long_msg, summary[0])
        self.assertLess(len(summary[0]), 200)
        rows = [ln for ln in printed.splitlines() if "skipped:" in ln]
        self.assertEqual(len(rows), 3)
        self.assertIn(long_msg, rows[0])                    # ...but not on the row lines


def _make_todo_db(path: Path, rows: list[dict]) -> Path:
    """A throwaway todo.db with just the columns these queries read.

    Tests MUST build their own DB and pass db_path explicitly — the helpers
    default to the real vault's Todo/todo.db, so an implicit default would make
    assertions depend on live vault state.
    """
    import sqlite3 as _sq

    con = _sq.connect(path)
    try:
        con.execute("""
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                path TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                outcome TEXT, next_action TEXT, tags TEXT,
                not_before TEXT, worker_mode TEXT,
                auto_merge INTEGER NOT NULL DEFAULT 1,
                parent_slug TEXT,
                is_parent INTEGER NOT NULL DEFAULT 0
            )""")
        for r in rows:
            con.execute(
                "INSERT INTO tasks (slug, title, status, parent_slug, is_parent, "
                "worker_mode) VALUES (?,?,?,?,?,?)",
                (r["slug"], r.get("title", r["slug"]), r.get("status", "open"),
                 r.get("parent_slug"), r.get("is_parent", 0),
                 r.get("worker_mode", "auto")))
        con.commit()
    finally:
        con.close()
    return path


class ParentsNeedingReviewTest(unittest.TestCase):
    """An `is_parent` task is excluded from the dispatch query, so no worker ever
    closes it — and nothing else watched it either. Observed on
    worldgraph-project: all 22 leaves done, the parent sat `status: open` for 18
    days still advertising a stale `next_action`. Surface it; never auto-close.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "todo.db"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rows(self, rows):
        _make_todo_db(self.db, rows)
        return tasks.parents_needing_review(db_path=self.db)

    def test_parent_whose_children_all_closed_is_surfaced(self):
        # the worldgraph-project shape, reproduced at SQL level
        out = self._rows([
            {"slug": "p", "title": "worldgraph", "is_parent": 1, "status": "open"},
            {"slug": "c1", "parent_slug": "p", "status": "done"},
            {"slug": "c2", "parent_slug": "p", "status": "cancelled"},
        ])
        self.assertEqual([r["slug"] for r in out], ["p"])
        self.assertEqual(out[0]["n_children"], 2)
        self.assertEqual(out[0]["title"], "worldgraph")

    def test_childless_parent_is_not_vacuously_complete(self):
        # the empty-set trap: "all children are closed" is trivially true of a
        # parent with no children at all, which is a freshly-created project.
        self.assertEqual(self._rows([
            {"slug": "p", "is_parent": 1, "status": "open"},
        ]), [])

    def test_one_open_child_holds_the_parent_back(self):
        self.assertEqual(self._rows([
            {"slug": "p", "is_parent": 1, "status": "open"},
            {"slug": "c1", "parent_slug": "p", "status": "done"},
            {"slug": "c2", "parent_slug": "p", "status": "open"},
        ]), [])

    def test_already_closed_parent_is_not_resurfaced(self):
        self.assertEqual(self._rows([
            {"slug": "p", "is_parent": 1, "status": "done"},
            {"slug": "c1", "parent_slug": "p", "status": "done"},
        ]), [])

    def test_ordinary_leaf_with_children_is_not_a_parent(self):
        # review-worldgraph-4-relation-inference-… sets `parent:` to a LEAF task,
        # so "has children" alone would flag every reviewed leaf in the vault.
        # is_parent=1 is exactly the population the dispatch query drops.
        self.assertEqual(self._rows([
            {"slug": "leaf", "is_parent": 0, "status": "open"},
            {"slug": "review-leaf", "parent_slug": "leaf", "status": "done"},
        ]), [])

    def test_american_spelling_counts_as_closed(self):
        out = self._rows([
            {"slug": "p", "is_parent": 1, "status": "open"},
            {"slug": "c1", "parent_slug": "p", "status": "canceled"},
        ])
        self.assertEqual([r["slug"] for r in out], ["p"])

    def test_dispatch_query_never_sees_the_parent(self):
        # the premise of the whole bug, pinned against the live dispatch query:
        # this is why nobody closes a parent task.
        _make_todo_db(self.db, [
            {"slug": "p", "is_parent": 1, "status": "open"},
            {"slug": "c1", "parent_slug": "p", "status": "done"},
        ])
        self.assertEqual([t.slug for t in tasks.load_dispatchable(db_path=self.db)], [])


class ReconcileParentsTest(unittest.TestCase):
    """Orchestrator wiring for the parent sweep: ping once, never auto-close, and
    never raise into tick()."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.pings: list[str] = []
        self.orch = Orchestrator(self.mb, self.reg, self.ledger,
                                 notify=lambda text, *a, **k: self.pings.append(text))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _found(self, rows):
        return mock.patch.object(tasks, "parents_needing_review", return_value=rows)

    def test_surfaces_the_parent_once(self):
        rows = [{"slug": "worldgraph-project", "title": "worldgraph", "n_children": 22}]
        with self._found(rows):
            self.assertEqual(self.orch.reconcile_parents(), ["worldgraph-project"])
            self.assertEqual(len(self.pings), 1)
            self.assertIn("worldgraph-project", self.pings[0])
            # true on every tick until a human closes the parent
            self.assertEqual(self.orch.reconcile_parents(), [])
        self.assertEqual(len(self.pings), 1)

    def test_never_auto_closes_the_parent(self):
        rows = [{"slug": "p", "title": "p", "n_children": 2}]
        with self._found(rows), \
             mock.patch.object(tasks, "mark_done") as done, \
             mock.patch.object(self.orch, "_close_task") as close:
            self.orch.reconcile_parents()
        done.assert_not_called()
        close.assert_not_called()

    def test_nothing_to_report_is_silent(self):
        with self._found([]):
            self.assertEqual(self.orch.reconcile_parents(), [])
        self.assertEqual(self.pings, [])

    def test_never_raises_into_tick(self):
        with mock.patch.object(tasks, "parents_needing_review",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(self.orch.reconcile_parents(), [])


_ORPHANED = {"branch": "worker/t", "base": "origin/main",
             "tip": "deadbeef1234567890abcdef01234567890abcde", "count": 3,
             "shas": ["aaa1111222233334444555566667777888899990",
                      "bbb1111222233334444555566667777888899991",
                      "deadbeef1234567890abcdef01234567890abcde"]}


class OrphanedCommitNotifyTest(unittest.TestCase):
    """Branch prep rewinds a prior attempt's unpushed commits, and the only place
    that ever said so was run.log — a file nobody opens unless they already
    suspect something. The loss has to reach the channel a human actually reads.

    But only a FAILED run's rewind is a loss worth a human's judgement. A run that
    rewound a prior attempt and then finished the SAME task (RESULT) superseded
    those commits — mulkko-39 (2026-09-17) paged for 5 "rewound" commits whose
    work had just been redone and merged as PR #77, with a cherry-pick command that
    would have stacked a duplicate on main. So RESULT is silent (history trace
    only) and FAIL alerts.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.sent: list = []
        self.orch = Orchestrator(self.mb, self.reg, self.ledger,
                                 notify=lambda t, link=None: self.sent.append(t))
        # Line 2 of every alert is now the task title; stub it to the slug so the
        # real Todo/items/ tree is never read (the isolation this suite promises).
        self.orch._task_title = lambda slug: slug

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _collect(self, type_, payload):
        """Dispatch, reply with `payload`, and let collect() route it. mark_done /
        task_status are patched so the real Todo/items/ tree is never touched."""
        a = self.orch.dispatch(Task("t", {"summary": "x"}))
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=type_, frm="w",
                                task_id=a.task_id, corr_id=a.corr_id, payload=payload))
        with mock.patch.object(tasks, "mark_done", lambda slug, **kw: None), \
                mock.patch.object(tasks, "task_status", return_value="open"):
            self.orch.collect()
        return self.sent

    def _payload(self, **kw):
        p = {"branch": "worker/t", "run_dir": "/v/Worker/runs/20260818T220603-t",
             "repo": "git@github.com:x/y.git", "orphaned_commits": dict(_ORPHANED)}
        p.update(kw)
        return p

    def test_a_failed_run_reports_what_it_destroyed(self):
        sent = self._collect(m.FAIL, self._payload())

        self.assertEqual(len(sent), 1, f"expected exactly one ping, got {sent}")
        [text] = sent
        self.assertIn(_ORPHANED["tip"][:8], text)   # the sha to cherry-pick from
        self.assertIn("3", text)                    # how many
        self.assertIn("worker/t", text)

    def test_a_successful_run_supersedes_the_rewound_commits_silently(self):
        """The mulkko-39 false alarm: the retry that rewound attempt 1 redid the
        task and merged it. Nothing to judge, so nothing is sent — but the fact
        stays on the assignment's history."""
        sent = self._collect(m.RESULT, self._payload())

        self.assertEqual(sent, [], f"superseded rewind must not page, got {sent}")
        a = self.ledger.load("t")
        self.assertEqual(a.state, A.DONE)
        [trace] = [h for h in a.history if h.get("type") == "orphans-superseded"]
        self.assertEqual(trace["count"], 3)
        self.assertTrue(_ORPHANED["tip"].startswith(trace["tip"]))

    def test_a_review_required_run_sends_only_the_review_ping(self):
        """The PR under review IS the redone work; a second, loss-framed ping
        beside it would ask the human to recover what the PR already contains."""
        sent = self._collect(m.RESULT, self._payload(review_required=True,
                                                     pr_url="https://x/pull/1"))
        self.assertEqual(len(sent), 1, f"expected the review ping only, got {sent}")
        self.assertFalse(any(_ORPHANED["tip"][:8] in t for t in sent))

    def test_a_non_dict_report_neither_pages_nor_breaks_collect(self):
        sent = self._collect(m.RESULT, self._payload(orphaned_commits="garbage"))
        self.assertEqual(sent, [])
        self.assertEqual(self.ledger.load("t").state, A.DONE)

    def test_a_normal_run_is_completely_silent(self):
        """No loss, no key, no extra notification — the whole point of gating on
        the key rather than on the run's outcome."""
        p = self._payload()
        p.pop("orphaned_commits")
        self.assertEqual(self._collect(m.RESULT, p), [])

    def test_an_empty_report_is_not_a_report(self):
        """`orphaned_commits: None` is the normal value, not a loss."""
        self.assertEqual(self._collect(m.RESULT, self._payload(orphaned_commits=None)), [])

    def test_the_message_tells_the_human_recovery_is_theirs(self):
        [text] = self._collect(m.FAIL, self._payload())
        self.assertIn("cherry-pick", text)

    def test_the_message_survives_the_kakao_length_cut_intact(self):
        """kakao_send.sh truncates at 179 chars + '…'. A blind cut lands mid-sha,
        and a truncated sha still LOOKS usable — so the message has to fit, and
        the count+tip head has to be there whatever else is dropped."""
        from todo_system.orchestrator import human
        long_run = "/home/alice/Projects/MaestroACP/Worker/runs/20260818T220603-x"
        [text] = self._collect(m.FAIL, self._payload(run_dir=long_run))

        self.assertLessEqual(len(text), human.KAKAO_TEXT_LIMIT,
                             f"kakao would cut this: {text!r}")
        self.assertIn(_ORPHANED["tip"][:8], text)         # AC②: the sha
        self.assertIn("3", text)                          # AC②: the count
        for sha in text.split("cherry-pick", 1)[-1].split("\n")[0].split():
            if sha.startswith("("):
                continue                                   # the "(+N)" overflow marker
            self.assertEqual(len(sha), 8, f"{sha!r} is a bisected sha")

    def test_a_long_task_id_cannot_push_the_shas_out_of_a_valid_command(self):
        """The overflow path: when lines must be dropped, they are dropped whole
        and the head still carries what the alert exists for."""
        from todo_system.orchestrator import human
        info = dict(_ORPHANED)
        info["shas"] = [f"{i:040x}" for i in range(9)]
        info["count"] = 9
        a = self.orch.dispatch(Task("a-very-long-task-slug-that-eats-the-budget-whole",
                                    {"summary": "x"}))
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="w",
                                task_id=a.task_id, corr_id=a.corr_id,
                                payload=self._payload(orphaned_commits=info)))
        with mock.patch.object(tasks, "mark_done", lambda slug, **kw: None), \
                mock.patch.object(tasks, "task_status", return_value="open"):
            self.orch.collect()

        [text] = self.sent
        self.assertLessEqual(len(text), human.KAKAO_TEXT_LIMIT)
        self.assertIn("9", text)                       # count survives
        self.assertIn(_ORPHANED["tip"][:8], text)      # tip survives
        self.assertIn("(+6)", text)                    # 3 shown, 6 more on record

    def test_a_broken_notifier_does_not_break_collect(self):
        """Same contract as _notify_review: an alert failure must never cost the
        state transition."""
        self.orch.notify = mock.Mock(side_effect=RuntimeError("kakao down"))
        a = self.orch.dispatch(Task("t", {"summary": "x"}))
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="w",
                                task_id=a.task_id, corr_id=a.corr_id,
                                payload=self._payload()))
        with mock.patch.object(tasks, "mark_done", lambda slug, **kw: None), \
                mock.patch.object(tasks, "task_status", return_value="open"):
            self.orch.collect()
        self.orch.notify.assert_called()                  # the alert was attempted
        row = self.ledger.load("t")
        self.assertEqual(row.attempts, 2)                 # ...and the retry still routed
        self.assertEqual(row.state, A.DISPATCHED)


class ReviewNotifyTest(unittest.TestCase):
    """_notify_review's no-PR branch used to declare every PR-less run a failure.

    Its only collect() route is the SUCCESS one — RESULT + review_required
    (:357-360); a FAIL without a blocker goes to _retry, never here. So the
    "실패 원인 확인 필요" wording was wrong essentially every time it fired, and it
    pointed at report.md, which delegate/quick pipelines never write
    (dripcoffee-integcheck-advote 2026-09-04: a clean 0-commit delegate pass was
    announced as a failure). The alert has to read the result it reports.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.sent: list = []
        self.links: list = []           # the `link` (열기 button) arg per notify call
        self.orch = Orchestrator(
            self.mb, self.reg, self.ledger,
            notify=lambda t, link=None: (self.sent.append(t), self.links.append(link)))
        # Line 2 is the task title; stub to slug so the real Todo/items/ is untouched.
        self.orch._task_title = lambda slug: slug

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _collect(self, type_, payload, slug="t"):
        """Dispatch, reply with `payload`, and let collect() route it. mark_done /
        task_status are patched so the real Todo/items/ tree is never touched."""
        a = self.orch.dispatch(Task(slug, {"summary": "x"}))
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=type_, frm="w",
                                task_id=a.task_id, corr_id=a.corr_id, payload=payload))
        with mock.patch.object(tasks, "mark_done", lambda slug, **kw: None), \
                mock.patch.object(tasks, "task_status", return_value="open"):
            self.orch.collect()
        return self.sent

    def _payload(self, **kw):
        """A 0-commit delegate pass parked for review — the false-failure case."""
        p = {"branch": "worker/t", "run_dir": "/v/Worker/runs/20260904T101715-t",
             "repo": "git@github.com:x/y.git", "review_required": True,
             "stages": ["delegate"]}
        p.update(kw)
        return p

    def test_a_successful_no_pr_run_is_not_announced_as_a_failure(self):
        """AC: RESULT + review_required + no pr_url. No failure wording, the
        pointer names the artifact that actually exists, and the parking policy
        (AWAITING_HUMAN) is untouched — this task fixes the words, not the gate."""
        sent = self._collect(m.RESULT, self._payload())

        self.assertEqual(len(sent), 1, f"expected exactly one ping, got {sent}")
        [text] = sent
        self.assertNotIn("실패", text)
        self.assertIn("delegate.md", text)
        self.assertNotIn("report.md", text)
        self.assertEqual(self.ledger.load("t").state, A.AWAITING_HUMAN)

    def test_the_artifact_comes_from_the_last_completed_stage(self):
        """A full 5-stage run points at report.md — mapping, not a new hardcode."""
        sent = self._collect(m.RESULT, self._payload(
            stages=["research", "plan", "implement", "verify", "report"]))
        self.assertIn("report.md", sent[0])

    def test_rebase_conflict_summary_rides_into_the_parked_note(self):
        """The runner stamps a fixed concurrency-conflict summary into the RESULT;
        it must flow onto last_note (apply_reply reads summary) so the parked row
        — and both dashboards, which render last_note — show the '겹침 오판' reason,
        with the task parked AWAITING_HUMAN (not auto-closed, not retried)."""
        summary = ("staged pipeline complete (5 stages) — 동시 실행 충돌 — 겹침 오판 "
                   "(rebase 충돌; 자동 해결 안 함, 사람 검토)")
        self._collect(m.RESULT, self._payload(
            stages=["research", "plan", "implement", "verify", "report"],
            summary=summary, pr_url="https://x/pull/9"))
        row = self.ledger.load("t")
        self.assertEqual(row.state, A.AWAITING_HUMAN)
        self.assertIn("동시 실행 충돌 — 겹침 오판", row.last_note)

    def test_a_payload_without_stages_asserts_no_filename(self):
        """Old envelope / early death: point at the run dir and stop — naming a
        file the run may never have written is the original bug again."""
        p = self._payload()
        p.pop("stages")
        sent = self._collect(m.RESULT, p)

        [text] = sent
        self.assertNotIn("delegate.md", text)
        self.assertNotIn("report.md", text)
        self.assertIn("20260904T101715-t", text)   # the run dir basename survives
        self.assertNotIn("실패", text)

    def test_a_pr_run_still_gets_the_review_and_merge_ask(self):
        sent = self._collect(m.RESULT, self._payload(pr_url="https://x/pull/1"))
        self.assertEqual(len(sent), 1, f"expected exactly one ping, got {sent}")
        self.assertIn("PR 열림", sent[0])
        self.assertIn("머지", sent[0])                      # the ask lives in the headline
        # The URL now rides in the link arg (the "열기" button), NOT the text body —
        # so a long PR URL can no longer be bisected by the length cut.
        self.assertEqual(self.links[-1], "https://x/pull/1")
        self.assertNotIn("https://x/pull/1", sent[0])

    def test_a_blocked_fail_still_leads_with_the_blocker(self):
        """FAIL + blocked_on rides collect's :363 park-for-human route; the
        blocker branch outranks both no-PR branches and leads with the blocked
        framing (선행 조건 미충족), not a generic code-failure wording."""
        sent = self._collect(m.FAIL, self._payload(
            blocked_on="upstream PR", reason="verify: blocked"))
        self.assertEqual(len(sent), 1, f"expected exactly one ping, got {sent}")
        self.assertIn("선행 조건", sent[0].splitlines()[0])   # headline frames it as blocked
        self.assertNotIn("실패 원인", sent[0])                 # NOT announced as a code failure
        self.assertIn("upstream PR", sent[0])                # the actual blocker is named

    def test_a_blockerless_fail_keeps_the_failure_wording(self):
        """collect() routes blockerless FAILs to _retry, so this branch is
        defensive — called directly. If it ever fires it must still say failure,
        and point at the last artifact that captured output (run.log for a run
        that died at implement), not a report.md that was never written."""
        a = self.orch.dispatch(Task("t", {"summary": "x"}))
        msg = m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="w", task_id="t",
                         corr_id=a.corr_id,
                         payload={"stages": ["research", "plan", "implement"],
                                  "run_dir": "/v/Worker/runs/x-t"})
        self.orch._notify_review(a, msg)

        self.assertIn("실패 원인 확인", self.sent[0])          # still says failure
        self.assertIn("run.log", self.sent[0])
        self.assertNotIn("report.md", self.sent[0])

    def test_the_success_verdict_and_artifact_survive_the_kakao_cut(self):
        """With the real incident's slug and run-dir lengths, the verdict, the ask
        and the artifact pointer must all be present AND fit within the budget —
        now by construction (self-limiting compose), not by hoping the blind
        179-char cut lands after them. A truncated alert that reads as unexplained
        is the attention leak all over again."""
        from todo_system.orchestrator import human
        slug = "dripcoffee-integcheck-advote"
        run_dir = f"/home/alice/Projects/MaestroACP/Worker/runs/20260904T101715-{slug}"
        sent = self._collect(m.RESULT,
                             self._payload(branch=f"worker/{slug}", run_dir=run_dir),
                             slug=slug)

        text = sent[0]
        self.assertLessEqual(len(text), human.KAKAO_TEXT_LIMIT)   # fits, no blind cut needed
        self.assertIn("정상 완료", text)                            # the verdict
        self.assertIn("머지할 PR 없음", text)
        self.assertIn("닫", text)                                  # the ask: close the task
        self.assertIn("delegate.md", text)                         # the artifact pointer


class AlertKindStampTest(unittest.TestCase):
    """어느 알림 분기가 실제 발화했는지가 어사인먼트에 남아야 한다.

    reconcile_merged의 거짓 경보 자가 감지는 이 스탬프를 읽는다. 도달 가능성은
    가정하지 않는다 — "사실상 도달 불가"로 믿는 방어 분기도 회귀로 발화하기
    시작하면 그대로 감사에 잡혀야 한다. assert는 상수가 아니라 문자열 리터럴로:
    이 값은 git-tracked 어사인먼트 JSON에 남는 와이어 포맷이다."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.sent: list = []
        self.links: list = []           # the `link` (열기 button) arg per notify call
        self.orch = Orchestrator(
            self.mb, self.reg, self.ledger,
            notify=lambda t, link=None: (self.sent.append(t), self.links.append(link)))
        # Line 2 is the task title; stub to slug so the real Todo/items/ is untouched.
        self.orch._task_title = lambda slug: slug

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _collect(self, type_, payload, slug="t"):
        """Dispatch, reply with `payload`, and let collect() route it. mark_done /
        task_status are patched so the real Todo/items/ tree is never touched."""
        a = self.orch.dispatch(Task(slug, {"summary": "x"}))
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=type_, frm="w",
                                task_id=a.task_id, corr_id=a.corr_id, payload=payload))
        with mock.patch.object(tasks, "mark_done", lambda slug, **kw: None), \
                mock.patch.object(tasks, "task_status", return_value="open"):
            self.orch.collect()
        return self.ledger.load(slug)

    def test_zero_commit_result_stamps_result_no_pr(self):
        got = self._collect(m.RESULT, {"branch": "worker/t", "review_required": True,
                                       "stages": ["delegate"],
                                       "run_dir": "/v/Worker/runs/x-t"})
        self.assertEqual(got.last_alert_kind, "result-no-pr")

    def test_pr_review_parking_stamps_review_pr(self):
        got = self._collect(m.RESULT, {"branch": "worker/t", "review_required": True,
                                       "pr_url": "https://x/pull/1"})
        self.assertEqual(got.last_alert_kind, "review-pr")

    def test_blocked_fail_stamps_blocked(self):
        got = self._collect(m.FAIL, {"blocked_on": "upstream-task", "verify": "FAIL"})
        self.assertEqual(got.last_alert_kind, "blocked")

    def test_defensive_fail_branch_stamps_failure_suspected(self):
        # collect() routes blockerless FAILs to _retry, so this branch is
        # defensive-only post-PR #89 — which is exactly why the stamp exists:
        # if it ever fires again, the framing must be on record.
        a = self.orch.dispatch(Task("t", {"summary": "x"}))
        msg = m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="w", task_id="t",
                         corr_id=a.corr_id,
                         payload={"stages": ["research", "plan", "implement"],
                                  "run_dir": "/v/Worker/runs/x-t"})
        self.orch._notify_review(a, msg)
        self.assertEqual(a.last_alert_kind, "failure-suspected")

    def test_a_notify_crash_does_not_lose_the_stamp(self):
        # The stamp is set where head is chosen, OUTSIDE the notify try — a kakao
        # failure loses the ping, never the record.
        self.orch.notify = mock.Mock(side_effect=RuntimeError("kakao down"))
        a = self.orch.dispatch(Task("t", {"summary": "x"}))
        msg = m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="w", task_id="t",
                         corr_id=a.corr_id, payload={})
        self.orch._notify_review(a, msg)
        self.assertEqual(a.last_alert_kind, "failure-suspected")

    def test_poison_quarantine_stamps_before_the_ledger_save(self):
        # dispatch() saves the quarantined assignment BEFORE _notify_poison runs,
        # so the stamp must be set before that save — a set inside the notifier
        # would never persist. Reload from disk to pin the ordering.
        self.ledger.save(Assignment(task_id="t", agent_id="w", corr_id="c",
                                    kind="ai-local", state=A.FAILED, refail_count=3))
        self.assertIsNone(self.orch.dispatch(Task("t")))
        got = self.ledger.load("t")
        self.assertEqual(got.state, A.AWAITING_HUMAN)
        self.assertEqual(got.last_alert_kind, "quarantine")


class FalseAlarmEpisodeTest(unittest.TestCase):
    """실패 프레이밍 파킹 → 양성 reconcile 종결 = 거짓 경보. 사람 개입 없이
    Memory/episodes/에 자가 기록되어야 한다 (2026-09-04 dripcoffee-integcheck-
    advote: 10:32 실패 프레이밍 알림 → 10:52 "todo already closed" 양성 종결).

    Ledger는 vault 모양 tmp(`tmp/Worker/assignments`)를 쓴다 — 훅이 에피소드
    디렉토리를 ledger root 기준(root.parent.parent/Memory/episodes)으로 유도하므로,
    기존 테스트들의 `tmp/assignments` 모양이면 tmp *바깥*(시스템 임시 루트)으로
    샌다. 실제 vault 경로는 어디에도 등장하지 않는다."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "Worker" / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.sent: list = []
        self.orch = Orchestrator(self.mb, self.reg, self.ledger,
                                 notify=lambda t, link=None: self.sent.append(t))
        self.episodes = self.tmp / "Memory" / "episodes"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _park_failure_framed(self, slug="t"):
        """방어 분기(blocker/pr/RESULT 아님)를 실제로 발화시켜 failure-suspected로
        파킹된 어사인먼트를 만든다 — 상류 ReviewNotifyTest의 직접 호출 패턴."""
        a = Assignment(task_id=slug, agent_id="w", corr_id="C1", kind="ai-local",
                       state=A.AWAITING_HUMAN)
        a.last_note = "verify: 실패 — 원인 불명"
        msg = m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="w", task_id=slug,
                         corr_id="C1", payload={"run_dir": f"/v/Worker/runs/x-{slug}"})
        self.orch._notify_review(a, msg)          # stamps failure-suspected
        self.ledger.save(a)
        return a

    def _reconcile(self, gh=None, todo="done"):
        with mock.patch.object(stages, "gh_pr_merged", return_value=gh), \
             mock.patch.object(tasks, "task_status", return_value=todo), \
             mock.patch.object(self.orch, "_close_task"):
            return self.orch.reconcile_merged()

    def test_failure_framed_parking_closed_benign_writes_an_episode(self):
        """[AC2] 실패 프레이밍 파킹이 양성 노트로 닫히면 에피소드가 생긴다."""
        self._park_failure_framed()
        out = self._reconcile()
        self.assertEqual([x.task_id for x in out], ["t"])
        self.assertEqual(self.ledger.load("t").state, A.DONE)
        files = list(self.episodes.glob("*-false-alarm-t.md"))
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        self.assertIn("failure-suspected", text)
        self.assertIn("todo already closed", text)          # 양성 종결 노트
        # 파킹 시점의 사유가 남아야 한다 — 훅이 reconcile의 last_note 덮어쓰기
        # *앞*에 있음을 고정 (뒤로 옮기면 여기엔 reconciled 노트가 들어간다).
        self.assertIn("verify: 실패 — 원인 불명", text)

    def test_quarantined_task_closed_benign_writes_an_episode(self):
        """격리(poison)도 실패 프레이밍이다 — dispatch 종단으로 검증."""
        self.ledger.save(Assignment(task_id="q", agent_id="w", corr_id="c",
                                    kind="ai-local", state=A.FAILED, refail_count=3))
        self.assertIsNone(self.orch.dispatch(Task("q")))     # → quarantine + stamp
        self.assertEqual(self.ledger.load("q").state, A.AWAITING_HUMAN)
        out = self._reconcile()
        self.assertEqual([x.task_id for x in out], ["q"])
        self.assertEqual(len(list(self.episodes.glob("*-false-alarm-q.md"))), 1)

    def test_pr_review_parking_writes_no_episode(self):
        """[AC3] 양성 프레이밍(PR 리뷰 대기)의 종결은 기록 대상이 아니다."""
        a = self.orch.dispatch(Task("t", {"summary": "x"}))
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.RESULT, frm="w",
                                task_id="t", corr_id=a.corr_id,
                                payload={"review_required": True, "branch": "worker/t",
                                         "pr_url": "https://x/pull/1"}))
        with mock.patch.object(tasks, "mark_done", lambda slug, **kw: None), \
                mock.patch.object(tasks, "task_status", return_value="open"):
            self.orch.collect()
        parked = self.ledger.load("t")
        self.assertEqual(parked.state, A.AWAITING_HUMAN)
        self.assertEqual(parked.last_alert_kind, "review-pr")
        out = self._reconcile(gh=True)                       # PR이 실제로 머지됨
        self.assertEqual([x.task_id for x in out], ["t"])
        self.assertEqual(self.ledger.load("t").state, A.DONE)
        self.assertFalse(self.episodes.exists())

    def test_blocked_parking_writes_no_episode(self):
        """[AC3] blocked 파킹도 양성 프레이밍 — 종결돼도 에피소드 없음."""
        a = self.orch.dispatch(Task("b", {"summary": "x"}))
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="w",
                                task_id="b", corr_id=a.corr_id,
                                payload={"blocked_on": "upstream-task",
                                         "verify": "FAIL"}))
        self.orch.collect()                                  # parks + stamps "blocked"
        self.assertEqual(self.ledger.load("b").last_alert_kind, "blocked")
        out = self._reconcile()                              # todo 밖에서 닫힘
        self.assertEqual([x.task_id for x in out], ["b"])
        self.assertFalse(self.episodes.exists())

    def test_recurrence_appends_instead_of_new_file(self):
        """[AC4] 같은 태스크의 재발은 새 파일이 아니라 기존 에피소드에 append."""
        self._park_failure_framed()
        self._reconcile()
        self._park_failure_framed()                          # 재발: 다시 실패 프레이밍 파킹
        self._reconcile()
        files = list(self.episodes.glob("*-false-alarm-t.md"))
        self.assertEqual(len(files), 1)                      # 파일 수 그대로
        text = files[0].read_text(encoding="utf-8")
        self.assertEqual(text.count("양성 종결 기록"), 2)     # 종결 기록은 2개

    def test_a_broken_episode_write_does_not_cost_the_done_transition(self):
        """에피소드 실패 격리: 진단이 DONE 전이·잔여 스윕을 막으면 안 된다."""
        self._park_failure_framed()
        self.episodes.parent.mkdir(parents=True, exist_ok=True)
        self.episodes.write_text("디렉토리 자리를 파일이 점유", encoding="utf-8")
        out = self._reconcile()
        self.assertEqual([x.task_id for x in out], ["t"])    # 스윕 생존
        self.assertEqual(self.ledger.load("t").state, A.DONE)


if __name__ == "__main__":
    unittest.main()


class ReconcileClosedTest(unittest.TestCase):
    """play-store-3 (2026-09-10): ledger DONE, todo still open, three leaves
    blocked on it for 15 hours. The backstop closes exactly that shape and
    nothing else — not a fresh open task, not a task already done, not a row
    closed long ago (which a human may have reopened on purpose)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "assignments").mkdir()
        self.ledger = Ledger(self.tmp / "assignments")
        self.orch = Orchestrator(FileMailbox(self.tmp / "mb"), Registry([]), self.ledger,
                                 notify=lambda t, level=None: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _row(self, slug, state, age_s=60):
        a = Assignment(task_id=slug, agent_id="w", corr_id="c", kind="ai-local", state=state)
        a.updated_ts = (dt.datetime.now() - dt.timedelta(seconds=age_s)).isoformat(timespec="seconds")
        self.ledger.save(a)
        return a

    def test_done_but_open_is_reclosed(self):
        self._row("ps3", A.DONE)
        closed = []
        with mock.patch.object(tasks, "task_status", return_value="open"), \
                mock.patch.object(tasks, "mark_done", side_effect=lambda s, **kw: closed.append(s) or True):
            self.assertEqual(self.orch.reconcile_closed(), ["ps3"])
        self.assertEqual(closed, ["ps3"])

    def test_agreeing_rows_and_old_rows_are_left_alone(self):
        self._row("done-ok", A.DONE)                      # todo says done too
        self._row("still-running", A.IN_PROGRESS)         # not done yet
        self._row("ancient", A.DONE, age_s=3 * 86400)     # maybe reopened on purpose
        with mock.patch.object(tasks, "task_status",
                               side_effect=lambda s: "done" if s == "done-ok" else "open"), \
                mock.patch.object(tasks, "mark_done") as md:
            self.assertEqual(self.orch.reconcile_closed(), [])
        md.assert_not_called()

    def test_a_failing_close_is_reported_not_swallowed(self):
        self._row("boom", A.DONE)
        buf = io.StringIO()
        with mock.patch.object(tasks, "task_status", return_value="open"), \
                mock.patch.object(tasks, "mark_done", side_effect=OSError("disk")), \
                contextlib.redirect_stdout(buf):
            self.assertEqual(self.orch.reconcile_closed(), [])
        self.assertIn("boom", buf.getvalue())
        self.assertIn("OSError", buf.getvalue())

    def test_close_task_failure_is_logged(self):
        buf = io.StringIO()
        with mock.patch.object(tasks, "mark_done", side_effect=RuntimeError("locked")), \
                mock.patch.object(self.orch, "_propagate_downstream"), contextlib.redirect_stdout(buf):
            self.orch._close_task("ps3")
        self.assertIn("mark_done(ps3) failed", buf.getvalue())
        self.assertIn("RuntimeError", buf.getvalue())


class _TmpVaultCase(unittest.TestCase):
    """A vault-shaped tmp tree driven by the REAL close/reopen code: the ledger at
    tmp/Worker/assignments, task markdown + todo.db under tmp/Todo, and cli's
    module paths pointed there, so mark_done, task_status and `cli reopen` read
    and write inside tmp — never the live Todo/items or todo.db. (cmd_reopen also
    regenerates NOW/FOCUS/Dashboard from module-level paths, patched here too; its
    worker panel reads ROOT/Worker/queue.json, which is tmp.)

    Inputs are real transformations — mark_done's close, cmd_reopen's reopen, a
    frontmatter hand edit — never hand-built "after" strings, so a verdict here is
    about what those operations actually leave on disk."""

    def setUp(self):
        from todo_system import cli
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        todo = self.tmp / "Todo"
        self.items = todo / "items"
        self.items.mkdir(parents=True)
        self.db = todo / "todo.db"
        for name, value in (("ROOT", self.tmp), ("TODO_DIR", todo),
                            ("ITEMS_DIR", self.items), ("DB_PATH", self.db),
                            ("NOW_PATH", todo / "NOW.md"), ("FOCUS_PATH", todo / "FOCUS.md"),
                            ("DASHBOARD_PATH", todo / "Dashboard.md")):
            patcher = mock.patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "Worker" / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        # alert_route stubbed: the default router keeps its dedup state under the
        # real vault's logs/, and a blocked FAIL does alert.
        self.orch = Orchestrator(self.mb, self.reg, self.ledger,
                                 notify=lambda *a, **k: None,
                                 alert_route=lambda text, **kw: ("send", text))
        self.today = dt.date.today().isoformat()

    def _p(self, slug) -> str:
        """Write the adversarial pre-close markdown P and return it. No checkboxes
        and `updated:` already today — a task created and finished the same day,
        with no boxes like the incident's own md — so a close changes nothing but
        `status:`, and a reopen (cmd_reopen is mark_done's exact inverse) restores
        P's bytes unless the close left something else behind."""
        text = (f"---\ntags:\n  - todo\nstatus: open\nupdated: {self.today}\n"
                f"worker_mode: auto\n---\n\n# {slug}\n\n## Progress Log\n\n"
                f"- {self.today}: 생성\n")
        (self.items / f"{slug}.md").write_text(text, encoding="utf-8")
        return text

    def _dispatch(self, slug):
        return self.orch.dispatch(Task(slug, {"summary": slug, "branch": f"worker/{slug}"}))

    def _reply(self, a, type_, payload):
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=type_, frm="w",
                                task_id=a.task_id, corr_id=a.corr_id, payload=payload))

    def _collect(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.orch.collect()

    def _close_via_result(self, slug, lose=False):
        """dispatch -> RESULT -> collect(): the auto-merged close. `lose` fails the
        RESULT-time write the way play-store-3's was lost (2026-09-10)."""
        self._reply(self._dispatch(slug), m.RESULT, {"branch": f"worker/{slug}"})
        lost = (mock.patch.object(tasks, "mark_done", side_effect=OSError("write lost"))
                if lose else contextlib.nullcontext())
        with lost:
            self._collect()
        self.assertEqual(self.ledger.load(slug).state, A.DONE)

    def _close_via_merged(self, slug):
        """A PR-review park that reconcile_merged closes once gh says merged —
        the incident's 06:47:40 `reconciled-merged` close."""
        self.ledger.save(Assignment(task_id=slug, agent_id="w", corr_id="C1",
                                    kind="ai-local", state=A.AWAITING_HUMAN,
                                    payload={"branch": f"worker/{slug}"}))
        with mock.patch.object(stages, "gh_pr_merged", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual([x.task_id for x in self.orch.reconcile_merged()], [slug])

    def _edit(self, slug, **fm):
        """A human's hand edit of the frontmatter (the 06:49 f2c1e1118 commit)."""
        from todo_system import cli
        path = self.items / f"{slug}.md"
        text = path.read_text(encoding="utf-8")
        for key, value in fm.items():
            text = cli.update_frontmatter_value(text, key, value)
        path.write_text(text, encoding="utf-8")

    def _cli_reopen(self, slug):
        """The official reopen: `python -m todo_system.cli reopen <slug>`."""
        from todo_system import cli
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cli.cmd_reopen(argparse.Namespace(slug=slug, db=str(self.db), subtask=None))
        self.assertEqual(rc, 0)

    def _tick(self):
        """One tick() with reconcile_closed REAL and every other sweep stubbed."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = _hermetic_tick(self.orch, [], live=("reconcile_closed",))
        return result, buf.getvalue()


class HumanReopenRespectedTest(_TmpVaultCase):
    """2026-09-17 macp-dashboard-public-access: reconcile_merged closed the task at
    06:47:40, a human reopened it at 06:49 (f2c1e1118: `status: open`,
    `worker_mode: manual`), and the next tick's reconcile_closed read ledger=done +
    todo=open as a lost close and closed it again, logging "the RESULT-time close
    did not land". The Funnel stayed off while the task sat in no human-waiting
    list. The 24h backstop only ever protected a reopen that came a day later.

    A lost or rewound close leaves the md exactly as it was before the close; a
    reopen does not. Every fixture is the adversarial P from `_p`."""

    def test_incident_reopen_survives_tick(self):
        pre = {s: self._p(s) for s in ("dash-edit", "res-cli", "res-status")}
        for text in pre.values():                # P really is the adversarial shape
            self.assertNotIn("- [", text)
            self.assertIn(f"updated: {self.today}", text)
        self._close_via_merged("dash-edit")                             # 06:47:40
        self._edit("dash-edit", status="open", worker_mode="manual")    # 06:49
        self._close_via_result("res-status")
        self._edit("res-status", status="open")                         # status line only
        self._close_via_result("res-cli")
        self._cli_reopen("res-cli")                                     # the official reopen
        logs = []
        for _ in range(2):                                              # the next two ticks
            result, out = self._tick()
            self.assertEqual(result["closed"], [])
            logs.append(out)
        logged = "".join(logs)
        self.assertNotIn("the RESULT-time close did not land", logged)
        for slug in pre:
            self.assertEqual(tasks.task_status(slug), "open", slug)
            said = [ln for ln in logged.splitlines() if slug in ln and "human reopen" in ln]
            self.assertEqual(len(said), 1, logged)          # said once, not every tick
            history = self.ledger.load(slug).history
            self.assertEqual(sum(h.get("type") == "reopen-respected" for h in history), 1)

    def test_lost_and_rewound_closes_reclose_but_a_reopen_does_not(self):
        # The recovery reconcile_closed exists for (play-store-3) must survive the
        # fix — in the SAME sweep as a reopen that it must now leave alone.
        pre = {s: self._p(s) for s in ("lost", "reopened", "rewound")}
        self._close_via_result("lost", lose=True)       # the close write never landed
        self._close_via_result("rewound")
        (self.items / "rewound.md").write_bytes(pre["rewound"].encode("utf-8"))  # sync rewound it
        self._close_via_result("reopened")
        self._cli_reopen("reopened")
        result, out = self._tick()
        self.assertEqual(result["closed"], ["lost", "rewound"])
        self.assertEqual(tasks.task_status("lost"), "done")
        self.assertEqual(tasks.task_status("rewound"), "done")
        self.assertEqual(tasks.task_status("reopened"), "open")
        self.assertEqual(out.count("the RESULT-time close did not land"), 2)

    def test_retry_found_closed_row_is_not_reclosed_after_reopen(self):
        # _retry's DONE path (the todo was already closed when a FAIL arrived)
        # writes nothing at all, so any later `open` is someone's reopen.
        self._p("retry")
        self._edit("retry", status="done")              # closed outside the pipeline
        self._reply(self._dispatch("retry"), m.FAIL, {})
        self._collect()
        row = self.ledger.load("retry")
        self.assertEqual(row.state, A.DONE)
        self.assertTrue(any(h.get("type") == "retry-skipped-closed" for h in row.history))
        self._cli_reopen("retry")
        result, _ = self._tick()
        self.assertEqual(result["closed"], [])
        self.assertEqual(tasks.task_status("retry"), "open")

    def test_row_without_evidence_keeps_legacy_reclose(self):
        # A row written by an older build carries no evidence: keep the old
        # unconditional re-close rather than guess.
        self._p("legacy")
        raw = json.loads(Assignment(task_id="legacy", agent_id="w", corr_id="C1",
                                    kind="ai-local", state=A.DONE).to_json())
        raw.pop("close_fingerprint", None)              # as an older build wrote it
        self.ledger.root.mkdir(parents=True, exist_ok=True)
        (self.ledger.root / "legacy.json").write_text(json.dumps(raw), encoding="utf-8")
        result, out = self._tick()
        self.assertEqual(result["closed"], ["legacy"])
        self.assertEqual(tasks.task_status("legacy"), "done")
        self.assertIn("the RESULT-time close did not land", out)


class MergedButBlockedTest(_TmpVaultCase):
    """The 06:47:40 half of the incident: reconcile_merged closed a row whose
    Verify had named a step still owed by a human (`blocked_on`), because its PR
    had merged. Starts from the row collect() really parks — the incident's
    history reads FAIL 19:42:38 → reconciled-merged, stamped `blocked`."""

    # verbatim from Worker/assignments/macp-dashboard-public-access.json
    BLOCKER = ("AC4/AC5 need a human on acme-hq: sudo-enable Tailscale Funnel "
               "(operator/sudo required — a forbidden hard rule, and `tailscale` is "
               "not in the verify whitelist), then from the phone with Tailscale OFF "
               "confirm no-token→401 / token→200 on the public DNS host. The code "
               "half (AC1–AC3, AC6) is complete and green.")

    def _merged_sweep(self):
        with mock.patch.object(stages, "gh_pr_merged", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()):
            return self.orch.reconcile_merged()

    def test_incident_blocked_row_parks_until_human_closes(self):
        self._p("dash")
        self._reply(self._dispatch("dash"), m.FAIL,
                    {"blocked_on": self.BLOCKER, "verify": "FAIL"})
        self._collect()
        parked = self.ledger.load("dash")
        self.assertEqual(parked.state, A.AWAITING_HUMAN)
        self.assertEqual(parked.last_alert_kind, "blocked")
        # PR #129 merges while the Funnel is still off
        self.assertEqual(self._merged_sweep(), [])
        row = self.ledger.load("dash")
        self.assertEqual(row.state, A.AWAITING_HUMAN)
        self.assertEqual(row.blocked_on, self.BLOCKER)
        self.assertEqual(tasks.task_status("dash"), "open")
        board = dashboard.render(self.ledger, self.reg)
        waiting = board.split("## ⏳ 사람 대기")[1].split("## ❌")[0]
        self.assertIn("`dash`", waiting)
        self.assertIn("⛔ blocked_on: AC4/AC5", waiting)
        # the human finishes AC4/AC5 and closes the md: the existing path ends it
        self._edit("dash", status="done")
        self.assertEqual([x.task_id for x in self._merged_sweep()], ["dash"])
        row = self.ledger.load("dash")
        self.assertEqual(row.state, A.DONE)
        self.assertIn("todo already closed", row.last_note)

    def test_real_merged_park_renders_the_close_exit(self):
        # dashboard tells a merged park apart by the history stamp this sweep
        # writes; a row parked by the REAL reconcile_merged is what pins the two
        # copies of that literal together.
        self._p("dash")
        self._reply(self._dispatch("dash"), m.FAIL,
                    {"blocked_on": self.BLOCKER, "verify": "FAIL"})
        self._collect()

        def waiting():
            board = dashboard.render(self.ledger, self.reg)
            return board.split("## ⏳ 사람 대기")[1].split("## ❌")[0]

        before = waiting()             # blocked, PR not merged yet: the re-queue recipe
        self.assertIn("worker_mode", before)
        self.assertNotIn("status: done", before)
        self.assertEqual(self._merged_sweep(), [])
        after = waiting()
        self.assertIn("⛔ blocked_on: AC4/AC5", after)
        self.assertIn("status: done", after)
        self.assertNotIn("worker_mode", after)

    def test_real_merged_park_trace_says_close_not_requeue(self):
        # `macp trace` must give the same exit as the dashboard for a row the REAL
        # sweep parked — both read the stamp through the one _merged_park.
        self._p("dash")
        self._reply(self._dispatch("dash"), m.FAIL,
                    {"blocked_on": self.BLOCKER, "verify": "FAIL"})
        self._collect()

        def ledger_part():
            md = trace.render("dash", self.ledger, runs_dir=self.tmp / "runs",
                              repo_dir=self.tmp, pr_lookup=lambda *a, **k: None,
                              branch_lookup=lambda *a, **k: False)
            return md.split("## 원장")[1].split("## 브랜치")[0]

        before = ledger_part()         # blocked, PR not merged yet: the re-queue recipe
        self.assertIn("worker_mode", before)
        self.assertNotIn("status: done", before)
        self.assertEqual(self._merged_sweep(), [])
        after = ledger_part()
        self.assertIn("- blocked_on: AC4/AC5", after)
        self.assertIn("status: done", after)
        self.assertIn("reconcile_merged()", after)
        self.assertNotIn("worker_mode", after)
        self.assertNotIn("reconcile_unparked", after)


class DiscoverMergeDecisionTest(unittest.TestCase):
    def test_discover_never_parks_for_review_even_with_auto_merge_false(self):
        from todo_system.orchestrator.runner import StagedExecutor
        self.assertEqual(StagedExecutor._merge_decision(
            {"worker_mode": "discover", "auto_merge": False}), (False, False))
        # the rule it bypasses still holds for everyone else
        review, merge = StagedExecutor._merge_decision({"worker_mode": "auto", "auto_merge": False})
        self.assertTrue(review)


class PullMainRaceTest(unittest.TestCase):
    """`_pull_main` rides out a concurrent ref move, falls back to a merge for a
    genuine divergence, and never swallows a persistent failure in a truncated
    line. All git is faked through `_run`; no real pull ever executes."""

    def _fake(self, head="main", merge_rcs=None, merge_stderr="", fetch_rc=0):
        merge_rcs = list(merge_rcs or [])
        calls = []

        def run(argv, **kw):
            calls.append(list(argv[1:]))
            sub = argv[1]
            if sub == "rev-parse":
                return subprocess.CompletedProcess(argv, 0, head + "\n", "")
            if sub == "fetch":
                return subprocess.CompletedProcess(
                    argv, fetch_rc, "", "net" if fetch_rc else "")
            if sub == "merge":
                if "--abort" in argv:
                    return subprocess.CompletedProcess(argv, 0, "", "")
                rc = merge_rcs.pop(0) if merge_rcs else 0
                return subprocess.CompletedProcess(
                    argv, rc, "", merge_stderr if rc else "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        return run, calls

    def _pull(self, run, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _pull_main(Path("/x"), _run=run, _sleep=lambda *_a: None, **kw)
        return buf.getvalue()

    def test_pure_behind_fast_forwards_without_a_merge_commit(self):
        run, calls = self._fake(merge_rcs=[0])
        out = self._pull(run)
        merges = [c for c in calls if c[0] == "merge"]
        self.assertEqual(len(merges), 1)          # no non-ff fallback, no abort
        self.assertIn("--ff-only", merges[0])
        self.assertEqual(out, "")                 # silent success

    def test_a_transient_race_is_retried_not_reported(self):
        # first round both the ff-only and the real merge fail (the ref moved
        # under us); the second round's fast-forward lands. No error line.
        run, calls = self._fake(merge_rcs=[1, 1, 0])
        out = self._pull(run)
        self.assertEqual(out, "")
        self.assertEqual(len([c for c in calls if c[0] == "fetch"]), 2)

    def test_genuine_divergence_falls_back_to_a_merge_commit(self):
        # ff-only refuses (local is ahead); the real merge succeeds first try.
        run, calls = self._fake(merge_rcs=[1, 0])
        out = self._pull(run)
        self.assertEqual(out, "")
        merges = [c for c in calls if c[0] == "merge"]
        self.assertIn("--ff-only", merges[0])
        self.assertIn("--no-edit", merges[1])

    def test_a_persistent_failure_is_logged_in_full_not_truncated(self):
        long = "E" * 300
        run, _ = self._fake(merge_rcs=[1] * 9, merge_stderr=long)
        out = self._pull(run, attempts=3)
        self.assertIn("hq-host pull: non-fatal", out)
        self.assertIn(long, out)                  # the whole tail, not [:200]
        self.assertGreater(len(out), 300)

    def test_head_not_main_touches_nothing(self):
        run, calls = self._fake(head="worker/some-task", merge_rcs=[0])
        out = self._pull(run)
        self.assertEqual(out, "")
        self.assertEqual([c[0] for c in calls], ["rev-parse"])

    def test_fetch_failure_retries_then_gives_up_non_fatally(self):
        # every fetch fails outright (offline) — merge is never reached.
        run, calls = self._fake(fetch_rc=1)
        out = self._pull(run, attempts=2)
        self.assertIn("hq-host pull: non-fatal", out)
        fetches = [c for c in calls if c[0] == "fetch"]
        merges = [c for c in calls if c[0] == "merge"]
        self.assertEqual(len(fetches), 2)         # attempts exhausted
        self.assertEqual(len(merges), 0)          # never got past fetch


class CheckDivergenceTest(unittest.TestCase):
    """`_check_divergence` counts behind/ahead, tracks a consecutive-behind
    streak across ticks, alerts at the threshold, and persists to a host-local
    file — never raising into the tick. Git is faked through `_run`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake(self, left_right="7\t188", count_rc=0, fetch_rc=0):
        def run(argv, **kw):
            sub = argv[1]
            if sub == "fetch":
                return subprocess.CompletedProcess(argv, fetch_rc, "", "")
            if sub == "rev-list":
                return subprocess.CompletedProcess(
                    argv, count_rc, left_right + "\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return run

    def _state(self):
        return json.loads((self.tmp / "Worker" / "_state" / "divergence.json")
                          .read_text(encoding="utf-8"))

    def test_streak_grows_and_alerts_at_the_threshold(self):
        run = self._fake("7\t188")
        for expected in (1, 2, 3):
            st = _check_divergence(self.tmp, _run=run, threshold=3)
            self.assertEqual(st["behind"], 7)
            self.assertEqual(st["ahead"], 188)
            self.assertEqual(st["consecutive_behind"], expected)
            self.assertEqual(st["alert"], expected >= 3)
        # persisted where the dashboard reads it
        self.assertEqual(self._state()["consecutive_behind"], 3)
        self.assertTrue(self._state()["alert"])

    def test_a_synced_tick_resets_the_streak(self):
        behind = self._fake("4\t0")
        _check_divergence(self.tmp, _run=behind, threshold=3)
        _check_divergence(self.tmp, _run=behind, threshold=3)
        self.assertEqual(self._state()["consecutive_behind"], 2)
        st = _check_divergence(self.tmp, _run=self._fake("0\t9"), threshold=3)
        self.assertEqual(st["behind"], 0)
        self.assertEqual(st["consecutive_behind"], 0)
        self.assertFalse(st["alert"])

    def test_a_failed_count_never_raises_and_still_persists(self):
        st = _check_divergence(self.tmp, _run=self._fake(count_rc=1), threshold=3)
        self.assertEqual(st["behind"], 0)
        self.assertEqual(st["consecutive_behind"], 0)
        self.assertTrue((self.tmp / "Worker" / "_state" / "divergence.json").exists())

    def test_a_run_exception_is_caught_not_raised(self):
        def raising(argv, **kw):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=120)

        st = _check_divergence(self.tmp, _run=raising, threshold=3)
        self.assertEqual(st["behind"], 0)
        self.assertEqual(st["ahead"], 0)
        self.assertIn("last_detail", st)
        self.assertNotEqual(st["last_detail"], "")

    def test_unparseable_prior_streak_resets_to_zero(self):
        state_dir = self.tmp / "Worker" / "_state"
        state_dir.mkdir(parents=True)
        (state_dir / "divergence.json").write_text(
            json.dumps({"consecutive_behind": "not-a-number"}), encoding="utf-8")
        st = _check_divergence(self.tmp, _run=self._fake("1\t2"), threshold=3)
        self.assertEqual(st["consecutive_behind"], 1)  # prev_streak reset to 0, then +1

    def test_state_persist_failure_is_swallowed(self):
        (self.tmp / "Worker").write_text("x", encoding="utf-8")  # blocks mkdir
        st = _check_divergence(self.tmp, _run=self._fake("0\t0"), threshold=3)
        self.assertEqual(st["behind"], 0)
        self.assertFalse(st["alert"])


class MinProgressModeTest(unittest.TestCase):
    """Opt-in minimum-progress mode (USAGE_GUARD_MIN_PROGRESS): during a PACE
    usage hold with nothing live, tick() sends exactly one task — the top of
    load_dispatchable's priority order — with `bypass_guard` on the envelope
    only. Hard-cap / paused / no-data holds, an explicit pause and the daily
    budget still dispatch nothing; the switch is off by default."""

    ETA = "2026-10-30T03:00:00+00:00"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
        self.orch = Orchestrator(self.mb, self.reg, self.ledger, notify=lambda *a, **k: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pace(self) -> dict:
        return {"allowed": False,
                "reason": "session 70% ahead of pace (10% elapsed + 30pp allowance)",
                "blocked_until": 1_900_000_000.0, "release_eta": self.ETA,
                "windows": [{"name": "session", "blocked": True,
                             "utilization": 70.0, "elapsed_pct": 10.0,
                             "reason": "session 70% ahead of pace"}]}

    def _hard_cap(self) -> dict:
        cap = usage_guard._cfg()["weekly_hard_cap"]
        return {"allowed": False,
                "reason": f"weekly {cap + 1:.0f}% >= hard cap {cap:.0f}% (until reset)",
                "blocked_until": 1_900_000_000.0, "release_eta": self.ETA,
                "windows": [{"name": "weekly", "blocked": True,
                             "utilization": cap + 1.0, "elapsed_pct": 50.0,
                             "reason": "weekly >= hard cap (until reset)"}]}

    def _tick(self, tasks_, decision, *, env="1", **kw):
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(usage_guard, "check", return_value=decision))
            st.enter_context(mock.patch.object(usage_guard, "refresh_inactive_accounts",
                                               return_value={}))
            st.enter_context(mock.patch.dict(os.environ))
            if env is None:
                os.environ.pop("USAGE_GUARD_MIN_PROGRESS", None)
            else:
                os.environ["USAGE_GUARD_MIN_PROGRESS"] = env
            return _hermetic_tick(self.orch, tasks_, usage_hold=_INHERIT, **kw)

    def _two(self):
        return [Task("task-high"), Task("task-low")]   # load_dispatchable order

    def test_pace_hold_dispatches_only_top_priority_task(self):
        r = self._tick(self._two(), self._pace())
        self.assertEqual([a.task_id for a in r["dispatched"]], ["task-high"])
        sent = self.mb.receive("w")
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].payload.get("bypass_guard"))
        self.assertTrue(sent[0].payload.get("min_progress"))
        row = self.ledger.load("task-high")
        self.assertFalse((row.payload or {}).get("bypass_guard"))   # envelope only
        self.assertTrue(any(h["type"] == "min-progress-dispatch" for h in row.history))

    def test_pace_hold_with_live_run_dispatches_nothing(self):
        self.ledger.save(Assignment(task_id="task-other", agent_id="w", corr_id="C0",
                                    state=A.IN_PROGRESS, kind="ai-local"))
        r = self._tick(self._two(), self._pace())
        self.assertEqual(r["dispatched"], [])
        self.assertEqual(self.mb.receive("w"), [])

    def test_hard_cap_hold_dispatches_nothing(self):
        r = self._tick(self._two(), self._hard_cap())
        self.assertEqual(r["dispatched"], [])
        self.assertEqual(self.mb.receive("w"), [])

    def test_paused_decision_dispatches_nothing(self):
        d = {"allowed": False, "paused": True, "reason": "all accounts unavailable: a(x)",
             "blocked_until": None, "release_eta": None, "windows": []}
        self.assertEqual(self._tick(self._two(), d)["dispatched"], [])

    def test_carried_no_data_hold_dispatches_nothing(self):
        d = dict(self._pace(), carried=True, stale=True,
                 reason="no usage data (offline) — holding last known block: x")
        self.assertEqual(self._tick(self._two(), d)["dispatched"], [])

    def test_explicit_pause_and_budget_block_min_progress(self):
        self.assertEqual(self._tick(self._two(), self._pace(), paused=True)["dispatched"], [])
        self.assertEqual(
            self._tick(self._two(), self._pace(), budget_exceeded=True)["dispatched"], [])
        self.assertEqual(self.mb.receive("w"), [])

    def test_default_off_keeps_hold(self):
        self.assertEqual(self._tick(self._two(), self._pace(), env=None)["dispatched"], [])
        self.assertEqual(self._tick(self._two(), self._pace(), env="0")["dispatched"], [])
        self.assertEqual(self.mb.receive("w"), [])

    def test_top_task_in_cooldown_falls_through_to_next(self):
        self.ledger.save(Assignment(task_id="task-high", agent_id="w", corr_id="C0",
                                    state=A.FAILED, kind="ai-local", attempts=1,
                                    updated_ts=m.now_iso()))
        r = self._tick(self._two(), self._pace())
        self.assertEqual([a.task_id for a in r["dispatched"]], ["task-low"])

    def test_retry_of_min_progress_assignment_does_not_carry_bypass(self):
        self._tick(self._two(), self._pace())
        first = self.mb.receive("w")
        self.mb.ack("w", first)
        row = self.ledger.load("task-high")
        row.state = A.FAILED
        with mock.patch.object(tasks, "task_status", return_value="open"):
            self.orch._retry(row)
        resent = self.mb.receive("w")
        self.assertEqual(len(resent), 1)
        self.assertFalse((resent[0].payload or {}).get("bypass_guard"))

    def test_usage_hold_info_and_eta_in_tick_result(self):
        r = self._tick(self._two(), self._pace())
        self.assertEqual(r["usage_hold_eta"], self.ETA)
        self.assertEqual(self.orch.usage_hold_info["kind"], "pace")
        self.assertIsNone(self._tick([], {"allowed": True})["usage_hold_eta"])

    def test_min_progress_fail_open(self):
        with mock.patch.object(usage_guard, "min_progress_enabled",
                               side_effect=RuntimeError("boom")):
            r = self._tick(self._two(), self._pace())
        self.assertEqual(r["dispatched"], [])
        self.assertIsNotNone(r["usage_hold"])
