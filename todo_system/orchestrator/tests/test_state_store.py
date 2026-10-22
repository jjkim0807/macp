"""GitStateStore is a pass-through: each sub-surface behaves like the object it wraps.

Leaf ② of docs/design/state-store.md. Every store here is built on a tmp vault;
nothing uses the default vault_root.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess

import pytest

from todo_system import worker
from todo_system.orchestrator import message as m
from todo_system.orchestrator import metrics
from todo_system.orchestrator.assignment import DONE, Assignment, Ledger
from todo_system.orchestrator.mailbox import (
    DEFAULT_ROOT,
    FileMailbox,
    GitMailbox,
    Mailbox,
    open_mailbox,
)
from todo_system.orchestrator.message import Envelope
from todo_system.orchestrator.state_store import (
    GitStateStore,
    PlannerKV,
    StateStore,
    TodoCacheReader,
)


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    return v


# ---------------------------------------------------------------------------
# ABC shape (design §②)
# ---------------------------------------------------------------------------


def test_abc_abstract_methods_match_design_signatures():
    assert StateStore.__abstractmethods__ == {"ledger", "mailbox", "planner_kv", "todo_cache"}
    assert PlannerKV.__abstractmethods__ == {"get", "put", "all"}
    assert TodoCacheReader.__abstractmethods__ == {"counts"}


@pytest.mark.parametrize("abc", [StateStore, PlannerKV, TodoCacheReader])
def test_abc_cannot_be_instantiated(abc):
    with pytest.raises(TypeError):
        abc()


def test_git_store_sub_surfaces_are_the_abc_types(vault):
    store = GitStateStore(vault, mailbox_uri=f"file://{DEFAULT_ROOT}")
    assert isinstance(store, StateStore)
    assert isinstance(store.ledger(), Ledger)
    assert isinstance(store.mailbox(), Mailbox)
    assert isinstance(store.planner_kv(), PlannerKV)
    assert isinstance(store.todo_cache(), TodoCacheReader)


# ---------------------------------------------------------------------------
# ledger ↔ Ledger(<vault>/Worker/assignments)
# ---------------------------------------------------------------------------


def test_ledger_same_root_same_files_same_results(vault):
    store = GitStateStore(vault)
    direct = Ledger(vault / "Worker" / "assignments")
    assert store.ledger().root == direct.root

    a1 = Assignment(task_id="t1", agent_id="a", corr_id="c")
    store.ledger().save(a1)
    assert (vault / "Worker" / "assignments" / "t1.json").is_file()
    assert direct.load("t1").to_json() == a1.to_json()

    direct.save(Assignment(task_id="t2", agent_id="a", corr_id="c2", state=DONE))
    assert [a.to_json() for a in store.ledger().all()] == [a.to_json() for a in direct.all()]
    assert [a.task_id for a in direct.all()] == ["t1", "t2"]
    assert store.ledger().active("t2") is None and direct.active("t2") is None
    assert store.ledger().active("t1").to_json() == direct.active("t1").to_json()
    assert store.ledger().load("missing") is None


def test_ledger_is_cached(vault):
    store = GitStateStore(vault)
    assert store.ledger() is store.ledger()


# ---------------------------------------------------------------------------
# mailbox ↔ open_mailbox(...)
# ---------------------------------------------------------------------------


def _git_vault(vault):
    for args in (["init", "-q"],
                 ["config", "user.email", "test@example.invalid"],
                 ["config", "user.name", "state-store-test"]):
        subprocess.run(["git", *args], cwd=vault, check=True, capture_output=True)
    (vault / DEFAULT_ROOT).mkdir(parents=True)


def test_git_mailbox_same_transport_root_and_messages(vault):
    _git_vault(vault)
    store = GitStateStore(vault)
    s = store.mailbox()
    d = open_mailbox(f"git://{DEFAULT_ROOT}", base=vault)

    assert type(s) is type(d) is GitMailbox
    assert s.root == d.root == vault / "Worker" / "agents"
    assert s.repo_dir == d.repo_dir
    assert s.branch == d.branch

    msg = Envelope(to="agentX", type=m.TASK_ASSIGN, task_id="t1")
    path = s.send(msg)
    assert path == d._inbox("agentX") / f"{msg.msg_id}.json"
    assert [e.msg_id for e in d.receive("agentX")] == [e.msg_id for e in s.receive("agentX")]
    assert [e.msg_id for e in d.receive("agentX")] == [msg.msg_id]

    # The cursor is on disk, so an ack through the store is seen by the direct object.
    s.ack("agentX", msg)
    assert d.receive("agentX") == []
    assert s.receive("agentX") == []


def test_mailbox_is_cached(vault):
    _git_vault(vault)
    store = GitStateStore(vault)
    assert store.mailbox() is store.mailbox()


def test_file_mailbox_uri_passes_through(vault):
    uri = f"file://{DEFAULT_ROOT}"
    s = GitStateStore(vault, mailbox_uri=uri).mailbox()
    d = open_mailbox(uri, base=vault)
    assert type(s) is type(d) is FileMailbox
    assert s.root == d.root == vault / "Worker" / "agents"


# ---------------------------------------------------------------------------
# planner_kv ↔ worker.load/save_planner_state
# ---------------------------------------------------------------------------


@pytest.fixture
def planner_path(vault, monkeypatch):
    p = vault / "Worker" / "planner_state.json"
    monkeypatch.setattr(worker, "PLANNER_STATE_PATH", p)
    # save_planner_state hashes Todo/items + the queue; keep it off the real vault.
    monkeypatch.setattr(worker, "compute_todo_state_hash", lambda queue=None: "deadbeefdeadbeef")
    return p


def test_planner_kv_default_path_follows_worker(vault, planner_path):
    kv = GitStateStore(vault).planner_kv()
    assert kv.path == worker.PLANNER_STATE_PATH == planner_path


def test_planner_kv_reads_what_save_planner_state_wrote(vault, planner_path):
    worker.save_planner_state("exhausted")
    kv = GitStateStore(vault).planner_kv()
    assert kv.all() == worker.load_planner_state()
    assert kv.get("verdict") == "exhausted"
    assert kv.get("todo_state_hash") == "deadbeefdeadbeef"
    assert kv.get("missing") is None


def test_planner_kv_put_is_read_by_load_planner_state(vault, planner_path):
    worker.save_planner_state("exhausted")
    before = worker.load_planner_state()
    kv = GitStateStore(vault).planner_kv()

    kv.put("verdict", "continue")

    after = worker.load_planner_state()
    assert after["verdict"] == "continue"
    assert after["todo_state_hash"] == before["todo_state_hash"]
    assert after["last_run"] == before["last_run"]
    # Same byte format as save_planner_state.
    assert planner_path.read_text(encoding="utf-8") == (
        json.dumps(after, ensure_ascii=False, indent=2) + "\n")


def test_planner_kv_put_creates_missing_file_as_raw_utf8(vault, planner_path):
    assert not planner_path.parent.exists()
    kv = GitStateStore(vault).planner_kv()
    kv.put("note", "한글")
    assert worker.load_planner_state() == {"note": "한글"}
    assert "한글" in planner_path.read_text(encoding="utf-8")


def test_planner_kv_corrupt_file_reads_empty_and_put_recovers(vault, planner_path):
    planner_path.parent.mkdir(parents=True)
    planner_path.write_text("{not json", encoding="utf-8")
    kv = GitStateStore(vault).planner_kv()
    assert kv.all() == {} == worker.load_planner_state()
    kv.put("k", 1)
    assert worker.load_planner_state() == {"k": 1}


def test_planner_kv_explicit_path_overrides_worker_default(vault, planner_path):
    own = vault / "elsewhere" / "planner.json"
    kv = GitStateStore(vault, planner_state_path=own).planner_kv()
    assert kv.path == own
    kv.put("verdict", "continue")
    assert json.loads(own.read_text(encoding="utf-8")) == {"verdict": "continue"}
    assert not planner_path.exists()


def test_planner_kv_is_cached(vault):
    store = GitStateStore(vault)
    assert store.planner_kv() is store.planner_kv()


# ---------------------------------------------------------------------------
# todo_cache ↔ metrics._db_counts
# ---------------------------------------------------------------------------


def _make_db(db):
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE tasks (slug TEXT, status TEXT, worker_mode TEXT)")
        con.executemany("INSERT INTO tasks VALUES (?, ?, ?)", [
            ("a", "done", "auto"),
            ("b", "done", "delegate"),
            ("c", "done", None),
            ("d", "cancelled", "auto"),
            ("e", "open", "auto"),
        ])
        con.commit()
    finally:
        con.close()


def test_todo_cache_counts_match_metrics(vault, monkeypatch):
    db = vault / "Todo" / "todo.db"
    _make_db(db)
    monkeypatch.setattr(metrics, "DB", db)
    store = GitStateStore(vault)
    assert store.todo_cache().counts() == metrics._db_counts() == {
        "done": 3, "auto_done": 2, "cancelled": 1}


def test_todo_cache_missing_db_is_zero_and_leaves_no_file(vault):
    counts = GitStateStore(vault).todo_cache().counts()
    assert counts == {"done": 0, "auto_done": 0, "cancelled": 0}
    assert not (vault / "Todo" / "todo.db").exists()


def test_todo_cache_tableless_db_matches_metrics_fallback(vault, monkeypatch):
    db = vault / "Todo" / "todo.db"
    db.parent.mkdir(parents=True)
    sqlite3.connect(db).close()
    monkeypatch.setattr(metrics, "DB", db)
    assert GitStateStore(vault).todo_cache().counts() == metrics._db_counts() == {
        "done": 0, "auto_done": 0, "cancelled": 0}


def test_todo_cache_explicit_db_path(vault, tmp_path):
    db = tmp_path / "other" / "todo.db"
    _make_db(db)
    store = GitStateStore(vault, todo_db_path=db)
    assert store.todo_cache().counts()["done"] == 3
    assert store.todo_cache() is store.todo_cache()
