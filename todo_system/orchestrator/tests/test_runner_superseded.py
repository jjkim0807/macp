"""Runner side of the superseded verdict (superseded.py): a run in flight stops
at the next stage boundary once its task is over.

These drive the REAL stage loop: `StagedExecutor._pipeline_for` and
`ClaudeAgentBackend.run_stage` are not patched, so the five stages of
`STAGES_AUTO` are walked by production code and only the stage subprocess
itself is a fake (it records its name, writes that stage's artifact and can
close the task md mid-run, the way a person does). Git plumbing around the loop
is mocked like the sibling runner suites; the squash-merge cases use a real
bare origin + clones.

Every "stops early" test has a control on the same harness in which the md
stays open and all five stages run. Without it, a harness that could never get
past `implement` would make the early stop look like a pass.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import message as m
from todo_system.orchestrator import repos, stages
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator import superseded as sup
from todo_system.orchestrator.assignment import Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import ALERT_RESULT_NO_PR, Orchestrator, Task
from todo_system.orchestrator.registry import Agent, Registry
from todo_system.orchestrator.runner import Runner, StagedExecutor
from todo_system.orchestrator.tests.test_superseded import BRANCH, _GitFixture

SLUG = "t"                       # branch_for() -> worker/t == the git fixture's BRANCH
TASK_PATH = "Todo/items/t.md"    # vault-relative, as tasks.load_dispatchable writes it
REPO_URL = "git@example.invalid:x/superseded-fixture.git"
ALL_STAGES = list(stages.STAGES_AUTO)

_ARTIFACTS = {
    "research": ("requirements.md", "# requirements\n"),
    "plan": ("implementation_plan.md", "---\ndeclares_oversize: no\n---\n# plan\n"),
    "verify": ("verify.md", "---\noverall_status: pass\n---\n"),
    "report": ("report.md", "---\noverall_status: pass\npr_url: null\n---\n"),
    "delegate": ("delegate.md", "# delegate\n"),
}


def _md(status: str) -> str:
    return "\n".join([
        "---", "tags:", "  - todo", f"status: {status}", "updated: 2026-01-01",
        "---", "", "# t", "", "- [ ] step", ""])


def _blank_run(argv, **kw):
    """What the sibling suites give every stray `subprocess.run`: rc 0, no output."""
    return subprocess.CompletedProcess(argv, 0, "", "")


def _agent(**kw) -> Agent:
    base = dict(id="w", kind="ai-local", projects=[SLUG])
    base.update(kw)
    return Agent(**base)


class _HarnessMixin:
    """The patch bundle of test_runner.ImplementCheckpointTest._run, minus the
    pipeline patch, plus a temp vault so `task_path` resolves to a real md."""

    def _setup_harness(self, clone: Path | None = None) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.vault = self.tmp / "vault"
        self.items = self.vault / "Todo" / "items"
        self.items.mkdir(parents=True)
        self.md = self.items / "t.md"
        self.md.write_text(_md("open"), encoding="utf-8")
        self.runs = self.vault / "Worker" / "runs"
        self.runs.mkdir(parents=True)
        # Not created unless a test passes a real clone: the git probe then
        # stops at "not a checkout" without spawning anything.
        self.run_clone = clone if clone is not None else self.tmp / "clone"
        self.ran: list[str] = []
        self.on_stage: dict = {}
        self.progress: list[str] = []
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(sup.KILL_ENV, None)
        # No usage pause in the loop (the wait returns at once, unpatched).
        os.environ[runner_mod.STAGE_GATE_ENV] = "0"

    # -- the fake stage -----------------------------------------------------
    def _run_dir(self) -> Path:
        dirs = [d for d in self.runs.iterdir() if d.is_dir()]
        self.assertEqual(len(dirs), 1, dirs)
        return dirs[0]

    def _fake_stage(self, argv, **kw):
        stage = argv[1]
        self.ran.append(stage)
        artifact = _ARTIFACTS.get(stage)
        if artifact:
            (self._run_dir() / artifact[0]).write_text(artifact[1], encoding="utf-8")
        hook = self.on_stage.get(stage)
        if hook is not None:
            hook()
        return subprocess.CompletedProcess(argv, 0, f"{stage} complete\n", "")

    def _close_md(self, status: str = "done"):
        return lambda: self.md.write_text(_md(status), encoding="utf-8")

    # -- patches ------------------------------------------------------------
    @contextlib.contextmanager
    def _patched(self, inherited: tuple = ()):
        """`inherited`: stages a prior run already finished (a resumed run)."""
        vp = subprocess.CompletedProcess([], 0, "", "")
        vp.problem = None
        vp.orphaned_commits = None
        mk = types.SimpleNamespace(order=[])

        def rescue(repo_dir, branch, slug, progress=None):
            mk.order.append("rescue")
            return {"committed": False, "sha": None, "note": "nothing to rescue"}

        def checkpoint(repo_dir, branch, progress=None):
            mk.order.append("checkpoint")
            return {"pushed": True, "tip": "abc1234", "commits": 1,
                    "note": "checkpoint pushed 1 commit(s) to origin/worker/t"}

        with contextlib.ExitStack() as stack:
            enter = stack.enter_context
            enter(mock.patch.object(repos, "ensure_worktree", return_value=self.run_clone))
            mk.restore_main = enter(mock.patch.object(stages, "restore_main"))
            enter(mock.patch.object(stages, "install_push_guard"))
            enter(mock.patch.object(stages, "default_branch", return_value="main"))
            enter(mock.patch.object(
                stages, "prepare_external_branch",
                return_value=subprocess.CompletedProcess([], 0, "", "")))
            enter(mock.patch.object(stages, "prepare_vault_branch", return_value=vp))
            enter(mock.patch.object(stages, "remote_branch_exists", return_value=True))
            enter(mock.patch.object(
                stages, "resumable_run",
                return_value=((self.tmp / "prior-run", list(inherited))
                              if inherited else None)))
            enter(mock.patch.object(stages, "inherit_artifacts",
                                    return_value=list(inherited)))
            enter(mock.patch.object(stages, "record_related", return_value=None))
            enter(mock.patch.object(stages, "RUNS_DIR", self.runs))
            enter(mock.patch.object(stages, "_VAULT_DATA_ROOT", self.vault))
            enter(mock.patch.object(
                stages, "build_stage_argv",
                side_effect=lambda stage, **kw: ["fake-stage", stage]))
            enter(mock.patch.object(stages, "run_stage_subprocess",
                                    side_effect=self._fake_stage))
            mk.rescue = enter(mock.patch.object(stages, "rescue_uncommitted",
                                                side_effect=rescue))
            mk.checkpoint = enter(mock.patch.object(stages, "checkpoint_push",
                                                    side_effect=checkpoint))
            enter(mock.patch.object(stages, "cleanup_build_daemons"))
            mk.remove_worktree = enter(mock.patch.object(stages, "remove_worktree"))
            mk.publish = enter(mock.patch.object(
                stages, "publish_branch", return_value={"note": "", "pr_url": None}))
            mk.merge_pr = enter(mock.patch.object(stages, "merge_pr"))
            enter(mock.patch.object(StagedExecutor, "_account_env",
                                    return_value=(None, "")))
            enter(mock.patch.object(StagedExecutor, "_live_sibling_hold",
                                    return_value=None))
            enter(mock.patch.object(StagedExecutor, "_acquire_checkout_lease",
                                    return_value=None))
            enter(mock.patch.object(runner_mod.disk_guard, "ensure_free_space",
                                    return_value={}))
            enter(mock.patch("todo_system.worker.runner_heartbeat_start"))
            mk.kakao = enter(mock.patch("todo_system.worker.kakao_notify"))
            enter(mock.patch.object(runner_mod.followups, "append_followups"))
            enter(mock.patch("subprocess.run", side_effect=_blank_run))
            yield mk

    def _payload(self, **kw) -> dict:
        base = {"repo": REPO_URL, "auto_merge": False, "model_tier": "heavy",
                "task_path": TASK_PATH}
        base.update(kw)
        return {k: v for k, v in base.items() if v is not None}

    def _run(self, payload: dict | None = None, agent=None, inherited: tuple = ()):
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id=SLUG,
                         payload=self._payload() if payload is None else payload)
        with self._patched(inherited) as mk:
            out = StagedExecutor().execute(
                msg, _agent() if agent is None else agent,
                progress=self.progress.append)
        return out, mk

    # -- shared assertions ----------------------------------------------------
    def _assert_ran_to_the_end(self, out, mk):
        self.assertEqual(self.ran, ALL_STAGES)
        self.assertTrue(out.ok, out.summary)
        self.assertNotIn(sup.RESULT_KEY, out.payload)
        self.assertEqual(mk.order, ["rescue", "checkpoint"])
        self.assertNotIn("superseded", json.loads(
            (self._run_dir() / "stages.json").read_text(encoding="utf-8")))

    def _assert_stopped(self, out, mk, *, reason, ran, where):
        self.assertEqual(self.ran, ran)
        self.assertTrue(out.ok, out.summary)           # RESULT, not FAIL
        self.assertEqual(out.payload[sup.RESULT_KEY], reason)
        self.assertIs(out.payload["review_required"], False)
        self.assertEqual(out.payload["stages"], ran)
        self.assertEqual(out.payload["branch"], BRANCH)
        self.assertNotIn("cancelled", out.payload)
        self.assertIn("nothing pushed", out.summary)
        self.assertIn(where, out.summary)
        mk.checkpoint.assert_not_called()               # nothing pushed
        mk.publish.assert_not_called()
        mk.merge_pr.assert_not_called()
        mk.remove_worktree.assert_not_called()          # kept; the sweep reclaims it
        mk.kakao.assert_not_called()                    # nobody paged
        mk.restore_main.assert_called()                 # the vault still goes back to main
        record = json.loads(
            (self._run_dir() / "stages.json").read_text(encoding="utf-8"))
        self.assertEqual(record["superseded"]["reason"], reason)
        self.assertIn(where, record["superseded"]["where"])
        # (stages.json has no `completed` key until a first stage has finished)
        self.assertEqual(record.get("completed", []), ran)
        log = (self._run_dir() / "run.log").read_text(encoding="utf-8")
        self.assertIn("=== superseded (", log)
        self.assertEqual(len([p for p in self.progress if "superseded" in p]), 1,
                         self.progress)


class _Harness(_HarnessMixin, unittest.TestCase):
    def setUp(self):
        self._setup_harness()


class MdClosedMidRunTest(_Harness):
    def test_control_open_md_runs_all_five_stages(self):
        out, mk = self._run()
        self._assert_ran_to_the_end(out, mk)
        self.assertEqual(out.payload["stages"], ALL_STAGES)
        self.assertEqual(self.progress.count("stage verify …"), 1)

    def test_md_done_during_implement_skips_verify_and_report(self):
        self.on_stage["implement"] = self._close_md("done")
        out, mk = self._run()
        self._assert_stopped(out, mk, reason=sup.REASON_MD_CLOSED,
                             ran=["research", "plan", "implement"],
                             where="after implement, before the checkpoint push")
        self.assertIn("task md status: done", out.summary)
        self.assertEqual(mk.order, ["rescue"])          # rescue ran, the push did not
        self.assertNotIn("stage verify …", self.progress)

    def test_md_done_during_research_stops_before_plan(self):
        self.on_stage["research"] = self._close_md("done")
        out, mk = self._run()
        self._assert_stopped(out, mk, reason=sup.REASON_MD_CLOSED,
                             ran=["research"], where="before stage plan")
        self.assertEqual(mk.order, [])

    def test_md_done_during_verify_stops_before_report(self):
        self.on_stage["verify"] = self._close_md("done")
        out, mk = self._run()
        self.assertEqual(self.ran, ["research", "plan", "implement", "verify"])
        self.assertEqual(out.payload[sup.RESULT_KEY], sup.REASON_MD_CLOSED)
        self.assertIn("before stage report", out.summary)
        # The implement checkpoint had already gone out; nothing after it did.
        self.assertEqual(mk.order, ["rescue", "checkpoint"])
        mk.publish.assert_not_called()
        mk.remove_worktree.assert_not_called()
        mk.kakao.assert_not_called()

    def test_md_already_closed_before_the_first_stage_runs_nothing(self):
        self.md.write_text(_md("done"), encoding="utf-8")
        out, mk = self._run()
        self._assert_stopped(out, mk, reason=sup.REASON_MD_CLOSED, ran=[],
                             where="before stage research")

    def test_cancelled_spellings_and_case_stop_too(self):
        for status in ("cancelled", "canceled", "Done"):
            with self.subTest(status=status):
                self.ran.clear()
                self.progress.clear()
                shutil.rmtree(self.runs)
                self.runs.mkdir()
                self.md.write_text(_md("open"), encoding="utf-8")
                self.on_stage["plan"] = self._close_md(status)
                out, mk = self._run()
                self._assert_stopped(out, mk, reason=sup.REASON_MD_CLOSED,
                                     ran=["research", "plan"],
                                     where="before stage implement")
                self.assertIn(f"task md status: {status.lower()}", out.summary)

    def test_absolute_task_path_is_read_too(self):
        self.on_stage["plan"] = self._close_md("done")
        out, mk = self._run(self._payload(task_path=str(self.md)))
        self.assertEqual(self.ran, ["research", "plan"])
        self.assertEqual(out.payload[sup.RESULT_KEY], sup.REASON_MD_CLOSED)


class FailOpenTest(_Harness):
    """Whenever the verdict cannot be reached, the run goes on as it always did."""

    def setUp(self):
        super().setUp()
        self.on_stage["implement"] = self._close_md("done")

    def test_control_this_fixture_stops_when_nothing_fails(self):
        out, _ = self._run()
        self.assertEqual(self.ran, ["research", "plan", "implement"])
        self.assertEqual(out.payload[sup.RESULT_KEY], sup.REASON_MD_CLOSED)

    def test_unreadable_frontmatter_runs_all_stages(self):
        with mock.patch.object(repos, "_frontmatter",
                               side_effect=ModuleNotFoundError("No module named 'yaml'")):
            out, mk = self._run()
        self._assert_ran_to_the_end(out, mk)

    def test_a_raising_check_runs_all_stages(self):
        with mock.patch.object(sup, "check_run", side_effect=RuntimeError("boom")):
            out, mk = self._run()
        self._assert_ran_to_the_end(out, mk)

    def test_kill_switch_runs_all_stages(self):
        os.environ[sup.KILL_ENV] = "0"
        out, mk = self._run()
        self._assert_ran_to_the_end(out, mk)

    def test_payload_without_task_path_runs_all_stages(self):
        out, mk = self._run(self._payload(task_path=None))
        self._assert_ran_to_the_end(out, mk)

    def test_task_path_naming_no_file_runs_all_stages(self):
        out, mk = self._run(self._payload(task_path="Todo/items/missing.md"))
        self._assert_ran_to_the_end(out, mk)

    def test_a_bare_mock_agent_changes_nothing(self):
        # test_stages.py hands _execute a Mock() agent; the check must not read it.
        agent = mock.Mock()
        out, _ = self._run(agent=agent)
        self.assertEqual(self.ran, ["research", "plan", "implement"])
        self.assertEqual(out.payload[sup.RESULT_KEY], sup.REASON_MD_CLOSED)
        self.assertEqual(
            [c for c in agent.mock_calls if "superseded" in str(c)], [])

    def test_failing_record_writes_do_not_void_the_stop(self):
        # run.log / stages.json / PROGRESS are records of the stop, not the stop.
        real_update = stages._update_progress

        def update(run_dir, **fields):
            if "superseded" in fields:
                raise OSError("disk full")
            return real_update(run_dir, **fields)

        def progress(text):
            if "superseded" in text:
                raise RuntimeError("mailbox down")
            self.progress.append(text)

        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id=SLUG,
                         payload=self._payload())
        with self._patched() as mk, \
                mock.patch.object(stages, "_update_progress", side_effect=update):
            out = StagedExecutor().execute(msg, _agent(), progress=progress)
        self.assertEqual(self.ran, ["research", "plan", "implement"])
        self.assertEqual(out.payload[sup.RESULT_KEY], sup.REASON_MD_CLOSED)
        mk.checkpoint.assert_not_called()


class UsagePauseTest(_Harness):
    def test_md_closed_during_the_usage_pause_skips_the_stage(self):
        seen: list[str] = []

        def wait(stage, payload, progress, **kw):
            seen.append(stage)
            if stage == "verify":
                self._close_md("done")()        # closed while the run was paused
            return True

        with mock.patch.object(runner_mod, "_wait_for_session_headroom",
                               side_effect=wait):
            out, mk = self._run()
        self.assertEqual(seen, ["research", "plan", "implement", "verify"])
        self.assertEqual(self.ran, ["research", "plan", "implement"])
        self.assertEqual(out.payload[sup.RESULT_KEY], sup.REASON_MD_CLOSED)
        self.assertIn("after the usage pause, before stage verify", out.summary)
        mk.kakao.assert_not_called()

    def test_control_a_pause_with_the_md_left_open_runs_everything(self):
        with mock.patch.object(runner_mod, "_wait_for_session_headroom",
                               return_value=True):
            out, mk = self._run()
        self._assert_ran_to_the_end(out, mk)


class VaultDelegateTest(_Harness):
    """A vault delegate run publishes its branch (and may merge it) in `_execute`'s
    `finally`. A superseded run must not: same contract as CANCEL."""

    def _delegate_payload(self):
        return {"worker_mode": "delegate", "auto_merge": True, "task_path": TASK_PATH}

    def test_closed_md_runs_no_stage_and_publishes_nothing(self):
        self.md.write_text(_md("done"), encoding="utf-8")
        out, mk = self._run(self._delegate_payload())
        self.assertEqual(self.ran, [])
        self.assertTrue(out.ok)
        self.assertEqual(out.payload[sup.RESULT_KEY], sup.REASON_MD_CLOSED)
        self.assertIs(out.payload["review_required"], False)
        mk.publish.assert_not_called()
        mk.merge_pr.assert_not_called()
        mk.restore_main.assert_called()
        self.assertNotIn("pr_url", out.payload)

    def test_control_open_md_runs_delegate_and_publishes(self):
        out, mk = self._run(self._delegate_payload())
        self.assertEqual(self.ran, ["delegate"])
        self.assertNotIn(sup.RESULT_KEY, out.payload)
        mk.publish.assert_called_once()

    def test_vault_task_never_probes_git(self):
        # A vault worker branch carries sync commits and is cut from a local
        # main: the md is the only signal used there.
        with mock.patch.object(sup, "branch_landed") as landed:
            out, mk = self._run(self._payload(repo=None))
        landed.assert_not_called()
        self.assertEqual(self.ran, ALL_STAGES)
        self.assertNotIn(sup.RESULT_KEY, out.payload)


class GitWiringTest(_HarnessMixin, _GitFixture):
    """The `merged` half through the loop, against real git. The run's checkout
    is a real clone on worker/t with one pushed commit; "a person" squash-merges
    that branch into origin main from a second clone."""

    def setUp(self):
        _GitFixture.setUp(self)
        self._branch_commit()
        self._setup_harness(clone=self.clone)
        self.probes: list = []
        real = sup.branch_landed

        def spy(repo_dir, branch):
            answer = real(repo_dir, branch)
            self.probes.append((list(self.ran), answer))
            return answer

        patcher = mock.patch.object(sup, "branch_landed", side_effect=spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_control_unmerged_branch_runs_all_five_stages(self):
        out, mk = self._run()
        self._assert_ran_to_the_end(out, mk)
        # Probed for real, after implement only, and every answer was "not landed".
        self.assertTrue(self.probes)
        for ran, answer in self.probes:
            self.assertIn("implement", ran)
            self.assertIs(answer, False)

    def test_squash_merge_during_implement_stops_before_the_checkpoint(self):
        self.on_stage["implement"] = self._land_squash
        out, mk = self._run()
        self._assert_stopped(out, mk, reason=sup.REASON_MERGED,
                             ran=["research", "plan", "implement"],
                             where="after implement, before the checkpoint push")
        self.assertIn("already in the default branch", out.summary)
        self.assertEqual(mk.order, ["rescue"])
        self.assertEqual(self.probes, [(["research", "plan", "implement"], True)])
        # The md is still open: the orchestrator closes it, not the runner.
        self.assertEqual(self.md.read_text(encoding="utf-8"), _md("open"))

    def test_branch_merged_before_the_run_still_gets_its_implement(self):
        # At run start "already merged" cannot tell a stale re-dispatch from
        # "part of this was merged by hand, carry on", so git is not asked
        # until implement has had its turn.
        self._land_squash()
        out, mk = self._run()
        self._assert_stopped(out, mk, reason=sup.REASON_MERGED,
                             ran=["research", "plan", "implement"],
                             where="after implement, before the checkpoint push")
        self.assertEqual(self.probes, [(["research", "plan", "implement"], True)])

    def test_implement_that_adds_work_on_a_merged_branch_carries_on(self):
        self._land_squash()

        def more_work():
            (self.clone / "b.txt").write_text("new work\n", encoding="utf-8")
            self._ok(self.clone, "commit", "-qam", "more")

        self.on_stage["implement"] = more_work
        out, mk = self._run()
        self._assert_ran_to_the_end(out, mk)

    def test_squash_merge_during_verify_stops_before_report(self):
        self.on_stage["verify"] = self._land_squash
        out, mk = self._run()
        self.assertEqual(self.ran, ["research", "plan", "implement", "verify"])
        self.assertEqual(out.payload[sup.RESULT_KEY], sup.REASON_MERGED)
        self.assertIn("before stage report", out.summary)

    def test_inherited_implement_on_a_merged_branch_stops_before_verify(self):
        # A resumed run skips implement; the first boundary it reaches still asks.
        self._land_squash()
        out, mk = self._run(inherited=("research", "plan", "implement"))
        self.assertEqual(self.ran, [])
        self.assertEqual(out.payload[sup.RESULT_KEY], sup.REASON_MERGED)
        self.assertEqual(out.payload["stages"], ["research", "plan", "implement"])
        self.assertIn("before stage verify", out.summary)
        self.assertEqual(self.probes, [([], True)])
        mk.checkpoint.assert_not_called()

    def test_control_inherited_implement_on_an_unmerged_branch_verifies(self):
        out, mk = self._run(inherited=("research", "plan", "implement"))
        self.assertEqual(self.ran, ["verify", "report"])
        self.assertNotIn(sup.RESULT_KEY, out.payload)

    def test_failed_fetch_carries_on(self):
        self.on_stage["implement"] = lambda: (
            self._land_squash(),
            self._ok(self.clone, "remote", "set-url", "origin",
                     str(self.root / "gone.git")))
        out, mk = self._run()
        self._assert_ran_to_the_end(out, mk)
        self.assertTrue(self.probes)
        self.assertTrue(all(answer is None for _, answer in self.probes))


class EndToEndTest(_Harness):
    """dispatch -> Runner.poll_once (real stage loop) -> collect: the runner's
    reply and the orchestrator's reading of it, joined."""

    def setUp(self):
        super().setUp()
        self.mb = FileMailbox(self.vault / "Worker" / "agents")
        self.ledger = Ledger(self.vault / "Worker" / "assignments")
        self.sent: list[tuple] = []
        self.routed: list[tuple] = []

        def route(text, **kw):
            self.routed.append((text, kw))
            return ("send", text)

        self.orch = Orchestrator(
            self.mb, Registry([_agent()]), self.ledger,
            notify=lambda text, link=None: self.sent.append((text, link)),
            alert_route=route)
        self.runner = Runner(self.mb, _agent(), StagedExecutor(),
                             log=lambda *_: None, ledger_root=self.ledger.root)

    def _dispatch_and_run(self):
        a = self.orch.dispatch(Task(SLUG, self._payload()))
        self.assertIsNotNone(a)
        with self._patched() as mk:
            self.assertEqual(self.runner.poll_once(), 1)
        replies = self.mb.receive(m.ORCHESTRATOR)
        self.orch.collect()
        return replies, mk, self.ledger.load(SLUG)

    def test_md_closed_mid_run_ends_quietly_on_both_sides(self):
        self.on_stage["implement"] = self._close_md("done")
        closed = _md("done").encode("utf-8")
        replies, mk, row = self._dispatch_and_run()
        self.assertEqual(self.ran, ["research", "plan", "implement"])
        types_ = [r.type for r in replies]
        self.assertNotIn(m.FAIL, types_)
        self.assertNotIn(m.NEEDS_INPUT, types_)
        self.assertEqual(types_[0], m.ACCEPT)
        self.assertEqual(types_[-1], m.RESULT)
        self.assertEqual(replies[-1].payload[sup.RESULT_KEY], sup.REASON_MD_CLOSED)
        self.assertEqual(row.state, A.DONE)
        self.assertEqual(
            [h["reason"] for h in row.history if h.get("type") == sup.HISTORY_TYPE],
            [sup.REASON_MD_CLOSED])
        self.assertEqual(row.last_alert_kind, "")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.routed, [])
        mk.kakao.assert_not_called()
        mk.checkpoint.assert_not_called()
        # The md is exactly as the person left it: no box ticked, no trace line.
        self.assertEqual(self.md.read_bytes(), closed)
        # ...and nothing is re-dispatched.
        self.assertEqual([e for e in self.mb.receive("w") if e.type == m.TASK_ASSIGN], [])
        self.assertIsNone(self.orch.dispatch(Task(SLUG, self._payload())))

    def test_control_open_md_runs_to_the_review_park_and_alerts(self):
        # Same wiring with the md left open: all five stages run, and (the task
        # is auto_merge:false) the orchestrator parks it for review and pages.
        replies, mk, row = self._dispatch_and_run()
        self.assertEqual(self.ran, ALL_STAGES)
        self.assertEqual(replies[-1].type, m.RESULT)
        self.assertNotIn(sup.RESULT_KEY, replies[-1].payload)
        self.assertEqual(row.state, A.AWAITING_HUMAN)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(row.last_alert_kind, ALERT_RESULT_NO_PR)   # report: pr_url null


if __name__ == "__main__":
    unittest.main()
