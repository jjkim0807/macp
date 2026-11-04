"""project_stall: the quiet-stall detector that opens ONE human decision task.

Phase-1 tests pin the PURE classifier (``classify_project``) with hand-built
ProjectView fixtures — most importantly the six-project regression the task's
if_then names (three stalled, three not) — plus a boundary case for each stall
condition (a–e), the exclusions, and the 14-day re-suppression window. Phase-2
tests drive the collector against a real tmp vault + todo.db; Phase-3/4 cover the
deterministic decision-task render, the dedup/suppression state file, and the
fail-open orchestrator wiring.

NEUTRAL identifiers only: this file sits inside the engine tree the
public-identifier gate scans (todo_system/) and is not self-excluded, so every
slug/path here is a made-up project name and all filesystem work goes under
pytest's ``tmp_path``.
"""

from __future__ import annotations

import datetime as dt

from todo_system.orchestrator import project_stall as ps
from todo_system.orchestrator.project_stall import (
    LeafView, ProjectView, classify_project,
)

# A fixed clock so every idle / suppression window is deterministic.
NOW = dt.datetime(2026, 10, 13, 12, 0, 0)


def _hours_ago(h: float) -> dt.datetime:
    return NOW - dt.timedelta(hours=h)


def _days_ago(d: float) -> dt.datetime:
    return NOW - dt.timedelta(days=d)


def _leaf(slug: str, *, status: str = "open", worker_mode: str = "auto",
          not_before: str = "", next_action: str = "", if_then: str = "",
          in_flight: bool = False) -> LeafView:
    return LeafView(slug=slug, status=status, worker_mode=worker_mode,
                    not_before=not_before, next_action=next_action, if_then=if_then,
                    in_flight=in_flight)


def _done(slug: str, **kw) -> LeafView:
    return _leaf(slug, status="done", **kw)


def _proj(slug: str, **kw) -> ProjectView:
    kw.setdefault("last_activity", _hours_ago(72))
    return ProjectView(slug=slug, **kw)


# --------------------------------------------------------------------------- #
# The six-project regression the task's if_then pins.
# --------------------------------------------------------------------------- #

def test_fixture_worldgraph_all_leaves_done_is_stalled():
    # (a): every leaf done, idle, no run, planner exhausted → stalled.
    view = _proj(
        "worldgraph-project",
        next_action="프로젝트 마무리",   # a stale umbrella action does NOT save an all-done project
        leaves=tuple(_done(f"worldgraph-{i}") for i in range(61)),
        last_activity=_hours_ago(50), planner_exhausted=True)
    verdict = classify_project(view, now=NOW)
    assert verdict.stalled is True
    assert len(verdict.done_leaves) == 61
    assert "all 61 leaves are done" in verdict.reason


def test_fixture_advote_all_done_no_planner_record_is_stalled():
    # (a) with (e) UNKNOWN (no planner/discovery record) → judged on a–d → stalled.
    view = _proj(
        "advote-voting",
        leaves=tuple(_done(f"advote-{i}") for i in range(17)),
        last_activity=_hours_ago(60),
        planner_exhausted=None, discovery_pending=None)
    assert classify_project(view, now=NOW).stalled is True


def test_fixture_gutenberg_all_done_is_stalled():
    view = _proj(
        "gutenberg-reader",
        leaves=tuple(_done(f"gutenberg-{i}") for i in range(25)),
        last_activity=_hours_ago(80))
    assert classify_project(view, now=NOW).stalled is True


def test_fixture_parent_match_app_status_waiting_is_not_stalled():
    # exclusion: a project whose own status is waiting is never a stall candidate.
    view = _proj(
        "parent-match-app", status="waiting",
        leaves=tuple(_done(f"parent-match-{i}") for i in range(5)),
        last_activity=_hours_ago(200))
    verdict = classify_project(view, now=NOW)
    assert verdict.stalled is False
    assert "status is 'waiting'" in verdict.reason


def test_fixture_ai_stock_open_auto_leaf_is_not_stalled():
    # not stalled: an open auto leaf means the worker still has work (neither a nor b).
    view = _proj(
        "ai-stock-trading-agent",
        leaves=(_done("ai-stock-1"), _leaf("ai-stock-2", worker_mode="auto")),
        last_activity=_hours_ago(200), planner_exhausted=True)
    verdict = classify_project(view, now=NOW)
    assert verdict.stalled is False
    assert "auto-dispatchable leaf" in verdict.reason


