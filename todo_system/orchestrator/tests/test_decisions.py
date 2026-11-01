"""Tests for the decision action-item sweep.

Everything runs on temp fixtures. `Todo/items/` is gitignored, so a test that
read the real vault would pass on this machine and fail in CI against an empty
directory — and the sweep's whole job is to distinguish "no such task" from
"task exists", which is exactly the distinction a missing directory destroys.
Both directories are therefore injected into every call.

The fixtures mirror shapes measured in `Decisions/` on 2026-08-18: frontmatter
with `tags/date/status` only (3 files), frontmatter with `affects_tasks` in bare
and wikilink form (3 files), and ADRs with no frontmatter at all (2 files).
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from todo_system.orchestrator import decisions

ADR_NO_FRONTMATTER = """\
# ADR: 오케스트레이터 데이터 SSOT 및 버전관리 정책

**Status**: Accepted
**Date**: 2026-06-13

## Context
"""


class DecisionsTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.decisions = self.tmp / "Decisions"
        self.items = self.tmp / "items"
        self.decisions.mkdir()
        self.items.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_decision(self, name: str, body: str) -> Path:
        p = self.decisions / f"{name}.md"
        p.write_text(body, encoding="utf-8")
        return p

    def decision(self, name: str, frontmatter: str, body: str = "\n# 결정\n") -> Path:
        return self.write_decision(name, f"---\n{frontmatter}---{body}")

    def task(self, slug: str, status: str = "open") -> Path:
        p = self.items / f"{slug}.md"
        p.write_text(f"---\ntags:\n  - todo\nstatus: {status}\n---\n\n# {slug}\n",
                     encoding="utf-8")
        return p

    def issues(self):
        return decisions.find_action_item_issues(self.decisions, self.items)

    def kinds(self):
        return [r["kind"] for r in self.issues()]


class FrontmatterReaderTest(DecisionsTestBase):
    def test_adr_without_frontmatter_is_not_an_error(self):
        """2 of 8 real decision files are ADRs with no frontmatter block."""
        p = self.write_decision("adr", ADR_NO_FRONTMATTER)
        self.assertEqual(decisions._read_frontmatter(p), (decisions.FM_NONE, {}))

    def test_plain_frontmatter_parsed(self):
        p = self.decision("d", "tags:\n  - decision\ndate: 2026-07-29\nstatus: recorded\n")
        kind, fm = decisions._read_frontmatter(p)
        self.assertEqual(kind, decisions.FM_OK)
        self.assertEqual(fm["status"], "recorded")

    def test_broken_yaml_is_broken_not_absent(self):
        """The distinction is the product: un-migrated vs. actually malformed."""
        p = self.decision("bad", "tags: [unclosed\n  - nope\n")
        self.assertEqual(decisions._read_frontmatter(p)[0], decisions.FM_BROKEN)

    def test_unclosed_fence_is_broken(self):
        p = self.write_decision("nofence", "---\ntags:\n  - decision\n\n# 본문만 있음\n")
        self.assertEqual(decisions._read_frontmatter(p)[0], decisions.FM_BROKEN)

    def test_empty_block_reads_as_absent(self):
        p = self.write_decision("empty", "---\n---\n\n# 본문\n")
        self.assertEqual(decisions._read_frontmatter(p), (decisions.FM_NONE, {}))

    def test_scalar_frontmatter_is_broken(self):
        p = self.write_decision("scalar", "---\njust a string\n---\n\n# 본문\n")
        self.assertEqual(decisions._read_frontmatter(p)[0], decisions.FM_BROKEN)

    def test_missing_file_never_raises(self):
        self.assertEqual(
            decisions._read_frontmatter(self.tmp / "nope.md")[0], decisions.FM_BROKEN)


class NormalizeSlugTest(unittest.TestCase):
    def test_bare_slug(self):
        """08-16 and 08-17 write `affects_tasks` as bare slugs (7 refs)."""
        self.assertEqual(
            decisions._normalize_slug("ai-stock-trading-5-strategy-ai"),
            "ai-stock-trading-5-strategy-ai")

    def test_wikilink(self):
        """05-19 writes all 8 of its refs as quoted wikilinks."""
        self.assertEqual(decisions._normalize_slug("[[withdrawal-process]]"),
                         "withdrawal-process")

    def test_aliased_wikilink(self):
        self.assertEqual(decisions._normalize_slug("[[withdrawal-process|자퇴 절차]]"),
                         "withdrawal-process")

    def test_path_qualified_wikilink(self):
        self.assertEqual(decisions._normalize_slug("[[Todo/items/foo|표시]]"), "foo")

    def test_md_suffix_stripped(self):
        self.assertEqual(decisions._normalize_slug("foo.md"), "foo")

    def test_absent_forms(self):
        for v in ("", "  ", "-", None, 42, ["x"]):
            self.assertEqual(decisions._normalize_slug(v), "", repr(v))


class UndeclaredVsExplicitlyEmptyTest(DecisionsTestBase):
    """Acceptance criterion 4 — the two must not collapse into one bucket."""

    def test_missing_key_is_undeclared(self):
        self.decision("d", "tags:\n  - decision\ndate: 2026-07-29\nstatus: recorded\n")
        self.assertEqual(self.kinds(), ["undeclared"])

    def test_no_frontmatter_at_all_is_undeclared(self):
        self.write_decision("adr", ADR_NO_FRONTMATTER)
        self.assertEqual(self.kinds(), ["undeclared"])

    def test_none_sentinel_is_not_an_issue(self):
        self.decision("d", "action_items: none\n")
        self.assertEqual(self.issues(), [])

    def test_empty_list_is_not_an_issue(self):
        self.decision("d", "action_items: []\n")
        self.assertEqual(self.issues(), [])

    def test_bare_key_is_declared_not_undeclared(self):
        """`action_items:` with no value — the key was typed, so it counts."""
        self.decision("d", "action_items:\n")
        self.assertEqual(self.issues(), [])

    def test_three_states_are_distinguishable(self):
        self.decision("undeclared-one", "status: recorded\n")
        self.decision("explicit-none", "action_items: none\n")
        self.decision("explicit-empty", "action_items: []\n")
        self.assertEqual([(r["decision"], r["kind"]) for r in self.issues()],
                         [("undeclared-one", "undeclared")])


class OrphanDetectionTest(DecisionsTestBase):
    """Acceptance criterion 2."""

    def test_orphan_slug_surfaced(self):
        self.decision("d", "action_items:\n"
                           "  - what: 반증 스파이크\n"
                           "    task: worldgraph-falsification-spike\n"
                           "    status: open\n")
        [row] = self.issues()
        self.assertEqual(row["kind"], "orphan")
        self.assertEqual(row["task"], "worldgraph-falsification-spike")
        self.assertEqual(row["what"], "반증 스파이크")

    def test_existing_task_is_clean_and_carries_status(self):
        self.task("real-task", status="open")
        self.decision("d", "action_items:\n"
                           "  - what: 할 일\n    task: real-task\n    status: open\n")
        self.assertEqual(self.issues(), [])
        [row] = [r for r in decisions.scan(self.decisions, self.items)]
        self.assertEqual(row["kind"], "ok")
        self.assertEqual(row["task_status"], "open")

    def test_existing_but_closed_task_reports_its_status(self):
        """Criterion 2's second half — the task's state, where it is load-bearing."""
        self.task("finished", status="done")
        self.decision("d", "action_items:\n"
                           "  - what: 할 일\n    task: finished\n    status: open\n")
        [row] = self.issues()
        self.assertEqual(row["kind"], "closed_task")
        self.assertEqual(row["task_status"], "done")

    def test_cancelled_task_status_reported_verbatim(self):
        """`cancelled` is not in cli.py's status choices; the raw string survives."""
        self.task("scrapped", status="cancelled")
        self.decision("d", "action_items:\n"
                           "  - what: 할 일\n    task: scrapped\n    status: open\n")
        [row] = self.issues()
        self.assertEqual(row["kind"], "closed_task")
        self.assertEqual(row["task_status"], "cancelled")

    def test_wikilink_task_resolves_to_the_real_file(self):
        self.task("withdrawal-process")
        self.decision("d", 'action_items:\n'
                           '  - what: 절차 확인\n'
                           '    task: "[[withdrawal-process]]"\n'
                           '    status: open\n')
        self.assertEqual(self.issues(), [])

    def test_task_none_means_no_task_needed(self):
        self.decision("d", "action_items:\n"
                           "  - what: 태스크 필요 없음\n    task: none\n    status: open\n")
        self.assertEqual(self.issues(), [])

    def test_blank_task_is_empty_task(self):
        self.decision("d", 'action_items:\n'
                           '  - what: 무언가\n    task: ""\n    status: open\n')
        self.assertEqual(self.kinds(), ["empty_task"])

    def test_dash_sentinel_task_is_empty_task(self):
        """CLAUDE.md writes "no value" as "-"; it must not read as a slug."""
        self.decision("d", 'action_items:\n'
                           '  - what: 무언가\n    task: "-"\n    status: open\n')
        self.assertEqual(self.kinds(), ["empty_task"])

    def test_missing_task_key_is_empty_task(self):
        self.decision("d", "action_items:\n  - what: 무언가\n    status: open\n")
        self.assertEqual(self.kinds(), ["empty_task"])

    def test_resolved_item_excluded_from_orphan_check(self):
        """A finished action is not chased, however broken its link."""
        self.decision("d", "action_items:\n"
                           "  - what: 이미 함\n    task: ghost\n    status: resolved\n")
        self.assertEqual(self.issues(), [])

    def test_dropped_item_excluded(self):
        self.decision("d", "action_items:\n"
                           "  - what: 안 하기로 함\n    task: ghost\n    status: dropped\n")
        self.assertEqual(self.issues(), [])

    def test_open_item_with_ghost_task_still_caught(self):
        self.decision("d", "action_items:\n"
                           "  - what: 아직 안 함\n    task: ghost\n    status: open\n")
        self.assertEqual(self.kinds(), ["orphan"])

    def test_multiple_items_classified_independently(self):
        self.task("exists")
        self.decision("d", "action_items:\n"
                           "  - what: A\n    task: exists\n    status: open\n"
                           "  - what: B\n    task: ghost\n    status: open\n"
                           "  - what: C\n    task: none\n    status: open\n"
                           "  - what: D\n    task: gone\n    status: resolved\n")
        self.assertEqual([(r["what"], r["kind"]) for r in self.issues()],
                         [("B", "orphan")])


