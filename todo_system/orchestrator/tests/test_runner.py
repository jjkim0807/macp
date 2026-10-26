"""Tests for the runner loop + executors (P2). No Claude Code is spawned."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system import worker as _worker
from todo_system.orchestrator import claims
from todo_system.orchestrator import message as m
from todo_system.orchestrator import repos, stages
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator.assignment import (
    DISPATCHED, DONE, FAILED, Assignment, Ledger,
)
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.registry import Agent
from argparse import Namespace

from todo_system.orchestrator.runner import (
    ClaudeCodeExecutor,
    ExecOutcome,
    Runner,
    ShadowExecutor,
    StagedExecutor,
    make_executor,
    resolve_agent,
)


def _agent(**kw) -> Agent:
    base = dict(id="armchair-commander", kind="ai-local",
                capabilities=["godot", "git"], repo="~/Worker/armchair-commander")
    base.update(kw)
    return Agent(**base)


class RunnerShadowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.agent = _agent()
        self.runner = Runner(self.mb, self.agent, ShadowExecutor(), log=lambda *_: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_assign_yields_accept_progress_result(self):
        assign = m.Envelope(to=self.agent.id, type=m.TASK_ASSIGN, task_id="core-loop",
                            payload={"summary": "명령→AI유닛→피드백 1턴",
                                     "task_path": "Todo/items/core-loop.md"})
        self.mb.send(assign)

        self.assertEqual(self.runner.poll_once(), 1)
        replies = self.mb.receive(m.ORCHESTRATOR)
        self.assertEqual([r.type for r in replies], [m.ACCEPT, m.PROGRESS, m.RESULT])
        self.assertTrue(all(r.task_id == "core-loop" for r in replies))
        self.assertTrue(all(r.frm == self.agent.id for r in replies))
        self.assertTrue(all(r.corr_id == assign.corr_id for r in replies))
        self.assertEqual(replies[-1].payload["mode"], "shadow")
        # consumed
        self.assertEqual(self.mb.receive(self.agent.id), [])

    def test_reject_on_missing_capability(self):
        self.mb.send(m.Envelope(to=self.agent.id, type=m.TASK_ASSIGN, task_id="t",
                                payload={"requires": ["windows-only"],
                                         "task_path": "Todo/items/t.md"}))
        self.runner.poll_once()
        [rep] = self.mb.receive(m.ORCHESTRATOR)
        self.assertEqual(rep.type, m.REJECT)
        self.assertIn("windows-only", rep.payload["reason"])

    def test_ping_replies_heartbeat(self):
        self.mb.send(m.Envelope(to=self.agent.id, type=m.PING))
        self.runner.poll_once()
        [hb] = self.mb.receive(m.ORCHESTRATOR)
        self.assertEqual(hb.type, m.HEARTBEAT)
        self.assertEqual(hb.payload["mode"], "shadow")

    def test_bad_message_does_not_break_loop(self):
        # unknown-but-valid dispatch type is simply consumed without a crash
        self.mb.send(m.Envelope(to=self.agent.id, type=m.WAKE))
        self.assertEqual(self.runner.poll_once(), 1)
        self.assertEqual(self.mb.receive(self.agent.id), [])


class _CountingExecutor:
    """Records every execution. `**_` absorbs optional executor kwargs so a new
    one (like on_run_start) can never make this fake the thing that fails."""

    mode = "fake"

    def __init__(self, on_execute=None):
        self.calls: list[str] = []
        self._on_execute = on_execute

    def execute(self, msg, agent, progress, **_):
        self.calls.append(msg.task_id)
        if self._on_execute is not None:
            self._on_execute(msg, **_)
        return ExecOutcome(True, "fake ok", {})


class _KeepingMailbox(FileMailbox):
    """FileMailbox that keeps inbox files after ack — GitMailbox's behaviour (the
    files are git-synced, so unlinking them would only churn the tree). That is
    what makes a corrupt cursor redeliver *everything* in production."""

    def _after_ack(self, agent_id, acked_ids):
        return set()


class RunnerClaimTest(unittest.TestCase):
    """The crash loop: `poll_once` used to ack only after `handle()` returned, so
    a SIGKILL / reboot / daemon restart left the envelope un-acked and the next
    idle poll re-ran all five stages — on work that may already be merged."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.agent = _agent()
        self.ex = _CountingExecutor()
        self.runner = Runner(self.mb, self.agent, self.ex, log=lambda *_: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assign(self, **payload) -> m.Envelope:
        msg = m.Envelope(to=self.agent.id, type=m.TASK_ASSIGN, task_id="core-loop",
                         payload=payload or {"task_path": "Todo/items/core-loop.md"})
        self.mb.send(msg)
        return msg

    @staticmethod
    def _dead_pid():
        """Force the liveness probe to report death, deterministically — no real
        pid is ever signalled."""
        return mock.patch.object(claims, "_alive_default", lambda pid: False)

    # -- direction 1: a claimed envelope must NOT be re-run ----------------
    def test_crashed_run_is_classified_not_re_executed(self):
        msg = self._assign()
        # exactly the crash state: execution started (claim on disk), the process
        # died before it could ack, so the envelope is still in the inbox.
        self.runner.claims.open(msg, pid=4242)

        with self._dead_pid():
            self.assertEqual(self.runner.poll_once(), 1)

        self.assertEqual(self.ex.calls, [])           # the pipeline did NOT re-run
        self.assertEqual(self.mb.receive(self.agent.id), [])   # and it is consumed
        replies = self.mb.receive(m.ORCHESTRATOR)
        self.assertEqual([r.type for r in replies], [m.NEEDS_INPUT])
        self.assertEqual(replies[0].payload["verdict"], claims.ORPHAN)
        self.assertTrue(replies[0].payload["redelivered"])
        self.assertEqual(replies[0].payload["claim"]["task_id"], "core-loop")
        self.assertIn("4242", replies[0].payload["evidence"])
        # never FAIL: orchestrator._retry() would re-dispatch the task, which is
        # the very re-run this whole mechanism exists to prevent.
        self.assertNotIn(m.FAIL, [r.type for r in replies])

    def test_live_run_is_left_alone_and_recorded(self):
        msg = self._assign()
        # our own pid is unambiguously alive — a second runner polling the same
        # inbox must not touch a pipeline that is still executing.
        self.runner.claims.open(msg, pid=os.getpid())

        self.assertEqual(self.runner.poll_once(), 1)

        self.assertEqual(self.ex.calls, [])
        self.assertEqual(self.mb.receive(m.ORCHESTRATOR), [])   # no ledger churn
        hist = self.runner.claims.read(msg.msg_id)["redeliveries"]
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["verdict"], claims.LIVE)

    def test_corrupt_cursor_leaves_the_claim_as_the_only_defence(self):
        # `_load_seen` swallows JSONDecodeError and returns an empty set, so a
        # truncated cursor redelivers every message the inbox still holds.
        mb = _KeepingMailbox(self.tmp / "git-agents")
        runner = Runner(mb, self.agent, self.ex, log=lambda *_: None)
        msg = m.Envelope(to=self.agent.id, type=m.TASK_ASSIGN, task_id="core-loop")
        mb.send(msg)
        runner.claims.open(msg, pid=4242)
        mb.ack(self.agent.id, msg)        # acked, then killed before close()
        mb._cursor_path(self.agent.id).write_text("{", encoding="utf-8")

        self.assertEqual(mb.receive(self.agent.id), [msg])   # gate 1 is gone
        with self._dead_pid():
            runner.poll_once()
        self.assertEqual(self.ex.calls, [])                  # gate 2 held

    def test_bypass_path_is_claimed_too(self):
        # a bypass_guard run that dies mid-pipeline is the same accident, so the
        # classification sits AFTER the only_bypass filter, not before it.
        msg = self._assign(bypass_guard=True)
        self.runner.claims.open(msg, pid=4242)

        with self._dead_pid():
            self.assertEqual(self.runner.poll_once(only_bypass=True), 1)

        self.assertEqual(self.ex.calls, [])
        self.assertEqual([r.type for r in self.mb.receive(m.ORCHESTRATOR)],
                         [m.NEEDS_INPUT])

    # -- direction 2: an UNCLAIMED envelope keeps at-least-once ------------
    def test_unstarted_message_is_never_acked_or_claimed(self):
        """The opposite accident: acking before execution would silently lose a
        task that never ran. Filtered-out messages must stay exactly as they were."""
        msg = self._assign()                       # no bypass_guard, no claim

        self.assertEqual(self.runner.poll_once(only_bypass=True), 0)

        self.assertEqual(self.ex.calls, [])
        self.assertEqual([e.msg_id for e in self.mb.receive(self.agent.id)],
                         [msg.msg_id])             # still queued, still un-acked
        self.assertIsNone(self.runner.claims.read(msg.msg_id))
        # and it still runs normally when the guard clears
        self.assertEqual(self.runner.poll_once(), 1)
        self.assertEqual(self.ex.calls, ["core-loop"])

    # -- the claim is written BEFORE handle(), and cleaned up after --------
    def test_claim_exists_on_disk_while_the_executor_runs(self):
        seen: list[dict | None] = []
        ex = _CountingExecutor(on_execute=lambda msg, **_: seen.append(
            runner.claims.read(msg.msg_id)))
        runner = Runner(self.mb, self.agent, ex, log=lambda *_: None)
        msg = self._assign()

        runner.poll_once()

        # proves the start was durable *outside the process* before any work began
        self.assertEqual(len(seen), 1)
        self.assertIsNotNone(seen[0])
        self.assertEqual(seen[0]["msg_id"], msg.msg_id)
        self.assertEqual(seen[0]["task_path"], "Todo/items/core-loop.md")
        self.assertEqual(seen[0]["pid"], os.getpid())
        self.assertTrue(seen[0]["started_at"])
        # …and a clean finish removes it, so a completed envelope is not mistaken
        # for a crashed one and `_state/` does not grow without bound.
        self.assertIsNone(runner.claims.read(msg.msg_id))

    def test_on_run_start_binds_run_id_to_the_claim(self):
        captured: list[dict | None] = []

        def _exec(msg, on_run_start=None, **_):
            on_run_start("20260818T010203-core-loop", Path("/vault/Worker/runs/x"))
            captured.append(runner.claims.read(msg.msg_id))

        runner = Runner(self.mb, self.agent, _CountingExecutor(on_execute=_exec),
                        log=lambda *_: None)
        self._assign()

        runner.poll_once()

        # the back-reference that replaces slug + task_path + nearest-earlier-
        # timestamp archaeology in the report stage
        self.assertEqual(captured[0]["run_id"], "20260818T010203-core-loop")
        self.assertEqual(captured[0]["run_dir"], "/vault/Worker/runs/x")

    def test_a_claim_that_cannot_be_written_does_not_block_the_work(self):
        with mock.patch.object(claims.ClaimStore, "open",
                               side_effect=OSError("read-only fs")):
            self._assign()
            self.assertEqual(self.runner.poll_once(), 1)
        self.assertEqual(self.ex.calls, ["core-loop"])   # recording is not a gate


