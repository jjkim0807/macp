"""Plan-stage split: the engine writes the split plan for a Plan-stage park.

When the Plan stage declares a task too big for one Implement stage, the runner
skips Implement and parks the task — but no agent is left to write the
machine-readable split plan (`rescope.json`), so every such park used to reach a
human as ⛔. The engine now builds that plan itself from the plan's own
`### Phase N:` headings. This module pins:

- the pure builder `rescope_from_plan_phases` — Phase grouping, the parent
  criterion → Phase mapping (leading `§N` keys against Phase *heading* keys
  only), the child shape, and every fail-closed (None) case;
- the never-raising writer `write_plan_rescope` — writes once, never
  overwrites, and leaves no file whenever the split must stay with a human;
- the guard additions the split relies on: `human_gate` denies the auto-split,
  children inherit the parent `touches` as a block list, and a plan-carried
  `run_ref` points the children at the plan instead of run.log.

The route-level wiring (runner plan-park → collect() → execute_rescope) lives in
test_plan_rescope_wiring.py.

Fixtures are synthesised from the shapes of two real design-task plans (one with
five Phases whose last is "Gates + fixes + diff check", one with four). Every
identifier is neutral (an `example` repo, nothing outside the tmp dirs) because
the public-identifier gate scans this tree.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from todo_system import paths, worker
from todo_system import task_validate as _task_validate
from todo_system.orchestrator import repos, rescope
from todo_system.orchestrator.assignment import AWAITING_HUMAN, Assignment, Ledger

PARENT = "example-ladder-design"
RUN_ID = f"20261113T000000-{PARENT}"
PARENT_TITLE = "example ladder design"
REPO_URL = "https://github.com/example/Repo.git"
ALLOWED_TOOLS = "Read,Edit,Write,Bash(git:*),Bash(grep:*),Bash(uv:*),Bash(uvx:*)"
TOUCHES = ["docs/design/example.md"]

LEAF_IF_THEN = ("계획서 단계가 이 leaf 범위를 넘으면 → 범위 안의 절만 쓰고 나머지는 "
                "자리표시로 남겨 머지한다(다시 한 스테이지 초과로 파킹하지 않는다)")

GATE_AC = ("문서만 바꾼다(코드 변경 없음). `uvx ruff check .`·"
           "`bash scripts/check_engine_isolation.sh` 로컬 green — 공개 리포라 개인 데이터 금지")
SECTION_ACS = [
    "§1 현황 전수표 — 교훈이 남는 곳, 다음 런이 그것을 읽는 경로, 한계",
    "§2 반복 감지 — 같은 유형으로 보는 기준, 승격 후보 기준, 오탐을 줄이는 규칙",
    "§3 사다리 세 단 — 제안·스킬·루틴 각 단의 파일 형식과 저장 위치",
    "§4 안전 — 권한을 넓히지 못한다, 내보내기 때 비밀값 제외, 가져온 것은 정지 상태로 시작",
    "§5 효과 측정 — 적용 전후의 소요·재시도·검증 통과율 비교, 나아지지 않으면 내린다",
    "§6 단계별 구현 분할안(leaf 목록, depends_on 체인, 로컬 검증 명령)과 배선 완료 조건",
]
ACS = SECTION_ACS + [GATE_AC]
S = SECTION_ACS

# Since the detector fix, the real plans these are shaped on no longer trip it,
# so both fixtures DECLARE the oversize explicitly — in the frontmatter verdict
# the current plan prompt writes, and in prose.
_DECLARATION = """## Size judgment

This task exceeds a single Implement stage — needs re-scoping.
"""

SKILL_SHAPE_PLAN = """---
stage: plan
run_id: 20261113T000000-example-ladder-design
slug: example-ladder-design
generated: 2026-11-13T13:32:00+09:00
declares_oversize: yes
---

# Implementation Plan — example ladder design

## Nature of this task (read first)