def test_fixture_research_job_search_human_working_is_not_stalled():
    # not stalled (b counter-example): open leaves are all human-owned, but a
    # current human action IS defined → a project a person is hand-developing.
    view = _proj(
        "research-staff-job-search",
        next_action="다음 공고 지원서 작성",
        leaves=(_done("research-1"),
                _leaf("research-2", worker_mode="manual"),
                _leaf("research-3", worker_mode="blocked")),
        last_activity=_hours_ago(200), planner_exhausted=True)
    verdict = classify_project(view, now=NOW)
    assert verdict.stalled is False
    assert "human action is defined" in verdict.reason


# --------------------------------------------------------------------------- #
# Condition boundaries.
# --------------------------------------------------------------------------- #

def test_b_path_all_manual_no_action_is_stalled():
    # (b): open leaves all non-auto AND no human action anywhere → stalled.
    view = _proj(
        "abandoned-side-project",
        leaves=(_leaf("asp-1", worker_mode="manual"),
                _leaf("asp-2", worker_mode="blocked")),
        last_activity=_hours_ago(50), planner_exhausted=True)
    verdict = classify_project(view, now=NOW)
    assert verdict.stalled is True
    assert "all non-auto" in verdict.reason


def test_b_path_human_action_on_a_leaf_is_not_stalled():
    # (b) counter: the human action can live on an open leaf, not the umbrella.
    view = _proj(
        "half-manual-project",
        leaves=(_leaf("hmp-1", worker_mode="manual",
                      next_action="콘솔에서 키 발급"),),
        last_activity=_hours_ago(50))
    assert classify_project(view, now=NOW).stalled is False


def test_c_run_in_flight_blocks_stall():
    view = _proj(
        "busy-project",
        leaves=tuple(_done(f"busy-{i}") for i in range(3)),
        last_activity=_hours_ago(50), run_in_flight=True)
    verdict = classify_project(view, now=NOW)
    assert verdict.stalled is False
    assert "run is in flight" in verdict.reason


def test_c_leaf_in_flight_blocks_stall():
    # the in-flight signal can sit on a leaf's ledger row, not just the umbrella.
    view = _proj(
        "busy-leaf-project",
        leaves=(_done("blp-1"), _leaf("blp-2", status="open", in_flight=True)),
        last_activity=_hours_ago(50))
    # blp-2 is open + in_flight; it is also auto by default, so the a/b gate would
    # already stop it — give it a non-auto mode to isolate the (c) path.
    view = _proj(
        "busy-leaf-project",
        leaves=(_done("blp-1"), _leaf("blp-2", status="open",
                                      worker_mode="manual", in_flight=True)),
        last_activity=_hours_ago(50))
    assert classify_project(view, now=NOW).stalled is False


def test_d_recent_activity_blocks_stall():
    view = _proj(
        "fresh-project",
        leaves=tuple(_done(f"fresh-{i}") for i in range(3)),
        last_activity=_hours_ago(10))
    verdict = classify_project(view, now=NOW)
    assert verdict.stalled is False
    assert "last activity 10h ago" in verdict.reason


def test_d_exactly_48h_is_stalled_boundary():
    # >= 48h stalls; the boundary itself (48h) is NOT "< 48h", so it stalls.
    view = _proj(
        "boundary-project",
        leaves=(_done("bp-1"),),
        last_activity=_hours_ago(48))
    assert classify_project(view, now=NOW).stalled is True


def test_d_no_activity_timestamp_is_stalled():
    # No recency evidence at all → cannot claim recent → judged stalled on a-c.
    view = _proj(
        "timeless-project",
        leaves=(_done("tp-1"),),
        last_activity=None)
    assert classify_project(view, now=NOW).stalled is True


def test_e_discovery_pending_blocks_stall():
    # (e) positive evidence: discovery is actively exploring this project → not stalled.
    view = _proj(
        "being-explored",
        leaves=tuple(_done(f"be-{i}") for i in range(4)),
        last_activity=_hours_ago(50), discovery_pending=True)
    verdict = classify_project(view, now=NOW)
    assert verdict.stalled is False
    assert "discovery is exploring" in verdict.reason


def test_e_absent_record_falls_through_to_abcd():
    view = _proj(
        "no-planner-record",
        leaves=(_done("npr-1"),),
        last_activity=_hours_ago(50),
        planner_exhausted=None, discovery_pending=None)
    assert classify_project(view, now=NOW).stalled is True


def test_e_planner_exhausted_is_noted_in_reason():
    view = _proj(
        "exhausted-project",
        leaves=(_done("ep-1"),),
        last_activity=_hours_ago(50), planner_exhausted=True)
    verdict = classify_project(view, now=NOW)
    assert verdict.stalled is True
    assert "planner exhausted" in verdict.reason


