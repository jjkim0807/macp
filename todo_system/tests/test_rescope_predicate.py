"""Pure-function coverage for the auto-rescope decision + plan discovery (AC3).

Each "hand to a human" reason in `can_auto_rescope` is pinned by one otherwise-
valid plan that trips exactly that branch, plus the happy AUTO path. No I/O: the
predicate takes a plan dict + a parent frontmatter dict. The discovery helpers
(`load_rescope` / `find_latest_rescope`) get their own tmp-dir tests, including
the slug-suffix trap the #54 run-glob bug taught.

Identifiers are neutral on purpose (`example` repos): new files under
`todo_system/` are scanned by `scripts/check_public_identifiers.sh`.
"""

from __future__ import annotations

import json
from pathlib import Path

from todo_system.orchestrator import rescope


def _subtask(slug: str, **over) -> dict:
    base = {
        "slug": slug,
        "title": f"title for {slug}",
        "outcome": f"outcome for {slug}",
        "acceptance_criteria": ["does the thing", "tests green"],
        "files": ["src/a.py"],
    }
    base.update(over)
    return base


def _plan(n: int = 3, *, parent_slug: str = "big-task", **over) -> dict:
    plan = {
        "version": rescope.RESCOPE_SCHEMA_VERSION,
        "parent_slug": parent_slug,
        "reason": "exceeds a single Implement stage",
        "design_notes": ["decided fact A", "decided fact B"],
        "subtasks": [_subtask(f"big-task-part-{i}") for i in range(n)],
    }
    plan.update(over)
    return plan


def _parent(**over) -> dict:
    fm = {
        "auto_merge": True,
        "repo": "https://github.com/example/Repo.git",
        "if_then": "worker가 이 태스크를 잡으면 분할안대로 집행",
        "tags": ["todo", "worker"],
        "allowed_tools": "Read,Edit,Write,Bash(git:*)",
        "model_tier": "heavy",
    }
    fm.update(over)
    return fm


# -- happy path -------------------------------------------------------------


def test_happy_auto_allows():
    d = rescope.can_auto_rescope(_plan(3), _parent())
    assert d.allow is True
    assert "3 subtasks" in d.reason


def test_happy_auto_two_and_five_subtasks_ok():
    assert rescope.can_auto_rescope(_plan(2), _parent()).allow is True
    assert rescope.can_auto_rescope(_plan(5), _parent()).allow is True


# -- "hand to a human" branches (one per reason) ----------------------------


def test_none_plan_is_human():
    d = rescope.can_auto_rescope(None, _parent())
    assert d.allow is False
    assert d.reason == "no/broken rescope plan"


def test_non_dict_plan_is_human():
    assert rescope.can_auto_rescope(["not", "a", "dict"], _parent()).allow is False


def test_too_few_subtasks_is_human():
    d = rescope.can_auto_rescope(_plan(1), _parent())
    assert d.allow is False
    assert "2..5 subtasks" in d.reason


def test_too_many_subtasks_is_human():
    assert rescope.can_auto_rescope(_plan(6), _parent()).allow is False


def test_subtasks_not_a_list_is_human():
    assert rescope.can_auto_rescope(_plan(subtasks="nope"), _parent()).allow is False


def test_subtask_missing_required_key_is_human():
    plan = _plan(3)
    del plan["subtasks"][1]["outcome"]
    d = rescope.can_auto_rescope(plan, _parent())
    assert d.allow is False
    assert d.reason.startswith("subtask malformed")


def test_subtask_empty_acceptance_is_human():
    plan = _plan(3)
    plan["subtasks"][0]["acceptance_criteria"] = []
    d = rescope.can_auto_rescope(plan, _parent())
    assert d.allow is False
    assert "acceptance_criteria" in d.reason


def test_subtask_duplicate_slug_is_human():
    plan = _plan(3)
    plan["subtasks"][2]["slug"] = plan["subtasks"][0]["slug"]
    d = rescope.can_auto_rescope(plan, _parent())
    assert d.allow is False
    assert "duplicate" in d.reason


def test_lineage_depth_reached_via_arg_is_human():
    d = rescope.can_auto_rescope(_plan(3), _parent(), lineage_depth_reached=True)
    assert d.allow is False
    assert "depth cap 1" in d.reason


def test_lineage_depth_reached_via_parent_fm_is_human():
    # a task that was itself born of an auto-rescope carries auto_rescoped_from
    d = rescope.can_auto_rescope(_plan(3), _parent(auto_rescoped_from="grandparent-task"))
    assert d.allow is False
    assert "depth cap 1" in d.reason