This is a design-document task. The canonical model is the sister note: mirror its
§3.1 census table and its §4.3 gate discipline.

""" + _DECLARATION + """
## Phases

Each phase appends its sections to `docs/design/example.md` and commits.

### Phase 1: Scaffold + §1 census
- **Goal**: Front-matter, the intro, and §1 — the census of where lessons live today.
- **Files**: `docs/design/example.md` (create). Read-only anchor sources:
  `.claude/commands/worker-planner.md`, `docs/concepts.md`.
- **Steps**:
  1. Copy the sister doc's front-matter shape.
  2. Write §1 as a table.
- **Risk**: an anchor drifts. Mitigation: grep each cited symbol first.

### Phase 2: §2 detection + §3 ladder
- **Goal**: §2 defines "same-type"; §3 specifies the three rungs.
- **Files**: `docs/design/example.md` (append). Anchors:
  `.claude/commands/worker-planner.md` (read-only), `todo_system/graph_memory.py`.
- **Steps**:
  1. §2 detection signal and the promotion bar.
  2. §3 rung formats and storage.
- **Risk**: a named path is not real.

### Phase 3: §4 safety + §5 measurement
- **Goal**: §4 permission rules and §5 metrics, with the same discipline as
  sister-doc.md §5 (and the leaf rules of §6.2 stay out of it).
- **Files**: `docs/design/example.md` (append).
- **Steps**:
  1. §4 permission non-expansion.
  2. §5 before/after metrics and the demotion rule.
- **Risk**: a metric with no retrievable source.

### Phase 4: §6 leaf split + appendix
- **Goal**: §6 as a leaf table, then the appendix.
- **Files**: `docs/design/example.md` (append).
- **Steps**:
  1. §6 leaf table with each leaf's local verify command.
- **Risk**: a leaf implies a guarded write.

### Phase 5: Gates + fixes + diff check
- **Goal**: Make the local gates green on the committed tree.
- **Files**: `docs/design/example.md` (fix-ups only).
- **Steps**:
  1. `uvx ruff check .`
  2. `bash scripts/check_engine_isolation.sh`
- **Risk**: an identifier leaks into an example.

## Long-running work

None.

## Parallelizable subtasks

None. The five phases all edit the same file and run serially.

## Verification strategy

- §1–§6 all present → `grep -n '^## §' docs/design/example.md`.

## Rollback plan

Delete the feature branch and re-dispatch.
"""

CHAT_SHAPE_PLAN = """---
stage: plan
run_id: 20261113T000001-example-channel-design
slug: example-channel-design
generated: 2026-11-13T17:12:00+09:00
declares_oversize: yes
---

# Implementation Plan — example channel design

## Nature of the work (read first)

Design-document-only task. Only if the orchestrator ever has to split it, the
hand-split boundary is Phase 2 end / Phase 3 start = §1~§3 / §4~§6.

""" + _DECLARATION + """
## Phases

### Phase 1: Scaffold + §1 census + §2 channel model
- **Goal**: Front-matter, §1 census and §2 channel model.
- **Files**: `docs/design/example.md` (create). Anchor: the sister note's §3.3.
- **Steps**:
  1. §1 table.
  2. §2 model.
- **Risk**: census misses an entry point.

### Phase 2: §3 routing
- **Goal**: §3 routing rules.
- **Files**: `docs/design/example.md` (append).
- **Steps**:
  1. §3 routing table.
- **Risk**: an ambiguous route.

### Phase 3: §4 reply + §5 safety
- **Goal**: §4 result reply and §5 safety.
- **Files**: `docs/design/example.md` (append).
- **Steps**:
  1. §4 reply path.
  2. §5 safety rules.
- **Risk**: a reply path that widens permissions.

### Phase 4: §6 split + appendix + self-gate
- **Goal**: §6 split, the appendix and the local self-gate.
- **Files**: `docs/design/example.md` (append + fix-ups).
- **Steps**:
  1. §6 leaf table.
  2. Run the local gates.
