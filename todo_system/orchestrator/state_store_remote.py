"""Remote state backend — the external SQLite reached over an authenticated HTTP hop.

Leaf ⑦ of docs/design/state-store.md (§⑧ option (a)). The ``external`` backend
(leaf ④) keeps the authoritative SQLite on one primary host (HQ); a runner on a
*different* machine cannot open that file handle, so this module exposes the same
state over the network:

  * :func:`make_server` — an HQ-side HTTP state API that wraps an
    :class:`~todo_system.orchestrator.state_store_sqlite.ExternalStateStore`
    behind one bearer-token gate and a route *table*. The table is extensible: a
    later ``macp-graph-memory-v2`` query path mounts by passing extra ``routes``,
    so auth/routing stay general rather than hardened to state-only (task body).
  * :class:`RemoteStateStore` — the client the remote runner uses. Its ledger /
    mailbox / planner_kv / todo_cache sub-surfaces call the API; a network failure
    raises :class:`StateStoreUnavailable` after a bounded retry/backoff, and a
    failed *write* is appended to a host-local append-only outbox so the task
    state is not lost (:meth:`RemoteStateStore.flush_outbox` replays it when HQ
    recovers — design §⑧ "본부 장애 시 동작").
  * :meth:`RemoteStateStore.claim` — the single-execution lease (design §⑧
    lease/claim): a ``BEGIN IMMEDIATE`` conditional insert into a dedicated
    ``claims`` table, so two runners that receive the same message execute it
    exactly once. The claims table is bootstrapped by the server on the HQ
    SQLite, so ``state_store_sqlite.py`` (whose contract is out of scope) is NOT
    modified.

Stdlib-only (``http.server`` / ``urllib`` / ``json`` / ``hmac`` / ``sqlite3`` via
the sibling's ``_connect``), so ``scripts/check_engine_isolation.sh`` stays
``RESULT: PASS``. ``open_state_store`` imports this module lazily, so the git and
external paths never load ``http.server``.

This module is the code only — opening a real network service, installing a unit,
or ``tailscale serve`` is task 7b (human).
"""

from __future__ import annotations

import hmac
import http.server
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .assignment import Assignment, Ledger
from .mailbox import Mailbox
from .message import Envelope
from .state_store import PlannerKV, StateStore, TodoCacheReader
from .state_store_sqlite import _connect

__all__ = [
    "DEFAULT_CLAIM_TTL_S",
    "RemoteStateStore",
    "StateStoreUnavailable",
    "default_routes",
    "make_server",
]


# --- exception (design §② "오류 타입") --------------------------------------


class StateStoreUnavailable(RuntimeError):
    """The remote state API could not be reached or authenticated.

    The common network-failure type the worker tick catches to drive the §⑧
    retry → local-outbox fallback, exactly where a git backend's subprocess
    timeout surfaces today. ``unauthorized`` (HTTP 401) raises this immediately
    with no retry and no outbox.
    """


# --- timeout / backoff (mirror mailbox.py:43-45; read at call time) ----------
# urllib takes ONE timeout covering connect+read, so a single constant is the
# honest shape here (unlike the git path's two ceilings). These are module
# globals read inside _request, so a test can shrink them with monkeypatch.
HTTP_TIMEOUT_S = 30.0
RETRIES = 3
BACKOFF_S = 0.5

# Default lease length for claim(); the server stamps expires_ts = now + ttl.
DEFAULT_CLAIM_TTL_S = 3600.0

_ZERO_COUNTS = {"done": 0, "auto_done": 0, "cancelled": 0}


# ===========================================================================
# Server — HQ-side state API wrapping an ExternalStateStore
# ===========================================================================

_CLAIMS_SCHEMA = """
CREATE TABLE IF NOT EXISTS claims (
  task_id     TEXT PRIMARY KEY,
  owner       TEXT NOT NULL,
  acquired_ts REAL NOT NULL,
  expires_ts  REAL NOT NULL
);
"""


