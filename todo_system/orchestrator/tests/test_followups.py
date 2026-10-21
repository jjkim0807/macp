"""Tests for durable report follow-up pickup (followups.py).

The load-bearing regression is the acceptance's durable half: a follow-up that is
recorded and NOT resolved must still be visible after a full planner cycle — the
"조용히 증발" path this task closes. The state paths are monkeypatched onto a
tempdir because the real ones (Worker/_state/, Todo/items/) are gitignored live
state a test must never touch — the same reason the fail-open preservation tests
inject their paths.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from todo_system.orchestrator import followups


# Both follow-up sections carry a real bullet; "무엇을 했는가" is not a follow-up
# section (ignored), and the nested sub-bullet under 사람이 검토 must be excluded.
REPORT_BOTH = """---
stage: report
slug: demo
---

# Worker Run Report — demo

## 무엇을 했는가

- 뭔가 했다

## 사람이 검토해야 할 부분

- 검토 필요: fail-open 분기 수동 확인
  - 이 서브불릿은 무시된다

## 다음 단계

- 후속 A: 무언가를 재측정
- 후속 B: 별도 태스크로 분리
"""

# 사람이 검토 section is the "없음" sentinel → skipped; only 다음 단계 yields an item.
REPORT_EMPTY_REVIEW = """---
stage: report
---

## 사람이 검토해야 할 부분

- 없음

## 다음 단계