- **Risk**: a gate goes red.

## Long-running work

None.

## Rollback plan

Delete the feature branch.
"""


def _parent_fm(**over) -> dict:
    """The parent frontmatter as `repos._frontmatter` returns it (YAML types)."""
    fm = {
        "tags": ["todo", "private", "worker", "architecture"],
        "status": "open",
        "worker_mode": "auto",
        "auto_merge": True,
        "max_turns": 200,
        "model_tier": "heavy",
        "repo": REPO_URL,
        "allowed_tools": ALLOWED_TOOLS,
        "touches": list(TOUCHES),
        "outcome": "docs/design/example.md가 main에 머지돼 설계가 정리된다",
        "next_action": "지금 교훈이 어디에 남는지 전수표를 만든다",
        "if_then": "전수표가 나오면 → 감지 기준을 비교하고 세 단의 형식과 분할안을 쓴다",
        "estimate_p50": "3h",
        "estimate_p90": "6h",
        "acceptance_criteria": list(ACS),
    }
    fm.update(over)
    return fm


def _yaml_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)       # a valid YAML double-quoted scalar


def _render_md(fm: dict, title: str = PARENT_TITLE) -> str:
    """A task md in the vault's own quoting: block lists with double-quoted items."""
    lines = ["---"]
    for k, v in fm.items():
        if v is None:
            continue
        if isinstance(v, list):
            lines.append(f"{k}:")
            lines += [f"  - {_yaml_scalar(x)}" for x in v]
        else:
            lines.append(f"{k}: {_yaml_scalar(v)}")
    lines += ["---", "", f"# {title}", "", "## Progress Log", "", "- 2026-11-13: 생성", ""]
    return "\n".join(lines)


def _build(plan_text, fm, *, title: str = PARENT_TITLE):
    return rescope.rescope_from_plan_phases(
        plan_text, fm, parent_slug=PARENT, run_id=RUN_ID, parent_title=title)


def _phases_plan(*titles: str) -> str:
    lines = ["# Plan", "", "## Phases", ""]
    for i, t in enumerate(titles, 1):
        lines += [f"### Phase {i}: {t}", "- **Goal**: g", ""]
    lines += ["## Rollback plan", "", "Delete the branch.", ""]
    return "\n".join(lines)


def _slugs(plan: dict) -> list[str]:
    return [s["slug"] for s in plan["subtasks"]]


def _acs(plan: dict) -> list[list[str]]:
    return [s["acceptance_criteria"] for s in plan["subtasks"]]


# -- the pure builder: the two real shapes -----------------------------------


def test_skill_shape_splits_into_four():
    plan = _build(SKILL_SHAPE_PLAN, _parent_fm())
    assert plan is not None
    # Phase 5 ("Gates + fixes + diff check") owns no criterion → folds into Phase 4.
    assert _slugs(plan) == [f"{PARENT}-p1", f"{PARENT}-p2", f"{PARENT}-p3", f"{PARENT}-p4-5"]
    assert _acs(plan) == [
        [S[0], GATE_AC],
        [S[1], S[2], GATE_AC],
        [S[3], S[4], GATE_AC],
        [S[5], GATE_AC],
    ]
    subs = plan["subtasks"]
    plan_ref = f"Worker/runs/{RUN_ID}/implementation_plan.md"
    req_ref = f"Worker/runs/{RUN_ID}/requirements.md"
    # next_action matches the byte shape of the hand-made leaves
    assert subs[0]["next_action"] == (
        f"{plan_ref}의 부모 계획서 Phase 1과 {req_ref}를 읽고 그 단계만 수행한다"
        "(이미 머지된 앞 부분은 고치지 않는다)")
    assert plan_ref in subs[3]["next_action"]
    assert "Phase 4·5" in subs[3]["next_action"]
    assert req_ref in subs[3]["next_action"]
    assert subs[3]["title"] == (
        f"{PARENT_TITLE} — Phase 4·5: §6 leaf split + appendix / Gates + fixes + diff check")
    for s in subs:
        assert s["if_then"] == LEAF_IF_THEN
        assert s["files"] == TOUCHES
        assert s["estimate_p50"] == "3h" and s["estimate_p90"] == "6h"
        assert s["outcome"].startswith(f"부모 {PARENT} 계획서 Phase ")
    assert subs[3]["steps"] == ["Phase 4: §6 leaf split + appendix",
                                "Phase 5: Gates + fixes + diff check"]
    assert plan["version"] == rescope.RESCOPE_SCHEMA_VERSION
    assert plan["parent_slug"] == PARENT
    assert plan["source"] == "plan-phases"
    assert plan["run_ref"] == plan_ref
    assert any(plan_ref in n and req_ref in n for n in plan["design_notes"])
    # The Files lines cite a guardrail prompt read-only; nothing from them leaks,
    # so the guardrail scan sees only the real write scope and the split is allowed.
    assert ".claude/" not in json.dumps(plan, ensure_ascii=False)
    assert rescope.can_auto_rescope(plan, _parent_fm()).allow