class RunnerOrphanAssignTest(unittest.TestCase):
    """A TASK_ASSIGN with no dispatch payload (e.g. `dispatch --payload '{}'`)
    used to run all five stages on nothing. poll_once and poll_parallel both
    reach handle() through _run_claimed, so the guard in _accept_reason covers
    both; the poll_parallel case below pins that."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.agent = _agent()
        self.ex = _CountingExecutor()
        self.logs: list[str] = []
        self.runner = Runner(self.mb, self.agent, self.ex, log=self.logs.append)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _send(self, task_id, payload) -> m.Envelope:
        msg = m.Envelope(to=self.agent.id, type=m.TASK_ASSIGN, task_id=task_id,
                         payload=payload)
        self.mb.send(msg)
        return msg

    def test_orphan_assign_is_rejected_without_running(self):
        self._send("t-dispatch-1", {})

        self.assertEqual(self.runner.poll_once(), 1)

        self.assertEqual(self.ex.calls, [])
        replies = self.mb.receive(m.ORCHESTRATOR)
        self.assertEqual([r.type for r in replies], [m.REJECT])
        self.assertIn("task_path", replies[0].payload["reason"])
        self.assertEqual(self.mb.receive(self.agent.id), [])   # consumed
        # not a silent drop: the reason is in the runner log
        self.assertTrue(any("REJECT" in line and "task_path" in line
                            for line in self.logs), self.logs)

    def test_orphan_assign_is_rejected_on_the_parallel_path_too(self):
        self._send("t-dispatch-1", {})
        with mock.patch.multiple("todo_system.worker",
                                 runner_heartbeat_start=mock.DEFAULT,
                                 runner_heartbeat_clear=mock.DEFAULT), \
                mock.patch.object(runner_mod, "_mem_available_mb", return_value=None):
            self.assertEqual(self.runner.poll_parallel(1), 1)
            import time as _t
            end = _t.time() + 5.0
            while self.runner._inflight and _t.time() < end:
                _t.sleep(0.01)

        self.assertEqual(self.ex.calls, [])
        self.assertEqual([r.type for r in self.mb.receive(m.ORCHESTRATOR)], [m.REJECT])

    def test_normal_dispatch_still_accepts_and_executes(self):
        # the shape tasks.load_dispatchable() builds
        self._send("x", {
            "summary": "s", "outcome": "o", "next_action": "n", "tags": ["todo"],
            "worker_mode": "full", "branch": "worker/x",
            "task_path": "Todo/items/x.md", "auto_merge": True,
        })

        self.assertEqual(self.runner.poll_once(), 1)

        self.assertEqual(self.ex.calls, ["x"])
        self.assertEqual([r.type for r in self.mb.receive(m.ORCHESTRATOR)],
                         [m.ACCEPT, m.RESULT])

    def test_task_path_without_md_is_not_dropped(self):
        # A md-less dispatch (discovery runs) is real: load_dispatchable sends
        # `r["path"] or ""`. The guard must key on the payload, never the disk.
        for task_path in ("", "Todo/items/no-such-task-md.md"):
            with self.subTest(task_path=task_path):
                self.ex.calls.clear()
                self._send("d", {"task_path": task_path})

                self.assertEqual(self.runner.poll_once(), 1)

                self.assertEqual(self.ex.calls, ["d"])
                replies = self.mb.receive(m.ORCHESTRATOR)
                self.assertEqual([r.type for r in replies], [m.ACCEPT, m.RESULT])
                for r in replies:
                    self.mb.ack(m.ORCHESTRATOR, r)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@example.invalid",
                           "-c", "user.name=t", *args],
                          check=True, capture_output=True, text=True)


class RunnerResurrectedAckTest(unittest.TestCase):
    """The ack unlinks the inbox file *after* the run already restored the vault,
    and nothing commits the deletion. A later `git restore`/`checkout -- .` then
    brings the envelope back with no claim and no cursor record — it looked brand
    new and the whole pipeline ran again."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _git(self.tmp, "init", "-q")
        self.mb = FileMailbox(self.tmp / "Worker" / "agents")
        self.agent = _agent()
        self.ex = _CountingExecutor()
        self.runner = Runner(self.mb, self.agent, self.ex, log=lambda *_: None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_git_restored_acked_assign_is_not_rerun(self):
        msg = m.Envelope(to=self.agent.id, type=m.TASK_ASSIGN, task_id="core-loop",
                         payload={"task_path": "Todo/items/no-such-task.md"})
        path = self.mb.send(msg)
        _git(self.tmp, "add", "-A")
        _git(self.tmp, "commit", "-q", "-m", "sync")   # todo-sync committed it

        self.assertEqual(self.runner.poll_once(), 1)
        self.assertEqual(self.ex.calls, ["core-loop"])
        self.assertFalse(path.exists())                 # acked = unlinked, uncommitted

        _git(self.tmp, "checkout", "--", ".")           # a stage agent's restore
        self.assertTrue(path.exists())                  # the restore really resurrected it

        self.runner.poll_once()

        self.assertEqual(self.ex.calls, ["core-loop"])  # NOT run a second time
        replies = self.mb.receive(m.ORCHESTRATOR)
        self.assertEqual([r.type for r in replies], [m.ACCEPT, m.RESULT])
        self.assertNotIn(m.NEEDS_INPUT, [r.type for r in replies])


class RunnerSkipGuardTest(unittest.TestCase):
    """Second line behind the tombstones: right before ACCEPT, a TASK_ASSIGN for a
    task that is already done — or has since become a rescoped parent — is consumed
    without running and without any reply that could rewrite its ledger row."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "Worker" / "agents")
        self.ledger = Ledger(self.tmp / "Worker" / "assignments")
        self.items = self.tmp / "Todo" / "items"
        self.items.mkdir(parents=True)
        self.agent = _agent()
        self.ex = _CountingExecutor()
        self.logs: list[str] = []
        self.runner = Runner(self.mb, self.agent, self.ex, log=self.logs.append)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _md(self, slug: str, fm: str) -> Path:
        p = self.items / f"{slug}.md"
        p.write_text(f"---\n{fm}\n---\n\n# {slug}\n", encoding="utf-8")
        return p

    def _row(self, slug: str, state: str, corr_id: str = "C-old") -> None:
        self.ledger.save(Assignment(task_id=slug, agent_id=self.agent.id,
                                    corr_id=corr_id, state=state))

    def _assign(self, slug: str, task_path: str) -> m.Envelope:
        msg = m.Envelope(to=self.agent.id, type=m.TASK_ASSIGN, task_id=slug,
                         payload={"task_path": task_path})
        self.mb.send(msg)
        return msg

    def _assert_skipped(self, msg: m.Envelope) -> None:
        self.assertEqual(self.runner.poll_once(), 1)
        self.assertEqual(self.ex.calls, [])
        self.assertEqual(self.mb.receive(m.ORCHESTRATOR), [])     # no reply at all
        self.assertEqual(self.mb.receive(self.agent.id), [])      # consumed
        self.assertIsNone(self.runner.claims.read(msg.msg_id))    # claim closed
        self.assertTrue(any("SKIP" in line for line in self.logs), self.logs)

    def _assert_ran(self, slug: str) -> None:
        self.assertEqual(self.runner.poll_once(), 1)
        self.assertEqual(self.ex.calls, [slug])
        self.assertEqual([r.type for r in self.mb.receive(m.ORCHESTRATOR)],
                         [m.ACCEPT, m.RESULT])

    # -- skipped -----------------------------------------------------------
    def test_done_ledger_row_is_skipped(self):
        self._row("t", DONE)
        msg = self._assign("t", "Todo/items/t.md")
        self._assert_skipped(msg)
        self.assertEqual(self.ledger.load("t").state, DONE)        # row untouched

    def test_worker_mode_off_md_is_skipped(self):
        # bare `off` is how the vault writes it, and YAML reads it as False
        p = self._md("t", "status: open\nworker_mode: off")
        self._assert_skipped(self._assign("t", str(p)))           # absolute path

    def test_quoted_worker_mode_off_md_is_skipped(self):
        self._md("t", 'status: open\nworker_mode: "Off"')
        self._assert_skipped(self._assign("t", "Todo/items/t.md"))

    def test_auto_rescoped_md_is_skipped(self):
        self._md("t", "status: open\nworker_mode: auto\nauto_rescoped: true")
        self._assert_skipped(self._assign("t", "Todo/items/t.md"))  # vault-relative

    def test_is_parent_md_is_skipped(self):
        self._md("t", "status: open\nworker_mode: auto\nis_parent: true")
        self._assert_skipped(self._assign("t", "Todo/items/t.md"))

    # -- still runs --------------------------------------------------------
    def test_failed_row_runs(self):
        self._row("t", FAILED)
        self._assign("t", "Todo/items/t.md")
        self._assert_ran("t")

    def test_redispatch_with_new_corr_id_runs(self):
        self._row("t", DISPATCHED, corr_id="C-previous")
        self._md("t", "status: open\nworker_mode: auto")
        self._assign("t", "Todo/items/t.md")
        self._assert_ran("t")

    def test_child_of_rescoped_parent_runs(self):
        # every split child has an `off` parent by construction; the guard must
        # read the dispatched task's own md, never its ancestor
        self._md("bundle", "status: open\nworker_mode: off\nauto_rescoped: true\n"
                           "is_parent: true")
        self._md("bundle-step", 'status: open\nworker_mode: auto\n'
                                'parent: "[[bundle]]"')
        self._assign("bundle-step", "Todo/items/bundle-step.md")
        self._assert_ran("bundle-step")

    def test_corrupt_ledger_row_runs(self):
        self.ledger.root.mkdir(parents=True)
        (self.ledger.root / "t.json").write_text("{", encoding="utf-8")
        self._assign("t", "Todo/items/t.md")
        self._assert_ran("t")

    def test_unreadable_md_fails_open(self):
        self._md("t", "status: open\nworker_mode: off")
        self._assign("t", "Todo/items/t.md")
        with mock.patch.object(runner_mod.repos, "_frontmatter",
                               side_effect=RuntimeError("yaml missing")):
            self._assert_ran("t")
        self.assertTrue(any("skip-guard failed" in line for line in self.logs))


class ResolveAgentTest(unittest.TestCase):
    def test_inline_identity_no_registry(self):
        # a bare remote host builds its agent from flags, no registry/PyYAML
        a = resolve_agent(Namespace(
            agent=None, agent_id="sc1-homeland", kind="ai-remote",
            repo="/home/x/sc1-homeland", projects="sc1-homeland",
            capabilities="python,windows-only", registry=None))
        self.assertEqual(a.id, "sc1-homeland")
        self.assertEqual(a.kind, "ai-remote")
        self.assertEqual(a.projects, ["sc1-homeland"])
        self.assertEqual(a.capabilities, ["python", "windows-only"])
        self.assertEqual(a.repo, "/home/x/sc1-homeland")

    def test_none_when_neither(self):
        self.assertIsNone(resolve_agent(Namespace(agent=None, agent_id=None)))


class ExecutorPureTest(unittest.TestCase):
    def test_branch_default_and_override(self):
        ex = ClaudeCodeExecutor()
        self.assertEqual(
            ex.branch_for(m.Envelope(to="a", type=m.TASK_ASSIGN, task_id="t2")),
            "worker/t2")
        self.assertEqual(
            ex.branch_for(m.Envelope(to="a", type=m.TASK_ASSIGN, task_id="t2",
                                     payload={"branch": "agent/custom"})),
            "agent/custom")

    def test_build_argv(self):
        ex = ClaudeCodeExecutor(max_turns=25)
        argv = ex.build_argv(m.Envelope(to="a", type=m.TASK_ASSIGN, task_id="t2"))
        self.assertEqual(argv[0], "claude")
        self.assertIn("--max-turns", argv)
        self.assertIn("25", argv)
        self.assertIn("--allowed-tools", argv)

    def test_pipeline_right_sizing(self):
        from todo_system.orchestrator import stages
        from todo_system.orchestrator.runner import StagedExecutor
        ex = StagedExecutor()
        # the model tier rides the same payload: the classifier's verdict plus
        # the field that decided it, and never an exception
        self.assertEqual(ex._model_tier_for({"worker_mode": "quick"}), ("light", "worker_mode: quick"))
        self.assertEqual(ex._model_tier_for({"model_tier": "heavy"}), ("heavy", "model_tier: heavy"))
        self.assertIn(ex._model_tier_for({})[0], ("heavy", "standard", "light"))
        self.assertEqual(ex._pipeline_for({}), stages.STAGES_AUTO)
        self.assertEqual(ex._pipeline_for({"worker_mode": "delegate"}), stages.STAGES_DELEGATE)
        self.assertEqual(ex._pipeline_for({"worker_mode": "quick"}), stages.STAGES_QUICK)
        self.assertEqual(ex._pipeline_for({"tags": ["work", "trivial"]}), stages.STAGES_QUICK)

    def test_live_execute_guards_missing_repo(self):
        ex = ClaudeCodeExecutor()
        out = ex.execute(m.Envelope(to="a", type=m.TASK_ASSIGN, task_id="t"),
                         _agent(repo=None), progress=lambda _: None)
        self.assertFalse(out.ok)
        self.assertIn("no repo", out.summary)

    def test_unpreparable_repo_note_carries_the_reason(self):
        """When ensure_worktree can't produce a checkout it appends the reason to
        the opt-in log; _execute must splice that into the RESULT note instead of
        the bare "could not prepare repo <url>" that used to hide the cause."""
        def _fail(*a, **k):
            k["log"].append("clone failed: fatal: repository 'x' does not exist")
            return None

        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="t",
                         payload={"repo": "git@local:x/y.git"})
        with mock.patch.object(repos, "ensure_worktree", side_effect=_fail):
            out = StagedExecutor()._execute(msg, _agent(), progress=lambda _: None, sink={})

        self.assertFalse(out.ok)
        self.assertIn("could not prepare repo git@local:x/y.git", out.summary)
        self.assertIn("clone failed:", out.summary)

    def test_unpreparable_repo_note_falls_back_when_no_reason(self):
        """No reason logged → the note stays the plain form (no dangling ': ')."""
        with mock.patch.object(repos, "ensure_worktree", return_value=None):
            msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="t",
                             payload={"repo": "git@local:x/y.git"})
            out = StagedExecutor()._execute(msg, _agent(), progress=lambda _: None, sink={})

        self.assertFalse(out.ok)
        self.assertEqual(out.summary, "could not prepare repo git@local:x/y.git")

    def test_make_executor(self):
        self.assertIsInstance(make_executor("shadow"), ShadowExecutor)
        self.assertIsInstance(make_executor("live"), ClaudeCodeExecutor)
        with self.assertRaises(ValueError):
            make_executor("bogus")


_ORPHANED = {"branch": "worker/t", "base": "origin/main",
             "tip": "deadbeef1234567890abcdef01234567890abcde", "count": 3,
             "shas": ["aaa1111222233334444555566667777888899990",
                      "bbb1111222233334444555566667777888899991",
                      "deadbeef1234567890abcdef01234567890abcde"]}


def _prepared(rc: int, orphaned: dict | None) -> subprocess.CompletedProcess:
    """What prepare_external_branch returns: the `checkout -B` result carrying the
    extra attribute. rc and the attribute are independent — the rewind (and the
    loss) happens BEFORE the checkout that decides rc."""
    cp = subprocess.CompletedProcess(args=["git", "checkout", "-B"], returncode=rc,
                                     stdout="", stderr="checkout failed" if rc else "")
    cp.orphaned_commits = orphaned
    return cp


class OrphanedCommitEscalationTest(unittest.TestCase):
    """The rewind in prepare_external_branch reports the commits it drops, and
    until now nothing read that report: `cp.orphaned_commits` had zero consumers
    in production code, so the only trace of a prior attempt's lost work was a
    progress line buried in run.log. These pin the runner half — the fact has to
    leave the process on ExecOutcome.payload (which _send splats into the
    RESULT/FAIL envelope) and land in the run's stages.json.

    Everything that touches the live vault is mocked: restore_main would
    `git checkout main` in the real repo, install_push_guard would write to its
    .git, and RUNS_DIR would litter Worker/runs/."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "clone"
        self.repo.mkdir()
        self.runs = self.tmp / "runs"
        self.runs.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @contextlib.contextmanager
    def _sandbox(self, prepared):
        with mock.patch.object(repos, "ensure_worktree", return_value=self.repo), \
             mock.patch.object(stages, "restore_main"), \
             mock.patch.object(stages, "install_push_guard"), \
             mock.patch.object(stages, "default_branch", return_value="main"), \
             mock.patch.object(stages, "prepare_external_branch", return_value=prepared), \
             mock.patch.object(stages, "remove_worktree"), \
             mock.patch.object(stages, "RUNS_DIR", self.runs), \
             mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")), \
             mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None), \
             mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}):
            yield

    def _run(self, prepared, **payload):
        # auto_merge:False keeps `merge_landed` (a live `gh` call) out of the path.
        base = {"repo": "git@github.com:x/y.git", "auto_merge": False}
        base.update(payload)
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="t", payload=base)
        with self._sandbox(prepared), \
                mock.patch.object(StagedExecutor, "_pipeline_for", return_value=()):
            return StagedExecutor().execute(msg, _agent(), progress=lambda _: None)

    def test_failed_branch_prep_carries_the_loss_out_of_the_process(self):
        """The worst case: the rewind already dropped the commits and then the
        checkout failed, so the run aborts at the earliest return of all."""
        out = self._run(_prepared(1, dict(_ORPHANED)))

        self.assertFalse(out.ok)
        got = out.payload.get("orphaned_commits")
        self.assertIsNotNone(got, "3 commits were dropped and the payload said nothing")
        self.assertEqual(got["count"], 3)
        self.assertEqual(got["tip"], _ORPHANED["tip"])
        self.assertEqual(got["shas"], _ORPHANED["shas"])

    def test_a_clean_rewind_stays_silent(self):
        """Nothing was lost: no key, so no notification downstream can fire."""
        out = self._run(_prepared(1, None))
        self.assertFalse(out.ok)
        self.assertNotIn("orphaned_commits", out.payload)

    def test_the_loss_is_also_written_to_the_runs_structured_record(self):
        """Detection happens before run_dir exists, so the record has to be
        written after mkdir — and survive the stages that follow."""
        out = self._run(_prepared(0, dict(_ORPHANED)))

        run_dirs = list(self.runs.iterdir())
        self.assertEqual(len(run_dirs), 1, f"expected exactly one run dir, got {run_dirs}")
        rec = json.loads((run_dirs[0] / stages.STAGE_PROGRESS).read_text(encoding="utf-8"))
        self.assertEqual(rec["orphaned_commits"]["shas"], _ORPHANED["shas"])
        self.assertEqual(out.payload["orphaned_commits"]["count"], 3)

    def test_every_exit_path_carries_it_not_just_the_early_one(self):
        """execute() has eight returns. Rather than patch each, the value rides a
        per-call sink the wrapper merges on the way out — so a run that gets all
        the way to 'pipeline complete' still reports what it destroyed on entry."""
        def fake(self_, msg, agent, progress, on_run_start=None, sink=None):
            sink["orphaned_commits"] = dict(_ORPHANED)
            return ExecOutcome(True, "staged pipeline complete (5 stages)",
                               {"branch": "worker/t"})

        with mock.patch.object(StagedExecutor, "_execute", fake):
            out = StagedExecutor().execute(
                m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="t"),
                _agent(), progress=lambda _: None)

        self.assertTrue(out.ok)
        self.assertEqual(out.payload["orphaned_commits"]["count"], 3)
        self.assertEqual(out.payload["branch"], "worker/t")   # existing keys intact

    def test_the_sink_is_per_call_and_never_leaks_between_runs(self):
        """A shared dict would make one task's loss haunt the next task's run —
        an alarm about commits that are not missing is worse than no alarm."""
        seen: list = []

        def fake(self_, msg, agent, progress, on_run_start=None, sink=None):
            if msg.task_id == "loud":
                sink["orphaned_commits"] = dict(_ORPHANED)
            seen.append(sink)
            return ExecOutcome(True, "ok", {})

        ex = StagedExecutor()
        with mock.patch.object(StagedExecutor, "_execute", fake):
            loud = ex.execute(m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="loud"),
                              _agent(), progress=lambda _: None)
            quiet = ex.execute(m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="quiet"),
                               _agent(), progress=lambda _: None)

        self.assertIn("orphaned_commits", loud.payload)
        self.assertNotIn("orphaned_commits", quiet.payload)
        self.assertIsNot(seen[0], seen[1])

    def test_the_executor_protocol_signature_is_unchanged(self):
        """The wrapper must stay drop-in for Runner, which calls execute() with
        exactly these arguments."""
        import inspect
        params = list(inspect.signature(StagedExecutor.execute).parameters)
        self.assertEqual(params, ["self", "msg", "agent", "progress", "on_run_start"])


