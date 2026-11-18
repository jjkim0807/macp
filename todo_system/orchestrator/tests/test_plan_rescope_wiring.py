"""Route-level wiring: a Plan stage that declares the task too big for one
Implement stage ends in an automatic split instead of a ⛔.

Drives the real path end to end with only the stage subprocess (and the git /
worktree plumbing around it) mocked:

1. StagedExecutor's plan-park branch writes `rescope.json`, built from the plan's
   `### Phase N:` headings, and still returns today's park outcome unchanged;
2. `collect()` sees a viable plan → stamps ALERT_RESCOPE_PENDING, sends zero ⛔;
3. `execute_rescope` creates the serially chained children, flips the parent to an
   off bundle and closes the park (DONE, not AWAITING_HUMAN) with one 🔀.

A contraction guard pins that a parent the split must not touch
(`auto_merge: false`) still reaches a human exactly as before: no file, one ⛔.

The executor harness follows test_runner_implement_rescue.ImplementRescueWiringTest;
the tmp vault and the Orchestrator helper follow todo_system/tests/test_rescope_sweep.py.
Identifiers are neutral.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from todo_system import cli, paths, worker
from todo_system.orchestrator import message as m
from todo_system.orchestrator import repos, rescope, stages
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator.assignment import (
    AWAITING_HUMAN, DONE, IN_PROGRESS, Assignment, Ledger,
)
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import (
    ALERT_BLOCKED, ALERT_RESCOPE_PENDING, Orchestrator,
)
from todo_system.orchestrator.registry import Agent, Registry
from todo_system.orchestrator.runner import StagedExecutor
from todo_system.orchestrator.tests.test_plan_rescope import (
    ACS, ALLOWED_TOOLS, CHAT_SHAPE_PLAN, GATE_AC, REPO_URL, TOUCHES, _render_md,
)
from todo_system.orchestrator.tests.test_plan_split_block import (
    CHILDREN, PARENT, PARENT_MD, PLAN_1118, SPLIT_1118, with_block,
)

SLUG = "example-channel-design"
PARK_SUMMARY = ("blocked on task exceeds a single Implement stage — needs "
                "re-scoping (plan stage) — not a defect")
PARK_KEYS = {"branch", "run_dir", "verify", "stages", "blocked_on", "repo"}


def _parent_md(*, auto_merge: bool = True) -> str:
    fm = {
        "tags": ["todo", "private", "worker", "architecture"],
        "status": "open",
        "worker_mode": "auto",
        "auto_merge": auto_merge,
        "max_turns": 200,
        "model_tier": "heavy",
        "repo": REPO_URL,
        "allowed_tools": ALLOWED_TOOLS,
        "touches": list(TOUCHES),
        "outcome": "docs/design/example.md가 main에 머지돼 채널 설계가 정리된다",
        "next_action": "지금 들어오는 입구를 전수표로 만든다",
        "if_then": "전수표가 나오면 → 채널 모델과 라우팅 규칙을 쓴다",
        "estimate_p50": "3h",
        "estimate_p90": "6h",
        "acceptance_criteria": list(ACS),
    }
    return _render_md(fm, "example channel design")


@pytest.fixture
def vault(tmp_path: Path, monkeypatch):
    root = tmp_path / "vault"
    items = root / "Todo" / "items"
    templates = root / "Todo" / "templates"
    runs = root / "Worker" / "runs"
    for d in (items, templates, runs, root / "Worker" / "assignments", root / "Worker" / "_state"):
        d.mkdir(parents=True, exist_ok=True)
    shutil.copy(paths.vault_root() / "Todo" / "templates" / "auto-worker-task-template.md",
                templates / "auto-worker-task-template.md")
    ledger = Ledger(root / "Worker" / "assignments")
    # collect()/execute_rescope find run dirs at ledger.root/../runs; the runner
    # writes them under stages.RUNS_DIR — the two must be the same directory.
    assert Path(ledger.root).parent / "runs" == runs
    monkeypatch.setattr(cli, "ITEMS_DIR", items)
    return root, items, templates, runs, ledger


def _agent() -> Agent:
    return Agent(id="worker-default", kind="ai-local", capabilities=["git"],
                 repo="~/Worker/example")


def _execute_plan_stage(vault_root: Path, clone: Path, slug: str, plan_body: str):
    """Run StagedExecutor through the plan stage with the subprocess mocked: the
    mocked plan stage writes `plan_body` as this run's implementation_plan.md."""
    runs = vault_root / "Worker" / "runs"
    progress: list[str] = []

    def current_run_dir() -> Path | None:
        found = [p for p in runs.iterdir() if p.is_dir() and p.name.endswith(f"-{slug}")]
        return max(found, key=lambda p: p.name) if found else None

    def fake_run(argv, **kw):
        rd = current_run_dir()
        if rd is not None:
            (rd / stages.STAGE_ARTIFACT["plan"]).write_text(plan_body, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")

    vp = subprocess.CompletedProcess([], 0, "", "")
    vp.problem = None
    vp.orphaned_commits = None
    msg = m.Envelope(to="worker-default", type=m.TASK_ASSIGN, task_id=slug, corr_id="c1",
                     payload={"repo": REPO_URL, "auto_merge": True, "model_tier": "heavy"})
    with contextlib.ExitStack() as stack:
        for p in (
            mock.patch.object(repos, "ensure_worktree", return_value=clone),
            mock.patch.object(stages, "restore_main"),
            mock.patch.object(stages, "install_push_guard"),
            mock.patch.object(stages, "default_branch", return_value="main"),
            mock.patch.object(stages, "prepare_external_branch",
                              return_value=subprocess.CompletedProcess([], 0, "", "")),
            mock.patch.object(stages, "prepare_vault_branch", return_value=vp),
            mock.patch.object(stages, "remote_branch_exists", return_value=True),
            mock.patch.object(stages, "_VAULT_DATA_ROOT", vault_root),
            mock.patch.object(stages, "RUNS_DIR", runs),
            mock.patch.object(stages, "record_related", return_value=None),
            mock.patch.object(stages, "build_stage_argv", return_value=["true"]),
            mock.patch.object(stages, "rescue_uncommitted",
                              return_value={"committed": False, "sha": None,
                                            "note": "nothing to rescue"}),
            mock.patch.object(stages, "checkpoint_push", return_value={}),
            mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")),
            mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
            mock.patch.object(StagedExecutor, "_acquire_checkout_lease", return_value=None),
            mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
            mock.patch.object(StagedExecutor, "_pipeline_for",
                              return_value=("plan", "implement")),
            mock.patch("todo_system.worker.runner_heartbeat_start"),
            mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run),
            mock.patch("subprocess.run", side_effect=fake_run),
        ):
            stack.enter_context(p)
        out = StagedExecutor().execute(msg, _agent(), progress=progress.append)
    return out, progress