def test_chat_shape_splits_into_four():
    plan = _build(CHAT_SHAPE_PLAN, _parent_fm(), title="")
    assert plan is not None
    assert _slugs(plan) == [f"{PARENT}-p{k}" for k in (1, 2, 3, 4)]
    assert _acs(plan) == [
        [S[0], S[1], GATE_AC],
        [S[2], GATE_AC],
        [S[3], S[4], GATE_AC],
        [S[5], GATE_AC],
    ]
    # no H1 → the title falls back to the parent slug
    assert plan["subtasks"][1]["title"] == f"{PARENT} — Phase 2: §3 routing"
    assert rescope.can_auto_rescope(plan, _parent_fm()).allow


# -- mapping and grouping rules ---------------------------------------------


def test_section_key_is_whole_token():
    text = _phases_plan("§10 ten", "§2 two")
    ok = _parent_fm(acceptance_criteria=["§10 ten", "§2 two", GATE_AC])
    assert _slugs(_build(text, ok)) == [f"{PARENT}-p1", f"{PARENT}-p2"]
    # §1 must not be read out of Phase 1's §10 — nothing owns it → no split
    bad = _parent_fm(acceptance_criteria=["§1 one", "§2 two", GATE_AC])
    assert _build(text, bad) is None


def test_range_keys_expand():
    text = _phases_plan("§1~§2 first half", "§3 rest")
    fm = _parent_fm(acceptance_criteria=["§1 a", "§2 b", "§3 c", GATE_AC])
    assert _acs(_build(text, fm)) == [["§1 a", "§2 b", GATE_AC], ["§3 c", GATE_AC]]
    # a criterion keyed by a range lands where the whole range lives
    fm2 = _parent_fm(acceptance_criteria=["§1~§2 a and b", "§3 c", GATE_AC])
    assert _acs(_build(text, fm2))[0] == ["§1~§2 a and b", GATE_AC]
    # ... and one that straddles two Phases is ambiguous
    fm3 = _parent_fm(acceptance_criteria=["§2~§3 b and c", "§1 a", GATE_AC])
    assert _build(text, fm3) is None


def test_more_than_five_groups_merge_to_five():
    text = _phases_plan(*(f"§{k} part" for k in range(1, 8)))
    fm = _parent_fm(acceptance_criteria=[f"§{k} item" for k in range(1, 8)] + [GATE_AC])
    plan = _build(text, fm)
    assert _slugs(plan) == [f"{PARENT}-p1-2", f"{PARENT}-p3-4", f"{PARENT}-p5",
                            f"{PARENT}-p6", f"{PARENT}-p7"]
    assert _acs(plan)[0] == ["§1 item", "§2 item", GATE_AC]
    assert "Phase 1·2" in plan["subtasks"][0]["next_action"]
    assert rescope.can_auto_rescope(plan, fm).allow


