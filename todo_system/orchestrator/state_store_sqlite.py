"""External state backend — host-local SQLite outside the vault git tree.

Leaf ④ of docs/design/state-store.md. The ``external`` backend keeps the
assignment ledger, the mailbox, and the planner kv in a single SQLite file
(``paths.state_sqlite_path()``, default ``~/.local/share/macp/state.sqlite``),
*outside* the vault tree — so none of the git-manipulation hazards the git
backend carries (a stage's ``git restore`` / ``reset`` / ``stash`` resurrecting
an acked message or rewinding the ledger) can touch authoritative state.

The sub-surfaces are subclasses of the git-backend implementations (design §②
composition decision):

  * ``SqliteLedger(Ledger)``       — ``save`` / ``load`` / ``all`` on the
    ``ledger`` table (``active`` inherited; it only calls the overridden
    ``load``).
  * ``SqliteMailbox(Mailbox)``     — ``send`` / ``receive`` + the cursor seams on
    the ``mailbox`` / ``mailbox_cursor`` tables; the base ``ack`` (at-least-once
    + tombstones) is reused unchanged.
  * ``SqlitePlannerKV(PlannerKV)`` — ``get`` / ``put`` / ``all`` on the
    ``planner_kv`` table.
  * todo_cache is NOT migrated — ``todo.db`` is a derived md cache (design §②),
    so the external store reuses the git backend's ``GitTodoCacheReader``.

Each row stores the object's full canonical JSON in an authoritative blob column
(``data_json`` / ``envelope_json``); ``load`` / ``receive`` reconstruct ONLY
from that blob via ``Assignment.from_json`` / ``Envelope.from_json``, so the
parametrized git+external parity suite holds *by construction* and a future
field added to ``Assignment`` / ``Envelope`` cannot be silently dropped by a
per-field column map (the hazard ``from_json``'s unknown-key drop already
guards). The denormalized columns (``task_id`` / ``state`` / ``to_agent`` /
``msg_id`` / timestamps) back the design's ``ledger_state`` / ``mailbox_to``
indexes and the leaf ⑤/⑥ consumers.

Connections are short-lived and per-operation (the ``GitTodoCacheReader``
pattern): every DB touch opens a fresh connection with ``PRAGMA busy_timeout``
and ``PRAGMA journal_mode=WAL``, runs its statement(s) in one transaction, and
closes. That is thread-safe (the runner runs stages on threads) *and*
process-safe — two OS processes get independent connections that WAL +
busy_timeout serialize without a "database is locked" error.

Stdlib-only (``sqlite3`` / ``json``), so ``scripts/check_engine_isolation.sh``
stays ``RESULT: PASS``. ``open_state_store`` imports this module lazily, so the
default (git) path never loads it.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .. import paths
from .assignment import Assignment, Ledger
from .mailbox import Mailbox
from .message import Envelope, now_iso
from .state_store import GitTodoCacheReader, PlannerKV, StateStore, TodoCacheReader

__all__ = [
    "BUSY_TIMEOUT_MS",
    "SCHEMA_VERSION",
    "ExternalStateStore",
    "SqliteLedger",
    "SqliteMailbox",
    "SqlitePlannerKV",
]

SCHEMA_VERSION = 1
# Per-connection pragma, read at connect time so a test can shrink it before a
# contention check. 5 s ≫ any single statement here, so a contended writer waits
# and wins rather than raising OperationalError.
BUSY_TIMEOUT_MS = 5000

# §③ schema, finalized: full canonical JSON is authoritative (data_json /
# envelope_json); the scalar columns are denormalized for the design's indexes
# and the leaf ⑤/⑥ consumers.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS ledger (
  task_id    TEXT PRIMARY KEY,
  agent_id   TEXT NOT NULL,
  corr_id    TEXT NOT NULL,
  state      TEXT NOT NULL,
  created_ts TEXT,
  updated_ts TEXT,
  data_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_state ON ledger(state);
CREATE TABLE IF NOT EXISTS mailbox (
  msg_id        TEXT PRIMARY KEY,
  to_agent      TEXT NOT NULL,
  type          TEXT NOT NULL,
  task_id       TEXT,
  corr_id       TEXT,
  created_ts    TEXT NOT NULL,
  envelope_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS mailbox_to ON mailbox(to_agent, msg_id);
CREATE TABLE IF NOT EXISTS mailbox_cursor (
  agent_id TEXT NOT NULL,
  msg_id   TEXT NOT NULL,
  status   TEXT NOT NULL,
  acked_at REAL,
  PRIMARY KEY (agent_id, msg_id)
);
CREATE TABLE IF NOT EXISTS planner_kv (
  key        TEXT PRIMARY KEY,
  value_json TEXT,
  updated_ts TEXT
);
"""


