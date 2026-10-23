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

from .assignment import Assignment, Ledger
from .message import now_iso
from .state_store import PlannerKV

__all__ = [
    "BUSY_TIMEOUT_MS",
    "SCHEMA_VERSION",
    "SqliteLedger",
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