class TimeoutCaptureTest(unittest.TestCase):
    """A stage that hits its subprocess timeout must checkpoint-push the external
    branch and report timed_out=True — not let TimeoutExpired escape the pipeline
    (which walked HEAD to main and let the retry's branch prep rewind the work,
    the mulkko-32 loss on 2026-09-13)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runs = self.tmp / "runs"
        self.runs.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_timeout(self, repo_url="git@github.com:x/y.git", pipeline=("implement",)):
        pushed: dict = {}

        def fake_checkpoint(repo_dir, branch, progress=None):
            pushed["called"] = (str(repo_dir), branch)
            return {"pushed": True, "tip": "abc1234def", "commits": 2,
                    "note": "checkpoint pushed 2 commit(s) to origin/worker/t"}

        def fake_run(argv, **kw):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout"),
                                            output="partial stage output before the kill")

        vp = subprocess.CompletedProcess([], 0, "", "")
        vp.problem = None
        vp.orphaned_commits = None
        payload = {"repo": repo_url, "auto_merge": False, "model_tier": "heavy"}
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="t", payload=payload)
        # The stage spawn moved from `subprocess.run` to `stages.run_stage_subprocess`
        # (the group-reaping Popen helper), so `fake_run` is bound to BOTH seams: the
        # new one is what the stage loop calls, and the old one stays as the sandbox
        # that keeps any other subprocess on this path off the real machine.
        # The vault path takes the live `Worker/checkout.lock` lease and reads the
        # host disk; like `_live_sibling_hold`, both are patched off the host here.
        # An ExitStack, not one `with`: 18 managers sat two short of CPython's
        # static block-nesting limit (a 21st is a SyntaxError at import).
        with contextlib.ExitStack() as stack:
            for p in (
                mock.patch.object(repos, "ensure_worktree", return_value=self.tmp / "clone"),
                mock.patch.object(stages, "restore_main"),
                mock.patch.object(stages, "install_push_guard"),
                mock.patch.object(stages, "default_branch", return_value="main"),
                mock.patch.object(stages, "prepare_external_branch",
                                  return_value=subprocess.CompletedProcess([], 0, "", "")),
                mock.patch.object(stages, "prepare_vault_branch", return_value=vp),
                mock.patch.object(stages, "remote_branch_exists", return_value=True),
                mock.patch.object(stages, "RUNS_DIR", self.runs),
                mock.patch.object(stages, "build_stage_argv", return_value=["true"]),
                mock.patch.object(stages, "checkpoint_push", side_effect=fake_checkpoint),
                mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")),
                mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
                mock.patch.object(StagedExecutor, "_acquire_checkout_lease", return_value=None),
                mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
                mock.patch.object(StagedExecutor, "_pipeline_for", return_value=pipeline),
                mock.patch("todo_system.worker.runner_heartbeat_start"),
                mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run),
                mock.patch("subprocess.run", side_effect=fake_run),
            ):
                stack.enter_context(p)
            out = StagedExecutor().execute(msg, _agent(), progress=lambda _: None)
        return out, pushed

    def test_external_timeout_checkpoints_and_reports_timed_out(self):
        out, pushed = self._run_timeout()
        self.assertFalse(out.ok)
        self.assertTrue(out.payload.get("timed_out"))
        self.assertEqual(out.payload.get("timeout_stage"), "implement")
        self.assertEqual(out.payload.get("checkpoint_sha"), "abc1234def")
        self.assertEqual(pushed.get("called", (None, None))[1], "worker/t")
        # the timeout is spelled out in run.log in the stage-accumulator idiom
        run_dirs = list(self.runs.iterdir())
        self.assertEqual(len(run_dirs), 1, run_dirs)
        log = (run_dirs[0] / "run.log").read_text(encoding="utf-8")
        self.assertIn("(timeout after", log)
        self.assertIn("partial stage output", log)          # the partial output is kept
        # stages.json records the marker the retry's resume note reads
        rec = json.loads((run_dirs[0] / stages.STAGE_PROGRESS).read_text(encoding="utf-8"))
        self.assertEqual(rec["timed_out_stage"], "implement")
        self.assertEqual(rec["checkpoint_sha"], "abc1234def")

    def test_vault_timeout_reports_timed_out_without_a_checkpoint(self):
        # A vault task (no repo_url) has no external branch to push, but must still
        # report timed_out=True so the streak guard and the resume note both fire.
        out, pushed = self._run_timeout(repo_url=None)
        self.assertFalse(out.ok)
        self.assertTrue(out.payload.get("timed_out"))
        self.assertNotIn("called", pushed)                  # checkpoint_push not invoked
        self.assertIsNone(out.payload.get("checkpoint_sha"))

    def test_report_stage_timeout_skips_checkpoint(self):
        # RED control for defect B (timeout path): `report` runs only AFTER a
        # squash-merge that deleted origin/<branch>, so it carries no unpushed work.
        # Left to checkpoint, checkpoint_push would base off origin/<default>, count
        # the already-merged commits as "ahead", and re-push the merged branch (a
        # duplicate-PR shape). The deferral must still be reported, but WITHOUT push.
        out, pushed = self._run_timeout(pipeline=("report",))
        self.assertFalse(out.ok)
        self.assertTrue(out.payload.get("timed_out"))
        self.assertEqual(out.payload.get("timeout_stage"), "report")
        self.assertNotIn("called", pushed)                  # checkpoint_push not invoked
        self.assertIsNone(out.payload.get("checkpoint_sha"))


class BrowserOwnerEnvRunnerTest(unittest.TestCase):
    """The stage loop hands browser stages MACP_BROWSER_OWNER=<run_id> (the
    logged-in Chrome lease owner, CLAUDE.md §12) and strips it from the others.
    Each run stops at its first stage through the timeout path, which is the
    cheapest way out of the loop with every host seam patched off."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runs = self.tmp / "runs"
        self.runs.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stage_env(self, stage: str) -> tuple[dict, str]:
        seen: dict = {}

        def fake_run(argv, **kw):
            seen["env"] = kw.get("env")
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout"))

        vp = subprocess.CompletedProcess([], 0, "", "")
        vp.problem = None
        vp.orphaned_commits = None
        payload = {"auto_merge": False, "model_tier": "heavy"}
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="t", payload=payload)
        with contextlib.ExitStack() as stack:
            for p in (
                mock.patch.dict(os.environ, {stages.BROWSER_OWNER_ENV: "stale-owner"}),
                mock.patch.object(stages, "restore_main"),
                mock.patch.object(stages, "default_branch", return_value="main"),
                mock.patch.object(stages, "prepare_vault_branch", return_value=vp),
                mock.patch.object(stages, "RUNS_DIR", self.runs),
                mock.patch.object(stages, "build_stage_argv", return_value=["true"]),
                mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")),
                mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
                mock.patch.object(StagedExecutor, "_acquire_checkout_lease", return_value=None),
                mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
                mock.patch.object(StagedExecutor, "_pipeline_for", return_value=(stage,)),
                mock.patch("todo_system.worker.runner_heartbeat_start"),
                mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run),
                mock.patch("subprocess.run", side_effect=fake_run),
            ):
                stack.enter_context(p)
            out = StagedExecutor().execute(msg, _agent(), progress=lambda _: None)
        self.assertTrue(out.payload.get("timed_out"), out.payload)
        run_dirs = list(self.runs.iterdir())
        self.assertEqual(len(run_dirs), 1, run_dirs)
        return seen["env"], run_dirs[0].name

    def test_browser_stages_get_the_run_id(self):
        for stage in ("implement", "verify"):
            with self.subTest(stage=stage):
                env, run_id = self._stage_env(stage)
                self.assertEqual(env[stages.BROWSER_OWNER_ENV], run_id)
                self.assertEqual(env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "1")
                shutil.rmtree(self.runs)
                self.runs.mkdir()

    def test_other_stages_get_no_owner(self):
        for stage in ("research", "plan"):
            with self.subTest(stage=stage):
                env, _ = self._stage_env(stage)
                self.assertNotIn(stages.BROWSER_OWNER_ENV, env)
                shutil.rmtree(self.runs)
                self.runs.mkdir()


