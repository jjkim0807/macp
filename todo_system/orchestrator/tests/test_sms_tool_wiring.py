"""Worker stages may read the phone-SMS inbox (CLAUDE.md §11) without a task
`allowed_tools` edit.

The whitelist splits on spaces, so the CLI is granted through one space-free
PATH wrapper rule, stages.SMS_TOOL_RULE == `Bash(macp-sms:*)`. These tests pin
which stages get it, that delegate gets nothing broader, that research/plan
stay read-only, that a task's own list keeps it, and that §11 documents the
same command name the whitelist grants.
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
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
WRAPPER = REPO_ROOT / "scripts" / "macp-sms"


def _section_11(text: str) -> str:
    body = text[text.index("\n## 11.") + 1:]
    end = body.find("\n## ")
    return body if end == -1 else body[:end]


class SmsToolWiringTest(unittest.TestCase):

    def test_write_stages_grant_the_sms_rule(self):
        self.assertEqual(set(stages.SMS_TOOL_STAGES),
                         {"implement", "verify", "quick", "delegate"})
        for stage in stages.SMS_TOOL_STAGES:
            with self.subTest(stage=stage):
                self.assertIn(stages.SMS_TOOL_RULE, stages._safe_tools(stage))

    def test_other_stages_do_not_get_it(self):
        for stage in ("research", "plan", "report", "discover"):
            with self.subTest(stage=stage):
                self.assertNotIn(stages.SMS_TOOL_RULE, stages._safe_tools(stage))
        self.assertNotIn(stages.SMS_TOOL_RULE, stages.FALLBACK_RUNNER_TOOLS)

    def test_research_and_plan_stay_read_only(self):
        self.assertEqual(stages.STAGE_TOOLS["research"], "Read Write")
        self.assertEqual(stages.STAGE_TOOLS["plan"], "Read Write")

    def test_delegate_gets_no_broad_runner(self):
        granted = stages.STAGE_TOOLS["delegate"].split()
        for broad in ("Bash(uv:*)", "Bash(python3:*)", "Bash(python:*)", "Bash(bash:*)"):
            with self.subTest(rule=broad):
                self.assertNotIn(broad, granted)

    def test_rule_is_one_space_free_token(self):
        self.assertEqual(stages.SMS_TOOL_RULE, "Bash(macp-sms:*)")
        self.assertNotIn(" ", stages.SMS_TOOL_RULE)
        self.assertEqual(stages._safe_tools("delegate").count(stages.SMS_TOOL_RULE), 1)
        for bad in stages.FORBIDDEN_TOOL_SUBSTRINGS:
            self.assertNotIn(bad, stages.SMS_TOOL_RULE)
        self.assertEqual(task_validate._check_bash_patterns(stages.SMS_TOOL_RULE), [])

    def test_task_override_keeps_the_sms_rule(self):
        self.assertEqual(stages._safe_tools("implement", "Read,Bash(gcloud:*)"),
                         ["Read", "Bash(gcloud:*)", stages.SMS_TOOL_RULE,
                          stages.BROWSER_TOOL_RULE, stages.REMOTE_GATE_TOOL_RULE,
                          stages.EMULATOR_TOOL_RULE, stages.MAIL_TOOL_RULE])
        # Already listed by the task: exactly once.
        self.assertEqual(
            stages._safe_tools("implement", f"Read,{stages.SMS_TOOL_RULE}"),
            ["Read", stages.SMS_TOOL_RULE, stages.BROWSER_TOOL_RULE,
             stages.REMOTE_GATE_TOOL_RULE, stages.EMULATOR_TOOL_RULE,
             stages.MAIL_TOOL_RULE])
        # Stages a task cannot widen still return their own default untouched.
        self.assertEqual(stages._safe_tools("verify", "Read,Bash(gcloud:*)"),
                         stages.STAGE_TOOLS["verify"].split())

    @unittest.skipUnless(CLAUDE_MD.is_file(),
                         "vault-only: the engine-only export tree has no vault CLAUDE.md")
    def test_claude_md_section_11_names_the_wrapper(self):
        section = _section_11(CLAUDE_MD.read_text(encoding="utf-8"))
        self.assertIn(f"{stages.SMS_CLI} wait-code", section)
        self.assertIn(stages.SMS_TOOL_RULE, section)
        self.assertNotIn("uv run --directory", section)

    @unittest.skipUnless(WRAPPER.is_file(), "scripts/macp-sms not in this tree")
    def test_wrapper_runs_from_any_cwd(self):
        self.assertEqual(WRAPPER.name, stages.SMS_CLI)
        self.assertTrue(os.access(WRAPPER, os.X_OK), "scripts/macp-sms must be 0755")
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "MACP_REPO": str(REPO_ROOT),
                   "MACP_SMS_DIR": str(Path(tmp) / "sms")}
            env.pop("PYTHONPATH", None)
            seed = subprocess.run(
                ["bash", str(WRAPPER), "ingest"], cwd=tmp, env=env,
                input='{"from": "0000", "body": "[TEST] 123456", "id": "fixture-1"}',
                capture_output=True, text=True, timeout=30)
            self.assertEqual(seed.returncode, 0, seed.stderr)
            out = subprocess.run(
                ["bash", str(WRAPPER), "list", "--since", "1m"], cwd=tmp, env=env,
                capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("[TEST] 123456", out.stdout)


if __name__ == "__main__":
    unittest.main()