def _orch(root: Path, ledger: Ledger):
    mb = FileMailbox(root / "Worker" / "mb")
    sent: list = []
    routed: list = []

    def route(text, *, level=None, event_key=None, cooldown_s=0.0):
        routed.append((text, level, event_key))
        return "send", text

    orch = Orchestrator(mb, Registry([]), ledger,
                        notify=lambda t, link=None: sent.append((t, link)),
                        alert_route=route)
    orch._task_title = lambda slug: "example channel design"
    return orch, mb, sent, routed


def _collect_fail(root: Path, ledger: Ledger, out):
    """Hand the runner's outcome to collect() the way Runner.handle sends it."""
    ledger.save(Assignment(task_id=SLUG, agent_id="worker-default", corr_id="c1",
                           state=IN_PROGRESS, kind="ai-local"))
    orch, mb, sent, routed = _orch(root, ledger)
    mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="worker-default",
                       task_id=SLUG, corr_id="c1",
                       payload={"reason": out.summary, **out.payload}))
    orch.collect()
    return ledger.load(SLUG), sent, routed


def _notifier():
    calls: list[tuple[str, str, str]] = []

    def fake(msg, *, event_key, level="digest"):
        calls.append((msg, event_key, level))

    return fake, calls


def test_plan_park_auto_splits_end_to_end(vault, tmp_path):
    root, items, templates, runs, ledger = vault
    (items / f"{SLUG}.md").write_text(_parent_md(), encoding="utf-8")

    out, progress = _execute_plan_stage(root, tmp_path / "clone", SLUG, CHAT_SHAPE_PLAN)

    # 1. runner: the park outcome is unchanged, and the split plan sits beside it
    assert out.ok is False
    assert out.summary == PARK_SUMMARY
    assert set(out.payload) == PARK_KEYS
    run_dir = Path(out.payload["run_dir"])
    assert run_dir.parent == runs
    plan = rescope.load_rescope(run_dir)
    assert plan is not None
    assert [s["slug"] for s in plan["subtasks"]] == [f"{SLUG}-p{k}" for k in (1, 2, 3, 4)]
    assert any(p.startswith("plan split: rescope.json written") for p in progress), progress

    # 2. collect(): a viable plan → ALERT_RESCOPE_PENDING, zero ⛔
    a, sent, routed = _collect_fail(root, ledger, out)
    assert a.state == AWAITING_HUMAN
    assert a.last_alert_kind == ALERT_RESCOPE_PENDING
    assert a.payload.get("last_run_dir") == run_dir.name
    assert sent == [] and routed == []

    # 3. the auto-split sweep: children, chain, inheritance, bundle, one 🔀
    notify, calls = _notifier()
    done = rescope.execute_rescope(ledger, root, notify, items_dir=items,
                                   templates_dir=templates, db_path=tmp_path / "todo.db")
    assert done == [SLUG]
    row = ledger.load(SLUG)
    assert row.state == DONE and row.state != AWAITING_HUMAN
    assert row.blocked_on == ""

    kids = [items / f"{SLUG}-p{k}.md" for k in (1, 2, 3, 4)]
    assert all(k.exists() for k in kids)
    fms = [worker.parse_frontmatter(k.read_text(encoding="utf-8"))[0] for k in kids]
    assert fms[0]["worker_mode"] == "auto"
    for prev, fm in zip(kids, fms[1:]):
        assert fm["worker_mode"] == "blocked"
        assert fm["on_unblock"] == "auto"
        assert fm["depends_on"] == [prev.stem]
    plan_ref = f"Worker/runs/{run_dir.name}/implementation_plan.md"
    for fm in fms:
        assert fm["parent"] == f"[[{SLUG}]]"
        assert fm["repo"] == REPO_URL
        assert fm["allowed_tools"] == ALLOWED_TOOLS
        assert fm["model_tier"] == "heavy"
        assert fm["touches"] == TOUCHES
        assert plan_ref in fm["next_action"]
        assert fm["acceptance_criteria"][-1] == GATE_AC
    assert fms[2]["acceptance_criteria"] == [ACS[3], ACS[4], GATE_AC]

    pfm = worker.parse_frontmatter((items / f"{SLUG}.md").read_text(encoding="utf-8"))[0]
    assert pfm["worker_mode"] == "off"
    assert str(pfm["auto_rescoped"]).lower() == "true"

    assert len(calls) == 1
    assert calls[0][0].splitlines()[0].startswith("🔀")