def _bootstrap_claims(sqlite_path: Path | str) -> None:
    """Create the ``claims`` table on the HQ SQLite (idempotent).

    Added here, not in ``state_store_sqlite._bootstrap``, so the leaf-④ schema
    contract is untouched — claims are a remote-only concern.
    """
    con = _connect(sqlite_path)
    try:
        con.executescript(_CLAIMS_SCHEMA)
        con.commit()
    finally:
        con.close()


def _claim(sqlite_path: Path | str, key: str, owner: str, ttl: float) -> bool:
    """Atomic single-execution lease. Returns True iff ``owner`` holds the claim.

    ``BEGIN IMMEDIATE`` takes the write lock up front, so two racing callers
    serialize: the first inserts and wins; the second waits (WAL + busy_timeout),
    finds the row via ``ON CONFLICT DO NOTHING``, reads back the first owner, and
    is denied. An expired row (``expires_ts <= now``) is cleared first, so a
    crashed owner's lease can be re-acquired after its TTL (design §⑧ "TTL 로
    크래시 복구").
    """
    now = time.time()
    con = _connect(sqlite_path)
    con.isolation_level = None  # manual transaction control for BEGIN IMMEDIATE
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM claims WHERE task_id=? AND expires_ts<=?", (key, now))
        con.execute(
            "INSERT INTO claims (task_id, owner, acquired_ts, expires_ts)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(task_id) DO NOTHING",
            (key, owner, now, now + ttl),
        )
        row = con.execute(
            "SELECT owner FROM claims WHERE task_id=?", (key,)
        ).fetchone()
        con.execute("COMMIT")
    finally:
        con.close()
    return row is not None and row[0] == owner


# Route handlers: (store, query, body) -> (http_status, json_obj)


def _r_ledger_save(store, query, body):
    store.ledger().save(Assignment.from_json(body["data_json"]))
    return 200, {"ok": True}


def _r_ledger_load(store, query, body):
    a = store.ledger().load(query["task_id"])
    return 200, {"data_json": a.to_json() if a is not None else None}


def _r_ledger_all(store, query, body):
    return 200, {"assignments": [a.to_json() for a in store.ledger().all()]}


def _r_mailbox_send(store, query, body):
    store.mailbox().send(Envelope.from_json(body["envelope"]))
    return 200, {"ok": True}


def _r_mailbox_receive(store, query, body):
    envs = store.mailbox().receive(query["agent_id"])
    return 200, {"envelopes": [e.to_json() for e in envs]}


def _r_mailbox_ack(store, query, body):
    envs = [Envelope.from_json(e) for e in body.get("envelopes", [])]
    store.mailbox().ack(body["agent_id"], envs)
    return 200, {"ok": True}


def _r_mailbox_init_agent(store, query, body):
    store.mailbox().init_agent(body["agent_id"])
    return 200, {"ok": True}


def _r_planner_get(store, query, body):
    # Membership on all(), not get(): distinguishes "absent" from a stored null.
    state = store.planner_kv().all()
    key = query["key"]
    if key in state:
        return 200, {"found": True, "value": state[key]}
    return 200, {"found": False}


def _r_planner_put(store, query, body):
    store.planner_kv().put(body["key"], body["value"])
    return 200, {"ok": True}


def _r_planner_all(store, query, body):
    return 200, {"state": store.planner_kv().all()}


def _r_todo_counts(store, query, body):
    return 200, {"counts": store.todo_cache().counts()}


def _r_claim(store, query, body):
    granted = _claim(
        store.sqlite_path, body["key"], body["owner"],
        float(body.get("ttl", DEFAULT_CLAIM_TTL_S)),
    )
    return 200, {"granted": granted}


