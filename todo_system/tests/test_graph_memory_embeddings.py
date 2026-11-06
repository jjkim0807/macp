"""Embeddings + sqlite-vec vec0 tests for ``todo_system.graph_memory`` (memory v2 · G2).

Covers the second additive leaf of the graph-memory v2 design
(``docs/design/graph-memory.md`` §4.2 G2):

* **Local embedder seam** — a deterministic, zero-network default embedder
  (feature-hashing) behind an injectable ``Callable[[str], Sequence[float]]``
  seam; tests inject a tiny fake. (Phase 2.)
* **R5 boundary filter** — SMS / mail body / health-raw / OTP docs (by path
  segment or frontmatter tag) are never embedded, pinned by a RED test. (Phase 2.)
* **Incremental plan + dry-run** — vectors are a derived cache: a byte-identical
  summary is never re-embedded, an edited one is, a gone/newly-denied one is
  removed; ``embed --dry-run`` reports the plan with zero side effects. (Phase 3.)
* **sqlite-vec vec0** — the optional extension loads lazily, builds the ``vec0``
  virtual table, inserts/updates/deletes vectors by rowid, and a KNN query
  returns the nearest source; absent, embed skips with a clear message and the
  rest of graph_memory keeps working. (Phase 4.)

Isolation: the pure-function cases (embedder / filter / corpus) drive the
functions directly against a ``tmp_path`` or ``isolated_vault`` with explicit
``con=`` — the habit that keeps a scan/embed off the real
``Memory/relations.sqlite``. The CLI case uses a plain ``tmp_path`` +
``--vault-root`` (NOT ``isolated_vault``) so the "real vault untouched" guard
stays meaningful (conftest convention). Fixtures use only fake slugs, relative
paths and placeholder values (public repo — no real name/token/host/abs-home path).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from todo_system import graph_memory as gm


# ---------------------------------------------------------------------------
# Phase 2 — default embedder seam
# ---------------------------------------------------------------------------


def test_default_embedder_is_deterministic_normalized_and_right_length():
    """The default embedder returns a length-``EMBED_DIM``, L2-normalised vector,
    byte-identical across two calls on the same text (``hashlib``, not salted
    ``hash``) — the property that makes content-hash incrementality sound."""
    text = "worker stage retried after a stale injected FAIL note"
    v1 = gm._default_embedder(text)
    v2 = gm._default_embedder(text)

    assert len(v1) == gm.EMBED_DIM
    assert v1 == v2  # deterministic across calls
    norm = sum(x * x for x in v1) ** 0.5
    assert abs(norm - 1.0) < 1e-9  # L2-normalised
    assert all(isinstance(x, float) for x in v1)


def test_default_embedder_separates_unrelated_texts():
    """Different content yields a different vector (the hashing actually spreads),
    and an empty string is the zero vector (no tokens → no buckets)."""
    a = gm._default_embedder("alarm permission exact android 14")
    b = gm._default_embedder("robolectric nonuniform shape clip swallows touch")
    assert a != b

    zero = gm._default_embedder("")
    assert zero == [0.0] * gm.EMBED_DIM


def test_default_embedder_dim_is_configurable():
    """``dim=`` overrides the vector length without touching the seam — the
    fake-embedder tests rely on a small dim."""
    v = gm._default_embedder("some summary text", dim=8)
    assert len(v) == 8


def test_embed_input_text_is_nonempty_from_stem_alone():
    """Even a doc with no summary frontmatter and an empty body embeds its stem,
    so every included corpus doc has a non-empty summary."""
    text = gm._embed_input_text(Path("Notes/some-note.md"), {}, "")
    assert text.strip() == "some-note"

    rich = gm._embed_input_text(
        Path("Todo/items/a-task.md"),
        {"outcome": "close when X is green", "if_then": "do Y next"},
        "body line\n",
    )
    assert "a-task" in rich
    assert "close when X is green" in rich
    assert "do Y next" in rich
    assert "body line" in rich


# ---------------------------------------------------------------------------
# Phase 2 — R5 boundary filter (the RED test fixes the privacy boundary)
# ---------------------------------------------------------------------------


def test_is_embeddable_allows_ordinary_docs():
    """An ordinary note / task / entity with innocuous tags is embeddable."""
    assert gm.is_embeddable("Notes/a-plain-note.md", {"tags": ["note", "infra"]})
    assert gm.is_embeddable("Todo/items/a-task.md", {"tags": ["todo", "work"]})
    assert gm.is_embeddable("Memory/entities/some-concept.md", {})


def test_is_embeddable_denies_r5_path_segments():
    """R5: a doc under a denied path segment (sms/mail/inbox/health-raw/otp) is
    not embeddable — matched as a whole segment, not a substring."""
    assert not gm.is_embeddable("Memory/sms/code.md", {})
    assert not gm.is_embeddable("Notes/mail/thread.md", {})
    assert not gm.is_embeddable("Memory/health-raw/labs.md", {})
    assert not gm.is_embeddable("Notes/otp/one-time.md", {})
    # the filename stem itself counts as a segment
    assert not gm.is_embeddable("Notes/sms.md", {})
    # but a substring is NOT a segment — this stays embeddable
    assert gm.is_embeddable("Notes/smsidebar-design.md", {"tags": ["note"]})


def test_is_embeddable_denies_r5_tags():
    """R5: a doc carrying a denied frontmatter tag (sms/mail/health-raw/pii/otp)
    is not embeddable regardless of its path."""
    assert not gm.is_embeddable("Notes/innocent-looking.md", {"tags": ["note", "sms"]})
    assert not gm.is_embeddable("Notes/x.md", {"tags": ["pii"]})
    assert not gm.is_embeddable("Notes/y.md", {"tags": ["health-raw"]})
    assert not gm.is_embeddable("Notes/z.md", {"tags": "otp"})  # scalar tag form


def test_r5_denied_docs_are_absent_from_corpus(isolated_vault):
    """The RED boundary test: SMS / mail-body / health-raw / tag-denied docs are
    **excluded from the embed corpus**, while an ordinary note is included.

    This fails if the R5 filter is removed (``is_embeddable`` always True) — the
    denied docs would then appear in ``iter_embed_corpus`` — so it pins the
    privacy boundary (design §2 R5, CLAUDE.md §11/§11b).
    """
    isolated_vault.write("Notes/ordinary-note.md", "a normal fact worth remembering\n")
    isolated_vault.write("Notes/sms/verification.md", "123456 is your code\n")
    isolated_vault.write("Memory/health-raw/panel.md", "cholesterol 999\n")
    isolated_vault.write(
        "Notes/tagged-private.md",
        "---\ntags:\n  - note\n  - sms\n---\n\nsome body\n",
    )

    corpus = list(gm.iter_embed_corpus(isolated_vault.root))
    sources = {doc.source_file for doc in corpus}

    assert "Notes/ordinary-note.md" in sources
    assert "Notes/sms/verification.md" not in sources
    assert "Memory/health-raw/panel.md" not in sources
    assert "Notes/tagged-private.md" not in sources


def test_corpus_covers_scan_roots_with_nonempty_text(isolated_vault):
    """The corpus walk spans SCAN_ROOTS (task / note / entity / decision /
    episode) and derives a non-empty summary per included doc."""
    isolated_vault.write("Todo/items/a-task.md", "---\noutcome: close it\n---\n\nstep\n")
    isolated_vault.write("Notes/a-note.md", "note body\n")
    isolated_vault.write("Memory/entities/a-concept.md", "entity card\n")
    isolated_vault.write("Decisions/a-decision.md", "we decided X\n")
    isolated_vault.write("Memory/episodes/2026-11-06-a-run.md", "a retro\n")

    corpus = list(gm.iter_embed_corpus(isolated_vault.root))
    sources = {doc.source_file for doc in corpus}
    assert sources == {
        "Todo/items/a-task.md",
        "Notes/a-note.md",
        "Memory/entities/a-concept.md",
        "Decisions/a-decision.md",
        "Memory/episodes/2026-11-06-a-run.md",
    }
    assert all(doc.text.strip() for doc in corpus)


def test_corpus_skips_memory_sessions(isolated_vault):
    """Session journals live outside SCAN_ROOTS and are never in the corpus
    (defensive overlap with R5 — session logs can quote sensitive context)."""
    isolated_vault.write("Memory/sessions/2026-11-06-a-session.md", "manual work log\n")
    isolated_vault.write("Notes/kept.md", "keep me\n")

    sources = {doc.source_file for doc in gm.iter_embed_corpus(isolated_vault.root)}
    assert "Notes/kept.md" in sources
    assert not any(s.startswith("Memory/sessions/") for s in sources)


# ---------------------------------------------------------------------------
# Phase 3 — incremental plan + dry-run + CLI (plain table, no extension)
# ---------------------------------------------------------------------------


def _seed_vec_sources(con, corpus, *, dim=gm.EMBED_DIM):
    """Simulate a prior embed by filling the plain ``vec_sources`` table with the
    current corpus' content hashes — no sqlite-vec needed to test the diff."""
    gm.init_vec_sources(con)
    for doc in corpus:
        con.execute(
            "INSERT INTO vec_sources (source_file, content_hash, mtime, dim, embedded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (doc.source_file, gm._content_hash(doc.text), doc.mtime, dim, "2026-11-06T00:00:00"),
        )
    con.commit()