def test_parent_the_split_must_not_touch_still_reaches_a_human(vault, tmp_path):
    root, items, templates, runs, ledger = vault
    (items / f"{SLUG}.md").write_text(_parent_md(auto_merge=False), encoding="utf-8")

    out, progress = _execute_plan_stage(root, tmp_path / "clone", SLUG, CHAT_SHAPE_PLAN)

    assert out.ok is False
    assert out.summary == PARK_SUMMARY
    assert set(out.payload) == PARK_KEYS
    run_dir = Path(out.payload["run_dir"])
    assert not (run_dir / rescope.RESCOPE_FILENAME).exists()

    a, sent, routed = _collect_fail(root, ledger, out)
    assert a.state == AWAITING_HUMAN
    assert a.last_alert_kind == ALERT_BLOCKED
    assert len(sent) == 1 and sent[0][0].splitlines()[0].startswith("⛔")
    assert not list(items.glob(f"{SLUG}-p*.md"))


# ---------------------------------------------------------------------------
# The plan's own split block (test_plan_split_block.py), through the tick.
#
# The parent here is the 2026-11-18 one: six criteria with no `§N` key, so the
# Phase-heading builder has nothing to map and the park used to reach a person.
# These drive `Orchestrator.tick()` itself — collect() and reconcile_rescope are
# the real methods, in the order the tick calls them — because that is where the
# auto-split is wired: reconcile_rescope → rescope.execute_rescope.
# ---------------------------------------------------------------------------

_TICK_STUBS = (
    "reroute_remote", "reap_stale", "reconcile_unparked", "reconcile_resumes",
    "reconcile_rescope", "_dispatch_serial_per_checkout", "collect",
    "reconcile_split_parents", "reconcile_batch_gate", "reconcile_merge_holds",
    "reconcile_merged", "reconcile_closed", "reconcile_unblocked", "reconcile_parents",
    "reconcile_human_ready", "reconcile_queue", "_advote_bridge_tick",
    "_beta_sweep_tick", "_project_stall_tick", "renudge_due",
)
_LIVE = ("collect", "reconcile_rescope")


def _tick(orch, live: tuple = _LIVE):
    """`orch.tick([])` with every sweep stubbed except the ones `live` names and
    the three holds answered, so the result says something about the split's place
    in the tick and nothing about this host. Copied from
    test_batch_ready_state._tick rather than imported: that module is being
    changed by a sibling task."""
    patches = [mock.patch.object(orch, name, return_value=[])
               for name in _TICK_STUBS if name not in live]
    patches += [mock.patch.object(orch, "usage_hold", return_value=None),
                mock.patch.object(orch, "paused", return_value=False),
                mock.patch.object(orch, "budget_exceeded", return_value=False)]
    for patch in patches:
        patch.start()
    try:
        return orch.tick([])
    finally:
        for patch in reversed(patches):
            patch.stop()


