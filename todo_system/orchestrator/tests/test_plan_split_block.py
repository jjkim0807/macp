"""Plan-authored split: the plan's own `rescope` block becomes the split plan.

A Plan stage that declares a task too big for one Implement stage can now carry
its split as ONE fenced block whose info string is `rescope` — each sub-task's
slug, the Phases it covers, its acceptance criteria and its `touches`. The engine
turns that block into the schema-v1 `rescope.json`, so the park is split without
a person even when the parent's criteria carry no `§N` keys (the only thing the
Phase-heading builder can map). This module pins:

- `find_plan_split_blocks` — which fences count as the plan's split block;
- `rescope_from_split_block` — the child shape, what is derived or inherited, and
  every fail-closed case (a block is used as written or not at all);
- `write_plan_rescope` — the block wins, an unusable block never falls back to
  the Phase-heading split, and every guard of the old path still applies;
- the generator — a child's own `touches` and `model_tier` win over the parent's.

The regression case is the 2026-11-18 plan this feature was built for: eight
Phases, six parent criteria without a single `§N` key, and a split into three
that had to be applied by hand. `fixtures/plan_split_1118/` holds a neutralised
copy of that plan, of its parent task md, and of the three hand-made children
(as `split.json`); every identifier in it is an `example` value.

The route-level wiring (runner plan-park → tick → collect() → execute_rescope)
is in test_plan_rescope_wiring.py.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from todo_system import cli, paths, worker
from todo_system import task_validate as _task_validate
from todo_system.orchestrator import model_select, repos, rescope
from todo_system.orchestrator.tests import test_plan_rescope as phase_path

FIX = Path(__file__).parent / "fixtures" / "plan_split_1118"
PARENT = "example-batch-verify-light-tasks"
RUN_ID = f"20261118T123354-{PARENT}"
CHILDREN = [
    "example-batch-verify-1-quick-verify-state",
    "example-batch-verify-2-integration-gate",
    "example-batch-verify-3-dashboard",
]
PLAN_1118 = (FIX / "implementation_plan.md").read_text(encoding="utf-8")
PARENT_MD = (FIX / "parent.md").read_text(encoding="utf-8")
SPLIT_1118 = json.loads((FIX / "split.json").read_text(encoding="utf-8"))
PARENT_TITLE = "macp — 가벼운 태스크는 묶어서 한 번에 검증 (통합 게이트)"
REPO_URL = "https://github.com/example/macp.git"
PLAN_REF = f"Worker/runs/{RUN_ID}/implementation_plan.md"
REQ_REF = f"Worker/runs/{RUN_ID}/requirements.md"
LABELS = ["1·2·3", "4·5·6", "7·8"]
PHASES_MARKER = "\n## Phases\n"
REQUIRED_KEYS = ("slug", "phases", "acceptance_criteria", "touches")

_UNSET = object()


def _fm(md_text: str) -> dict:
    """A task md's frontmatter as `repos._frontmatter` reads it (YAML types)."""
    return repos._parse_frontmatter_block(repos._frontmatter_block(md_text))[0]


def _one_line(text: str) -> str:
    return " ".join(text.split())


def block(split=None, *, fence: str = "```", info: str = "rescope") -> str:
    """One fenced split block, the way a plan carries it."""
    body = json.dumps(SPLIT_1118 if split is None else split, ensure_ascii=False, indent=1)
    return f"{fence}{info}\n{body}\n{fence}\n"


def with_block(plan_text: str, split=None, *, raw: str | None = None, **kw) -> str:
    """`plan_text` with a split block placed right before its `## Phases` section
    (`raw` is used verbatim instead of a rendered block)."""
    assert plan_text.count(PHASES_MARKER) == 1
    head, tail = plan_text.split(PHASES_MARKER)
    return head + "\n" + (block(split, **kw) if raw is None else raw) + PHASES_MARKER + tail


def _build(plan_text, fm=_UNSET, *, blocks=_UNSET, **over):
    """`(plan, why)` from the pure builder, fed the way the writer feeds it."""
    if blocks is _UNSET:
        blocks = rescope.find_plan_split_blocks(plan_text)
    kw = {"parent_slug": PARENT, "run_id": RUN_ID, "parent_title": PARENT_TITLE, **over}
    return rescope.rescope_from_split_block(
        blocks, plan_text, _fm(PARENT_MD) if fm is _UNSET else fm, **kw)


def _split(mutate=None) -> dict:
    """A deep copy of the 11-18 split, with `mutate(subtasks)` applied."""
    split = copy.deepcopy(SPLIT_1118)
    if mutate is not None:
        mutate(split["subtasks"])
    return split


def _set(i: int, key: str, value):
    def mutate(subs):
        subs[i][key] = value
    return mutate


def _drop(i: int, key: str):
    def mutate(subs):
        del subs[i][key]
    return mutate


def _minimal(subs) -> None:
    subs[:] = [{k: s[k] for k in REQUIRED_KEYS} for s in subs]


def _digest(plan: dict) -> str:
    return hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, indent=2).encode("utf-8")).hexdigest()


# -- the fixture, and why the 11-18 park reached a person ---------------------------


def test_fixture_is_the_1118_shape():
    assert rescope.plan_declares_oversize(PLAN_1118)
    assert len(rescope.parse_plan_phases(PLAN_1118)) == 8
    assert rescope._md_title(PARENT_MD) == PARENT_TITLE
    fm = _fm(PARENT_MD)
    assert fm["repo"] == REPO_URL and fm["auto_merge"] is True
    assert len(fm["acceptance_criteria"]) == 6
    assert not any(ac.lstrip().startswith("§") for ac in fm["acceptance_criteria"])
    assert len(fm["touches"]) == 8
    # the plan itself carries the split only as a table and bullets — no block
    assert "```rescope" not in PLAN_1118 and "~~~rescope" not in PLAN_1118

    subs = SPLIT_1118["subtasks"]
    assert [s["slug"] for s in subs] == CHILDREN
    assert [s["phases"] for s in subs] == [[1, 2, 3], [4, 5, 6], [7, 8]]
    assert [s["model_tier"] for s in subs] == ["heavy", "heavy", "standard"]
    assert [(s["estimate_p50"], s["estimate_p90"]) for s in subs] == [
        ("2h", "4h"), ("3h", "5h"), ("1h", "2h")]
    assert [len(s["acceptance_criteria"]) for s in subs] == [5, 5, 3]
    assert [len(s["touches"]) for s in subs] == [8, 4, 4]
    assert len({s["acceptance_criteria"][-1] for s in subs}) == 1    # the shared gate line
    assert [s["depends_on"] for s in subs] == [[], [CHILDREN[0]], [CHILDREN[1]]]
    # each child's write scope is its own: none of them is the parent's list
    assert all(s["touches"] != fm["touches"] for s in subs)