def test_e_discovery_found_nothing_still_stalls_and_is_noted():
    # discovery ran and found nothing new (False) does NOT save the project.
    view = _proj(
        "explored-empty",
        leaves=(_done("ee-1"),),
        last_activity=_hours_ago(50), discovery_pending=False)
    verdict = classify_project(view, now=NOW)
    assert verdict.stalled is True
    assert "discovery found nothing new" in verdict.reason


# --------------------------------------------------------------------------- #
# Exclusions.
# --------------------------------------------------------------------------- #

def test_exclusion_decision_gate_off():
    view = _proj(
        "opted-out-project",
        leaves=(_done("oop-1"),),
        last_activity=_hours_ago(50), decision_gate_off=True)
    verdict = classify_project(view, now=NOW)
    assert verdict.stalled is False
    assert "decision_gate: off" in verdict.reason


def test_exclusion_open_decision_task():
    view = _proj(
        "already-asked",
        leaves=(_done("aa-1"),),
        last_activity=_hours_ago(50),
        open_decision_task="already-asked-next-direction")
    verdict = classify_project(view, now=NOW)
    assert verdict.stalled is False
    assert "already open" in verdict.reason


def test_exclusion_project_not_before_future():
    view = _proj(
        "shelved-project",
        leaves=(_done("sp-1"),),
        last_activity=_hours_ago(50),
        not_before="2026-12-31")
    verdict = classify_project(view, now=NOW)
    assert verdict.stalled is False
    assert "not_before" in verdict.reason


def test_cancelled_and_done_project_excluded():
    for status in ("cancelled", "done", "blocked"):
        view = _proj(
            f"{status}-project", status=status,
            leaves=(_done("x-1"),), last_activity=_hours_ago(50))
        assert classify_project(view, now=NOW).stalled is False


# --------------------------------------------------------------------------- #
# 14-day re-suppression window.
# --------------------------------------------------------------------------- #

def _suppressible() -> dict:
    return dict(
        leaves=(_done("rs-1"), _done("rs-2")),
        last_activity=_hours_ago(50),
        state_fingerprint="shape-v1")


def test_resuppressed_recent_close_same_shape_is_quiet():
    view = _proj(
        "recently-decided", **_suppressible(),
        prior_decision_closed_ts=_days_ago(5),
        prior_decision_fingerprint="shape-v1")
    verdict = classify_project(view, now=NOW)
    assert verdict.stalled is False
    assert "re-suppressed" in verdict.reason


def test_resuppression_lifts_when_shape_changes():
    # a new leaf / status change flips the fingerprint → suppression lifts.
    view = _proj(
        "recently-decided-but-changed", **_suppressible(),
        prior_decision_closed_ts=_days_ago(5),
        prior_decision_fingerprint="shape-v0")   # != current shape-v1
    assert classify_project(view, now=NOW).stalled is True


def test_resuppression_expires_after_14_days():
    view = _proj(
        "decided-long-ago", **_suppressible(),
        prior_decision_closed_ts=_days_ago(20),
        prior_decision_fingerprint="shape-v1")
    assert classify_project(view, now=NOW).stalled is True


# --------------------------------------------------------------------------- #
# Drift guard: the duplicated DISPATCHABLE_MODES must track tasks.py.
# --------------------------------------------------------------------------- #

def test_dispatchable_modes_match_tasks_module():
    from todo_system.orchestrator import tasks
    assert ps.DISPATCHABLE_MODES == tasks.DISPATCHABLE_MODES


def test_closed_statuses_match_tasks_module():
    from todo_system.orchestrator import repos, tasks
    assert ps.CLOSED_STATUSES is tasks.CLOSED_STATUSES
    assert repos.CLOSED_STATUSES is tasks.CLOSED_STATUSES
    assert "canceled" in tasks.CLOSED_STATUSES and "cancelled" in tasks.CLOSED_STATUSES


# =========================================================================== #
# Phase 2 — collector against a real tmp vault + todo.db (import-md).
# =========================================================================== #

from pathlib import Path   # noqa: E402

import pytest   # noqa: E402

from todo_system import cli   # noqa: E402
from todo_system.orchestrator.assignment import (   # noqa: E402
    DISPATCHED, Assignment, Ledger,
)
from todo_system.orchestrator.project_stall import (   # noqa: E402
    collect_project_views, decision_slug, project_fingerprint,
)


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "vault"
    items = root / "Todo" / "items"
    items.mkdir(parents=True)
    (root / "Worker" / "assignments").mkdir(parents=True)
    (root / "Worker" / "_state").mkdir(parents=True)
    (root / "Worker" / "runs").mkdir(parents=True)
    db = root / "Todo" / "todo.db"
    monkeypatch.setattr(cli, "ROOT", root)
    monkeypatch.setattr(cli, "TODO_DIR", root / "Todo")
    monkeypatch.setattr(cli, "ITEMS_DIR", items)
    monkeypatch.setattr(cli, "DB_PATH", db)
    return root, items, db