def default_routes() -> dict:
    """The default ``(method, path) -> handler`` state route table.

    A caller extends it (e.g. a later graph-memory-v2 query path) by passing
    ``routes=`` to :func:`make_server`, which merges them over these.
    """
    return {
        ("POST", "/ledger/save"): _r_ledger_save,
        ("GET", "/ledger/load"): _r_ledger_load,
        ("GET", "/ledger/all"): _r_ledger_all,
        ("POST", "/mailbox/send"): _r_mailbox_send,
        ("GET", "/mailbox/receive"): _r_mailbox_receive,
        ("POST", "/mailbox/ack"): _r_mailbox_ack,
        ("POST", "/mailbox/init_agent"): _r_mailbox_init_agent,
        ("GET", "/planner/get"): _r_planner_get,
        ("POST", "/planner/put"): _r_planner_put,
        ("GET", "/planner/all"): _r_planner_all,
        ("GET", "/todo/counts"): _r_todo_counts,
        ("POST", "/claim"): _r_claim,
    }


def make_server(store, token, *, routes=None, bind=("127.0.0.1", 0)):
    """A :class:`~http.server.ThreadingHTTPServer` exposing ``store`` behind a
    bearer-token gate and a route table.

    ``store`` is an ``ExternalStateStore`` (the HQ SoT). ``token`` is the shared
    bearer secret. ``routes`` are merged over :func:`default_routes`, so a caller
    can mount extra paths without losing the state routes (extensibility — the
    task body's graph-memory-v2 requirement). ``bind`` defaults to an ephemeral
    loopback port (``("127.0.0.1", 0)``) for tests; read the actual port from
    ``server.server_address[1]``.
    """
    _bootstrap_claims(store.sqlite_path)
    route_table = default_routes()
    if routes:
        route_table.update(routes)

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence the default stderr access log
            pass

        def _auth_ok(self) -> bool:
            header = self.headers.get("Authorization", "")
            prefix = "Bearer "
            if not header.startswith(prefix):
                return False
            provided = header[len(prefix):]
            # constant-time compare; an empty bearer also fails.
            return bool(provided) and hmac.compare_digest(provided, token)

        def _send(self, status: int, obj) -> None:
            payload = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _dispatch(self, method: str) -> None:
            # Drain the request body first (so a 401 does not desync the socket),
            # THEN gate on auth before parsing it or touching any route — an
            # unauthenticated call applies no mutation.
            raw = b""
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    raw = self.rfile.read(length)
            if not self._auth_ok():
                self._send(401, {"error": "unauthorized"})
                return
            parsed = urllib.parse.urlparse(self.path)
            handler = route_table.get((method, parsed.path))
            if handler is None:
                self._send(404, {"error": "not found"})
                return
            body = {}
            if raw:
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    self._send(400, {"error": "bad json"})
                    return
            query = {
                k: v[-1]
                for k, v in urllib.parse.parse_qs(parsed.query).items()
            }
            try:
                status, obj = handler(store, query, body)
            except Exception as exc:  # noqa: BLE001 — surface as 500 so the client retries
                self._send(500, {"error": str(exc)})
                return
            self._send(status, obj)

        def do_GET(self):  # noqa: N802 (http.server API)
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

    return http.server.ThreadingHTTPServer(bind, _Handler)


# ===========================================================================
# Client — the RemoteStateStore a remote runner uses
# ===========================================================================


class RemoteLedger(Ledger):
    """Ledger over HTTP. ``active()`` is inherited (it only calls ``load``).

    ``Ledger.root`` is never read — ``save`` / ``load`` / ``all`` are overridden
    and ``_path`` (the only reader) is never reached — so ``__init__`` does not
    set it, mirroring ``SqliteLedger``.
    """

    def __init__(self, client: "RemoteStateStore"):
        self._client = client

    def save(self, a: Assignment) -> None:
        self._client._request(
            "POST", "/ledger/save", body={"data_json": a.to_json()}, write=True)

    def load(self, task_id: str) -> Assignment | None:
        resp = self._client._request("GET", "/ledger/load", query={"task_id": task_id})
        data_json = resp.get("data_json")
        return Assignment.from_json(data_json) if data_json else None

    def all(self) -> list[Assignment]:
        resp = self._client._request("GET", "/ledger/all")
        return [Assignment.from_json(dj) for dj in resp.get("assignments", [])]


