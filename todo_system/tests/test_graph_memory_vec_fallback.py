"""Pure-Python vector fallback tests for ``todo_system.graph_memory`` (#147).

When the optional sqlite-vec extension cannot be loaded (no wheel for the
platform — e.g. Windows ARM64 + a brand-new CPython — or a Python built without
loadable-extension support), ``embed_vault`` / ``query_related`` used to skip the
semantic layer silently. They now fall back to a stdlib-only backend: a plain
SQLite table (``vec_embeddings_py``) holding ``array('f')`` float32 BLOBs, scanned
exhaustively at query time. This file pins that contract:

* embed + query work through the fallback, with the same result shapes;
* the top-k slugs, order and scores match the sqlite-vec path on the same input
  (compared for real when sqlite-vec is installed; fallback-only otherwise);
* content-hash incrementality holds (an unchanged doc is never re-embedded);
* the fallback is announced exactly once per process, on **stderr** (stdout is
  parsed by callers — the #145 reason);
* a dim change mirrors the vec0 contract (refused without ``--rebuild``, query
  degrades to BFS-only);
* the sqlite-vec path, its tables and schema stay exactly as before, and the
  fallback never reads or writes them.

The extension is blocked the honest way — ``sys.modules["sqlite_vec"] = None`` via
``monkeypatch`` — so the real ``_load_sqlite_vec`` raises ``EmbeddingsUnavailable``.
Isolation: ``isolated_vault`` + explicit ``con``. Fixtures use only fake slugs and
relative paths (public repo).
"""

from __future__ import annotations

import importlib.util
import math
import sys

import pytest

from todo_system import graph_memory as gm

PY_TABLE = "vec_embeddings_py"
NOTICE = "pure-Python vector fallback"


@pytest.fixture(autouse=True)
def _reset_notice(monkeypatch):
    """The once-per-process notice flag is module state — reset it per test."""
    monkeypatch.setattr(gm, "_VEC_FALLBACK_NOTICED", False, raising=False)


def _block_sqlite_vec(monkeypatch) -> None:
    """Make ``importlib.import_module("sqlite_vec")`` raise, so the real
    ``_load_sqlite_vec`` raises ``EmbeddingsUnavailable`` (auto-undone)."""
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)


def _have_sqlite_vec() -> bool:
    return importlib.util.find_spec("sqlite_vec") is not None


# --- fake embedders ---------------------------------------------------------


def _onehot_embedder(text: str) -> list[float]:
    t = text.lower()
    if "apple" in t:
        return [1.0, 0.0, 0.0]
    if "banana" in t:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


def _onehot16(text: str) -> list[float]:
    t = text.lower()
    v = [0.0] * 16
    if "apple" in t:
        v[0] = 1.0
    elif "banana" in t:
        v[1] = 1.0
    else:
        v[2] = 1.0
    return v


_DOCS = [f"doc{i}" for i in range(6)]


def _graded_vector(i: int) -> list[float]:
    return [1.0, 0.2 * i, 0.05 * i * i, 0.3]


_QUERY_VECTOR = [1.0, 0.45, 0.1, 0.3]


def _graded_embedder(text: str) -> list[float]:
    """Dim-4, deliberately **not** normalised, with pairwise-distinct distances
    from the query vector so the ranking is defined in both backends."""
    t = text.lower()
    if "query" in t:
        return list(_QUERY_VECTOR)
    for i, name in enumerate(_DOCS):
        if name in t:
            return _graded_vector(i)
    return [0.0, 0.0, 0.0, 1.0]


def _counting(embed_fn, calls: list[str]):
    def fn(text: str):
        calls.append(text)
        return embed_fn(text)

    return fn


def _py_rows(con) -> dict[str, tuple[str, int, bytes]]:
    return {
        row["source_file"]: (row["content_hash"], row["dim"], bytes(row["embedding"]))
        for row in con.execute(
            f"SELECT source_file, content_hash, dim, embedding FROM {PY_TABLE}"
        )
    }


