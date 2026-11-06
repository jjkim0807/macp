"""Characterization tests for ``todo_system.graph_memory``.

This module runs in production on every brief but had zero tests. These pin the
behaviour *as it is today* — several assertions below encode things that look
like bugs (documented inline). They are deliberately frozen, not fixed: the task
that produced this file is scoped to characterization only, and each divergence
is written up in the run's report instead.

Safety: almost every test takes ``isolated_vault`` (see ``conftest.py``) and passes
an explicit ``con=`` into ``scan_vault_and_index``. The exceptions are the two
regression tests at the bottom, which omit ``con=`` on purpose to pin the fact
that the DB path now follows ``vault_root`` instead of the real, import-frozen
``DB_PATH`` — they use ``tmp_path`` rather than ``isolated_vault`` precisely
because that fixture patches ``DB_PATH`` and would make them vacuous.
"""

from __future__ import annotations

import datetime as dt
import inspect
import sqlite3
from pathlib import Path

import pytest

from todo_system import graph_memory as gm

TODAY = dt.date.today().isoformat()


def _scan(vault):
    """Index the tmp vault and hand back the (tmp) connection."""
    con = vault.connect()
    gm.scan_vault_and_index(vault_root=vault.root, con=con)
    return con


def _edges(con):
    return [dict(r) for r in con.execute("SELECT * FROM edges ORDER BY id")]


def _live_edges(con):
    """Only currently-valid edges (``valid_until IS NULL``), id-ordered."""
    return [
        dict(r)
        for r in con.execute(
            "SELECT * FROM edges WHERE valid_until IS NULL ORDER BY id"
        )
    ]


# ---------------------------------------------------------------------------
# A guard for the guard
# ---------------------------------------------------------------------------


def test_the_isolation_fixture_redirects_every_path_global(isolated_vault, tmp_path):
    """If this regresses, every test in this file starts writing to the real vault.

    The real root is recomputed here from the module's own ``__file__`` rather
    than imported from conftest, so a mistake in conftest cannot make the check
    agree with itself.
    """
    from todo_system import session_log

    real_vault = Path(gm.__file__).resolve().parents[1]
    globals_under_test = [(gm, name) for name in (
        "ROOT", "MEMORY_DIR", "ENTITIES_DIR", "EPISODES_DIR", "SESSIONS_DIR",
        "DECISIONS_DIR", "TODO_ITEMS_DIR", "NOTES_DIR", "INDEX_PATH", "DB_PATH",
    )] + [(session_log, name) for name in ("ROOT", "SESSIONS_DIR")]

    for module, name in globals_under_test:
        value = getattr(module, name)
        assert value.is_relative_to(tmp_path), f"{module.__name__}.{name} = {value}"
        assert not value.is_relative_to(real_vault), f"{module.__name__}.{name} = {value}"


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------


def test_parse_frontmatter_returns_content_untouched_when_absent():
    assert gm.parse_frontmatter("# heading\n\nbody\n") == ({}, "# heading\n\nbody\n")


def test_parse_frontmatter_unterminated_block_is_not_frontmatter():
    """Opening `---` with no closing delimiter yields no fields at all."""
    content = "---\ntitle: x\nstill going\n"
    assert gm.parse_frontmatter(content) == ({}, content)


def test_parse_frontmatter_scalars_lists_and_quotes():
    fm, body = gm.parse_frontmatter(
        "---\n"
        "title: Hello\n"
        "double: \"quoted\"\n"
        "single: 'quoted'\n"
        "tags:\n"
        "  - alpha\n"
        '  - "beta"\n'
        "---\n"
        "\nbody text\n"
    )
    assert fm == {
        "title": "Hello",
        "double": "quoted",
        "single": "quoted",
        "tags": ["alpha", "beta"],
    }
    assert body == "\nbody text\n"


def test_parse_frontmatter_skips_lines_without_a_colon():
    fm, _ = gm.parse_frontmatter("---\nkept: yes\njust prose\n---\n\n")
    assert fm == {"kept": "yes"}


# ---------------------------------------------------------------------------
# normalize_target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[[foo]]", "foo"),
        ("foo", "foo"),
        ("foo|Display Name", "foo"),
        ("[[foo|Display Name]]", "foo"),
        ("Todo/items/foo.md", "foo"),
        ("Notes/foo", "foo"),
        ("Memory/entities/foo", "foo"),
        ("Memory/episodes/2026-08-06-foo", "2026-08-06-foo"),
        ("Decisions/foo.md", "foo"),
        ("  spaced  ", "spaced"),
        ("", ""),
    ],
)
def test_normalize_target_strips_brackets_aliases_prefixes_and_suffix(raw, expected):
    assert gm.normalize_target(raw) == expected


