"""Hybrid retrieval tests for ``todo_system.graph_memory`` (memory v2 · G3).

Covers the third additive leaf of the graph-memory v2 design
(``docs/design/graph-memory.md`` §4.2 G3): the public ``query_related`` hybrid
query that blends a **semantic** embedding-KNN layer (over the G2 ``vec_embeddings``
vec0 table) with a **structural** ``get_related``-style BFS (over the G1 bi-temporal
``edges`` table), returning a ranked list where every item carries its provenance
(``source_file`` / ``run_id`` / ``commit_sha``) and its score composition (separate
``vector`` and ``graph`` components).

The four behaviours pinned here map one-to-one to the task's acceptance criteria:

* **Fallback** (AC#1) — with the embedding layer unavailable (sqlite-vec gone at
  query time, *or* the vault was never embedded so the vec tables are absent),
  ``query_related`` degrades to BFS-only from the task slug and never raises.
* **Provenance + score composition** (AC#2) — a structurally-reached item carries
  its connecting edge's ``run_id`` / ``commit_sha`` / ``source_file`` and a non-zero
  ``graph`` score; a pure-semantic item carries ``run_id = commit_sha = None`` but a
  ``source_file`` and a non-zero ``vector`` score; ``combined`` is the documented
  weighted blend of the two components.
* **as_of** (AC#3) — the time-axis argument narrows the structural traversal to the
  edges valid at that instant (``recorded_at <= as_of AND (valid_until IS NULL OR
  valid_until > as_of)``); the current-valid default (``as_of=None``) matches
  ``get_related``.
* **Preservation** — ``get_related`` itself is untouched: a shape-specific guard on
  its real edge tuples fails if anyone edits its SELECT or signature.

Isolation mirrors the G1/G2 files: library-level cases drive the functions against
an ``isolated_vault`` with an explicit ``con=``. Fixtures use only fake slugs,
relative paths and placeholder provenance (public repo — no real
name/token/host/abs-home path).
"""

from __future__ import annotations

import pytest

from todo_system import graph_memory as gm

# Placeholder provenance — fake values, never a real identifier (G1 convention).
FAKE_RUN_ID = "20260806T000000-fake-run"
FAKE_COMMIT_SHA = "0" * 40


def _onehot_embedder(text: str) -> list[float]:
    """A tiny fake embedder: a one-hot 3-vector keyed by a fruit keyword, so the
    nearest-neighbour answer for a crafted query is predictable (G2 idiom)."""
    t = text.lower()
    if "apple" in t:
        return [1.0, 0.0, 0.0]
    if "banana" in t:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


def _insert_edge(
    con,
    from_slug: str,
    to_slug: str,
    *,
    edge_type: str = "depends_on",
    source_file: str | None = None,
    recorded_at: str = "2026-08-06T00:00:00",
    valid_until: str | None = None,
    run_id: str | None = None,
    commit_sha: str | None = None,
    event_date: str | None = None,
) -> None:
    """Insert one explicit edge with chosen provenance + time columns, so the
    as_of / provenance cases can pin the exact row the BFS should discover."""
    gm.insert_edges(
        con,
        [
            {
                "from_slug": from_slug,
                "to_slug": to_slug,
                "edge_type": edge_type,
                "source_file": source_file or f"Todo/items/{from_slug}.md",
                "event_date": event_date,
                "recorded_at": recorded_at,
                "valid_until": valid_until,
                "run_id": run_id,
                "commit_sha": commit_sha,
            }
        ],
    )


# ---------------------------------------------------------------------------
# AC#1 — fallback to BFS-only when the embedding layer is unavailable
# ---------------------------------------------------------------------------


def test_query_related_falls_back_to_bfs_when_vec_tables_absent(isolated_vault):
    """Most common real case: sqlite-vec was never installed, so the vault was
    never embedded and the ``vec_embeddings`` / ``vec_sources`` tables do not
    exist. ``query_related`` must return the structural neighbour BFS-only — no
    exception, ``vector`` component hard-zero — without touching sqlite-vec."""
    con = isolated_vault.connect()
    try:
        _insert_edge(con, "task-main", "struct-neighbor", edge_type="depends_on")
        assert not gm._table_exists(con, "vec_embeddings")

        items = gm.query_related(con, {"slug": "task-main", "title": "no fruit here"})

        assert {it.slug for it in items} == {"struct-neighbor"}
        neighbor = items[0]
        assert neighbor.score.vector == 0.0
        assert neighbor.score.graph > 0.0
        assert neighbor.hops == 1
        assert neighbor.edge_type == "depends_on"
    finally:
        con.close()


def test_query_related_falls_back_to_bfs_when_embeddings_unavailable(
    isolated_vault, monkeypatch
):
    """The vec tables exist (the vault was embedded once) but the extension can no
    longer be loaded at query time. ``_load_sqlite_vec`` raising
    ``EmbeddingsUnavailable`` is caught and the query degrades to BFS-only — the
    same graceful contract ``embed_vault`` already honours (G2 monkeypatch idiom)."""
    pytest.importorskip("sqlite_vec")
    isolated_vault.write("Notes/some-doc.md", "some ordinary content\n")
    con = isolated_vault.connect()
    try:
        gm.embed_vault(
            con, vault_root=isolated_vault.root, embedder=_onehot_embedder, dim=3
        )
        assert gm._table_exists(con, "vec_embeddings")
        _insert_edge(con, "task-main", "struct-neighbor", edge_type="depends_on")

        def _raise(_con):
            raise gm.EmbeddingsUnavailable("sqlite-vec gone at query time (simulated)")

        monkeypatch.setattr(gm, "_load_sqlite_vec", _raise)

        items = gm.query_related(
            con,
            {"slug": "task-main", "title": "anything"},
            embedder=_onehot_embedder,
            dim=3,
        )

        assert "struct-neighbor" in {it.slug for it in items}
        neighbor = next(it for it in items if it.slug == "struct-neighbor")
        assert neighbor.score.vector == 0.0
        assert neighbor.score.graph > 0.0
        # the vector layer degraded cleanly: no item carries a semantic score
        assert all(it.score.vector == 0.0 for it in items)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# AC#2 — every item carries provenance + both score components