def _reply_fail(root: Path, ledger: Ledger, slug: str, out):
    """Save the in-flight row and queue the runner's FAIL the way Runner.handle
    sends it. collect() is NOT called: the tick does that."""
    ledger.save(Assignment(task_id=slug, agent_id="worker-default", corr_id="c1",
                           state=IN_PROGRESS, kind="ai-local"))
    orch, mb, sent, routed = _orch(root, ledger)
    mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="worker-default",
                       task_id=slug, corr_id="c1",
                       payload={"reason": out.summary, **out.payload}))
    return orch, mb, sent, routed


def _tmp_todo_paths(root: Path, tmp_path: Path, monkeypatch) -> None:
    """reconcile_rescope hands execute_rescope no paths, so the items dir, the
    templates dir and the DB come from cli.* — every one must be the tmp vault's
    (the `vault` fixture rebinds ITEMS_DIR; the other two are rebound here).
    Otherwise the children would be written into the real vault and dispatched."""
    monkeypatch.setattr(cli, "TODO_DIR", root / "Todo")
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "todo.db")
    tmp = tmp_path.resolve()
    for path in (cli.ITEMS_DIR, cli.TODO_DIR / "templates", cli.DB_PATH):
        assert tmp in Path(path).resolve().parents, path


def _one_line(text: str) -> str:
    return " ".join(text.split())


def test_1118_plan_block_splits_through_the_tick(vault, tmp_path, monkeypatch):
    root, items, templates, runs, ledger = vault
    _tmp_todo_paths(root, tmp_path, monkeypatch)
    (items / f"{PARENT}.md").write_text(PARENT_MD, encoding="utf-8")

    out, progress = _execute_plan_stage(root, tmp_path / "clone", PARENT,
                                        with_block(PLAN_1118))

    # 1. runner: the park outcome is unchanged; the split plan is the block's
    assert out.ok is False
    assert out.summary == PARK_SUMMARY
    assert set(out.payload) == PARK_KEYS
    run_dir = Path(out.payload["run_dir"])
    assert run_dir.parent == runs
    plan = rescope.load_rescope(run_dir)
    assert plan is not None
    assert [s["slug"] for s in plan["subtasks"]] == CHILDREN
    assert plan["source"] == "plan-split-block"
    assert any(p.startswith("plan split: rescope.json written") for p in progress), progress

    # 2. first tick: reconcile_rescope runs BEFORE collect, so this tick only
    #    parks the row as pending — zero ⛔, nothing created yet
    orch, _mb, sent, routed = _reply_fail(root, ledger, PARENT, out)
    result = _tick(orch)
    row = ledger.load(PARENT)
    assert row.state == AWAITING_HUMAN
    assert row.last_alert_kind == ALERT_RESCOPE_PENDING
    assert row.payload.get("last_run_dir") == run_dir.name
    assert result["rescoped"] == []
    assert sent == [] and routed == []
    assert sorted(p.name for p in items.glob("*.md")) == [f"{PARENT}.md"]

    # 3. second tick: the sweep splits it — children, chain, each child's own
    #    criteria / write scope / tier, the parent a bundle, the park closed
    result = _tick(orch)
    assert result["rescoped"] == [PARENT]
    row = ledger.load(PARENT)
    assert row.state == DONE and row.state != AWAITING_HUMAN
    assert row.blocked_on == ""

    kids = [items / f"{c}.md" for c in CHILDREN]
    assert all(k.exists() for k in kids)
    fms = [repos._frontmatter(k) for k in kids]
    assert fms[0]["worker_mode"] == "auto"
    assert "depends_on" not in fms[0] and "on_unblock" not in fms[0]
    for prev, fm in zip(CHILDREN, fms[1:]):
        assert fm["worker_mode"] == "blocked"
        assert fm["on_unblock"] == "auto"
        assert fm["depends_on"] == [prev]
    plan_ref = f"Worker/runs/{run_dir.name}/implementation_plan.md"
    parent_touches = repos._frontmatter(items / f"{PARENT}.md")["touches"]
    for fm, want in zip(fms, SPLIT_1118["subtasks"]):
        assert fm["parent"] == f"[[{PARENT}]]"
        assert fm["auto_rescoped_from"] == PARENT
        assert fm["acceptance_criteria"] == [_one_line(x) for x in want["acceptance_criteria"]]
        assert fm["touches"] == want["touches"] and fm["touches"] != parent_touches
        assert fm["model_tier"] == want["model_tier"]
        assert (fm["estimate_p50"], fm["estimate_p90"]) == (
            want["estimate_p50"], want["estimate_p90"])
        assert fm["repo"] == "https://github.com/example/macp.git"
        assert fm["auto_merge"] is True
        assert plan_ref in fm["next_action"]
    assert [fm["model_tier"] for fm in fms] == ["heavy", "heavy", "standard"]

    parent_text = (items / f"{PARENT}.md").read_text(encoding="utf-8")
    pfm = repos._frontmatter(items / f"{PARENT}.md")
    assert pfm["worker_mode"] == "off"
    assert str(pfm["auto_rescoped"]).lower() == "true"
    assert str(pfm["is_parent"]).lower() == "true"
    assert "자동 분할 → 하위 3개" in parent_text

    # 4. the alert: one 🔀 digest through the routing gate, and no ⛔ anywhere
    assert [r[1:] for r in routed] == [("digest", f"rescope:{PARENT}")]
    assert routed[0][0].splitlines()[0].startswith("🔀 자동 분할")
    assert [text.splitlines()[0][:1] for text, _link in sent] == ["🔀"]

    # 5. a third tick finds nothing left to do
    result = _tick(orch)
    assert result["rescoped"] == []
    assert len(routed) == 1 and len(sent) == 1
    assert sorted(p.name for p in items.glob("*.md")) == sorted(
        f"{s}.md" for s in [PARENT, *CHILDREN])

    # 6. nothing leaked into the vault this process resolves by default
    real_items = paths.vault_root() / "Todo" / "items"
    assert not any((real_items / f"{s}.md").exists() for s in [PARENT, *CHILDREN])