def _connect(path: Path | str) -> sqlite3.Connection:
    """Open a fresh connection with the per-connection pragmas set.

    ``busy_timeout`` is per-connection and MUST be set on every connect;
    ``journal_mode=WAL`` is persisted in the DB header by :func:`_bootstrap` and
    re-asserted here defensively (idempotent). Callers own the connection and
    close it in a ``finally``.
    """
    con = sqlite3.connect(str(path))
    # busy_timeout first, so the WAL pragma below (and every later statement)
    # waits on a contended write lock instead of raising at once.
    con.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_MS)}")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def _bootstrap(path: Path) -> None:
    """Create the schema (idempotent) and stamp ``meta.schema_version``.

    Safe to run concurrently from several processes: every statement is
    ``IF NOT EXISTS`` / ``INSERT OR IGNORE`` and runs under WAL + busy_timeout.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    con = _connect(path)
    try:
        con.executescript(_SCHEMA)
        con.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        con.commit()
    finally:
        con.close()


class SqliteLedger(Ledger):
    """:class:`~todo_system.orchestrator.assignment.Ledger` on the ``ledger`` table.

    ``save`` / ``load`` / ``all`` are overridden to hit SQLite; ``active`` is
    inherited (it only calls the overridden ``load``). ``Ledger.root`` is never
    touched, so ``__init__`` does not set it.
    """

    def __init__(self, sqlite_path: str | Path):
        self.sqlite_path = Path(sqlite_path)

    def save(self, a: Assignment) -> None:
        con = _connect(self.sqlite_path)
        try:
            con.execute(
                "INSERT INTO ledger"
                " (task_id, agent_id, corr_id, state, created_ts, updated_ts, data_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(task_id) DO UPDATE SET"
                "   agent_id=excluded.agent_id, corr_id=excluded.corr_id,"
                "   state=excluded.state, created_ts=excluded.created_ts,"
                "   updated_ts=excluded.updated_ts, data_json=excluded.data_json",
                (a.task_id, a.agent_id, a.corr_id, a.state,
                 a.created_ts, a.updated_ts, a.to_json()),
            )
            con.commit()
        finally:
            con.close()

    def load(self, task_id: str) -> Assignment | None:
        con = _connect(self.sqlite_path)
        try:
            row = con.execute(
                "SELECT data_json FROM ledger WHERE task_id=?", (task_id,)
            ).fetchone()
        finally:
            con.close()
        return Assignment.from_json(row[0]) if row is not None else None

    def all(self) -> list[Assignment]:
        con = _connect(self.sqlite_path)
        try:
            rows = con.execute(
                "SELECT data_json FROM ledger ORDER BY task_id"
            ).fetchall()
        finally:
            con.close()
        # ORDER BY task_id matches the git Ledger's filename-sorted all().
        return [Assignment.from_json(r[0]) for r in rows]


class SqlitePlannerKV(PlannerKV):
    """:class:`~todo_system.orchestrator.state_store.PlannerKV` on ``planner_kv``.

    Like the git ``GitPlannerKV``, ``all()`` is the whole state dict and
    ``put()`` replaces one key while keeping the others.
    """

    def __init__(self, sqlite_path: str | Path):
        self.sqlite_path = Path(sqlite_path)

    def get(self, key: str) -> Any | None:
        con = _connect(self.sqlite_path)
        try:
            row = con.execute(
                "SELECT value_json FROM planner_kv WHERE key=?", (key,)
            ).fetchone()
        finally:
            con.close()
        return json.loads(row[0]) if row is not None else None

    def put(self, key: str, value: Any) -> None:
        con = _connect(self.sqlite_path)
        try:
            con.execute(
                "INSERT OR REPLACE INTO planner_kv(key, value_json, updated_ts)"
                " VALUES (?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), now_iso()),
            )
            con.commit()
        finally:
            con.close()

    def all(self) -> dict:
        con = _connect(self.sqlite_path)
        try:
            rows = con.execute("SELECT key, value_json FROM planner_kv").fetchall()
        finally:
            con.close()
        return {k: json.loads(v) for k, v in rows}


class SqliteMailbox(Mailbox):
    """:class:`~todo_system.orchestrator.mailbox.Mailbox` on SQLite tables.

    The base ``ack`` is pure orchestration over ``_load_cursor`` / ``_after_ack``
    / ``_save_cursor`` / ``_prune_tombstones`` (``mailbox.py``), so overriding
    the first three to hit the ``mailbox`` / ``mailbox_cursor`` tables preserves
    the at-least-once + tombstone contract **without re-implementing ack**.
    ``send`` / ``receive`` are file-coupled in the base, so they are overridden;
    ``init_agent`` is a no-op (no inbox dir — rows are keyed by ``to_agent`` and
    created on ``send``). With these seams overridden no reused base method ever
    reads ``self.root``.

    Like ``FileMailbox``, an acked row is DELETEd (consumption is permanent); the
    tombstone the inherited ``ack`` still records is redundant here but kept so
    the cursor shape stays identical to the git backend (§⑤ migration / parity).
    """

    def __init__(self, sqlite_path: str | Path):
        # Sets self.root (unused) so the Mailbox contract holds; every seam that
        # would read it is overridden below.
        super().__init__(sqlite_path)
        self.sqlite_path = Path(sqlite_path)

    def init_agent(self, agent_id: str) -> None:
        pass  # no inbox directory in SQLite

    def send(self, msg: Envelope) -> Path:
        con = _connect(self.sqlite_path)
        try:
            con.execute(
                "INSERT OR REPLACE INTO mailbox"
                " (msg_id, to_agent, type, task_id, corr_id, created_ts, envelope_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (msg.msg_id, msg.to, msg.type, msg.task_id, msg.corr_id,
                 msg.ts, msg.to_json()),
            )
            con.commit()
        finally:
            con.close()
        # External messages live in the DB, not a file; the -> Path signature is
        # satisfied by the DB path. No production caller depends on send()'s
        # return being an inbox file (routing is leaf ⑥); only tests read it.
        return self.sqlite_path

    def receive(self, agent_id: str) -> list[Envelope]:
        """Unacked messages for ``agent_id``, FIFO by ``msg_id`` (ULID time
        order). At-least-once: a message stays returned until ``ack``ed (its row
        is present and its id is in neither ``seen`` nor the ack tombstones)."""
        seen, acked = self._load_cursor(agent_id)
        con = _connect(self.sqlite_path)
        try:
            rows = con.execute(
                "SELECT msg_id, envelope_json FROM mailbox"
                " WHERE to_agent=? ORDER BY msg_id",
                (agent_id,),
            ).fetchall()
        finally:
            con.close()
        out: list[Envelope] = []
        for msg_id, envelope_json in rows:
            if msg_id in seen or msg_id in acked:   # same filter as mailbox.py
                continue
            try:
                out.append(Envelope.from_json(envelope_json))
            except (json.JSONDecodeError, ValueError):
                continue  # skip a malformed row; a later pass can still see it
        return out

    def _load_cursor(self, agent_id: str) -> tuple[set[str], dict[str, float]]:
        con = _connect(self.sqlite_path)
        try:
            rows = con.execute(
                "SELECT msg_id, status, acked_at FROM mailbox_cursor WHERE agent_id=?",
                (agent_id,),
            ).fetchall()
        finally:
            con.close()
        seen = {mid for mid, status, _ in rows if status == "seen"}
        acked = {mid: float(acked_at) for mid, status, acked_at in rows
                 if status == "acked" and acked_at is not None}
        return seen, acked

    def _save_cursor(self, agent_id: str, seen: set[str],
                     acked: dict[str, float]) -> None:
        # Rewrite the agent's cursor wholesale, mirroring the base's atomic
        # cursor-file write. seen and acked are disjoint by the time ack() calls
        # this, so no (agent_id, msg_id) PK collision.
        con = _connect(self.sqlite_path)
        try:
            con.execute("DELETE FROM mailbox_cursor WHERE agent_id=?", (agent_id,))
            con.executemany(
                "INSERT INTO mailbox_cursor(agent_id, msg_id, status, acked_at)"
                " VALUES (?, ?, 'seen', NULL)",
                [(agent_id, mid) for mid in sorted(seen)],
            )
            con.executemany(
                "INSERT INTO mailbox_cursor(agent_id, msg_id, status, acked_at)"
                " VALUES (?, ?, 'acked', ?)",
                [(agent_id, mid, ts) for mid, ts in sorted(acked.items())],
            )
            con.commit()
        finally:
            con.close()

    def _after_ack(self, agent_id: str, acked_ids: set[str]) -> set[str]:
        """Delete the consumed rows and report them all gone — the FileMailbox
        contract (``mailbox.py``: return everything consumed, whether a row was
        present or already absent). The inherited ``ack`` then moves these ids to
        tombstones and prunes them."""
        ids = list(acked_ids)
        if ids:
            con = _connect(self.sqlite_path)
            try:
                placeholders = ",".join("?" * len(ids))
                con.execute(
                    f"DELETE FROM mailbox WHERE to_agent=? AND msg_id IN ({placeholders})",
                    (agent_id, *ids),
                )
                con.commit()
            finally:
                con.close()
        return set(acked_ids)


class ExternalStateStore(StateStore):
    """The external backend: one host-local SQLite file, built by composition.

    Mirrors :class:`~todo_system.orchestrator.state_store.GitStateStore` — the
    sub-surfaces are built on first use and cached per instance. The schema is
    bootstrapped in ``__init__`` so a freshly-pointed path is usable at once.
    todo_cache reuses the git backend's ``GitTodoCacheReader`` unchanged (todo.db
    is a derived md cache, not migrated — design §②).
    """

    def __init__(
        self,
        sqlite_path: str | Path | None = None,
        vault_root: str | Path | None = None,
        todo_db_path: str | Path | None = None,
    ):
        self.sqlite_path = (
            Path(sqlite_path) if sqlite_path is not None
            else paths.state_sqlite_path()
        )
        # Same resolution as GitStateStore, for the todo_cache db default below.
        self.vault_root = (
            Path(vault_root) if vault_root is not None
            else paths.vault_root(start=paths.vault_search_start())
        )
        self.todo_db_path = todo_db_path
        _bootstrap(self.sqlite_path)
        self._ledger: Ledger | None = None
        self._mailbox: Mailbox | None = None
        self._planner_kv: PlannerKV | None = None
        self._todo_cache: TodoCacheReader | None = None

    def ledger(self) -> Ledger:
        if self._ledger is None:
            self._ledger = SqliteLedger(self.sqlite_path)
        return self._ledger

    def mailbox(self) -> Mailbox:
        if self._mailbox is None:
            self._mailbox = SqliteMailbox(self.sqlite_path)
        return self._mailbox

    def planner_kv(self) -> PlannerKV:
        if self._planner_kv is None:
            self._planner_kv = SqlitePlannerKV(self.sqlite_path)
        return self._planner_kv

    def todo_cache(self) -> TodoCacheReader:
        if self._todo_cache is None:
            db = self.todo_db_path or self.vault_root / "Todo" / "todo.db"
            self._todo_cache = GitTodoCacheReader(db)
        return self._todo_cache
