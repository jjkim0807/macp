"""RemoteStateStore (HTTP) — the remote-only behaviors.

Leaf ⑦ of docs/design/state-store.md (§⑧ option (a)). The git+external+remote
*parity* of the ledger / mailbox / planner contracts lives in
``test_state_store.py`` (parametrized over backends); this file holds what only
the remote backend has:

  * #2 bearer-auth rejection (no header / wrong token → 401, no mutation);
  * #3 HQ unreachable → retry-then-stop-without-loss (connection failure, server
    5xx, and read timeout all retry, append to the outbox, and raise; a later
    ``flush_outbox`` against a live HQ lands the row);
  * #4 concurrent lease/claim single execution (two runners, exactly one wins);
  * an end-to-end two-process round-trip over HTTP.

Every server here is an in-process loopback ``ThreadingHTTPServer`` on an
ephemeral port with an injected test token — explicitly NOT a real tailnet
round-trip (that is 7b).
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time

import pytest

from todo_system.orchestrator import message as m
from todo_system.orchestrator import state_store_remote as ssr
from todo_system.orchestrator.assignment import IN_PROGRESS, Assignment
from todo_system.orchestrator.message import Envelope
from todo_system.orchestrator.state_store_remote import (
    RemoteStateStore,
    StateStoreUnavailable,
    make_server,
)
from todo_system.orchestrator.state_store_sqlite import ExternalStateStore

TOKEN = "test-token"  # noqa: S105 — a test fixture's loopback bearer, not a secret


def _closed_port() -> int:
    """An ephemeral port that is bound then immediately released, so a connect to
    it fails fast with ConnectionRefused — a deterministic 'HQ unreachable'."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@contextlib.contextmanager
def _running_server(backend, routes=None):
    """A loopback ThreadingHTTPServer on an ephemeral port, torn down on exit.

    ``handle_error`` is silenced so a client that abandons a slow request (the
    read-timeout test) does not print a broken-pipe traceback from the server
    thread into the captured test output."""
    srv = make_server(backend, TOKEN, routes=routes)
    srv.handle_error = lambda request, client_address: None
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


@pytest.fixture
def backend(tmp_path):
    """The HQ-side ExternalStateStore the server wraps (tmp sqlite)."""
    return ExternalStateStore(sqlite_path=tmp_path / "state.sqlite", vault_root=tmp_path)


@pytest.fixture
def base_url(backend):
    with _running_server(backend) as url:
        yield url


@pytest.fixture
def client(base_url, tmp_path):
    return RemoteStateStore(base_url, TOKEN, outbox_path=tmp_path / "outbox.jsonl")


# ---------------------------------------------------------------------------
# #2 auth — no token / wrong token rejected; no mutation applied
# ---------------------------------------------------------------------------


def test_missing_auth_header_rejected(base_url, tmp_path):
    noauth = RemoteStateStore(base_url, None, outbox_path=tmp_path / "ob.jsonl")
    with pytest.raises(StateStoreUnavailable, match="unauthorized"):
        noauth.ledger().all()


def test_wrong_token_auth_rejected_and_no_mutation(base_url, backend, tmp_path):
    outbox = tmp_path / "ob.jsonl"
    bad = RemoteStateStore(base_url, "wrong-token", outbox_path=outbox)
    a = Assignment(task_id="t-auth", agent_id="x", corr_id="c")
    with pytest.raises(StateStoreUnavailable, match="unauthorized"):
        bad.ledger().save(a)
    # The server applied no write ...
    assert backend.ledger().load("t-auth") is None
    # ... and a 401 is terminal (not a transient outage), so it never outboxes.
    assert not outbox.exists()


def test_correct_token_auth_succeeds(client, backend):
    a = Assignment(task_id="t-ok", agent_id="x", corr_id="c", state=IN_PROGRESS)
    client.ledger().save(a)
    assert backend.ledger().load("t-ok").to_json() == a.to_json()


# ---------------------------------------------------------------------------
# #3 HQ unreachable → retry, then stop WITHOUT losing the task
# ---------------------------------------------------------------------------


def test_connection_failure_retries_outboxes_and_raises(backend, tmp_path, monkeypatch):
    monkeypatch.setattr(ssr, "RETRIES", 3)
    monkeypatch.setattr(ssr, "BACKOFF_S", 0.0)
    outbox = tmp_path / "ob.jsonl"
    dead = RemoteStateStore(f"http://127.0.0.1:{_closed_port()}", TOKEN, outbox_path=outbox)
    a = Assignment(task_id="t-lost", agent_id="ai", corr_id="c", state=IN_PROGRESS)
    with pytest.raises(StateStoreUnavailable):
        dead.ledger().save(a)
    # The write was preserved in the outbox, not silently dropped.
    assert outbox.exists() and outbox.read_text(encoding="utf-8").strip()


def test_server_5xx_retries_then_outboxes(backend, tmp_path, monkeypatch):
    calls = {"n": 0}

    def failing(store, query, body):
        calls["n"] += 1
        return 503, {"error": "down"}

    monkeypatch.setattr(ssr, "RETRIES", 3)
    monkeypatch.setattr(ssr, "BACKOFF_S", 0.0)
    outbox = tmp_path / "ob.jsonl"
    a = Assignment(task_id="t-5xx", agent_id="ai", corr_id="c", state=IN_PROGRESS)
    with _running_server(backend, routes={("POST", "/ledger/save"): failing}) as url:
        c = RemoteStateStore(url, TOKEN, outbox_path=outbox)
        with pytest.raises(StateStoreUnavailable):
            c.ledger().save(a)
    assert calls["n"] == 3  # a 5xx is transient → retried exactly RETRIES times
    assert outbox.exists() and outbox.read_text(encoding="utf-8").strip()


