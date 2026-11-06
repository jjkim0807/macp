"""Bi-temporal + provenance tests for ``todo_system.graph_memory`` (memory v2 · G1).

Covers the first additive leaf of the graph-memory v2 design
(``docs/design/graph-memory.md`` §4.2 G1):

* **Provenance** — ``edges`` gains nullable ``run_id`` / ``commit_sha`` columns,
  added to a pre-G1 DB with an in-place ``ALTER TABLE ADD COLUMN`` so existing
  rows survive losslessly (provenance = NULL). (Phase 1, below.)
* **Tombstoning** — a vault rescan no longer ``DELETE``\\ s every edge and
  reinserts; a relation that disappears has ``valid_until`` stamped instead, and
  one that reappears becomes a *new* live row while the tombstone persists, so
  ``recorded_at`` and history are never overwritten. (Phase 2, below.)
* **Current-valid invariance** — ``get_related`` / ``most_referenced_slugs`` /
  the index aggregates read only live (``valid_until IS NULL``) edges, so their
  results match a from-scratch full rebuild of the same vault state. (Phase 2.)

Isolation: the migration/provenance cases drive ``init_relations_db`` /
``insert_edges`` directly against a ``tmp_path`` DB (no ``isolated_vault``, which
monkeypatches ``DB_PATH`` and is only needed when a function reads a module
global). The scan cases take ``isolated_vault`` and pass an explicit ``con=`` —
the habit that keeps a scan off the real ``Memory/relations.sqlite``. Fixtures
use only fake slugs, relative paths and placeholder provenance values (public
repo — no real name/token/host/abs-home path).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from todo_system import graph_memory as gm

# A placeholder run_id / commit sha — fake values, never a real identifier.
FAKE_RUN_ID = "20260806T000000-fake-run"
FAKE_COMMIT_SHA = "0" * 40


def _all(con: sqlite3.Connection) -> list[dict]:
    """Every edge row (live + tombstoned), id-ordered — the history view."""
    return [dict(r) for r in con.execute("SELECT * FROM edges ORDER BY id")]


def _live(con: sqlite3.Connection) -> list[dict]:
    """Only currently-valid edges (``valid_until IS NULL``), id-ordered."""
    return [
        dict(r)
        for r in con.execute(
            "SELECT * FROM edges WHERE valid_until IS NULL ORDER BY id"
        )
    ]


def _build_pre_g1_db(db_path: Path, recorded_at: str = "2026-08-06T00:00:00") -> None:
    """Hand-build a pre-G1 ``edges`` table (no provenance columns) + one row.

    Mirrors the 9-column schema as it stood before ``run_id`` / ``commit_sha``
    were added, so ``init_relations_db`` must migrate it in place.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    con.executescript(
        """
        CREATE TABLE edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_slug TEXT NOT NULL,
            to_slug TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            source_file TEXT NOT NULL,
            event_date TEXT,
            recorded_at TEXT NOT NULL,
            valid_until TEXT,
            confidence REAL
        );
        """
    )
    con.execute(
        """
        INSERT INTO edges (from_slug, to_slug, edge_type, source_file,
                           event_date, recorded_at, valid_until, confidence)
        VALUES ('a', 'b', 'wikilink', 'Notes/a.md', '2026-08-01', ?, NULL, NULL)
        """,
        (recorded_at,),
    )
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Phase 1 — additive schema: provenance columns + lossless migration
# ---------------------------------------------------------------------------