class UnparseableTest(DecisionsTestBase):
    def test_broken_frontmatter_is_unparseable_not_undeclared(self):
        self.decision("bad", "tags: [unclosed\n  - nope\n")
        self.assertEqual(self.kinds(), ["unparseable"])

    def test_string_action_items_that_is_not_none(self):
        self.decision("d", "action_items: 나중에 적을게요\n")
        self.assertEqual(self.kinds(), ["unparseable"])

    def test_mapping_action_items_is_unparseable(self):
        self.decision("d", "action_items:\n  what: 목록이 아님\n")
        self.assertEqual(self.kinds(), ["unparseable"])

    def test_bare_string_list_item_is_unparseable(self):
        """The likeliest near-miss migration: a prose list under the key."""
        self.decision("d", "action_items:\n  - 반증 스파이크 돌리기\n")
        rows = self.issues()
        self.assertEqual([r["kind"] for r in rows], ["unparseable"])
        self.assertIn("반증 스파이크", rows[0]["note"])


class AffectsTasksBucketTest(DecisionsTestBase):
    """`affects_tasks` is reported separately and never merged."""

    def test_broken_affects_link_found_in_its_own_bucket(self):
        self.decision("d", "affects_tasks:\n  - ghost-task\naction_items: none\n")
        self.assertEqual(self.issues(), [])
        [row] = decisions.find_affects_tasks_orphans(self.decisions, self.items)
        self.assertEqual(row["task"], "ghost-task")

    def test_wikilink_and_bare_forms_both_resolve(self):
        self.task("withdrawal-process")
        self.task("ai-stock-trading-5-strategy-ai")
        self.decision("d", 'affects_tasks:\n'
                           '  - "[[withdrawal-process]]"\n'
                           '  - ai-stock-trading-5-strategy-ai\n'
                           '  - "[[ghost|없는 것]]"\n'
                           'action_items: none\n')
        found = decisions.find_affects_tasks_orphans(self.decisions, self.items)
        self.assertEqual([r["task"] for r in found], ["ghost"])

    def test_affects_tasks_is_not_promoted_to_action_items(self):
        """It has no `what` and no resolution state — reusing it invents both."""
        self.decision("d", "affects_tasks:\n  - ghost-task\n")
        self.assertEqual(self.kinds(), ["undeclared"])   # NOT "orphan"

    def test_no_frontmatter_file_contributes_nothing(self):
        self.write_decision("adr", ADR_NO_FRONTMATTER)
        self.assertEqual(decisions.find_affects_tasks_orphans(self.decisions, self.items), [])


