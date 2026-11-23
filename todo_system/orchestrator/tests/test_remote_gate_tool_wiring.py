"""Worker stages may ship a heavy / over-memory verification command to a
separate node (remote_gate) without a task `allowed_tools` edit.

The whitelist splits on spaces, so the executor is granted through one space-free
PATH-wrapper rule, stages.REMOTE_GATE_TOOL_RULE == `Bash(macp-remote-gate:*)`.
These tests pin which stages get it, that verify stays read-only, that a task's
own list keeps it, that the rule is a single safe token, that build_stage_argv
injects the usage metadata (on the metadata like the BROWSER line), and that
the wrapper runs `decide` from any cwd.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from todo_system import task_validate
from todo_system.orchestrator import stages

REPO_ROOT = Path(stages.__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "macp-remote-gate"
RULE = stages.REMOTE_GATE_TOOL_RULE


class RemoteGateToolWiringTest(unittest.TestCase):

    def test_write_stages_grant_the_rule_once(self):
        self.assertEqual(set(stages.REMOTE_GATE_TOOL_STAGES),
                         {"implement", "verify", "quick", "delegate"})
        for stage in stages.REMOTE_GATE_TOOL_STAGES:
            with self.subTest(stage=stage):
                self.assertEqual(stages._safe_tools(stage).count(RULE), 1)

    def test_other_stages_do_not_get_it(self):
        for stage in ("report", "discover"):
            with self.subTest(stage=stage):
                self.assertNotIn(RULE, stages._safe_tools(stage))
        self.assertNotIn(RULE, stages.FALLBACK_RUNNER_TOOLS)

    def test_verify_stays_read_only_with_the_rule(self):
        """verify being test-only is what lets the pipeline trust its verdict; the
        remote-gate rule is a Bash runner, never Edit."""
        self.assertNotIn("Edit", stages._safe_tools("verify"))

    def test_rule_is_one_space_free_safe_token(self):
        self.assertEqual(RULE, "Bash(macp-remote-gate:*)")
        self.assertNotIn(" ", RULE)
        for bad in stages.FORBIDDEN_TOOL_SUBSTRINGS:
            self.assertNotIn(bad, RULE)
        self.assertEqual(task_validate._check_bash_patterns(RULE), [])

    def test_task_override_keeps_the_rule(self):
        # a task that supplies its own list (as this macp task does) still carries
        # the standing remote-gate rule, exactly once.
        tools = stages._safe_tools("implement", "Read,Edit,Write,Bash(git:*),Bash(gcloud:*)")
        self.assertEqual(tools.count(RULE), 1)
        # verify is not a task-overridable stage, but the rule is in its default.
        self.assertIn(RULE, stages._safe_tools("verify", "Read,Bash(gcloud:*)"))

    def _argv(self, stage):
        return stages.build_stage_argv(
            stage, task_id="demo-task", run_id="20260101T000000-demo-task",
            run_dir=Path("/vault/Worker/runs/x"), branch="worker/demo-task")

    def test_metadata_line_present_for_write_stages_absent_for_report(self):
        for stage in ("implement", "verify", "quick", "delegate"):
            with self.subTest(stage=stage):
                prompt = self._argv(stage)[2]
                self.assertIn(stages.REMOTE_GATE_CLI, prompt)
                self.assertIn("decide --repo", prompt)
                self.assertIn("run --repo", prompt)
                self.assertIn("demo-task", prompt)     # the --task-id is filled in
        self.assertNotIn(stages.REMOTE_GATE_CLI, self._argv("report")[2])

    @unittest.skipUnless(WRAPPER.is_file(), "scripts/macp-remote-gate not in this tree")
    def test_wrapper_runs_decide_from_any_cwd(self):
        self.assertEqual(WRAPPER.name, stages.REMOTE_GATE_CLI)
        self.assertTrue(os.access(WRAPPER, os.X_OK), "scripts/macp-remote-gate must be 0755")
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "MACP_REPO": str(REPO_ROOT)}
            env.pop("PYTHONPATH", None)
            out = subprocess.run(
                ["bash", str(WRAPPER), "decide", "--repo", tmp, "--",
                 "./gradlew :shared:testDebugUnitTest"],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=90)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn(out.stdout.strip(), ("local", "remote"))


if __name__ == "__main__":
    unittest.main()