def test_normalize_target_does_not_strip_subdirectories_despite_its_docstring():
    """Divergence, frozen as-is.

    The docstring claims it strips ``Todo/items/work/`` prefixes, but the regex is
    anchored and only removes the two leading segments, so the subdirectory stays
    in the slug. A link written as ``[[Todo/items/work/foo]]`` therefore produces
    the edge target ``work/foo``, which will never match the file's stem ``foo``.
    """
    assert "work/" in gm.PATH_PREFIX_RE.sub("", "Todo/items/work/foo")
    assert gm.normalize_target("Todo/items/work/foo") == "work/foo"


def test_normalize_target_does_not_know_about_memory_sessions():
    """``Memory/sessions/`` is absent from PATH_PREFIX_RE, so it survives intact.

    Frozen, not fixed — it is why a hand-written link to a session journal cannot
    resolve to the same slug the index renders.
    """
    assert gm.normalize_target("Memory/sessions/2026-08-06-proj") == "Memory/sessions/2026-08-06-proj"


# ---------------------------------------------------------------------------
# edges_from_file
# ---------------------------------------------------------------------------


def test_edges_from_file_covers_every_declared_edge_field(isolated_vault):
    fields = "\n".join(f"{name}:\n  - target-{name}" for name, _ in gm.LIST_EDGE_FIELDS)
    scalars = "\n".join(f"{name}: target-{name}" for name, _ in gm.SCALAR_EDGE_FIELDS)
    path = isolated_vault.write("Notes/every-field.md", f"---\n{fields}\n{scalars}\n---\n\nbody\n")

    edges = gm.edges_from_file(path, vault_root=isolated_vault.root)

    expected_types = {t for _, t in gm.LIST_EDGE_FIELDS} | {t for _, t in gm.SCALAR_EDGE_FIELDS}
    assert {e["edge_type"] for e in edges} == expected_types
    assert len(edges) == len(gm.LIST_EDGE_FIELDS) + len(gm.SCALAR_EDGE_FIELDS)
    assert {e["from_slug"] for e in edges} == {"every-field"}


def test_edges_from_file_order_metadata_and_self_reference(isolated_vault):
    path = isolated_vault.write(
        "Notes/alpha.md",
        "---\n"
        "depends_on:\n"
        "  - beta\n"
        '  - "[[gamma]]"\n'
        "supersedes: delta\n"
        "confidence: 0.8\n"
        "event_date: 2026-08-01\n"
        "---\n"
        "\nLinks [[epsilon]], [[Todo/items/zeta|alias]] and itself [[alpha]].\n",
    )

    edges = gm.edges_from_file(path, vault_root=isolated_vault.root, recorded_at="2026-08-06T00:00:00")

    # Frontmatter lists first (in LIST_EDGE_FIELDS order), then scalars, then body.
    assert [(e["to_slug"], e["edge_type"]) for e in edges] == [
        ("beta", "depends_on"),
        ("gamma", "depends_on"),
        ("delta", "supersedes"),
        ("epsilon", "wikilink"),
        ("zeta", "wikilink"),
    ]
    # A link to the file's own slug is dropped.
    assert "alpha" not in {e["to_slug"] for e in edges}
    # File-level metadata is stamped onto every edge from that file.
    assert {e["confidence"] for e in edges} == {0.8}
    assert {e["event_date"] for e in edges} == {"2026-08-01"}
    assert {e["recorded_at"] for e in edges} == {"2026-08-06T00:00:00"}
    assert {e["source_file"] for e in edges} == {"Notes/alpha.md"}
    assert {e["valid_until"] for e in edges} == {None}


def test_edges_from_file_without_frontmatter_still_reads_body_links(isolated_vault):
    path = isolated_vault.write("Notes/bare.md", "no frontmatter, just [[target]].\n")
    edges = gm.edges_from_file(path, vault_root=isolated_vault.root)
    assert [(e["to_slug"], e["edge_type"]) for e in edges] == [("target", "wikilink")]
    assert edges[0]["event_date"] is None
    assert edges[0]["confidence"] is None


def test_edges_from_file_placeholder_and_unparsable_metadata(isolated_vault):
    """`supersedes: -` is the vault's "unset" placeholder; bad confidence → None."""
    path = isolated_vault.write(
        "Notes/placeholders.md",
        "---\nsupersedes: -\nparent: real-parent\nconfidence: high\nevent_date: 20260801\n---\n\n",
    )
    edges = gm.edges_from_file(path, vault_root=isolated_vault.root)
    assert [(e["to_slug"], e["edge_type"]) for e in edges] == [("real-parent", "parent")]
    assert edges[0]["confidence"] is None
    # event_date is passed through verbatim — no date validation happens.
    assert edges[0]["event_date"] == "20260801"


def test_edges_from_file_returns_empty_for_unreadable_path(isolated_vault):
    missing = isolated_vault.root / "Notes" / "does-not-exist.md"
    assert gm.edges_from_file(missing, vault_root=isolated_vault.root) == []