def test_plan_first_run_embeds_every_doc(isolated_vault):
    """First run (empty ``vec_sources``) → every corpus doc is to-embed."""
    isolated_vault.write("Notes/a.md", "alpha fact\n")
    isolated_vault.write("Notes/b.md", "beta fact\n")
    con = isolated_vault.connect()
    try:
        corpus = list(gm.iter_embed_corpus(isolated_vault.root))
        to_embed, unchanged, to_remove = gm.plan_embedding(con, corpus)
        assert {i.source_file for i in to_embed} == {"Notes/a.md", "Notes/b.md"}
        assert unchanged == []
        assert to_remove == []
        # plan is read-only: it must not have created vec_sources
        assert not gm._table_exists(con, "vec_sources")
    finally:
        con.close()


def test_plan_unchanged_rescan_embeds_nothing(isolated_vault):
    """A byte-identical rescan re-embeds nothing — vectors are a derived cache."""
    isolated_vault.write("Notes/a.md", "alpha fact\n")
    isolated_vault.write("Notes/b.md", "beta fact\n")
    con = isolated_vault.connect()
    try:
        corpus = list(gm.iter_embed_corpus(isolated_vault.root))
        _seed_vec_sources(con, corpus)
        to_embed, unchanged, to_remove = gm.plan_embedding(con, corpus)
        assert to_embed == []
        assert set(unchanged) == {"Notes/a.md", "Notes/b.md"}
        assert to_remove == []
    finally:
        con.close()