def test_leading_phase_without_criteria_folds_forward():
    text = _phases_plan("Setup", "§1 one", "§2 two")
    fm = _parent_fm(acceptance_criteria=["§1 one", "§2 two", GATE_AC])
    plan = _build(text, fm)
    assert _slugs(plan) == [f"{PARENT}-p1-2", f"{PARENT}-p3"]
    assert _acs(plan) == [["§1 one", GATE_AC], ["§2 two", GATE_AC]]


_TWO = ["§1 one", "§2 two", GATE_AC]

_FAIL_CLOSED = [
    ("one-phase", _phases_plan("§1 one"), ["§1 one", GATE_AC]),
    ("no-phases-section",
     "# Plan\n\n## Steps\n\n### Phase 1: §1 one\n\n### Phase 2: §2 two\n", _TWO),
    ("two-phases-sections",
     _phases_plan("§1 one", "§2 two") + "\n## Phases\n\nSee above.\n", _TWO),
    ("numbering-gap",
     "## Phases\n\n### Phase 1: §1 one\n\n### Phase 3: §2 two\n", _TWO),
    ("out-of-order",
     "## Phases\n\n### Phase 2: §2 two\n\n### Phase 1: §1 one\n", _TWO),
    ("unnumbered-phase-heading",
     "## Phases\n\n### Phase 1: §1 one\n\n### Phase 2a: §2 two\n\n### Phase 2: tail\n", _TWO),
    ("headings-only-in-a-fence",
     "## Phases\n\n```\n### Phase 1: §1 one\n### Phase 2: §2 two\n```\n", _TWO),
    ("keyless-non-gate", _phases_plan("§1 one", "§2 two"), ["§1 one", "§2 two", "tests pass"]),
    ("backtick-without-green", _phases_plan("§1 one", "§2 two"),
     ["§1 one", "§2 two", "`docs/design/example.md` 갱신"]),
    ("key-in-two-phases", _phases_plan("§1 one", "§1 again + §2"), _TWO),
    ("key-in-no-phase", _phases_plan("§1 one", "§2 two"), ["§1 one", "§2 two", "§3 orphan"]),
    ("only-gates", _phases_plan("§1 one", "§2 two"), [GATE_AC]),
    ("empty-criteria", _phases_plan("§1 one", "§2 two"), []),
    ("non-list-criteria", _phases_plan("§1 one", "§2 two"), "§1 one"),
    ("blank-criterion", _phases_plan("§1 one", "§2 two"), ["§1 one", "§2 two", "  "]),
    ("empty-plan", "", _TWO),
    ("none-plan", None, _TWO),
    ("inverted-range-in-title", _phases_plan("§2~§1 odd", "§3 c"), ["§1 a", "§3 c"]),
    ("too-wide-range", _phases_plan("§1~§99 everything", "§100 c"), ["§1 a", "§100 c"]),
    ("trailing-ac-less-only", _phases_plan("§1 one", "Gates"), ["§1 one", GATE_AC]),
]


@pytest.mark.parametrize("text,acs", [(t, a) for _, t, a in _FAIL_CLOSED],
                         ids=[i for i, _, _ in _FAIL_CLOSED])
def test_fail_closed(text, acs):
    assert _build(text, _parent_fm(acceptance_criteria=acs)) is None


def test_fail_closed_on_unusable_parent():
    assert rescope.rescope_from_plan_phases(
        SKILL_SHAPE_PLAN, None, parent_slug=PARENT, run_id=RUN_ID) is None
    for slug in ("", "../escape", "a/b"):
        assert rescope.rescope_from_plan_phases(
            SKILL_SHAPE_PLAN, _parent_fm(), parent_slug=slug, run_id=RUN_ID) is None
    assert rescope.rescope_from_plan_phases(
        SKILL_SHAPE_PLAN, _parent_fm(), parent_slug=PARENT, run_id="") is None


