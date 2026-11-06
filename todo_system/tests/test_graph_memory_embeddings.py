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

from pathlib import Path

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