class RemoteMailbox(Mailbox):
    """Mailbox over HTTP. The cursor/tombstones live server-side in the wrapped
    ``SqliteMailbox``, so ``ack`` is overridden entirely (no local cursor), and
    every ``self.root``-reading seam is overridden too (the root is unused).
    """

    def __init__(self, client: "RemoteStateStore"):
        super().__init__(Path("."))  # root unused; every seam below is overridden
        self._client = client

    def init_agent(self, agent_id: str) -> None:
        # No-op server-side; not a write to preserve, so it never hits the outbox.
        self._client._request(
            "POST", "/mailbox/init_agent", body={"agent_id": agent_id}, write=False)

    def send(self, msg: Envelope) -> Path:
        self._client._request(
            "POST", "/mailbox/send", body={"envelope": msg.to_json()}, write=True)
        return Path(".")  # sentinel: production routing never reads send()'s path

    def receive(self, agent_id: str) -> list[Envelope]:
        resp = self._client._request("GET", "/mailbox/receive", query={"agent_id": agent_id})
        return [Envelope.from_json(e) for e in resp.get("envelopes", [])]

    def ack(self, agent_id: str, msgs) -> None:
        if isinstance(msgs, Envelope):
            msgs = [msgs]
        payload = [m.to_json() for m in msgs]
        if not payload:
            return
        self._client._request(
            "POST", "/mailbox/ack",
            body={"agent_id": agent_id, "envelopes": payload}, write=True)


class RemotePlannerKV(PlannerKV):
    """Planner kv over HTTP. ``all()`` is the whole state dict; ``get`` returns
    None for an absent key (the server distinguishes absent from a stored null)."""

    def __init__(self, client: "RemoteStateStore"):
        self._client = client

    def get(self, key: str):
        resp = self._client._request("GET", "/planner/get", query={"key": key})
        return resp.get("value") if resp.get("found") else None

    def put(self, key: str, value) -> None:
        self._client._request(
            "POST", "/planner/put", body={"key": key, "value": value}, write=True)

    def all(self) -> dict:
        resp = self._client._request("GET", "/planner/all")
        return resp.get("state", {})


class RemoteTodoCacheReader(TodoCacheReader):
    """todo.db counts over HTTP (read from the HQ's derived cache)."""

    def __init__(self, client: "RemoteStateStore"):
        self._client = client

    def counts(self) -> dict:
        resp = self._client._request("GET", "/todo/counts")
        return resp.get("counts", dict(_ZERO_COUNTS))


