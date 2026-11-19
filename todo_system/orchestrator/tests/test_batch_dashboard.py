"""Orchestrator.md shows batch verify: a "📦 묶음 검증 대기" section for
`batch_ready` rows (only when there are any) and a "묶음 복귀" label in the
deferred section for rows the batch gate sent back.

The no-batch board is pinned to the output captured BEFORE dashboard.py learned
about batch rows (BASELINE below), so "no batch rows ⇒ same board" is checked
against the old renderer, not against itself.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import batch_gate, dashboard
from todo_system.orchestrator.assignment import Assignment, Ledger
from todo_system.orchestrator.registry import Agent, Registry

NOW = "2026-01-01T09:00:00"
REPO = "https://example.test/acme/app.git"

# render() output for _base_rows() captured from dashboard.py before this change.
BASELINE = (
    "---\ntags: [orchestrator, dashboard]\nupdated: 2026-01-01T09:00:00\n---\n\n"
    "# 🛰 Orchestrator\n\n"
    "> assignments: 4 · active 1 · awaiting-human 0 · deferred 1 · done 1 · failed 1\n\n"
    "## 🔀 Divergence (origin/main)\n- 없음\n\n"
    "## 🏃 진행 중\n- `core-loop` → **w** — watch: claude attach abc123\n\n"
    "## ⏳ 사람 대기 (parked)\n- 없음\n\n"
    "## 💤 대기 (deferred — 실패 아님, 자동 재시도)\n"
    "- `wait-turn` → **w** (체크아웃 대기) — sibling owns the checkout\n\n"
    "## ❌ 실패 (원인·재실패횟수)\n- `broke-b` → **w** (재실패 1) — tests red\n"
    "    ↳ run_dir: `Worker/runs/r1`\n\n"
    "## 🕰 방치된 사람 태스크 (마감 없음 · 사람 소유)\n- 없음\n\n"
    "## 📌 결정문 행동 항목 (미기재 · 끊긴 링크)\n- 없음\n\n"
    "## ✅ 완료\n- `old-thing` → **w**\n\n"
    "## 🤖 에이전트 (liveness)\n- ⚫ unknown **w** (ai-local) — *\n"
)
BATCH_HEADER = "## 📦 묶음 검증 대기 (통합 게이트 후 머지)"


def _base_rows(led: Ledger) -> None:
    led.save(Assignment(task_id="core-loop", agent_id="w", corr_id="c",
                        state=A.IN_PROGRESS, kind="ai-local",
                        payload={"attach": "claude attach abc123", "repo": REPO},
                        created_ts=NOW, updated_ts=NOW))
    led.save(Assignment(task_id="wait-turn", agent_id="w", corr_id="c",
                        state=A.FAILED, kind="ai-local", checkout_busy=True,
                        last_note="sibling owns the checkout",
                        created_ts=NOW, updated_ts=NOW))
    led.save(Assignment(task_id="broke-b", agent_id="w", corr_id="c",
                        state=A.FAILED, kind="ai-local", refail_count=1,
                        last_note="tests red", payload={"run_dir": "Worker/runs/r1"},
                        created_ts=NOW, updated_ts=NOW))
    led.save(Assignment(task_id="old-thing", agent_id="w", corr_id="c",
                        state=A.DONE, kind="ai-local",
                        created_ts=NOW, updated_ts=NOW))


def _registry() -> Registry:
    return Registry([Agent(id="w", kind="ai-local", projects=["*"])])


def _render(led: Ledger) -> str:
    with mock.patch.object(dashboard.m, "now_iso", return_value=NOW):
        return dashboard.render(led, _registry())


def _section(md: str, header: str) -> str:
    """Text from `header` up to the next '## ' heading."""
    rest = md.split(header, 1)[1]
    return rest.split("\n## ", 1)[0]


class BatchDashboardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.led = Ledger(self.tmp / "assignments")
        _base_rows(self.led)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_batch_rows(self) -> None:
        # One row moved by the real transition (carries batch_gate's last_note)…
        a = Assignment(task_id="batch-one", agent_id="w", corr_id="c",
                       state=A.IN_PROGRESS, kind="ai-local", payload={"repo": REPO},
                       created_ts=NOW, updated_ts=NOW)
        moved = batch_gate.mark_ready(
            a, {"pr_url": "https://example.test/acme/app/pull/7", "repo": REPO,
                batch_gate.KEY_BATCH: {"head_sha": "abc1234"}}, now_iso=NOW)
        self.assertTrue(moved)
        self.led.save(a)
        # …and one with no note at all.
        self.led.save(Assignment(task_id="batch-two", agent_id="w", corr_id="c",
                                 state=A.BATCH_READY, kind="ai-local",
                                 created_ts=NOW, updated_ts=NOW))

    # (a) no batch rows: byte-identical to the pre-change renderer
    def test_no_batch_rows_matches_pre_change_output(self):
        md = _render(self.led)
        self.assertEqual(md, BASELINE)
        self.assertNotIn("📦", md)
        self.assertNotIn("묶음", md)

    # (b) batch_ready rows get their own section, right after 진행 중
    def test_batch_section_lists_batch_ready_rows(self):
        self._add_batch_rows()
        md = _render(self.led)
        self.assertEqual(md.count(BATCH_HEADER), 1)
        expected_block = (
            "## 🏃 진행 중\n- `core-loop` → **w** — watch: claude attach abc123\n\n"
            f"{BATCH_HEADER}\n"
            "- `batch-one` → **w** — 묶음 검증 대기 — quick Verify 통과, 통합 게이트 뒤 머지\n"
            "- `batch-two` → **w**\n\n"
            "## ⏳ 사람 대기 (parked)"
        )
        self.assertIn(expected_block, md)
        # The rows appear nowhere else.
        outside = md.replace(_section(md, BATCH_HEADER), "")
        self.assertNotIn("batch-one", outside)
        self.assertNotIn("batch-two", outside)
        # Head counts unchanged except the total.
        self.assertIn("> assignments: 6 · active 1 · awaiting-human 0 · deferred 1 "
                      "· done 1 · failed 1", md)
        # Dropping the section block (and the total) gives back the baseline.
        stripped = md.replace(
            f"\n{BATCH_HEADER}\n"
            "- `batch-one` → **w** — 묶음 검증 대기 — quick Verify 통과, 통합 게이트 뒤 머지\n"
            "- `batch-two` → **w**\n", "")
        stripped = stripped.replace("> assignments: 6 ", "> assignments: 4 ")
        self.assertEqual(stripped, BASELINE)

    # (c) a row the batch gate sent back is a deferral with its own label
    def test_batch_returned_row_shows_in_deferred(self):
        a = Assignment(task_id="came-back", agent_id="w", corr_id="c",
                       state=A.BATCH_READY, kind="ai-local", payload={"repo": REPO},
                       created_ts=NOW, updated_ts=NOW)
        batch_gate.mark_returned(a, batch_gate.RETURN_GATE_FAILED, now_iso=NOW)
        self.led.save(a)
        md = _render(self.led)
        deferred = _section(md, "## 💤 대기 (deferred — 실패 아님, 자동 재시도)")
        self.assertIn("- `came-back` → **w** (묶음 복귀 — 개별 Verify 대기) — "
                      "묶음 복귀 — 개별 전체 Verify로 (gate-failed)", deferred)
        self.assertNotIn("came-back", _section(md, "## ❌ 실패 (원인·재실패횟수)"))
        self.assertNotIn(BATCH_HEADER, md)
        self.assertIn("deferred 2 · done 1 · failed 1", md)

    # (d) branch order: checkout_busy keeps its own label
    def test_checkout_busy_label_unchanged(self):
        md = _render(self.led)
        self.assertIn("- `wait-turn` → **w** (체크아웃 대기) — sibling owns the checkout", md)
        self.assertNotIn("묶음 복귀", md)

    def test_batch_line_tolerates_missing_fields(self):
        class Bare:
            task_id = "t"
            agent_id = "w"
        self.assertEqual(dashboard._batch_line(Bare()), "- `t` → **w**")


if __name__ == "__main__":
    unittest.main()