def test_1118_without_a_block_still_builds_nothing():
    """The 11-18 cause, kept as a control: with no block the Phase-heading builder
    has nothing to map the keyless criteria with."""
    assert rescope.rescope_from_plan_phases(
        PLAN_1118, _fm(PARENT_MD), parent_slug=PARENT, run_id=RUN_ID) is None


# -- the Phase-heading path is untouched ---------------------------------------------


def test_phase_path_leaf_fields_are_unchanged():
    """The block builder shares its leaf wording with the Phase-heading builder.
    These literals and digests were taken before that code was shared: the
    Phase-heading split of a plan without a block must stay byte-identical."""
    fm = phase_path._parent_fm()
    slug, rid = phase_path.PARENT, phase_path.RUN_ID
    plan_ref = f"Worker/runs/{rid}/implementation_plan.md"
    req_ref = f"Worker/runs/{rid}/requirements.md"
    plan = phase_path._build(phase_path.CHAT_SHAPE_PLAN, fm)
    subs = plan["subtasks"]

    assert list(plan) == ["version", "parent_slug", "source", "reason", "run_ref",
                          "design_notes", "subtasks"]
    assert list(subs[1]) == ["slug", "title", "outcome", "acceptance_criteria",
                             "next_action", "if_then", "spec", "steps",
                             "estimate_p50", "estimate_p90", "files"]
    assert subs[1]["outcome"] == (
        f"부모 {slug} 계획서 Phase 2(§3 routing) 범위가 머지돼 이 하위의 "
        "acceptance_criteria가 충족된다 — 범위 밖 절은 자리표시로 남기고 다음 하위가 채운다")
    assert subs[1]["spec"] == {
        "goal": "부모 계획서 Phase 2(§3 routing)만 수행한다",
        "scope_note": ("다른 Phase 범위는 건드리지 않는다 — 이미 머지된 앞 하위의 결과는 "
                       "고치지 않고, 뒤 Phase의 절은 자리표시로 남긴다"),
    }
    assert subs[-1]["outcome"] == (
        f"부모 {slug} 계획서 Phase 4(§6 split + appendix + self-gate) 범위가 머지돼 이 하위의 "
        "acceptance_criteria가 충족된다 — 앞 하위가 머지한 부분은 고치지 않는다")
    assert plan["design_notes"] == [
        f"계획서: {plan_ref} · 요구사항: {req_ref} — 설계를 다시 도출하지 말고 이 둘을 따른다.",
        ("분할: 계획서 Phase 4개 → 하위 4개 (" + ", ".join(
            f"Phase {k} → {slug}-p{k}" for k in (1, 2, 3, 4)) + ")"),
        "부모 outcome: docs/design/example.md가 main에 머지돼 설계가 정리된다",
        ("각 하위는 자기 Phase 범위만 머지한다 — 범위 밖 절은 자리표시로 남기고, "
         "이미 머지된 앞 하위의 결과는 고치지 않는다."),
    ]
    assert plan["reason"] == (
        "plan stage declared the task larger than one Implement stage — split at "
        "the plan's Phase boundaries (4 phases → 4 subtasks)")
    # ... and everything else, key order included
    assert _digest(plan) == (
        "4272d85e3c369740a641006c222017e98f4fe6a06d28bc2dfde85c6554df9d22")
    assert _digest(phase_path._build(phase_path.SKILL_SHAPE_PLAN, fm)) == (
        "85937dcd4b748b7d0d827bcaf58770cd677164dcfb0346c3a6eb116420c1d7a0")


# -- which fences are the plan's split block ------------------------------------------

_FIND = [
    ("none", "# Plan\n\nNo fence at all.\n", []),
    ("one", '# Plan\n\n```rescope\n{"a": 1}\n```\n\ntail\n', ['{"a": 1}']),
    ("two", "```rescope\n{}\n```\n\ntext\n\n```rescope\n[]\n```\n", ["{}", "[]"]),
    ("tilde-fence", "~~~rescope\n{}\n~~~\n", ["{}"]),
    ("tilde-not-closed-by-backticks", "~~~rescope\n```\n{}\n~~~\n", ["```\n{}"]),
    ("indented-in-a-list", '- split:\n\n  ```rescope\n  {"a": 1}\n  ```\n', ['  {"a": 1}']),
    ("info-with-a-second-word", "```rescope json\n{}\n```\n", ["{}"]),
    ("info-uppercase", "```RESCOPE\n{}\n```\n", ["{}"]),
    ("info-after-spaces", "```  rescope\n{}\n```\n", ["{}"]),
    ("longer-fence", "````rescope\n{\n```\n}\n````\n", ["{\n```\n}"]),
    ("closed-by-a-longer-fence", "```rescope\n{}\n`````\n\nafter\n", ["{}"]),
    ("example-nested-in-a-longer-fence", "````text\n```rescope\n{}\n```\n````\n", []),
    ("example-nested-in-a-tilde-fence", "~~~markdown\n```rescope\n{}\n```\n~~~\n", []),
    ("other-info", "```json\n{}\n```\n", []),
    ("info-is-a-longer-word", "```rescoped\n{}\n```\n", []),
    ("info-second-word-only", "```json rescope\n{}\n```\n", []),
    ("no-info", "```\n{}\n```\n", []),
    ("after-an-unrelated-fence", "```python\nx = 1\n```\n\n```rescope\n{}\n```\n", ["{}"]),
    ("frontmatter-skipped", "---\nnote: |\n  ```rescope\n  {}\n  ```\n---\n\n# Plan\n", []),
    ("unclosed", '# Plan\n\n```rescope\n{"a": 1}\n', ['{"a": 1}']),
    ("empty-body", "```rescope\n```\n", [""]),
    ("one-line-code-span", "```rescope {}```\n\ntext\n", []),
    ("code-span-does-not-open-a-fence", "```rescope {}```\n\n```rescope\n{}\n```\n", ["{}"]),
    ("crlf", "```rescope\r\n{}\r\n```\r\n", ["{}"]),
    ("empty", "", []),
    ("blank", "  \n\n", []),
    ("none-text", None, []),
    ("not-a-string", 5, []),
]


@pytest.mark.parametrize("text,expected", [(t, e) for _, t, e in _FIND],
                         ids=[i for i, _, _ in _FIND])
def test_find_blocks(text, expected):
    assert rescope.find_plan_split_blocks(text) == expected


