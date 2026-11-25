"""Free-slot dispatch: the orchestrator assigns a task only to a worker that has
a free slot, and takes back an assignment that no worker started.

Until 2026-11-25 the orchestrator pushed every runnable task into a worker's
mailbox and the runner judged usage and slots again before taking one out. A
task could sit `dispatched` for days in one mailbox while another worker idled.

Every case here drives the real `Orchestrator.tick()` and the real `Runner`
poll over one file mailbox. Only the executor (a gated fake) and the sweeps
that read `gh` or todo.db are stubbed.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import message as m
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator import tasks
from todo_system.orchestrator.assignment import Assignment, Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import Orchestrator, Task
from todo_system.orchestrator.registry import Agent, Registry
from todo_system.orchestrator.runner import ExecOutcome, Runner

DEFAULT = "worker-default"
REMOTE = "desktop-runner"

# Sweeps that read machine state outside the tmp vault. collect, reap_stale,
# reroute_remote and the dispatch pass stay real: they are the subject.
_STUBBED = ("reconcile_merged", "reconcile_closed", "reconcile_queue",
            "reconcile_parents", "reconcile_resumes", "reconcile_rescope",
            "reconcile_split_parents", "reconcile_human_ready",
            "_advote_bridge_tick", "_beta_sweep_tick", "_server_deploy_watch_tick")


def _ago(seconds: float) -> str:
    return (dt.datetime.now().astimezone()
            - dt.timedelta(seconds=seconds)).isoformat(timespec="seconds")


class _GatedExecutor:
    """Holds every run until released."""

    mode = "fake"

    def __init__(self):
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.started: list[str] = []

    def execute(self, msg, agent, progress, **_):
        with self._lock:
            self.started.append(msg.task_id)
        self.release.wait(10)
        return ExecOutcome(True, "gated ok", {})


class _Base(unittest.TestCase):
    CONCURRENCY = 2

    def setUp(self):
        self.vault = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        worker = self.vault / "Worker"
        self.uri = f"file://{worker / 'agents'}"
        self.mb = FileMailbox(worker / "agents")
        self.ledger = Ledger(worker / "assignments")
        self.reg = Registry([
            Agent(id=DEFAULT, kind="ai-local", projects=["*"],
                  concurrency=self.CONCURRENCY, mailbox=self.uri),
            Agent(id=REMOTE, kind="ai-remote", projects=[],
                  capabilities=["heavy", "device"], concurrency=1, mailbox=self.uri),
        ])
        self.orch = Orchestrator(self.mb, self.reg, self.ledger,
                                 notify=lambda *a, **k: None, default_uri=self.uri)
        for p in (
            mock.patch.multiple("todo_system.worker",
                                runner_heartbeat_start=mock.DEFAULT,
                                runner_heartbeat_clear=mock.DEFAULT),
            mock.patch.object(runner_mod, "_mem_available_mb", return_value=None),
            mock.patch.object(tasks, "task_status", return_value="open"),
            mock.patch.object(tasks, "mark_done", return_value=None),
        ):
            p.start()
            self.addCleanup(p.stop)

    # -- fixtures -------------------------------------------------------------
    def runner(self, agent_id: str = DEFAULT):
        ex = _GatedExecutor()
        r = Runner(self.mb, self.reg.get(agent_id), ex, log=lambda *_: None,
                   code_time_fn=lambda: None)      # never drains by itself

        def stop():
            r._drain_requested = True
            ex.release.set()
            self.wait(lambda: not r._inflight)

        self.addCleanup(stop)
        return r, ex

    def wait(self, pred, timeout: float = 10.0) -> None:
        end = time.time() + timeout
        while not pred() and time.time() < end:
            time.sleep(0.01)
        self.assertTrue(pred(), "timed out waiting")

    def task(self, slug: str, **extra) -> Task:
        # One repo per task: the per-checkout serial gate is not the subject.
        return Task(slug, {"summary": slug, "task_path": f"Todo/items/{slug}.md",
                           "branch": f"worker/{slug}",
                           "repo": f"https://example.invalid/acme/{slug}.git", **extra})

    def tick(self, task_list, *, usage_hold=None, paused=False):
        with contextlib.ExitStack() as st:
            for name in _STUBBED:
                st.enter_context(mock.patch.object(self.orch, name, return_value=[]))
            for name, value in (("paused", paused), ("budget_exceeded", False),
                                ("usage_hold", usage_hold)):
                st.enter_context(mock.patch.object(self.orch, name, return_value=value))
            return self.orch.tick(task_list)

    def inbox(self, agent_id: str, type_: str = m.TASK_ASSIGN) -> list[m.Envelope]:
        return [e for e in self.mb.receive(agent_id) if e.type == type_]

    def slugs(self, rows) -> list[str]:
        return [a.task_id for a in rows]

    def wait_reason(self, task_list, slug: str, **kw) -> dict:
        return {r["slug"]: r for r in self.orch.queue_snapshot(task_list, **kw)}[slug]


class FreeSlotTickTest(_Base):
    def test_no_free_slot_keeps_the_task_queued_and_a_slot_heartbeat_assigns_it(self):
        r, ex = self.runner()
        a, b = self.task("alpha"), self.task("beta")
        r._announce(1)                                     # one slot, free

        res = self.tick([a, b])
        self.assertEqual(self.slugs(res["dispatched"]), ["alpha"])
        self.assertIsNone(self.ledger.load("beta"))        # still in the queue
        self.assertEqual([e.task_id for e in self.inbox(DEFAULT)], ["alpha"])

        # The worker starts what it was given at once, then says it is full.
        self.assertEqual(r.poll_parallel(1), 1)
        self.wait(lambda: ex.started == ["alpha"])
        r._announce(1)
        res = self.tick([a, b])
        self.assertEqual(res["dispatched"], [])
        self.assertEqual(self.ledger.load("alpha").state, A.ACCEPTED)
        self.assertIsNone(self.ledger.load("beta"))
        self.assertEqual([e.task_id for e in self.inbox(DEFAULT)], ["alpha"])
        row = self.wait_reason([a, b], "beta")
        self.assertEqual(row["wait"], "no_slot")
        self.assertIn(DEFAULT, row["wait_text"])

        # The run ends and the slot heartbeat arrives: the NEXT tick assigns.
        ex.release.set()
        self.wait(lambda: not r._inflight)
        r._announce(1)
        res = self.tick([a, b])
        self.assertEqual(self.slugs(res["dispatched"]), ["beta"])
        self.assertEqual(self.ledger.load("alpha").state, A.DONE)
        self.assertEqual(self.ledger.load("beta").agent_id, DEFAULT)

    def test_the_announced_number_decides_not_the_registry(self):
        r, _ex = self.runner()
        r._announce(0)                                     # e.g. every account at its cap
        res = self.tick([self.task("alpha")])
        self.assertEqual(res["dispatched"], [])
        self.assertIsNone(self.ledger.load("alpha"))
        self.assertEqual(self.inbox(DEFAULT), [])

    def test_a_worker_that_never_announced_is_capped_by_its_concurrency(self):
        ts = [self.task(s) for s in ("alpha", "beta", "gamma")]
        res = self.tick(ts)
        self.assertEqual(self.slugs(res["dispatched"]), ["alpha", "beta"])   # CONCURRENCY
        self.assertIsNone(self.ledger.load("gamma"))
        self.assertEqual(self.wait_reason(ts, "gamma")["wait"], "no_slot")

    def test_a_worker_that_stopped_announcing_gets_nothing(self):
        r, _ex = self.runner()
        r._announce(2)
        self.tick([])                                      # the announcement is on record
        p = self.orch._slots_path()
        rec = json.loads(p.read_text(encoding="utf-8"))
        rec[DEFAULT]["ts"] = _ago(3600)
        p.write_text(json.dumps(rec), encoding="utf-8")
        a = self.task("alpha")
        res = self.tick([a])
        self.assertEqual(res["dispatched"], [])
        self.assertIsNone(self.ledger.load("alpha"))
        self.assertEqual(self.wait_reason([a], "alpha")["wait"], "no_slot")

    def test_a_heavy_task_goes_to_whichever_worker_is_free(self):
        local, _ = self.runner(DEFAULT)
        remote, _ = self.runner(REMOTE)
        local._announce(1)
        remote._announce(1)
        heavy = [self.task(s, heavy=True) for s in ("h1", "h2", "h3")]
        res = self.tick(heavy)
        # The heavy remote is free: it gets the first. It is then full, so the
        # second falls to the default worker, and the third waits in the queue.
        self.assertEqual(self.slugs(res["dispatched"]), ["h1", "h2"])
        self.assertEqual(self.ledger.load("h1").agent_id, REMOTE)
        self.assertEqual(self.ledger.load("h2").agent_id, DEFAULT)
        self.assertIsNone(self.ledger.load("h3"))
        self.assertEqual([e.task_id for e in self.inbox(REMOTE)], ["h1"])
        self.assertEqual([e.task_id for e in self.inbox(DEFAULT)], ["h2"])

    def test_a_plain_task_never_spills_to_the_heavy_remote(self):
        local, _ = self.runner(DEFAULT)
        remote, _ = self.runner(REMOTE)
        local._announce(0)
        remote._announce(1)
        res = self.tick([self.task("plain")])
        self.assertEqual(res["dispatched"], [])
        self.assertEqual(self.inbox(REMOTE), [])

    def test_bypass_guard_gets_one_slot_through_a_usage_hold(self):
        r, _ex = self.runner()
        r._announce(0)                                     # paced capacity under the hold
        ts = [self.task("plain"), self.task("urgent", bypass_guard=True),
              self.task("urgent-2", bypass_guard=True)]
        hold = "weekly 95% >= hard cap 95%"
        res = self.tick(ts, usage_hold=hold)
        self.assertEqual(self.slugs(res["dispatched"]), ["urgent"])
        self.assertEqual(self.wait_reason(ts, "plain", usage_hold=hold)["wait"], "usage_hold")
        self.assertEqual(self.wait_reason(ts, "urgent-2", usage_hold=hold)["wait"], "no_slot")

    def test_a_failed_run_is_not_resent_into_the_mailbox_under_a_hold(self):
        a = self.task("alpha")
        self.tick([a])
        row = self.ledger.load("alpha")
        self.mb.ack(DEFAULT, self.mb.receive(DEFAULT))     # the worker took it
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, frm=DEFAULT, type=m.FAIL,
                                task_id="alpha", corr_id=row.corr_id,
                                payload={"reason": "verify failed"}))
        self.tick([a], usage_hold="session 91% >= hard cap 80%")
        row = self.ledger.load("alpha")
        self.assertTrue(A.is_requeued(row))                # back in the queue
        self.assertEqual(row.display_state, A.DEFERRED)
        self.assertEqual((row.attempts, row.refail_count), (1, 0))
        self.assertEqual(self.inbox(DEFAULT), [])          # nothing waits in the mailbox


class RequeueUnstartedTest(_Base):
    """This week's case (2026-11-20 … 25): rows pushed into worker-default's
    mailbox on 11-20 and 11-21 sat `dispatched` for days under a usage hold."""

    # slug -> (seconds since dispatch, refail_count at the time)
    STUCK = {
        "dripcoffee-dial-5-app-snapshot-applier": (4 * 86400 + 17 * 3600, 0),
        "mindchat-prep-note-generator-2-format": (4 * 86400 + 6 * 3600, 1),
        "worldgraph-webtoon-v2-13-image-anatomy-spike-1-draw": (4 * 86400 + 6 * 3600, 0),
    }

    def _row(self, slug: str, age_s: float, refail: int = 0,
             state: str = A.DISPATCHED, in_mailbox: bool = True) -> Assignment:
        t = self.task(slug)
        env = m.Envelope(to=DEFAULT, type=m.TASK_ASSIGN, task_id=slug,
                         payload=t.payload, ts=_ago(age_s))
        if in_mailbox:
            self.mb.send(env)
        row = Assignment(task_id=slug, agent_id=DEFAULT, corr_id=env.corr_id,
                         state=state, kind="ai-local", refail_count=refail,
                         created_ts=_ago(age_s), updated_ts=_ago(age_s),
                         payload=t.payload)
        self.ledger.save(row)
        return row

    def setUp(self):
        super().setUp()
        self.before = {slug: self._row(slug, age, refail)
                       for slug, (age, refail) in self.STUCK.items()}
        self._row("just-assigned", 300)                         # inside the 30 minutes
        self._row("really-running", 1200, state=A.IN_PROGRESS, in_mailbox=False)

    def test_stuck_rows_go_back_to_the_queue_without_penalty(self):
        hold = "weekly 95% >= hard cap 95%"
        with mock.patch.object(self.orch, "_notify_routed") as alert, \
                mock.patch.object(self.orch, "notify") as notify:
            res = self.tick([self.task(s) for s in self.STUCK], usage_hold=hold)
        self.assertEqual(sorted(self.slugs(res["returned"])), sorted(self.STUCK))
        self.assertEqual(res["dispatched"], [])                 # the hold still holds
        for slug, (_age, refail) in self.STUCK.items():
            row = self.ledger.load(slug)
            self.assertTrue(A.is_requeued(row), slug)
            self.assertEqual(row.display_state, A.DEFERRED)
            self.assertEqual(row.refail_count, refail)          # no refail
            self.assertEqual(row.last_alert_kind, "")
            self.assertEqual(self.orch._ledger_gate(row).kind, "go")   # no cooldown, no poison
        cancels = {e.task_id: e.corr_id for e in self.inbox(DEFAULT, m.CANCEL)}
        self.assertEqual(cancels, {s: r.corr_id for s, r in self.before.items()})
        alert.assert_not_called()                               # no human alert
        notify.assert_not_called()
        self.assertEqual(self.ledger.load("just-assigned").state, A.DISPATCHED)
        self.assertEqual(self.ledger.load("really-running").state, A.IN_PROGRESS)

    def test_a_returned_row_is_assigned_again_when_a_slot_is_free(self):
        ts = [self.task(s) for s in self.STUCK]
        self.tick(ts, usage_hold="weekly 95% >= hard cap 95%")
        r, ex = self.runner()
        # The cancelled envelopes are consumed without a run and without a reply.
        r.poll_parallel(4)
        self.wait(lambda: ex.started == ["just-assigned"])
        self.wait(lambda: len(r._inflight) == 1)
        self.assertEqual({e.task_id for e in self.mb.receive(m.ORCHESTRATOR)},
                         {"just-assigned"})
        r._announce(4)          # 4 slots; the ledger has 2 in flight on this worker
        res = self.tick(ts)
        self.assertEqual(len(res["dispatched"]), 2)             # the two free slots
        for a in res["dispatched"]:
            self.assertEqual(a.agent_id, DEFAULT)
            self.assertEqual(a.refail_count, self.STUCK[a.task_id][1])   # still no refail
            self.assertNotEqual(a.corr_id, self.before[a.task_id].corr_id)
        left = set(self.STUCK) - set(self.slugs(res["dispatched"]))
        self.assertEqual(self.wait_reason(ts, left.pop())["wait"], "no_slot")

    def test_a_runner_does_not_start_an_assignment_past_its_ttl(self):
        # Deploy order: the runner restarts on the new code BEFORE the first
        # orchestrator tick has sent any CANCEL. The stale envelopes must not run.
        r, ex = self.runner()
        r.poll_parallel(4)
        self.wait(lambda: ex.started == ["just-assigned"])
        ex.release.set()
        self.wait(lambda: not r._inflight)
        self.assertEqual(ex.started, ["just-assigned"])
        replied = {e.task_id for e in self.mb.receive(m.ORCHESTRATOR)}
        self.assertEqual(replied, {"just-assigned"})            # no reply for a skipped one
        self.assertEqual(self.inbox(DEFAULT), [])               # all consumed


class RunnerSlotsTest(_Base):
    def _beats(self) -> list[dict]:
        return [e.payload for e in self.mb.receive(m.ORCHESTRATOR)
                if e.type == m.HEARTBEAT]

    def test_heartbeat_carries_slots_and_repeats_only_on_change(self):
        r, ex = self.runner()
        self.assertTrue(r._announce(3))
        self.assertFalse(r._announce(3))                        # nothing changed
        self.mb.send(m.Envelope(to=DEFAULT, type=m.TASK_ASSIGN, task_id="alpha",
                                payload=self.task("alpha").payload))
        r.poll_parallel(3)
        self.wait(lambda: ex.started == ["alpha"])
        self.assertTrue(r._announce(3))
        beats = self._beats()
        self.assertEqual([b["slots"] for b in beats],
                         [{"max": 3, "used": 0}, {"max": 3, "used": 1}])
        self.assertEqual([b["status"] for b in beats], ["idle", "busy"])
        self.assertTrue(r._announce(3, interval=0))             # the periodic repeat

    def test_memory_pressure_closes_the_extra_slots(self):
        r, ex = self.runner()
        self.mb.send(m.Envelope(to=DEFAULT, type=m.TASK_ASSIGN, task_id="alpha",
                                payload=self.task("alpha").payload))
        r.poll_parallel(3)
        self.wait(lambda: ex.started == ["alpha"])
        with mock.patch.object(runner_mod, "_mem_available_mb", return_value=500.0):
            r._announce(3)
        self.assertEqual(self._beats()[-1]["slots"], {"max": 1, "used": 1})

    def test_run_forever_starts_assigned_work_without_judging_usage_again(self):
        # The guard says blocked and the paced capacity is 0. The orchestrator
        # judged usage when it assigned; the runner starts what it was given.
        r, ex = self.runner()
        blocked = {"allowed": False, "reason": "weekly 95% >= hard cap 95%"}
        cap = {"total": 0, "per_account": {"dev": 0}, "slack": {}, "order": [],
               "active": "dev"}
        self.mb.send(m.Envelope(to=DEFAULT, type=m.TASK_ASSIGN, task_id="alpha",
                                payload=self.task("alpha").payload))
        with mock.patch("todo_system.usage_guard.check", return_value=blocked), \
                mock.patch.object(r, "_account_slots", return_value=cap), \
                mock.patch.object(r, "_write_slots_snapshot"):
            t = threading.Thread(target=r.run_forever, kwargs={"interval": 0.01},
                                 daemon=True)
            t.start()
            self.wait(lambda: ex.started == ["alpha"])
            self.wait(lambda: {"max": 0, "used": 1} in
                      [b.get("slots") for b in self._beats()])
            r._drain_requested = True
            ex.release.set()
            t.join(10)
        self.assertFalse(t.is_alive())

    def test_a_draining_runner_announces_no_slot(self):
        # Code changed: the runner takes nothing new until it restarts.
        r, _ex = self.runner()
        r._announce(3)
        r._drain_requested = True
        r.run_forever(interval=0.01)                            # idle: returns at once
        self.assertEqual(self._beats()[-1]["slots"], {"max": 0, "used": 0})


if __name__ == "__main__":
    unittest.main()