# ---------------------------------------------------------------------------
# Scanning — AC#2, rearmed
# ---------------------------------------------------------------------------


def test_links_to_nonexistent_slugs_are_indexed_and_can_rank_top(isolated_vault):
    """The pinned form of "broken links are reported as broken".

    ``edges_from_file`` / ``normalize_target`` never check that the target exists,
    so a wikilink pointing at nothing still becomes an edge and competes for the
    index's top-10 on raw reference count. That is the contract the index page is
    built on, and it is what makes a dangling link *visible* rather than silently
    dropped. If a future change starts filtering unresolvable targets, this fails.
    """
    for name in ("a", "b", "c"):
        isolated_vault.write(f"Notes/{name}.md", f"see [[ghost-slug]] from {name}\n")
    isolated_vault.write("Notes/real-target.md", "I exist.\n")
    isolated_vault.write("Notes/d.md", "see [[real-target]]\n")

    con = _scan(isolated_vault)

    # Precondition: the target genuinely does not exist anywhere in the vault.
    assert list(isolated_vault.root.rglob("ghost-slug.md")) == []
    ghost = [e for e in _edges(con) if e["to_slug"] == "ghost-slug"]
    assert len(ghost) == 3
    assert gm.most_referenced_slugs(con) == [("ghost-slug", 3), ("real-target", 1)]
    con.close()


def test_scan_only_walks_scan_roots_so_session_journals_are_never_indexed(isolated_vault):
    """``Memory/sessions/`` is not in SCAN_ROOTS — frozen, not fixed.

    Session journals written by ``session_log`` carry ``related_tasks`` wikilinks,
    but they contribute no edges, so manual-session work never shows up in the
    graph even though it shows up in the rendered index.
    """
    isolated_vault.write("Notes/indexed.md", "[[from-notes]]\n")
    isolated_vault.write(
        "Memory/sessions/2026-08-06-proj.md",
        "---\nrelated_tasks:\n  - \"[[Todo/items/some-task]]\"\n---\n\n[[from-session]]\n",
    )

    con = _scan(isolated_vault)
    targets = {e["to_slug"] for e in _edges(con)}
    assert targets == {"from-notes"}
    assert "from-session" not in targets
    assert "some-task" not in targets
    con.close()


def test_scan_replaces_rather_than_accumulates(isolated_vault):
    isolated_vault.write("Notes/a.md", "[[x]]\n")
    con = isolated_vault.connect()

    gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
    first = len(_edges(con))
    gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
    # A second identical scan neither inserts nor tombstones: the one row stays.
    assert len(_edges(con)) == first == 1
    original = _edges(con)[0]

    # Removing the source tombstones its edge on the next scan — the incremental
    # valid_until path, not clear_edges + reinsert. The live set empties, but the
    # history row survives in place with its original id and recorded_at.
    (isolated_vault.root / "Notes" / "a.md").unlink()
    gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con)
    assert _live_edges(con) == []
    survivors = _edges(con)
    assert len(survivors) == 1
    assert survivors[0]["id"] == original["id"]
    assert survivors[0]["valid_until"] is not None
    assert survivors[0]["recorded_at"] == original["recorded_at"]
    con.close()


def test_scan_returns_edge_count(isolated_vault):
    isolated_vault.write("Notes/a.md", "[[x]] [[y]]\n")
    isolated_vault.write("Todo/items/t.md", "[[z]]\n")
    con = isolated_vault.connect()
    assert gm.scan_vault_and_index(vault_root=isolated_vault.root, con=con) == 3
    con.close()


def test_iter_markdown_files_skips_missing_roots_and_sorts(isolated_vault):
    isolated_vault.write("Notes/b.md", "")
    isolated_vault.write("Notes/a.md", "")
    isolated_vault.write("Notes/nested/c.md", "")
    # Decisions/ exists but is empty; Memory/entities/ likewise — neither raises.
    names = [p.name for p in gm.iter_markdown_files(isolated_vault.root)]
    assert names == ["a.md", "b.md", "c.md"]


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def test_get_related_walks_hops_but_re_reports_edges_it_already_returned(isolated_vault):
    """Depth > 1 returns duplicates — frozen, not fixed.

    Each hop re-queries every edge touching the new frontier, and ``a --> b`` is
    such an edge for both ``a`` and ``b``, so it comes back twice. Callers that
    count rows (rather than dedupe) will overstate connectivity.
    """
    isolated_vault.write("Notes/a.md", "---\ndepends_on:\n  - b\n---\n\n")
    isolated_vault.write("Notes/b.md", "---\ndepends_on:\n  - c\n---\n\n")
    con = _scan(isolated_vault)

    depth1 = gm.get_related(con, "a", depth=1)
    assert [(r["from_slug"], r["to_slug"]) for r in depth1] == [("a", "b")]

    # Asserted as a multiset: the hop query has no ORDER BY, so the sequence is
    # query-planner dependent (it differs between the OR-index union and a scan).
    # The characterized fact is the duplication, not the ordering.
    depth2 = gm.get_related(con, "a", depth=2)
    pairs = [(r["from_slug"], r["to_slug"]) for r in depth2]
    assert len(pairs) == 3
    assert pairs.count(("a", "b")) == 2  # returned again as an edge touching `b`
    assert pairs.count(("b", "c")) == 1
    con.close()