def test_parse_plan_phases_reads_only_the_phases_section():
    phases = rescope.parse_plan_phases(SKILL_SHAPE_PLAN)
    assert phases == [
        (1, "Scaffold + §1 census"),
        (2, "§2 detection + §3 ladder"),
        (3, "§4 safety + §5 measurement"),
        (4, "§6 leaf split + appendix"),
        (5, "Gates + fixes + diff check"),
    ]
    # An unfenced `# …` line in a Phase body does not end the section (only an H2
    # does), and Phase-like headings in a later section are not Phases.
    text = ("## Phases\n\n### Phase 1: §1 one\n- run:\n# a shell comment, not a heading\n\n"
            "### Phase 2: §2 two\n\n## Notes\n\n### Phase 3: not a phase\n")
    assert rescope.parse_plan_phases(text) == [(1, "§1 one"), (2, "§2 two")]


# -- guard additions ----------------------------------------------------------
# These use a plain hand-written split plan (the shape an Implement/Verify stage
# leaves), not the Phase builder: the guards apply to every split plan.


def _plain_plan(n: int = 2) -> dict:
    return {
        "version": rescope.RESCOPE_SCHEMA_VERSION,
        "parent_slug": PARENT,
        "reason": "exceeds a single Implement stage",
        "design_notes": ["결정 사실 A"],
        "subtasks": [
            {"slug": f"{PARENT}-part-{i}", "title": f"하위 {i}",
             "outcome": f"하위 {i} 완료 조건", "acceptance_criteria": [f"하위 {i} AC"],
             "files": list(TOUCHES), "spec": {"goal": f"하위 {i} 목표"}}
            for i in range(n)
        ],
    }


_ABSENT = object()


@pytest.mark.parametrize("value,allowed", [
    (True, False), ("true", False), ("yes", False),
    (False, True), ("false", True), (_ABSENT, True),
])
def test_human_gate_denies(value, allowed):
    fm = _parent_fm() if value is _ABSENT else _parent_fm(human_gate=value)
    decision = rescope.can_auto_rescope(_plain_plan(), fm)
    assert decision.allow is allowed
    if not allowed:
        assert "human_gate" in decision.reason


# -- the writer ------------------------------------------------------------------


@pytest.fixture
def plan_env(tmp_path: Path):
    items = tmp_path / "items"
    items.mkdir()
    run_dir = tmp_path / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "implementation_plan.md").write_text(SKILL_SHAPE_PLAN, encoding="utf-8")
    (items / f"{PARENT}.md").write_text(_render_md(_parent_fm()), encoding="utf-8")
    return tmp_path, items, run_dir


def test_writer_writes_once_never_overwrites(plan_env):
    _tmp, items, run_dir = plan_env
    note = rescope.write_plan_rescope(run_dir, parent_slug=PARENT, items_dir=items)
    assert isinstance(note, str) and note.startswith("rescope.json written"), note
    dest = run_dir / rescope.RESCOPE_FILENAME
    first = dest.read_bytes()
    # the file holds exactly what the pure builder makes of the parent md
    expected = _build(SKILL_SHAPE_PLAN, repos._frontmatter(items / f"{PARENT}.md"))
    assert expected is not None
    assert rescope.load_rescope(run_dir) == expected
    # a second call leaves it byte-identical
    note2 = rescope.write_plan_rescope(run_dir, parent_slug=PARENT, items_dir=items)
    assert "already present" in note2
    assert dest.read_bytes() == first
    # a file that was already there (e.g. the plan agent's own) is never replaced
    dest.write_text('{"keep": true}', encoding="utf-8")
    rescope.write_plan_rescope(run_dir, parent_slug=PARENT, items_dir=items)
    assert dest.read_text(encoding="utf-8") == '{"keep": true}'
    assert not list(run_dir.glob("*.tmp"))


def _rewrite_parent(**over):
    def mutate(tmp, items, run_dir):
        (items / f"{PARENT}.md").write_text(_render_md(_parent_fm(**over)), encoding="utf-8")
        return run_dir
    return mutate