def _import(db: Path) -> None:
    con = cli.db_connect(db)
    cli.init_db(con)
    cli.import_markdown_to_db(con)
    con.close()


def _task_md(items: Path, slug: str, *, status: str = "open", worker_mode: str = "auto",
             parent: str | None = None, is_parent: bool = False,
             updated: str = "2026-10-05", next_action: str = "", if_then: str = "",
             not_before: str = "", decision_gate: str | None = None,
             tags: tuple[str, ...] = ("todo",)) -> Path:
    lines = ["---", "tags:"]
    lines += [f"  - {t}" for t in tags]
    lines.append(f"status: {status}")
    if worker_mode:
        lines.append(f"worker_mode: {worker_mode}")
    if parent:
        lines.append(f'parent: "[[{parent}]]"')
    if is_parent:
        lines.append("is_parent: true")
    lines.append(f"updated: {updated}")
    if next_action:
        lines.append(f'next_action: "{next_action}"')
    if if_then:
        lines.append(f'if_then: "{if_then}"')
    if not_before:
        lines.append(f"not_before: {not_before}")
    if decision_gate is not None:
        lines.append(f"decision_gate: {decision_gate}")
    lines += ["outcome: 테스트 프로젝트", "---", "", f"# {slug}", "", "- [ ] step", ""]
    path = items / f"{slug}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _view_by_slug(views, slug):
    return next((v for v in views if v.slug == slug), None)


def test_collector_detects_is_parent_umbrella_and_rolls_up_leaves(vault):
    root, items, db = vault
    _task_md(items, "worldgraph-project", worker_mode="off", is_parent=True)
    _task_md(items, "worldgraph-1", parent="worldgraph-project", status="done")
    _task_md(items, "worldgraph-2", parent="worldgraph-project", status="done")
    _task_md(items, "worldgraph-3", parent="worldgraph-project", status="open",
             worker_mode="manual")
    _import(db)

    views = collect_project_views(db, items_dir=items)
    view = _view_by_slug(views, "worldgraph-project")
    assert view is not None
    assert {leaf.slug for leaf in view.leaves} == {
        "worldgraph-1", "worldgraph-2", "worldgraph-3"}
    assert sum(1 for leaf in view.leaves if leaf.status == "done") == 2


def test_collector_detects_named_parent_without_is_parent_flag(vault):
    # advote-voting shape: kids link by `parent:` only; the umbrella carries no
    # is_parent flag but is still a top-level project.
    root, items, db = vault
    _task_md(items, "advote-voting", worker_mode="off")     # no is_parent
    _task_md(items, "advote-1", parent="advote-voting", status="done")
    _import(db)

    view = _view_by_slug(collect_project_views(db, items_dir=items), "advote-voting")
    assert view is not None and len(view.leaves) == 1


def test_collector_skips_a_task_that_has_a_parent_of_its_own(vault):
    root, items, db = vault
    _task_md(items, "top", worker_mode="off", is_parent=True)
    _task_md(items, "top-child", parent="top", status="open", worker_mode="auto")
    _import(db)

    views = collect_project_views(db, items_dir=items)
    assert _view_by_slug(views, "top") is not None
    assert _view_by_slug(views, "top-child") is None     # a child is never an umbrella


def test_collector_nested_subtree_counts_leaves_only(vault):
    # project -> mid (a parent) -> two leaves. `mid` is an intermediate parent and
    # must NOT be counted as a leaf.
    root, items, db = vault
    _task_md(items, "proj", worker_mode="off", is_parent=True)
    _task_md(items, "proj-mid", parent="proj", worker_mode="off", is_parent=True)
    _task_md(items, "proj-mid-a", parent="proj-mid", status="done")
    _task_md(items, "proj-mid-b", parent="proj-mid", status="open", worker_mode="auto")
    _import(db)

    view = _view_by_slug(collect_project_views(db, items_dir=items), "proj")
    assert {leaf.slug for leaf in view.leaves} == {"proj-mid-a", "proj-mid-b"}


def test_collector_run_in_flight_from_ledger(vault):
    root, items, db = vault
    _task_md(items, "busy", worker_mode="off", is_parent=True)
    _task_md(items, "busy-1", parent="busy", status="open", worker_mode="auto")
    _import(db)
    ledger = Ledger(root / "Worker" / "assignments")
    ledger.save(Assignment(task_id="busy-1", agent_id="w", corr_id="c", state=DISPATCHED))

    view = _view_by_slug(collect_project_views(db, ledger=ledger, items_dir=items), "busy")
    assert view.run_in_flight is True
    assert any(leaf.slug == "busy-1" and leaf.in_flight for leaf in view.leaves)