def test_find_blocks_in_the_1118_plan():
    # the plan's own `json` and `python` fences (indented, inside list items) are
    # walked past without losing step: none before the block, exactly one with it
    assert rescope.find_plan_split_blocks(PLAN_1118) == []
    blocks = rescope.find_plan_split_blocks(with_block(PLAN_1118))
    assert len(blocks) == 1
    assert json.loads(blocks[0]) == SPLIT_1118
    # and the Phase parser still reads all eight Phases after it
    assert len(rescope.parse_plan_phases(with_block(PLAN_1118))) == 8


# -- the builder: the 11-18 split -----------------------------------------------------


def test_1118_block_builds_the_three_children():
    plan, why = _build(with_block(PLAN_1118))
    assert plan is not None, why
    assert why == ""
    subs = plan["subtasks"]
    assert [s["slug"] for s in subs] == CHILDREN
    for s, want, label in zip(subs, SPLIT_1118["subtasks"], LABELS):
        assert s["acceptance_criteria"] == [_one_line(x) for x in want["acceptance_criteria"]]
        assert s["touches"] == want["touches"]
        assert s["files"] == want["touches"]      # what the guardrail scan reads
        assert s["phases"] == want["phases"]
        assert s["model_tier"] == want["model_tier"]
        assert (s["estimate_p50"], s["estimate_p90"]) == (
            want["estimate_p50"], want["estimate_p90"])
        assert s["title"] == _one_line(want["title"])
        assert s["outcome"] == _one_line(want["outcome"])
        assert s["next_action"] == (
            f"{PLAN_REF}의 부모 계획서 Phase {label}과 {REQ_REF}를 읽고 그 단계만 수행한다"
            "(이미 머지된 앞 부분은 고치지 않는다)")
        assert s["if_then"] == (
            f"워커가 잡으면 → {PLAN_REF}의 Phase {label}과 {REQ_REF}를 읽고 그 Phase만 "
            "수행한다(설계는 계획서를 따르고 다시 도출하지 않는다). 범위 밖 Phase는 건드리지 "
            "않고, 한 스테이지에 다 안 들어가면 끝낸 Phase까지 커밋하고 남은 Phase를 run.log에 "
            "적는다(다시 한 스테이지 초과로 파킹하지 않는다)")
        assert s["spec"]["goal"].startswith(f"부모 계획서 Phase {label}(")
        assert len(s["steps"]) == len(want["phases"])
        assert [step.split(":")[0] for step in s["steps"]] == [
            f"Phase {n}" for n in want["phases"]]
        # the block is the child's whole contract: nothing of it is a list alias
        assert s["touches"] is not s["files"]
    assert subs[2]["steps"] == [
        "Phase 7: 대시보드 — Orchestrator.md 섹션, `/api/status` 필드, 웹 카드 (하위 ③)",
        "Phase 8: 문서, 후속 항목, 마감 게이트 (하위 ③)",
    ]

    assert plan["version"] == rescope.RESCOPE_SCHEMA_VERSION
    assert plan["parent_slug"] == PARENT
    assert plan["source"] == "plan-split-block"
    assert plan["run_ref"] == PLAN_REF
    assert rescope._plan_run_ref(plan) == PLAN_REF
    assert plan["reason"] == (
        "plan stage declared the task larger than one Implement stage — split by "
        "the plan's own rescope block (8 phases → 3 subtasks)")
    notes = plan["design_notes"]
    assert notes[0] == (f"계획서: {PLAN_REF} · 요구사항: {REQ_REF} — 설계를 다시 도출하지 "
                        "말고 이 둘을 따른다.")
    assert notes[1] == ("분할: 계획서 Phase 8개 → 하위 3개 (" + ", ".join(
        f"Phase {label} → {slug}" for label, slug in zip(LABELS, CHILDREN)) + ")")
    assert notes[2].startswith("부모 outcome: macp 오케스트레이터가")
    assert len(notes) == 4
    # a code task has no document sections to leave as placeholders
    wording = json.dumps([notes[3]] + [s["spec"] for s in subs] + [s["if_then"] for s in subs],
                         ensure_ascii=False)
    assert "자리표시" not in wording and "leaf" not in wording
    assert rescope.can_auto_rescope(plan, _fm(PARENT_MD)).allow
    json.dumps(plan)                                   # plain JSON types throughout


def test_minimal_block_derives_and_inherits():
    plan, why = _build(with_block(PLAN_1118, _split(_minimal)))
    assert plan is not None, why
    subs = plan["subtasks"]
    assert [s["slug"] for s in subs] == CHILDREN
    for s, label in zip(subs, LABELS):
        assert s["title"].startswith(f"{PARENT_TITLE} — Phase {label}: ")
        assert len(s["title"]) <= 200
        assert s["outcome"].startswith(f"부모 {PARENT} 계획서 Phase {label}(")
        assert s["outcome"].endswith("이 하위의 acceptance_criteria가 충족된다")
        assert "model_tier" not in s               # absent → the parent's, at generation
        assert (s["estimate_p50"], s["estimate_p90"]) == ("6h", "10h")   # the parent's
        assert PLAN_REF in s["if_then"] and f"Phase {label}" in s["if_then"]
    assert subs[2]["title"] == (
        f"{PARENT_TITLE} — Phase 7·8: 대시보드 — Orchestrator.md 섹션, `/api/status` 필드, "
        "웹 카드 (하위 ③) / 문서, 후속 항목, 마감 게이트 (하위 ③)")
    assert rescope.can_auto_rescope(plan, _fm(PARENT_MD)).allow

    # no parent title → the parent slug; no parent estimates → no estimate keys
    fm = {**_fm(PARENT_MD), "estimate_p50": "-", "estimate_p90": None}
    bare, why = _build(with_block(PLAN_1118, _split(_minimal)), fm, parent_title="")
    assert bare is not None, why
    assert bare["subtasks"][0]["title"].startswith(f"{PARENT} — Phase 1·2·3: ")
    assert not any(k.startswith("estimate") for s in bare["subtasks"] for k in s)


def _engine_owned(subs) -> None:
    subs[0].update(summary="한 줄 요약", next_action="do something else",
                   if_then="when X then Y", files=["elsewhere/**"],
                   spec={"goal": "another goal"}, steps=["step zero"])


