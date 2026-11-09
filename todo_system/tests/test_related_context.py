"""The shared related-records renderer (``orchestrator/related_context.py``).

Everything here runs against the ``isolated_vault`` fixture (which repoints
``graph_memory.ROOT`` / ``DB_PATH`` at a tmp tree, and therefore repoints this
module's ``vault_root()`` / ``db_path()`` too, since both read those as
attributes at call time). Nothing touches the real vault, and no case needs
sqlite-vec: the BFS layer alone is enough to pin every contract below, and
``graph_memory._knn_seeds`` returns ``{}`` by itself when the vec tables are
absent — which is exactly the degradation AC#1 asks for.

The three contracts pinned, in the order they matter:

* **Fail-open** — a missing DB, a DB with no ``edges`` table, and a raising
  ``query_related`` each yield ``[]`` / no file / no section, never an exception.
  The runner calls this *before* the research stage, so a raise here would kill
  runs over a hint.
* **R5 metadata only** — the rendered output carries slug / title / source /
  score / edge, and must NOT carry the task's ``outcome`` or ``next_action``
  (which are query *input*) nor any body text of the related doc. The negative
  assertions are the regression guard; they are written against fixtures that
  really do contain those strings, so they cannot pass vacuously.
* **Byte-identical prompt** — an empty result renders ``""``, which is what lets
  ``planner_tick.sh`` / ``brief.sh`` leave their prompt untouched.

Fixtures use only placeholder slugs and relative paths (public repo).
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout

import pytest

from todo_system import graph_memory as gm
from todo_system.orchestrator import related_context as rc

TASK_SLUG = "sample-task-alpha"
# Deliberately distinctive: every "must not appear in the output" assertion below
# greps for these exact strings, so a renderer that leaked them could not pass.
TASK_OUTCOME = "OUTCOME-MUST-NOT-LEAK"
TASK_NEXT = "NEXT-ACTION-MUST-NOT-LEAK"
DOC_BODY = "BODY-EXCERPT-MUST-NOT-LEAK"

FAKE_RUN_ID = "20260101T000000-fake-run"


def _task(**extra):
    task = {"slug": TASK_SLUG, "title": "샘플 태스크 A",
            "outcome": TASK_OUTCOME, "next_action": TASK_NEXT}
    task.update(extra)
    return task


def _edge(con, from_slug: str, to_slug: str, *, edge_type: str = "depends_on",
          run_id: str | None = None) -> None:
    gm.insert_edges(con, [{
        "from_slug": from_slug,
        "to_slug": to_slug,
        "edge_type": edge_type,
        "source_file": f"Todo/items/{from_slug}.md",
        "event_date": None,
        "recorded_at": "2026-01-01T00:00:00",
        "valid_until": None,
        "run_id": run_id,
        "commit_sha": None,
    }])


def _seed(vault, n: int = 3):
    """`n` neighbours of TASK_SLUG, each with its own titled episode doc."""
    con = vault.connect()
    try:
        for i in range(n):
            slug = f"episode-{i:02d}"
            _edge(con, TASK_SLUG, slug, run_id=FAKE_RUN_ID)
            vault.write(
                f"Memory/episodes/{slug}.md",
                f"---\ntitle: 에피소드 {i:02d}\noutcome: {TASK_OUTCOME}\n---\n\n{DOC_BODY}\n",
            )
    finally:
        con.close()


# -- the query ----------------------------------------------------------------

def test_related_items_returns_bfs_only_hits_without_sqlite_vec(isolated_vault):
    _seed(isolated_vault, 3)

    items = rc.related_items(_task())

    assert [it.slug for it in items] == ["episode-00", "episode-01", "episode-02"]
    assert all(it.score.graph > 0 for it in items)
    assert all(it.score.vector == 0.0 for it in items), "no vec tables ⇒ pure graph"
    assert rc.mode_of(items) == "bfs-only"


def test_related_items_truncates_to_the_limit(isolated_vault):
    _seed(isolated_vault, 12)

    assert len(rc.related_items(_task(), limit=8)) == 8
    assert len(rc.related_items(_task(), limit=2)) == 2
    assert rc.related_items(_task(), limit=0) == []


def test_max_items_is_the_eight_the_acceptance_criterion_names(isolated_vault):
    _seed(isolated_vault, 12)

    assert rc.MAX_ITEMS == 8
    assert len(rc.related_items(_task())) == 8


def test_a_missing_db_is_silent_not_fatal(isolated_vault):
    # No vault.connect() — Memory/relations.sqlite was never created.
    assert not isolated_vault.db_path.exists()

    assert rc.related_items(_task()) == []
    assert rc._connect_ro() is None
    assert not isolated_vault.db_path.exists(), "a read path must never create the DB"


def test_a_db_without_an_edges_table_is_silent_not_fatal(isolated_vault):
    """The last fallback of AC#1. A ro connection cannot CREATE the table, so the
    SELECT raises and the wrapper swallows it — which is the wanted behaviour:
    creating it would be a write AND would hide that nothing is indexed yet."""
    isolated_vault.memory.mkdir(parents=True, exist_ok=True)
    import sqlite3

    con = sqlite3.connect(str(isolated_vault.db_path))
    con.execute("CREATE TABLE unrelated (x INTEGER)")
    con.commit()
    con.close()

    assert rc.related_items(_task()) == []


def test_a_raising_query_is_silent_not_fatal(isolated_vault, monkeypatch):
    _seed(isolated_vault, 2)

    def _boom(*_a, **_kw):
        raise RuntimeError("query exploded")

    monkeypatch.setattr(gm, "query_related", _boom)

    assert rc.related_items(_task()) == []


def test_the_kill_switch_stops_the_query(isolated_vault, monkeypatch):
    _seed(isolated_vault, 2)
    calls = []
    monkeypatch.setattr(gm, "query_related", lambda *a, **k: calls.append(a) or [])
    monkeypatch.setenv(rc.KILL_ENV, "0")

    assert rc.related_items(_task()) == []
    assert calls == [], "the switch must stop the query, not just its output"
    assert rc.enabled() is False


# -- titles -------------------------------------------------------------------

def test_title_comes_from_the_items_own_frontmatter(isolated_vault):
    isolated_vault.write("Memory/episodes/episode-00.md",
                         "---\ntitle: 진짜 제목\n---\n\nbody\n")

    assert rc.title_for(isolated_vault.root, "episode-00") == "진짜 제목"


def test_title_falls_back_to_the_slug_when_there_is_no_frontmatter(isolated_vault):
    isolated_vault.write("Memory/episodes/episode-00.md", "no frontmatter at all\n")

    assert rc.title_for(isolated_vault.root, "episode-00") == "episode-00"
    assert rc.title_for(isolated_vault.root, "nothing-on-disk") == "nothing-on-disk"


def test_a_graph_hits_source_file_does_not_supply_the_neighbours_title(isolated_vault):
    """`source_file` on a graph hit is the file that DECLARED the edge — usually
    the OTHER endpoint. Reading a title out of it would label the item with its
    neighbour's title, so the stem has to match before it is trusted."""
    isolated_vault.write(f"Todo/items/{TASK_SLUG}.md",
                         "---\ntitle: 이웃의 제목\n---\n\nbody\n")
    isolated_vault.write("Memory/episodes/episode-00.md",
                         "---\ntitle: 자기 제목\n---\n\nbody\n")

    got = rc.title_for(isolated_vault.root, "episode-00",
                       source_file=f"Todo/items/{TASK_SLUG}.md")

    assert got == "자기 제목"