def test_collector_last_activity_is_newest_of_db_ledger_rundir(vault):
    root, items, db = vault
    _task_md(items, "aged", worker_mode="off", is_parent=True, updated="2026-10-01")
    _task_md(items, "aged-1", parent="aged", status="done", updated="2026-10-03")
    _import(db)
    # a run dir dated later than any `updated` date
    (root / "Worker" / "runs" / "20261007T090000-aged-1").mkdir()

    view = _view_by_slug(
        collect_project_views(db, runs_dir=root / "Worker" / "runs", items_dir=items),
        "aged")
    assert view.last_activity == dt.datetime(2026, 10, 7, 9, 0, 0)


def test_collector_open_decision_task_detected(vault):
    root, items, db = vault
    _task_md(items, "asked", worker_mode="off", is_parent=True)
    _task_md(items, "asked-1", parent="asked", status="done")
    _task_md(items, decision_slug("asked"), parent="asked", status="open",
             worker_mode="manual")
    _import(db)

    view = _view_by_slug(collect_project_views(db, items_dir=items), "asked")
    assert view.open_decision_task == "asked-next-direction"


def test_collector_decision_gate_off_read_from_md(vault):
    root, items, db = vault
    _task_md(items, "opted-out", worker_mode="off", is_parent=True, decision_gate="off")
    _task_md(items, "opted-out-1", parent="opted-out", status="done")
    _import(db)

    view = _view_by_slug(collect_project_views(db, items_dir=items), "opted-out")
    assert view.decision_gate_off is True


def test_fingerprint_excludes_decision_task_but_tracks_new_leaves():
    # the decision task's own open->done flip leaves the fingerprint unchanged …
    leaves_open = [LeafView("p-1", status="done"),
                   LeafView(decision_slug("p"), status="open")]
    leaves_closed = [LeafView("p-1", status="done"),
                     LeafView(decision_slug("p"), status="done")]
    fp_open = project_fingerprint("open", leaves_open, exclude_slug=decision_slug("p"))
    fp_closed = project_fingerprint("open", leaves_closed, exclude_slug=decision_slug("p"))
    assert fp_open == fp_closed
    # … but a genuinely new leaf changes it (suppression lifts).
    leaves_plus = leaves_closed + [LeafView("p-2", status="open")]
    fp_plus = project_fingerprint("open", leaves_plus, exclude_slug=decision_slug("p"))
    assert fp_plus != fp_closed


def test_collector_to_classifier_stalled_end_to_end(vault):
    root, items, db = vault
    _task_md(items, "worldgraph-project", worker_mode="off", is_parent=True,
             updated="2026-10-05")
    for i in range(3):
        _task_md(items, f"worldgraph-{i}", parent="worldgraph-project",
                 status="done", updated="2026-10-05")
    _import(db)

    views = collect_project_views(db, items_dir=items,
                                  runs_dir=root / "Worker" / "runs")
    view = _view_by_slug(views, "worldgraph-project")
    assert classify_project(view, now=NOW).stalled is True


def test_collector_to_classifier_active_not_stalled(vault):
    root, items, db = vault
    _task_md(items, "ai-stock-trading-agent", worker_mode="off", is_parent=True,
             updated="2026-10-05")
    _task_md(items, "ai-stock-1", parent="ai-stock-trading-agent", status="done",
             updated="2026-10-05")
    _task_md(items, "ai-stock-2", parent="ai-stock-trading-agent", status="open",
             worker_mode="auto", updated="2026-10-05")
    _import(db)

    views = collect_project_views(db, items_dir=items)
    view = _view_by_slug(views, "ai-stock-trading-agent")
    assert classify_project(view, now=NOW).stalled is False


def test_collector_closed_umbrella_still_collected_but_classifier_excludes(vault):
    # a done/cancelled umbrella is NOT in LIVE_PROJECT, so collect_projects would
    # not list it — mirror that here (nothing to classify).
    root, items, db = vault
    _task_md(items, "finished", worker_mode="off", is_parent=True, status="done")
    _task_md(items, "finished-1", parent="finished", status="done")
    _import(db)

    views = collect_project_views(db, items_dir=items)
    assert _view_by_slug(views, "finished") is None


# =========================================================================== #
# Phase 3 — deterministic render / create + state file.
# =========================================================================== #

from todo_system import task_validate as tv   # noqa: E402
from todo_system import worker as W   # noqa: E402
from todo_system.orchestrator import project_stall as psm   # noqa: E402