def _write_fruit(vault) -> None:
    vault.write("Notes/apple.md", "apple orchard harvest\n")
    vault.write("Notes/banana.md", "banana tree tropical\n")
    vault.write("Notes/cherry.md", "cherry blossom spring\n")


def _write_graded(vault) -> None:
    for name in _DOCS:
        vault.write(f"Notes/{name}.md", f"{name} body\n")


# --- embed + query through the fallback --------------------------------------


def test_embed_uses_python_fallback_when_sqlite_vec_absent(isolated_vault, monkeypatch):
    _write_fruit(isolated_vault)
    _block_sqlite_vec(monkeypatch)
    con = isolated_vault.connect()
    try:
        result = gm.embed_vault(
            con, vault_root=isolated_vault.root, embedder=_onehot_embedder, dim=3
        )
        assert result.skipped_reason is None
        assert result.embedded == 3
        assert result.dim == 3
        assert gm._table_exists(con, PY_TABLE)
        rows = _py_rows(con)
        assert set(rows) == {"Notes/apple.md", "Notes/banana.md", "Notes/cherry.md"}
        assert all(dim == 3 and len(blob) == 12 for _h, dim, blob in rows.values())
        # the fallback never creates the sqlite-vec tables
        assert not gm._table_exists(con, "vec_embeddings")
        assert not gm._table_exists(con, "vec_sources")
    finally:
        con.close()


def test_query_related_uses_python_fallback(isolated_vault, monkeypatch):
    _write_fruit(isolated_vault)
    _block_sqlite_vec(monkeypatch)
    con = isolated_vault.connect()
    try:
        gm.embed_vault(con, vault_root=isolated_vault.root, embedder=_onehot_embedder, dim=3)
        items = gm.query_related(
            con, {"slug": "q", "title": "apple harvest"}, k=5, embedder=_onehot_embedder, dim=3
        )
        by_slug = {it.slug: it for it in items}
        apple = by_slug["apple"]
        assert apple.score.vector == pytest.approx(1.0)  # distance 0
        assert by_slug["banana"].score.vector == pytest.approx(1.0 / (1.0 + math.sqrt(2.0)))
        # pure-semantic provenance, same shape as the vec0 path
        assert apple.source_file == "Notes/apple.md"
        assert apple.run_id is None
        assert apple.commit_sha is None
        assert apple.edge_type is None
        assert apple.hops is None
        assert items[0].slug == "apple"
    finally:
        con.close()


def test_fallback_topk_matches_sqlite_vec(isolated_vault, monkeypatch):
    """Same input → the same top-k slugs, in the same order, with the same scores,
    from both backends. With sqlite-vec absent, the fallback alone is checked
    against hand-computed L2 distances."""
    _write_graded(isolated_vault)
    q = _QUERY_VECTOR
    dists = {
        name: math.sqrt(sum((a - b) ** 2 for a, b in zip(_graded_vector(i), q)))
        for i, name in enumerate(_DOCS)
    }
    assert len(set(round(d, 9) for d in dists.values())) == len(dists)  # no ties
    expected_order = ["doc2", "doc1", "doc3", "doc0"]
    assert sorted(dists, key=dists.get)[:4] == expected_order

    task = {"slug": "q", "title": "query"}
    con = isolated_vault.connect()
    try:
        ref = None
        ref_items = None
        if _have_sqlite_vec():
            r = gm.embed_vault(
                con, vault_root=isolated_vault.root, embedder=_graded_embedder, dim=4
            )
            assert r.skipped_reason is None and r.embedded == 6
            ref = gm._knn_seeds(con, "query", k=4, embedder=_graded_embedder, dim=4)
            ref_items = gm.query_related(con, task, k=4, embedder=_graded_embedder, dim=4)

        with monkeypatch.context() as mp:
            _block_sqlite_vec(mp)
            r = gm.embed_vault(
                con, vault_root=isolated_vault.root, embedder=_graded_embedder, dim=4
            )
            assert r.skipped_reason is None and r.embedded == 6
            got = gm._knn_seeds(con, "query", k=4, embedder=_graded_embedder, dim=4)
            got_items = gm.query_related(con, task, k=4, embedder=_graded_embedder, dim=4)

        assert list(got) == expected_order
        for slug in expected_order:
            assert got[slug]["vector"] == pytest.approx(1.0 / (1.0 + dists[slug]), rel=1e-5)
            assert got[slug]["source_file"] == f"Notes/{slug}.md"
        assert [it.slug for it in got_items] == expected_order

        if ref is not None:
            assert list(got) == list(ref)
            for slug in ref:
                assert got[slug]["vector"] == pytest.approx(ref[slug]["vector"], rel=1e-5)
                assert got[slug]["source_file"] == ref[slug]["source_file"]
            assert [it.slug for it in got_items] == [it.slug for it in ref_items]
    finally:
        con.close()