def test_plan_reembeds_only_the_edited_doc(isolated_vault):
    """Editing one doc re-plans exactly that one; the rest stay unchanged."""
    isolated_vault.write("Notes/a.md", "alpha fact\n")
    isolated_vault.write("Notes/b.md", "beta fact\n")
    con = isolated_vault.connect()
    try:
        _seed_vec_sources(con, list(gm.iter_embed_corpus(isolated_vault.root)))
        isolated_vault.write("Notes/a.md", "alpha fact — now edited\n")
        corpus2 = list(gm.iter_embed_corpus(isolated_vault.root))
        to_embed, unchanged, to_remove = gm.plan_embedding(con, corpus2)
        assert {i.source_file for i in to_embed} == {"Notes/a.md"}
        assert unchanged == ["Notes/b.md"]
        assert to_remove == []
    finally:
        con.close()


def test_plan_removes_deleted_and_newly_denied_docs(isolated_vault):
    """A doc gone from disk, or newly caught by the R5 filter, lands in to-remove."""
    isolated_vault.write("Notes/gone.md", "will be deleted\n")
    isolated_vault.write("Notes/becomes-private.md", "will gain a denied tag\n")
    isolated_vault.write("Notes/stays.md", "stays put\n")
    con = isolated_vault.connect()
    try:
        _seed_vec_sources(con, list(gm.iter_embed_corpus(isolated_vault.root)))
        (isolated_vault.root / "Notes" / "gone.md").unlink()
        isolated_vault.write(
            "Notes/becomes-private.md", "---\ntags:\n  - sms\n---\n\nnow private\n"
        )
        corpus2 = list(gm.iter_embed_corpus(isolated_vault.root))
        to_embed, unchanged, to_remove = gm.plan_embedding(con, corpus2)
        assert to_remove == ["Notes/becomes-private.md", "Notes/gone.md"]
        assert unchanged == ["Notes/stays.md"]
        assert to_embed == []
    finally:
        con.close()