class RebaseConflictParkRunnerTest(unittest.TestCase):
    """A later concurrent same-repo PR whose report stage aborted a conflicting
    rebase (stamping `rebase_conflict: true`) must park on a human with the FIXED
    note '동시 실행 충돌 — 겹침 오판' — the reason string owned by the runner, not
    the agent. Drives the real landed-gate with auto_merge=True + a report-only
    pipeline, `merge_landed` forced not-True (so the gate fires without a live
    `gh` call), and the report.md the fake report stage writes carrying (or not)
    the marker. followups is stubbed so the report-text follow-up scan is hermetic."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        self.clone = self.tmp / "clone"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_dir(self) -> Path:
        dirs = list(self.runs.iterdir())
        self.assertEqual(len(dirs), 1, dirs)
        return dirs[0]

    def _run(self, report_body: str):
        def fake_run(argv, **kw):
            (self._run_dir() / stages.STAGE_ARTIFACT["report"]).write_text(
                report_body, encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")

        vp = subprocess.CompletedProcess([], 0, "", "")
        vp.problem = None
        vp.orphaned_commits = None
        payload = {"repo": "git@github.com:x/y.git", "auto_merge": True,
                   "model_tier": "heavy"}
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="t", payload=payload)
        with contextlib.ExitStack() as stack:
            for p in (
                mock.patch.object(repos, "ensure_worktree", return_value=self.clone),
                mock.patch.object(stages, "restore_main"),
                mock.patch.object(stages, "install_push_guard"),
                mock.patch.object(stages, "default_branch", return_value="main"),
                mock.patch.object(stages, "prepare_external_branch",
                                  return_value=subprocess.CompletedProcess([], 0, "", "")),
                mock.patch.object(stages, "prepare_vault_branch", return_value=vp),
                mock.patch.object(stages, "remote_branch_exists", return_value=True),
                mock.patch.object(stages, "RUNS_DIR", self.runs),
                mock.patch.object(stages, "build_stage_argv", return_value=["true"]),
                mock.patch.object(stages, "checkpoint_push",
                                  return_value={"pushed": False, "note": "", "tip": None}),
                mock.patch.object(stages, "cleanup_build_daemons", return_value={}),
                mock.patch.object(stages, "remove_worktree"),
                # force the landed gate to fire without a live `gh pr view`
                mock.patch.object(stages, "merge_landed", return_value=False),
                mock.patch.object(runner_mod.followups, "append_followups"),
                mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")),
                mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
                mock.patch.object(StagedExecutor, "_acquire_checkout_lease", return_value=None),
                mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
                mock.patch.object(StagedExecutor, "_pipeline_for", return_value=("report",)),
                mock.patch("todo_system.worker.runner_heartbeat_start"),
                mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run),
                mock.patch("subprocess.run", side_effect=fake_run),
            ):
                stack.enter_context(p)
            return StagedExecutor().execute(msg, _agent(), progress=lambda _: None)

    def test_marker_parks_with_the_fixed_reason(self):
        out = self._run("---\nmerged: no\nrebase_conflict: true\n---\n# report\n")
        self.assertTrue(out.ok, out.summary)                 # review-required is a success exit
        self.assertTrue(out.payload.get("review_required"))
        self.assertIn("동시 실행 충돌 — 겹침 오판", out.summary)
        # the fixed string is owned by the runner, not the agent's prose
        self.assertIn("rebase 충돌", out.summary)

    def test_no_marker_falls_through_to_the_generic_note(self):
        # a plain not-landed (no marker) still parks for review, but with the
        # generic note — the fixed concurrency string must NOT appear
        out = self._run("---\nmerged: no\nworker_mode_recommendation: auto\n---\n# report\n")
        self.assertTrue(out.ok, out.summary)
        self.assertTrue(out.payload.get("review_required"))
        self.assertNotIn("동시 실행 충돌", out.summary)
        self.assertIn("did NOT land", out.summary)


class CapacityCheckpointTest(unittest.TestCase):
    """A stage DEFERRED on a usage/rate limit must checkpoint-push the external
    branch — the same save the timeout path does — and report capacity_limited=True.
    Without it the stage's local commits are rewound into orphans by the next
    dispatch's prepare_external_branch and the retry re-implements blind (the
    mulkko-32 timeout loss, in the capacity shape). Mirrors TimeoutCaptureTest."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runs = self.tmp / "runs"
        self.runs.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_capacity(self, repo_url="git@github.com:x/y.git", pipeline=("implement",),
                      returncode=1, usage_rec=None,
                      stdout="You've reached your usage limit - run /usage-credits"):
        pushed: dict = {}

        def fake_checkpoint(repo_dir, branch, progress=None):
            pushed["called"] = (str(repo_dir), branch)
            return {"pushed": True, "tip": "abc1234def", "commits": 2,
                    "note": "checkpoint pushed 2 commit(s) to origin/worker/t"}

        def fake_run(argv, **kw):
            # A capacity marker in stdout is what is_capacity_limit catches.
            # returncode != 0 drives the REAL rc-gated capacity branch (not a mock);
            # returncode == 0 reproduces the 2026-07-31 blind spot, where the CLI
            # printed the out-of-credits notice yet exited 0 — discriminated by the
            # usage record (the mocked record_stage_usage return, below).
            return subprocess.CompletedProcess(argv, returncode, stdout, "")

        vp = subprocess.CompletedProcess([], 0, "", "")
        vp.problem = None
        vp.orphaned_commits = None
        payload = {"repo": repo_url, "auto_merge": False, "model_tier": "heavy"}
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="t", payload=payload)
        # An ExitStack, not one `with`: 20 managers sat exactly at CPython's
        # static block-nesting limit (a 21st is a SyntaxError at import).
        with contextlib.ExitStack() as stack:
            for p in (
                mock.patch.object(repos, "ensure_worktree", return_value=self.tmp / "clone"),
                mock.patch.object(stages, "restore_main"),
                mock.patch.object(stages, "install_push_guard"),
                mock.patch.object(stages, "default_branch", return_value="main"),
                mock.patch.object(stages, "prepare_external_branch",
                                  return_value=subprocess.CompletedProcess([], 0, "", "")),
                mock.patch.object(stages, "prepare_vault_branch", return_value=vp),
                mock.patch.object(stages, "remote_branch_exists", return_value=True),
                mock.patch.object(stages, "RUNS_DIR", self.runs),
                mock.patch.object(stages, "build_stage_argv", return_value=["true"]),
                mock.patch.object(stages, "checkpoint_push", side_effect=fake_checkpoint),
                mock.patch.object(stages, "invalidate_model_cache_if_unavailable"),
                mock.patch.object(stages, "record_stage_usage", return_value=usage_rec),
                mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")),
                mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
                mock.patch.object(StagedExecutor, "_acquire_checkout_lease", return_value=None),
                mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
                mock.patch.object(StagedExecutor, "_pipeline_for", return_value=pipeline),
                mock.patch("todo_system.worker.runner_heartbeat_start"),
                mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run),
                mock.patch("subprocess.run", side_effect=fake_run),
            ):
                stack.enter_context(p)
            out = StagedExecutor().execute(msg, _agent(), progress=lambda _: None)
        return out, pushed

    def test_external_capacity_checkpoints_and_reports_deferred(self):
        out, pushed = self._run_capacity()
        self.assertFalse(out.ok)
        self.assertTrue(out.payload.get("capacity_limited"))
        self.assertEqual(out.payload.get("checkpoint_sha"), "abc1234def")
        # checkpoint_push was called positionally with the CLONE dir and worker
        # branch — Concern C (verify.md 2026-09-20 :63): the checkpoint must target
        # the external clone, never the vault root. Present and asserted at HEAD.
        self.assertEqual(pushed.get("called", (None, None))[0], str(self.tmp / "clone"))
        self.assertEqual(pushed.get("called", (None, None))[1], "worker/t")
        # stages.json records the marker the retry's resume note reads
        run_dirs = list(self.runs.iterdir())
        self.assertEqual(len(run_dirs), 1, run_dirs)
        rec = json.loads((run_dirs[0] / stages.STAGE_PROGRESS).read_text(encoding="utf-8"))
        self.assertEqual(rec["capacity_limited_stage"], "implement")
        self.assertEqual(rec["checkpoint_sha"], "abc1234def")

    def test_vault_capacity_defers_without_a_checkpoint(self):
        # A vault task (no repo_url) has no external branch to push, but must still
        # report capacity_limited=True so the deferral + resume note both fire.
        out, pushed = self._run_capacity(repo_url=None)
        self.assertFalse(out.ok)
        self.assertTrue(out.payload.get("capacity_limited"))
        self.assertNotIn("called", pushed)                  # checkpoint_push not invoked
        self.assertIsNone(out.payload.get("checkpoint_sha"))

    def test_report_stage_capacity_skips_checkpoint(self):
        # RED control for defect B (capacity path): `report` runs only AFTER a
        # squash-merge that deleted origin/<branch>, so it carries no unpushed work.
        # Left to checkpoint, checkpoint_push would base off origin/<default>, count
        # the already-merged commits as "ahead", and re-push the merged branch (a
        # duplicate-PR shape). The deferral must still be reported, but WITHOUT push.
        out, pushed = self._run_capacity(pipeline=("report",))
        self.assertFalse(out.ok)
        self.assertTrue(out.payload.get("capacity_limited"))
        self.assertNotIn("called", pushed)                  # checkpoint_push not invoked
        self.assertIsNone(out.payload.get("checkpoint_sha"))
        run_dirs = list(self.runs.iterdir())
        self.assertEqual(len(run_dirs), 1, run_dirs)
        rec = json.loads((run_dirs[0] / stages.STAGE_PROGRESS).read_text(encoding="utf-8"))
        self.assertEqual(rec["capacity_limited_stage"], "report")
        self.assertIsNone(rec["checkpoint_sha"])

    def test_rc0_quota_cut_no_api_work_defers(self):
        # RED control (2026-07-31 blind spot): the CLI printed its out-of-credits
        # notice to stdout yet exited 0, so the rc!=0-only gate let a refused,
        # zero-work implement fall through to the artifact check (run.log the
        # runner always writes) and be promoted to `completed` — verify then judged
        # partial work. The rc=0 discriminator is the usage record: a refused call
        # logs no API message, so record_stage_usage yields {"unavailable": "no
        # usage in transcript"} (stage_usage's rc=0 signature — no suffix). Must now
        # defer like the rc!=0 arm: checkpoint + capacity_limited + stages.json.
        out, pushed = self._run_capacity(
            returncode=0, usage_rec={"unavailable": "no usage in transcript"})
        self.assertTrue(out.payload.get("capacity_limited"))
        self.assertEqual(out.payload.get("checkpoint_sha"), "abc1234def")
        run_dirs = list(self.runs.iterdir())
        self.assertEqual(len(run_dirs), 1, run_dirs)
        rec = json.loads((run_dirs[0] / stages.STAGE_PROGRESS).read_text(encoding="utf-8"))
        self.assertEqual(rec["capacity_limited_stage"], "implement")
        self.assertEqual(rec["checkpoint_sha"], "abc1234def")

    def test_rc0_green_stage_quoting_capacity_not_deferred(self):
        # The false-positive pin (the other direction). THIS task's own implement
        # writes the capacity markers into _CAPACITY_MARKERS, so a healthy green
        # stage can echo "/usage-credits" in stdout while having done real API work.
        # It must NOT be deferred: the usage record carries a real turn count, not
        # the refused-call "no usage in transcript" marker. Green pre- AND post-fix.
        out, pushed = self._run_capacity(
            returncode=0,
            usage_rec={"num_turns": 3, "input_tokens": 100, "model": "claude-opus-4-8"})
        self.assertFalse(out.payload.get("capacity_limited"))
        run_dirs = list(self.runs.iterdir())
        self.assertEqual(len(run_dirs), 1, run_dirs)
        rec = json.loads((run_dirs[0] / stages.STAGE_PROGRESS).read_text(encoding="utf-8"))
        self.assertNotIn("capacity_limited_stage", rec)
        self.assertIn("implement", rec.get("completed", []))
        # NB: the :853 implement-checkpoint DOES fire on this green path, so we
        # deliberately do NOT assert checkpoint_push was uncalled.


