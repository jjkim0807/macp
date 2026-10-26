"""keep_alive: the shared actionable-queue rule, the starved judgement, the CLI.

Neutral prefixes only (proj-a-, proj-b-): this file sits inside the tree the
public identifier gate scans.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from todo_system.orchestrator import keep_alive as ka


def _db(tmp_path: Path, rows) -> Path:
    db = tmp_path / "Todo" / "todo.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tasks (slug TEXT, status TEXT, is_parent INT, "
                "worker_mode TEXT, not_before TEXT)")
    con.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    return db


def _assign(tmp_path: Path, **rows: str) -> Path:
    d = tmp_path / "Worker" / "assignments"
    d.mkdir(parents=True, exist_ok=True)
    for task_id, state in rows.items():
        (d / f"{task_id}.json").write_text(json.dumps({"task_id": task_id, "state": state}))
    return d


def _policy(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "Worker" / "planner_policy.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# --- load_policy ------------------------------------------------------------------


def test_load_policy_missing_file_is_empty(tmp_path):
    assert ka.load_policy(tmp_path / "nope.yaml") == []


@pytest.mark.parametrize("text", [
    "limits:\n  auto_queue_depth_limit: 5\n",          # key absent
    "keep_alive_projects:\n",                           # empty (None)
    "keep_alive_projects: []\n",
    "keep_alive_projects: {prefix: proj-a-}\n",         # not a list
    "keep_alive_projects: proj-a-\n",
    "keep_alive_projects: [\n  - : :\n",                # broken YAML
    "- just\n- a list\n",                               # top level not a dict
    "",
])
def test_load_policy_absent_empty_or_broken_is_empty(tmp_path, text):
    assert ka.load_policy(_policy(tmp_path, text)) == []


def test_load_policy_normalises_and_drops_bad_entries(tmp_path):
    p = _policy(tmp_path, (
        "keep_alive_projects:\n"
        "  - prefix: proj-a-\n"
        "    decision: Decisions/a.md\n"
        "  - prefix: proj-b-\n"
        "  - decision: Decisions/orphan.md\n"
        "  - prefix: ''\n"
        "  - prefix: 7\n"
        "  - just-a-string\n"
        "  - prefix: proj-c-\n"
        "    decision: ''\n"
    ))
    assert ka.load_policy(p) == [
        {"prefix": "proj-a-", "decision": "Decisions/a.md"},
        {"prefix": "proj-b-", "decision": None},
        {"prefix": "proj-c-", "decision": None},
    ]


# --- actionable_slugs (the Gate 1 rule) -------------------------------------------


def test_actionable_slugs_applies_every_exclusion(tmp_path):
    db = _db(tmp_path, [
        ("ok-auto", "open", 0, "auto", None),
        ("ok-delegate", "open", 0, "delegate", ""),
        ("ok-discover", "open", 0, "discover", "2000-01-01"),
        ("ok-past-time", "open", 0, "auto", "2000-01-01T10:00"),
        ("no-done", "done", 0, "auto", None),
        ("no-parent", "open", 1, "auto", None),
        ("no-quick", "open", 0, "quick", None),
        ("no-blocked", "open", 0, "blocked", None),
        ("no-future-date", "open", 0, "auto", "2999-01-01"),
        ("no-future-T", "open", 0, "auto", "2999-01-01T10:00"),
        ("no-failed", "open", 0, "auto", None),
        ("ok-ledger-done", "open", 0, "auto", None),
    ])
    d = _assign(tmp_path, **{"no-failed": "failed", "ok-ledger-done": "in_progress"})
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    assert ka.actionable_slugs(db, d) == {
        "ok-auto", "ok-delegate", "ok-discover", "ok-past-time", "ok-ledger-done"}


def test_actionable_slugs_missing_db_raises_and_creates_nothing(tmp_path):
    db = tmp_path / "Todo" / "todo.db"
    with pytest.raises(sqlite3.Error):
        ka.actionable_slugs(db, tmp_path / "none")
    assert not db.exists()


def test_failed_task_ids_missing_dir_is_empty(tmp_path):
    assert ka.failed_task_ids(tmp_path / "none") == set()


# --- starved (pure) ---------------------------------------------------------------

_POL = [{"prefix": "proj-a-", "decision": "Decisions/a.md"},
        {"prefix": "proj-b-", "decision": None}]


def test_starved_match_is_not_starved():
    assert ka.starved(_POL, {"proj-a-step-1", "proj-b-x"}) == []


def test_starved_no_match_is_starved_in_policy_order():
    assert ka.starved(_POL, {"other-thing"}) == _POL
    assert ka.starved(_POL, {"proj-b-x"}) == [_POL[0]]


def test_starved_prefix_must_lead_the_slug():
    assert ka.starved(_POL[:1], {"x-proj-a-step"}) == _POL[:1]


def test_starved_empty_policy_is_empty():
    assert ka.starved([], set()) == []


def test_starved_counts_deferred_and_failed_as_absent(tmp_path):
    # Only a future-not_before task, or only a failed one: still starved.
    db = _db(tmp_path, [("proj-a-later", "open", 0, "auto", "2999-01-01"),
                        ("proj-b-broken", "open", 0, "auto", None)])
    d = _assign(tmp_path, **{"proj-b-broken": "failed"})
    assert ka.starved(_POL, ka.actionable_slugs(db, d)) == _POL


# --- render_context ---------------------------------------------------------------


def test_render_context_empty_is_empty_string():
    assert ka.render_context([]) == ""


def test_render_context_names_prefixes_decisions_and_rules():
    out = ka.render_context(_POL)
    assert out.startswith(ka.HEADING + "\n")
    assert "- [proj-a-] decision: Decisions/a.md" in out
    assert "- [proj-b-] decision: (없음)" in out
    assert sum(1 for line in out.splitlines() if line.startswith("- [")) == 2
    for needle in ("최소 1건", "exhausted", "verdict: error", "kakao_on_error",
                   "max_new_tasks_per_invocation", "blocked_patterns", "required_fields",
                   "auto queue depth limit reached",
                   "no trigger events since last exhausted plan", "not_before"):
        assert needle in out, needle


# --- CLI --------------------------------------------------------------------------


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(ka, "POLICY_PATH", tmp_path / "Worker" / "planner_policy.yaml")
    monkeypatch.setattr(ka, "DB_PATH", tmp_path / "Todo" / "todo.db")
    monkeypatch.setattr(ka, "ASSIGNMENTS_DIR", tmp_path / "Worker" / "assignments")
    return tmp_path


_POLICY_A = "keep_alive_projects:\n  - prefix: proj-a-\n    decision: Decisions/a.md\n"


def test_cli_emit_context_prints_block_when_starved(vault, capsys):
    _policy(vault, _POLICY_A)
    _db(vault, [("other-1", "open", 0, "auto", None)])
    assert ka.main(["emit-context"]) == 0
    out = capsys.readouterr().out
    assert out == ka.render_context([{"prefix": "proj-a-", "decision": "Decisions/a.md"}])
    assert "- [proj-a-]" in out


def test_cli_emit_context_silent_when_fed(vault, capsys):
    _policy(vault, _POLICY_A)
    _db(vault, [("proj-a-next", "open", 0, "delegate", None)])
    assert ka.main(["emit-context"]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("policy", [None, "limits: {}\n", "keep_alive_projects: []\n"])
def test_cli_emit_context_silent_without_the_key(vault, capsys, policy):
    if policy is not None:
        _policy(vault, policy)
    _db(vault, [])
    assert ka.main(["emit-context"]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("corrupt", [False, True])
def test_cli_emit_context_fails_safe_on_db_trouble(vault, capsys, corrupt):
    _policy(vault, _POLICY_A)
    if corrupt:
        (vault / "Todo").mkdir()
        (vault / "Todo" / "todo.db").write_text("not a database", encoding="utf-8")
    assert ka.main(["emit-context"]) == 0
    assert capsys.readouterr().out == ""


def test_cli_queue_depth_prints_count(vault, capsys):
    _db(vault, [("a", "open", 0, "auto", None), ("b", "open", 0, "delegate", None),
                ("c", "open", 0, "auto", None)])
    _assign(vault, c="failed")
    assert ka.main(["queue-depth"]) == 0
    assert capsys.readouterr().out.strip() == "2"


def test_cli_queue_depth_missing_db_is_zero_and_not_created(vault, capsys):
    assert ka.main(["queue-depth"]) == 0
    assert capsys.readouterr().out.strip() == "0"
    assert not (vault / "Todo" / "todo.db").exists()