def test_rebuild_flag_replans_every_doc_even_if_unchanged(isolated_vault):
    """``rebuild=True`` ignores the content-hash cache and re-embeds everything."""
    isolated_vault.write("Notes/a.md", "alpha fact\n")
    con = isolated_vault.connect()
    try:
        corpus = list(gm.iter_embed_corpus(isolated_vault.root))
        _seed_vec_sources(con, corpus)
        to_embed, unchanged, to_remove = gm.plan_embedding(con, corpus, rebuild=True)
        assert {i.source_file for i in to_embed} == {"Notes/a.md"}
        assert unchanged == []
    finally:
        con.close()


def test_dry_run_is_side_effect_free(isolated_vault):
    """``embed_vault(dry_run=True)`` reports the plan and writes nothing — no
    vec0 table, no vec_sources rows, no sqlite-vec needed."""
    isolated_vault.write("Notes/a.md", "alpha fact\n")
    con = isolated_vault.connect()
    try:
        result = gm.embed_vault(con, vault_root=isolated_vault.root, dry_run=True)
        assert result.dry_run is True
        assert result.to_embed == 1
        assert result.embedded == 0
        assert not gm._table_exists(con, "vec_embeddings")
        assert not gm._table_exists(con, "vec_sources")
    finally:
        con.close()