def test_1118_plan_without_block_still_reaches_a_human(vault, tmp_path, monkeypatch):
    """The control for the test above: the same parent and plan minus the block
    is the 2026-11-18 park, unchanged — no file, one ⛔, no children."""
    root, items, templates, runs, ledger = vault
    _tmp_todo_paths(root, tmp_path, monkeypatch)
    (items / f"{PARENT}.md").write_text(PARENT_MD, encoding="utf-8")

    out, progress = _execute_plan_stage(root, tmp_path / "clone", PARENT, PLAN_1118)

    assert out.ok is False
    assert out.summary == PARK_SUMMARY
    assert set(out.payload) == PARK_KEYS
    run_dir = Path(out.payload["run_dir"])
    assert not (run_dir / rescope.RESCOPE_FILENAME).exists()
    assert any(p.startswith("plan split: no split") for p in progress), progress

    orch, _mb, sent, routed = _reply_fail(root, ledger, PARENT, out)
    for _ in range(2):
        assert _tick(orch)["rescoped"] == []
    row = ledger.load(PARENT)
    assert row.state == AWAITING_HUMAN
    assert row.last_alert_kind == ALERT_BLOCKED
    assert len(sent) == 1 and sent[0][0].splitlines()[0].startswith("⛔")
    assert sorted(p.name for p in items.glob("*.md")) == [f"{PARENT}.md"]
    assert repos._frontmatter(items / f"{PARENT}.md")["worker_mode"] == "auto"


def test_unusable_block_reaches_a_human_even_when_phase_keys_map(vault, tmp_path):
    """A plan that wrote a block gets that split or none: a block the engine
    cannot use is not replaced by the Phase-heading split, even for a parent
    whose `§N` criteria that builder could map (it does, in the first test of
    this module, for the same plan without the block)."""
    root, items, templates, runs, ledger = vault
    (items / f"{SLUG}.md").write_text(_parent_md(), encoding="utf-8")
    broken = with_block(CHAT_SHAPE_PLAN, raw='```rescope\n{"subtasks": [,]}\n```\n')

    out, progress = _execute_plan_stage(root, tmp_path / "clone", SLUG, broken)

    assert out.ok is False
    assert out.summary == PARK_SUMMARY
    assert set(out.payload) == PARK_KEYS
    run_dir = Path(out.payload["run_dir"])
    assert not (run_dir / rescope.RESCOPE_FILENAME).exists()
    assert any(p.startswith("plan split: no split: plan rescope block unusable")
               for p in progress), progress

    a, sent, routed = _collect_fail(root, ledger, out)
    assert a.state == AWAITING_HUMAN
    assert a.last_alert_kind == ALERT_BLOCKED
    assert len(sent) == 1 and sent[0][0].splitlines()[0].startswith("⛔")
    assert not list(items.glob(f"{SLUG}-p*.md"))