def test_a_semantic_hits_nested_source_file_does_supply_the_title(isolated_vault):
    """The stem matches, so a nested path outside the flat scan-root layout
    (what a pure-semantic hit carries) still gets its real title."""
    isolated_vault.write("Notes/subdir/nested-note.md",
                         "---\ntitle: 중첩 노트\n---\n\nbody\n")

    got = rc.title_for(isolated_vault.root, "nested-note",
                       source_file="Notes/subdir/nested-note.md")

    assert got == "중첩 노트"


# -- rendering + the R5 boundary ---------------------------------------------

def test_related_md_carries_metadata_and_nothing_else(isolated_vault):
    _seed(isolated_vault, 2)
    items = rc.related_items(_task())

    text = rc.render_related_md(_task(), items, root=isolated_vault.root,
                                run_id=FAKE_RUN_ID)

    assert "kind: related" in text
    assert f"run_id: {FAKE_RUN_ID}" in text
    assert "mode: bfs-only" in text
    assert "count: 2" in text
    assert "`episode-00`" in text
    assert "에피소드 00" in text
    assert "source: `Todo/items/" in text
    assert "edge: depends_on, 1 hop" in text
    # R5: the task's own query fields and the related doc's body stay out.
    assert TASK_OUTCOME not in text
    assert TASK_NEXT not in text
    assert DOC_BODY not in text