def test_cli_embed_dry_run_prints_plan_and_exits_zero(tmp_path, capsys):
    """``main(["embed", --vault-root, tmp, --dry-run])`` prints the planned docs,
    returns 0, and creates no vector tables. Uses a plain tmp vault (not
    ``isolated_vault``) so the real-vault-untouched guard stays meaningful."""
    import sqlite3

    vault = tmp_path / "vault"
    (vault / "Notes").mkdir(parents=True)
    (vault / "Notes" / "a.md").write_text("alpha fact\n", encoding="utf-8")

    rc = gm.main(["embed", "--vault-root", str(vault), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "embed plan (dry-run)" in out
    assert "Notes/a.md" in out

    con = sqlite3.connect(vault / "Memory" / "relations.sqlite")
    try:
        names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    assert "vec_embeddings" not in names
    assert "vec_sources" not in names


def test_embed_skips_gracefully_when_sqlite_vec_absent(isolated_vault, monkeypatch, capsys):
    """Optional-dep contract: with sqlite-vec unavailable, a non-dry-run embed
    prints a clear skip message and returns 0 effect — and the rest of
    graph_memory (scan / get_related / render_index) keeps working untouched."""
    isolated_vault.write("Notes/a.md", "[[x]] alpha fact\n")
    con = isolated_vault.connect()
    try:
        def _raise(_con):
            raise gm.EmbeddingsUnavailable("sqlite-vec is not installed (simulated)")

        monkeypatch.setattr(gm, "_load_sqlite_vec", _raise)

        result = gm.embed_vault(con, vault_root=isolated_vault.root, dry_run=False)
        assert result.skipped_reason is not None
        assert result.embedded == 0
        assert "skipping embed" in capsys.readouterr().out
        # the absent path returned before building anything
        assert not gm._table_exists(con, "vec_embeddings")
        assert not gm._table_exists(con, "vec_sources")

        # rest of graph_memory still works on the same connection
        gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
        related = {(r["from_slug"], r["to_slug"]) for r in gm.get_related(con, "a")}
        assert ("a", "x") in related
        assert "Memory Index" in gm.render_index_markdown(con, vault_root=isolated_vault.root)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Phase 4 — real sqlite-vec vec0 build / KNN / incremental upsert / delete
# ---------------------------------------------------------------------------
#
# These need the optional extension, so each guards with importorskip. In the
# dev/test env sqlite-vec IS installed (dependency-groups.dev), so the gate runs
# them for real; the installed-wheel smoke is --no-dev and skips them, which is
# the optional contract. Run the suite with -rs once to confirm they RAN here.


def _onehot_embedder(text: str) -> list[float]:
    """A tiny fake embedder: a one-hot 3-vector keyed by a fruit keyword, so the
    nearest-neighbour answer for a crafted query is predictable."""
    t = text.lower()
    if "apple" in t:
        return [1.0, 0.0, 0.0]
    if "banana" in t:
        return [0.0, 1.0, 0.0]
    return [0.0, 0.0, 1.0]


def _hash_embedder(text: str, dim: int = 4) -> list[float]:
    """A deterministic fake embedder whose vector changes whenever the text does
    — so editing a doc produces an observably different vector."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [digest[i] / 255.0 for i in range(dim)]


def _vec_snapshot(con) -> dict[str, tuple[int, str, bytes]]:
    """Per-source (rowid, content_hash, embedding-blob), by point lookup on rowid
    (vec0 rows are addressable by rowid)."""
    out: dict[str, tuple[int, str, bytes]] = {}
    for row in con.execute("SELECT rowid, source_file, content_hash FROM vec_sources"):
        blob = con.execute(
            "SELECT embedding FROM vec_embeddings WHERE rowid = ?", (row["rowid"],)
        ).fetchone()[0]
        out[row["source_file"]] = (row["rowid"], row["content_hash"], blob)
    return out


def test_vec0_embed_builds_table_and_knn_finds_nearest(isolated_vault):
    """A full embed builds ``vec_embeddings``, one row per included doc, and a
    serialized KNN query returns the expected nearest source_file."""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    isolated_vault.write("Notes/apple.md", "apple orchard harvest\n")
    isolated_vault.write("Notes/banana.md", "banana tree tropical\n")
    isolated_vault.write("Notes/cherry.md", "cherry blossom spring\n")
    con = isolated_vault.connect()
    try:
        result = gm.embed_vault(
            con, vault_root=isolated_vault.root, embedder=_onehot_embedder, dim=3
        )
        assert result.embedded == 3
        assert result.skipped_reason is None
        assert gm._table_exists(con, "vec_embeddings")
        assert con.execute("SELECT COUNT(*) FROM vec_embeddings").fetchone()[0] == 3
        assert con.execute("SELECT COUNT(*) FROM vec_sources").fetchone()[0] == 3

        query = sqlite_vec.serialize_float32([0.9, 0.1, 0.0])  # near the apple axis
        row = con.execute(
            "SELECT rowid FROM vec_embeddings WHERE embedding MATCH ? "
            "ORDER BY distance LIMIT 1",
            (query,),
        ).fetchone()
        nearest = con.execute(
            "SELECT source_file FROM vec_sources WHERE rowid = ?", (row["rowid"],)
        ).fetchone()
        assert nearest["source_file"] == "Notes/apple.md"
    finally:
        con.close()


def test_reembed_updates_only_the_edited_rowid_in_place(isolated_vault):
    """Re-embedding after editing one doc updates exactly that rowid's vector and
    content_hash **in place** (same rowid); the other doc's row is untouched."""
    pytest.importorskip("sqlite_vec")
    isolated_vault.write("Notes/apple.md", "apple version one\n")
    isolated_vault.write("Notes/banana.md", "banana version one\n")
    con = isolated_vault.connect()
    try:
        gm.embed_vault(con, vault_root=isolated_vault.root, embedder=_hash_embedder, dim=4)
        before = _vec_snapshot(con)

        isolated_vault.write("Notes/apple.md", "apple version two — edited\n")
        result = gm.embed_vault(
            con, vault_root=isolated_vault.root, embedder=_hash_embedder, dim=4
        )
        assert result.embedded == 1
        assert result.unchanged == 1
        assert result.removed == 0
        after = _vec_snapshot(con)

        # apple: same rowid (in-place update), new content hash, new vector blob
        assert after["Notes/apple.md"][0] == before["Notes/apple.md"][0]
        assert after["Notes/apple.md"][1] != before["Notes/apple.md"][1]
        assert after["Notes/apple.md"][2] != before["Notes/apple.md"][2]
        # banana: fully untouched
        assert after["Notes/banana.md"] == before["Notes/banana.md"]
    finally:
        con.close()


def test_removed_doc_deletes_both_vec0_and_vec_sources_rows(isolated_vault):
    """Deleting a doc on disk removes both its vec0 row and its vec_sources row on
    the next embed; the surviving doc is intact."""
    pytest.importorskip("sqlite_vec")
    isolated_vault.write("Notes/keep.md", "keep me around\n")
    isolated_vault.write("Notes/drop.md", "drop me later\n")
    con = isolated_vault.connect()
    try:
        gm.embed_vault(con, vault_root=isolated_vault.root, embedder=_hash_embedder, dim=4)
        drop_rowid = con.execute(
            "SELECT rowid FROM vec_sources WHERE source_file = ?", ("Notes/drop.md",)
        ).fetchone()["rowid"]

        (isolated_vault.root / "Notes" / "drop.md").unlink()
        result = gm.embed_vault(
            con, vault_root=isolated_vault.root, embedder=_hash_embedder, dim=4
        )
        assert result.removed == 1

        assert con.execute(
            "SELECT COUNT(*) FROM vec_sources WHERE source_file = ?", ("Notes/drop.md",)
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM vec_embeddings WHERE rowid = ?", (drop_rowid,)
        ).fetchone()[0] == 0
        # keep.md survives in both tables
        assert con.execute("SELECT COUNT(*) FROM vec_sources").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM vec_embeddings").fetchone()[0] == 1
    finally:
        con.close()


def test_default_embedder_full_roundtrip_is_incremental(isolated_vault):
    """The real default (256-dim feature-hashing) embedder runs end to end through
    vec0: first run embeds all, a byte-identical rescan embeds nothing."""
    pytest.importorskip("sqlite_vec")
    isolated_vault.write("Notes/a.md", "a durable fact\n")
    isolated_vault.write("Notes/b.md", "b durable fact\n")
    con = isolated_vault.connect()
    try:
        first = gm.embed_vault(con, vault_root=isolated_vault.root)
        assert first.embedded == 2
        assert first.dim == gm.EMBED_DIM
        assert con.execute("SELECT COUNT(*) FROM vec_embeddings").fetchone()[0] == 2

        second = gm.embed_vault(con, vault_root=isolated_vault.root)
        assert second.embedded == 0
        assert second.unchanged == 2
    finally:
        con.close()


def test_cli_embed_real_run_builds_vectors(tmp_path):
    """``main(["embed", --vault-root, tmp])`` (non-dry-run) builds the vec tables.

    Plain tmp vault (not ``isolated_vault``) so the real-vault-untouched guard
    stays meaningful. Re-opens the DB and loads the extension to inspect it."""
    import sqlite3

    pytest.importorskip("sqlite_vec")
    vault = tmp_path / "vault"
    (vault / "Notes").mkdir(parents=True)
    (vault / "Notes" / "a.md").write_text("alpha fact\n", encoding="utf-8")

    assert gm.main(["embed", "--vault-root", str(vault)]) == 0

    import sqlite_vec

    con = sqlite3.connect(vault / "Memory" / "relations.sqlite")
    try:
        con.enable_load_extension(True)
        sqlite_vec.load(con)
        con.enable_load_extension(False)
        assert con.execute("SELECT COUNT(*) FROM vec_embeddings").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM vec_sources").fetchone()[0] == 1
    finally:
        con.close()
