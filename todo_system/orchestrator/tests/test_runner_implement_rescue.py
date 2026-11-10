"""Tests for the Implement WIP-rescue safety net.

(A) `stages.rescue_uncommitted` — the never-raising helper that commits a green
    stage's uncommitted leftovers onto the worker branch so checkpoint_push can
    push them (the macp#131 loss: a stage went green, hit the turn limit before
    committing, nothing was saved).
(B) the runner wiring — rescue runs before the implement checkpoint (normal,
    timeout and capacity exits), never on a vault/default-branch task, and a
    plan that declares the task oversize short-circuits Implement to the rescope
    park.

No Claude Code is spawned; (A) uses a real temp git repo, (B) mocks the stage
subprocess like the sibling runner tests.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import message as m
from todo_system.orchestrator import repos, stages
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator.registry import Agent
from todo_system.orchestrator.runner import StagedExecutor


def _agent(**kw) -> Agent:
    base = dict(id="armchair-commander", kind="ai-local",
                capabilities=["godot", "git"], repo="~/Worker/armchair-commander")
    base.update(kw)
    return Agent(**base)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=False)


class RescueUncommittedHelperTest(unittest.TestCase):
    """(A) the pure git helper, driven against a real temp repo."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@example.com")
        _git(self.repo, "config", "user.name", "t")
        (self.repo / "tracked.txt").write_text("one\n", encoding="utf-8")
        _git(self.repo, "add", "tracked.txt")
        _git(self.repo, "commit", "-q", "-m", "init")
        # a worker branch, the normal rescue target
        _git(self.repo, "checkout", "-q", "-b", "worker/t")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _head(self) -> str:
        return _git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def test_commits_tracked_edit_and_new_file(self):
        (self.repo / "tracked.txt").write_text("two\n", encoding="utf-8")
        (self.repo / "new.txt").write_text("new\n", encoding="utf-8")
        res = stages.rescue_uncommitted(self.repo, "worker/t", "t")
        self.assertTrue(res["committed"], res)
        self.assertTrue(res["sha"])
        subj = _git(self.repo, "log", "-1", "--format=%s").stdout.strip()
        self.assertEqual(subj, "chore(worker): t WIP rescue after implement")
        self.assertEqual(_git(self.repo, "status", "--porcelain").stdout.strip(), "")
        files = _git(self.repo, "show", "--stat", "--format=", "HEAD").stdout
        self.assertIn("tracked.txt", files)
        self.assertIn("new.txt", files)

    def test_gitignored_file_is_not_committed(self):
        (self.repo / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
        (self.repo / "secret.txt").write_text("nope\n", encoding="utf-8")
        (self.repo / "keep.txt").write_text("keep\n", encoding="utf-8")
        res = stages.rescue_uncommitted(self.repo, "worker/t", "t")
        self.assertTrue(res["committed"], res)
        files = _git(self.repo, "show", "--stat", "--format=", "HEAD").stdout
        self.assertIn("keep.txt", files)
        self.assertNotIn("secret.txt", files)
        # secret.txt is still on disk, just untracked/ignored
        self.assertTrue((self.repo / "secret.txt").exists())

    def test_refuses_default_branch(self):
        _git(self.repo, "checkout", "-q", "main")
        (self.repo / "tracked.txt").write_text("edited on main\n", encoding="utf-8")
        before = self._head()
        res = stages.rescue_uncommitted(self.repo, "main", "t")
        self.assertFalse(res["committed"])
        self.assertIn("default branch", res["note"])
        self.assertEqual(self._head(), before)      # no commit created

    def test_clean_tree_is_noop(self):
        res = stages.rescue_uncommitted(self.repo, "worker/t", "t")
        self.assertFalse(res["committed"])
        self.assertEqual(res["note"], "nothing to rescue")

    def test_skips_when_head_is_a_different_branch(self):
        # Asked to rescue worker/t, but HEAD is on main → must not touch anything.
        _git(self.repo, "checkout", "-q", "main")
        (self.repo / "tracked.txt").write_text("edited\n", encoding="utf-8")
        before = self._head()
        res = stages.rescue_uncommitted(self.repo, "worker/t", "t")
        self.assertFalse(res["committed"])
        self.assertIn("not worker/t", res["note"])
        self.assertEqual(self._head(), before)
        # the edit is still uncommitted on main, untouched
        self.assertEqual(_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main")

    def test_skips_detached_head(self):
        _git(self.repo, "checkout", "-q", "--detach")
        (self.repo / "tracked.txt").write_text("edited detached\n", encoding="utf-8")
        res = stages.rescue_uncommitted(self.repo, "worker/t", "t")
        self.assertFalse(res["committed"])
        self.assertIn("detached", res["note"])

    def test_only_stages_inside_the_worktree(self):
        # A file OUTSIDE the repo must never be committed — `git -C add -A` is
        # repo-relative, so this is structural, pinned here regardless.
        outside = self.tmp / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        (self.repo / "inside.txt").write_text("inside\n", encoding="utf-8")
        res = stages.rescue_uncommitted(self.repo, "worker/t", "t")
        self.assertTrue(res["committed"], res)
        files = _git(self.repo, "show", "--stat", "--format=", "HEAD").stdout
        self.assertIn("inside.txt", files)
        self.assertNotIn("outside.txt", files)

    def test_never_raises_on_bad_repo(self):
        res = stages.rescue_uncommitted(self.tmp / "does-not-exist", "worker/t", "t")
        self.assertFalse(res["committed"])
        self.assertIsNone(res["sha"])


class ImplementRescueWiringTest(unittest.TestCase):
    """(B) the runner wiring: on a green implement the rescue runs BEFORE the
    checkpoint push, in that order, on an external repo; a vault task skips it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        self.clone = self.tmp / "clone"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, repo_url="git@github.com:x/y.git", pipeline=("implement",),
             plan_body=None):
        order: list[str] = []

        def fake_rescue(repo_dir, branch, slug, progress=None):
            order.append(("rescue", str(repo_dir), branch, slug))
            return {"committed": True, "sha": "deadbeefcafe",
                    "note": "rescued uncommitted work on worker/t (deadbeef)"}

        def fake_checkpoint(repo_dir, branch, progress=None):
            order.append(("checkpoint", str(repo_dir), branch))
            return {"pushed": True, "tip": "deadbeefcafe", "commits": 1,
                    "note": "checkpoint pushed 1 commit(s) to origin/worker/t"}

        def fake_run(argv, **kw):
            # write each stage's artifact so the stage counts as completed
            for st in pipeline:
                art = self._run_dir() / stages.STAGE_ARTIFACT[st]
                if st == "plan" and plan_body is not None:
                    art.write_text(plan_body, encoding="utf-8")
                elif not art.exists():
                    art.write_text("ok\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")

        vp = subprocess.CompletedProcess([], 0, "", "")
        vp.problem = None
        vp.orphaned_commits = None
        payload = {"repo": repo_url, "auto_merge": False, "model_tier": "heavy"}
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
                mock.patch.object(stages, "rescue_uncommitted", side_effect=fake_rescue),
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
        return out, order

    def _run_dir(self) -> Path:
        self.runs.mkdir(exist_ok=True)
        dirs = [d for d in self.runs.iterdir() if d.is_dir()]
        if not dirs:
            d = self.runs / "run-under-test"
            d.mkdir()
            return d
        return dirs[0]

    def test_rescue_runs_before_checkpoint_on_external_implement(self):
        out, order = self._run()
        kinds = [o[0] for o in order]
        self.assertIn("rescue", kinds)
        self.assertIn("checkpoint", kinds)
        self.assertLess(kinds.index("rescue"), kinds.index("checkpoint"))
        # targeted the external CLONE and the worker branch, with the slug
        resc = next(o for o in order if o[0] == "rescue")
        self.assertEqual(resc[1], str(self.clone))
        self.assertEqual(resc[2], "worker/t")
        self.assertEqual(resc[3], "t")
        # and run.log carries the note line
        log = (self._run_dir() / "run.log").read_text(encoding="utf-8")
        self.assertIn("implement WIP rescue", log)

    def test_vault_implement_does_not_rescue(self):
        out, order = self._run(repo_url=None)
        self.assertNotIn("rescue", [o[0] for o in order])
        self.assertNotIn("checkpoint", [o[0] for o in order])

    def test_plan_declares_oversize_short_circuits_implement(self):
        plan = ("# Plan\nThis task exceeds a single Implement stage — it must be "
                "re-scoped into 3 sub-tasks.\n")
        out, order = self._run(pipeline=("plan", "implement"), plan_body=plan)
        self.assertFalse(out.ok)
        self.assertEqual(out.payload.get("verify"), "FAIL")
        self.assertIn("re-scop", out.payload.get("blocked_on", "").lower())
        # implement never ran → no rescue, no checkpoint
        self.assertEqual(order, [])
        # the recognised oversize park keyword set sees it
        from todo_system.orchestrator import rescope
        self.assertTrue(rescope.is_oversize_rescope_park(out.payload["blocked_on"]))

    def test_template_plan_does_not_short_circuit(self):
        # Control: an ordinary plan (the `## Parallelizable subtasks` heading, no
        # oversize declaration) must let Implement proceed — rescue + checkpoint run.
        plan = ("# Plan\n## Phases\n### Phase 1\n- edit a\n"
                "## Parallelizable subtasks\nNone; run the subtasks serially.\n")
        out, order = self._run(pipeline=("plan", "implement"), plan_body=plan)
        self.assertIn("rescue", [o[0] for o in order])
        self.assertIn("checkpoint", [o[0] for o in order])


if __name__ == "__main__":
    unittest.main()