_ACCEPTED = [
    ("depends-on-omitted", lambda subs: [s.pop("depends_on") for s in subs],
     lambda subs: None),
    ("depends-on-null", lambda subs: [s.update(depends_on=None) for s in subs],
     lambda subs: None),
    ("tier-any-case", _set(2, "model_tier", " Standard "),
     lambda subs: subs[2]["model_tier"] == "standard"),
    ("tier-null-is-absent", _set(2, "model_tier", None),
     lambda subs: "model_tier" not in subs[2]),
    ("touches-duplicates-collapse",
     _set(2, "touches", ["docs/a.md", "todo_system/x.py", "docs/a.md", " docs/a.md "]),
     lambda subs: subs[2]["touches"] == ["docs/a.md", "todo_system/x.py"]
     and subs[2]["files"] == ["docs/a.md", "todo_system/x.py"]),
    ("touches-leading-dot-slash", _set(2, "touches", ["./docs/a.md", "todo_system/**"]),
     lambda subs: subs[2]["touches"] == ["docs/a.md", "todo_system/**"]),
    ("touches-globs", _set(2, "touches", ["docs/*.md", "todo_system/orchestrator/**"]),
     lambda subs: subs[2]["touches"] == ["docs/*.md", "todo_system/orchestrator/**"]),
    ("estimate-spelling", _set(2, "estimate_p50", " 1H 30m "),
     lambda subs: subs[2]["estimate_p50"] == "1h30m"),
    ("estimate-null-is-the-parents", _set(2, "estimate_p90", None),
     lambda subs: subs[2]["estimate_p90"] == "10h"),
    ("title-and-criteria-on-one-line",
     lambda subs: subs[0].update(title="첫째\n  하위", outcome="한\t줄로\n정리",
                                 acceptance_criteria=["기준\n하나", "  둘  "]),
     lambda subs: subs[0]["title"] == "첫째 하위" and subs[0]["outcome"] == "한 줄로 정리"
     and subs[0]["acceptance_criteria"] == ["기준 하나", "둘"]),
    ("long-title-clipped", _set(0, "title", "가" * 300),
     lambda subs: len(subs[0]["title"]) == 200 and subs[0]["title"].endswith("…")),
    ("unknown-and-engine-owned-keys", _engine_owned,
     lambda subs: subs[0]["next_action"].startswith(PLAN_REF)
     and PLAN_REF in subs[0]["if_then"]
     and subs[0]["files"] == subs[0]["touches"] == SPLIT_1118["subtasks"][0]["touches"]
     and subs[0]["spec"]["goal"].startswith("부모 계획서 Phase 1·2·3(")
     and len(subs[0]["steps"]) == 3 and "summary" not in subs[0]),
]


@pytest.mark.parametrize("mutate,check", [(m, c) for _, m, c in _ACCEPTED],
                         ids=[i for i, _, _ in _ACCEPTED])
def test_accepted_spellings(mutate, check):
    plan, why = _build(with_block(PLAN_1118, _split(mutate)))
    assert plan is not None, why
    assert [s["slug"] for s in plan["subtasks"]] == CHILDREN
    assert check(plan["subtasks"]) is not False
    assert rescope.can_auto_rescope(plan, _fm(PARENT_MD)).allow


def test_two_and_five_children_are_the_bounds():
    def two(subs):
        subs[:] = [{**subs[0], "phases": [1, 2, 3, 4]},
                   {**subs[1], "phases": [5, 6, 7, 8]}]

    def five(subs):
        base = {k: subs[0][k] for k in ("acceptance_criteria", "touches")}
        subs[:] = [{**base, "slug": f"example-part-{i}", "phases": ph}
                   for i, ph in enumerate(([1, 2], [3, 4], [5], [6], [7, 8]), 1)]

    for mutate, n in ((two, 2), (five, 5)):
        plan, why = _build(with_block(PLAN_1118, _split(mutate)))
        assert plan is not None, why
        assert len(plan["subtasks"]) == n
        assert rescope.can_auto_rescope(plan, _fm(PARENT_MD)).allow


# -- the builder: fail-closed ---------------------------------------------------------
# A block is used exactly as written or not at all. Every row is the 11-18 plan
# and split with ONE defect, so the defect is the only thing that can refuse it
# (test_1118_block_builds_the_three_children is the control).


def _bad(mutate):
    return lambda: with_block(PLAN_1118, _split(mutate))


def _raw(body: str):
    return lambda: with_block(PLAN_1118, raw=f"```rescope\n{body}\n```\n")


def _one(subs) -> None:
    subs[:] = [{**subs[0], "phases": list(range(1, 9))}]


def _six(subs) -> None:
    base = {k: subs[0][k] for k in ("acceptance_criteria", "touches")}
    subs[:] = [{**base, "slug": f"example-part-{i}", "phases": ph}
               for i, ph in enumerate(([1], [2], [3], [4], [5], [6, 7, 8]), 1)]


def _swap_first_two_phases(subs) -> None:
    subs[0]["phases"], subs[1]["phases"] = subs[1]["phases"], subs[0]["phases"]


def _interleaved(subs) -> None:
    subs[0]["phases"], subs[1]["phases"] = [1, 3], [2, 4, 5, 6]


def _unnumbered_phase() -> str:
    text = with_block(PLAN_1118)
    assert text.count("\n### Phase 2: ") == 1
    return text.replace("\n### Phase 2: ", "\n### Phase 2a: ")


def _not_closed() -> str:
    head, tail = PLAN_1118.split(PHASES_MARKER)
    body = json.dumps(SPLIT_1118, ensure_ascii=False)
    return head + "\n```rescope\n" + body + "\n" + PHASES_MARKER + tail


_LIST_BODY = json.dumps(SPLIT_1118["subtasks"], ensure_ascii=False)
_OTHER_KEY = json.dumps({"children": SPLIT_1118["subtasks"]}, ensure_ascii=False)
_NOT_A_LIST = json.dumps({"subtasks": {"first": SPLIT_1118["subtasks"][0]}}, ensure_ascii=False)