class CleanupHookTest(unittest.TestCase):
    """Build-daemon cleanup must fire after EVERY stage and on every run exit, so
    Gradle/Kotlin daemons do not accumulate in the runner cgroup (the runner died
    with its in-flight run, 2026-09-14) and so a timed-out stage's JVM is gone
    before the checkpoint push has to talk to a remote over `memory.high`
    (2026-09-25). Pins all three exits — success, verify-FAIL, and the timeout
    whose TimeoutExpired escapes the `try` — plus the per-stage cadence, the
    cleanup-before-checkpoint ORDER, and fail-open.

    The counts below are `stages + 1`: one call per stage in the loop, one in the
    `finally` (which is still the only call on the paths that never reach the
    loop). A bare `assert_called_once` would go quiet the moment either half is
    dropped, so the count is spelled out."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        self.clone = self.tmp / "clone"
        self.checkpoint = mock.Mock(return_value={"pushed": False, "note": "", "tip": None})
        # Cleanup and checkpoint append their names here, so a test can assert the
        # ORDER of two calls that both merely "happened" under call_count.
        self.order: list[str] = []
        # Mocked, never real: an unmocked removal would run git / rmtree under ~/Worker.
        self.remove_wt = mock.Mock(side_effect=lambda *a, **k: (
            self.order.append("remove_worktree") or True))
        self.remove_reg = mock.Mock(side_effect=lambda *a, **k: (
            self.order.append("remove_registry") or True))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, pipeline, fake_run, repo_url="git@github.com:x/y.git",
             cleanup_side_effect=None):
        def record_cleanup(repo_dir, progress=None):
            self.order.append("cleanup")
            if cleanup_side_effect is not None:
                raise cleanup_side_effect
            return {"ran": True, "note": "rc=0"}

        cleanup = mock.Mock(side_effect=record_cleanup)
        vp = subprocess.CompletedProcess([], 0, "", "")
        vp.problem = None
        vp.orphaned_commits = None
        payload = {"repo": repo_url, "auto_merge": False, "model_tier": "heavy"}
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="t", payload=payload)
        # An ExitStack, not one `with`: 19 managers left one slot below CPython's
        # static block-nesting limit (a 21st is a SyntaxError at import).
        with contextlib.ExitStack() as stack:
            for p in (
                mock.patch.object(repos, "ensure_worktree", return_value=self.clone),
                mock.patch.object(stages, "restore_main"),
                mock.patch.object(stages, "install_push_guard"),
                mock.patch.object(stages, "default_branch", return_value="main"),
                mock.patch.object(stages, "prepare_external_branch",
                                  return_value=subprocess.CompletedProcess([], 0, "", "")),
                mock.patch.object(stages, "prepare_vault_branch", return_value=vp),
                mock.patch.object(stages, "remote_branch_exists", return_value=True),
                mock.patch.object(stages, "RUNS_DIR", self.runs),
                mock.patch.object(stages, "build_stage_argv", return_value=["true"]),
                mock.patch.object(stages, "checkpoint_push", self.checkpoint),
                mock.patch.object(stages, "cleanup_build_daemons", cleanup),
                mock.patch.object(stages, "remove_worktree", self.remove_wt),
                mock.patch.object(runner_mod.worktree_sweep, "remove_registry", self.remove_reg),
                mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")),
                mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
                mock.patch.object(StagedExecutor, "_acquire_checkout_lease", return_value=None),
                mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
                mock.patch.object(StagedExecutor, "_pipeline_for", return_value=pipeline),
                mock.patch("todo_system.worker.runner_heartbeat_start"),
                mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run),
                mock.patch("subprocess.run", side_effect=fake_run),
            ):
                stack.enter_context(p)
            out = StagedExecutor().execute(msg, _agent(), progress=lambda _: None)
        return out, cleanup

    def _run_dir(self) -> Path:
        dirs = list(self.runs.iterdir())
        self.assertEqual(len(dirs), 1, dirs)
        return dirs[0]

    def _assert_all_targeted(self, cleanup, target) -> None:
        """Every call — per-stage and finally alike — names the same dir."""
        self.assertEqual([c.args[0] for c in cleanup.call_args_list],
                         [target] * cleanup.call_count)

    def test_success_path_stops_daemons_in_the_clone(self):
        out, cleanup = self._run(
            ("implement",),
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
        self.assertTrue(out.ok, out.summary)
        self.assertEqual(cleanup.call_count, 2)     # 1 stage + finally
        self._assert_all_targeted(cleanup, self.clone)

    def test_verify_fail_path_stops_daemons_in_the_clone(self):
        def fake_run(argv, **kw):
            (self._run_dir() / "verify.md").write_text(
                "---\noverall_status: fail\n---\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")

        out, cleanup = self._run(("verify",), fake_run)
        self.assertFalse(out.ok)
        self.assertEqual(out.payload.get("verify"), "FAIL")
        self.assertEqual(cleanup.call_count, 2)     # 1 stage + finally
        self._assert_all_targeted(cleanup, self.clone)

    def test_timeout_path_stops_daemons_in_the_clone(self):
        def fake_run(argv, **kw):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout"),
                                            output="partial")

        out, cleanup = self._run(("implement",), fake_run)
        self.assertTrue(out.payload.get("timed_out"))
        self.assertEqual(cleanup.call_count, 2)     # timeout path + finally
        self._assert_all_targeted(cleanup, self.clone)

    def test_vault_run_stops_daemons_in_the_vault_root(self):
        # A vault task (no repo_url) still calls cleanup, with the vault-data root
        # — which no-ops for real (no gradlew) but both hooks must still fire.
        out, cleanup = self._run(
            ("implement",),
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
            repo_url=None)
        self.assertEqual(cleanup.call_count, 2)     # 1 stage + finally
        self._assert_all_targeted(cleanup, stages._VAULT_DATA_ROOT)

    def test_per_stage_cleanup_runs_for_every_stage(self):
        """The cadence AC 5 asks for: once the stage ENDS, not once per run. With
        only the `finally` hook this is 1 however long the pipeline is."""
        def fake_run(argv, **kw):
            # build_stage_argv is stubbed to ["true"], so the fake cannot tell
            # which stage called it — write all three artifacts up front so every
            # stage clears its own artifact check and the loop reaches the end.
            d = self._run_dir()
            for s in ("research", "plan", "implement"):
                (d / stages.STAGE_ARTIFACT[s]).write_text("x\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")

        out, cleanup = self._run(("research", "plan", "implement"), fake_run)
        self.assertTrue(out.ok, out.summary)
        self.assertEqual(cleanup.call_count, 4)     # 3 stages + finally
        self._assert_all_targeted(cleanup, self.clone)
        self.assertEqual(self.order.count("cleanup"), 4)

    def test_timeout_stops_daemons_before_the_checkpoint_push(self):
        """The substance of this leaf. The checkpoint talks to a remote, and on
        2026-09-25 that git call wedged for tens of minutes because the timed-out
        stage's Gradle JVM still held the runner cgroup over `memory.high`. So the
        daemons have to be gone BEFORE the push, not in the `finally` after it."""
        def fake_run(argv, **kw):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout"),
                                            output="partial")

        self.checkpoint.side_effect = lambda *a, **kw: (
            self.order.append("checkpoint")
            or {"pushed": True, "note": "n", "tip": "abc1234"})
        out, _ = self._run(("implement",), fake_run)
        self.assertTrue(out.payload.get("timed_out"))
        self.assertEqual(out.payload.get("checkpoint_sha"), "abc1234")
        # the cleanup that precedes the push is the timeout path's own, and the
        # `finally` one still runs after it
        self.assertEqual(self.order, ["cleanup", "checkpoint", "cleanup"])

    def test_a_raising_cleanup_does_not_change_the_run_outcome(self):
        """Fail-open, per AC 5's second half: `_stop_build_daemons` wraps a
        contract that already never raises, and the run must survive even if that
        contract is broken — including the new in-loop call site, where a raise
        would land inside the stage loop rather than the `finally`."""
        out, cleanup = self._run(
            ("implement",),
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
            cleanup_side_effect=RuntimeError("boom"))
        self.assertTrue(out.ok, out.summary)
        self.assertEqual(cleanup.call_count, 2)

    def test_success_reaps_worktree_then_its_registry_after_daemon_stop(self):
        out, _ = self._run(
            ("implement",),
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
        self.assertTrue(out.ok, out.summary)
        self.remove_wt.assert_called_once()
        self.assertEqual(self.remove_wt.call_args.args,
                         (repos.local_path("git@github.com:x/y.git"), self.clone))
        self.remove_reg.assert_called_once()
        self.assertEqual(self.remove_reg.call_args.args, (self.clone.name,))
        self.assertEqual(self.order, ["cleanup", "cleanup", "remove_worktree", "remove_registry"])

    def test_failed_worktree_removal_keeps_the_registry(self):
        self.remove_wt.side_effect = None
        self.remove_wt.return_value = False
        out, _ = self._run(
            ("implement",),
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
        self.assertTrue(out.ok, out.summary)
        self.remove_wt.assert_called_once()
        self.remove_reg.assert_not_called()

    def test_verify_fail_keeps_worktree_and_registry_as_evidence(self):
        def fake_run(argv, **kw):
            (self._run_dir() / "verify.md").write_text(
                "---\noverall_status: fail\n---\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")

        out, _ = self._run(("verify",), fake_run)
        self.assertFalse(out.ok)
        self.assertEqual(out.payload.get("verify"), "FAIL")
        self.remove_wt.assert_not_called()
        self.remove_reg.assert_not_called()

    def test_timeout_keeps_worktree_and_registry_as_evidence(self):
        def fake_run(argv, **kw):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout"),
                                            output="partial")

        out, _ = self._run(("implement",), fake_run)
        self.assertTrue(out.payload.get("timed_out"))
        self.remove_wt.assert_not_called()
        self.remove_reg.assert_not_called()

    def test_vault_run_reaps_nothing(self):
        self._run(("implement",),
                  lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
                  repo_url=None)
        self.remove_wt.assert_not_called()
        self.remove_reg.assert_not_called()

    def test_stage_subprocess_env_carries_worker_gradle_opts(self):
        # The wiring for criterion ③: the stage subprocess must run with the
        # worker Gradle limits in its GRADLE_OPTS (never None, so os.environ is
        # inherited whole). `stage_env_with_gradle` is left real here on purpose.
        captured = {}

        def fake_run(argv, **kw):
            captured["env"] = kw.get("env")
            return subprocess.CompletedProcess(argv, 0, "", "")

        out, _ = self._run(("implement",), fake_run)
        self.assertTrue(out.ok, out.summary)
        self.assertIsNotNone(captured["env"], "stage got env=None, not the Gradle-limited env")
        self.assertIn(stages.WORKER_GRADLE_OPTS, captured["env"].get("GRADLE_OPTS", ""))

    def test_stage_subprocess_runs_with_background_tasks_disabled(self):
        # mulkko-39 (2026-09-17): verify backgrounded a Gradle run, ended its turn
        # "waiting", and the process exited with no verify.md. The stage env must
        # carry the CLI switch that makes run_in_background run in the foreground.
        captured = {}

        def fake_run(argv, **kw):
            captured["env"] = kw.get("env")
            return subprocess.CompletedProcess(argv, 0, "", "")

        out, _ = self._run(("implement",), fake_run)
        self.assertTrue(out.ok, out.summary)
        self.assertEqual(captured["env"].get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"), "1")

    def test_finished_implement_is_checkpointed_before_verify_runs(self):
        # The work must be on origin BEFORE the next stage can die: verify below
        # exits without verify.md, and the checkpoint has already happened.
        order = []

        def fake_checkpoint(repo, branch, progress=None):
            order.append("checkpoint")
            return {"pushed": True, "note": "checkpoint pushed 5 commit(s)", "tip": "7f98886"}

        def fake_run(argv, **kw):
            order.append("stage")
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.checkpoint.side_effect = fake_checkpoint
        out, _ = self._run(("implement", "verify"), fake_run)
        self.assertFalse(out.ok)
        self.assertIn("missing verify.md", out.summary)
        self.assertEqual(order, ["stage", "checkpoint", "stage"])   # implement, push, verify
        self.checkpoint.assert_called_once_with(self.clone, "worker/t")

    def test_no_checkpoint_when_implement_did_not_finish(self):
        # A stage that did not produce its artifact is not "finished work" — the
        # timeout path owns partial-implement checkpoints; this hook does not.
        self.checkpoint.reset_mock()
        with mock.patch.object(stages, "STAGE_ARTIFACT",
                               {**stages.STAGE_ARTIFACT, "implement": "never-written.log"}):
            out, _ = self._run(("implement",),
                               lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
        self.assertFalse(out.ok)
        self.checkpoint.assert_not_called()

    def test_vault_implement_is_not_checkpointed(self):
        # No external clone, no branch to push from here (vault branches have
        # their own publish path).
        # A held host checkout lock used to defer this run (`checkout_busy`) before
        # the stage loop, so "not checkpointed" passed without ever being tested.
        # The lease is held here on purpose: the run must not reach it at all.
        with mock.patch.object(runner_mod.lease_mod, "Lease") as lease_cls:
            lease_cls.return_value.acquire.return_value = False
            out, _ = self._run(("implement",),
                               lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
                               repo_url=None)
        self.checkpoint.assert_not_called()
        self.assertNotIn("checkout_busy", out.payload, out.summary)
        self._run_dir()                     # the stage loop was reached
        lease_cls.assert_not_called()       # no lease on the live vault path


class ImplementCheckpointTest(unittest.TestCase):
    """After `implement` is recorded done, an EXTERNAL-repo run checkpoint-pushes
    worker/<slug> to origin — so if verify then dies (max-turns, subprocess
    timeout, missing verify.md) resumable_run inherits implement (runner :701-711)
    and the retry CONTINUES the commits instead of the branch prep rewinding them
    into orphans. Best-effort: a push failure only logs a warning and the run goes
    on. Vault tasks and already-inherited implements never checkpoint."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        self.clone = self.tmp / "clone"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, pipeline, fake_run, checkpoint, *,
             repo_url="git@github.com:x/y.git", resumable=None, inherit=None,
             remote_exists=True):
        progress_lines: list[str] = []
        vp = subprocess.CompletedProcess([], 0, "", "")
        vp.problem = None
        vp.orphaned_commits = None
        payload = {"repo": repo_url, "auto_merge": False, "model_tier": "heavy"}
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="t", payload=payload)
        # An ExitStack, not one `with`: 21 managers in a single statement exceed
        # CPython's static block-nesting limit (SyntaxError at import).
        with contextlib.ExitStack() as stack:
            for p in (
                mock.patch.object(repos, "ensure_worktree", return_value=self.clone),
                mock.patch.object(stages, "restore_main"),
                mock.patch.object(stages, "install_push_guard"),
                mock.patch.object(stages, "default_branch", return_value="main"),
                mock.patch.object(stages, "prepare_external_branch",
                                  return_value=subprocess.CompletedProcess([], 0, "", "")),
                mock.patch.object(stages, "prepare_vault_branch", return_value=vp),
                mock.patch.object(stages, "remote_branch_exists", return_value=remote_exists),
                mock.patch.object(stages, "resumable_run", return_value=resumable),
                mock.patch.object(stages, "inherit_artifacts", return_value=(inherit or [])),
                mock.patch.object(stages, "RUNS_DIR", self.runs),
                mock.patch.object(stages, "build_stage_argv", return_value=["true"]),
                mock.patch.object(stages, "checkpoint_push", checkpoint),
                mock.patch.object(stages, "cleanup_build_daemons"),
                mock.patch.object(stages, "remove_worktree"),
                mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")),
                mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
                mock.patch.object(StagedExecutor, "_acquire_checkout_lease", return_value=None),
                mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
                mock.patch.object(StagedExecutor, "_pipeline_for", return_value=pipeline),
                mock.patch("todo_system.worker.runner_heartbeat_start"),
                mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run),
                mock.patch("subprocess.run", side_effect=fake_run),
            ):
                stack.enter_context(p)
            out = StagedExecutor().execute(
                msg, _agent(), progress=lambda s: progress_lines.append(s))
        return out, progress_lines

    def _run_dir(self) -> Path:
        dirs = list(self.runs.iterdir())
        self.assertEqual(len(dirs), 1, dirs)
        return dirs[0]

    def test_success_checkpoints_external_branch(self):
        checkpoint = mock.Mock(return_value={
            "pushed": True, "tip": "abc1234", "commits": 1,
            "note": "checkpoint pushed 1 commit(s) to origin/worker/t"})
        out, progress = self._run(
            ("implement",),
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
            checkpoint)
        self.assertTrue(out.ok, out.summary)
        checkpoint.assert_called_once()
        # called positionally with the CLONE and the worker branch, no `progress` kwarg
        self.assertEqual(checkpoint.call_args.args, (self.clone, "worker/t"))
        self.assertEqual(checkpoint.call_args.kwargs, {})
        self.assertTrue(any("implement checkpoint:" in p for p in progress), progress)

    def test_push_failure_is_best_effort(self):
        # checkpoint_push raising must NOT sink the run: verify + report still run.
        checkpoint = mock.Mock(side_effect=RuntimeError("boom"))

        def fake_run(argv, **kw):
            d = self._run_dir()
            (d / "verify.md").write_text("---\noverall_status: pass\n---\n", encoding="utf-8")
            (d / "report.md").write_text(
                "---\noverall_status: pass\npr_url: null\n---\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")

        out, progress = self._run(("implement", "verify", "report"), fake_run, checkpoint)
        self.assertTrue(out.ok, out.summary)
        self.assertIn("3 stages", out.summary)          # verify+report ran past the failure
        checkpoint.assert_called_once()
        self.assertTrue(any("implement checkpoint skipped" in p for p in progress), progress)

    def test_rejected_push_note_is_logged_and_run_continues(self):
        # A non-raising rejection (rc!=0 → note, pushed=False) also just logs.
        checkpoint = mock.Mock(return_value={
            "pushed": False, "tip": "abc1234", "commits": 1,
            "note": "checkpoint push of worker/t rejected: non-fast-forward"})
        out, progress = self._run(
            ("implement",),
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
            checkpoint)
        self.assertTrue(out.ok, out.summary)
        self.assertTrue(any("rejected" in p for p in progress), progress)

    def test_vault_task_does_not_checkpoint(self):
        checkpoint = mock.Mock(return_value={"pushed": False, "note": "", "tip": None})
        # Same pin as CleanupHookTest.test_vault_implement_is_not_checkpointed: a
        # held host lock must not turn this into an early, vacuous pass.
        with mock.patch.object(runner_mod.lease_mod, "Lease") as lease_cls:
            lease_cls.return_value.acquire.return_value = False
            out, _ = self._run(
                ("implement",),
                lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
                checkpoint, repo_url=None)
        checkpoint.assert_not_called()
        self.assertNotIn("checkout_busy", out.payload, out.summary)
        self._run_dir()
        lease_cls.assert_not_called()

    def test_inherited_implement_does_not_checkpoint(self):
        # implement inherited from a prior run `continue`s before the checkpoint
        # (runner :734-737), so a resumed run never re-pushes.
        checkpoint = mock.Mock(return_value={"pushed": True, "note": "n", "tip": "t"})
        prior = self.tmp / "prior"
        prior.mkdir()
        out, _ = self._run(
            ("implement",),
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
            checkpoint, resumable=(prior, ["implement"]), inherit=["implement"])
        checkpoint.assert_not_called()


