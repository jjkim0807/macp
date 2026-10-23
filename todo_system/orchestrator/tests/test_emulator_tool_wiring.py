"""Worker stages may drive the on-demand emulator node (beta_smoke.run_smoke on
macp-app-test) for a pre-merge on-device check without a task `allowed_tools` edit.

The whitelist splits on spaces, so the CLI is granted through one space-free
PATH-wrapper rule, stages.EMULATOR_TOOL_RULE == `Bash(macp-emulator:*)`. These
tests pin which stages get it (implement/verify/quick — NOT delegate, unlike
SMS/BROWSER/REMOTE_GATE), that verify stays read-only, that research/plan stay
read-only, that a task's own list keeps it, that the rule is a single safe
token, that build_stage_argv injects the usage metadata (the worker-*.md prompts
under .claude/** are unwriteable headless, so it rides on metadata like the
BROWSER line), that with_emulator_owner injects the run_id owner on exactly the
three stages, and that the wrapper runs `status` from any cwd.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from todo_system import task_validate
from todo_system.orchestrator import stages

REPO_ROOT = Path(stages.__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "macp-emulator"
RULE = stages.EMULATOR_TOOL_RULE
OWNER_ENV = "MACP_EMULATOR_OWNER"


class EmulatorToolWiringTest(unittest.TestCase):

    def test_write_stages_grant_the_rule_once(self):
        # implement/verify/quick — and NOT delegate, which the other three
        # standing rules grant but an on-device check does not belong to.
        self.assertEqual(set(stages.EMULATOR_TOOL_STAGES),
                         {"implement", "verify", "quick"})
        for stage in stages.EMULATOR_TOOL_STAGES:
            with self.subTest(stage=stage):
                self.assertEqual(stages._safe_tools(stage).count(RULE), 1)

    def test_delegate_does_not_get_it_unlike_the_other_standing_rules(self):
        # The distinguishing property: SMS/BROWSER/REMOTE_GATE reach delegate,
        # the emulator rule deliberately does not.
        self.assertNotIn(RULE, stages._safe_tools("delegate"))
        for other in (stages.SMS_TOOL_RULE, stages.BROWSER_TOOL_RULE,
                      stages.REMOTE_GATE_TOOL_RULE):
            self.assertIn(other, stages._safe_tools("delegate"))

    def test_other_stages_do_not_get_it(self):
        for stage in ("research", "plan", "report", "discover", "delegate"):
            with self.subTest(stage=stage):
                self.assertNotIn(RULE, stages._safe_tools(stage))
        self.assertNotIn(RULE, stages.FALLBACK_RUNNER_TOOLS)

    def test_research_and_plan_stay_read_only(self):
        self.assertEqual(stages.STAGE_TOOLS["research"], "Read Write")
        self.assertEqual(stages.STAGE_TOOLS["plan"], "Read Write")

    def test_verify_stays_read_only_with_the_rule(self):
        """verify being test-only is what lets the pipeline trust its verdict; the
        emulator rule is a Bash runner, never Edit."""
        self.assertNotIn("Edit", stages._safe_tools("verify"))

    def test_rule_is_one_space_free_safe_token(self):
        self.assertEqual(RULE, "Bash(macp-emulator:*)")
        self.assertEqual(stages.EMULATOR_CLI, "macp-emulator")
        self.assertNotIn(" ", RULE)
        for bad in stages.FORBIDDEN_TOOL_SUBSTRINGS:
            self.assertNotIn(bad, RULE)
        self.assertEqual(task_validate._check_bash_patterns(RULE), [])

    def test_task_override_keeps_the_rule(self):
        # the macp task supplies its own allowed_tools; the standing emulator rule
        # still rides along on the three write stages, exactly once.
        tools = stages._safe_tools("implement", "Read,Edit,Write,Bash(git:*),Bash(gcloud:*)")
        self.assertEqual(tools.count(RULE), 1)
        self.assertIn(RULE, stages._safe_tools("verify", "Read,Bash(gcloud:*)"))
        # A task that already lists it does not get a duplicate.
        self.assertEqual(stages._safe_tools("implement", f"Read,{RULE}").count(RULE), 1)

    def test_owner_env_constant(self):
        self.assertEqual(stages.EMULATOR_OWNER_ENV, OWNER_ENV)

    def test_with_emulator_owner_sets_only_emulator_stages(self):
        base = {"PATH": "/bin", OWNER_ENV: "stale-owner"}
        snapshot = dict(base)
        for stage in stages.EMULATOR_TOOL_STAGES:
            with self.subTest(stage=stage):
                env = stages.with_emulator_owner(stage, base, "RUNID-x")
                self.assertEqual(env[OWNER_ENV], "RUNID-x")
                self.assertEqual(env["PATH"], "/bin")
        # delegate is excluded here too — the key is stripped, not set.
        for stage in ("research", "plan", "report", "discover", "delegate"):
            with self.subTest(stage=stage):
                env = stages.with_emulator_owner(stage, base, "RUNID-x")
                self.assertNotIn(OWNER_ENV, env)
                self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(base, snapshot)        # never mutates its input

    def _argv(self, stage):
        return stages.build_stage_argv(
            stage, task_id="demo-task", run_id="20260101T000000-demo-task",
            run_dir=Path("/vault/Worker/runs/x"), branch="worker/demo-task")

    def test_metadata_line_present_for_write_stages_absent_for_research(self):
        for stage in ("implement", "verify", "quick"):
            with self.subTest(stage=stage):
                prompt = self._argv(stage)[2]
                self.assertIn(stages.EMULATOR_CLI, prompt)
                self.assertIn("run --repo-dir", prompt)
                self.assertIn("--seed owner|none", prompt)
                self.assertIn("blocked_on", prompt)
                self.assertIn(f"${OWNER_ENV}", prompt)   # owner is injected via env
                # the 75 (lease busy) / 76 (over budget) contract is documented
                self.assertIn("75", prompt)
                self.assertIn("76", prompt)
        # research is read-only and must carry no emulator affordance.
        self.assertNotIn(stages.EMULATOR_CLI, self._argv("research")[2])
        # delegate carries the other three affordances but not this one.
        self.assertNotIn(stages.EMULATOR_CLI, self._argv("delegate")[2])

    @unittest.skipUnless(WRAPPER.is_file(), "scripts/macp-emulator not in this tree")
    def test_wrapper_runs_status_from_any_cwd(self):
        self.assertEqual(WRAPPER.name, stages.EMULATOR_CLI)
        self.assertTrue(os.access(WRAPPER, os.X_OK), "scripts/macp-emulator must be 0755")
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "MACP_REPO": str(REPO_ROOT)}
            env.pop("PYTHONPATH", None)
            env.pop(OWNER_ENV, None)
            out = subprocess.run(
                ["bash", str(WRAPPER), "status"],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=90)
            self.assertEqual(out.returncode, 0, out.stderr)
            data = json.loads(out.stdout)        # status prints one JSON object
            for key in ("node_lease", "month_spent_usd", "monthly_cap_usd", "over_budget"):
                self.assertIn(key, data)


if __name__ == "__main__":
    unittest.main()