_BLOCK_FAIL_CLOSED = [
    # -- the block itself
    ("two-blocks", lambda: with_block(with_block(PLAN_1118)), {}),
    ("block-not-closed", _not_closed, {}),
    ("json-trailing-comma", _raw('{"subtasks": [{"slug": "a-b", "phases": [1],},]}'), {}),
    ("json-empty", _raw(""), {}),
    ("json-not-json", _raw("slug: a-b\nphases: 1~3"), {}),
    ("json-nan", _raw('{"subtasks": NaN}'), {}),
    ("top-level-list", _raw(_LIST_BODY), {}),
    ("top-level-string", _raw('"subtasks"'), {}),
    ("no-subtasks-key", _raw(_OTHER_KEY), {}),
    ("subtasks-not-a-list", _raw(_NOT_A_LIST), {}),
    ("block-too-large", _bad(_set(0, "summary", "x" * 64_000)), {}),
    ("one-subtask", _bad(_one), {}),
    ("six-subtasks", _bad(_six), {}),
    ("subtask-not-an-object", _bad(lambda subs: subs.__setitem__(1, "second")), {}),
    ("subtask-is-a-list", _bad(lambda subs: subs.__setitem__(1, ["second"])), {}),
    # -- slug
    ("slug-missing", _bad(_drop(0, "slug")), {}),
    ("slug-not-a-string", _bad(_set(0, "slug", 5)), {}),
    ("slug-empty", _bad(_set(0, "slug", "")), {}),
    ("slug-uppercase", _bad(_set(0, "slug", "Example-Batch-Verify-1")), {}),
    ("slug-underscore", _bad(_set(0, "slug", "example_batch_verify_1")), {}),
    ("slug-with-a-slash", _bad(_set(0, "slug", "example/one")), {}),
    ("slug-dot-dot", _bad(_set(0, "slug", "../example-one")), {}),
    ("slug-leading-dot", _bad(_set(0, "slug", ".example-one")), {}),
    ("slug-with-a-space", _bad(_set(0, "slug", "example one")), {}),
    ("slug-padded", _bad(_set(0, "slug", " example-one ")), {}),
    ("slug-trailing-newline", _bad(_set(0, "slug", "example-one\n")), {}),
    ("slug-leading-hyphen", _bad(_set(0, "slug", "-example-one")), {}),
    ("slug-trailing-hyphen", _bad(_set(0, "slug", "example-one-")), {}),
    ("slug-double-hyphen", _bad(_set(0, "slug", "example--one")), {}),
    ("slug-md-suffix", _bad(_set(0, "slug", "example-one.md")), {}),
    ("slug-81-chars", _bad(_set(0, "slug", "a" * 81)), {}),
    ("slug-is-the-parent", _bad(_set(0, "slug", PARENT)), {}),
    ("slug-duplicates-a-sibling", _bad(_set(2, "slug", CHILDREN[0])), {}),
    # -- phases
    ("phases-missing", _bad(_drop(0, "phases")), {}),
    ("phases-empty", _bad(_set(0, "phases", [])), {}),
    ("phases-a-range-string", _bad(_set(0, "phases", "1-3")), {}),
    ("phases-number-strings", _bad(_set(0, "phases", ["1", "2", "3"])), {}),
    ("phases-a-fraction", _bad(_set(0, "phases", [1.5, 2, 3])), {}),
    ("phases-whole-floats", _bad(_set(0, "phases", [1.0, 2.0, 3.0])), {}),
    ("phases-a-bool", _bad(_set(0, "phases", [True, 2, 3])), {}),
    ("phases-a-single-number", _bad(_set(0, "phases", 1)), {}),
    ("phases-gap", _bad(_set(0, "phases", [1, 2])), {}),
    ("phases-overlap", _bad(_set(1, "phases", [3, 4, 5, 6])), {}),
    ("phases-out-of-order", _bad(_swap_first_two_phases), {}),
    ("phases-beyond-the-plan", _bad(_set(2, "phases", [7, 8, 9])), {}),
    ("phases-interleaved", _bad(_interleaved), {}),
    ("phases-descending", _bad(_set(0, "phases", [3, 2, 1])), {}),
    ("phases-repeated", _bad(_set(0, "phases", [1, 1, 2, 3])), {}),
    ("phases-from-zero", _bad(_set(0, "phases", [0, 1, 2, 3])), {}),
    ("phases-last-one-missing", _bad(_set(2, "phases", [7])), {}),
    # -- depends_on
    ("depends-first-names-a-sibling", _bad(_set(0, "depends_on", [CHILDREN[1]])), {}),
    ("depends-not-the-one-before", _bad(_set(2, "depends_on", [CHILDREN[0]])), {}),
    ("depends-later-empty", _bad(_set(1, "depends_on", [])), {}),
    ("depends-a-string", _bad(_set(1, "depends_on", CHILDREN[0])), {}),
    ("depends-two", _bad(_set(2, "depends_on", [CHILDREN[0], CHILDREN[1]])), {}),
    ("depends-outside-the-split", _bad(_set(1, "depends_on", ["some-other-task"])), {}),
    ("depends-on-the-parent", _bad(_set(1, "depends_on", [PARENT])), {}),
    # -- acceptance_criteria
    ("criteria-missing", _bad(_drop(1, "acceptance_criteria")), {}),
    ("criteria-empty", _bad(_set(1, "acceptance_criteria", [])), {}),
    ("criteria-blank-item", _bad(_set(1, "acceptance_criteria", ["기준", "  "])), {}),
    ("criteria-a-string", _bad(_set(1, "acceptance_criteria", "기준 하나")), {}),
    ("criteria-non-string-item", _bad(_set(1, "acceptance_criteria", ["기준", 5])), {}),
    ("criteria-nested-list", _bad(_set(1, "acceptance_criteria", [["기준"]])), {}),
    ("criteria-control-character", _bad(_set(1, "acceptance_criteria", ["기준\x00"])), {}),
    # -- touches
    ("touches-missing", _bad(_drop(1, "touches")), {}),
    ("touches-empty", _bad(_set(1, "touches", [])), {}),
    ("touches-a-string", _bad(_set(1, "touches", "todo_system/i18n.py")), {}),
    ("touches-braces", _bad(_set(1, "touches", ["todo_system/{a,b}.py"])), {}),
    ("touches-absolute", _bad(_set(1, "touches", ["/etc/passwd"])), {}),
    ("touches-home", _bad(_set(1, "touches", ["~/notes.md"])), {}),
    ("touches-dot-dot", _bad(_set(1, "touches", ["todo_system/../../x.py"])), {}),
    ("touches-leading-dot-dot", _bad(_set(1, "touches", ["../x.py"])), {}),
    ("touches-blank-item", _bad(_set(1, "touches", ["todo_system/i18n.py", " "])), {}),
    ("touches-only-dot-slash", _bad(_set(1, "touches", ["./"])), {}),
    ("touches-non-string-item", _bad(_set(1, "touches", ["todo_system/i18n.py", 5])), {}),
    ("touches-backslash", _bad(_set(1, "touches", ["todo_system\\i18n.py"])), {}),
    ("touches-two-lines", _bad(_set(1, "touches", ["a.py\nb.py"])), {}),
    ("touches-too-long", _bad(_set(1, "touches", ["d/" * 100 + "x.py"])), {}),
    ("touches-everything", _bad(_set(1, "touches", ["**/*.md"])), {}),
    ("touches-every-file", _bad(_set(1, "touches", ["*"])), {}),
    ("touches-worker-dir", _bad(_set(1, "touches", ["Worker/**"])), {}),
    ("touches-guardrail-policy",
     _bad(_set(1, "touches", ["docs/a.md", "Worker/planner_policy.yaml"])), {}),
    ("touches-guardrail-by-wildcard",
     _bad(_set(1, "touches", ["Worker/planner_pol*.yaml"])), {}),
    ("touches-guardrail-behind-dot-slash",
     _bad(_set(1, "touches", ["./Worker/planner_policy.yaml"])), {}),
    # -- optional fields
    ("tier-unknown", _bad(_set(2, "model_tier", "turbo")), {}),
    ("tier-a-number", _bad(_set(2, "model_tier", 3)), {}),
    ("tier-empty", _bad(_set(2, "model_tier", "")), {}),
    ("estimate-a-word", _bad(_set(2, "estimate_p50", "soon")), {}),
    ("estimate-without-a-unit", _bad(_set(2, "estimate_p90", "2")), {}),
    ("estimate-a-number", _bad(_set(2, "estimate_p50", 2)), {}),
    ("estimate-seconds", _bad(_set(2, "estimate_p50", "90s")), {}),
    ("estimate-empty", _bad(_set(2, "estimate_p90", "")), {}),
    ("title-empty", _bad(_set(0, "title", "")), {}),
    ("title-blank", _bad(_set(0, "title", " \n ")), {}),
    ("title-a-number", _bad(_set(0, "title", 5)), {}),
    ("outcome-empty", _bad(_set(0, "outcome", "")), {}),
    ("outcome-a-list", _bad(_set(0, "outcome", ["완료"])), {}),
    ("title-control-character", _bad(_set(0, "title", "제목\x07")), {}),
    # -- text that names a human-owned guardrail file (the write scope may hide it)
    ("criteria-name-a-guardrail-file",
     _bad(_set(1, "acceptance_criteria",
               ["기준", "`Worker/planner_policy.yaml`에 규칙을 더한다"])), {}),
    ("outcome-names-a-guardrail-file",
     _bad(_set(1, "outcome", "Worker/planner_policy.yaml 의 한도를 올린다")), {}),
    ("title-names-a-guardrail-file",
     _bad(_set(1, "title", "Worker/planner_policy.yaml 손보기")), {}),
    # -- the plan and the parent
    ("plan-phase-unnumbered", _unnumbered_phase, {}),
    ("plan-without-phases", lambda: "# Plan\n\n" + block(), {}),
    ("parent-slug-with-a-slash", lambda: with_block(PLAN_1118), {"parent_slug": "a/b"}),
    ("parent-slug-dotted", lambda: with_block(PLAN_1118), {"parent_slug": ".hidden"}),
    ("parent-slug-empty", lambda: with_block(PLAN_1118), {"parent_slug": ""}),
    ("run-id-empty", lambda: with_block(PLAN_1118), {"run_id": ""}),
    ("run-id-with-a-slash", lambda: with_block(PLAN_1118), {"run_id": "../x"}),
    ("parent-fm-none", lambda: with_block(PLAN_1118), {"fm": None}),
    ("parent-fm-a-list", lambda: with_block(PLAN_1118), {"fm": ["auto_merge"]}),
    ("blocks-none", lambda: with_block(PLAN_1118), {"blocks": None}),
    ("blocks-empty", lambda: with_block(PLAN_1118), {"blocks": []}),
    ("blocks-a-string", lambda: with_block(PLAN_1118), {"blocks": block()}),
    ("blocks-not-strings", lambda: with_block(PLAN_1118), {"blocks": [5]}),
    ("plan-text-none", lambda: None, {"blocks": [json.dumps(SPLIT_1118)]}),
]