def _stalled_verdict(view: ProjectView):
    verdict = classify_project(view, now=NOW)
    assert verdict.stalled is True, verdict.reason
    return verdict


def _render(view: ProjectView) -> str:
    return psm.render_decision_task(view, _stalled_verdict(view), NOW.date())


def _sample_stalled_view() -> ProjectView:
    return _proj(
        "worldgraph-project", title="WorldGraph",
        tags=("todo", "worldgraph"), outcome="지식 그래프 MVP 완성",
        leaves=tuple(_done(f"worldgraph-{i}",
                           next_action="") for i in range(4)),
        last_activity=_hours_ago(50), planner_exhausted=True)


def test_decision_task_renders_and_validates_clean(tmp_path):
    view = _sample_stalled_view()
    # patch titles onto the done leaves so the body shows real names/dates
    view.leaves = tuple(
        LeafView(slug=leaf.slug, status="done", title=f"리프 {i}",
                 updated=f"2026-10-0{i + 1}")
        for i, leaf in enumerate(view.leaves))
    path = tmp_path / f"{psm.decision_slug(view.slug)}.md"
    path.write_text(_render(view), encoding="utf-8")

    issues = tv.validate_file(path)
    assert [i for i in issues if i.level == tv.ERROR] == [], issues
    assert issues == [], issues   # aspire to zero WARN too

    fm, _ = W.parse_frontmatter(path.read_text(encoding="utf-8"))
    assert fm["worker_mode"] == "manual"
    assert fm["parent"] == "[[worldgraph-project]]"
    assert fm["decision_gate_for"] == "worldgraph-project"
    assert fm["status"] == "open"


def test_decision_task_body_has_all_required_sections():
    view = _sample_stalled_view()
    view.leaves = tuple(
        LeafView(slug=f"worldgraph-{i}", status="done", title=f"리프 {i}",
                 updated=f"2026-10-0{i + 1}") for i in range(4))
    text = _render(view)
    assert "## 판정 사유" in text
    assert "지식 그래프 MVP 완성" in text          # project outcome
    assert "## 최근 완료된 leaf" in text
    assert "리프 0 (2026-10-01)" in text            # a done leaf title + date
    assert "## 남은 열린/블록 leaf" in text
    assert "planner=exhausted" in text              # planner/discovery summary
    for choice in ("①", "②", "③", "④", "⑤"):
        assert choice in text
    assert "discovery 1회" in text                  # choice ④
    assert "# 다음 방향 결정 — WorldGraph" in text   # title lives in the heading


def test_decision_task_due_is_today_plus_three():
    text = _render(_sample_stalled_view())
    fm, _ = W.parse_frontmatter(text + "\n")   # parse_frontmatter needs a trailing body
    assert fm["due"] == (NOW.date() + dt.timedelta(days=3)).isoformat()
    assert fm["updated"] == NOW.date().isoformat()


def test_decision_task_caps_done_leaves_at_ten():
    view = _proj(
        "big-project", outcome="o",
        leaves=tuple(_done(f"big-{i}", ) for i in range(15)),
        last_activity=_hours_ago(50))
    view.leaves = tuple(
        LeafView(slug=f"big-{i}", status="done", title=f"T{i}",
                 updated=f"2026-10-{(i % 28) + 1:02d}") for i in range(15))
    text = _render(view)
    shown = [ln for ln in text.splitlines()
             if ln.startswith("- T")]
    assert len(shown) == 10


def test_create_decision_task_writes_once_then_skips(tmp_path):
    view = _sample_stalled_view()
    verdict = _stalled_verdict(view)
    first = psm.create_decision_task(view, verdict, today=NOW.date(), items_dir=tmp_path)
    assert first is not None and first.exists()
    assert first.name == "worldgraph-project-next-direction.md"
    second = psm.create_decision_task(view, verdict, today=NOW.date(), items_dir=tmp_path)
    assert second is None    # the file itself is the dedup


def test_created_decision_task_imports_as_a_manual_leaf(vault):
    root, items, db = vault
    view = _sample_stalled_view()
    verdict = _stalled_verdict(view)
    _task_md(items, "worldgraph-project", worker_mode="off", is_parent=True)
    psm.create_decision_task(view, verdict, today=NOW.date(), items_dir=items)
    _import(db)

    con = cli.db_connect(db)
    row = con.execute(
        "SELECT status, worker_mode, parent_slug FROM tasks WHERE slug=?",
        ("worldgraph-project-next-direction",)).fetchone()
    con.close()
    assert tuple(row) == ("open", "manual", "worldgraph-project")