def _child_exists(tmp, items, run_dir):
    (items / f"{PARENT}-p1.md").write_text("---\nstatus: open\n---\n\n# unrelated\n",
                                           encoding="utf-8")
    return run_dir


def _parent_missing(tmp, items, run_dir):
    (items / f"{PARENT}.md").unlink()
    return run_dir


def _plan_missing(tmp, items, run_dir):
    (run_dir / "implementation_plan.md").unlink()
    return run_dir


def _run_dir_is_a_file(tmp, items, run_dir):
    f = tmp / f"20261113T000001-{PARENT}"
    f.write_text("not a directory", encoding="utf-8")
    return f


_WRITER_FAIL_CLOSED = [
    ("auto-merge-false", _rewrite_parent(auto_merge=False)),
    ("human-gate", _rewrite_parent(human_gate=True)),
    ("live-tag", _rewrite_parent(tags=["todo", "worker", "live"])),
    ("guardrail-touches", _rewrite_parent(touches=["Worker/planner_policy.yaml"])),
    ("already-auto-rescoped", _rewrite_parent(auto_rescoped_from="older-parent")),
    ("if-then-asks-a-human", _rewrite_parent(if_then="분할 전에 사람에게 확인한다")),
    ("keyless-criteria", _rewrite_parent(acceptance_criteria=["tests pass", "lint clean"])),
    ("child-md-exists", _child_exists),
    ("parent-md-missing", _parent_missing),
    ("plan-md-missing", _plan_missing),
    ("run-dir-is-a-file", _run_dir_is_a_file),
]


@pytest.mark.parametrize("mutate", [m for _, m in _WRITER_FAIL_CLOSED],
                         ids=[i for i, _ in _WRITER_FAIL_CLOSED])
def test_writer_fail_closed_and_never_raises(plan_env, mutate):
    tmp, items, run_dir = plan_env
    run_dir = mutate(tmp, items, run_dir)
    note = rescope.write_plan_rescope(run_dir, parent_slug=PARENT, items_dir=items)
    assert isinstance(note, str) and note.startswith("no split"), note
    assert not (Path(run_dir) / rescope.RESCOPE_FILENAME).exists()
    if Path(run_dir).is_dir():
        assert not list(Path(run_dir).glob("*.tmp"))


# -- generator: touches inheritance ----------------------------------------------


@pytest.fixture
def templates(tmp_path: Path) -> Path:
    d = tmp_path / "templates"
    d.mkdir()
    shutil.copy(paths.vault_root() / "Todo" / "templates" / "auto-worker-task-template.md",
                d / "auto-worker-task-template.md")
    return d


def _generate(items: Path, templates: Path, fm: dict, plan: dict | None = None) -> list[Path]:
    items.mkdir(parents=True, exist_ok=True)
    return rescope.generate_subtasks(plan or _plain_plan(3), parent_slug=PARENT, parent_fm=fm,
                                     items_dir=items, templates_dir=templates,
                                     run_ref=f"Worker/runs/{RUN_ID}/run.log")


def test_children_inherit_touches_block_list(tmp_path, templates):
    out = _generate(tmp_path / "items", templates, _parent_fm())
    assert [p.stem for p in out] == [f"{PARENT}-part-{i}" for i in range(3)]
    for p in out:
        text = p.read_text(encoding="utf-8")
        fm, _ = worker.parse_frontmatter(text)
        assert fm["touches"] == TOUCHES
        assert sum(1 for line in text.splitlines() if line.startswith("touches:")) == 1
        assert "# touches:" in text                     # the template's comment survives
        errors = [i for i in _task_validate.validate_file(p) if i.level == _task_validate.ERROR]
        assert errors == [], f"{p.stem}: {[i.message for i in errors]}"

    # a string `touches` (invalid) or none at all → the child carries no key
    for i, over in enumerate(({"touches": "docs/design/example.md"}, {"touches": None})):
        kids = _generate(tmp_path / f"items-{i}", templates, _parent_fm(**over))
        for p in kids:
            text = p.read_text(encoding="utf-8")
            assert "touches" not in worker.parse_frontmatter(text)[0]
            assert not any(line.startswith("touches:") for line in text.splitlines())


