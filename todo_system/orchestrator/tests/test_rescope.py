"""Tests for rescope.py pure judgment functions. No files, no I/O."""

from __future__ import annotations

import unittest

from todo_system.orchestrator import rescope


class PlanDeclaresOversizeTest(unittest.TestCase):
    """`plan_declares_oversize` must fire ONLY on a plan that explicitly declares
    the task too big for one Implement stage, and must NOT fire on an ordinary
    plan — the template carries a `## Parallelizable subtasks` heading on every
    task, so a naive reuse of `_RESCOPE_PARK_KEYWORDS` (which matches the bare
    word "subtask") would skip Implement on everything."""

    def test_strong_positive_declaration_is_true(self):
        for txt in (
            "This task exceeds a single Implement stage — needs re-scoping.",
            "Recommendation: re-scope into 3 sub-tasks before implementing.",
            "## Conclusion\nThe work must be rescoped; one stage cannot land it.",
        ):
            self.assertTrue(rescope.plan_declares_oversize(txt), txt)

    def test_template_shaped_plan_is_false(self):
        # The real plan template: a `## Parallelizable subtasks` heading and the
        # bare word "subtask", but NO oversize declaration. Must stay False or
        # every task's Implement would be skipped.
        plan = (
            "# Implementation Plan\n"
            "## Phases\n"
            "### Phase 1: add helper\n- edit foo.py\n"
            "### Phase 2: wire it\n- edit bar.py\n"
            "## Parallelizable subtasks\n"
            "None. Phase 2 depends on Phase 1, so run the subtasks in order.\n"
        )
        self.assertFalse(rescope.plan_declares_oversize(plan))

    def test_negated_lines_are_false(self):
        for txt in (
            "This does not exceed a single implement stage.",
            "No re-scoping needed — one Implement stage is enough.",
            "re-scope 불필요: 한 스테이지로 충분하다.",
            "The plan does not require re-scoping.",
        ):
            self.assertFalse(rescope.plan_declares_oversize(txt), txt)

    def test_negator_is_line_local_not_document_wide(self):
        # A negator on one line must NOT cancel a genuine declaration on another.
        plan = (
            "Phase 1 does not exceed a single implement stage on its own.\n"
            "But taken together this task exceeds a single implement stage.\n"
        )
        self.assertTrue(rescope.plan_declares_oversize(plan))

    def test_code_and_conditional_mentions_are_false(self):
        # 2026-11-13: three design-doc plans were skipped as "oversize" by these
        # exact shapes — none of them declared the task too big.
        for txt in (
            # an inline-code file citation
            "  `todo_system/orchestrator/rescope.py` (split→leaf pattern as a model),",
            # indented grep commands naming the file / alternating on the word
            '     grep -n "\\.claude/commands\\|allowed_tools\\|rescope\\|followups" docs/concepts.md\n'
            '     grep -n "_GUARDRAIL_PATHS" todo_system/orchestrator/rescope.py',
            # a conditional wrapped across lines
            "must exist with its table. Only if the orchestrator ever has to\n"
            "rescope, the hand-split boundary is Phase 2 end / Phase 3 start.",
            # fenced code
            "```\nrescope.json written by verify\n```",
            "만약 re-scope가 필요하면 Phase 2에서 자른다.",
            # substrings of other words (1065-plan audit, 2026-11-14)
            "Run the care-scoped subset of the suite.",
            "Add `requireScope` and the agent scopes table.",
        ):
            self.assertFalse(rescope.plan_declares_oversize(txt), txt)

    def test_declaration_wrapped_across_lines_is_true(self):
        plan = ("## Conclusion\nThis task is too large; it exceeds a single\n"
                "implement stage.\n")
        self.assertTrue(rescope.plan_declares_oversize(plan))

    def test_empty_and_none_are_false(self):
        self.assertFalse(rescope.plan_declares_oversize(""))
        self.assertFalse(rescope.plan_declares_oversize(None))  # type: ignore[arg-type]

    def test_is_oversize_rescope_park_still_matches_subtask(self):
        # Contraction guard: the existing park classifier is UNCHANGED — it still
        # matches the bare word "subtask" in a blocked_on string. plan_declares_
        # oversize is a separate, stricter function; it must not have altered this.
        self.assertTrue(rescope.is_oversize_rescope_park(
            "needs re-scoping into 3 sub-tasks"))
        self.assertTrue(rescope.is_oversize_rescope_park("", "split into subtasks"))
        self.assertFalse(rescope.plan_declares_oversize(
            "Break the work into 3 subtasks."))  # "subtask" alone ≠ declaration


if __name__ == "__main__":
    unittest.main()