@pytest.mark.parametrize("make,kwargs", [(m, k) for _, m, k in _BLOCK_FAIL_CLOSED],
                         ids=[i for i, _, _ in _BLOCK_FAIL_CLOSED])
def test_block_fail_closed(make, kwargs):
    plan, why = _build(make(), **kwargs)
    assert plan is None
    assert isinstance(why, str) and why.strip()
    assert "\n" not in why and len(why) <= 300      # one line for the runner's log


def test_fail_closed_ids_are_unique():
    ids = [i for i, _, _ in _BLOCK_FAIL_CLOSED]
    assert len(ids) == len(set(ids))


def test_reasons_name_the_defect():
    def why(mutate):
        plan, reason = _build(with_block(PLAN_1118, _split(mutate)))
        assert plan is None
        return reason

    assert "2" in _build(with_block(with_block(PLAN_1118)))[1]
    assert "slug" in why(_set(0, "slug", "Example"))
    assert CHILDREN[0] in why(_set(2, "slug", CHILDREN[0]))
    assert "#1" in why(_set(1, "touches", ["/etc/passwd"]))
    assert "touches" in why(_set(1, "touches", ["/etc/passwd"]))
    assert "Phase" in why(_set(0, "phases", [1, 2]))
    assert "depends_on" in why(_set(1, "depends_on", []))
    assert "model_tier" in why(_set(2, "model_tier", "turbo"))
    assert "estimate_p50" in why(_set(2, "estimate_p50", "soon"))


def test_builder_never_raises_on_junk():
    junk = [None, 0, "", "x", [], {}, [None], [{}], [[]], {"subtasks": None}]
    for blocks in junk + [[json.dumps(j)] for j in junk]:
        for fm in (None, {}, _fm(PARENT_MD)):
            plan, why = rescope.rescope_from_split_block(
                blocks, with_block(PLAN_1118), fm, parent_slug=PARENT, run_id=RUN_ID)
            assert plan is None and isinstance(why, str) and why
    deep = "[" * 30_000 + "]" * 30_000      # under the size cap: a parser recursion bomb
    plan, why = _build(with_block(PLAN_1118), blocks=[deep])
    assert plan is None and why


# -- the writer -----------------------------------------------------------------------


@pytest.fixture
def env_1118(tmp_path: Path):
    """A tmp items dir holding the 11-18 parent, and that task's (empty) run dir."""
    items = tmp_path / "items"
    items.mkdir()
    run_dir = tmp_path / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    (items / f"{PARENT}.md").write_text(PARENT_MD, encoding="utf-8")
    return items, run_dir


def _write(run_dir: Path, plan_text: str, *, slug: str = PARENT, items: Path) -> str:
    (run_dir / "implementation_plan.md").write_text(plan_text, encoding="utf-8")
    return rescope.write_plan_rescope(run_dir, parent_slug=slug, items_dir=items)


def test_writer_uses_the_block(env_1118):
    items, run_dir = env_1118
    note = _write(run_dir, with_block(PLAN_1118), items=items)
    assert isinstance(note, str) and note.startswith("rescope.json written"), note
    assert all(slug in note for slug in CHILDREN)
    expected, why = _build(with_block(PLAN_1118), repos._frontmatter(items / f"{PARENT}.md"))
    assert expected is not None, why
    assert rescope.load_rescope(run_dir) == expected
    assert not list(run_dir.glob("*.tmp"))
    # a second call never replaces it
    first = (run_dir / rescope.RESCOPE_FILENAME).read_bytes()
    assert "already present" in rescope.write_plan_rescope(
        run_dir, parent_slug=PARENT, items_dir=items)
    assert (run_dir / rescope.RESCOPE_FILENAME).read_bytes() == first