- 후속 하나만 있다
"""

TS = "2026-01-01T00:00:00"


class FollowupsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.items = self.tmp / "items"
        self.items.mkdir()
        self._orig = (followups.FOLLOWUPS_PATH, followups.ITEMS_DIR,
                      followups.REJECTIONS_PATH)
        followups.FOLLOWUPS_PATH = self.tmp / "_state" / "followups.jsonl"
        followups.ITEMS_DIR = self.items
        followups.REJECTIONS_PATH = self.tmp / "_state" / "followups_rejected.jsonl"

    def tearDown(self):
        (followups.FOLLOWUPS_PATH, followups.ITEMS_DIR,
         followups.REJECTIONS_PATH) = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _append_both(self, task="demo", run="r1"):
        return followups.append_followups(
            REPORT_BOTH, source_task=task, source_run=run, ts=TS)

    # ---- parse ----------------------------------------------------------

    def test_parse_captures_both_sections_excludes_subbullets(self):
        items = followups.parse_report_followups(REPORT_BOTH)
        secs = [it["section"] for it in items]
        self.assertIn("다음 단계", secs)
        self.assertIn("사람이 검토해야 할 부분", secs)
        self.assertEqual(len(items), 3)                     # 1 review + 2 next
        self.assertTrue(all("서브불릿" not in it["text"] for it in items))
        self.assertTrue(all("뭔가 했다" not in it["text"] for it in items))

    def test_parse_skips_empty_marker_and_absent_sections(self):
        items = followups.parse_report_followups(REPORT_EMPTY_REVIEW)
        self.assertEqual([it["section"] for it in items], ["다음 단계"])
        self.assertTrue(all(not it["text"].startswith("없음") for it in items))
        self.assertEqual(
            followups.parse_report_followups("# title\n\nno sections here"), [])
        self.assertEqual(followups.parse_report_followups(""), [])

    def test_parse_korean_output_is_pinned(self):
        # Exact output and id for a Korean report — pinned before the English
        # aliases landed so Korean parsing stays byte-identical.
        self.assertEqual(followups.parse_report_followups(REPORT_BOTH), [
            {"section": "다음 단계", "text": "후속 A: 무언가를 재측정"},
            {"section": "다음 단계", "text": "후속 B: 별도 태스크로 분리"},
            {"section": "사람이 검토해야 할 부분", "text": "검토 필요: fail-open 분기 수동 확인"},
        ])
        self.assertEqual(
            followups._fid("demo", "다음 단계", "후속 A: 무언가를 재측정"), "1b18a845689b")
        self.assertEqual(followups.FOLLOWUP_SECTIONS,
                         ("다음 단계", "사람이 검토해야 할 부분"))

    # ---- append / dedup -------------------------------------------------

    def test_append_records_open_followups(self):
        ids = self._append_both()
        self.assertEqual(len(ids), 3)
        rows = followups.open_followups()
        self.assertEqual({r["id"] for r in rows}, set(ids))
        self.assertTrue(all(r["status"] == "open" and r["resolution"] is None
                            for r in rows))
        self.assertTrue(all(r["source_task"] == "demo" for r in rows))

    def test_reappend_is_idempotent_and_run_id_not_in_key(self):
        a = self._append_both(run="r1")
        # a DIFFERENT run re-parsing the SAME report must add nothing — the dedup
        # key is (task, section, text), deliberately not run_id.
        b = self._append_both(run="r2")
        self.assertEqual(len(a), 3)
        self.assertEqual(b, [])
        self.assertEqual(len(followups.load_followups()), 3)

    def test_no_followups_writes_nothing(self):
        got = followups.append_followups(
            "# report\n\n## 다음 단계\n\n- 없음\n",
            source_task="t", source_run="r", ts=TS)
        self.assertEqual(got, [])
        self.assertFalse(followups.FOLLOWUPS_PATH.exists())

    # ---- CORE regression: durable persistence across a cycle ------------

    def test_unmatched_followup_persists_across_a_cycle(self):
        ids = self._append_both()
        self.assertEqual(len(followups.open_followups()), 3)
        # one full planner cycle's reconcile with NOTHING matching (no created
        # task, no rejection) must close nothing…
        self.assertEqual(followups.reconcile(), [])
        # …and every follow-up is still open and still rendered into the context.
        still = followups.open_followups()
        self.assertEqual({r["id"] for r in still}, set(ids))
        ctx = followups.render_context(still)
        for i in ids:
            self.assertIn(i, ctx)

    # ---- mark_done / reconcile ------------------------------------------

    def test_done_followup_is_not_reopened_by_reappend(self):
        ids = self._append_both(run="r1")
        followups.mark_done([ids[0]], resolution="created: x")
        again = followups.append_followups(
            REPORT_BOTH, source_task="demo", source_run="r2", ts=TS)
        self.assertNotIn(ids[0], again)
        self.assertNotIn(ids[0], {r["id"] for r in followups.open_followups()})

    def test_reconcile_created_task_closes_followup(self):
        ids = self._append_both()
        target = ids[0]
        (self.items / "new-task.md").write_text(
            f"---\ntags:\n  - todo\nstatus: open\nfrom_followup: {target}\n---\n\n"
            "# new task\n", encoding="utf-8")
        self.assertEqual(followups.reconcile(), [target])
        rec = {r["id"]: r for r in followups.load_followups()}[target]
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["resolution"], "created: new-task")
        self.assertNotIn(target, {r["id"] for r in followups.open_followups()})

    def test_reconcile_rejection_closes_followup(self):
        ids = self._append_both()
        target = ids[-1]
        followups.REJECTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        followups.REJECTIONS_PATH.write_text(
            json.dumps({"id": target, "reason": "not actionable"}) + "\n",
            encoding="utf-8")
        self.assertEqual(followups.reconcile(), [target])
        rec = {r["id"]: r for r in followups.load_followups()}[target]
        self.assertEqual(rec["status"], "done")
        self.assertTrue(rec["resolution"].startswith("rejected: not actionable"))

    def test_from_followup_in_body_not_frontmatter_is_ignored(self):
        ids = self._append_both()
        # a mention in the BODY (not frontmatter) must not spuriously close it
        (self.items / "prose.md").write_text(
            f"---\ntags:\n  - todo\n---\n\nnote: from_followup: {ids[0]}\n",
            encoding="utf-8")
        self.assertEqual(followups.reconcile(), [])
        self.assertEqual(len(followups.open_followups()), 3)

    # ---- render / main --------------------------------------------------

    def test_render_empty_is_blank(self):
        self.assertEqual(followups.render_context([]), "")

    def test_render_lists_ids_and_resolve_protocol(self):
        ids = self._append_both()
        ctx = followups.render_context(followups.open_followups())
        for i in ids:
            self.assertIn(i, ctx)
        self.assertIn("from_followup", ctx)
        self.assertIn("followups_rejected.jsonl", ctx)

    def test_main_emit_context_and_unknown(self):
        self._append_both()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = followups.main(["emit-context"])
        self.assertEqual(rc, 0)
        self.assertIn("미처리 report 후속", buf.getvalue())
        # empty state → emit-context prints nothing (byte-identical no-op path)
        followups.mark_done([r["id"] for r in followups.open_followups()], "done")
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            followups.main(["emit-context"])
        self.assertEqual(buf2.getvalue(), "")
        self.assertEqual(followups.main(["bogus"]), 2)

    def test_main_reconcile_subcommand(self):
        ids = self._append_both()
        (self.items / "t.md").write_text(
            f"---\nfrom_followup: {ids[0]}\n---\n", encoding="utf-8")
        self.assertEqual(followups.main(["reconcile"]), 0)
        self.assertNotIn(ids[0], {r["id"] for r in followups.open_followups()})


if __name__ == "__main__":
    unittest.main()
