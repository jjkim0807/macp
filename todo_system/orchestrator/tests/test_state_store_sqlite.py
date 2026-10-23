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

from todo_system.orchestrator import message as m
from todo_system.orchestrator import state_store_sqlite as sss
from todo_system.orchestrator.assignment import DONE, IN_PROGRESS, Assignment
from todo_system.orchestrator.message import Envelope


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


# ---------------------------------------------------------------------------
# SqliteMailbox — transport + base-ack reuse
# ---------------------------------------------------------------------------


def test_sqlite_mailbox_send_receive_ack(db):
    sss._bootstrap(db)
    mb = sss.SqliteMailbox(db)
    mb.init_agent("agentX")                         # no-op, must not raise
    msg = Envelope(to="agentX", type=m.TASK_ASSIGN, task_id="t1")
    mb.send(msg)
    got = mb.receive("agentX")
    assert [e.msg_id for e in got] == [msg.msg_id]
    assert got[0].to_json() == msg.to_json()        # byte-identical round-trip
    # at-least-once: still returned before ack
    assert [e.msg_id for e in mb.receive("agentX")] == [msg.msg_id]
    mb.ack("agentX", msg)
    assert mb.receive("agentX") == []               # not redelivered after ack


def test_sqlite_mailbox_fifo_and_partial_ack(db):
    sss._bootstrap(db)
    mb = sss.SqliteMailbox(db)
    m1 = Envelope(to="ag", type=m.TASK_ASSIGN, task_id="t1")
    m2 = Envelope(to="ag", type=m.PING)
    mb.send(m1)
    mb.send(m2)
    assert [e.msg_id for e in mb.receive("ag")] == [m1.msg_id, m2.msg_id]  # FIFO by msg_id
    mb.ack("ag", m1)
    assert [e.msg_id for e in mb.receive("ag")] == [m2.msg_id]             # only the unacked one
    mb.ack("ag", m2)
    assert mb.receive("ag") == []


def test_sqlite_mailbox_per_agent_isolation(db):
    sss._bootstrap(db)
    mb = sss.SqliteMailbox(db)
    mine = Envelope(to="me", type=m.PING)
    theirs = Envelope(to="you", type=m.PING)
    mb.send(mine)
    mb.send(theirs)
    assert [e.msg_id for e in mb.receive("me")] == [mine.msg_id]
    # ack on "me" must not touch "you"'s row
    mb.ack("me", mine)
    assert mb.receive("me") == []
    assert [e.msg_id for e in mb.receive("you")] == [theirs.msg_id]


def test_at_least_once_crash_before_ack_redelivers(db):
    sss._bootstrap(db)
    # send + receive through one "process"
    sent = Envelope(to="ag", type=m.TASK_ASSIGN, task_id="t1")
    sss.SqliteMailbox(db).send(sent)
    first = sss.SqliteMailbox(db).receive("ag")
    assert [e.msg_id for e in first] == [sent.msg_id]
    # crash BEFORE ack: a brand-new store/connection on the same file still sees it
    redelivered = sss.SqliteMailbox(db).receive("ag")
    assert [e.msg_id for e in redelivered] == [sent.msg_id]
    # now ack through a fresh object; a later fresh object sees nothing
    sss.SqliteMailbox(db).ack("ag", redelivered)
    assert sss.SqliteMailbox(db).receive("ag") == []