def test_parent_auto_merge_false_is_human():
    for falsey in (False, "false", "no", "off", "0"):
        d = rescope.can_auto_rescope(_plan(3), _parent(auto_merge=falsey))
        assert d.allow is False, falsey
        assert "auto_merge:false" in d.reason


def test_if_then_human_confirmation_is_human():
    d = rescope.can_auto_rescope(_plan(3), _parent(if_then="머지 전 사람 확인 필요"))
    assert d.allow is False
    assert "human confirmation" in d.reason


def test_live_account_repo_marker_is_human():
    d = rescope.can_auto_rescope(
        _plan(3), _parent(repo="https://github.com/example/ai-stock-trading.git"))
    assert d.allow is False
    assert "real-account" in d.reason


def test_live_account_tag_is_human():
    d = rescope.can_auto_rescope(_plan(3), _parent(tags=["todo", "live"]))
    assert d.allow is False
    assert "real-account" in d.reason


def test_guardrail_in_subtask_files_is_human():
    plan = _plan(3)
    plan["subtasks"][1]["files"] = ["Worker/planner_policy.yaml"]
    d = rescope.can_auto_rescope(plan, _parent())
    assert d.allow is False
    assert "guardrail" in d.reason


def test_guardrail_in_parent_outcome_is_human():
    d = rescope.can_auto_rescope(
        _plan(3), _parent(outcome="edit Worker/planner_policy.yaml safely"))
    assert d.allow is False
    assert "guardrail" in d.reason


# -- discovery helpers ------------------------------------------------------


def test_load_rescope_missing_and_broken_return_none(tmp_path: Path):
    assert rescope.load_rescope(tmp_path) is None            # no file
    (tmp_path / rescope.RESCOPE_FILENAME).write_text("{not json", encoding="utf-8")
    assert rescope.load_rescope(tmp_path) is None            # broken json
    (tmp_path / rescope.RESCOPE_FILENAME).write_text("[1,2]", encoding="utf-8")
    assert rescope.load_rescope(tmp_path) is None            # json but not a dict


def test_load_rescope_reads_a_dict(tmp_path: Path):
    (tmp_path / rescope.RESCOPE_FILENAME).write_text(json.dumps(_plan(2)), encoding="utf-8")
    got = rescope.load_rescope(tmp_path)
    assert isinstance(got, dict) and got["parent_slug"] == "big-task"


def _mk_run(runs: Path, run_id: str, plan: dict | None) -> Path:
    d = runs / run_id
    d.mkdir(parents=True)
    if plan is not None:
        (d / rescope.RESCOPE_FILENAME).write_text(json.dumps(plan), encoding="utf-8")
    return d


def test_find_latest_rescope_exact_slug_and_latest(tmp_path: Path):
    runs = tmp_path / "runs"
    _mk_run(runs, "20261010T100000-big-task", _plan(2))
    newer = _mk_run(runs, "20261010T200000-big-task", _plan(3))
    found = rescope.find_latest_rescope(runs, "big-task")
    assert found is not None
    run_dir, plan = found
    assert run_dir == newer                      # latest by timestamp-ordered name
    assert len(plan["subtasks"]) == 3


def test_find_latest_rescope_does_not_match_slug_suffix(tmp_path: Path):
    # the #54 trap: slug "big" must NOT pick up "big-task"'s run dir
    runs = tmp_path / "runs"
    _mk_run(runs, "20261010T200000-big-task", _plan(3))
    assert rescope.find_latest_rescope(runs, "big") is None


def test_find_latest_rescope_skips_runs_without_a_plan(tmp_path: Path):
    runs = tmp_path / "runs"
    _mk_run(runs, "20261010T200000-big-task", None)          # newer, no plan
    older = _mk_run(runs, "20261010T100000-big-task", _plan(2))
    found = rescope.find_latest_rescope(runs, "big-task")
    assert found is not None and found[0] == older


def test_plan_is_auto_viable_gate(tmp_path: Path):
    runs = tmp_path / "runs"
    run_dir = _mk_run(runs, "20261010T200000-big-task", _plan(3))
    state = rescope._rescoped_state_path(tmp_path)
    # viable
    assert rescope.plan_is_auto_viable(
        run_dir, _parent(), state_path=state, parent_slug="big-task") is True
    # wrong parent_slug -> not viable
    assert rescope.plan_is_auto_viable(
        run_dir, _parent(), state_path=state, parent_slug="other") is False
    # already recorded -> not viable (idempotent)
    rescope._record_rescoped(state, "big-task", ["a", "b"])
    assert rescope.plan_is_auto_viable(
        run_dir, _parent(), state_path=state, parent_slug="big-task") is False
