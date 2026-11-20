"""reconcile_split_parents: a parent a human split BY HAND into children (no
`auto_rescoped`, no `is_parent`) is auto-closed once every child is done, and its
parked ledger row is tidied from AWAITING_HUMAN/FAILED to DONE. An ordinary
project umbrella (no oversize-rescope park) is still only surfaced, never
auto-closed.

Reproduces the `dancemirror-unified-projects` situation (2026-10-10~11) but with
NEUTRAL identifiers only: the parent md omits `repo` / uses a placeholder, because
the real DanceMirror URL contains a personal token the public-identifier gate
forbids. The auto-close signal is the ledger `blocked_on` text + child status, not
the repo URL, so neutral fixtures lose nothing.

Uses a real tmp vault + todo.db (import-md) + Ledger because the rollup reads the
DB (parent_slug / child status) and the close touches the markdown. The `cli`
globals are rebound to the tmp vault on top of the package conftest's runtime-path
isolation so nothing touches the live vault.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from todo_system import cli
from todo_system import worker
from todo_system.orchestrator import rescope, tasks
from todo_system.orchestrator.assignment import (
    AWAITING_HUMAN, DONE, FAILED, Assignment, Ledger,
)
from todo_system.orchestrator.orchestrator import (
    ALERT_BLOCKED, ALERT_RESULT_NO_PR, Orchestrator,
)
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.registry import Agent, Registry

# The real parked-row wording collect() left on dancemirror-unified-projects —
# copied verbatim so the predicate is pinned against production shape, not a
# paraphrase. Carries no personal identifier.
REAL_BLOCKED_ON = (
    "task exceeds a single Implement stage — needs re-scoping into the 3 sub-tasks "
    "Implement recommended (unified-model-foundation → …-switchover-logic → "
    "…-ui-and-export). Not a code/env fault; a straight retry re-runs "
    "research→plan→implement and hits the same no-green-intermediate wall."
)


def _parent_md(items: Path, slug: str, *, status: str = "open",
               is_parent: bool = False, auto_rescoped: bool = False) -> Path:
    """A hand-split parent: worker_mode off, NO is_parent / auto_rescoped by default
    (that is the whole point — a human split it without the auto-rescope markers).
    No `repo` field: the signal is the ledger, never a URL."""
    ip = "is_parent: true\n" if is_parent else ""
    ar = "auto_rescoped: true\n" if auto_rescoped else ""
    (items / f"{slug}.md").write_text(
        f"---\ntags:\n  - todo\n  - worker\nstatus: {status}\nworker_mode: off\n"
        f"{ip}{ar}outcome: 묶음\n---\n\n# {slug}\n\n"
        f"## Progress Log\n\n- 2026-10-10: 생성\n",
        encoding="utf-8")
    return items / f"{slug}.md"


def _child_md(items: Path, slug: str, parent: str, *, status: str = "done") -> None:
    (items / f"{slug}.md").write_text(
        f"---\ntags:\n  - todo\nstatus: {status}\nparent: \"[[{parent}]]\"\n"
        f"worker_mode: auto\noutcome: 하위\n---\n\n# {slug}\n\n- [ ] step\n",
        encoding="utf-8")


def _parked(ledger: Ledger, slug: str, *, state: str = AWAITING_HUMAN,
            blocked_on: str = REAL_BLOCKED_ON, last_alert_kind: str = ALERT_BLOCKED,
            last_note: str = "") -> Assignment:
    """Save a parked ledger row for `slug`, shaped like the real oversize-rescope
    park (kind=ai-local, last_alert_kind=blocked, re-scope wording in blocked_on)."""
    a = Assignment(task_id=slug, agent_id="worker-default", corr_id=f"corr-{slug}",
                   state=state, kind="ai-local", last_alert_kind=last_alert_kind,
                   blocked_on=blocked_on, last_note=last_note)
    ledger.save(a)
    return a


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "vault"
    items = root / "Todo" / "items"
    items.mkdir(parents=True)
    (root / "Worker" / "assignments").mkdir(parents=True)
    (root / "Worker" / "_state").mkdir(parents=True)
    (root / "Memory" / "episodes").mkdir(parents=True)
    db = root / "Todo" / "todo.db"
    monkeypatch.setattr(cli, "ROOT", root)
    monkeypatch.setattr(cli, "TODO_DIR", root / "Todo")
    monkeypatch.setattr(cli, "ITEMS_DIR", items)
    monkeypatch.setattr(cli, "DB_PATH", db)
    return root, items, db


def _import(db: Path):
    con = cli.db_connect(db)
    cli.init_db(con)
    cli.import_markdown_to_db(con)
    con.close()


def _status(path: Path) -> str:
    return worker.parse_frontmatter(path.read_text(encoding="utf-8"))[0]["status"]


def _orch(root: Path, *, sent: list | None = None, agents=None) -> Orchestrator:
    return Orchestrator(FileMailbox(root / "Worker" / "mb"), Registry(agents or []),
                        Ledger(root / "Worker" / "assignments"),
                        notify=(lambda t, link=None: sent.append(t)) if sent is not None
                        else (lambda t, link=None: None),
                        alert_route=lambda text, **kw: ("send", text))


def _human_agent() -> Agent:
    """A registered human, so the sweeps see a realistic registry."""
    return Agent(id="me", kind="human", projects=["*"])


# --------------------------------------------------------------------------- #
# Phase 1 unit tests — pure predicate + DB rollup, each independently.
# --------------------------------------------------------------------------- #

def test_predicate_matches_real_dancemirror_blocked_on():
    # The real parked-row text must read as an oversize re-scope park.
    assert rescope.is_oversize_rescope_park(REAL_BLOCKED_ON) is True


@pytest.mark.parametrize("blocked_on", [
    "dripcoffee-bean-cards-1-token-refresh (PR #22, OPEN)",   # external blocker (another slug)
    "ANTHROPIC_API_KEY missing in the runner env",            # tooling blocker
    "waiting on a console setting nobody made",               # opaque human blocker
    "",                                                        # no blocker at all
])
def test_predicate_rejects_non_rescope_blockers(blocked_on):
    assert rescope.is_oversize_rescope_park(blocked_on) is False


def test_predicate_rejects_timeout_park():
    # ALERT_TIMEOUT_PARK: empty blocked_on, last_note is the timeout streak note —
    # carries no re-scope keyword, so it stays surfaced, never auto-closed.
    assert rescope.is_oversize_rescope_park("", "2회 연속 stage timeout — 이어가기 불가") is False


def test_predicate_reads_last_note_too():
    # The keyword can live in last_note rather than blocked_on.
    assert rescope.is_oversize_rescope_park("", "needs re-scoping into sub-tasks") is True


def test_child_rollup_all_done(vault):
    root, items, db = vault
    _parent_md(items, "p")
    for i in range(3):
        _child_md(items, f"p-child-{i}", "p", status="done")
    _import(db)
    roll = tasks.child_status_rollup("p", db)
    assert roll == {"n": 3, "closed": 3, "all_closed": True}


def test_child_rollup_partial_is_not_all_closed(vault):
    root, items, db = vault
    _parent_md(items, "p")
    _child_md(items, "p-child-0", "p", status="done")
    _child_md(items, "p-child-1", "p", status="open")
    _import(db)
    roll = tasks.child_status_rollup("p", db)
    assert roll["n"] == 2 and roll["closed"] == 1 and roll["all_closed"] is False


def test_child_rollup_cancelled_counts_as_closed(vault):
    root, items, db = vault
    _parent_md(items, "p")
    _child_md(items, "p-child-0", "p", status="done")
    _child_md(items, "p-child-1", "p", status="cancelled")
    _import(db)
    roll = tasks.child_status_rollup("p", db)
    assert roll["all_closed"] is True and roll["closed"] == 2


def test_child_rollup_no_children_is_not_vacuously_closed(vault):
    root, items, db = vault
    _parent_md(items, "lonely")
    _import(db)
    roll = tasks.child_status_rollup("lonely", db)
    assert roll == {"n": 0, "closed": 0, "all_closed": False}


# --------------------------------------------------------------------------- #
# Phase 3 integration — reconcile_split_parents against a real tmp vault + ledger,
# reproducing the dancemirror-unified-projects (2026-10-10~11) situation.
# --------------------------------------------------------------------------- #

def test_manual_split_parent_auto_closed(vault):
    # AC1 + AC2: a hand-split parent (open, worker_mode off, NO is_parent /
    # auto_rescoped) whose children are all done, parked as an oversize re-scope
    # park, is closed and its ledger row tidied.
    root, items, db = vault
    parent = _parent_md(items, "split-parent")
    for i in range(3):
        _child_md(items, f"split-parent-part-{i}", "split-parent", status="done")
    _import(db)

    # cause ①: a hand-split parent carries no is_parent, so parents_needing_review
    # (the only other all-children-done watcher) never sees it — hence this sweep.
    assert tasks.parents_needing_review(db) == []

    ledger = Ledger(root / "Worker" / "assignments")
    _parked(ledger, "split-parent")
    assert ledger.load("split-parent").state == AWAITING_HUMAN

    orch = _orch(root, agents=[_human_agent()])
    closed = orch.reconcile_split_parents()

    assert "split-parent" in closed
    assert _status(parent) == "done"                             # AC1: no auto_rescoped stamp needed
    row = ledger.load("split-parent")
    assert row.state == DONE                                     # AC2: ledger tidied
    assert any(h.get("type") == "reconciled-manual-split" for h in row.history)
    assert row.last_note == "manual-split parent auto-closed: 3 children done"


def test_md_done_ledger_parked_is_tidied_without_fighting_reconcile_closed(vault):
    # AC3: a human closed the md directly; the ledger still lags at AWAITING_HUMAN.
    # The sweep tidies the ledger to DONE and does not fight reconcile_closed.
    root, items, db = vault
    parent = _parent_md(items, "already-closed", status="done")
    _import(db)
    ledger = Ledger(root / "Worker" / "assignments")
    _parked(ledger, "already-closed")

    orch = _orch(root)
    closed = orch.reconcile_split_parents()

    assert "already-closed" in closed
    row = ledger.load("already-closed")
    assert row.state == DONE
    assert any(h.get("type") == "reconciled-ledger-tidy" for h in row.history)
    assert _status(parent) == "done"

    # reconcile_closed only acts on ledger=DONE + md=OPEN; the md is done here, so it
    # is a no-op and never re-opens or churns the md (close_fingerprint is honoured).
    assert orch.reconcile_closed() == []
    assert _status(parent) == "done"
    assert not any(h.get("type") == "reopen-respected"
                   for h in ledger.load("already-closed").history)


def test_failed_ledger_with_closed_md_is_tidied(vault):
    # AC3 also covers a genuinely FAILED row whose md a human closed/withdrew — the
    # same reason reconcile_merged sweeps FAILED (a late retry can land the terminal
    # FAILED after the close). The FAILURE framing also records a false alarm.
    root, items, db = vault
    _parent_md(items, "failed-but-closed", status="done")
    _import(db)
    ledger = Ledger(root / "Worker" / "assignments")
    _parked(ledger, "failed-but-closed", state=FAILED,
            last_alert_kind="failure-suspected", blocked_on="")

    orch = _orch(root)
    assert "failed-but-closed" in orch.reconcile_split_parents()
    assert ledger.load("failed-but-closed").state == DONE
    # the false alarm (failure framing -> benign close) was recorded for the planner
    episodes = list((root / "Memory" / "episodes").glob("*-false-alarm-failed-but-closed.md"))
    assert len(episodes) == 1


def test_ordinary_umbrella_not_auto_closed_only_surfaced(vault):
    # AC4: an ordinary project umbrella (is_parent, children all done) with NO parked
    # re-scope ledger row is never auto-closed here — reconcile_parents still only
    # SURFACES it (existing behavior preserved).
    root, items, db = vault
    umbrella = _parent_md(items, "umbrella", is_parent=True)
    for i in range(2):
        _child_md(items, f"umbrella-child-{i}", "umbrella", status="done")
    _import(db)

    sent: list = []
    orch = _orch(root, sent=sent)

    assert orch.reconcile_split_parents() == []     # the new sweep leaves it alone
    assert _status(umbrella) == "open"              # NOT auto-closed

    processed = orch.reconcile_parents()
    assert "umbrella" in processed
    assert _status(umbrella) == "open"              # surfaced, still a human's call to close
    assert len(sent) == 1 and "umbrella" in sent[0]


def test_rescope_park_with_an_open_child_stays_parked(vault):
    # False-positive guard (a): children not all done -> stay parked (미탐 over 오탐).
    root, items, db = vault
    parent = _parent_md(items, "half-done")
    _child_md(items, "half-done-0", "half-done", status="done")
    _child_md(items, "half-done-1", "half-done", status="open")
    _import(db)
    ledger = Ledger(root / "Worker" / "assignments")
    _parked(ledger, "half-done")

    orch = _orch(root)
    assert orch.reconcile_split_parents() == []
    assert _status(parent) == "open"
    assert ledger.load("half-done").state == AWAITING_HUMAN


def test_rescope_park_with_no_children_stays_parked(vault):
    # False-positive guard (b): zero children -> not vacuously complete -> parked.
    root, items, db = vault
    parent = _parent_md(items, "childless")
    _import(db)
    ledger = Ledger(root / "Worker" / "assignments")
    _parked(ledger, "childless")

    orch = _orch(root)
    assert orch.reconcile_split_parents() == []
    assert _status(parent) == "open"
    assert ledger.load("childless").state == AWAITING_HUMAN


def test_non_rescope_park_with_all_children_done_stays_parked(vault):
    # False-positive guard (c): all children done but the park is an EXTERNAL blocker
    # (no re-scope wording) -> the keyword gate keeps it parked, so child status alone
    # never auto-closes a parent.
    root, items, db = vault
    parent = _parent_md(items, "ext-blocked")
    _child_md(items, "ext-blocked-0", "ext-blocked", status="done")
    _import(db)
    ledger = Ledger(root / "Worker" / "assignments")
    _parked(ledger, "ext-blocked", blocked_on="upstream-thing (PR #9, OPEN)")

    orch = _orch(root)
    assert orch.reconcile_split_parents() == []
    assert _status(parent) == "open"
    assert ledger.load("ext-blocked").state == AWAITING_HUMAN


# --------------------------------------------------------------------------- #
# A parent the WORKER split during its own run (mulkko-95-focus-block-implementation,
# 2026-10-13): the 0-commit run created the children and ended `result-no-pr`
# ("merge withheld for human review"), so its ledger row reads AWAITING_HUMAN with no
# re-scope wording at all. While children are open it is an umbrella in progress —
# not nudged; once they are all done it is closed like a hand-split parent.
# --------------------------------------------------------------------------- #

_NO_PR_NOTE = ("staged pipeline complete (5 stages) — merge withheld for human review "
               "(agent's call, not a failure)")


def _worker_split(ledger: Ledger, slug: str) -> Assignment:
    return _parked(ledger, slug, blocked_on="", last_alert_kind=ALERT_RESULT_NO_PR,
                   last_note=_NO_PR_NOTE)


def test_parents_with_open_children(vault):
    root, items, db = vault
    _parent_md(items, "busy")
    _child_md(items, "busy-0", "busy", status="done")
    _child_md(items, "busy-1", "busy", status="open")
    _parent_md(items, "finished")
    _child_md(items, "finished-0", "finished", status="done")
    _child_md(items, "finished-1", "finished", status="cancelled")
    _parent_md(items, "leaf")
    _import(db)
    assert tasks.parents_with_open_children(db) == {"busy"}


def test_parents_with_open_children_missing_db_is_empty(tmp_path):
    assert tasks.parents_with_open_children(tmp_path / "nope.db") == set()


def test_worker_split_parent_in_progress_is_left_open(vault):
    root, items, db = vault
    parent = _parent_md(items, "self-split")
    _child_md(items, "self-split-0", "self-split", status="done")
    _child_md(items, "self-split-1", "self-split", status="open")
    _import(db)
    ledger = Ledger(root / "Worker" / "assignments")
    _worker_split(ledger, "self-split")

    orch = _orch(root, agents=[_human_agent()])
    assert orch.reconcile_split_parents() == []          # children still open
    assert _status(parent) == "open"


def test_worker_split_parent_closed_when_children_done(vault):
    root, items, db = vault
    parent = _parent_md(items, "self-split")
    for i in range(3):
        _child_md(items, f"self-split-{i}", "self-split", status="done")
    _import(db)
    ledger = Ledger(root / "Worker" / "assignments")
    _worker_split(ledger, "self-split")

    orch = _orch(root, agents=[_human_agent()])
    assert orch.reconcile_split_parents() == ["self-split"]
    assert _status(parent) == "done"
    row = ledger.load("self-split")
    assert row.state == DONE
    assert any(h.get("type") == "reconciled-manual-split" for h in row.history)


def test_no_pr_park_without_children_stays_parked(vault):
    # result-no-pr alone is not a split: no children -> nothing to close.
    root, items, db = vault
    parent = _parent_md(items, "no-kids")
    _import(db)
    ledger = Ledger(root / "Worker" / "assignments")
    _worker_split(ledger, "no-kids")
    orch = _orch(root)
    assert orch.reconcile_split_parents() == []
    assert _status(parent) == "open"
    assert ledger.load("no-kids").state == AWAITING_HUMAN


def test_tick_runs_the_sweep_before_reconcile_merged(vault, monkeypatch):
    # Wiring (decisions 2 & 3): tick() calls reconcile_split_parents, threads its
    # result into the dict, and runs it BEFORE reconcile_merged (so the now-DONE row
    # is skipped by that gh-gated sweep). Other machine-state reads are stubbed so
    # this is a statement about wiring only.
    root, items, db = vault
    orch = _orch(root)
    order: list[str] = []
    monkeypatch.setattr(orch, "usage_hold", lambda: None)
    for name in ("collect", "reconcile_closed", "reconcile_queue", "reconcile_parents",
                 "reap_stale", "reconcile_resumes", "reconcile_rescope",
                 "_advote_bridge_tick", "_beta_sweep_tick"):
        monkeypatch.setattr(orch, name, lambda *a, **k: [])
    monkeypatch.setattr(orch, "reconcile_split_parents",
                        lambda: (order.append("split"), ["x"])[1])
    monkeypatch.setattr(orch, "reconcile_merged",
                        lambda: (order.append("merged"), [])[1])

    result = orch.tick([])

    assert result["split_parents"] == ["x"]          # threaded into the result dict
    assert order == ["split", "merged"]              # runs BEFORE reconcile_merged