def test_fallback_incremental_reembeds_only_changed_docs(isolated_vault, monkeypatch):
    isolated_vault.write("Notes/apple.md", "apple version one\n")
    isolated_vault.write("Notes/banana.md", "banana version one\n")
    _block_sqlite_vec(monkeypatch)
    con = isolated_vault.connect()
    try:
        calls: list[str] = []
        fn = _counting(_onehot_embedder, calls)

        first = gm.embed_vault(con, vault_root=isolated_vault.root, embedder=fn, dim=3)
        assert first.embedded == 2
        assert len(calls) == 2

        calls.clear()
        second = gm.embed_vault(con, vault_root=isolated_vault.root, embedder=fn, dim=3)
        assert second.embedded == 0
        assert second.unchanged == 2
        assert calls == []

        before = _py_rows(con)
        isolated_vault.write("Notes/apple.md", "apple version two — edited\n")
        calls.clear()
        third = gm.embed_vault(con, vault_root=isolated_vault.root, embedder=fn, dim=3)
        assert third.embedded == 1
        assert third.unchanged == 1
        assert len(calls) == 1 and "apple version two" in calls[0]
        after = _py_rows(con)
        assert after["Notes/apple.md"][0] != before["Notes/apple.md"][0]
        assert after["Notes/banana.md"] == before["Notes/banana.md"]

        (isolated_vault.root / "Notes" / "banana.md").unlink()
        calls.clear()
        fourth = gm.embed_vault(con, vault_root=isolated_vault.root, embedder=fn, dim=3)
        assert fourth.removed == 1
        assert calls == []
        assert set(_py_rows(con)) == {"Notes/apple.md"}
    finally:
        con.close()


def test_fallback_notice_goes_to_stderr_once(isolated_vault, monkeypatch, capsys):
    _write_fruit(isolated_vault)
    _block_sqlite_vec(monkeypatch)
    con = isolated_vault.connect()
    try:
        gm.embed_vault(con, vault_root=isolated_vault.root, embedder=_onehot_embedder, dim=3)
        gm.embed_vault(con, vault_root=isolated_vault.root, embedder=_onehot_embedder, dim=3)
        gm.query_related(
            con, {"slug": "q", "title": "apple"}, k=3, embedder=_onehot_embedder, dim=3
        )
        captured = capsys.readouterr()
        assert captured.err.count(NOTICE) == 1
        assert NOTICE not in captured.out
    finally:
        con.close()


def test_fallback_dim_change_refused_without_rebuild_then_rebuilt(isolated_vault, monkeypatch):
    _write_fruit(isolated_vault)
    _block_sqlite_vec(monkeypatch)
    con = isolated_vault.connect()
    try:
        r1 = gm.embed_vault(con, vault_root=isolated_vault.root, embedder=_onehot_embedder, dim=3)
        assert r1.embedded == 3
        before = _py_rows(con)

        r2 = gm.embed_vault(con, vault_root=isolated_vault.root, embedder=_onehot16, dim=16)
        assert r2.skipped_reason is not None
        assert "--rebuild" in r2.skipped_reason
        assert _py_rows(con) == before  # untouched: 3 rows, all dim 3

        r3 = gm.embed_vault(
            con, vault_root=isolated_vault.root, embedder=_onehot16, dim=16, rebuild=True
        )
        assert r3.skipped_reason is None
        assert r3.embedded == 3
        rows = _py_rows(con)
        assert len(rows) == 3
        assert {dim for _h, dim, _b in rows.values()} == {16}

        seeds = gm._knn_seeds(con, "apple orchard", k=1, embedder=_onehot16, dim=16)
        assert list(seeds) == ["apple"]
    finally:
        con.close()


