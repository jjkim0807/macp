"""A long_job keeps being started and checked through a usage hold.

The usage hold paces Claude. Until a long_job task's job is over, its dispatch
only starts the detached job (jobs.poll `absent`) or polls it (`running`):
`StagedExecutor._long_job_gate` returns before any account, agent or stage. So
the usage hold does not apply to that dispatch (`hold_wait`), and it needs no
usage-paced slot (`_slot_gate`). Once the job is over (`done` / `failed`) the
task waits for the hold like any other. PAUSED, the daily run budget and
`bypass_guard` are unchanged.

`LongJobThroughHoldPathTest` drives the real `Orchestrator.tick()`, the real
`Runner` poll and the real `StagedExecutor` over one file mailbox, and a real
detached job. Only the sweeps that read `gh` or todo.db, and the usage verdict
itself, are stubbed.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import jobs, stages, tasks
from todo_system.orchestrator import message as m
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator.assignment import Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import (
    LONG_JOB_PHASE_STATES, WAIT_BUDGET, WAIT_PAUSED, WAIT_USAGE_HOLD,
    Orchestrator, Task, hold_wait, long_job_phase,
)
from todo_system.orchestrator.registry import Agent, Registry
from todo_system.orchestrator.runner import Runner, StagedExecutor

DEFAULT = "worker-default"
REMOTE = "desktop-runner"
HOLD = "weekly 95% >= hard cap 95%"
JOB = {"long_job": "python3 collect.py"}

# Sweeps that read machine state outside the tmp vault (same list as
# test_free_slot_dispatch). collect, reap_stale, reroute_remote and the
# dispatch pass stay real: they are the subject.
_STUBBED = ("reconcile_merged", "reconcile_closed", "reconcile_queue",
            "reconcile_parents", "reconcile_resumes", "reconcile_rescope",
            "reconcile_split_parents", "reconcile_human_ready",
            "_advote_bridge_tick", "_beta_sweep_tick", "_server_deploy_watch_tick")


def _ago(seconds: float) -> str:
    return (dt.datetime.now().astimezone()
            - dt.timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _wait_for(pred, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return bool(pred())


class LongJobPhaseTest(unittest.TestCase):
    """The one verdict, pure."""

    def test_a_job_still_to_start_or_running_is_the_job_phase(self):
        self.assertEqual(LONG_JOB_PHASE_STATES, {"absent", "running"})
        for state in ("absent", "running"):
            self.assertTrue(long_job_phase(JOB, state), state)

    def test_a_finished_job_or_no_long_job_is_not(self):
        for payload, state in ((JOB, "done"), (JOB, "failed"), (JOB, None),
                               ({}, "absent"), ({}, "running"), (None, "running"),
                               ({"long_job": ""}, "running")):
            self.assertFalse(long_job_phase(payload, state), (payload, state))


class HoldWaitTest(unittest.TestCase):
    def wait(self, payload, job_state, **kw):
        args = {"paused": False, "usage_hold": HOLD, "budget_exceeded": False, **kw}
        return hold_wait(payload, job_state=job_state, **args)

    def test_an_unfinished_job_passes_the_usage_hold(self):
        for state in ("absent", "running"):
            self.assertIsNone(self.wait(JOB, state), state)

    def test_a_finished_job_waits_for_the_usage_hold(self):
        for state in ("done", "failed"):
            self.assertEqual(self.wait(JOB, state), WAIT_USAGE_HOLD, state)

    def test_without_a_long_job_the_usage_hold_stands(self):
        for state in (None, "absent", "running"):
            self.assertEqual(self.wait({}, state), WAIT_USAGE_HOLD, state)

    def test_pause_still_stops_the_job_phase(self):
        for state in ("absent", "running"):
            self.assertEqual(self.wait(JOB, state, paused=True), WAIT_PAUSED, state)

    def test_the_daily_budget_is_not_widened(self):
        self.assertEqual(self.wait(JOB, "running", usage_hold=None, budget_exceeded=True),
                         WAIT_BUDGET)
        # Both cost holds: the usage hold no longer applies, the budget still does.
        self.assertEqual(self.wait(JOB, "running", budget_exceeded=True), WAIT_BUDGET)

    def test_no_hold_and_bypass_guard_are_unchanged(self):
        for state in (None, "absent", "running", "done", "failed"):
            self.assertIsNone(self.wait(JOB, state, usage_hold=None), state)
            self.assertIsNone(self.wait({**JOB, "bypass_guard": True}, state), state)

    def test_a_caller_that_reads_no_job_state_keeps_the_old_verdict(self):
        self.assertEqual(hold_wait(JOB, paused=False, usage_hold=HOLD,
                                   budget_exceeded=False), WAIT_USAGE_HOLD)


class _Harness(unittest.TestCase):
    """A tmp vault: file mailbox, ledger, jobs dir; the default worker and a
    heavy remote; the real Orchestrator and real Runners."""

    def setUp(self):
        self.vault = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        worker = self.vault / "Worker"
        self.uri = f"file://{worker / 'agents'}"
        self.mb = FileMailbox(worker / "agents")
        self.ledger = Ledger(worker / "assignments")
        self.jobs_dir = worker / "jobs"
        self.reg = Registry([
            Agent(id=DEFAULT, kind="ai-local", projects=["*"], concurrency=2,
                  mailbox=self.uri),
            Agent(id=REMOTE, kind="ai-remote", projects=[],
                  capabilities=["heavy", "device"], concurrency=1, mailbox=self.uri),
        ])
        self.orch = Orchestrator(self.mb, self.reg, self.ledger,
                                 notify=lambda *a, **k: None, default_uri=self.uri)
        for p in (
            # Never the live vault's Worker/jobs: a real job is started below.
            mock.patch.object(jobs, "JOBS_DIR", self.jobs_dir),
            mock.patch.object(jobs, "_scope_prefix", return_value=[]),
            # The vault task's cwd, and so the job's cwd.
            mock.patch.object(stages, "_VAULT_DATA_ROOT", self.vault),
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
    def runner(self, agent_id: str = DEFAULT) -> Runner:
        r = Runner(self.mb, self.reg.get(agent_id), StagedExecutor(),
                   log=lambda *_: None, code_time_fn=lambda: None)

        def stop():
            r._drain_requested = True
            _wait_for(lambda: not r._inflight)

        self.addCleanup(stop)
        return r

    @staticmethod
    def job_task(slug: str, cmd: str = "python3 collect.py") -> Task:
        """A vault task (no `repo`) with a long_job — heavy by its long_job."""
        return Task(slug, {"summary": slug, "task_path": f"Todo/items/{slug}.md",
                           "branch": f"worker/{slug}", "worker_mode": "auto",
                           "long_job": cmd})

    @staticmethod
    def task(slug: str) -> Task:
        """A plain task on a repo of its own (no checkout sharing, not heavy)."""
        return Task(slug, {"summary": slug, "task_path": f"Todo/items/{slug}.md",
                           "branch": f"worker/{slug}", "worker_mode": "auto",
                           "repo": f"https://example.invalid/acme/{slug}.git"})

    @contextlib.contextmanager
    def job_states(self, states: dict[str, str]):
        """jobs.poll answering `states` (default `absent`) — no processes."""
        with mock.patch.object(jobs, "poll",
                               side_effect=lambda slug: {"state": states.get(slug, "absent")}):
            yield

    def tick(self, task_list, *, usage_hold=None, paused=False, budget=False):
        with contextlib.ExitStack() as st:
            for name in _STUBBED:
                st.enter_context(mock.patch.object(self.orch, name, return_value=[]))
            for name, value in (("paused", paused), ("budget_exceeded", budget),
                                ("usage_hold", usage_hold)):
                st.enter_context(mock.patch.object(self.orch, name, return_value=value))
            return self.orch.tick(task_list)

    def wait_reason(self, task_list, slug: str, **kw) -> dict:
        return {r["slug"]: r for r in self.orch.queue_snapshot(task_list, **kw)}[slug]

    def inbox(self, agent_id: str) -> list[m.Envelope]:
        return [e for e in self.mb.receive(agent_id) if e.type == m.TASK_ASSIGN]

    @staticmethod
    def slugs(rows) -> list[str]:
        return [a.task_id for a in rows]

    def backdate(self, slug: str, seconds: float) -> None:
        row = self.ledger.load(slug)
        row.updated_ts = _ago(seconds)
        self.ledger.save(row)


class GatesUnderHoldTest(_Harness):
    """hold_wait and _slot_gate as tick() and the queue view run them."""

    def write_job(self, slug: str, rc: int | None) -> None:
        """A job state file. pid 999999999 is above any pid_max, so with no
        `rc` the job reads as killed (`failed`), never as running."""
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        (self.jobs_dir / f"{slug}.json").write_text(json.dumps(
            {"slug": slug, "pid": 999999999, "cmd": "x", "cwd": None,
             "started_ts": _ago(60)}), encoding="utf-8")
        if rc is not None:
            (self.jobs_dir / f"{slug}.rc").write_text(str(rc), encoding="utf-8")

    def test_job_state_reads_this_hosts_job_files(self):
        job = self.job_task("j").payload
        self.assertEqual(self.orch._job_state("j", job), "absent")
        self.write_job("j", 0)
        self.assertEqual(self.orch._job_state("j", job), "done")
        self.write_job("j", 3)
        self.assertEqual(self.orch._job_state("j", job), "failed")
        jobs.clear("j")
        self.write_job("j", None)                 # killed without an exit code
        self.assertEqual(self.orch._job_state("j", job), "failed")
        # Nothing to read: no long_job, or no slug. A broken read holds as before.
        self.assertIsNone(self.orch._job_state("j", {"summary": "j"}))
        self.assertIsNone(self.orch._job_state(None, job))
        with mock.patch.object(jobs, "poll", side_effect=OSError("disk")):
            self.assertIsNone(self.orch._job_state("j", job))

    def test_the_job_phase_gets_one_slot_on_a_worker_paced_to_zero(self):
        r = self.runner()
        r._announce(0)                            # every account at its cap
        self.orch.collect()                       # the announcement is on record
        agent = self.reg.get(DEFAULT)
        for state, free in (("absent", 1), ("running", 1), ("done", 0),
                            ("failed", 0), (None, 0)):
            self.assertEqual(self.orch._slot_gate(agent, JOB, job_state=state).free,
                             free, state)
        self.assertEqual(self.orch._slot_gate(agent, {}, job_state="running").free, 0)

    def test_a_hold_lets_out_only_jobs_still_to_start_or_running(self):
        r = self.runner()
        r._announce(0)                            # paced capacity under the hold
        ts = [self.task("plain"), self.job_task("finished"), self.job_task("to-start")]
        with self.job_states({"finished": "done"}):
            self.assertEqual(self.wait_reason(ts, "to-start", usage_hold=HOLD)["wait"],
                             "next_tick")
            res = self.tick(ts, usage_hold=HOLD)
            self.assertEqual(self.slugs(res["dispatched"]), ["to-start"])
            self.assertEqual(self.ledger.load("to-start").agent_id, DEFAULT)
            for slug in ("plain", "finished"):
                self.assertIsNone(self.ledger.load(slug), slug)
                w = self.wait_reason(ts, slug, usage_hold=HOLD)
                self.assertEqual(w["wait"], "usage_hold", slug)
                self.assertEqual(w["wait_text"], f"사용량 HOLD — {HOLD}", slug)
        self.assertEqual([e.task_id for e in self.inbox(DEFAULT)], ["to-start"])

    def test_a_remote_job_is_checked_on_the_remote_through_a_hold(self):
        # The job runs on the heavy remote, so this host has no file for it. Both
        # workers are paced to zero; without the job-phase slot on the remote the
        # poll would fall back to this host, which would start a second job.
        local, remote = self.runner(DEFAULT), self.runner(REMOTE)
        local._announce(0)
        remote._announce(0)
        job = self.job_task("collect")
        with self.job_states({}):
            res = self.tick([job], usage_hold=HOLD)
        self.assertEqual(self.slugs(res["dispatched"]), ["collect"])
        self.assertEqual(self.ledger.load("collect").agent_id, REMOTE)
        self.assertEqual([e.task_id for e in self.inbox(REMOTE)], ["collect"])
        self.assertEqual(self.inbox(DEFAULT), [])

    def test_pause_and_the_daily_budget_still_hold_the_job_phase(self):
        r = self.runner()
        r._announce(2)
        job = self.job_task("collect")
        with self.job_states({"collect": "running"}):
            res = self.tick([job], usage_hold=HOLD, paused=True)
            self.assertEqual(res["dispatched"], [])
            self.assertEqual(self.wait_reason([job], "collect", usage_hold=HOLD,
                                              paused=True)["wait"], "paused")
            res = self.tick([job], budget=True)
            self.assertEqual(res["dispatched"], [])
            self.assertEqual(self.wait_reason([job], "collect",
                                              budget_exceeded=True)["wait"], "budget")
        self.assertIsNone(self.ledger.load("collect"))
        self.assertEqual(self.inbox(DEFAULT), [])


SLUG = "collect-candles"
# Waits for a `release` file in its cwd (the vault), bounded so a failed test
# can never leave it running for long, then writes its output.
JOB_CMD = ("i=0; while [ ! -f release ] && [ $i -lt 600 ]; do sleep 0.05; "
           "i=$((i+1)); done; printf collected > collected.csv")


class LongJobThroughHoldPathTest(_Harness):
    def finish_job(self) -> None:
        (self.vault / "release").write_text("", encoding="utf-8")
        _wait_for(lambda: jobs.poll(SLUG)["state"] != "running")

    def run_assigned(self, r: Runner) -> None:
        """The runner takes what it was given, and the run ends."""
        self.assertEqual(r.poll_parallel(2), 1)
        self.assertTrue(_wait_for(lambda: not r._inflight), "the run did not end")

    def test_the_job_starts_and_is_checked_through_a_usage_hold(self):
        r = self.runner()
        r._announce(0)                            # the hold paces this worker to zero
        self.addCleanup(self.finish_job)
        job, plain = self.job_task(SLUG, JOB_CMD), self.task("plain")
        both = [job, plain]

        # 1. Under the hold the job's start is assigned; plain work is not.
        res = self.tick(both, usage_hold=HOLD)
        self.assertEqual(self.slugs(res["dispatched"]), [SLUG])
        self.assertIsNone(self.ledger.load("plain"))

        # 2. The runner starts what it was given: the real gate starts the job.
        self.run_assigned(r)
        self.assertEqual(jobs.poll(SLUG)["state"], "running")
        pid = jobs.read(SLUG)["pid"]
        self.assertGreater(pid, 0)
        self.assertFalse((self.vault / "collected.csv").exists())

        # 3. The deferral comes back. Its in-place retry — a poll — still goes out
        #    under the hold, and polls the same job instead of starting another.
        self.tick(both, usage_hold=HOLD)
        row = self.ledger.load(SLUG)
        self.assertEqual((row.state, row.attempts), (A.DISPATCHED, 2))
        self.run_assigned(r)
        self.assertEqual(jobs.read(SLUG)["pid"], pid)
        self.tick(both, usage_hold=HOLD)
        row = self.ledger.load(SLUG)
        self.assertEqual((row.state, row.job_running, row.refail_count), (A.FAILED, True, 0))
        # It waits on its poll interval, not on the hold.
        self.assertEqual(self.wait_reason(both, SLUG, usage_hold=HOLD)["wait"], "cooldown")

        # 4. After the poll interval the next check goes out, hold or not.
        self.backdate(SLUG, 400)
        res = self.tick(both, usage_hold=HOLD)
        self.assertEqual(self.slugs(res["dispatched"]), [SLUG])
        self.run_assigned(r)
        self.assertEqual(jobs.read(SLUG)["pid"], pid)

        # 5. The job ends. From here the task waits for the hold like any other:
        #    no in-place retry, no dispatch, and the queue view says why.
        (self.vault / "release").write_text("", encoding="utf-8")
        self.assertTrue(_wait_for(lambda: jobs.poll(SLUG)["state"] == "done"))
        self.assertEqual((self.vault / "collected.csv").read_text(encoding="utf-8"),
                         "collected")
        res = self.tick(both, usage_hold=HOLD)
        self.assertEqual(res["dispatched"], [])
        row = self.ledger.load(SLUG)
        self.assertEqual((row.state, row.history[-1]["type"]), (A.FAILED, "retry-held"))
        self.backdate(SLUG, 400)                  # past the poll interval too
        res = self.tick(both, usage_hold=HOLD)
        self.assertEqual(res["dispatched"], [])
        self.assertEqual(self.wait_reason(both, SLUG, usage_hold=HOLD)["wait"],
                         "usage_hold")
        self.assertEqual(self.inbox(DEFAULT), [])

        # 6. The hold lifts and the worker's slots come back: the task goes out,
        #    and the finished job is still there for its run to consume.
        r._announce(2)
        res = self.tick(both)
        self.assertIn(SLUG, self.slugs(res["dispatched"]))
        self.assertEqual([e.task_id for e in self.inbox(DEFAULT) if e.task_id == SLUG],
                         [SLUG])
        self.assertEqual(jobs.poll(SLUG)["state"], "done")


if __name__ == "__main__":
    unittest.main()
