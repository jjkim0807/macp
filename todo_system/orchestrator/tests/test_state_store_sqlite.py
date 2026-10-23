"""ExternalStateStore (SQLite) — external-only durability + sub-surface units.

Leaf ④ of docs/design/state-store.md. The git+external *parity* of the
ledger / mailbox / planner contracts lives in ``test_state_store.py``
(parametrized over backends); this file holds what only the external backend
has: schema bootstrap, at-least-once crash re-receive, and WAL two-process
concurrency.

Every store here is built on an explicit tmp ``sqlite_path`` — never the default
``paths.state_sqlite_path()``, which would write a real host file outside the
vault.
"""

from __future__ import annotations

import sqlite3

import pytest

from todo_system.orchestrator import state_store_sqlite as sss
from todo_system.orchestrator.assignment import DONE, IN_PROGRESS, Assignment


@pytest.fixture
def db(tmp_path):
    return tmp_path / "state.sqlite"


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


def test_schema_bootstrap_creates_tables_and_version(db):
    sss._bootstrap(db)
    assert db.exists()
    con = sqlite3.connect(str(db))
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"meta", "ledger", "mailbox", "mailbox_cursor", "planner_kv"} <= tables
        version = con.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        assert version == str(sss.SCHEMA_VERSION) == "1"
        # WAL is persisted in the DB header by bootstrap.
        mode = con.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        con.close()


def test_bootstrap_is_idempotent(db):
    sss._bootstrap(db)
    sss._bootstrap(db)  # a second call must not raise or duplicate the version row
    con = sqlite3.connect(str(db))
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM meta WHERE key='schema_version'").fetchone()[0]
        assert n == 1
    finally:
        con.close()


# ---------------------------------------------------------------------------
# SqliteLedger
# ---------------------------------------------------------------------------


def test_sqlite_ledger_roundtrip(db):
    sss._bootstrap(db)
    ledger = sss.SqliteLedger(db)
    a = Assignment(
        task_id="t-1", agent_id="ai-coder", corr_id="c-1", state=IN_PROGRESS,
        attempts=3, refail_count=1, job_running=True,
        history=[{"ts": "2026-10-23T00:00:00+09:00", "type": "PROGRESS",
                  "from": "ai-coder"}],
        payload={"note": "작업 중", "n": 7},
    )
    ledger.save(a)
    loaded = ledger.load("t-1")
    assert loaded is not None
    assert loaded.to_json() == a.to_json()          # byte-identical round-trip
    assert ledger.load("missing") is None

    # all() ordered by task_id; active() filters TERMINAL
    b = Assignment(task_id="t-0", agent_id="x", corr_id="c-0", state=DONE)
    ledger.save(b)
    assert [x.task_id for x in ledger.all()] == ["t-0", "t-1"]
    assert ledger.active("t-0") is None             # DONE is terminal
    assert ledger.active("t-1").to_json() == a.to_json()


def test_sqlite_ledger_save_upserts(db):
    sss._bootstrap(db)
    ledger = sss.SqliteLedger(db)
    a = Assignment(task_id="t-1", agent_id="a", corr_id="c", state=IN_PROGRESS)
    ledger.save(a)
    a.state = DONE
    ledger.save(a)                                  # same task_id -> UPDATE, not a 2nd row
    assert [x.task_id for x in ledger.all()] == ["t-1"]
    assert ledger.load("t-1").state == DONE


# ---------------------------------------------------------------------------
# SqlitePlannerKV
# ---------------------------------------------------------------------------


def test_sqlite_planner_kv_roundtrip(db):
    sss._bootstrap(db)
    kv = sss.SqlitePlannerKV(db)
    assert kv.get("missing") is None
    kv.put("verdict", "continue")
    kv.put("todo_state_hash", "deadbeef")
    kv.put("unicode", "한글 값")
    kv.put("count", 3)
    assert kv.get("verdict") == "continue"
    assert kv.get("unicode") == "한글 값"
    assert kv.get("count") == 3
    kv.put("verdict", "exhausted")                  # overwrite one key, keep the rest
    assert kv.get("verdict") == "exhausted"
    assert kv.all() == {
        "verdict": "exhausted", "todo_state_hash": "deadbeef",
        "unicode": "한글 값", "count": 3}