class LiveSiblingCheckoutGuardTest(unittest.TestCase):
    """`restore_main` forces past a dirty tree, and the vault is ONE checkout.

    So a run starting while a sibling run was mid-stage on `worker/<other>` used
    to `checkout -f main` straight over that sibling's uncommitted edits. These
    pin the guard from both sides: a LIVE sibling holds the run back, and
    everything else — including "cannot tell" — restores main exactly as before.

    The repo is a REAL tempdir git repo rather than a mock, because the bug being
    fixed *is* git's behaviour: the fail-open tests below only mean something if
    the destruction they permit actually happens. `stages._VAULT_DATA_ROOT` — the
    root the runner's vault-checkout git ops read — is patched to it, so the
    live vault is never touched.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "vault"
        self.repo.mkdir()
        self.mbroot = self.tmp / "agents"      # ClaimStore root (created on write)
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        self.f = self.repo / "f.txt"

        self._git("init", "-b", "main")
        self._git("config", "user.email", "worker@example.com")
        self._git("config", "user.name", "worker")
        self._git("config", "commit.gpgsign", "false")
        self.f.write_text("base\n", encoding="utf-8")
        self._git("add", "f.txt")
        self._git("commit", "-m", "base")
        # f.txt MUST differ between main and the sibling branch. If both sides
        # matched, a plain `git checkout main` would succeed and restore_main's
        # `checkout -f` fallback — the destructive path under test — would never
        # fire, leaving every assertion below vacuously true.
        self._git("checkout", "-b", "worker/sibling")
        self.f.write_text("sibling\n", encoding="utf-8")
        self._git("commit", "-am", "sibling")
        self.f.write_text("sibling+uncommitted\n", encoding="utf-8")   # the work at risk

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------
    def _git(self, *args) -> subprocess.CompletedProcess:
        r = subprocess.run(["git", "-C", str(self.repo), *args],
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, f"git {args} failed: {r.stderr}")
        return r

    def _head(self) -> str:
        return self._git("branch", "--show-current").stdout.strip()

    def _claim(self, slug: str, agent_id: str = "armchair-commander",
               branch: str | None = None) -> dict:
        """A claim for `slug` owned by THIS pid — so `classify` says LIVE.
        `branch` goes through the payload, the same route a real dispatch's
        override takes to `branch_for`."""
        store = claims.ClaimStore(self.mbroot, agent_id)
        payload = {"branch": branch} if branch else {}
        return store.open(m.Envelope(to=agent_id, type=m.TASK_ASSIGN, task_id=slug,
                                     payload=payload))

    def _strip_branch_field(self, claim: dict,
                            agent_id: str = "armchair-commander") -> None:
        """Rewrite a claim as one written before `open()` recorded `branch`."""
        p = claims.ClaimStore(self.mbroot, agent_id).path(claim["msg_id"])
        data = json.loads(p.read_text(encoding="utf-8"))
        # If open() ever stops recording the field, say so HERE rather than
        # letting the back-compat tests below quietly become the only path.
        self.assertIn("branch", data, "open() no longer records branch")
        data.pop("branch")
        p.write_text(json.dumps(data), encoding="utf-8")

    @contextlib.contextmanager
    def _sandbox(self, mock_restore: bool = False):
        """Yields the restore_main mock, or None when it runs for real."""
        with contextlib.ExitStack() as st:
            # The runner's vault-checkout git ops read stages._VAULT_DATA_ROOT;
            # patch it so restore_main's `checkout -f` can never reach the live vault.
            st.enter_context(mock.patch.object(stages, "_VAULT_DATA_ROOT", self.repo))
            st.enter_context(mock.patch.object(stages, "RUNS_DIR", self.runs))
            st.enter_context(mock.patch.object(stages, "install_push_guard"))
            st.enter_context(mock.patch.object(runner_mod, "DEFAULT_ROOT", self.mbroot))
            st.enter_context(mock.patch.object(StagedExecutor, "_account_env",
                                               return_value=(None, "")))
            st.enter_context(mock.patch.object(StagedExecutor, "_pipeline_for",
                                               return_value=()))
            st.enter_context(mock.patch.object(runner_mod.disk_guard, "ensure_free_space",
                                               return_value={}))
            restore = None
            if mock_restore:
                restore = st.enter_context(mock.patch.object(stages, "restore_main"))
            yield restore

    def _run(self, task_id: str = "mine"):
        # No `repo` key == a vault task, which is the shared-checkout case.
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id=task_id,
                         payload={"auto_merge": False})
        return StagedExecutor().execute(msg, _agent(), progress=lambda _: None)

    # -- (a) the guard holds ----------------------------------------------
    def test_a_live_sibling_holds_the_run_and_restore_main_never_runs(self):
        self._claim("sibling")

        with self._sandbox(mock_restore=True) as restore:
            out = self._run()

        self.assertEqual(restore.call_count, 0,
                         "restore_main ran while a LIVE sibling owned the tree")
        self.assertFalse(out.ok)
        self.assertIn("deferred", out.summary)
        self.assertTrue(out.payload["checkout_busy"])
        self.assertEqual(out.payload["held_by"], "sibling")
        self.assertEqual(self._head(), "worker/sibling", "HEAD was moved anyway")

    # -- (d) the whole point: the sibling's uncommitted work survives -------
    def test_the_siblings_uncommitted_file_survives(self):
        """Same as (a) but restore_main is NOT mocked, so the only thing standing
        between `checkout -f` and the file is the guard itself."""
        self._claim("sibling")

        with self._sandbox(mock_restore=False):
            out = self._run()

        self.assertTrue(out.payload["checkout_busy"])
        self.assertEqual(self.f.read_text(encoding="utf-8"), "sibling+uncommitted\n")
        self.assertEqual(self._head(), "worker/sibling")

    # -- (b) fail-open — and proof the fixture really is destructive --------
    def test_no_claim_fails_open_and_main_is_restored(self):
        """No claim at all: unchanged behaviour, destruction included. This GREEN
        is what stops (a)/(d) from passing vacuously — it shows the same fixture
        losing the file when the guard does not fire."""
        with self._sandbox(mock_restore=False):
            out = self._run()

        self.assertTrue(out.ok)
        self.assertNotIn("checkout_busy", out.payload)
        self.assertEqual(self._head(), "main")
        self.assertEqual(self.f.read_text(encoding="utf-8"), "base\n",
                         "fixture is not destructive — (a)/(d) would prove nothing")

    def test_an_orphan_claim_fails_open(self):
        """The claiming pid is gone. 'Probably dead' must not stall the queue."""
        self._claim("sibling")

        with self._sandbox(mock_restore=False), \
                mock.patch.object(claims, "_alive_default", return_value=False):
            out = self._run()

        self.assertNotIn("checkout_busy", out.payload)
        self.assertEqual(self._head(), "main")
        self.assertEqual(self.f.read_text(encoding="utf-8"), "base\n")

    def test_a_live_claim_for_an_unrelated_slug_fails_open(self):
        """A LIVE claim is not enough — it has to own the branch HEAD is on."""
        self._claim("somebody-else")

        with self._sandbox(mock_restore=False):
            out = self._run()

        self.assertNotIn("checkout_busy", out.payload)
        self.assertEqual(self._head(), "main")

    # -- (c) HEAD already on main -----------------------------------------
    def test_head_already_on_main_proceeds(self):
        self._git("checkout", "-f", "main")
        self._claim("sibling")

        with self._sandbox(mock_restore=True) as restore:
            out = self._run()

        self.assertGreaterEqual(restore.call_count, 1, "the run did not proceed")
        self.assertNotIn("checkout_busy", out.payload)
        self.assertTrue(out.ok)

    # -- (e) our own branch is not a sibling -------------------------------
    def test_our_own_live_claim_does_not_hold_us(self):
        """`poll_once` opens this run's claim before `handle()`, so the store
        always contains a LIVE claim for us. A resume/retry must still be able to
        check out its own branch."""
        self._claim("sibling")

        with self._sandbox(mock_restore=True) as restore:
            out = self._run(task_id="sibling")     # HEAD is worker/sibling == ours

        self.assertGreaterEqual(restore.call_count, 1, "held by our own claim")
        self.assertNotIn("checkout_busy", out.payload)

    def test_a_broken_guard_fails_open_instead_of_killing_the_run(self):
        """The guard is advisory and runs on EVERY run's start path, so a fault
        inside it must degrade to today's behaviour, not abort the pipeline.
        `agent.id` is a Mock here — exactly what several stage tests pass — which
        used to blow up ClaimStore.dir with a TypeError."""
        self._claim("sibling")

        with self._sandbox(mock_restore=False):
            msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="mine",
                             payload={"auto_merge": False})
            out = StagedExecutor().execute(msg, mock.Mock(), progress=lambda _: None)

        self.assertNotIn("checkout_busy", out.payload)
        self.assertEqual(self._head(), "main")

    def test_an_unreadable_claims_dir_fails_open(self):
        self._claim("sibling")

        with self._sandbox(mock_restore=True) as restore, \
                mock.patch.object(claims.ClaimStore, "all", side_effect=OSError("boom")):
            out = self._run()

        self.assertGreaterEqual(restore.call_count, 1)
        self.assertNotIn("checkout_busy", out.payload)

    # -- (f) the branch the claim actually recorded ------------------------
    def test_a_branch_overriding_live_sibling_now_holds(self):
        """A sibling whose payload overrode `branch` sits on a branch its slug
        does not spell, so recomputing `worker/<slug>` missed it and restored
        main straight over its tree. The recorded branch is what catches it."""
        self._git("checkout", "-b", "custom/sibling-override")   # f.txt stays dirty
        self._claim("sibling", branch="custom/sibling-override")

        with self._sandbox(mock_restore=False):
            out = self._run()

        self.assertTrue(out.payload["checkout_busy"])
        self.assertEqual(out.payload["held_by"], "sibling")
        self.assertEqual(self._head(), "custom/sibling-override")
        self.assertEqual(self.f.read_text(encoding="utf-8"), "sibling+uncommitted\n")

    def test_an_overriding_sibling_with_a_pre_field_claim_still_fails_open(self):
        """The one hole left, pinned: a claim written before `open()` recorded the
        field carries no branch to match, so an overriding sibling still loses its
        tree. This is also what stops the test above from passing vacuously — same
        branch, same dirty file, and the file dies as soon as the guard misses."""
        self._git("checkout", "-b", "custom/sibling-override")
        self._strip_branch_field(
            self._claim("sibling", branch="custom/sibling-override"))

        with self._sandbox(mock_restore=False):
            out = self._run()

        self.assertNotIn("checkout_busy", out.payload)
        self.assertEqual(self._head(), "main")
        self.assertEqual(self.f.read_text(encoding="utf-8"), "base\n",
                         "fixture is not destructive — the test above proves nothing")

    def test_a_pre_field_claim_still_holds_on_the_recomputed_branch(self):
        """Back-compat: with no `branch` key at all, the slug recomputation has to
        hold exactly as it did before the field existed."""
        self._strip_branch_field(self._claim("sibling"))

        with self._sandbox(mock_restore=False):
            out = self._run()

        self.assertTrue(out.payload["checkout_busy"])
        self.assertEqual(out.payload["held_by"], "sibling")
        self.assertEqual(self._head(), "worker/sibling")
        self.assertEqual(self.f.read_text(encoding="utf-8"), "sibling+uncommitted\n")

    def test_a_recorded_branch_that_does_not_match_head_fails_open(self):
        """The record WINS over the recomputation. This slug would match HEAD if
        recomputed, but its run is on another branch, so it does not own this
        tree. Reading it as 'either one matches' reddens only this test."""
        self._claim("sibling", branch="worker/other-thing")

        with self._sandbox(mock_restore=False):
            out = self._run()

        self.assertNotIn("checkout_busy", out.payload)
        self.assertEqual(self._head(), "main")

    # -- the helper's own contract ----------------------------------------
    def test_current_branch_is_empty_on_detached_head(self):
        """Both 'detached' and 'git failed' collapse to '', which the guard reads
        as 'cannot tell' → proceed."""
        self.assertEqual(stages.current_branch(self.repo), "worker/sibling")
        self._git("checkout", "-f", "--detach", "main")
        self.assertEqual(stages.current_branch(self.repo), "")
        self.assertEqual(stages.current_branch(self.tmp / "not-a-repo"), "")


_CLAUDE_OUTPUT = Path(__file__).resolve().parent / "fixtures" / "claude_output"
# Two live captures and three outcomes derived from them (their provenance says
# which); test_stages.StageUsageFixtureTest pins the captures themselves.
_USAGE_FIXTURES = ("success", "error_max_turns", "error_during_execution",
                   "capacity_limit", "cli_crash")


def _claude_fixture(name: str) -> dict:
    return json.loads((_CLAUDE_OUTPUT / f"{name}.json").read_text(encoding="utf-8"))


class StageUsageLoopTest(unittest.TestCase):
    """The stage loop with token usage wired in (stages.record_stage_usage).

    Each fixture goes through the real loop twice: once instrumented (its argv
    carries --session-id and the transcript is planted in a stand-in
    CLAUDE_CONFIG_DIR) and once as the reference run, with the argv the runner
    built before instrumentation. Everything the loop READS from a stage must come
    out the same both times — run.log, the text handed to the model-cache hook and
    to the capacity check, the ExecOutcome — and stages.json may differ only by
    the additive `usage` key. The fixtures themselves pin that the CLI prints the
    same bytes with and without the session id."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = self.tmp / "claude-config"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plant(self, leg: dict) -> None:
        if leg.get("transcript") is None:
            return
        p = self.cfg / "projects" / "-vault" / f"{leg['session_id']}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(f"{ln}\n" for ln in leg["transcript"]), encoding="utf-8")

    def _run(self, leg: dict | None = None, *, stage: str = "implement",
             timeout_output: str | None = None, real_argv: bool = False,
             patches: tuple = ()) -> dict:
        """One vault-task pass through StagedExecutor.execute. Its own RUNS_DIR
        each time: run ids have one-second resolution, so two passes would share
        a run_dir."""
        runs = Path(tempfile.mkdtemp(dir=self.tmp))
        seen: dict = {}
        cap = mock.Mock(wraps=stages.is_capacity_limit)
        inv = mock.Mock(return_value=False)

        def fake_run(argv, **kw):
            seen["argv"] = list(argv)
            if timeout_output is not None:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout"),
                                                output=timeout_output)
            return subprocess.CompletedProcess(argv, leg["rc"], leg["stdout"], leg["stderr"])

        vp = subprocess.CompletedProcess([], 0, "", "")
        vp.problem = None
        vp.orphaned_commits = None
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="t",
                         payload={"auto_merge": False, "model_tier": "standard"})
        with contextlib.ExitStack() as stack:
            if not real_argv:
                stack.enter_context(mock.patch.object(
                    stages, "build_stage_argv",
                    return_value=["claude", "-p", "x", *leg["argv_tail"]]))
            for p in (
                mock.patch.object(stages, "restore_main"),
                mock.patch.object(stages, "install_push_guard"),
                mock.patch.object(stages, "prepare_vault_branch", return_value=vp),
                mock.patch.object(stages, "RUNS_DIR", runs),
                mock.patch.object(stages, "is_capacity_limit", cap),
                mock.patch.object(stages, "invalidate_model_cache_if_unavailable", inv),
                mock.patch.object(stages, "cleanup_build_daemons"),
                # the per-account env is where the loop gets CLAUDE_CONFIG_DIR from
                mock.patch.object(StagedExecutor, "_account_env",
                                  return_value=({"CLAUDE_CONFIG_DIR": str(self.cfg)}, "acct")),
                mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
                mock.patch.object(StagedExecutor, "_acquire_checkout_lease", return_value=None),
                mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
                mock.patch.object(StagedExecutor, "_pipeline_for", return_value=(stage,)),
                mock.patch("todo_system.worker.runner_heartbeat_start"),
                # both seams, same fake: the stage loop calls the group-reaping
                # helper now, and the subprocess.run patch stays as the sandbox
                mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run),
                mock.patch("subprocess.run", side_effect=fake_run),
                *patches,
            ):
                stack.enter_context(p)
            out = StagedExecutor().execute(msg, _agent(), progress=lambda _: None)
        dirs = list(runs.iterdir())
        self.assertEqual(len(dirs), 1, dirs)
        log = dirs[0] / "run.log"
        return {"out": out, "argv": seen.get("argv"),
                "run_log": log.read_text(encoding="utf-8") if log.exists() else None,
                "progress": json.loads((dirs[0] / stages.STAGE_PROGRESS)
                                       .read_text(encoding="utf-8")),
                "inv": [c.args for c in inv.call_args_list],
                "cap": [c.args for c in cap.call_args_list]}

    def _assert_reads_unchanged(self, what: str, got: dict, ref: dict) -> None:
        """Instrumented vs reference: identical everywhere the loop reads the stage."""
        self.assertEqual(got["run_log"], ref["run_log"], what)
        self.assertEqual(got["inv"], ref["inv"], what)
        self.assertEqual(got["cap"], ref["cap"], what)
        self.assertEqual((got["out"].ok, got["out"].summary),
                         (ref["out"].ok, ref["out"].summary), what)
        drop = lambda d, k: {kk: v for kk, v in d.items() if kk != k}  # noqa: E731
        self.assertEqual(drop(got["out"].payload, "run_dir"),
                         drop(ref["out"].payload, "run_dir"), what)
        self.assertNotIn("usage", ref["progress"], what)
        self.assertEqual(drop(got["progress"], "usage"), ref["progress"], what)

    def test_every_fixture_reads_the_same_with_and_without_the_session_id(self):
        for name in _USAGE_FIXTURES:
            legs = _claude_fixture(name)["legs"]
            self._plant(legs["text_sid"])
            got = self._run(legs["text_sid"])
            ref = self._run(legs["text"])
            self.assertIn(stages.SESSION_ID_FLAG, got["argv"], name)
            self.assertNotIn(stages.SESSION_ID_FLAG, ref["argv"], name)
            self._assert_reads_unchanged(name, got, ref)
            # …and those reads are the CLI's own text, verbatim
            leg = legs["text"]
            self.assertEqual(ref["run_log"],
                             f"\n=== implement (rc={leg['rc']}) ===\n{leg['stdout'][-2000:]}\n",
                             name)
            self.assertEqual(ref["inv"], [(f"{leg['stdout']}\n{leg['stderr']}",)], name)
            self.assertEqual(ref["cap"], [(leg["stdout"],)] if leg["rc"] else [], name)
            self.assertIn("implement", got["progress"]["usage"], name)

    def test_the_capacity_fixture_still_takes_the_capacity_branch(self):
        legs = _claude_fixture("capacity_limit")["legs"]
        self._plant(legs["text_sid"])
        got = self._run(legs["text_sid"])
        self.assertTrue(got["out"].payload.get("capacity_limited"), got["out"].summary)
        self.assertEqual(got["progress"]["usage"],
                         {"implement": {"unavailable": "no usage in transcript (rc=1)"}})

    def test_missing_artifact_is_judged_the_same(self):
        # research's artifact is written by the stage itself; nobody writes it here
        legs = _claude_fixture("success")["legs"]
        self._plant(legs["text_sid"])
        got = self._run(legs["text_sid"], stage="research")
        ref = self._run(legs["text"], stage="research")
        self.assertEqual(ref["out"].summary, "stage research: missing requirements.md")
        self._assert_reads_unchanged("missing artifact", got, ref)
        self.assertEqual(tuple(got["progress"]["usage"]["research"]), stages.USAGE_KEYS)

    def test_success_adds_the_nine_keys_beside_the_existing_ones(self):
        leg = _claude_fixture("success")["legs"]["text_sid"]
        self._plant(leg)
        got = self._run(leg)
        rec = got["progress"]["usage"]["implement"]
        self.assertEqual(tuple(rec), stages.USAGE_KEYS)
        self.assertEqual(rec["session_id"], leg["session_id"])
        self.assertEqual(rec["model"], "claude-haiku-4-5-20251001")
        self.assertEqual(rec["num_turns"], 1)
        self.assertGreater(rec["cache_read_input_tokens"], 0)
        self.assertIsInstance(rec["total_cost_usd"], float)
        self.assertIsInstance(rec["duration_ms"], int)
        self.assertGreaterEqual(rec["duration_ms"], 0)
        self.assertEqual(got["progress"]["model_tier"], "standard")
        self.assertIn("model_tier_reason", got["progress"])
        self.assertEqual(got["progress"]["completed"], ["implement"])

    def test_timeout_is_unavailable_and_the_timeout_path_is_unchanged(self):
        legs = _claude_fixture("success")["legs"]
        self._plant(legs["text_sid"])        # a transcript exists — still unavailable
        for partial in ("", "partial stage output before the kill"):
            got = self._run(legs["text_sid"], timeout_output=partial)
            ref = self._run(legs["text"], timeout_output=partial)
            self._assert_reads_unchanged(f"timeout {partial!r}", got, ref)
            self.assertEqual(got["progress"]["usage"],
                             {"implement": {"unavailable": "timeout"}})
            self.assertTrue(got["out"].payload.get("timed_out"))
            self.assertEqual(got["progress"]["timed_out_stage"], "implement")
            self.assertIn(f"(timeout after 7200s) ===\n{partial}\n", got["run_log"])

    def test_a_crashed_cli_leaves_unavailable(self):
        legs = _claude_fixture("cli_crash")["legs"]
        got = self._run(legs["text_sid"])            # nothing planted: no transcript
        self.assertEqual(got["progress"]["usage"],
                         {"implement": {"unavailable": "cli crash (rc=1): transcript not found"}})

    def test_a_failing_usage_reader_changes_nothing(self):
        legs = _claude_fixture("success")["legs"]
        self._plant(legs["text_sid"])
        ref = self._run(legs["text"])
        for patch, recorded in (
            (mock.patch.object(stages, "stage_usage", side_effect=RuntimeError("boom")),
             {"unavailable": "usage reader error: RuntimeError"}),
            (mock.patch.object(stages, "transcript_usage", side_effect=ValueError("bad")),
             {"unavailable": "usage reader error: ValueError"}),
            (mock.patch.object(stages, "transcript_path", side_effect=PermissionError("no")),
             {"unavailable": "usage reader error: PermissionError"}),
            (mock.patch.object(stages, "record_usage", side_effect=OSError("disk")), None),
        ):
            got = self._run(legs["text_sid"], patches=(patch,))
            self._assert_reads_unchanged(str(recorded), got, ref)
            self.assertEqual(got["progress"].get("usage"),
                             None if recorded is None else {"implement": recorded})

    def test_kill_switch_off_gives_the_old_argv_and_records_nothing(self):
        """AC4 through the real build_stage_argv: MACP_STAGE_USAGE=0 and the argv
        reaching subprocess.run has no session id, stages.json no `usage`."""
        leg = _claude_fixture("success")["legs"]["text"]
        with mock.patch.dict(os.environ, {stages.USAGE_ENV: "0"}):
            off = self._run(leg, real_argv=True)
        with mock.patch.dict(os.environ, {stages.USAGE_ENV: "1"}):
            on = self._run(leg, real_argv=True)
        self.assertNotIn(stages.SESSION_ID_FLAG, off["argv"])
        self.assertNotIn("usage", off["progress"])
        i = on["argv"].index(stages.SESSION_ID_FLAG)
        # argv[2] is the prompt, which names each run's own run_id
        self.assertEqual(on["argv"][3:i] + on["argv"][i + 2:], off["argv"][3:])
        # a real, fresh id with no transcript behind it here
        self.assertEqual(on["progress"]["usage"],
                         {"implement": {"unavailable": "transcript not found"}})
        self._assert_reads_unchanged("kill switch", on, off)


