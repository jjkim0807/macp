"""Decision requests — the ```decision block a stage writes (stages.parse_decision_request).

The block is how a Verify or Report stage hands the engine a choice the plan did
not settle. The engine reads it deterministically (no LLM): a request whose
`gate` names one of the five irreversible steps of decision 2026-11-20 is a
person's decision; any other request is a reversible choice the worker makes
itself, so the engine records the recommended option instead of parking.

The real case is the 2026-11-27 park (decision_cases/visit_store_park.json,
anonymised): Verify parked a task as "human decision" over names and a bound
that differed from the plan. That is a reversible design difference, so under
the new rule it is the worker's pick.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from todo_system.orchestrator import stages

_CASE = Path(__file__).resolve().parent / "decision_cases" / "visit_store_park.json"


def load_case() -> dict:
    return json.loads(_CASE.read_text(encoding="utf-8"))


def block(obj) -> str:
    """`obj` as the fenced block a stage writes."""
    body = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=1)
    return f"```decision\n{body}\n```\n"


def verify_md(req, *, status: str = "fail", blocked_on: str | None = "x") -> str:
    """A verify.md with frontmatter, prose, and (when `req` is given) the block."""
    fm = ["---", "stage: verify", f"overall_status: {status}"]
    if blocked_on is not None:
        fm.append(f'blocked_on: "{blocked_on}"')
    fm.append("---")
    text = "\n".join(fm) + "\n\n# Verification\n\n## Conclusion\n\nprose.\n\n"
    return text + (block(req) if req is not None else "")


SEND = {"question": "지원서를 지금 제출할까요?",
        "background": ["초안은 다 썼고 마감은 내일이에요."],
        "options": [{"label": "지금 제출", "consequence": "되돌릴 수 없어요."},
                    {"label": "내일 아침에 다시 보기", "consequence": "마감 전에 한 번 더 읽어요."}],
        "recommended": 2, "reason": "제출은 되돌릴 수 없어서요.", "gate": "send"}


class ParseBlock(unittest.TestCase):
    def test_the_real_case_block_is_read_whole(self):
        case = load_case()
        req = stages.parse_decision_request(verify_md(case["decision_block"]))
        want = case["decision_block"]
        self.assertEqual(req["question"], want["question"])
        self.assertEqual(req["background"], want["background"])
        self.assertEqual(req["options"], want["options"])
        self.assertEqual((req["recommended"], req["reason"]), (1, want["reason"]))
        self.assertEqual(req["gate"], "")          # "none" = reversible

    def test_the_real_verify_md_has_no_block(self):
        # the 11-27 verify.md as it really was: an English blocked_on, no block
        text = load_case()["run"]["files"]["verify.md"]
        self.assertIsNotNone(stages.parse_blocked_on(text))
        self.assertIsNone(stages.parse_decision_request(text))

    def test_unusable_blocks_are_none(self):
        good = load_case()["decision_block"]
        for bad in ("{not json", "[1, 2]", "null",
                    {**good, "question": "  "},
                    {**good, "question": 3},
                    {**good, "options": [{"label": "only one"}]},
                    {**good, "options": [{"label": ""}, {"consequence": "no label"}]},
                    {**good, "options": "a, b"}):
            with self.subTest(bad=bad):
                self.assertIsNone(stages.parse_decision_request(verify_md(bad)))
        self.assertIsNone(stages.parse_decision_request(""))
        self.assertIsNone(stages.parse_decision_request(None))

    def test_only_a_decision_fence_counts(self):
        good = load_case()["decision_block"]
        text = "```json\n" + json.dumps(good) + "\n```\n"
        self.assertIsNone(stages.parse_decision_request(text))
        self.assertIsNotNone(stages.parse_decision_request("  ```decision\n"
                                                           + json.dumps(good) + "\n  ```\n"))

    def test_the_first_block_wins(self):
        first = {**SEND, "question": "첫째 질문?"}
        text = verify_md(first) + block({**SEND, "question": "둘째 질문?"})
        self.assertEqual(stages.parse_decision_request(text)["question"], "첫째 질문?")

    def test_text_is_one_line_and_clipped(self):
        req = stages.parse_decision_request(verify_md({
            "question": "  여러\n줄   질문?  " + "가" * 300,
            "background": "한 줄 배경",            # a bare string is one line
            "options": ["버튼만 있는 선택지", {"label": "나" * 120, "consequence": "다" * 300},
                        {"label": "셋째"}, {"label": "넷째"}, {"label": "다섯째"},
                        {"label": "여섯째"}, {"label": "일곱째"}],
            "reason": 7}))
        self.assertTrue(req["question"].startswith("여러 줄 질문? 가"))
        self.assertEqual(len(req["question"]), stages.DECISION_QUESTION_CHARS)
        self.assertEqual(req["background"], ["한 줄 배경"])
        self.assertEqual(req["options"][0], {"label": "버튼만 있는 선택지", "consequence": ""})
        self.assertEqual(len(req["options"][1]["label"]), stages.DECISION_LABEL_CHARS)
        self.assertEqual(len(req["options"][1]["consequence"]), stages.DECISION_LINE_CHARS)
        self.assertEqual(len(req["options"]), stages.DECISION_OPTIONS_MAX)
        self.assertEqual(req["reason"], "")       # not a string
        self.assertEqual(req["recommended"], 0)   # missing

    def test_background_keeps_three_lines(self):
        req = stages.parse_decision_request(verify_md(
            {**SEND, "background": ["하나", "", "둘", 3, "셋", "넷"]}))
        self.assertEqual(req["background"], ["하나", "둘", "셋"])

    def test_recommended_must_point_at_an_option(self):
        for rec, want in ((1, 1), (2, 2), (3, 0), (0, 0), (-1, 0), (True, 0), ("1", 0), (1.0, 0)):
            with self.subTest(rec=rec):
                req = stages.parse_decision_request(verify_md({**SEND, "recommended": rec}))
                self.assertEqual(req["recommended"], want)

    def test_gate_is_a_closed_set(self):
        self.assertEqual(stages.DECISION_GATES, ("money", "send", "public", "delete", "identity"))
        for gate in stages.DECISION_GATES:
            with self.subTest(gate=gate):
                req = stages.parse_decision_request(verify_md({**SEND, "gate": f" {gate.upper()} "}))
                self.assertEqual(req["gate"], gate)
        for gate in ("none", "", "reversible", "rename", "①", None, 2):
            with self.subTest(gate=gate):
                req = stages.parse_decision_request(verify_md({**SEND, "gate": gate}))
                self.assertEqual(req["gate"], "")
        no_gate = {k: v for k, v in SEND.items() if k != "gate"}
        self.assertEqual(stages.parse_decision_request(verify_md(no_gate))["gate"], "")

    def test_normalize_is_idempotent(self):
        req = stages.parse_decision_request(verify_md(load_case()["decision_block"]))
        self.assertEqual(stages.normalize_decision(req), req)
        self.assertIsNone(stages.normalize_decision("not a dict"))


class Judgment(unittest.TestCase):
    """AC2 at the reading level: which requests are a person's decision."""

    def test_a_reversible_design_difference_is_the_workers_pick(self):
        # the 11-27 case: names and a bound that differ from the plan
        req = stages.parse_decision_request(verify_md(load_case()["decision_block"]))
        self.assertEqual(stages.decision_record(req), {
            "question": "지난 진료일 저장을 계획과 다른 이름(visit-date)·400일 제한으로 만든 "
                        "PR #50을 그대로 받을까요?",
            "choice": "PR #50을 그대로 받기",
            "reason": "이미 쓰이고 있고, 이름은 나중에 언제든 바꿀 수 있어서요."})

    def test_an_irreversible_step_stays_a_persons_decision(self):
        for gate in stages.DECISION_GATES:
            with self.subTest(gate=gate):
                req = stages.parse_decision_request(verify_md({**SEND, "gate": gate}))
                self.assertIsNone(stages.decision_record(req))

    def test_a_reversible_request_with_no_pick_stays_open(self):
        # nothing to record: the person picks
        req = stages.parse_decision_request(verify_md({**SEND, "gate": "none", "recommended": 0}))
        self.assertIsNone(stages.decision_record(req))

    def test_record_needs_a_usable_request(self):
        self.assertIsNone(stages.decision_record(None))
        self.assertIsNone(stages.decision_record({"question": "?", "options": []}))


if __name__ == "__main__":
    unittest.main()