def test_migration_adds_nullable_provenance_columns_without_data_loss(tmp_path):
    """A pre-G1 DB is migrated in place: new columns appear, old row survives.

    The existing ``a -> b`` row keeps every field verbatim and reads back with
    ``run_id`` / ``commit_sha`` = NULL — the ``ALTER TABLE ADD COLUMN`` default.
    """
    db_path = tmp_path / "Memory" / "relations.sqlite"
    _build_pre_g1_db(db_path)

    con = gm.init_relations_db(db_path)
    try:
        cols = {row["name"] for row in con.execute("PRAGMA table_info(edges)")}
        assert {"run_id", "commit_sha"} <= cols

        rows = _all(con)
        assert len(rows) == 1  # lossless — the row was not dropped
        row = rows[0]
        assert row["from_slug"] == "a"
        assert row["to_slug"] == "b"
        assert row["edge_type"] == "wikilink"
        assert row["source_file"] == "Notes/a.md"
        assert row["event_date"] == "2026-08-01"
        assert row["recorded_at"] == "2026-08-06T00:00:00"
        assert row["valid_until"] is None
        assert row["run_id"] is None
        assert row["commit_sha"] is None
    finally:
        con.close()


def test_migration_is_idempotent_across_repeated_inits(tmp_path):
    """Re-opening an already-migrated DB adds nothing — one column each, once."""
    db_path = tmp_path / "Memory" / "relations.sqlite"
    _build_pre_g1_db(db_path)

    gm.init_relations_db(db_path).close()  # first open migrates
    con = gm.init_relations_db(db_path)  # second open is a no-op
    try:
        names = [row["name"] for row in con.execute("PRAGMA table_info(edges)")]
        assert names.count("run_id") == 1
        assert names.count("commit_sha") == 1
        # Trailing order: provenance columns are appended last.
        assert names[-2:] == ["run_id", "commit_sha"]
    finally:
        con.close()


def test_fresh_db_already_has_provenance_columns(tmp_path):
    """A brand-new DB is created with both columns; migration is a no-op there."""
    db_path = tmp_path / "Memory" / "relations.sqlite"
    con = gm.init_relations_db(db_path)
    try:
        names = [row["name"] for row in con.execute("PRAGMA table_info(edges)")]
        assert names.count("run_id") == 1
        assert names.count("commit_sha") == 1
        assert names[-2:] == ["run_id", "commit_sha"]
    finally:
        con.close()


def test_insert_edges_persists_provenance_and_defaults_to_null(tmp_path):
    """``insert_edges`` stores run_id/commit_sha when given, NULL when omitted."""
    db_path = tmp_path / "Memory" / "relations.sqlite"
    con = gm.init_relations_db(db_path)
    try:
        gm.insert_edges(
            con,
            [
                {
                    "from_slug": "a",
                    "to_slug": "x",
                    "edge_type": "wikilink",
                    "source_file": "Notes/a.md",
                    "recorded_at": "2026-08-06T00:00:00",
                    "run_id": FAKE_RUN_ID,
                    "commit_sha": FAKE_COMMIT_SHA,
                },
                {
                    "from_slug": "b",
                    "to_slug": "y",
                    "edge_type": "wikilink",
                    "source_file": "Notes/b.md",
                    "recorded_at": "2026-08-06T00:00:00",
                },
            ],
        )
        by_from = {r["from_slug"]: r for r in _all(con)}
        assert by_from["a"]["run_id"] == FAKE_RUN_ID
        assert by_from["a"]["commit_sha"] == FAKE_COMMIT_SHA
        assert by_from["b"]["run_id"] is None
        assert by_from["b"]["commit_sha"] is None
    finally:
        con.close()


def test_scan_path_leaves_provenance_null(isolated_vault):
    """The daemon/scan path does not populate provenance (G1 decision #3).

    ``edges_from_file`` never sets run_id/commit_sha and ``scan_vault_and_index``
    adds no git shell-out, so a plain rescan leaves both NULL. This also guards
    against a later leaf quietly wiring git HEAD into the scan (G2+ scope).
    """
    isolated_vault.write("Notes/a.md", "[[x]]\n")
    con = isolated_vault.connect()
    try:
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
        live = _live(con)
        assert len(live) == 1
        assert live[0]["run_id"] is None
        assert live[0]["commit_sha"] is None
    finally:
        con.close()