class _GatedExecutor:
    """Blocks every run until released, recording how many ran at once."""

    mode = "fake"

    def __init__(self):
        import threading
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.started: list[str] = []
        self.active = 0
        self.peak = 0

    def execute(self, msg, agent, progress, **_):
        with self._lock:
            self.started.append(msg.task_id)
            self.active += 1
            self.peak = max(self.peak, self.active)
        self.release.wait(5)
        with self._lock:
            self.active -= 1
        return ExecOutcome(True, "gated ok", {})


class RunnerParallelTest(unittest.TestCase):
    """run_forever with concurrency > 1: runs share nothing but the mailbox."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.agent = _agent()
        self.ex = _GatedExecutor()
        self.runner = Runner(self.mb, self.agent, self.ex, log=lambda *_: None)
        self._hb = mock.patch.multiple("todo_system.worker",
                                       runner_heartbeat_start=mock.DEFAULT,
                                       runner_heartbeat_clear=mock.DEFAULT)
        self.hb = self._hb.start()
        self._mem = mock.patch.object(runner_mod, "_mem_available_mb", return_value=None)
        self._mem.start()

    def tearDown(self):
        self.ex.release.set()
        self._wait_idle()
        self._mem.stop()
        self._hb.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _wait_idle(self, timeout=5.0):
        import time as _t
        end = _t.time() + timeout
        while self.runner._inflight and _t.time() < end:
            _t.sleep(0.01)

    def _wait_started(self, n, timeout=5.0):
        import time as _t
        end = _t.time() + timeout
        while len(self.ex.started) < n and _t.time() < end:
            _t.sleep(0.01)

    def _assign(self, task_id, repo=None):
        payload = {"task_path": f"Todo/items/{task_id}.md"}
        if repo:
            payload["repo"] = repo
        msg = m.Envelope(to=self.agent.id, type=m.TASK_ASSIGN, task_id=task_id,
                         payload=payload)
        self.mb.send(msg)
        return msg

    def test_runs_up_to_slots_at_once_and_queues_the_rest(self):
        for i in range(3):
            self._assign(f"t{i}", repo=f"https://example.com/r{i}.git")
        self.assertEqual(self.runner.poll_parallel(2), 2)
        self._wait_started(2)
        self.assertEqual(self.ex.peak, 2)
        # Re-polling while both run must not re-start them or the third.
        self.assertEqual(self.runner.poll_parallel(2), 0)
        self.assertEqual(sorted(self.ex.started), ["t0", "t1"])
        self.ex.release.set()
        self._wait_idle()
        self.assertEqual(self.runner.poll_parallel(2), 1)
        self._wait_idle()
        self.assertEqual(sorted(self.ex.started), ["t0", "t1", "t2"])
        self.assertEqual(self.mb.receive(self.agent.id), [])   # all acked

    def test_vault_runs_stay_serial_same_repo_parallelizes(self):
        # Same repo, different slugs -> distinct per-run worktree keys -> both run.
        # Two vault runs share the single "<vault>" checkout -> still serial.
        self._assign("a", repo="https://example.com/same.git")
        self._assign("b", repo="https://example.com/same.git")
        self._assign("c")                      # vault
        self._assign("d")                      # vault again
        # slots=4 so d is held back by the "<vault>" key, not the slot cap.
        self.assertEqual(self.runner.poll_parallel(4), 3)
        self._wait_started(3)
        self.assertEqual(sorted(self.ex.started), ["a", "b", "c"])

    def test_same_repo_different_slug_both_busy_and_dispatched(self):
        # AC regression: same repo + different slug -> two distinct per-run
        # worktree keys -> both held in-flight and both dispatched. leaf 5 raised
        # the dispatch cap; this run stops the runner re-serializing same-repo
        # runs, so this is the wall-clock-parallel enabler. (_mem_available_mb is
        # mocked None in setUp, so the second run's memory guard fails open.)
        self._assign("a", repo="https://example.com/same.git")
        self._assign("b", repo="https://example.com/same.git")
        self.assertEqual(self.runner.poll_parallel(2), 2)
        self._wait_started(2)
        self.assertEqual(sorted(self.ex.started), ["a", "b"])
        self.assertEqual(len(self.runner._inflight), 2)
        # Two distinct in-flight keys == per-run-worktree keying (not repo-URL,
        # which would have collapsed both onto one key and serialized them).
        self.assertEqual(len(set(self.runner._inflight.values())), 2)

    def test_extra_slot_waits_for_memory(self):
        self._assign("x", repo="https://example.com/x.git")
        self._assign("y", repo="https://example.com/y.git")
        with mock.patch.object(runner_mod, "_mem_available_mb", return_value=500.0):
            # the first run always goes; only the extra one needs headroom
            self.assertEqual(self.runner.poll_parallel(3), 1)
        self.assertEqual(self.runner.poll_parallel(3), 1)

    def test_heartbeat_held_until_the_last_run_ends(self):
        self._assign("p", repo="https://example.com/p.git")
        self._assign("q", repo="https://example.com/q.git")
        self.runner.poll_parallel(2)
        self._wait_started(2)
        self.hb["runner_heartbeat_clear"].assert_not_called()
        self.ex.release.set()
        self._wait_idle()
        self.assertEqual(self.hb["runner_heartbeat_clear"].call_count, 1)

    def test_max_parallel_from_env_or_registry(self):
        self.assertEqual(self.runner._max_parallel(), 1)   # _agent() default
        self.runner.agent = _agent(concurrency=3)
        self.assertEqual(self.runner._max_parallel(), 3)
        with mock.patch.dict(os.environ, {"RUNNER_MAX_PARALLEL": "1"}):
            self.assertEqual(self.runner._max_parallel(), 1)

    def test_per_account_max_env_else_registry(self):
        self.assertEqual(self.runner._per_account_max(), 1)   # _agent() concurrency
        self.runner.agent = _agent(concurrency=4)
        self.assertEqual(self.runner._per_account_max(), 4)   # registry default
        with mock.patch.dict(os.environ, {"RUNNER_MAX_PARALLEL_PER_ACCOUNT": "2"}):
            self.assertEqual(self.runner._per_account_max(), 2)
        with mock.patch.dict(os.environ, {"RUNNER_MAX_PARALLEL_PER_ACCOUNT": "x"}):
            self.assertEqual(self.runner._per_account_max(), 4)   # bad env → registry

    def test_global_cap_only_when_env_set(self):
        os.environ.pop("RUNNER_MAX_PARALLEL", None)
        self.assertIsNone(self.runner._global_cap())              # unset → no cut
        with mock.patch.dict(os.environ, {"RUNNER_MAX_PARALLEL": "5"}):
            self.assertEqual(self.runner._global_cap(), 5)
        with mock.patch.dict(os.environ, {"RUNNER_MAX_PARALLEL": "oops"}):
            self.assertIsNone(self.runner._global_cap())          # unparseable → None

    def test_account_slots_passes_caps_and_fails_soft(self):
        self.runner.agent = _agent(concurrency=4)
        os.environ.pop("RUNNER_MAX_PARALLEL", None)
        os.environ.pop("RUNNER_MAX_PARALLEL_PER_ACCOUNT", None)
        seen = {}
        cap = {"total": 6, "per_account": {"a": 3, "b": 3}, "slack": {},
               "order": ["a", "b"], "active": "a"}

        def fake(per_account_max, global_cap, *a, **k):
            seen["args"] = (per_account_max, global_cap)
            return cap

        with mock.patch("todo_system.usage_guard.account_slots", side_effect=fake):
            self.assertEqual(self.runner._account_slots(), cap)
        self.assertEqual(seen["args"], (4, None))   # per_account_max=concurrency, cap None
        # a guard error never kills the poll — empty cap (total 0 → the hold branch)
        with mock.patch("todo_system.usage_guard.account_slots",
                        side_effect=RuntimeError("boom")):
            self.assertEqual(self.runner._account_slots()["total"], 0)

    def test_assign_account_pin_then_slack_order(self):
        cap = {"total": 4, "per_account": {"school": 2, "dev": 2}, "slack": {},
               "order": ["dev", "school"], "active": "school"}
        with mock.patch("todo_system.macp.resolve", return_value={"project": None}):
            # no pin → first in order (widest slack) with a free per-account slot
            self.assertEqual(self.runner._assign_account({}, "t", cap, {}), "dev")
            # dev at its cap → spill to school
            self.assertEqual(self.runner._assign_account({}, "t", cap, {"dev": 2}), "school")
            # all accounts at cap → None (the caller skips this envelope)
            self.assertIsNone(self.runner._assign_account({}, "t", cap, {"dev": 2, "school": 2}))
        # a project pin wins over order AND ignores the per-account count
        with mock.patch("todo_system.macp.resolve",
                        return_value={"project": Namespace(account="school")}):
            self.assertEqual(self.runner._assign_account({}, "t", cap, {"school": 9}), "school")
        # a pin naming an account the cap does not know (blocked) → fall to order
        with mock.patch("todo_system.macp.resolve",
                        return_value={"project": Namespace(account="ghost")}):
            self.assertEqual(self.runner._assign_account({}, "t", cap, {}), "dev")
        # no account model in the cap → None (poll_parallel then gates nothing)
        self.assertIsNone(self.runner._assign_account(
            {}, "t", {"per_account": {}, "order": []}, {}))

    def test_poll_parallel_assigns_accounts_and_records_them(self):
        self._assign("a", repo="https://example.com/a.git")
        self._assign("b", repo="https://example.com/b.git")
        cap = {"total": 2, "per_account": {"school": 1, "dev": 1}, "slack": {},
               "order": ["school", "dev"], "active": "school"}
        self.assertEqual(self.runner.poll_parallel(4, cap=cap), 2)
        self._wait_started(2)
        # school's one slot fills first, then dev — two distinct accounts recorded
        self.assertEqual(sorted(v for v in self.runner._inflight_acct.values() if v),
                         ["dev", "school"])

    def test_poll_parallel_skips_when_the_only_account_is_full(self):
        self._assign("a", repo="https://example.com/a.git")
        self._assign("b", repo="https://example.com/b.git")
        cap = {"total": 1, "per_account": {"school": 1}, "slack": {},
               "order": ["school"], "active": "school"}
        self.assertEqual(self.runner.poll_parallel(4, cap=cap), 1)   # only one school slot
        self._wait_started(1)
        self.assertEqual(self.runner.poll_parallel(4, cap=cap), 0)   # b cannot get a slot
        self.assertEqual(self.ex.started, ["a"])

    def test_poll_parallel_no_account_model_leaves_inflight_acct_empty(self):
        self._assign("a", repo="https://example.com/a.git")
        self._assign("b", repo="https://example.com/b.git")
        empty = {"total": 2, "per_account": {}, "slack": {}, "order": [], "active": None}
        self.assertEqual(self.runner.poll_parallel(2, cap=empty), 2)   # unchanged dispatch
        self._wait_started(2)
        self.assertEqual(self.runner._inflight_acct, {})               # no assignment made


class RunnerInboxPriorityOrderTest(unittest.TestCase):
    """poll_parallel fills its few free slots highest-priority-first, not purely
    in inbox arrival (ULID) order: a late high-priority TASK_ASSIGN starts ahead
    of earlier low/none-priority ones, while priority-less envelopes keep arrival
    order (macp — 러너 inbox를 priority 순으로 집기)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.agent = _agent()
        self.ex = _GatedExecutor()
        self.runner = Runner(self.mb, self.agent, self.ex, log=lambda *_: None)
        self._hb = mock.patch.multiple("todo_system.worker",
                                       runner_heartbeat_start=mock.DEFAULT,
                                       runner_heartbeat_clear=mock.DEFAULT)
        self._hb.start()
        self._mem = mock.patch.object(runner_mod, "_mem_available_mb", return_value=None)
        self._mem.start()

    def tearDown(self):
        self.ex.release.set()
        self._wait_idle()
        self._mem.stop()
        self._hb.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _wait_idle(self, timeout=5.0):
        import time as _t
        end = _t.time() + timeout
        while self.runner._inflight and _t.time() < end:
            _t.sleep(0.01)

    def _wait_started(self, n, timeout=5.0):
        import time as _t
        end = _t.time() + timeout
        while len(self.ex.started) < n and _t.time() < end:
            _t.sleep(0.01)

    def _assign(self, task_id, priority, repo):
        # Distinct repo per msg → distinct per-run worktree keys (no collision).
        # send order == ULID order == inbox arrival order. priority None is sent
        # as an OLD envelope: no `priority` key at all (the pre-change dispatch).
        payload = {"task_path": f"Todo/items/{task_id}.md", "repo": repo}
        if priority is not None:
            payload["priority"] = priority
        self.mb.send(m.Envelope(to=self.agent.id, type=m.TASK_ASSIGN,
                                task_id=task_id, payload=payload))

    def test_late_high_priority_envelope_starts_before_earlier_low_ones(self):
        # The acceptance criterion: arrival (ULID) order 40, None, 95 into ONE
        # free slot. FIFO starts the first-arrived (40); priority ordering starts
        # the 95. slots=1 → exactly one run starts and the loop breaks on
        # running>=slots, so the single-element ex.started is deterministic.
        self._assign("t-low", 40, "https://example.com/low.git")
        self._assign("t-none", None, "https://example.com/none.git")
        self._assign("t-urgent", 95, "https://example.com/urgent.git")
        self.assertEqual(self.runner.poll_parallel(1), 1)
        self._wait_started(1)
        self.assertEqual(self.ex.started, ["t-urgent"])

    def test_by_priority_desc_null_last_stable_within_each_group(self):
        def env(task_id, priority, *, has_key=True):
            payload = {"priority": priority} if has_key else {}
            return m.Envelope(to="x", type=m.TASK_ASSIGN, task_id=task_id,
                              payload=payload)
        # Arrival a=40, b=(no priority key), c=95, d=40, e=None. Expect 95 first;
        # the two 40s keep arrival order a,d; the priority-less b and the
        # explicit-None e stay last in arrival order b,e — proving priority-desc,
        # NULL-last, AND "priority 없는 옛 envelope은 도착 순 유지".
        msgs = [env("a", 40), env("b", None, has_key=False), env("c", 95),
                env("d", 40), env("e", None)]
        out = runner_mod._by_priority(msgs)
        self.assertEqual([x.task_id for x in out], ["c", "a", "d", "b", "e"])
        # Stable sort must not mutate its input.
        self.assertEqual([x.task_id for x in msgs], ["a", "b", "c", "d", "e"])

    def test_bool_priority_is_treated_as_none(self):
        def env(task_id, priority):
            return m.Envelope(to="x", type=m.TASK_ASSIGN, task_id=task_id,
                              payload={"priority": priority})
        out = runner_mod._by_priority([env("real", 10), env("tt", True), env("ff", False)])
        self.assertEqual([x.task_id for x in out], ["real", "tt", "ff"])
        self.assertIsNone(runner_mod._envelope_priority(env("b", True)))
        self.assertIsNone(runner_mod._envelope_priority(env("s", "high")))
        self.assertEqual(runner_mod._envelope_priority(env("r", 7)), 7)