def test_plan_phase_children_are_valid_tasks(tmp_path, templates):
    """The children the Phase builder describes come out of the existing generator
    as valid, chained worker tasks that keep the plan's criteria and leaf rules."""
    fm = _parent_fm()
    plan = _build(SKILL_SHAPE_PLAN, fm)
    out = _generate(tmp_path / "items", templates, fm, plan)
    assert [p.stem for p in out] == [f"{PARENT}-p1", f"{PARENT}-p2", f"{PARENT}-p3",
                                     f"{PARENT}-p4-5"]
    for p, sub in zip(out, plan["subtasks"]):
        fm_c, _ = worker.parse_frontmatter(p.read_text(encoding="utf-8"))
        assert fm_c["acceptance_criteria"] == sub["acceptance_criteria"]
        assert fm_c["next_action"] == sub["next_action"]
        assert fm_c["if_then"] == LEAF_IF_THEN
        assert fm_c["touches"] == TOUCHES
        errors = [i for i in _task_validate.validate_file(p) if i.level == _task_validate.ERROR]
        assert errors == [], f"{p.stem}: {[i.message for i in errors]}"


# -- executor: the plan's run_ref -------------------------------------------------


@pytest.fixture
def vault(tmp_path: Path):
    root = tmp_path / "vault"
    items = root / "Todo" / "items"
    tdir = root / "Todo" / "templates"
    runs = root / "Worker" / "runs"
    for d in (items, tdir, runs, root / "Worker" / "assignments", root / "Worker" / "_state"):
        d.mkdir(parents=True, exist_ok=True)
    shutil.copy(paths.vault_root() / "Todo" / "templates" / "auto-worker-task-template.md",
                tdir / "auto-worker-task-template.md")
    ledger = Ledger(root / "Worker" / "assignments")
    assert Path(ledger.root).parent.parent == root
    return root, items, tdir, runs, ledger


@pytest.mark.parametrize("run_ref,expect", [
    (f"Worker/runs/{RUN_ID}/implementation_plan.md", "implementation_plan.md"),
    ("/etc/passwd", "run.log"),
    (f"Worker/runs/{RUN_ID}/implementation_plan.md\n## injected", "run.log"),
    (f"Worker/runs/../../{RUN_ID}/implementation_plan.md", "run.log"),
], ids=["plan-ref", "absolute", "two-lines", "dot-dot"])
def test_execute_prefers_plan_run_ref(vault, tmp_path, run_ref, expect):
    root, items, tdir, runs, ledger = vault
    (items / f"{PARENT}.md").write_text(_render_md(_parent_fm()), encoding="utf-8")
    plan = _plain_plan()
    plan["run_ref"] = run_ref
    d = runs / RUN_ID
    d.mkdir()
    (d / rescope.RESCOPE_FILENAME).write_text(json.dumps(plan, ensure_ascii=False),
                                              encoding="utf-8")
    ledger.save(Assignment(task_id=PARENT, agent_id="worker-default", corr_id="c1",
                           state=AWAITING_HUMAN, blocked_on="needs re-scope",
                           last_alert_kind=rescope.ALERT_RESCOPE_PENDING,
                           payload={"last_run_dir": RUN_ID}))

    done = rescope.execute_rescope(ledger, root, None, items_dir=items, templates_dir=tdir,
                                   db_path=tmp_path / "todo.db")

    assert done == [PARENT]
    body = (items / f"{PARENT}-part-0.md").read_text(encoding="utf-8")
    header = next(line for line in body.splitlines() if line.startswith("## 이미 정해진 설계"))
    assert header.endswith(f"Worker/runs/{RUN_ID}/{expect})"), header
