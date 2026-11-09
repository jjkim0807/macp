"""Tests for the unapplied prompt-proposal judgement (Worker/proposals/<slug>/ →
dashboard human `todo` rows). Every test builds a fake engine root in a tmp dir."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from todo_system.orchestrator import proposals

HUMAN_KEYS = {"slug", "title", "action", "why", "due", "not_before", "urgent",
              "later", "category", "project"}


class PendingProposalsTest(unittest.TestCase):
    def setUp(self):
        proposals._CACHE.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / ".claude" / "commands").mkdir(parents=True)
        (self.root / "Worker" / "proposals").mkdir(parents=True)

    def tearDown(self):
        proposals._CACHE.clear()
        self._tmp.cleanup()

    # ---- fixtures -------------------------------------------------------
    def _proposal(self, slug="demo-slug", files=None, readme=None,
                  layout="commands") -> Path:
        d = self.root / "Worker" / "proposals" / slug
        sub = d / (".claude/commands" if layout == "dotclaude" else "commands")
        sub.mkdir(parents=True, exist_ok=True)
        for name, body in (files or {}).items():
            (sub / name).write_text(body, encoding="utf-8")
        if readme is not None:
            (d / "README.md").write_text(readme, encoding="utf-8")
        return d

    def _dest(self, name, body):
        (self.root / ".claude" / "commands" / name).write_text(body, encoding="utf-8")

    # ---- judgement ------------------------------------------------------
    def test_differs_gives_one_row(self):
        self._proposal(files={"foo.md": "A\n"})
        self._dest("foo.md", "B\n")
        rows = proposals.pending_proposals(self.root)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["slug"], "demo-slug")
        self.assertEqual(r["why"], "todo")
        self.assertEqual(r["category"], "dev")
        self.assertEqual(r["project"], "macp")
        self.assertIs(r["urgent"], True)
        self.assertIs(r["later"], False)
        self.assertEqual(r["due"], "")
        self.assertEqual(r["not_before"], "")

    def test_identical_gives_no_row(self):
        self._proposal(files={"foo.md": "same\n"})
        self._dest("foo.md", "same\n")
        self.assertEqual(proposals.pending_proposals(self.root), [])

    def test_one_of_many_differs_is_unapplied(self):
        self._proposal(files={"foo.md": "same\n", "bar.md": "new\n"})
        self._dest("foo.md", "same\n")
        self._dest("bar.md", "old\n")
        self.assertEqual(len(proposals.pending_proposals(self.root)), 1)

    def test_missing_destination_is_unapplied(self):
        self._proposal(files={"brand-new.md": "x\n"})
        rows = proposals.pending_proposals(self.root)
        self.assertEqual([r["slug"] for r in rows], ["demo-slug"])

    def test_dotclaude_layout_is_accepted(self):
        self._proposal(files={"foo.md": "A\n"}, layout="dotclaude")
        self._dest("foo.md", "B\n")
        self.assertEqual(len(proposals.pending_proposals(self.root)), 1)

    def test_dir_without_command_files_is_ignored(self):
        d = self.root / "Worker" / "proposals" / "readme-only"
        d.mkdir()
        (d / "README.md").write_text("# nothing to apply\n", encoding="utf-8")
        self.assertEqual(proposals.pending_proposals(self.root), [])

    def test_readme_mapping_overrides_same_name_destination(self):
        readme = ("# demo\n\n```bash\n"
                  "cp Worker/proposals/demo-slug/commands/foo.md .claude/commands/bar.md\n"
                  "```\n")
        self._proposal(files={"foo.md": "body\n"}, readme=readme)
        self._dest("bar.md", "body\n")          # mapped destination already equal
        self.assertEqual(proposals.pending_proposals(self.root), [])

    def test_key_set_matches_human_rows(self):
        self._proposal(files={"foo.md": "A\n"})
        (row,) = proposals.pending_proposals(self.root)
        self.assertEqual(set(row), HUMAN_KEYS)

    # ---- row text -------------------------------------------------------
    def test_cp_line_and_summary_from_readme(self):
        readme = ("# Demo proposal (a person applies)\n\n"
                  "Adds one step to the planner prompt.\n"
                  "Nothing else changes.\n\n"
                  "```bash\n"
                  "cp Worker/proposals/demo-slug/commands/foo.md .claude/commands/foo.md\n"
                  "```\n")
        self._proposal(files={"foo.md": "A\n"}, readme=readme)
        (row,) = proposals.pending_proposals(self.root)
        self.assertTrue(row["action"].startswith(
            "cp Worker/proposals/demo-slug/commands/foo.md .claude/commands/foo.md"))
        self.assertIn("Adds one step", row["action"])
        self.assertLessEqual(len(row["action"]), 110)
        self.assertIn("demo-slug", row["title"])

    def test_cp_line_synthesised_without_readme(self):
        self._proposal(files={"foo.md": "A\n"})
        (row,) = proposals.pending_proposals(self.root)
        self.assertTrue(row["action"].startswith(
            "cp Worker/proposals/demo-slug/commands/foo.md .claude/commands/foo.md"),
            row["action"])

    def test_conditional_proposal_shows_condition_and_is_not_urgent(self):
        readme = ("# Demo\n\n"
                  "Wires a memory lookup step.\n\n"
                  "golden-set 평가가 권장일 때만 적용한다.\n")
        self._proposal(files={"foo.md": "A\n"}, readme=readme)
        (row,) = proposals.pending_proposals(self.root)
        self.assertIs(row["urgent"], False)
        self.assertIn("권장일 때만", row["action"])

    def test_conditional_english_keyword(self):
        readme = "# Demo\n\nApply only if the eval table says so.\n"
        self._proposal(files={"foo.md": "A\n"}, readme=readme)
        (row,) = proposals.pending_proposals(self.root)
        self.assertIs(row["urgent"], False)
        self.assertIn("only if", row["action"])

    # ---- fail-soft ------------------------------------------------------
    def test_missing_root_gives_empty(self):
        self.assertEqual(proposals.pending_proposals(self.root / "nope"), [])

    def test_no_proposals_dir_gives_empty(self):
        shutil.rmtree(self.root / "Worker")
        self.assertEqual(proposals.pending_proposals(self.root), [])

    def test_garbage_does_not_raise(self):
        d = self._proposal(files={"ok.md": "A\n"})
        (d / "commands" / "weird.md").mkdir()   # a directory named like a command
        (self.root / "Worker" / "proposals" / "stray-file.md").write_text("x")
        rows = proposals.pending_proposals(self.root)
        self.assertEqual([r["slug"] for r in rows], ["demo-slug"])

    def test_internal_error_gives_empty(self):
        self._proposal(files={"foo.md": "A\n"})
        with unittest.mock.patch.object(proposals, "_judge",
                                        side_effect=RuntimeError("boom")):
            self.assertEqual(proposals.pending_proposals(self.root), [])

    # ---- cache ----------------------------------------------------------
    def test_unchanged_second_call_reads_no_files(self):
        self._proposal(files={"foo.md": "A\n"}, readme="# x\n\nsummary.\n")
        self._dest("foo.md", "B\n")
        first = proposals.pending_proposals(self.root)
        with unittest.mock.patch.object(proposals, "_read",
                                        side_effect=AssertionError("re-read")):
            second = proposals.pending_proposals(self.root)
        self.assertEqual(first, second)
        self.assertEqual(len(second), 1)

    def test_returned_rows_do_not_poison_cache(self):
        self._proposal(files={"foo.md": "A\n"})
        (row,) = proposals.pending_proposals(self.root)
        row["group"] = "mutated"
        (again,) = proposals.pending_proposals(self.root)
        self.assertNotIn("group", again)

    def test_row_clears_after_cp_and_after_dir_removal(self):
        d = self._proposal(files={"foo.md": "applied body\n"})
        self._dest("foo.md", "old body!!!!\n")    # same size as the proposal
        self.assertEqual(len(proposals.pending_proposals(self.root)), 1)
        dest = self.root / ".claude" / "commands" / "foo.md"
        dest.write_bytes((d / "commands" / "foo.md").read_bytes())    # the human `cp`
        t = dest.stat().st_mtime_ns + 10**9
        os.utime(dest, ns=(t, t))
        self.assertEqual(proposals.pending_proposals(self.root), [])
        # re-diverge, then remove the proposal dir → row gone
        dest.write_text("diverged\n", encoding="utf-8")
        os.utime(dest, ns=(t + 10**9, t + 10**9))
        self.assertEqual(len(proposals.pending_proposals(self.root)), 1)
        shutil.rmtree(d)
        self.assertEqual(proposals.pending_proposals(self.root), [])


if __name__ == "__main__":
    unittest.main()