def test_the_r5_assertions_are_not_vacuous(isolated_vault):
    """Negative control for the three `not in` assertions above: the strings they
    look for really are present in the inputs, so their absence in the output is
    the renderer's doing and not a typo in the fixture."""
    _seed(isolated_vault, 1)
    task = _task()

    assert TASK_OUTCOME in gm._task_query_text(task)
    assert TASK_NEXT in gm._task_query_text(task)
    doc = (isolated_vault.root / "Memory" / "episodes" / "episode-00.md").read_text(
        encoding="utf-8")
    assert DOC_BODY in doc


def test_mode_says_hybrid_only_when_the_vector_layer_contributed():
    class _Score:
        def __init__(self, vector):
            self.vector, self.graph, self.combined = vector, 0.5, 0.5

    class _Item:
        def __init__(self, vector):
            self.score = _Score(vector)

    assert rc.mode_of([]) == "bfs-only"
    assert rc.mode_of([_Item(0.0), _Item(0.0)]) == "bfs-only"
    assert rc.mode_of([_Item(0.0), _Item(0.4)]) == "hybrid"


def test_an_empty_prompt_section_is_the_empty_string(isolated_vault):
    assert rc.render_prompt_section([], root=isolated_vault.root) == ""
    assert rc.render_prompt_section([(_task(), [])], root=isolated_vault.root) == ""


def test_a_non_empty_prompt_section_has_a_subheading_per_task(isolated_vault):
    _seed(isolated_vault, 2)
    items = rc.related_items(_task())

    out = rc.render_prompt_section([(_task(), items)], root=isolated_vault.root)

    assert out.startswith(rc.PROMPT_HEADING)
    assert f"### `{TASK_SLUG}`" in out
    assert "`episode-00`" in out
    assert TASK_OUTCOME not in out and TASK_NEXT not in out and DOC_BODY not in out


# -- write_related (the runner's entry point) --------------------------------

def test_write_related_writes_the_file_and_returns_one_line(isolated_vault, tmp_path):
    _seed(isolated_vault, 2)
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    line = rc.write_related(run_dir, _task(), run_id=FAKE_RUN_ID)

    assert line == "related: 2 record(s) → related.md (bfs-only)"
    text = (run_dir / rc.RELATED_FILENAME).read_text(encoding="utf-8")
    assert "`episode-00`" in text


def test_write_related_writes_nothing_when_there_is_nothing_to_say(isolated_vault, tmp_path):
    """Zero records ⇒ no file (the `record_orphaned_commits` rule: no key at all
    rather than an empty one), and the caller must read None as success."""
    isolated_vault.connect().close()        # a DB with an empty edges table
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    assert rc.write_related(run_dir, _task()) is None
    assert not (run_dir / rc.RELATED_FILENAME).exists()


def test_write_related_is_silent_when_the_run_dir_is_unwritable(isolated_vault, tmp_path):
    _seed(isolated_vault, 2)

    assert rc.write_related(tmp_path / "does" / "not" / "exist", _task()) is None


# -- task selection + the CLI -------------------------------------------------

