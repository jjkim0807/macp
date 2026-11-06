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


# ---------------------------------------------------------------------------
# Phase 2 — incremental tombstone sync + current-valid query invariance
# ---------------------------------------------------------------------------


def test_disappeared_relation_is_tombstoned_not_deleted(isolated_vault):
    """A relation whose source link is gone has ``valid_until`` stamped in place.

    The row is not ``DELETE``\\ d: it survives with its original id and
    ``recorded_at``, so the bi-temporal history is intact while nothing is live.
    """
    isolated_vault.write("Notes/a.md", "[[x]]\n")
    con = isolated_vault.connect()
    try:
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
        live = _live(con)
        assert len(live) == 1
        original_id = live[0]["id"]
        original_recorded = live[0]["recorded_at"]

        (isolated_vault.root / "Notes" / "a.md").unlink()
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)

        assert _live(con) == []  # nothing current-valid
        history = _all(con)
        assert len(history) == 1  # survived — tombstoned, not deleted
        assert history[0]["id"] == original_id
        assert history[0]["valid_until"] is not None
        assert history[0]["recorded_at"] == original_recorded
    finally:
        con.close()


def test_reappeared_relation_inserts_new_live_row_keeping_tombstone(isolated_vault):
    """A relation that comes back becomes a brand-new live row.

    The earlier tombstone is left untouched; the revived edge is a fresh insert
    (strictly higher id), never a flip of the dead row back to live.
    """
    isolated_vault.write("Notes/a.md", "[[x]]\n")
    con = isolated_vault.connect()
    try:
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
        (isolated_vault.root / "Notes" / "a.md").unlink()
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
        assert _live(con) == []
        tomb = _all(con)
        assert len(tomb) == 1 and tomb[0]["valid_until"] is not None
        tomb_id = tomb[0]["id"]

        # The same relation reappears.
        isolated_vault.write("Notes/a.md", "[[x]]\n")
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)

        rows = _all(con)
        assert len(rows) == 2  # tombstone kept + a new live row
        live = _live(con)
        assert len(live) == 1
        assert live[0]["valid_until"] is None
        assert live[0]["id"] > tomb_id  # a genuinely new insert
        kept = next(r for r in rows if r["id"] == tomb_id)
        assert kept["valid_until"] is not None  # old tombstone untouched
    finally:
        con.close()


def test_idempotent_rescan_preserves_recorded_at_and_row_identity(isolated_vault):
    """Re-scanning an unchanged vault changes nothing — no churn of history.

    Identity is matched without ``recorded_at``, so an identical row is left as
    is: same id, same ``recorded_at``, still live, no tombstone created.
    """
    isolated_vault.write("Notes/a.md", "[[x]] [[y]]\n")
    con = isolated_vault.connect()
    try:
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
        before = _all(con)
        assert len(before) == 2
        for _ in range(3):
            gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
        after = _all(con)
        assert len(after) == 2  # no reinsert, no tombstone
        assert [r["id"] for r in after] == [r["id"] for r in before]
        assert [r["recorded_at"] for r in after] == [r["recorded_at"] for r in before]
        assert all(r["valid_until"] is None for r in after)
    finally:
        con.close()


def test_duplicate_body_link_occurrences_are_tracked_by_multiplicity(isolated_vault):
    """Two identical ``[[x]]`` links in one body are two live edges, stably.

    Signature matching is by multiplicity (``Counter``), so a byte-identical
    rescan keeps exactly two — never collapsing them, never accumulating a third.
    """
    isolated_vault.write("Notes/a.md", "[[x]] and again [[x]]\n")
    con = isolated_vault.connect()
    try:
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
        assert len(_live(con)) == 2
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
        assert len(_live(con)) == 2  # stable, no accumulation
        assert len(_all(con)) == 2  # and no tombstone churn

        # Drop to a single occurrence: exactly one is tombstoned.
        isolated_vault.write("Notes/a.md", "[[x]] once\n")
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
        assert len(_live(con)) == 1
        assert sum(1 for r in _all(con) if r["valid_until"] is not None) == 1
    finally:
        con.close()