class RemoteStateStore(StateStore):
    """The state store a remote runner uses: every sub-surface calls the HQ API.

    Sub-surfaces are built on first use and cached per instance, mirroring
    ``GitStateStore`` / ``ExternalStateStore``. ``claim`` and ``flush_outbox``
    are remote-only (NOT on the ``StateStore`` ABC), so git/external parity is
    untouched.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None,
        *,
        outbox_path: str | Path | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._outbox_path = Path(outbox_path).expanduser() if outbox_path else None
        self._ledger: Ledger | None = None
        self._mailbox: Mailbox | None = None
        self._planner_kv: PlannerKV | None = None
        self._todo_cache: TodoCacheReader | None = None

    # -- sub-surfaces ----------------------------------------------------
    def ledger(self) -> Ledger:
        if self._ledger is None:
            self._ledger = RemoteLedger(self)
        return self._ledger

    def mailbox(self) -> Mailbox:
        if self._mailbox is None:
            self._mailbox = RemoteMailbox(self)
        return self._mailbox

    def planner_kv(self) -> PlannerKV:
        if self._planner_kv is None:
            self._planner_kv = RemotePlannerKV(self)
        return self._planner_kv

    def todo_cache(self) -> TodoCacheReader:
        if self._todo_cache is None:
            self._todo_cache = RemoteTodoCacheReader(self)
        return self._todo_cache

    # -- remote-only surfaces --------------------------------------------
    def claim(self, key: str, owner: str, ttl: float = DEFAULT_CLAIM_TTL_S) -> bool:
        """Acquire the single-execution lease for ``key``.

        ``write=False``: claiming needs a live HQ (read-degrade — a down HQ means
        "don't pick up new work", design §⑧), so a failure raises rather than
        queuing to the outbox.
        """
        resp = self._request(
            "POST", "/claim",
            body={"key": key, "owner": owner, "ttl": ttl}, write=False)
        return bool(resp.get("granted"))

    # -- HTTP + outbox ---------------------------------------------------
    def _request(self, method: str, path: str, *, query=None, body=None, write=False) -> dict:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {"Authorization": f"Bearer {self.token or ''}"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_exc: Exception | None = None
        for attempt in range(RETRIES):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                    raw = resp.read()
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    raise StateStoreUnavailable("unauthorized") from exc
                if exc.code < 500:
                    # A client error (bad request) is a bug, not a transient
                    # outage: do not retry and do not queue to the outbox.
                    raise StateStoreUnavailable(
                        f"HTTP {exc.code} for {method} {path}") from exc
                last_exc = exc  # 5xx is transient → retry
            except (urllib.error.URLError, OSError) as exc:  # timeout/refused/reset
                last_exc = exc
            if attempt < RETRIES - 1:
                time.sleep(BACKOFF_S * (attempt + 1))

        if write:
            self._append_outbox(method, path, body)
        raise StateStoreUnavailable(
            f"state API unreachable at {self.base_url}: {last_exc}")

    def _resolve_outbox(self) -> Path:
        if self._outbox_path is not None:
            return self._outbox_path
        from .. import paths

        return paths.state_sqlite_path().parent / "state_remote_outbox.jsonl"

    def _append_outbox(self, method: str, path: str, body) -> None:
        outbox = self._resolve_outbox()
        outbox.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"method": method, "path": path, "body": body}, ensure_ascii=False)
        with outbox.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def flush_outbox(self) -> int:
        """Replay queued writes against a live HQ, in order, then clear the outbox.

        Idempotent: every queued op is an upsert, so a partial earlier flush
        re-applies harmlessly. On a replay failure the outbox is left intact and
        :class:`StateStoreUnavailable` propagates; the next flush retries the
        lot. Returns the number of ops applied.
        """
        outbox = self._resolve_outbox()
        if not outbox.exists():
            return 0
        lines = [ln for ln in outbox.read_text(encoding="utf-8").splitlines() if ln.strip()]
        applied = 0
        for ln in lines:
            rec = json.loads(ln)
            self._request(rec["method"], rec["path"], body=rec.get("body"), write=False)
            applied += 1
        outbox.write_text("", encoding="utf-8")
        return applied


# ===========================================================================
# CLI — `serve` the HQ state API (code only; exposing the port is 7b/human)
# ===========================================================================


def _main(argv: list[str] | None = None) -> int:
    import argparse

    from .. import paths
    from .state_store_sqlite import ExternalStateStore

    p = argparse.ArgumentParser(
        prog="python -m todo_system.orchestrator.state_store_remote")
    sub = p.add_subparsers(dest="cmd", required=True)
    p_serve = sub.add_parser(
        "serve",
        help="Run the HQ state API server (code only; exposing the port / "
             "installing a unit / tailscale serve is task 7b, human)")
    p_serve.add_argument("--bind", default="127.0.0.1:8787", help="host:port to listen on")
    p_serve.add_argument(
        "--sqlite-path", default=None,
        help="HQ state SQLite (default: paths.state_sqlite_path())")

    args = p.parse_args(argv)
    if args.cmd == "serve":
        token = paths.state_remote_token()
        if not token:
            print("error: no bearer token — set MACP_STATE_REMOTE_TOKEN or "
                  "MACP_STATE_REMOTE_TOKEN_FILE")
            return 2
        sqlite_path = (
            Path(args.sqlite_path).expanduser() if args.sqlite_path
            else paths.state_sqlite_path())
        store = ExternalStateStore(sqlite_path=sqlite_path)
        host, _, port = args.bind.rpartition(":")
        server = make_server(store, token, bind=(host or "127.0.0.1", int(port)))
        # The token is never printed.
        print(f"state API serving on {args.bind} (sqlite={sqlite_path})")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()
            server.server_close()
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