def _snapshot(vault, slugs):
    vault.write("Todo/Snapshots/latest.json", json.dumps(
        {"top_priority": [{"slug": s, "title": f"제목 {s}"} for s in slugs]},
        ensure_ascii=False))


def test_top_open_tasks_prefers_the_snapshot(isolated_vault):
    _snapshot(isolated_vault, ["from-snapshot-a", "from-snapshot-b"])
    isolated_vault.write("Todo/items/from-items.md", "---\nstatus: open\n---\n")

    got = rc.top_open_tasks(isolated_vault.root, limit=5)

    assert [t["slug"] for t in got] == ["from-snapshot-a", "from-snapshot-b"]


def test_top_open_tasks_falls_back_to_a_frontmatter_scan(isolated_vault):
    """`vault-template` (what the gate command runs against) has no snapshot."""
    isolated_vault.write("Todo/items/low.md", "---\nstatus: open\npriority: 1\n---\n")
    isolated_vault.write("Todo/items/high.md", "---\nstatus: open\npriority: 9\n---\n")
    isolated_vault.write("Todo/items/closed.md", "---\nstatus: done\npriority: 99\n---\n")

    got = rc.top_open_tasks(isolated_vault.root, limit=5)

    assert [t["slug"] for t in got] == ["high", "low"], "priority desc, done excluded"


def test_top_open_tasks_is_empty_when_the_vault_has_neither(isolated_vault):
    assert rc.top_open_tasks(isolated_vault.root, limit=5) == []
    assert rc.top_open_tasks(isolated_vault.root, limit=0) == []


def test_emit_context_builds_a_section_for_the_top_tasks(isolated_vault):
    _seed(isolated_vault, 2)
    _snapshot(isolated_vault, [TASK_SLUG])

    out = rc.emit_context(limit=3, per_task=8, root=isolated_vault.root)

    assert rc.PROMPT_HEADING in out
    assert "`episode-00`" in out


def test_emit_context_is_empty_without_a_db(isolated_vault):
    _snapshot(isolated_vault, [TASK_SLUG])

    assert rc.emit_context(limit=3, root=isolated_vault.root) == ""


def test_the_cli_prints_nothing_when_the_switch_is_off(isolated_vault, monkeypatch):
    _seed(isolated_vault, 2)
    _snapshot(isolated_vault, [TASK_SLUG])
    monkeypatch.setenv(rc.KILL_ENV, "0")

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = rc.main(["emit-context"])

    assert code == 0
    assert out.getvalue() == ""


def test_the_cli_prints_the_block_and_always_exits_zero(isolated_vault):
    _seed(isolated_vault, 2)
    _snapshot(isolated_vault, [TASK_SLUG])

    out = io.StringIO()
    with redirect_stdout(out):
        code = rc.main(["emit-context", "--limit", "3", "--vault-root",
                        str(isolated_vault.root)])

    assert code == 0
    assert rc.PROMPT_HEADING in out.getvalue()


def test_the_cli_swallows_an_error_and_still_exits_zero(isolated_vault, monkeypatch):
    def _boom(**_kw):
        raise RuntimeError("emit exploded")

    monkeypatch.setattr(rc, "emit_context", _boom)

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = rc.main(["emit-context"])

    assert code == 0
    assert out.getvalue() == ""
    assert "related-context: error" in err.getvalue()


def test_the_module_is_importable_without_sqlite_vec():
    """The stdlib-only contract graph_memory is on: no top-level `import
    sqlite_vec` here either (it stays behind graph_memory's lazy seam)."""
    import ast
    from pathlib import Path

    source = Path(rc.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            imported.add(node.module.split(".")[0])

    assert "sqlite_vec" not in imported
    import sys

    assert imported - set(sys.stdlib_module_names) - {"todo_system"} == set()


@pytest.mark.parametrize("value,expected", [("0", False), ("", True), ("1", True)])
def test_only_an_explicit_zero_disables(monkeypatch, value, expected):
    monkeypatch.setenv(rc.KILL_ENV, value)
    assert rc.enabled() is expected