class ReadOnlyTest(DecisionsTestBase):
    """Acceptance criterion 5 — the sweep must not be able to change anything."""

    def test_sweep_mutates_no_file(self):
        self.decision("d", "action_items:\n"
                           "  - what: A\n    task: ghost\n    status: open\n")
        self.write_decision("adr", ADR_NO_FRONTMATTER)
        self.task("real-task")
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in sorted(self.tmp.rglob("*.md"))}

        decisions.find_action_item_issues(self.decisions, self.items)
        decisions.find_affects_tasks_orphans(self.decisions, self.items)
        decisions.scan(self.decisions, self.items)

        after = {p: (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in sorted(self.tmp.rglob("*.md"))}
        self.assertEqual(before, after)

    def test_sweep_creates_no_file(self):
        """Specifically: it must not create the task it failed to find."""
        self.decision("d", "action_items:\n"
                           "  - what: A\n    task: ghost\n    status: open\n")
        before = sorted(p.name for p in self.tmp.rglob("*"))
        decisions.find_action_item_issues(self.decisions, self.items)
        self.assertEqual(sorted(p.name for p in self.tmp.rglob("*")), before)
        self.assertFalse((self.items / "ghost.md").exists())


class ScanScopeTest(DecisionsTestBase):
    def test_readme_is_not_scanned_as_a_decision(self):
        """The schema doc lives in Decisions/ and must not report itself."""
        self.write_decision("README", "# Decisions/ — 행동 항목 스키마\n\n설명.\n")
        self.decision("real", "action_items: none\n")
        self.assertEqual(decisions.scan(self.decisions, self.items)[0]["decision"], "real")
        self.assertEqual(len(decisions.scan(self.decisions, self.items)), 1)

    def test_empty_directory_is_empty_not_an_error(self):
        self.assertEqual(self.issues(), [])

    def test_non_markdown_ignored(self):
        (self.decisions / "notes.txt").write_text("action_items: none", encoding="utf-8")
        self.assertEqual(self.issues(), [])

    def test_orphans_rank_above_undeclared(self):
        """A capped view must never bury the broken link under migration TODOs."""
        for i in range(8):
            self.decision(f"z-undeclared-{i}", "status: recorded\n")
        self.decision("a-has-orphan", "action_items:\n"
                                      "  - what: 반증 스파이크\n"
                                      "    task: ghost\n    status: open\n")
        rows = self.issues()
        self.assertEqual(rows[0]["kind"], "orphan")
        self.assertEqual(len(rows), 9)


class RenderSectionTest(DecisionsTestBase):
    def test_empty_renders_none(self):
        self.assertEqual(decisions.render_section([]), ["- 없음"])

    def test_cap_and_truncation_count(self):
        for i in range(8):
            self.decision(f"d{i}", "status: recorded\n")
        rows = self.issues()
        self.assertEqual(len(rows), 8)
        lines = decisions.render_section(rows, limit=5)
        self.assertEqual(len(lines), 6)                    # 5 shown + 1 tail
        self.assertIn("외 3건 더", lines[-1])
        self.assertIn("총 8건", lines[-1])                  # total, not just the cut

    def test_no_tail_when_under_cap(self):
        for i in range(3):
            self.decision(f"d{i}", "status: recorded\n")
        lines = decisions.render_section(self.issues(), limit=5)
        self.assertEqual(len(lines), 3)
        self.assertNotIn("외", "".join(lines))

    def test_line_carries_decision_kind_and_task(self):
        self.decision("worldgraph-no-go", "action_items:\n"
                                          "  - what: 9월 반증 스파이크\n"
                                          "    task: worldgraph-falsification-spike\n"
                                          "    status: open\n")
        [line] = decisions.render_section(self.issues())
        self.assertIn("`worldgraph-no-go`", line)
        self.assertIn("[orphan]", line)
        self.assertIn("9월 반증 스파이크", line)
        self.assertIn("worldgraph-falsification-spike", line)


class RealVaultShapeTest(DecisionsTestBase):
    """The measured 2026-08-18 shape of Decisions/, reproduced as fixtures.

    Pins the behaviour the migration depends on: today every real decision is
    undeclared, and none of them is an orphan or a crash.
    """

    def test_all_eight_real_shapes_classify_as_undeclared(self):
        self.decision("2026-05-19-withdrawal",
                      'tags:\n  - decision\ndecision_date: 2026-05-19\n'
                      'decision_status: accepted\n'
                      'affects_tasks:\n  - "[[withdrawal-process]]"\n'
                      'supersedes: "-"\n')
        self.decision("2026-08-16-strategy",
                      "tags:\n  - decision\ndecision_date: 2026-08-16\n"
                      "affects_tasks:\n  - ai-stock-trading-5-strategy-ai\n")
        self.decision("2026-07-29-worldgraph-no-go",
                      "tags:\n  - decision\n  - private\ndate: 2026-07-29\n"
                      "status: recorded\n")
        self.write_decision("2026-06-orchestrator-data-ssot", ADR_NO_FRONTMATTER)

        rows = self.issues()
        self.assertEqual({r["kind"] for r in rows}, {"undeclared"})
        self.assertEqual(len(rows), 4)
        self.assertIn("2026-07-29-worldgraph-no-go", {r["decision"] for r in rows})

    def test_worldgraph_no_go_becomes_an_orphan_once_migrated(self):
        """Criterion 6's orphan path, on a copy — the real file is never touched.

        The September falsification spike has no task in `Todo/items/` (404
        files checked, 2026-08-18), so the moment a human writes the schema
        block the sweep flags it.
        """
        self.decision("2026-07-29-worldgraph-no-go",
                      "tags:\n  - decision\n  - private\ndate: 2026-07-29\n"
                      "status: recorded\n"
                      "action_items:\n"
                      "  - what: 9월 주말 1회(~12h) 반증 스파이크 — 숫자 두 개만 측정\n"
                      "    task: worldgraph-falsification-spike\n"
                      "    status: open\n")
        [row] = self.issues()
        self.assertEqual(row["kind"], "orphan")
        self.assertEqual(row["task"], "worldgraph-falsification-spike")

    def test_keyword_lookalike_task_does_not_satisfy_the_link(self):
        """`worldgraph-sqlite-store-and-bugfixes` mentions the spike in its body
        but is a prerequisite task, not the spike. Slug links only."""
        self.task("worldgraph-sqlite-store-and-bugfixes", status="done")
        self.decision("2026-07-29-worldgraph-no-go",
                      "action_items:\n"
                      "  - what: 9월 반증 스파이크\n"
                      "    task: worldgraph-falsification-spike\n"
                      "    status: open\n")
        self.assertEqual(self.kinds(), ["orphan"])


class DashboardSectionTest(DecisionsTestBase):
    """The surfacing hook. A detector nothing calls is the failure this task
    exists to fix, so the wiring is tested as carefully as the sweep."""

    def setUp(self):
        super().setUp()
        from todo_system.orchestrator.assignment import Ledger
        from todo_system.orchestrator.registry import Registry
        self.led = Ledger(self.tmp / "assignments")
        self.reg = Registry([])

    def _dashboard(self):
        from todo_system.orchestrator import dashboard
        return dashboard

    def test_section_rendered_with_the_issue(self):
        self.decision("worldgraph-no-go", "action_items:\n"
                                          "  - what: 반증 스파이크\n"
                                          "    task: ghost\n    status: open\n")
        md = self._dashboard().render(self.led, self.reg, None,
                                      self.decisions, self.items)
        self.assertIn("## 📌 결정문 행동 항목", md)
        self.assertIn("worldgraph-no-go", md)

    def test_section_precedes_completed_list(self):
        """The brief stops reading at ✅ 완료, so order is load-bearing."""
        self.decision("d", "status: recorded\n")
        md = self._dashboard().render(self.led, self.reg, None,
                                      self.decisions, self.items)
        self.assertLess(md.index("📌 결정문 행동 항목"), md.index("## ✅ 완료"))

    def test_unwired_paths_are_not_an_error(self):
        """Existing two-arg callers must keep working, and must not read the vault."""
        md = self._dashboard().render(self.led, self.reg)
        self.assertIn("## 📌 결정문 행동 항목", md)
        self.assertNotIn("스윕 실패", md)

    def test_broken_dir_fails_soft_but_visibly(self):
        md = self._dashboard().render(self.led, self.reg, None,
                                      self.tmp / "nope", self.items)
        self.assertIn("## ✅ 완료", md)              # rest still rendered
        self.assertNotIn("스윕 실패", md)            # a missing dir globs to empty

    def test_unreadable_decisions_dir_reports_failure(self):
        """A hard error must be loud, never swallowed into 없음."""
        md = self._dashboard().render(self.led, self.reg, None, object(), self.items)
        self.assertIn("스윕 실패", md)
        self.assertIn("## ✅ 완료", md)

    def test_write_passes_paths_through(self):
        self.decision("d", "action_items:\n"
                           "  - what: X\n    task: ghost\n    status: open\n")
        out = self.tmp / "Orchestrator.md"
        self._dashboard().write(self.led, self.reg, str(out), None,
                                self.decisions, self.items)
        self.assertIn("ghost", out.read_text(encoding="utf-8"))


class OrchestratorCliWiringTest(unittest.TestCase):
    """Both dashboard writers must accept the paths — a section that renders in
    only one of them is a section nobody trusts."""

    def _help(self, subcommand: str) -> str:
        import contextlib
        import io

        from todo_system.orchestrator import orchestrator

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            orchestrator._main([subcommand, "--help"])
        return buf.getvalue()

    def test_dashboard_declares_the_flags(self):
        help_text = self._help("dashboard")
        self.assertIn("--decisions-dir", help_text)
        self.assertIn("--items-dir", help_text)

    def test_serve_declares_the_flags(self):
        """`serve` is the coexistence tick; skipping it would render the section
        in only one of the two writers."""
        help_text = self._help("serve")
        self.assertIn("--decisions-dir", help_text)
        self.assertIn("--items-dir", help_text)


class DecisionsCliHelpTest(unittest.TestCase):
    def test_help_exits_zero(self):
        self.assertEqual(decisions._main(["--help"]), 0)


if __name__ == "__main__":
    unittest.main()