def test_writer_1118_without_block_writes_nothing(env_1118):
    """The control for the two tests around it: the same plan minus the block."""
    items, run_dir = env_1118
    note = _write(run_dir, PLAN_1118, items=items)
    assert isinstance(note, str) and note.startswith("no split"), note
    assert not (run_dir / rescope.RESCOPE_FILENAME).exists()


@pytest.fixture
def env_keyed(tmp_path: Path):
    """The Phase-path fixture: a parent whose criteria all carry `§N` keys, so
    the Phase-heading builder CAN split its plan."""
    items = tmp_path / "items"
    items.mkdir()
    run_dir = tmp_path / "runs" / phase_path.RUN_ID
    run_dir.mkdir(parents=True)
    (items / f"{phase_path.PARENT}.md").write_text(
        phase_path._render_md(phase_path._parent_fm()), encoding="utf-8")
    return items, run_dir


def _keyed_split() -> dict:
    gate = phase_path.GATE_AC
    return {"subtasks": [
        {"slug": "example-ladder-first-half", "phases": [1, 2, 3],
         "acceptance_criteria": [*phase_path.SECTION_ACS[:5], gate],
         "touches": ["docs/design/example.md"]},
        {"slug": "example-ladder-second-half", "phases": [4, 5],
         "depends_on": ["example-ladder-first-half"],
         "acceptance_criteria": [phase_path.SECTION_ACS[5], gate],
         "touches": ["docs/design/example.md", "docs/design/appendix.md"]},
    ]}


def test_block_wins_over_phase_keys(env_keyed):
    items, run_dir = env_keyed
    # control: without a block this plan splits into four along its headings
    assert len(phase_path._build(phase_path.SKILL_SHAPE_PLAN,
                                 phase_path._parent_fm())["subtasks"]) == 4
    note = _write(run_dir, with_block(phase_path.SKILL_SHAPE_PLAN, _keyed_split()),
                  slug=phase_path.PARENT, items=items)
    assert note.startswith("rescope.json written"), note
    plan = rescope.load_rescope(run_dir)
    assert [s["slug"] for s in plan["subtasks"]] == [
        "example-ladder-first-half", "example-ladder-second-half"]
    assert plan["source"] == "plan-split-block"
    assert plan["subtasks"][1]["touches"] == [
        "docs/design/example.md", "docs/design/appendix.md"]


def test_unusable_block_never_falls_back_to_phase_keys(env_keyed):
    items, run_dir = env_keyed
    broken = with_block(phase_path.SKILL_SHAPE_PLAN,
                        raw='```rescope\n{"subtasks": [,]}\n```\n')
    note = _write(run_dir, broken, slug=phase_path.PARENT, items=items)
    assert note.startswith("no split"), note
    assert "rescope block" in note
    assert not (run_dir / rescope.RESCOPE_FILENAME).exists()
    assert not list(run_dir.glob("*.tmp"))
    # the same plan with the block taken out splits as it always did
    (run_dir / "implementation_plan.md").unlink()
    assert _write(run_dir, phase_path.SKILL_SHAPE_PLAN, slug=phase_path.PARENT,
                  items=items).startswith("rescope.json written")
    assert rescope.load_rescope(run_dir)["source"] == "plan-phases"


def _parent_with(key: str, value: str):
    def mutate(items: Path, run_dir: Path) -> None:
        text = cli.update_frontmatter_value(PARENT_MD, key, value)
        assert text != PARENT_MD
        (items / f"{PARENT}.md").write_text(text, encoding="utf-8")
    return mutate


def _live_tag(items: Path, run_dir: Path) -> None:
    assert PARENT_MD.count("  - feature\n") == 1
    (items / f"{PARENT}.md").write_text(
        PARENT_MD.replace("  - feature\n", "  - feature\n  - live\n"), encoding="utf-8")


def _second_child_exists(items: Path, run_dir: Path) -> None:
    (items / f"{CHILDREN[1]}.md").write_text("---\nstatus: open\n---\n\n# unrelated\n",
                                             encoding="utf-8")


def _parent_missing(items: Path, run_dir: Path) -> None:
    (items / f"{PARENT}.md").unlink()


_WRITER_GUARDS = [
    ("auto-merge-false", _parent_with("auto_merge", "false")),
    ("human-gate", _parent_with("human_gate", "true")),
    ("live-tag", _live_tag),
    ("already-auto-rescoped", _parent_with("auto_rescoped_from", "older-parent")),
    ("if-then-asks-a-human", _parent_with("if_then", '"분할 전에 사람에게 확인한다"')),
    ("child-md-exists", _second_child_exists),
    ("parent-md-missing", _parent_missing),
]


@pytest.mark.parametrize("mutate", [m for _, m in _WRITER_GUARDS],
                         ids=[i for i, _ in _WRITER_GUARDS])
def test_writer_guards_still_apply_with_a_valid_block(env_1118, mutate):
    """A usable block does not get past any guard of the Phase path
    (test_writer_uses_the_block is the control: unmutated, it writes)."""
    items, run_dir = env_1118
    mutate(items, run_dir)
    note = _write(run_dir, with_block(PLAN_1118), items=items)
    assert isinstance(note, str) and note.startswith("no split"), note
    assert not (run_dir / rescope.RESCOPE_FILENAME).exists()
    assert not list(run_dir.glob("*.tmp"))


def test_writer_leaves_an_existing_file_alone(env_1118):
    items, run_dir = env_1118
    dest = run_dir / rescope.RESCOPE_FILENAME
    dest.write_text('{"keep": true}', encoding="utf-8")
    note = _write(run_dir, with_block(PLAN_1118), items=items)
    assert "already present" in note
    assert dest.read_text(encoding="utf-8") == '{"keep": true}'


# -- the generator: a child's own touches and tier -------------------------------------


@pytest.fixture
def templates(tmp_path: Path) -> Path:
    d = tmp_path / "templates"
    d.mkdir()
    shutil.copy(paths.vault_root() / "Todo" / "templates" / "auto-worker-task-template.md",
                d / "auto-worker-task-template.md")
    return d


def _errors(path: Path) -> list[str]:
    return [i.message for i in _task_validate.validate_file(path)
            if i.level == _task_validate.ERROR]


