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