def test_get_related_depth_zero_behaves_like_depth_one(isolated_vault):
    """``max(1, depth)`` means depth=0 (and negatives) still do one hop."""
    isolated_vault.write("Notes/a.md", "[[b]]\n")
    con = _scan(isolated_vault)
    assert gm.get_related(con, "a", depth=0) == gm.get_related(con, "a", depth=1)
    con.close()


def test_get_related_is_direction_agnostic_and_empty_for_unknown_slug(isolated_vault):
    isolated_vault.write("Notes/a.md", "[[b]]\n")
    con = _scan(isolated_vault)
    assert len(gm.get_related(con, "b", depth=1)) == 1  # inbound counts too
    assert gm.get_related(con, "nobody", depth=1) == []
    con.close()


def test_most_referenced_slugs_ties_break_alphabetically_and_limit_applies(isolated_vault):
    body = " ".join(f"[[t{i:02d}]]" for i in range(1, 12))
    isolated_vault.write("Notes/hub.md", body + "\n")
    con = _scan(isolated_vault)

    top = gm.most_referenced_slugs(con)
    assert len(top) == 10
    assert [slug for slug, _ in top] == [f"t{i:02d}" for i in range(1, 11)]
    assert gm.most_referenced_slugs(con, limit=2) == [("t01", 1), ("t02", 1)]
    con.close()


# ---------------------------------------------------------------------------
# The vault_root argument is honoured
# ---------------------------------------------------------------------------


