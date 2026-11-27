"""Orchestrator side of a decision request: collect() records a reversible pick
and parks only an irreversible question (decision 2026-11-20).

* FAIL + `decided` (the runner's reversible pick, no `blocked_on`): the task
  md's Progress Log gets one 「워커 결정」 line, the row is retried like any
  FAIL — never awaiting_human, no alert.
* FAIL + `blocked_on` + `decision` (an irreversible gate): parks as before and
  the ledger row keeps the normalized request for the dashboard question.
* RESULT + `decided`: the line lands before the close.

Same layout as test_orchestrator_superseded: a temp vault, FileMailbox, Ledger,
recorders for notify / alert_route, cli.ITEMS_DIR pointed at the temp vault.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system import cli
from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import message as m
from todo_system.orchestrator import stages, tasks
from todo_system.orchestrator.assignment import Assignment, Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import ALERT_BLOCKED, Orchestrator
from todo_system.orchestrator.registry import Agent, Registry
from todo_system.orchestrator.tests.test_decision_request import SEND, load_case

SLUG = "decide-task"
TASK_PATH = f"Todo/items/{SLUG}.md"
CASE = load_case()
DECIDED = stages.decision_record(stages.normalize_decision(CASE["decision_block"]))


def _md(status: str = "open") -> str:
    return "\n".join(["---", "tags:", "  - todo", f"status: {status}", "worker_mode: auto",
                      "updated: 2026-01-01", 'outcome: "x"', "---", "", "# decide task", "",
                      "## Progress Log", "", "- 2026-01-01: created", ""])


class _Fixture(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"MACP_LANGUAGE": "ko"})
        p.start()
        self.addCleanup(p.stop)
        self.vault = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        self.items = self.vault / "Todo" / "items"
        self.items.mkdir(parents=True)
        self.md = self.items / f"{SLUG}.md"
        self.mb = FileMailbox(self.vault / "Worker" / "agents")
        self.ledger = Ledger(self.vault / "Worker" / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=[SLUG])])
        self.sent: list = []
        self.routed: list = []
        self.orch = Orchestrator(self.mb, self.reg, self.ledger,
                                 notify=lambda text, link=None: self.sent.append(text),
                                 alert_route=lambda text, **kw: (self.routed.append(text)
                                                                 or ("send", text)))
        patcher = mock.patch.object(cli, "ITEMS_DIR", self.items)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _row(self, state=A.IN_PROGRESS) -> None:
        self.ledger.save(Assignment(task_id=SLUG, agent_id="w", corr_id="C1", kind="ai-local",
                                    state=state,
                                    payload={"task_path": TASK_PATH, "summary": "x",
                                             "worker_mode": "auto"}))

    def _reply(self, type_: str, **payload) -> Assignment:
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=type_, frm="w", task_id=SLUG,
                                corr_id="C1", payload=payload))
        self.orch.collect()
        return self.ledger.load(SLUG)

    def decided_lines(self) -> list[str]:
        return [ln for ln in self.md.read_text(encoding="utf-8").splitlines()
                if "워커 결정" in ln]


class ReversibleChoiceIsRecorded(_Fixture):
    def test_a_fail_with_a_pick_is_recorded_and_retried_not_parked(self):
        self.md.write_text(_md(), encoding="utf-8")
        self._row()
        a = self._reply(m.FAIL, verify="FAIL", decided=DECIDED,
                        reason="verify FAIL — reversible choice, the worker picked: x")
        self.assertNotEqual(a.state, A.AWAITING_HUMAN)
        self.assertEqual(a.blocked_on, "")
        self.assertEqual(a.decision, {})
        self.assertIn(a.state, (A.DISPATCHED, A.FAILED))   # the ordinary retry path
        self.assertEqual(self.sent, [])
        self.assertEqual(self.routed, [])
        [line] = self.decided_lines()
        self.assertTrue(line.startswith("- 20"), line)
        self.assertIn("워커 결정(되돌릴 수 있는 선택이라 묻지 않음): PR #50을 그대로 받기", line)
        self.assertIn("질문: " + DECIDED["question"], line)
        self.assertIn("이유: " + DECIDED["reason"], line)
        # the line sits in the Progress Log, after the existing entry
        text = self.md.read_text(encoding="utf-8")
        self.assertLess(text.index("- 2026-01-01: created"), text.index(line))
        self.assertIn("worker-decided", [h.get("type") for h in a.history])

    def test_the_same_pick_twice_is_one_line(self):
        self.md.write_text(_md(), encoding="utf-8")
        for _ in range(2):
            self._row()
            self._reply(m.FAIL, verify="FAIL", decided=DECIDED, reason="x")
        self.assertEqual(len(self.decided_lines()), 1)

    def test_a_result_with_a_pick_records_it_before_the_close(self):
        self.md.write_text(_md(), encoding="utf-8")
        self._row()
        a = self._reply(m.RESULT, decided=DECIDED, review_required=False,
                        summary="staged pipeline complete")
        self.assertEqual(a.state, A.DONE)
        text = self.md.read_text(encoding="utf-8")
        self.assertEqual(tasks.task_status(SLUG), "done")
        [line] = self.decided_lines()
        self.assertLess(text.index(line), text.index(tasks.CLOSE_TRACE))

    def test_a_missing_md_or_a_bad_payload_never_breaks_collect(self):
        self._row()
        a = self._reply(m.FAIL, verify="FAIL", decided=DECIDED, reason="x")   # no md at all
        self.assertNotEqual(a.state, A.AWAITING_HUMAN)
        [mark] = [h for h in a.history if h.get("type") == "worker-decided"]
        self.assertFalse(mark["written"])
        self.md.write_text(_md(), encoding="utf-8")
        for bad in ("a string", {"choice": ""}, {"question": "q"}, ["list"]):
            with self.subTest(bad=bad):
                self._row()
                self._reply(m.FAIL, verify="FAIL", decided=bad, reason="x")
                self.assertEqual(self.decided_lines(), [])

    def test_a_progress_reply_carrying_decided_writes_nothing(self):
        self.md.write_text(_md(), encoding="utf-8")
        self._row()
        self._reply(m.PROGRESS, decided=DECIDED, note="still going")
        self.assertEqual(self.decided_lines(), [])


class IrreversibleQuestionParks(_Fixture):
    def test_a_gated_request_parks_and_the_ledger_keeps_the_question(self):
        self.md.write_text(_md(), encoding="utf-8")
        self._row()
        request = stages.normalize_decision(SEND)
        a = self._reply(m.FAIL, verify="FAIL", blocked_on="지원서 제출은 사람 몫",
                        decision=request, reason="blocked on 지원서 제출은 사람 몫 — not a defect")
        self.assertEqual(a.state, A.AWAITING_HUMAN)
        self.assertEqual(a.last_alert_kind, ALERT_BLOCKED)
        self.assertEqual(a.decision, request)
        self.assertEqual(self.decided_lines(), [])
        # the copy survives the ledger round trip and clears on the next reply
        self.assertEqual(Ledger(self.ledger.root).load(SLUG).decision, request)
        a.apply_reply(m.Envelope(to=m.ORCHESTRATOR, type=m.ACCEPT, frm="w", task_id=SLUG,
                                 corr_id="C1", payload={}))
        self.assertEqual(a.decision, {})

    def test_a_ledger_row_from_an_older_build_has_no_decision(self):
        self._row()
        raw = (self.ledger.root / f"{SLUG}.json").read_text(encoding="utf-8")
        old = raw.replace('"decision": {},', "")
        self.assertNotIn('"decision"', old)
        (self.ledger.root / f"{SLUG}.json").write_text(old, encoding="utf-8")
        self.assertEqual(self.ledger.load(SLUG).decision, {})


class AppendProgressEntry(unittest.TestCase):
    def setUp(self):
        self.items = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.items, ignore_errors=True)

    def test_creates_the_section_when_absent_and_is_idempotent(self):
        p = self.items / "t.md"
        p.write_text("---\nstatus: open\n---\n\n# t\n", encoding="utf-8")
        self.assertTrue(tasks.append_progress_entry("t", "- 2026-11-27: one", self.items))
        self.assertTrue(tasks.append_progress_entry("t", "- 2026-11-27: one", self.items))
        self.assertEqual(p.read_text(encoding="utf-8"),
                         "---\nstatus: open\n---\n\n# t\n\n## Progress Log\n\n- 2026-11-27: one\n")
        self.assertTrue(tasks.append_progress_entry("t", "- 2026-11-27: two", self.items))
        self.assertTrue(p.read_text(encoding="utf-8").endswith("one\n- 2026-11-27: two\n"))

    def test_missing_md_or_empty_entry_is_false(self):
        self.assertFalse(tasks.append_progress_entry("nope", "- x", self.items))
        (self.items / "t.md").write_text("x\n", encoding="utf-8")
        self.assertFalse(tasks.append_progress_entry("t", "   ", self.items))

    def test_a_file_that_keeps_moving_is_not_written(self):
        p = self.items / "t.md"
        p.write_text("a\n", encoding="utf-8")
        reads = iter(f"v{i}\n" for i in range(100))
        with mock.patch.object(Path, "read_text", lambda self, **kw: next(reads)):
            self.assertFalse(tasks.append_progress_entry("t", "- x", self.items))
        self.assertEqual(p.read_bytes(), b"a\n")


if __name__ == "__main__":
    unittest.main()
