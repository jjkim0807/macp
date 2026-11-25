"""Sweep + collect()-gating coverage (AC2/AC4/AC3).

`execute_rescope` is driven directly with an injected notifier (no real Kakao):
the happy path auto-splits a parked task into three chained sub-tasks, flips the
parent to an off bundle, tidies the parked assignment via `ledger.save`, and fires
exactly one 🔀 digest alert (zero ⛔). Idempotency, run-dir fallback, and the two
"hand to a human" paths (no plan, auto_merge:false) are pinned too. A separate
block drives `collect()` end to end to prove the ⛔ is suppressed when the park
carries a viable plan, and still fires when it does not.

Vault layout mirrors production so `Path(ledger.root).parent.parent` is the vault
root (same invariant test_resume.py asserts). Identifiers are neutral.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from todo_system import cli, paths
from todo_system.orchestrator import message as m
from todo_system.orchestrator.assignment import (
    AWAITING_HUMAN, DONE, IN_PROGRESS, Assignment, Ledger,
)
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import (
    ALERT_BLOCKED, ALERT_RESCOPE_PENDING, Orchestrator,
)
from todo_system.orchestrator.registry import Registry
from todo_system.orchestrator import rescope


def _plan(parent: str, n: int = 3) -> dict:
    return {
        "version": rescope.RESCOPE_SCHEMA_VERSION,
        "parent_slug": parent,
        "reason": "exceeds a single Implement stage — expand→migrate→contract",
        "design_notes": ["결정 사실 A (재도출 금지)", "결정 사실 B"],
        "subtasks": [
            {
                "slug": f"{parent}-part-{i}",
                "title": f"{parent} 하위 {i}",
                "outcome": f"하위 {i} 완료 조건",
                "acceptance_criteria": [f"하위 {i} AC1", f"하위 {i} AC2"],
                "files": [f"src/part_{i}.py"],
                "spec": {"goal": f"하위 {i} 목표"},
            }
            for i in range(n)
        ],
    }


_PARENT_MD = """---
tags:
  - todo
  - worker
status: open
worker_mode: auto
auto_merge: {auto_merge}
repo: "https://github.com/example/BigApp.git"
model_tier: heavy
allowed_tools: "Read,Edit,Write,Bash(git:*)"
if_then: "워커가 잡으면 분할안대로 집행"
outcome: "큰 리팩터"
---

# {title}

## Progress Log

