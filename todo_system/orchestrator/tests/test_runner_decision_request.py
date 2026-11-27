"""Runner side of a decision request — `StagedExecutor._execute`, for real.

Decision 2026-11-20: only an irreversible step is a person's decision. The
Verify stage's ```decision block (stages.parse_decision_request) decides how a
FAIL that names a blocker leaves the runner:

* a reversible choice (no gate) with a recommended option is NOT a park: the
  FAIL carries no `blocked_on`, only the pick as `decided`, so the orchestrator
  records it and retries like any other FAIL;
* a request that names a gate parks as before and carries the normalized
  request as `decision` (the ledger copy the dashboard asks from);
* a blocker with no block parks exactly as before.

A run that finishes carries a reversible pick (`decided`) or an irreversible
question (`decision`) on its RESULT too.

The pipeline is the real one (test_runner_full_verify_path's sandbox): real git
against a throwaway origin, the real prompts, only the stage commands stubbed to
write the artifacts below. The 2026-11-27 park is the reversible case.
"""
from __future__ import annotations

import json

import pytest

from todo_system.orchestrator import stages
from todo_system.orchestrator.tests.test_decision_request import SEND, block, load_case
from todo_system.orchestrator.tests.test_runner_full_verify_path import PR_URL, _Sandbox

BLOCKER = load_case()["ledger"]["blocked_on"]


def _verify_fail(req=None) -> str:
    text = ("---\nstage: verify\noverall_status: fail\nconfidence: high\n"
            f"blocked_on: {json.dumps(BLOCKER)}\n---\n\n# verify\n\n")
    return text + (block(req) if req is not None else "")


@pytest.fixture
def box(tmp_path, monkeypatch) -> _Sandbox:
    return _Sandbox(tmp_path, monkeypatch)


def test_a_reversible_choice_is_not_a_park(box):
    # the 11-27 case: names and a bound that differ from the plan
    box.artifacts["verify"] = _verify_fail(load_case()["decision_block"])
    out = box.execute(model_tier="heavy")
    assert out.ok is False
    assert box.stages_run() == ["implement", "verify"]        # no report after a FAIL
    assert "blocked_on" not in out.payload                     # → retried, not parked
    assert "decision" not in out.payload
    assert out.payload["decided"] == {
        "question": load_case()["decision_block"]["question"],
        "choice": "PR #50을 그대로 받기",
        "reason": load_case()["decision_block"]["reason"]}
    assert out.payload["verify"] == "FAIL"
    assert "not a defect" not in out.summary
    assert "PR #50을 그대로 받기" in out.summary


@pytest.mark.parametrize("gate", stages.DECISION_GATES)
def test_an_irreversible_request_parks_and_carries_the_question(box, gate):
    box.artifacts["verify"] = _verify_fail({**SEND, "gate": gate})
    out = box.execute()
    assert out.ok is False
    assert out.payload["blocked_on"] == BLOCKER
    assert out.summary == f"blocked on {BLOCKER} — not a defect"
    assert out.payload["decision"] == stages.normalize_decision({**SEND, "gate": gate})
    assert "decided" not in out.payload


def test_a_reversible_request_with_no_pick_parks_with_its_question(box):
    box.artifacts["verify"] = _verify_fail({**SEND, "gate": "none", "recommended": 0})
    out = box.execute()
    assert out.payload["blocked_on"] == BLOCKER
    assert out.payload["decision"]["question"] == SEND["question"]
    assert "decided" not in out.payload


def test_a_blocker_without_a_block_parks_as_before(box):
    # the 11-27 verify.md as it really was
    box.artifacts["verify"] = load_case()["run"]["files"]["verify.md"]
    out = box.execute()
    assert out.ok is False
    assert out.payload["blocked_on"] == BLOCKER
    assert out.summary == f"blocked on {BLOCKER} — not a defect"
    assert "decision" not in out.payload and "decided" not in out.payload


def test_a_finished_run_carries_its_reversible_pick(box):
    req = load_case()["decision_block"]
    box.artifacts["verify"] = ("---\noverall_status: pass\nconfidence: high\n---\n\n"
                               "# verify\n\n" + block(req))
    out = box.execute()
    assert out.ok, out.summary
    assert out.payload["pr_url"] == PR_URL
    assert out.payload["decided"]["choice"] == "PR #50을 그대로 받기"
    assert "decision" not in out.payload


def test_a_held_review_carries_the_irreversible_question(box):
    report = ("---\noverall_status: pass\nmerged: no\nworker_mode_recommendation: review\n"
              f"pr_url: {PR_URL}\n---\n\n# report\n\n" + block(SEND))
    box.artifacts["report"] = report
    box.merge_landed.return_value = False
    out = box.execute()
    assert out.payload["review_required"] is True
    assert out.payload["decision"] == stages.normalize_decision(SEND)
    assert "decided" not in out.payload


def test_a_finished_run_without_a_block_carries_neither(box):
    out = box.execute()
    assert out.ok, out.summary
    assert "decided" not in out.payload and "decision" not in out.payload