def test_sweep_due_throttle():
    assert psm._sweep_due({"last_sweep_ts": None}, NOW) is True
    recent = {"last_sweep_ts": _hours_ago(0.2).isoformat()}
    assert psm._sweep_due(recent, NOW) is False
    old = {"last_sweep_ts": _hours_ago(2).isoformat()}
    assert psm._sweep_due(old, NOW) is True


def test_apply_suppression_folds_state_into_view():
    view = _proj("p", state_fingerprint="fp")
    psm._apply_suppression(view, {"closed_ts": _days_ago(3).isoformat(),
                                  "fingerprint_at_close": "fp"})
    assert view.prior_decision_closed_ts == _days_ago(3)
    assert view.prior_decision_fingerprint == "fp"


def test_should_renotify_after_window_once():
    assert psm._should_renotify(
        {"surfaced_ts": _days_ago(8).isoformat(), "renotified": False}, NOW) is True
    assert psm._should_renotify(
        {"surfaced_ts": _days_ago(8).isoformat(), "renotified": True}, NOW) is False
    assert psm._should_renotify(
        {"surfaced_ts": _days_ago(3).isoformat(), "renotified": False}, NOW) is False


def test_state_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    assert psm.load_state(p) == {"last_sweep_ts": None, "projects": {}}
    psm.save_state({"last_sweep_ts": "x", "projects": {"a": {"surfaced_ts": "y"}}}, p)
    assert psm.load_state(p)["projects"]["a"]["surfaced_ts"] == "y"


def test_validator_flags_bad_decision_gate_and_accepts_good():
    bad = [i for i in tv.validate_frontmatter({"decision_gate": "of"})
           if i.field == "decision_gate"]
    assert bad and bad[0].level == tv.WARN
    # bare `off`/`on` (bool from yaml) and the string form are both clean
    assert tv.validate_frontmatter({"decision_gate": False}) == []
    assert tv.validate_frontmatter({"decision_gate": "off"}) == []
    assert tv.validate_frontmatter({"decision_gate_for": "worldgraph-project"}) == []
    bad_for = [i for i in tv.validate_frontmatter({"decision_gate_for": ["a"]})
               if i.field == "decision_gate_for"]
    assert bad_for and bad_for[0].level == tv.WARN


def test_enabled_env_off_switch():
    assert psm.enabled({}) is True
    assert psm.enabled({"MAESTRO_PROJECT_STALL": "0"}) is False
    assert psm.enabled({"MAESTRO_PROJECT_STALL": "off"}) is False


# =========================================================================== #
# Phase 4 — run_once sweep + orchestrator wiring (fail-open).
# =========================================================================== #

def _build_stalled_vault(items: Path, *, slug: str = "worldgraph-project",
                         n_done: int = 3, updated: str = "2026-10-05") -> None:
    _task_md(items, slug, worker_mode="off", is_parent=True, updated=updated)
    for i in range(n_done):
        _task_md(items, f"{slug}-{i}", parent=slug, status="done", updated=updated)


def _fake_notify():
    calls: list[tuple[str, str]] = []

    def notify(text, *, event_key):
        calls.append((text, event_key))

    return notify, calls


def _close_decision_md(items: Path, slug: str) -> None:
    p = items / f"{slug}-next-direction.md"
    p.write_text(p.read_text(encoding="utf-8").replace("status: open", "status: done", 1),
                 encoding="utf-8")


def test_run_once_creates_task_and_alerts_for_stalled(vault):
    root, items, db = vault
    _build_stalled_vault(items)
    _import(db)
    notify, calls = _fake_notify()

    created = psm.run_once(root, now=NOW, notify=notify)
    assert created == ["worldgraph-project-next-direction"]
    assert (items / "worldgraph-project-next-direction.md").exists()
    assert len(calls) == 1 and calls[0][1] == "project-stall:worldgraph-project"

    state = psm.load_state(psm.state_path(root))
    assert (state["projects"]["worldgraph-project"]["decision_task"]
            == "worldgraph-project-next-direction")


def test_run_once_dedups_once_decision_task_open(vault):
    root, items, db = vault
    _build_stalled_vault(items)
    _import(db)
    notify, calls = _fake_notify()

    psm.run_once(root, now=NOW, notify=notify)
    # past the 1h throttle; the decision md exists & is open -> no 2nd task/alert
    created2 = psm.run_once(root, now=NOW + dt.timedelta(hours=2), notify=notify)
    assert created2 == []
    assert len(calls) == 1