def test_read_timeout_outboxes_without_loss(backend, tmp_path, monkeypatch):
    def slow(store, query, body):
        time.sleep(1.0)  # longer than the shrunk client timeout below
        return 200, {"ok": True}

    monkeypatch.setattr(ssr, "HTTP_TIMEOUT_S", 0.2)
    monkeypatch.setattr(ssr, "RETRIES", 2)
    monkeypatch.setattr(ssr, "BACKOFF_S", 0.0)
    outbox = tmp_path / "ob.jsonl"
    a = Assignment(task_id="t-slow", agent_id="ai", corr_id="c", state=IN_PROGRESS)
    with _running_server(backend, routes={("POST", "/ledger/save"): slow}) as url:
        c = RemoteStateStore(url, TOKEN, outbox_path=outbox)
        with pytest.raises(StateStoreUnavailable):
            c.ledger().save(a)
    assert outbox.exists() and outbox.read_text(encoding="utf-8").strip()


def test_outbox_flush_recovers_on_live_server(backend, tmp_path, monkeypatch):
    monkeypatch.setattr(ssr, "RETRIES", 2)
    monkeypatch.setattr(ssr, "BACKOFF_S", 0.0)
    outbox = tmp_path / "shared-outbox.jsonl"
    a = Assignment(task_id="t-flush", agent_id="ai", corr_id="c", state=IN_PROGRESS)

    # 1) HQ down: the save retries, outboxes, and raises; nothing lands.
    dead = RemoteStateStore(f"http://127.0.0.1:{_closed_port()}", TOKEN, outbox_path=outbox)
    with pytest.raises(StateStoreUnavailable):
        dead.ledger().save(a)
    assert backend.ledger().load("t-flush") is None

    # 2) HQ recovered: a client on the live server flushes the queued write.
    with _running_server(backend) as url:
        live = RemoteStateStore(url, TOKEN, outbox_path=outbox)
        assert live.flush_outbox() == 1
        assert backend.ledger().load("t-flush").to_json() == a.to_json()  # not lost
        assert outbox.read_text(encoding="utf-8") == ""  # cleared after a full flush


def test_flush_outbox_is_idempotent(backend, base_url, tmp_path):
    # Two queued copies of the same save replay to a single row (upsert).
    outbox = tmp_path / "idem.jsonl"
    c = RemoteStateStore(base_url, TOKEN, outbox_path=outbox)
    a = Assignment(task_id="t-idem", agent_id="ai", corr_id="c", state=IN_PROGRESS)
    c._append_outbox("POST", "/ledger/save", {"data_json": a.to_json()})
    c._append_outbox("POST", "/ledger/save", {"data_json": a.to_json()})
    assert c.flush_outbox() == 2
    assert backend.ledger().load("t-idem").to_json() == a.to_json()
    assert [x.task_id for x in backend.ledger().all()] == ["t-idem"]  # one row, not two


def test_flush_outbox_empty_when_no_outbox(client):
    assert client.flush_outbox() == 0


# ---------------------------------------------------------------------------
# #4 concurrent lease/claim — two runners, the message executes exactly once
# ---------------------------------------------------------------------------


def test_two_runners_claim_same_task_exactly_once(base_url, tmp_path):
    task_id = "shared-task"
    results: dict[str, bool] = {}
    start = threading.Barrier(2)

    def run(owner):
        c = RemoteStateStore(base_url, TOKEN, outbox_path=tmp_path / f"ob-{owner}.jsonl")
        start.wait()  # release both threads together to maximize the race
        results[owner] = c.claim(task_id, owner=owner, ttl=60)

    threads = [threading.Thread(target=run, args=(o,)) for o in ("runner-A", "runner-B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert sorted(results) == ["runner-A", "runner-B"]
    granted = [o for o, v in results.items() if v]
    assert len(granted) == 1, f"expected exactly one grant, got {results}"


def test_claim_denies_second_owner_and_regrants_holder(client):
    assert client.claim("c-task", owner="A", ttl=60) is True
    assert client.claim("c-task", owner="B", ttl=60) is False  # A still holds the lease
    assert client.claim("c-task", owner="A", ttl=60) is True   # idempotent for the holder


def test_expired_claim_can_be_reacquired(client):
    # ttl=0 → the row is already expired, so a different owner re-acquires it
    # (design §⑧ "TTL 로 크래시 복구").
    assert client.claim("exp-task", owner="A", ttl=0) is True
    assert client.claim("exp-task", owner="B", ttl=60) is True


# ---------------------------------------------------------------------------
# End-to-end two-process round-trip over HTTP
# ---------------------------------------------------------------------------


def test_two_process_roundtrip_send_claim_ack(base_url, tmp_path):
    orch = RemoteStateStore(base_url, TOKEN, outbox_path=tmp_path / "orch.jsonl")
    runner = RemoteStateStore(base_url, TOKEN, outbox_path=tmp_path / "runner.jsonl")

    assign = Envelope(to="ai-remote", type=m.TASK_ASSIGN, task_id="task-xyz")
    orch.mailbox().send(assign)

    got = runner.mailbox().receive("ai-remote")
    assert [e.to_json() for e in got] == [assign.to_json()]  # byte-identical across the hop
    assert runner.claim("task-xyz", owner="ai-remote", ttl=60) is True
    runner.mailbox().ack("ai-remote", got)
    assert runner.mailbox().receive("ai-remote") == []  # consumed; not redelivered