class NewIdThreadSafetyTest(unittest.TestCase):
    def test_no_duplicate_ids_across_threads(self):
        import threading
        out: list[str] = []
        lock = threading.Lock()

        def mint():
            ids = [m.new_id() for _ in range(2000)]
            with lock:
                out.extend(ids)

        ts = [threading.Thread(target=mint) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(out), len(set(out)))


class CodeChangeTimeTest(unittest.TestCase):
    """`worker.code_change_time()` is the runner's read of the one code-timestamp
    rule (scripts/health_report.py). It must fail OPEN — a judgment that cannot be
    made has to look like "no change", because a guard that raises here would take
    the whole queue down with it."""

    def test_none_when_health_report_will_not_load(self):
        with mock.patch.object(_worker, "_health_report", return_value=None):
            self.assertIsNone(_worker.code_change_time())

    def test_none_when_the_rule_itself_raises(self):
        hr = mock.Mock()
        hr.gather_code_time.side_effect = RuntimeError("no git here")
        with mock.patch.object(_worker, "_health_report", return_value=hr):
            self.assertIsNone(_worker.code_change_time())

    def test_none_when_the_timestamp_is_unreadable(self):
        hr = mock.Mock()
        hr.gather_code_time.return_value = None
        with mock.patch.object(_worker, "_health_report", return_value=hr):
            self.assertIsNone(_worker.code_change_time())

    def test_epoch_seconds_from_the_rules_datetime(self):
        import datetime as _dt
        when = _dt.datetime(2026, 9, 26, 0, 0, tzinfo=_dt.timezone.utc)
        hr = mock.Mock()
        hr.gather_code_time.return_value = when
        with mock.patch.object(_worker, "_health_report", return_value=hr):
            self.assertEqual(_worker.code_change_time(), when.timestamp())


class _SeqCodeTime:
    """A `code_time_fn` that answers a scripted sequence; the last value repeats.
    An Exception instance in the sequence is raised instead of returned. No git,
    no subprocess — a drain is provoked without making a real commit."""

    def __init__(self, *values):
        self.values = list(values)
        self.calls = 0

    def __call__(self):
        v = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        if isinstance(v, Exception):
            raise v
        return v


class RunnerDrainTest(unittest.TestCase):
    """The resident runner holds the code it booted with, and `poll_once` used to
    run a whole `receive()` batch back to back — so while the queue stayed full,
    `worker._runner_idle` never saw an idle moment and `restart_stale_daemons`
    deferred the restart forever (10 consecutive deferrals, 2026-09-25). The fix
    is the runner draining itself between envelopes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.agent = _agent()
        self.ex = _CountingExecutor()
        self._hb = mock.patch.multiple("todo_system.worker",
                                       runner_heartbeat_start=mock.DEFAULT,
                                       runner_heartbeat_clear=mock.DEFAULT)
        self._hb.start()

    def tearDown(self):
        self._hb.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _runner(self, code_time_fn):
        return Runner(self.mb, self.agent, self.ex, log=lambda *_: None,
                      code_time_fn=code_time_fn)

    def _assign(self, task_id) -> m.Envelope:
        msg = m.Envelope(to=self.agent.id, type=m.TASK_ASSIGN, task_id=task_id,
                         payload={"task_path": f"Todo/items/{task_id}.md"})
        self.mb.send(msg)
        return msg

    def test_batch_drains_after_current_message_when_code_changes(self):
        for i in range(3):
            self._assign(f"t{i}")
        # call 1 = baseline (the commit this process booted on), call 2 = a newer
        # commit landed while the first envelope was running.
        runner = self._runner(_SeqCodeTime(100.0, 200.0))

        self.assertEqual(runner.poll_once(), 1)

        # the envelope in hand ran to completion — drain never interrupts work
        self.assertEqual(self.ex.calls, ["t0"])
        self.assertTrue(runner._drain_requested)
        # …and the rest are still queued, un-acked, for the process that replaces us
        self.assertEqual([e.task_id for e in self.mb.receive(self.agent.id)],
                         ["t1", "t2"])
        # the one that DID run was acked and its claim closed: a drain is a clean
        # stop between envelopes, not a crash in the middle of one.
        self.assertEqual([r.type for r in self.mb.receive(m.ORCHESTRATOR)],
                         [m.ACCEPT, m.RESULT])

    def test_the_flag_is_sticky_and_costs_no_further_lookups(self):
        for i in range(2):
            self._assign(f"t{i}")
        seq = _SeqCodeTime(100.0, 200.0)
        runner = self._runner(seq)
        runner.poll_once()
        self.assertEqual(seq.calls, 2)          # baseline + the one that tripped it

        self.assertTrue(runner._code_changed())
        self.assertEqual(seq.calls, 2)          # sticky: no third git read

    # -- the other direction: nothing changed, nothing may change -----------
    def test_no_drain_when_code_unchanged(self):
        for i in range(3):
            self._assign(f"t{i}")
        runner = self._runner(_SeqCodeTime(100.0))

        self.assertEqual(runner.poll_once(), 3)

        self.assertEqual(self.ex.calls, ["t0", "t1", "t2"])   # batch runs straight through
        self.assertFalse(runner._drain_requested)
        self.assertEqual(self.mb.receive(self.agent.id), [])  # all acked

    def test_no_drain_when_the_timestamp_is_unreadable(self):
        """Fail open: a host that cannot answer the question keeps the old
        behaviour. Draining on doubt would stop the queue on every such host."""
        for i in range(3):
            self._assign(f"t{i}")
        runner = self._runner(_SeqCodeTime(None))

        self.assertEqual(runner.poll_once(), 3)

        self.assertEqual(self.ex.calls, ["t0", "t1", "t2"])
        self.assertFalse(runner._drain_requested)

    def test_no_drain_and_no_crash_when_the_check_raises(self):
        for i in range(3):
            self._assign(f"t{i}")
        runner = self._runner(_SeqCodeTime(RuntimeError("git exploded")))

        self.assertEqual(runner.poll_once(), 3)     # the loop survives it

        self.assertEqual(self.ex.calls, ["t0", "t1", "t2"])
        self.assertFalse(runner._drain_requested)

    def test_an_older_timestamp_is_not_a_change(self):
        """Strictly `>`. A HEAD that moved backwards (a restored main after a
        worker branch) must not read as new code, or restarts would ping-pong."""
        for i in range(3):
            self._assign(f"t{i}")
        runner = self._runner(_SeqCodeTime(200.0, 100.0))

        self.assertEqual(runner.poll_once(), 3)

        self.assertFalse(runner._drain_requested)

    def test_a_single_message_poll_never_looks_the_code_up(self):
        """The common case is one envelope per poll every 15s; with nothing left
        to hand over there is nobody to drain for, so it must cost no subprocess."""
        self._assign("t0")
        seq = _SeqCodeTime(100.0)
        runner = self._runner(seq)

        self.assertEqual(runner.poll_once(), 1)

        self.assertEqual(seq.calls, 0)

    def test_a_runner_built_without_init_is_inert_not_broken(self):
        """`Runner.__new__` + a few hand-set fields is how other suites drive
        poll_once. The guard must answer "no change" there rather than raise: a
        fail-open check that can itself crash the poll path is not fail-open."""
        import threading as _threading
        r = Runner.__new__(Runner)
        r.mailbox, r.agent = self.mb, self.agent
        r.claims = claims.ClaimStore(self.tmp, self.agent.id)
        r._lock, r._inflight, r._busy = _threading.RLock(), {}, 0
        r.log = lambda *_: None
        r.handle = lambda msg: None
        for i in range(2):
            self._assign(f"t{i}")

        self.assertEqual(r.poll_once(), 2)      # both ran, nothing raised
        self.assertFalse(r._code_changed())


class RunnerParallelDrainTest(unittest.TestCase):
    """Parallel mode drains the same way, but an in-flight run is a live pipeline
    on a thread: the loop stops opening slots and then WAITS. Nothing here may
    interrupt work that is already running."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.agent = _agent()
        self.ex = _GatedExecutor()
        self._hb = mock.patch.multiple("todo_system.worker",
                                       runner_heartbeat_start=mock.DEFAULT,
                                       runner_heartbeat_clear=mock.DEFAULT)
        self._hb.start()
        self._mem = mock.patch.object(runner_mod, "_mem_available_mb", return_value=None)
        self._mem.start()
        self.seq = _SeqCodeTime(100.0)
        self.runner = Runner(self.mb, self.agent, self.ex, log=lambda *_: None,
                             code_time_fn=self.seq)

    def tearDown(self):
        self.ex.release.set()
        self._wait_idle()
        self._mem.stop()
        self._hb.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _wait_idle(self, timeout=5.0):
        import time as _t
        end = _t.time() + timeout
        while self.runner._inflight and _t.time() < end:
            _t.sleep(0.01)

    def _wait_started(self, n, timeout=5.0):
        import time as _t
        end = _t.time() + timeout
        while len(self.ex.started) < n and _t.time() < end:
            _t.sleep(0.01)

    def _assign(self, task_id, repo=None):
        payload = {"task_path": f"Todo/items/{task_id}.md"}
        if repo:
            payload["repo"] = repo
        msg = m.Envelope(to=self.agent.id, type=m.TASK_ASSIGN, task_id=task_id,
                         payload=payload)
        self.mb.send(msg)
        return msg

    def test_no_new_slot_opened_when_code_changed(self):
        for i in range(3):
            self._assign(f"t{i}", repo=f"https://example.com/r{i}.git")
        self.assertEqual(self.runner.poll_parallel(2), 2)
        self._wait_started(2)

        # a merge lands; the next poll must open nothing, even though a slot frees
        self.seq.values = [200.0]
        self.runner._code_checked_at = None          # TTL would hide it otherwise
        self.ex.release.set()
        self._wait_idle()

        self.assertEqual(self.runner.poll_parallel(2), 0)
        self.assertTrue(self.runner._drain_requested)
        self.assertEqual(sorted(self.ex.started), ["t0", "t1"])
        # the third is still queued and un-acked for the next process
        self.assertEqual([e.task_id for e in self.mb.receive(self.agent.id)], ["t2"])

    def test_the_check_runs_even_when_every_slot_is_full(self):
        """A permanently full queue is the defect being fixed, so the flag must be
        able to go up while `running >= slots` — i.e. BEFORE that break."""
        for i in range(2):
            self._assign(f"t{i}", repo=f"https://example.com/r{i}.git")
        self.assertEqual(self.runner.poll_parallel(1), 1)
        self._wait_started(1)

        self.seq.values = [200.0]
        self.runner._code_checked_at = None
        self.runner.poll_parallel(1)              # slot is full; check still runs

        self.assertTrue(self.runner._drain_requested)
        self.assertEqual(self.ex.started, ["t0"])

    def test_run_forever_exits_only_after_inflight_finish(self):
        import threading
        import time as _t
        self._assign("t0", repo="https://example.com/r0.git")
        self.runner.poll_parallel(1)
        self._wait_started(1)
        self.runner._drain_requested = True       # a merge landed

        t = threading.Thread(target=self.runner.run_forever,
                             kwargs={"interval": 0.01}, daemon=True)
        t.start()
        _t.sleep(0.3)
        # still running: the loop waits for the pipeline, it never cuts it short
        self.assertTrue(t.is_alive())
        self.assertEqual(self.ex.active, 1)

        self.ex.release.set()                     # the run finishes on its own
        t.join(timeout=5.0)
        self.assertFalse(t.is_alive())            # …and only then do we exit
        self.assertEqual(self.ex.started, ["t0"])

    def test_run_forever_claims_nothing_new_while_draining(self):
        import threading
        self._assign("t0")
        self.runner._drain_requested = True

        t = threading.Thread(target=self.runner.run_forever,
                             kwargs={"interval": 0.01}, daemon=True)
        t.start()
        t.join(timeout=5.0)

        self.assertFalse(t.is_alive())
        self.assertEqual(self.ex.started, [])     # never claimed
        self.assertEqual([e.task_id for e in self.mb.receive(self.agent.id)], ["t0"])


if __name__ == "__main__":
    unittest.main()