def test_1118_children_markdown(tmp_path, templates):
    """AC 1: the 11-18 plan and parent, through the generator — three chained
    children, each with its own criteria and write scope, and the parent a bundle."""
    items = tmp_path / "items"
    items.mkdir()
    parent_path = items / f"{PARENT}.md"
    parent_path.write_text(PARENT_MD, encoding="utf-8")
    fm = repos._frontmatter(parent_path)
    plan, why = _build(with_block(PLAN_1118), fm)
    assert plan is not None, why

    out = rescope.generate_subtasks(plan, parent_slug=PARENT, parent_fm=fm, items_dir=items,
                                    templates_dir=templates, run_ref=PLAN_REF)

    assert [p.stem for p in out] == CHILDREN
    for i, (p, want) in enumerate(zip(out, SPLIT_1118["subtasks"])):
        text = p.read_text(encoding="utf-8")
        child = repos._frontmatter(p)
        assert child["parent"] == f"[[{PARENT}]]"
        assert child["auto_rescoped_from"] == PARENT
        assert child["status"] == "open"
        if i == 0:
            assert child["worker_mode"] == "auto"
            assert "depends_on" not in child and "on_unblock" not in child
        else:
            assert child["worker_mode"] == "blocked"
            assert child["on_unblock"] == "auto"
            assert child["depends_on"] == [CHILDREN[i - 1]]
        assert child["acceptance_criteria"] == [_one_line(x) for x in want["acceptance_criteria"]]
        assert child["touches"] == want["touches"]          # its own list ...
        assert child["touches"] != fm["touches"]            # ... not the parent's eight
        assert child["model_tier"] == want["model_tier"]
        assert (child["estimate_p50"], child["estimate_p90"]) == (
            want["estimate_p50"], want["estimate_p90"])
        assert child["outcome"] == _one_line(want["outcome"])
        assert PLAN_REF in child["next_action"] and f"Phase {LABELS[i]}" in child["next_action"]
        assert PLAN_REF in child["if_then"]
        assert child["repo"] == REPO_URL
        assert child["auto_merge"] is True
        assert child["max_turns"] == 200
        assert child["priority"] == 75
        assert child["worker_branch"] == f"worker/{CHILDREN[i]}"
        assert child["tags"] == ["todo", "private", "macp", "worker", "feature"]
        lines = text.splitlines()
        assert sum(1 for line in lines if line.startswith("touches:")) == 1
        assert sum(1 for line in lines if line.startswith("model_tier:")) == 1
        assert f"# {_one_line(want['title'])}" in lines
        assert f"## 이미 정해진 설계 (다시 도출하지 말 것 — {PLAN_REF})" in lines
        assert _errors(p) == []

    rescope.flip_parent_to_bundle(parent_path, CHILDREN)
    # read with the line parser: to a YAML reader a bare `off` is the boolean false
    bundle = worker.parse_frontmatter(parent_path.read_text(encoding="utf-8"))[0]
    assert bundle["worker_mode"] == "off"
    assert bundle["auto_rescoped"] == "true"
    assert bundle["is_parent"] == "true"
    assert "자동 분할 → 하위 3개" in parent_path.read_text(encoding="utf-8")


def _plain_plan(first: dict, second: dict) -> dict:
    """A hand-written split plan (the shape an Implement or Verify stage leaves)."""
    def sub(i: int, extra: dict) -> dict:
        return {"slug": f"{PARENT}-part-{i}", "title": f"하위 {i}",
                "outcome": f"하위 {i} 완료 조건", "acceptance_criteria": [f"하위 {i} AC"],
                "files": ["docs/design/example.md"], "spec": {"goal": f"하위 {i} 목표"},
                **extra}
    return {"version": rescope.RESCOPE_SCHEMA_VERSION, "parent_slug": PARENT,
            "reason": "exceeds a single Implement stage", "design_notes": ["결정 사실 A"],
            "subtasks": [sub(0, first), sub(1, second)]}


_OWN = {"touches": ["docs/own.md", "todo_system/own/**"], "model_tier": "light"}

_CHILD_VALUES = [
    ("own-values", _OWN, ["docs/own.md", "todo_system/own/**"], "light"),
    ("tier-any-case", {"touches": ["docs/own.md"], "model_tier": " Standard "},
     ["docs/own.md"], "standard"),
    ("touches-a-string", {"touches": "docs/own.md"}, None, None),
    ("touches-empty", {"touches": []}, None, None),
    ("touches-blank-item", {"touches": ["docs/own.md", " "]}, None, None),
    ("touches-non-string-item", {"touches": ["docs/own.md", 5]}, None, None),
    ("tier-unknown", {"model_tier": "turbo"}, None, None),
    ("tier-a-number", {"model_tier": 3}, None, None),
]


@pytest.mark.parametrize("extra,touches,tier", [(e, t, m) for _, e, t, m in _CHILD_VALUES],
                         ids=[i for i, _, _, _ in _CHILD_VALUES])
def test_child_touches_and_tier_fall_back_to_the_parent(tmp_path, templates, extra, touches,
                                                        tier):
    """Whatever wrote the split plan: a child's usable `touches` / `model_tier`
    win, and a child without one — or with an unusable one — gets the parent's,
    exactly as before."""
    fm = _fm(PARENT_MD)
    items = tmp_path / "items"
    out = rescope.generate_subtasks(_plain_plan({}, extra), parent_slug=PARENT, parent_fm=fm,
                                    items_dir=items, templates_dir=templates, run_ref="")
    plain, own = (repos._frontmatter(p) for p in out)
    assert plain["touches"] == fm["touches"] and plain["model_tier"] == "heavy"
    assert own["touches"] == (touches or fm["touches"])
    assert own["model_tier"] == (tier or "heavy")
    for p in out:
        lines = p.read_text(encoding="utf-8").splitlines()
        assert sum(1 for line in lines if line.startswith("touches:")) == 1
        assert sum(1 for line in lines if line.startswith("model_tier:")) == 1
        assert _errors(p) == []


def test_child_values_without_a_parent_value(tmp_path, templates):
    # a parent that declares neither: the child's own values are still written,
    # and a child without them carries no key (the template's are comments)
    fm = {k: v for k, v in _fm(PARENT_MD).items() if k not in ("touches", "model_tier")}
    out = rescope.generate_subtasks(_plain_plan({}, _OWN), parent_slug=PARENT, parent_fm=fm,
                                    items_dir=tmp_path / "items", templates_dir=templates,
                                    run_ref="")
    plain, own = (repos._frontmatter(p) for p in out)
    assert "touches" not in plain and "model_tier" not in plain
    assert own["touches"] == _OWN["touches"] and own["model_tier"] == "light"


def test_tier_names_match_the_classifier():
    # rescope.py mirrors the tier names as strings (engine isolation)
    assert set(rescope._PLAN_SPLIT_TIERS) == set(model_select.TIERS)