- 2026-10-10: 생성
"""


def _write_parent(items: Path, slug: str, *, auto_merge: str = "true") -> Path:
    p = items / f"{slug}.md"
    p.write_text(_PARENT_MD.format(auto_merge=auto_merge, title=slug), encoding="utf-8")
    return p


def _write_plan(runs: Path, run_id: str, plan: dict) -> Path:
    d = runs / run_id
    d.mkdir(parents=True)
    (d / rescope.RESCOPE_FILENAME).write_text(json.dumps(plan), encoding="utf-8")
    return d


@pytest.fixture
def vault(tmp_path: Path):
    root = tmp_path / "vault"
    items = root / "Todo" / "items"
    templates = root / "Todo" / "templates"
    runs = root / "Worker" / "runs"
    for d in (items, templates, runs, root / "Worker" / "assignments", root / "Worker" / "_state"):
        d.mkdir(parents=True, exist_ok=True)
    shutil.copy(paths.vault_root() / "Todo" / "templates" / "auto-worker-task-template.md",
                templates / "auto-worker-task-template.md")
    ledger = Ledger(root / "Worker" / "assignments")
    assert Path(ledger.root).parent.parent == root     # the vault-root invariant
    return root, items, templates, runs, ledger


def _notifier():
    calls: list[tuple[str, str, str]] = []

    def fake(msg, *, event_key, level="digest"):
        calls.append((msg, event_key, level))

    return fake, calls


def _park(ledger: Ledger, slug: str, *, run_id: str | None,
          alert: str = ALERT_RESCOPE_PENDING) -> Assignment:
    payload = {"last_run_dir": run_id} if run_id else {}
    a = Assignment(task_id=slug, agent_id="worker-default", corr_id="c1",
                   state=AWAITING_HUMAN, blocked_on="needs re-scope",
                   last_alert_kind=alert, payload=payload)
    ledger.save(a)
    return a


# -- execute_rescope: happy path (AC2/AC4) ----------------------------------


def test_execute_auto_splits_and_fires_one_info_alert(vault):
    root, items, templates, runs, ledger = vault
    slug = "big-refactor"
    run_id = f"20261010T120000-{slug}"
    _write_parent(items, slug)
    _write_plan(runs, run_id, _plan(slug))
    _park(ledger, slug, run_id=run_id)
    notify, calls = _notifier()

    done = rescope.execute_rescope(ledger, root, notify, items_dir=items, templates_dir=templates)

    assert done == [slug]
    children = [items / f"{slug}-part-{i}.md" for i in range(3)]
    assert all(c.exists() for c in children)

    from todo_system import worker
    fm0 = worker.parse_frontmatter(children[0].read_text(encoding="utf-8"))[0]
    fm1 = worker.parse_frontmatter(children[1].read_text(encoding="utf-8"))[0]
    assert fm0["worker_mode"] == "auto"
    assert fm1["worker_mode"] == "blocked" and fm1["depends_on"] == [f"{slug}-part-0"]

    # parent flipped to an off bundle
    pfm = worker.parse_frontmatter((items / f"{slug}.md").read_text(encoding="utf-8"))[0]
    assert pfm["worker_mode"] == "off"
    assert str(pfm["auto_rescoped"]).lower() == "true"
    assert str(pfm["is_parent"]).lower() == "true"

    # parked assignment tidied the proper way (ledger.save, not hand-edit)
    a = ledger.load(slug)
    assert a.state == DONE and a.blocked_on == ""
    assert any(h.get("type") == "rescoped" for h in a.history)

    # exactly one info alert (🔀 digest), zero ⛔
    assert len(calls) == 1
    text, event_key, level = calls[0]
    assert text.splitlines()[0].startswith("🔀")
    assert event_key == f"rescope:{slug}" and level == "digest"


def test_execute_is_idempotent(vault):
    root, items, templates, runs, ledger = vault
    slug = "big-refactor"
    run_id = f"20261010T120000-{slug}"
    _write_parent(items, slug)
    _write_plan(runs, run_id, _plan(slug))
    _park(ledger, slug, run_id=run_id)
    notify, calls = _notifier()

    rescope.execute_rescope(ledger, root, notify, items_dir=items, templates_dir=templates)
    files_after_first = sorted(p.name for p in items.glob("*.md"))

    done2 = rescope.execute_rescope(ledger, root, notify, items_dir=items, templates_dir=templates)
    assert done2 == []                                   # nothing re-executed
    assert sorted(p.name for p in items.glob("*.md")) == files_after_first
    assert len(calls) == 1                               # no second alert


def test_execute_finds_run_dir_without_payload_hint(vault):
    root, items, templates, runs, ledger = vault
    slug = "big-refactor"
    _write_parent(items, slug)
    _write_plan(runs, f"20261010T120000-{slug}", _plan(slug))
    _park(ledger, slug, run_id=None)                     # no last_run_dir hint
    notify, calls = _notifier()

    done = rescope.execute_rescope(ledger, root, notify, items_dir=items, templates_dir=templates)
    assert done == [slug]
    assert (items / f"{slug}-part-0.md").exists()


# -- execute_rescope: hand-to-human fallbacks (AC3) -------------------------


def test_no_plan_falls_back_to_human(vault):
    root, items, templates, runs, ledger = vault
    slug = "big-refactor"
    _write_parent(items, slug)                           # no rescope.json anywhere
    _park(ledger, slug, run_id=None)
    notify, calls = _notifier()

    done = rescope.execute_rescope(ledger, root, notify, items_dir=items, templates_dir=templates)
    assert done == []
    assert not list(items.glob(f"{slug}-part-*.md"))     # nothing created
    assert len(calls) == 1
    text, event_key, level = calls[0]
    assert text.splitlines()[0].startswith("⛔") and level == "immediate"
    assert ledger.load(slug).last_alert_kind == ALERT_BLOCKED   # won't re-fire the sweep


def test_auto_merge_false_parent_falls_back_to_human(vault):
    root, items, templates, runs, ledger = vault
    slug = "big-refactor"
    run_id = f"20261010T120000-{slug}"
    _write_parent(items, slug, auto_merge="false")       # human merge gate
    _write_plan(runs, run_id, _plan(slug))
    _park(ledger, slug, run_id=run_id)
    notify, calls = _notifier()

    done = rescope.execute_rescope(ledger, root, notify, items_dir=items, templates_dir=templates)
    assert done == []
    assert not list(items.glob(f"{slug}-part-*.md"))
    assert len(calls) == 1 and calls[0][0].splitlines()[0].startswith("⛔")
    assert ledger.load(slug).last_alert_kind == ALERT_BLOCKED


# -- collect() gating: ⛔ suppression when a viable plan exists --------------


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
    orch._task_title = lambda slug: "big refactor"
    return orch, mb, sent, routed


def test_collect_suppresses_alert_when_plan_is_viable(vault, monkeypatch):
    root, items, templates, runs, ledger = vault
    monkeypatch.setattr(cli, "ITEMS_DIR", items)
    slug = "big-refactor"
    run_id = f"20261010T120000-{slug}"
    _write_parent(items, slug)
    _write_plan(runs, run_id, _plan(slug))
    ledger.save(Assignment(task_id=slug, agent_id="worker-default", corr_id="c1",
                           state=IN_PROGRESS, kind="ai-local"))
    orch, mb, sent, routed = _orch(root, ledger)

    mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="worker-default",
                       task_id=slug, corr_id="c1",
                       payload={"blocked_on": "needs re-scope", "run_dir": run_id}))
    orch.collect()

    a = ledger.load(slug)
    assert a.state == AWAITING_HUMAN
    assert a.last_alert_kind == ALERT_RESCOPE_PENDING    # stamped instead of ALERT_BLOCKED
    assert a.payload.get("last_run_dir") == run_id
    assert sent == [] and routed == []                   # ⛔ suppressed


def test_collect_still_alerts_when_not_auto_viable(vault, monkeypatch):
    root, items, templates, runs, ledger = vault
    monkeypatch.setattr(cli, "ITEMS_DIR", items)
    slug = "big-refactor"
    run_id = f"20261010T120000-{slug}"
    _write_parent(items, slug, auto_merge="false")       # human merge gate -> not viable
    _write_plan(runs, run_id, _plan(slug))
    ledger.save(Assignment(task_id=slug, agent_id="worker-default", corr_id="c1",
                           state=IN_PROGRESS, kind="ai-local"))
    orch, mb, sent, routed = _orch(root, ledger)

    mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="worker-default",
                       task_id=slug, corr_id="c1",
                       payload={"blocked_on": "needs re-scope", "run_dir": run_id}))
    orch.collect()

    a = ledger.load(slug)
    assert a.state == AWAITING_HUMAN
    assert a.last_alert_kind == ALERT_BLOCKED
    assert len(sent) == 1 and sent[0][0].splitlines()[0].startswith("⛔")


# -- remote-park bridge: read_rescope_for_payload / save_remote_rescope ------
# (the inline-plan helpers — a remote runner's run dir is on another host, so the
#  plan rides in the FAIL payload and HQ restores it to its own run dir)


def test_read_rescope_for_payload_reads_present_plan(tmp_path):
    rd = tmp_path / "20261121T111528-x"
    rd.mkdir()
    plan = _plan("x")
    (rd / rescope.RESCOPE_FILENAME).write_text(json.dumps(plan), encoding="utf-8")
    assert rescope.read_rescope_for_payload(rd) == plan


def test_read_rescope_for_payload_missing_is_none(tmp_path):
    assert rescope.read_rescope_for_payload(tmp_path / "nope") is None


def test_read_rescope_for_payload_over_cap_is_none(tmp_path):
    rd = tmp_path / "20261121T111528-x"
    rd.mkdir()
    plan = _plan("x")
    (rd / rescope.RESCOPE_FILENAME).write_text(json.dumps(plan), encoding="utf-8")
    assert rescope.read_rescope_for_payload(rd, max_bytes=50) is None       # over cap -> drop
    assert rescope.read_rescope_for_payload(rd, max_bytes=100_000) == plan  # under cap -> carry


def test_save_remote_rescope_writes_when_absent(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    plan = _plan("x")
    dest = rescope.save_remote_rescope(runs, "20261121T111528-x", plan)
    assert dest == runs / "20261121T111528-x" / rescope.RESCOPE_FILENAME
    assert dest.exists() and json.loads(dest.read_text(encoding="utf-8")) == plan


def test_save_remote_rescope_never_overwrites_hq_file(tmp_path):
    runs = tmp_path / "runs"
    rd = runs / "20261121T111528-x"
    rd.mkdir(parents=True)
    existing = _plan("hq-local")
    (rd / rescope.RESCOPE_FILENAME).write_text(json.dumps(existing), encoding="utf-8")
    assert rescope.save_remote_rescope(runs, "20261121T111528-x", _plan("remote")) is None
    assert json.loads((rd / rescope.RESCOPE_FILENAME).read_text(encoding="utf-8")) == existing


def test_save_remote_rescope_rejects_non_dict_empty_id_and_over_cap(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    assert rescope.save_remote_rescope(runs, "r", ["not", "a", "dict"]) is None
    assert rescope.save_remote_rescope(runs, "", _plan("x")) is None
    assert rescope.save_remote_rescope(runs, "r", _plan("x"), max_bytes=50) is None
    assert not list(runs.rglob(rescope.RESCOPE_FILENAME))     # nothing written


# -- remote park -> HQ restore -> auto-split, through collect() + sweep ------
# The 2026-11-21 incident: dripcoffee-dial-6 parked oversize on the macbook
# runner, whose run_dir was a Mac path HQ could not read, so reconcile could not
# find the plan and it reached a human. Here the FAIL carries the plan inline.

# A remote runner's absolute run-dir prefix: not this host's runs dir, and its
# basename is all HQ keeps (collect() stores Path(run_dir).name). Kept neutral —
# no personal path — so the public-identifier gate stays green.
_REMOTE_RUNS = "/remote-host/macp-worker/second-brain/Worker/runs"
_PLAN_PARK_BLOCKED = "task exceeds a single Implement stage — needs re-scoping (plan)"


def test_collect_restores_remote_inline_plan_and_auto_splits(vault, monkeypatch):
    """A plan-stage park on a REMOTE runner: its run dir lives on the Mac, so HQ
    has no rescope.json. The FAIL carries the split plan inline; collect() writes
    it into HQ's own run dir, suppresses the ⛔, and execute_rescope then splits the
    task into chained children exactly as for an HQ run — the whole 원격 파킹 →
    자동 분할 path end to end."""
    root, items, templates, runs, ledger = vault
    monkeypatch.setattr(cli, "ITEMS_DIR", items)
    slug = "big-refactor"
    run_id = f"20261121T111528-{slug}"
    remote_run_dir = f"{_REMOTE_RUNS}/{run_id}"              # a Mac path HQ cannot read
    _write_parent(items, slug)
    plan = _plan(slug)
    assert not (runs / run_id / rescope.RESCOPE_FILENAME).exists()   # nothing on HQ yet
    ledger.save(Assignment(task_id=slug, agent_id="worker-default", corr_id="c1",
                           state=IN_PROGRESS, kind="ai-local"))
    orch, mb, sent, routed = _orch(root, ledger)

    mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="worker-default",
                       task_id=slug, corr_id="c1",
                       payload={"blocked_on": _PLAN_PARK_BLOCKED,
                                "run_dir": remote_run_dir, "rescope": plan}))
    orch.collect()

    # collect(): restored to HQ's own run dir (basename only, never the Mac path),
    # stamped pending, zero ⛔
    a = ledger.load(slug)
    assert a.state == AWAITING_HUMAN
    assert a.last_alert_kind == ALERT_RESCOPE_PENDING
    assert a.payload.get("last_run_dir") == run_id
    restored = runs / run_id / rescope.RESCOPE_FILENAME
    assert restored.exists()
    assert json.loads(restored.read_text(encoding="utf-8")) == plan
    assert sent == [] and routed == []

    # the sweep splits it into three chained children, parent -> off bundle, one 🔀
    notify, calls = _notifier()
    done = rescope.execute_rescope(ledger, root, notify, items_dir=items, templates_dir=templates)
    assert done == [slug]
    assert all((items / f"{slug}-part-{i}.md").exists() for i in range(3))
    row = ledger.load(slug)
    assert row.state == DONE and row.blocked_on == ""
    assert len(calls) == 1 and calls[0][0].splitlines()[0].startswith("🔀")


def test_collect_remote_park_without_inline_plan_still_parks(vault, monkeypatch):
    """The control: the same remote park with NO `rescope` field (an old runner, or
    a park that wrote no viable plan). HQ has nothing to restore, so it reaches a
    human with one ⛔ — exactly as today (the #177 '범위 밖' behaviour)."""
    root, items, templates, runs, ledger = vault
    monkeypatch.setattr(cli, "ITEMS_DIR", items)
    slug = "big-refactor"
    run_id = f"20261121T111528-{slug}"
    remote_run_dir = f"{_REMOTE_RUNS}/{run_id}"
    _write_parent(items, slug)
    ledger.save(Assignment(task_id=slug, agent_id="worker-default", corr_id="c1",
                           state=IN_PROGRESS, kind="ai-local"))
    orch, mb, sent, routed = _orch(root, ledger)

    mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="worker-default",
                       task_id=slug, corr_id="c1",
                       payload={"blocked_on": _PLAN_PARK_BLOCKED, "run_dir": remote_run_dir}))
    orch.collect()

    a = ledger.load(slug)
    assert a.state == AWAITING_HUMAN
    assert a.last_alert_kind == ALERT_BLOCKED
    assert not (runs / run_id / rescope.RESCOPE_FILENAME).exists()   # nothing restored
    assert len(sent) == 1 and sent[0][0].splitlines()[0].startswith("⛔")
    assert not list(items.glob(f"{slug}-part-*.md"))