def test_run_once_throttled_within_the_hour(vault):
    root, items, db = vault
    _build_stalled_vault(items, slug="advote-voting")
    _import(db)
    notify, _ = _fake_notify()

    first = psm.run_once(root, now=NOW, notify=notify)
    assert first == ["advote-voting-next-direction"]
    # delete the created file so the ONLY thing that can block the 2nd call is the
    # 1h throttle, then call again 30 min later.
    (items / "advote-voting-next-direction.md").unlink()
    assert psm.run_once(root, now=NOW + dt.timedelta(minutes=30), notify=notify) == []


def test_run_once_skips_active_project(vault):
    root, items, db = vault
    _task_md(items, "ai-stock-trading-agent", worker_mode="off", is_parent=True,
             updated="2026-10-05")
    _task_md(items, "ai-stock-1", parent="ai-stock-trading-agent", status="open",
             worker_mode="auto", updated="2026-10-05")
    _import(db)
    notify, calls = _fake_notify()

    assert psm.run_once(root, now=NOW, notify=notify) == []
    assert calls == []
    assert not (items / "ai-stock-trading-agent-next-direction.md").exists()


def test_run_once_off_switch_creates_nothing(vault):
    root, items, db = vault
    _build_stalled_vault(items)
    _import(db)
    assert psm.run_once(root, now=NOW, env={"MAESTRO_PROJECT_STALL": "0"}) == []
    assert not (items / "worldgraph-project-next-direction.md").exists()


def test_run_once_renotifies_after_7_days_once(vault):
    root, items, db = vault
    _build_stalled_vault(items)
    _import(db)
    notify, calls = _fake_notify()

    psm.run_once(root, now=NOW, notify=notify)                         # create + alert #1
    psm.run_once(root, now=NOW + dt.timedelta(days=8), notify=notify)  # renotify #2
    psm.run_once(root, now=NOW + dt.timedelta(days=9), notify=notify)  # silent after
    assert len(calls) == 2
    assert all(c[1] == "project-stall:worldgraph-project" for c in calls)


def test_run_once_resuppresses_then_reasks_after_14_days(vault):
    root, items, db = vault
    _build_stalled_vault(items)
    _import(db)
    notify, _ = _fake_notify()

    assert psm.run_once(root, now=NOW, notify=notify) == [
        "worldgraph-project-next-direction"]
    _close_decision_md(items, "worldgraph-project")        # human dismisses it
    # the sweep right after the close records it and re-suppresses
    assert psm.run_once(root, now=NOW + dt.timedelta(days=1), notify=notify) == []
    # still quiet a week later (within 14d, shape unchanged)
    assert psm.run_once(root, now=NOW + dt.timedelta(days=8), notify=notify) == []
    # past 14 days from the close -> re-ask (overwrites the closed file)
    assert psm.run_once(root, now=NOW + dt.timedelta(days=16), notify=notify) == [
        "worldgraph-project-next-direction"]


def test_run_once_resuppression_lifts_on_new_leaf(vault):
    root, items, db = vault
    _build_stalled_vault(items)
    _import(db)
    notify, _ = _fake_notify()

    psm.run_once(root, now=NOW, notify=notify)
    _close_decision_md(items, "worldgraph-project")
    psm.run_once(root, now=NOW + dt.timedelta(days=1), notify=notify)   # record close
    # a genuinely new leaf changes the project shape -> suppression lifts early
    _task_md(items, "worldgraph-newleaf", parent="worldgraph-project",
             status="open", worker_mode="manual")
    _import(db)
    assert psm.run_once(root, now=NOW + dt.timedelta(days=3), notify=notify) == [
        "worldgraph-project-next-direction"]


# --- orchestrator wiring --------------------------------------------------- #

def _orch(root: Path, sent: list):
    from todo_system.orchestrator.mailbox import FileMailbox
    from todo_system.orchestrator.orchestrator import Orchestrator
    from todo_system.orchestrator.registry import Registry
    return Orchestrator(
        FileMailbox(root / "Worker" / "mb"), Registry([]),
        Ledger(root / "Worker" / "assignments"),
        notify=lambda t, link=None: sent.append(t),
        alert_route=lambda text, **kw: ("send", text))


def test_project_stall_tick_creates_and_notifies(vault):
    root, items, db = vault
    _build_stalled_vault(items)
    _import(db)
    sent: list = []
    orch = _orch(root, sent)

    created = orch._project_stall_tick()
    assert created == ["worldgraph-project-next-direction"]
    assert (items / "worldgraph-project-next-direction.md").exists()
    assert len(sent) == 1 and "worldgraph-project" in sent[0]


def test_project_stall_tick_is_fail_open_without_db(vault):
    # no todo.db at all (the DB open raises) -> the tick swallows it, returns [].
    root, items, db = vault
    sent: list = []
    orch = _orch(root, sent)
    assert orch._project_stall_tick() == []
    assert sent == []