# ---------------------------------------------------------------------------


def test_items_carry_provenance_and_both_score_components(isolated_vault):
    """A structurally-reached item exposes its connecting edge's provenance and a
    non-zero graph score; a pure-semantic item exposes ``run_id = commit_sha =
    None`` with a ``source_file`` and a non-zero vector score; ``combined`` is the
    documented 0.5/0.5 weighted blend of the two separate components."""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    assert sqlite_vec is not None
    isolated_vault.write("Notes/apple-doc.md", "apple orchard harvest season\n")
    con = isolated_vault.connect()
    try:
        gm.embed_vault(
            con, vault_root=isolated_vault.root, embedder=_onehot_embedder, dim=3
        )
        _insert_edge(
            con,
            "task-main",
            "struct-neighbor",
            edge_type="depends_on",
            source_file="Todo/items/task-main.md",
            run_id=FAKE_RUN_ID,
            commit_sha=FAKE_COMMIT_SHA,
        )

        items = gm.query_related(
            con,
            {"slug": "task-main", "title": "apple harvest planning"},
            embedder=_onehot_embedder,
            dim=3,
        )
        by_slug = {it.slug for it in items}
        assert {"struct-neighbor", "apple-doc"} <= by_slug

        struct = next(it for it in items if it.slug == "struct-neighbor")
        assert struct.run_id == FAKE_RUN_ID
        assert struct.commit_sha == FAKE_COMMIT_SHA
        assert struct.source_file == "Todo/items/task-main.md"
        assert struct.score.graph > 0.0
        assert struct.score.vector == 0.0
        assert struct.edge_type == "depends_on"
        assert struct.hops == 1

        sem = next(it for it in items if it.slug == "apple-doc")
        assert sem.run_id is None
        assert sem.commit_sha is None
        assert sem.source_file == "Notes/apple-doc.md"
        assert sem.edge_type is None
        assert sem.hops is None
        assert sem.score.vector > 0.0
        assert sem.score.graph == 0.0
        assert sem.score.combined == pytest.approx(
            0.5 * sem.score.vector + 0.5 * sem.score.graph
        )
    finally:
        con.close()


# ---------------------------------------------------------------------------
# AC#3 — as_of narrows the structural traversal to the valid window
# ---------------------------------------------------------------------------


def test_as_of_restricts_graph_traversal_to_the_valid_window(isolated_vault):
    """BFS-only (no vec tables) so the time filter is isolated. Edge X is valid in
    [T1, T2); edge Y is recorded only at T3 > T2 and still live. A query as of a
    moment inside X's window sees X only (Y not yet recorded); the current-valid
    default (as_of=None) sees Y only (X tombstoned). Pins the window predicate
    ``recorded_at <= as_of AND (valid_until IS NULL OR valid_until > as_of)``."""
    con = isolated_vault.connect()
    try:
        t1, t2, t3 = (
            "2026-01-01T00:00:00",
            "2026-02-01T00:00:00",
            "2026-03-01T00:00:00",
        )
        as_of_mid = "2026-01-15T00:00:00"
        _insert_edge(
            con, "task-main", "old-neighbor", recorded_at=t1, valid_until=t2
        )
        _insert_edge(
            con, "task-main", "new-neighbor", recorded_at=t3, valid_until=None
        )

        past = gm.query_related(con, {"slug": "task-main"}, as_of=as_of_mid)
        assert {it.slug for it in past} == {"old-neighbor"}

        now = gm.query_related(con, {"slug": "task-main"})
        assert {it.slug for it in now} == {"new-neighbor"}
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Preservation — get_related is byte-for-byte unchanged (parallel change)
# ---------------------------------------------------------------------------


def test_get_related_is_unchanged_and_returns_real_edge_tuples(isolated_vault):
    """Shape-specific guard (not merely "returns a list"): ``get_related`` yields a
    dict with exactly its six SELECT columns and the real ``(a, x, wikilink)`` edge,
    so it fails loudly if anyone touches ``get_related``'s SELECT or signature while
    adding the parallel ``query_related``."""
    isolated_vault.write("Notes/a.md", "[[x]]\n")
    con = isolated_vault.connect()
    try:
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
        rows = gm.get_related(con, "a")

        assert len(rows) == 1
        row = rows[0]
        assert set(row.keys()) == {
            "from_slug",
            "to_slug",
            "edge_type",
            "source_file",
            "event_date",
            "recorded_at",
        }
        assert (row["from_slug"], row["to_slug"], row["edge_type"]) == (
            "a",
            "x",
            "wikilink",
        )
        assert row["source_file"] == "Notes/a.md"
    finally:
        con.close()
