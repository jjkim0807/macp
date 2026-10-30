"""Worker stages may read the mail-inbox CLI (2FA codes sent by e-mail) without
a task `allowed_tools` edit.

The whitelist splits on spaces, so the CLI is granted through one space-free
PATH wrapper rule, stages.MAIL_TOOL_RULE == `Bash(macp-mail:*)`. These tests pin
which stages get it, that delegate gets nothing broader, that research/plan
stay read-only, and that a task's own list keeps it. The wrapper itself ships
from its own repo, so there is no wrapper-file test here.
"""

from __future__ import annotations

import unittest

from todo_system import task_validate
from todo_system.orchestrator import stages

RULE = stages.MAIL_TOOL_RULE


class MailToolWiringTest(unittest.TestCase):

    def test_write_stages_grant_the_rule_once(self):
        self.assertEqual(set(stages.MAIL_TOOL_STAGES),
                         {"implement", "verify", "quick", "delegate"})
        for stage in stages.MAIL_TOOL_STAGES:
            with self.subTest(stage=stage):
                self.assertEqual(stages._safe_tools(stage).count(RULE), 1)

    def test_other_stages_do_not_get_it(self):
        for stage in ("research", "plan", "report", "discover"):
            with self.subTest(stage=stage):
                self.assertNotIn(RULE, stages._safe_tools(stage))
        self.assertNotIn(RULE, stages.FALLBACK_RUNNER_TOOLS)

    def test_is_a_standing_rule(self):
        self.assertIn((RULE, stages.MAIL_TOOL_STAGES), stages._STANDING_RULES)

    def test_research_and_plan_stay_read_only(self):
        self.assertEqual(stages.STAGE_TOOLS["research"], "Read Write")
        self.assertEqual(stages.STAGE_TOOLS["plan"], "Read Write")
        # A task cannot widen them either.
        for stage in ("research", "plan"):
            with self.subTest(stage=stage):
                self.assertEqual(stages._safe_tools(stage, f"Read,{RULE}"),
                                 ["Read", "Write"])

    def test_delegate_gets_no_broad_runner(self):
        granted = stages.STAGE_TOOLS["delegate"].split()
        for broad in ("Bash(uv:*)", "Bash(python3:*)", "Bash(python:*)",
                      "Bash(bash:*)", "Bash(node:*)"):
            with self.subTest(rule=broad):
                self.assertNotIn(broad, granted)
        self.assertIn(RULE, granted)

    def test_rule_is_one_space_free_safe_token(self):
        self.assertEqual(stages.MAIL_CLI, "macp-mail")
        self.assertEqual(RULE, "Bash(macp-mail:*)")
        self.assertNotIn(" ", RULE)
        for bad in stages.FORBIDDEN_TOOL_SUBSTRINGS:
            self.assertNotIn(bad, RULE)
        self.assertEqual(task_validate._check_bash_patterns(RULE), [])

    def test_task_override_keeps_the_rule(self):
        tools = stages._safe_tools("implement", "Read,Bash(gcloud:*)")
        self.assertEqual(tools.count(RULE), 1)
        self.assertEqual(tools[-1], RULE)
        # Already listed by the task: exactly once.
        self.assertEqual(stages._safe_tools("implement", f"Read,{RULE}").count(RULE), 1)
        # Stages a task cannot widen still return their own default untouched.
        verify = stages._safe_tools("verify", "Read,Bash(gcloud:*)")
        self.assertEqual(verify, stages.STAGE_TOOLS["verify"].split())
        self.assertIn(RULE, verify)


if __name__ == "__main__":
    unittest.main()