def test_event_date_change_tombstones_old_signature_and_inserts_new(isolated_vault):
    """A changed ``event_date`` is a new signature: old row tombstoned, new live."""
    isolated_vault.write("Notes/a.md", "---\nevent_date: 2026-08-01\n---\n\n[[x]]\n")
    con = isolated_vault.connect()
    try:
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
        assert [r["event_date"] for r in _live(con)] == ["2026-08-01"]

        isolated_vault.write("Notes/a.md", "---\nevent_date: 2026-09-01\n---\n\n[[x]]\n")
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)

        live = _live(con)
        assert len(live) == 1
        assert live[0]["event_date"] == "2026-09-01"
        allrows = _all(con)
        assert len(allrows) == 2
        old = [r for r in allrows if r["event_date"] == "2026-08-01"]
        assert len(old) == 1 and old[0]["valid_until"] is not None
    finally:
        con.close()


def test_confidence_change_tombstones_old_signature_and_inserts_new(isolated_vault):
    """``confidence`` is part of the signature too — a change rolls the row over."""
    isolated_vault.write("Notes/a.md", "---\nconfidence: 0.5\n---\n\n[[x]]\n")
    con = isolated_vault.connect()
    try:
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
        assert [r["confidence"] for r in _live(con)] == [0.5]

        isolated_vault.write("Notes/a.md", "---\nconfidence: 0.9\n---\n\n[[x]]\n")
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)

        live = _live(con)
        assert len(live) == 1 and live[0]["confidence"] == 0.9
        allrows = _all(con)
        assert len(allrows) == 2
        old = [r for r in allrows if r["confidence"] == 0.5]
        assert len(old) == 1 and old[0]["valid_until"] is not None
    finally:
        con.close()


def test_current_valid_queries_exclude_tombstoned_edges(isolated_vault):
    """``get_related`` / ``most_referenced`` / index aggregates skip tombstones."""
    isolated_vault.write("Notes/a.md", "[[x]] [[y]]\n")
    con = isolated_vault.connect()
    try:
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
        isolated_vault.write("Notes/a.md", "[[x]]\n")  # y disappears
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)

        related = sorted((r["from_slug"], r["to_slug"]) for r in gm.get_related(con, "a"))
        assert related == [("a", "x")]
        assert gm.get_related(con, "y") == []  # tombstoned target unreachable
        assert gm.most_referenced_slugs(con) == [("x", 1)]

        md = gm.render_index_markdown(con, vault_root=isolated_vault.root)
        assert "> total edges: 1\n" in md
        assert "- `wikilink`: 1\n" in md
    finally:
        con.close()


def test_get_related_current_valid_matches_a_fresh_rebuild(isolated_vault, tmp_path):
    """AC#3 invariance: the incremental path's live result == a full rebuild's.

    After an edge is tombstoned, ``get_related`` on the incremental DB returns
    exactly what a from-scratch scan of the same final vault state produces —
    byte-for-byte — while the tombstoned edge still lives in history.
    """
    isolated_vault.write("Notes/a.md", "[[x]] [[y]]\n")
    con = isolated_vault.connect()
    try:
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
        isolated_vault.write("Notes/a.md", "[[x]]\n")  # y disappears -> tombstone
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
        incr = sorted((r["from_slug"], r["to_slug"]) for r in gm.get_related(con, "a"))

        # Full rebuild of the SAME final vault state into a separate, clean DB.
        fresh = gm.init_relations_db(tmp_path / "fresh" / "relations.sqlite")
        try:
            gm.scan_vault_and_index(vault_root=isolated_vault.root, con=fresh)
            rebuilt = sorted(
                (r["from_slug"], r["to_slug"]) for r in gm.get_related(fresh, "a")
            )
        finally:
            fresh.close()

        assert incr == rebuilt == [("a", "x")]
        # The tombstoned y edge is still recoverable from history on the incr DB.
        assert any(
            r["to_slug"] == "y" and r["valid_until"] is not None for r in _all(con)
        )
    finally:
        con.close()


def test_history_query_over_all_rows_returns_full_timeline(isolated_vault):
    """Querying all rows (incl. tombstoned) returns the full appear/vanish/return
    timeline, while current-valid queries collapse to the single live edge."""
    isolated_vault.write("Notes/a.md", "[[x]]\n")
    con = isolated_vault.connect()
    try:
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
        (isolated_vault.root / "Notes" / "a.md").unlink()
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
        isolated_vault.write("Notes/a.md", "[[x]]\n")
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)

        ax = [r for r in _all(con) if (r["from_slug"], r["to_slug"]) == ("a", "x")]
        assert len(ax) == 2  # the full timeline: one dead, one alive
        assert sum(1 for r in ax if r["valid_until"] is None) == 1
        assert sum(1 for r in ax if r["valid_until"] is not None) == 1
        assert len(gm.get_related(con, "a")) == 1  # current-valid sees one
    finally:
        con.close()
