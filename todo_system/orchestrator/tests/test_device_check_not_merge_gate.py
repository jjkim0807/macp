"""A device check is not a merge condition.

An app-UI change that passed verify merges like any other change, whether or not
a device check saw it this run: the post-verify screen check still runs and
records what it saw in verify.md, and the verdict decides the merge. Pinned at the
real `StagedExecutor._execute` seam; every git / gh / node effect is faked — no
real git, no emulator, nothing outside a `tempfile.mkdtemp()` sandbox. The
stage-side half (a device check that could not run is recorded as unverified,
never parked) is pinned on the run metadata in test_emulator_tool_wiring.py.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import agent_backend, repos
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator import screen_check, stages
from todo_system.orchestrator import message as m
from todo_system.orchestrator.registry import Agent
from todo_system.orchestrator.runner import StagedExecutor


def _agent(**kw) -> Agent:
    base = dict(id="w", kind="ai-local", capabilities=["git"], repo="~/Worker/x")
    base.update(kw)
    return Agent(**base)


def _prepared(rc_code: int = 0) -> subprocess.CompletedProcess:
    cp = subprocess.CompletedProcess(args=["git", "checkout", "-B"], returncode=rc_code,
                                     stdout="", stderr="")
    cp.orphaned_commits = None
    return cp


class AppUiChangeMergesTest(unittest.TestCase):
    """An app repo, a diff under `app/`, a verify stage that wrote
    `confidence: high`, and a screen check that saw nothing: the report stage is
    told to merge and the task does not park."""

    VERIFY_HIGH = "---\noverall_status: pass\nconfidence: high\n---\n\n# verify\n"
    REPORT_OK = ("---\noverall_status: pass\nmerged: no\n"
                 "pr_url: https://github.com/o/r/pull/1\n---\n")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "clone"
        self.repo.mkdir()
        (self.repo / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")  # app repo
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        self.vault = self.tmp / "vault"
        (self.vault / "Worker").mkdir(parents=True)
        self.captured_auto_merge: dict[str, object] = {}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_run_stage(this_test):
        def run_stage(self, stage, **kw):
            this_test.captured_auto_merge[stage] = kw.get("auto_merge")
            art = Path(kw["run_dir"]) / stages.STAGE_ARTIFACT[stage]
            if stage == "verify":
                art.write_text(this_test.VERIFY_HIGH, encoding="utf-8")
            elif stage == "report":
                art.write_text(this_test.REPORT_OK, encoding="utf-8")
            else:
                art.write_text("ok\n", encoding="utf-8")
            return subprocess.CompletedProcess(args=["claude"], returncode=0,
                                               stdout="", stderr="")
        return run_stage

    @contextlib.contextmanager
    def _sandbox(self):
        def fake_screen_check(self, repo, payload, run_dir, progress, branch=None):
            # The node could not show the screen this run: no device evidence.
            screen_check.append_note(run_dir, "화면 확인 불가(노드 오류)")

        patches = [
            mock.patch.object(repos, "ensure_worktree", return_value=self.repo),
            mock.patch.object(stages, "restore_main"),
            mock.patch.object(stages, "install_push_guard"),
            mock.patch.object(stages, "default_branch", return_value="main"),
            mock.patch.object(stages, "prepare_external_branch", return_value=_prepared()),
            mock.patch.object(stages, "remove_worktree"),
            mock.patch.object(stages, "RUNS_DIR", self.runs),
            mock.patch.object(stages, "_VAULT_DATA_ROOT", self.vault),
            mock.patch.object(stages, "merge_landed", return_value=True),
            mock.patch.object(runner_mod, "_stop_build_daemons"),
            mock.patch.object(StagedExecutor, "_publish_beta", return_value=""),
            mock.patch.object(StagedExecutor, "_changed_paths",
                              return_value=["app/src/Main.kt"]),
            mock.patch.object(StagedExecutor, "_screen_check_config", return_value=None),
            mock.patch.object(StagedExecutor, "_app_screen_check", fake_screen_check),
            mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")),
            mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
            mock.patch.object(StagedExecutor, "_pipeline_for", return_value=("verify", "report")),
            mock.patch.object(agent_backend.ClaudeAgentBackend, "run_stage",
                              self._fake_run_stage()),
            mock.patch.object(agent_backend.ClaudeAgentBackend, "report_usage", return_value=None),
            mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            yield

    def _run(self, *, payload_extra=None):
        payload = {"repo": "git@github.com:o/r.git", "summary": "an app UI change"}
        payload.update(payload_extra or {})
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="task-x", payload=payload)
        lines: list[str] = []
        with self._sandbox():
            out = StagedExecutor().execute(msg, _agent(), progress=lines.append)
        return out

    def _run_dir(self) -> Path:
        dirs = [d for d in self.runs.iterdir() if d.is_dir()]
        self.assertEqual(len(dirs), 1, f"expected one run dir, got {dirs}")
        return dirs[0]

    def test_app_ui_change_without_device_evidence_auto_merges(self):
        out = self._run()
        self.assertTrue(out.ok, out.summary)
        self.assertIs(self.captured_auto_merge["report"], True)    # report is told to merge
        self.assertFalse(out.payload.get("review_required"))       # nothing parks
        verify = (self._run_dir() / "verify.md").read_text(encoding="utf-8")
        self.assertIn("confidence: high", verify)                  # left as the stage wrote it
        self.assertIn("화면 확인 불가", verify)                     # the screen check still ran

    def test_auto_merge_false_task_still_parks_for_review(self):
        out = self._run(payload_extra={"auto_merge": False})
        self.assertTrue(out.ok, out.summary)
        self.assertIs(self.captured_auto_merge["report"], False)
        self.assertTrue(out.payload.get("review_required"))


if __name__ == "__main__":
    unittest.main()