def test_fallback_query_dim_mismatch_degrades_to_bfs(isolated_vault, monkeypatch, capsys):
    isolated_vault.write("Notes/apple.md", "apple orchard\n")
    _block_sqlite_vec(monkeypatch)
    con = isolated_vault.connect()
    try:
        gm.embed_vault(con, vault_root=isolated_vault.root, embedder=_onehot_embedder, dim=3)
        assert gm._table_exists(con, PY_TABLE)
        gm.insert_edges(
            con,
            [
                {
                    "from_slug": "q",
                    "to_slug": "struct-neighbor",
                    "edge_type": "depends_on",
                    "source_file": "Todo/items/q.md",
                    "event_date": None,
                    "recorded_at": "2026-11-09T00:00:00",
                    "valid_until": None,
                }
            ],
        )
        capsys.readouterr()

        items = gm.query_related(
            con, {"slug": "q", "title": "apple"}, k=5, embedder=_onehot16, dim=16
        )
        assert items
        assert all(it.score.vector == 0.0 for it in items)
        assert "dim" in capsys.readouterr().out.lower()
    finally:
        con.close()


# --- the sqlite-vec path is unchanged ----------------------------------------


def test_sqlite_vec_path_is_unchanged(isolated_vault, capsys):
    """Preservation guard: with sqlite-vec loadable, embed builds exactly the vec0
    tables (no fallback table, no notice)."""
    pytest.importorskip("sqlite_vec")
    _write_fruit(isolated_vault)
    con = isolated_vault.connect()
    try:
        result = gm.embed_vault(
            con, vault_root=isolated_vault.root, embedder=_onehot_embedder, dim=3
        )
        assert result.skipped_reason is None
        assert result.embedded == 3
        assert gm._table_exists(con, "vec_sources")
        assert gm._table_exists(con, "vec_embeddings")
        assert not gm._table_exists(con, PY_TABLE)
        sql = con.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'vec_embeddings'"
        ).fetchone()[0]
        assert "vec0(embedding float[3])" in sql
        seeds = gm._knn_seeds(con, "apple", k=1, embedder=_onehot_embedder, dim=3)
        assert list(seeds) == ["apple"]
        captured = capsys.readouterr()
        assert NOTICE not in captured.err
        assert "into vec0[3]" in captured.out
    finally:
        con.close()


def test_fallback_never_touches_vec0_tables(isolated_vault, monkeypatch):
    pytest.importorskip("sqlite_vec")
    _write_fruit(isolated_vault)
    con = isolated_vault.connect()
    try:
        gm.embed_vault(con, vault_root=isolated_vault.root, embedder=_onehot_embedder, dim=3)

        def _snapshot():
            sources = [
                tuple(r)
                for r in con.execute(
                    "SELECT rowid, source_file, content_hash, dim, embedded_at "
                    "FROM vec_sources ORDER BY rowid"
                )
            ]
            vecs = [
                (r[0], bytes(r[1]))
                for r in con.execute("SELECT rowid, embedding FROM vec_embeddings ORDER BY rowid")
            ]
            return sources, vecs

        before = _snapshot()

        with monkeypatch.context() as mp:
            _block_sqlite_vec(mp)
            r = gm.embed_vault(
                con, vault_root=isolated_vault.root, embedder=_onehot_embedder, dim=3
            )
            assert r.skipped_reason is None and r.embedded == 3
            isolated_vault.write("Notes/apple.md", "apple orchard — edited\n")
            r = gm.embed_vault(
                con, vault_root=isolated_vault.root, embedder=_onehot_embedder, dim=3
            )
            assert r.embedded == 1
            assert gm._table_exists(con, PY_TABLE)

        assert _snapshot() == before
    finally:
        con.close()