@pytest.fixture
def decoy_vault(tmp_path):
    """A second vault tree that is NOT the one the module globals point at."""
    root = tmp_path / "decoy"
    for rel in ("Memory/episodes", "Memory/sessions", "Decisions", "Notes"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root


def test_recent_episodes_reads_the_vault_root_it_is_given(isolated_vault, decoy_vault):
    """The parameter is accepted *and* read.

    ``recent_episodes`` resolves ``Memory/episodes`` under the argument rather
    than globbing the module-level ``EPISODES_DIR``, so passing a different
    vault returns *that* vault's episodes and the patched globals stay invisible.
    """
    isolated_vault.write(f"Memory/episodes/{TODAY}-patched.md", "")
    (decoy_vault / "Memory" / "episodes" / f"{TODAY}-decoy.md").write_text("", encoding="utf-8")

    assert [p.stem for p in gm.recent_episodes(decoy_vault)] == [f"{TODAY}-decoy"]


def test_recent_sessions_reads_the_vault_root_it_is_given(isolated_vault, decoy_vault):
    isolated_vault.write(f"Memory/sessions/{TODAY}-patched.md", "")
    (decoy_vault / "Memory" / "sessions" / f"{TODAY}-decoy.md").write_text("", encoding="utf-8")

    assert [p.stem for p in gm.recent_sessions(decoy_vault)] == [f"{TODAY}-decoy"]


def test_active_decisions_reads_the_vault_root_it_is_given(isolated_vault, decoy_vault):
    isolated_vault.write("Decisions/patched.md", "---\ndecision_status: accepted\n---\n\n")
    (decoy_vault / "Decisions" / "decoy.md").write_text(
        "---\ndecision_status: accepted\n---\n\n", encoding="utf-8"
    )

    assert [p.stem for p, _ in gm.active_decisions(decoy_vault)] == ["decoy"]


def test_recent_episodes_cutoff_and_unparsable_filename_fallback(isolated_vault):
    old = (dt.date.today() - dt.timedelta(days=30)).isoformat()
    edge = (dt.date.today() - dt.timedelta(days=14)).isoformat()
    isolated_vault.write(f"Memory/episodes/{old}-too-old.md", "")
    isolated_vault.write(f"Memory/episodes/{edge}-exactly-at-cutoff.md", "")
    isolated_vault.write(f"Memory/episodes/{TODAY}-fresh.md", "")
    # No leading date → date.fromisoformat raises → falls back to mtime (= now).
    isolated_vault.write("Memory/episodes/no-date-prefix.md", "")

    stems = {p.stem for p in gm.recent_episodes(isolated_vault.root)}
    assert stems == {f"{edge}-exactly-at-cutoff", f"{TODAY}-fresh", "no-date-prefix"}


def test_recent_episodes_returns_empty_when_the_directory_is_absent(isolated_vault):
    isolated_vault.episodes.rmdir()
    assert gm.recent_episodes(isolated_vault.root) == []


def test_active_decisions_only_accepts_the_accepted_status(isolated_vault):
    isolated_vault.write("Decisions/yes.md", "---\ndecision_status: ACCEPTED\n---\n\n")
    isolated_vault.write("Decisions/no.md", "---\ndecision_status: proposed\n---\n\n")
    isolated_vault.write("Decisions/none.md", "---\ntitle: no status\n---\n\n")
    assert [p.stem for p, _ in gm.active_decisions(isolated_vault.root)] == ["yes"]


# ---------------------------------------------------------------------------
# Unlinked-mention scanning
# ---------------------------------------------------------------------------


def _entity(vault, slug: str, name: str, aliases: list[str] | None = None) -> None:
    alias_block = ""
    if aliases:
        alias_block = "aliases:\n" + "".join(f"  - {a}\n" for a in aliases)
    vault.write(f"Memory/entities/{slug}.md", f"---\nname: {name}\n{alias_block}---\n\n")


def test_scan_unlinked_mentions_proposes_name_and_alias_matches(isolated_vault):
    _entity(isolated_vault, "acme-corp", "Acme Corp", ["Acme Inc"])
    isolated_vault.write("Notes/meeting.md", "Met Acme Corp and later Acme Inc today.\n")

    proposals = gm.scan_unlinked_mentions(isolated_vault.root)

    assert {(p["alias"], p["suggested_slug"]) for p in proposals} == {
        ("Acme Corp", "acme-corp"),
        ("Acme Inc", "acme-corp"),
    }
    assert {p["source_file"] for p in proposals} == {"Notes/meeting.md"}
    assert all("Acme" in p["snippet"] for p in proposals)


def test_scan_unlinked_mentions_skips_existing_links_code_fences_and_partial_words(isolated_vault):
    _entity(isolated_vault, "acme-corp", "Acme Corp")
    isolated_vault.write("Notes/linked.md", "Already [[Acme Corp]] here.\n")
    isolated_vault.write("Notes/fenced.md", "```\nAcme Corp\n```\n")
    isolated_vault.write("Notes/partial.md", "Acme Corporation is a different token.\n")

    assert gm.scan_unlinked_mentions(isolated_vault.root) == []


def test_scan_unlinked_mentions_ignores_short_aliases_and_entity_files_themselves(isolated_vault):
    _entity(isolated_vault, "ab", "AB")  # both < 3 chars → never registered
    _entity(isolated_vault, "acme-corp", "Acme Corp")
    isolated_vault.write("Notes/short.md", "AB AB AB and Acme Corp\n")

    proposals = gm.scan_unlinked_mentions(isolated_vault.root)
    assert {p["alias"] for p in proposals} == {"Acme Corp"}
    # Entity pages are excluded from the corpus, so acme-corp.md proposes nothing.
    assert all(not p["source_file"].startswith("Memory/entities/") for p in proposals)


def test_scan_unlinked_mentions_reads_both_entities_and_files_from_the_argument(
    isolated_vault, decoy_vault
):
    """A fully-honoured argument — both halves now come from ``vault_root``.

    The alias table used to come from the patched module global ``ENTITIES_DIR``
    while only the corpus came from the argument, so one call could describe two
    different vaults. Now the entities and the corpus are read from the same
    given root, and the patched globals contribute neither.
    """
    _entity(isolated_vault, "acme-corp", "Acme Corp")
    isolated_vault.write("Notes/in-patched-vault.md", "Acme Corp appears here.\n")

    decoy_entities = decoy_vault / "Memory" / "entities"
    decoy_entities.mkdir(parents=True, exist_ok=True)
    (decoy_entities / "beta-co.md").write_text("---\nname: Beta Co\n---\n\n", encoding="utf-8")
    (decoy_vault / "Notes" / "in-decoy.md").write_text(
        "Beta Co and Acme Corp both appear here.\n", encoding="utf-8"
    )

    proposals = gm.scan_unlinked_mentions(decoy_vault)

    # Corpus: only the decoy's notes (the patched vault's note is unseen).
    # Aliases: only the decoy's entities, so "Acme Corp" — an entity of the
    # *patched* vault — proposes nothing even though it appears in the text.
    assert [(p["source_file"], p["alias"], p["suggested_slug"]) for p in proposals] == [
        ("Notes/in-decoy.md", "Beta Co", "beta-co")
    ]


def test_scan_unlinked_mentions_returns_empty_without_an_entities_dir(isolated_vault):
    isolated_vault.entities.rmdir()
    isolated_vault.write("Notes/anything.md", "text\n")
    assert gm.scan_unlinked_mentions(isolated_vault.root) == []


def test_render_cross_link_queue_empty_and_populated():
    empty = gm.render_cross_link_queue([])
    assert "- 제안 없음.\n" in empty
    assert "> 총 0건 제안 (0 파일)\n" in empty

    proposals = [
        {"source_file": "Notes/a.md", "alias": "Acme Corp", "suggested_slug": "acme-corp", "offset": 0, "snippet": "s"},
        # Same (alias, snippet) from the same source is deduped.
        {"source_file": "Notes/a.md", "alias": "Acme Corp", "suggested_slug": "acme-corp", "offset": 9, "snippet": "s"},
    ]
    out = gm.render_cross_link_queue(proposals)
    assert "> 총 2건 제안 (1 파일)\n" in out  # the header counts pre-dedupe
    assert out.count("- **Acme Corp** → `[[acme-corp]]`") == 1
    assert "## [[a]]\n" in out


# ---------------------------------------------------------------------------
# Index rendering
# ---------------------------------------------------------------------------


def test_render_index_markdown_on_an_empty_vault(isolated_vault):
    con = _scan(isolated_vault)
    md = gm.render_index_markdown(con, vault_root=isolated_vault.root)
    con.close()

    assert md.startswith("---\ntags:\n  - memory\n  - index\n")
    assert f"updated: {TODAY}\n" in md
    assert "# 🧠 Memory Index\n" in md
    assert "> total edges: 0\n" in md
    assert "- (edges 없음)\n" in md
    assert "- (참조된 entity 없음)\n" in md
    assert "- (활성 결정 없음)\n" in md
    assert "- (최근 episode 없음)\n" in md
    assert "- (최근 수동 세션 없음)\n" in md


def test_render_index_markdown_sections(isolated_vault):
    isolated_vault.write("Notes/a.md", "---\ndepends_on:\n  - hub\n---\n\n[[hub]] [[side]]\n")
    isolated_vault.write("Notes/b.md", "[[hub]]\n")
    isolated_vault.write("Decisions/pick-sqlite.md", "---\ndecision_status: accepted\ndecision_date: 2026-07-01\n---\n\n")
    isolated_vault.write("Decisions/no-date.md", "---\ndecision_status: accepted\n---\n\n")
    isolated_vault.write(f"Memory/episodes/{TODAY}-ep.md", "")
    isolated_vault.write(
        f"Memory/sessions/{TODAY}-proj.md",
        "---\nstatus: active\ncheckpoints: 3\n---\n\n",
    )

    con = _scan(isolated_vault)
    md = gm.render_index_markdown(con, vault_root=isolated_vault.root)
    con.close()

    assert "> total edges: 4\n" in md
    assert "- `wikilink`: 3\n" in md
    assert "- `depends_on`: 1\n" in md
    assert "- [[hub]] — 3회 참조\n" in md
    assert "- [[side]] — 1회 참조\n" in md
    assert "- [[pick-sqlite]] (날짜: `2026-07-01`)\n" in md
    assert "- [[no-date]] (날짜: `-`)\n" in md  # missing date renders as a dash
    assert f"- [[{TODAY}-ep]]\n" in md
    assert f"- [[{TODAY}-proj]] (`active`, 3 checkpoints)\n" in md


def test_render_index_markdown_uses_question_marks_for_missing_session_frontmatter(isolated_vault):
    isolated_vault.write(f"Memory/sessions/{TODAY}-bare.md", "no frontmatter here\n")
    con = _scan(isolated_vault)
    md = gm.render_index_markdown(con, vault_root=isolated_vault.root)
    con.close()
    assert f"- [[{TODAY}-bare]] (`?`, ? checkpoints)\n" in md


def test_render_index_markdown_caps_episode_and_session_lists_at_twenty(isolated_vault):
    for i in range(25):
        isolated_vault.write(f"Memory/episodes/{TODAY}-ep{i:02d}.md", "")
    con = _scan(isolated_vault)
    md = gm.render_index_markdown(con, vault_root=isolated_vault.root)
    con.close()

    rendered = [f"- [[{TODAY}-ep{i:02d}]]\n" in md for i in range(25)]
    assert sum(rendered) == 20
    # It keeps the *last* 20 by sort order, so the earliest ones are dropped.
    assert not any(rendered[:5])
    assert all(rendered[5:])


def test_render_index_markdown_does_not_write_anything(isolated_vault):
    """Rendering is pure — only ``cmd_render_index`` writes INDEX_PATH."""
    con = _scan(isolated_vault)
    gm.render_index_markdown(con, vault_root=isolated_vault.root)
    con.close()
    assert not isolated_vault.index_path.exists()


# ---------------------------------------------------------------------------
# DB path follows vault_root (regression)
# ---------------------------------------------------------------------------


def _stat_or_none(path: Path) -> tuple[int, int] | None:
    """Read-only fingerprint of ``path``; ``None`` when it does not exist.

    Only ever stats — never opens, creates or writes. The real
    ``relations.sqlite`` must come out of this suite untouched.
    """
    if not path.exists():
        return None
    st = path.stat()
    return (st.st_size, st.st_mtime_ns)


def test_scan_without_con_writes_to_vault_root_db(tmp_path):
    """Omitting ``con=`` must index into *vault_root*'s DB, not the real one.

    Deliberately does **not** take ``isolated_vault``: that fixture monkeypatches
    ``gm.DB_PATH`` to a tmp file, which would make the "real DB untouched"
    assertion below vacuous — and that assertion is the whole point of the test.

    It also compares ``st_size``/``st_mtime_ns``, where ``_real_vault_untouched``
    in conftest deliberately avoids mtime (the host's todo-sync timer can
    regenerate the real vault mid-run and would look like a failure). That is
    safe here because the measurement window is one scan of a one-file tmp vault.
    """
    vault = tmp_path / "vault"
    (vault / "Notes").mkdir(parents=True)
    (vault / "Notes" / "a.md").write_text("[[x]]\n", encoding="utf-8")

    before = _stat_or_none(gm.DB_PATH)
    total = gm.scan_vault_and_index(vault_root=vault)

    tmp_db = vault / "Memory" / "relations.sqlite"
    assert tmp_db.exists(), "scan must create the edge DB under vault_root"
    con = sqlite3.connect(tmp_db)
    try:
        assert con.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == total == 1
    finally:
        con.close()

    # Absent stays absent (CI, where the DB is gitignored); present keeps its
    # exact size and mtime (developer box).
    assert _stat_or_none(gm.DB_PATH) == before


def test_no_arg_scan_path_is_unchanged():
    """A no-argument scan must still open exactly the file it always did.

    ``cmd_scan`` / ``cmd_render_index`` / ``cmd_show_related`` and ``cli.py`` all
    call in without a path; if this identity breaks they silently split across
    two different databases.
    """
    assert gm.MEMORY_DIR == gm.ROOT / "Memory"
    assert gm.DB_PATH == gm.ROOT / "Memory" / "relations.sqlite"

    sig = inspect.signature(gm.scan_vault_and_index)
    # ``db_path`` is appended last, so every existing positional call stays valid.
    assert list(sig.parameters) == ["vault_root", "con", "db_path"]
    assert sig.parameters["vault_root"].default / "Memory" / "relations.sqlite" == gm.DB_PATH
    assert sig.parameters["db_path"].default is None


# ---------------------------------------------------------------------------
# CLI (main) — end-to-end runs against a tmp vault
# ---------------------------------------------------------------------------
#
# These four commands had zero tests: until ``--vault-root`` existed they could
# only ever run against the developer's real vault, so exercising them meant
# rebuilding it.
#
# None of them take ``isolated_vault``. That fixture monkeypatches ``gm.ROOT``
# and ``gm.DB_PATH`` to tmp paths, which would make the "the real vault was not
# touched" assertions below vacuous — and those assertions are the whole point.
# A plain ``tmp_path`` plus an explicit ``--vault-root`` is used instead.
#
# They also compare ``st_size``/``st_mtime_ns`` on the real vault, where
# ``_real_vault_untouched`` in conftest deliberately avoids mtime (the host runs
# todo-sync on a timer, which legitimately regenerates ``Memory/*`` mid-run).
# That is safe here for the same reason it is safe in
# ``test_scan_without_con_writes_to_vault_root_db`` above: the measurement window
# is a single sub-second ``main()`` call over a one-or-two-file tmp vault.


def _tmp_vault(tmp_path: Path) -> Path:
    """A minimal vault: one note holding one wikilink, i.e. exactly one edge."""
    vault = tmp_path / "vault"
    (vault / "Notes").mkdir(parents=True)
    (vault / "Notes" / "a.md").write_text("[[x]]\n", encoding="utf-8")
    return vault


def _real_vault_fingerprint() -> tuple[tuple[int, int] | None, ...]:
    """Stat-only snapshot of every file these commands write in the real vault.

    ``cmd_scan``/``cmd_show_related`` write ``DB_PATH``, ``cmd_render_index``
    also writes ``INDEX_PATH``, ``cmd_cross_links`` writes the suggestions queue.
    """
    return (
        _stat_or_none(gm.DB_PATH),
        _stat_or_none(gm.INDEX_PATH),
        _stat_or_none(gm.ROOT / "Todo" / "Inbox" / "cross-link-suggestions.md"),
    )


def test_cli_scan_runs_against_tmp_vault(tmp_path):
    """``main(["scan", "--vault-root", tmp])`` indexes the tmp vault and exits 0."""
    vault = _tmp_vault(tmp_path)
    before = _real_vault_fingerprint()

    assert gm.main(["scan", "--vault-root", str(vault)]) == 0

    tmp_db = vault / "Memory" / "relations.sqlite"
    assert tmp_db.exists(), "scan must create the edge DB under --vault-root"
    con = sqlite3.connect(tmp_db)
    try:
        rows = con.execute("SELECT from_slug, to_slug, edge_type FROM edges").fetchall()
    finally:
        con.close()
    assert rows == [("a", "x", "wikilink")]

    assert _real_vault_fingerprint() == before


def test_cli_render_index_writes_only_under_vault_root(tmp_path, capsys):
    """``render-index`` renders into the tmp vault, never the real Memory/index.md."""
    vault = _tmp_vault(tmp_path)
    before = _real_vault_fingerprint()

    assert gm.main(["render-index", "--vault-root", str(vault)]) == 0

    tmp_index = vault / "Memory" / "index.md"
    assert tmp_index.exists(), "render-index must write index.md under --vault-root"
    body = tmp_index.read_text(encoding="utf-8")
    assert "# 🧠 Memory Index" in body
    # It re-scanned first (the tmp DB started empty), so the one edge is counted.
    assert "total edges: 1" in body

    # Printing the path *relative to the resolved root* is the proof that the
    # ``relative_to`` call was rebased: against the module-global ``ROOT`` a tmp
    # vault would raise ValueError instead.
    assert "wrote Memory/index.md" in capsys.readouterr().out

    assert _real_vault_fingerprint() == before


def test_cli_cross_links_writes_only_under_vault_root(tmp_path, capsys):
    """``cross-links`` writes its queue under --vault-root, not the real Todo/Inbox."""
    vault = tmp_path / "vault"
    (vault / "Memory" / "entities").mkdir(parents=True)
    (vault / "Memory" / "entities" / "graphviz.md").write_text("# graphviz\n", encoding="utf-8")
    (vault / "Notes").mkdir(parents=True)
    (vault / "Notes" / "note.md").write_text(
        "we should render this with graphviz someday\n", encoding="utf-8"
    )
    before = _real_vault_fingerprint()

    assert gm.main(["cross-links", "--vault-root", str(vault)]) == 0

    out_path = vault / "Todo" / "Inbox" / "cross-link-suggestions.md"
    assert out_path.exists(), "cross-links must write its queue under --vault-root"
    assert "graphviz" in out_path.read_text(encoding="utf-8")
    assert "wrote Todo/Inbox/cross-link-suggestions.md with 1 proposals" in capsys.readouterr().out

    assert _real_vault_fingerprint() == before


def test_cli_related_reads_only_the_tmp_vault(tmp_path, capsys):
    """``related`` opens the tmp vault's DB — it is wired to --vault-root too."""
    vault = _tmp_vault(tmp_path)
    before = _real_vault_fingerprint()

    assert gm.main(["related", "a", "--vault-root", str(vault)]) == 0

    assert "a --[wikilink]--> x" in capsys.readouterr().out
    assert (vault / "Memory" / "relations.sqlite").exists()

    assert _real_vault_fingerprint() == before


@pytest.mark.parametrize(
    "argv", [["scan"], ["render-index"], ["cross-links"], ["related", "some-slug"]]
)
def test_cli_no_arg_paths_are_module_globals(argv):
    """All four subcommands take ``--vault-root``; omitting it changes nothing.

    ``_cli_paths`` must hand back the module globals *themselves* rather than
    paths re-derived from ``ROOT``, so ``cli.py``, the daemon and every other
    no-argument caller keep opening the exact files they always did — and so
    ``monkeypatch.setattr(gm, "DB_PATH", ...)`` keeps working.

    This only *parses*: ``gm.main(["scan"])`` with no ``--vault-root`` is never
    called anywhere in this suite, because it would rebuild the real
    ``Memory/relations.sqlite``.
    """
    args = gm.build_parser().parse_args(argv)
    assert args.vault_root is None
    assert gm._cli_paths(args) == (gm.ROOT, gm.DB_PATH, gm.INDEX_PATH)

    elsewhere = gm.build_parser().parse_args([*argv, "--vault-root", "/somewhere/else"])
    assert elsewhere.vault_root == Path("/somewhere/else")
    assert gm._cli_paths(elsewhere) == (
        Path("/somewhere/else"),
        Path("/somewhere/else/Memory/relations.sqlite"),
        Path("/somewhere/else/Memory/index.md"),
    )


def test_index_path_identity_backs_the_no_arg_cli_path():
    """``INDEX_PATH`` must stay under ``ROOT`` or the no-arg print raises.

    ``test_no_arg_scan_path_is_unchanged`` above pins the same identity for
    ``DB_PATH``; ``cmd_render_index``'s ``relative_to`` needs it for the index.
    """
    assert gm.INDEX_PATH == gm.MEMORY_DIR / "index.md"
    assert gm.INDEX_PATH.relative_to(gm.ROOT) == Path("Memory/index.md")
