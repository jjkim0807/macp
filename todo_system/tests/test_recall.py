"""Free-text recall (``todo_system/recall.py``, ``scripts/macp-recall``) and the
record card behind it (``related_context.doc_card``).

What is pinned, in the order the acceptance criterion names it:

* **Format** — a question comes back as title / path / gist per result.
* **Privacy boundary** — a document outside the R5 boundary never comes out,
  even when the index still holds its row. Each of those cases has a control
  showing the row really is in the index (and, where it applies, that the same
  document was an answer before it was marked), so the absence is the filter's
  doing. Body text and the action fields never come out either.
* **4 KB** — the printed answer never exceeds ``MAX_OUTPUT_BYTES``.
* **Vault resolution** — the resolved vault by default, ``--vault-root`` when
  named, ``MACP_VAULT_ROOT`` through the wrapper from an unrelated cwd.
* **recall.log** — one JSON line per call, the question and the paths shown.

Everything runs against a tmp vault embedded with the stdlib ``hash`` backend,
so the nearest-neighbour answer is deterministic and nothing needs a model or
the network. sqlite-vec is optional here as everywhere: without it
``embed_vault`` fills the pure-Python table and the query reads that one.

Neutral placeholder names only: this file sits inside the tree the public
identifier gate scans.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from todo_system import graph_memory as gm
from todo_system import recall
from todo_system.orchestrator import related_context as rc

REPO_ROOT = Path(recall.__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "scripts" / "macp-recall"

QUERY = "gradle daemon disappeared during the beta build"
TASK = "gradle-daemon-task"
TASK_PATH = f"Todo/items/{TASK}.md"
TASK_TITLE = "Gradle daemon stops during the beta build"
TASK_OUTCOME = "the beta build survives a gradle stop from another run"
# Distinctive on purpose: every "must not come out" assertion looks for these
# exact strings, and a control shows they really are in the document.
BODY_TEXT = "BODY-TEXT-MUST-NOT-COME-OUT"
NEXT_ACTION = "NEXT-ACTION-MUST-NOT-COME-OUT"
IF_THEN = "IF-THEN-MUST-NOT-COME-OUT"


@pytest.fixture
def vault(isolated_vault, monkeypatch):
    """The isolated tmp vault, with every env knob recall reads cleared."""
    for name in ("MACP_EMBEDDER", recall.LOG_ENV, recall.TASK_ENV):
        monkeypatch.delenv(name, raising=False)
    return isolated_vault


def _write_task(root: Path, slug: str = TASK, *, title: str = TASK_TITLE,
                outcome: str = TASK_OUTCOME, extra: str = "") -> Path:
    path = root / "Todo" / "items" / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nstatus: open\noutcome: \"{outcome}\"\nnext_action: \"{NEXT_ACTION}\"\n"
        f"if_then: \"{IF_THEN}\"\n{extra}---\n\n# {title}\n\n"
        f"gradle daemon disappeared during the beta build. {BODY_TEXT}\n",
        encoding="utf-8")
    return path


def _write_filler(root: Path) -> None:
    (root / "Notes").mkdir(parents=True, exist_ok=True)
    (root / "Notes" / "pasta-recipe.md").write_text(
        "---\ntitle: Pasta recipe\n---\n\nboil water, salt it, cook the noodles\n",
        encoding="utf-8")


def _embed(root: Path) -> None:
    """Index ``root`` with the named stdlib backend — the one ``search`` resolves
    for a vault that configures none."""
    embed, dim, _name = gm.resolve_embedder(name="hash")
    con = gm.init_relations_db(root / "Memory" / "relations.sqlite")
    try:
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            gm.embed_vault(con, vault_root=root, embedder=embed, dim=dim)
    finally:
        con.close()


def _indexed(root: Path) -> set[str]:
    """The source paths the vector index holds a row for (either backend)."""
    con = sqlite3.connect(str(root / "Memory" / "relations.sqlite"))
    try:
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master")}
        table = "vec_sources" if "vec_sources" in tables else gm.PY_VEC_TABLE
        return {row[0] for row in con.execute(f"SELECT source_file FROM {table}")}
    finally:
        con.close()


def _paths(answer: recall.Answer) -> list[str]:
    return [card.path for card in answer.cards]


# -- format: title / path / gist ----------------------------------------------

def test_a_question_comes_back_as_title_path_and_gist(vault):
    _write_task(vault.root)
    _write_filler(vault.root)
    _embed(vault.root)

    answer = recall.search(QUERY, k=1)

    assert answer.embedder == "hash"
    assert answer.cards == [rc.DocCard(slug=TASK, path=TASK_PATH, title=TASK_TITLE,
                                       gist=TASK_OUTCOME)]
    page = recall.render(QUERY, answer, root=vault.root)
    assert page.paths == [TASK_PATH]
    assert page.text.splitlines() == [
        f'recall: 1 result(s) for "{QUERY}" (embedder: hash)',
        f"1. {TASK_TITLE}",
        f"   path: {TASK_PATH}",
        f"   gist: {TASK_OUTCOME}",
        f"paths are relative to the vault root {vault.root}",
        "Read the ones that look useful; when a result helps, name its path in "
        "your run log.",
    ]


def test_the_nearest_document_ranks_first_and_k_bounds_the_answer(vault):
    _write_task(vault.root)
    _write_filler(vault.root)
    _embed(vault.root)

    both = recall.search(QUERY, k=5)

    assert _paths(both) == [TASK_PATH, "Notes/pasta-recipe.md"]
    assert _paths(recall.search(QUERY, k=1)) == [TASK_PATH]


def test_an_episode_without_a_summary_field_gets_its_status_as_the_gist(vault):
    vault.write("Memory/episodes/2026-01-02-worker-gradle-run.md",
                "---\nstatus: fail\nevent_date: 2026-01-02\n---\n\n"
                "# 2026-01-02 — Worker run: gradle daemon\n\n"
                "gradle daemon disappeared during the beta build\n")
    _embed(vault.root)

    card = recall.search(QUERY, k=1).cards[0]

    assert card.title == "2026-01-02 — Worker run: gradle daemon"
    assert card.gist == "status: fail · 2026-01-02"


def test_a_document_with_no_gist_prints_a_dash(vault):
    vault.write("Notes/gradle-note.md", "# Gradle note\n\ngradle daemon disappeared beta build\n")
    _embed(vault.root)

    page = recall.render(QUERY, recall.search(QUERY, k=1), root=vault.root)

    assert "   gist: -" in page.text.splitlines()


def test_gist_comes_from_the_documents_own_summary_field():
    assert rc.gist_for({"description": "D", "summary": "S", "outcome": "O"}) == "D"
    assert rc.gist_for({"summary": "S", "outcome": "O"}) == "S"
    assert rc.gist_for({"outcome": "  two\n  lines  "}) == "two lines"
    assert rc.gist_for({"status": "adopted", "date": "2026-01-03"}) == "status: adopted · 2026-01-03"
    assert rc.gist_for({"status": "pass"}) == "status: pass"
    # A bare date says nothing, and the action fields are never a gist.
    assert rc.gist_for({"event_date": "2026-01-02"}) == ""
    assert rc.gist_for({"next_action": NEXT_ACTION, "if_then": IF_THEN}) == ""
    assert rc.gist_for({}) == ""
    long = rc.gist_for({"outcome": "가" * 400})
    assert len(long) == rc.GIST_MAX_CHARS and long.endswith("…")


# -- privacy boundary ----------------------------------------------------------

def test_body_text_and_action_fields_never_come_out(vault, capsys):
    doc = _write_task(vault.root)
    _embed(vault.root)

    assert recall.main([QUERY]) == 0
    out = capsys.readouterr().out

    assert TASK_TITLE in out and TASK_OUTCOME in out
    for leak in (BODY_TEXT, NEXT_ACTION, IF_THEN):
        assert leak not in out, leak
    # Control: all three are in the document, and the index saw them.
    text = doc.read_text(encoding="utf-8")
    fm, body = gm.parse_frontmatter(text)
    for leak in (BODY_TEXT, NEXT_ACTION, IF_THEN):
        assert leak in text and leak in gm._embed_input_text(doc, fm, body), leak


def test_a_document_marked_private_after_indexing_is_not_returned(vault, capsys):
    """The index is filtered when it is built; a document tagged since then is
    still in it until the next embed tick. The card is refused at read time."""
    note = "Notes/gradle-private-note.md"
    body = "\n# Private gradle note\n\ngradle daemon disappeared during the beta build\n"
    path = vault.write(note, "---\noutcome: PRIVATE-GIST\n---\n" + body)
    _write_filler(vault.root)
    _embed(vault.root)
    assert note in _paths(recall.search(QUERY))             # control: it WAS an answer

    path.write_text("---\noutcome: PRIVATE-GIST\ntags:\n  - pii\n---\n" + body,
                    encoding="utf-8")

    assert note in _indexed(vault.root), "the stale row must still be there"
    assert note not in _paths(recall.search(QUERY, k=recall.MAX_K))
    assert recall.main([QUERY, "--k", str(recall.MAX_K)]) == 0
    out = capsys.readouterr().out
    for leak in ("gradle-private-note", "Private gradle note", "PRIVATE-GIST"):
        assert leak not in out, leak


def test_a_document_under_a_denied_path_is_not_returned_even_when_indexed(vault, monkeypatch):
    """An index built with a weaker filter (here: none) still cannot leak —
    the boundary recall relies on is the read-time one."""
    denied = "Notes/mail/forwarded-gradle-thread.md"
    vault.write(denied, "---\ntitle: Forwarded thread\n---\n\n"
                        "gradle daemon disappeared during the beta build\n")
    _write_task(vault.root)
    with monkeypatch.context() as m:
        m.setattr(gm, "is_embeddable", lambda rel, fm: True)
        _embed(vault.root)

    assert denied in _indexed(vault.root)                   # control: the row is there
    answer = recall.search(QUERY, k=recall.MAX_K)
    assert _paths(answer) == [TASK_PATH]
    assert "Forwarded thread" not in recall.render(QUERY, answer, root=vault.root).text


@pytest.mark.parametrize("segment", gm.EMBED_DENY_PATH_SEGMENTS)
def test_the_card_refuses_every_denied_path_segment(vault, segment):
    rel = f"Notes/{segment}/some-note.md"
    vault.write(rel, "---\ntitle: T\noutcome: O\n---\n\nbody\n")

    assert rc.doc_card(vault.root, "some-note", rel) is None


@pytest.mark.parametrize("tag", gm.EMBED_DENY_TAGS)
def test_the_card_refuses_every_denied_tag(vault, tag):
    rel = "Notes/tagged-note.md"
    vault.write(rel, f"---\ntitle: T\noutcome: O\ntags:\n  - todo\n  - {tag}\n---\n\nbody\n")

    assert rc.doc_card(vault.root, "tagged-note", rel) is None


def test_the_card_boundary_is_the_embed_filter(vault, monkeypatch):
    """One filter, not a copy of it: whatever ``graph_memory.is_embeddable``
    says about the file as it is now decides whether a card exists."""
    rel = "Notes/ordinary-note.md"
    vault.write(rel, "---\ntitle: Ordinary\noutcome: plain\n---\n\nbody\n")
    assert rc.doc_card(vault.root, "ordinary-note", rel) == rc.DocCard(
        slug="ordinary-note", path=rel, title="Ordinary", gist="plain")

    seen = []
    monkeypatch.setattr(gm, "is_embeddable", lambda r, fm: seen.append(r) or False)

    assert rc.doc_card(vault.root, "ordinary-note", rel) is None
    assert seen == [rel]


def test_the_card_needs_a_readable_document_inside_the_vault(vault, tmp_path):
    (tmp_path / "outside-note.md").write_text("---\ntitle: Outside\n---\n", encoding="utf-8")

    assert rc.doc_card(vault.root, "never-written", "Notes/never-written.md") is None
    assert rc.doc_card(vault.root, "outside-note", "../outside-note.md") is None


def test_related_md_is_not_given_the_gist(vault):
    """The card is recall's alone: the engine-written related.md keeps to
    metadata, so a related document's summary field still stays out of it."""
    _write_task(vault.root)
    con = vault.connect()
    try:
        gm.insert_edges(con, [{
            "from_slug": "asking-task", "to_slug": TASK, "edge_type": "depends_on",
            "source_file": "Todo/items/asking-task.md", "event_date": None,
            "recorded_at": "2026-01-01T00:00:00", "valid_until": None,
            "run_id": None, "commit_sha": None}])
    finally:
        con.close()

    task = {"slug": "asking-task", "title": "asking"}
    text = rc.render_related_md(task, rc.related_items(task), root=vault.root)

    assert f"`{TASK}` — {TASK_TITLE}" in text
    assert TASK_OUTCOME not in text


# -- the 4 KB cap --------------------------------------------------------------

def _cards(count: int) -> list:
    return [rc.DocCard(slug=f"doc-{i:02d}",
                       path=f"Todo/items/{'매우-긴-경로-' * 6}doc-{i:02d}.md",
                       title="제목 " + "가나다라마바사" * 60,
                       gist="요지 " + "한글로 쓴 긴 요지 " * 40)
            for i in range(count)]


def test_the_rendered_answer_never_exceeds_the_byte_cap(vault):
    cards = _cards(recall.MAX_K)

    page = recall.render("질문 " * 200, recall.Answer(cards, "hash", ""), root=vault.root)

    size = len(page.text.encode("utf-8"))
    assert size <= recall.MAX_OUTPUT_BYTES == 4096, size
    assert 0 < len(page.paths) < len(cards), "some shown, some cut"
    assert page.paths == [c.path for c in cards[:len(page.paths)]]
    assert f"({len(cards) - len(page.paths)} more not shown: the answer is capped at 4096 bytes)" \
        in page.text.splitlines()
    # Whole blocks only: every shown result still has all three of its lines.
    assert page.text.count("   path: ") == page.text.count("   gist: ") == len(page.paths)
    assert page.text.endswith("name its path in your run log.\n"), "the footer survived"


def test_a_note_from_the_query_layer_counts_against_the_cap_too(vault):
    page = recall.render(QUERY, recall.Answer(_cards(recall.MAX_K), "hash", "n" * 200),
                         root=vault.root)

    assert len(page.text.encode("utf-8")) <= recall.MAX_OUTPUT_BYTES
    assert "note: " + "n" * 200 in page.text.splitlines()


@pytest.mark.parametrize("limit", [0, 1, 50, 300, 1000])
def test_a_limit_too_small_for_the_fixed_lines_is_still_honoured(vault, limit):
    page = recall.render(QUERY, recall.Answer(_cards(3), "hash", ""), root=vault.root,
                         limit=limit)

    assert len(page.text.encode("utf-8")) <= limit
    page.text.encode("utf-8").decode("utf-8")       # cut on a character boundary


def test_the_cli_answer_stays_under_4kb_at_the_largest_k(vault, capsysbinary):
    for i in range(30):
        _write_task(vault.root, f"gradle-daemon-task-{i:02d}",
                    title="베타 빌드 중 gradle 데몬이 멈춘다 " * 12,
                    outcome="다른 런의 gradle stop 에도 베타 빌드가 살아남는다 " * 12)
    _embed(vault.root)

    assert recall.main([QUERY, "--k", "500"]) == 0, "k is clamped, not refused"
    out = capsysbinary.readouterr().out

    assert 0 < len(out) <= recall.MAX_OUTPUT_BYTES, len(out)
    assert b"more not shown" in out


def test_k_is_clamped_to_the_range():
    assert recall.clamp_k(0) == 1
    assert recall.clamp_k(-3) == 1
    assert recall.clamp_k(7) == 7
    assert recall.clamp_k(10_000) == recall.MAX_K
    assert recall.clamp_k("not a number") == recall.DEFAULT_K == 5


# -- vault resolution ----------------------------------------------------------

def test_the_resolved_vault_is_asked_by_default(vault, capsys):
    _write_task(vault.root)
    _embed(vault.root)

    assert recall.main(["gradle", "daemon", "disappeared"]) == 0   # unquoted words join
    out = capsys.readouterr().out

    assert 'for "gradle daemon disappeared"' in out
    assert f"   path: {TASK_PATH}" in out
    assert f"paths are relative to the vault root {vault.root}" in out


def test_vault_root_names_another_vault_index_and_documents(vault, tmp_path, capsys):
    _write_task(vault.root)
    _embed(vault.root)
    other = tmp_path / "other-vault"
    _write_task(other, "other-gradle-task", title="The other vault's gradle task")
    _embed(other)

    assert recall.main([QUERY, "--vault-root", str(other)]) == 0
    out = capsys.readouterr().out

    assert "   path: Todo/items/other-gradle-task.md" in out
    assert TASK_PATH not in out, "read the resolved vault's index instead of the named one"
    assert f"paths are relative to the vault root {other.resolve()}" in out


def test_no_index_is_an_error_not_an_empty_answer(vault, capsys):
    assert not vault.db_path.exists()

    assert recall.main([QUERY]) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err.startswith("macp-recall: no memory index at ")
    assert captured.err.count("\n") == 1
    assert not vault.db_path.exists(), "a lookup must never create the index"


def test_an_index_that_was_never_embedded_answers_with_no_results(vault, capsys):
    vault.connect().close()                 # edges table only, no vectors

    assert recall.main([QUERY]) == 0
    out = capsys.readouterr().out

    assert out.splitlines()[0] == f'recall: no results for "{QUERY}" (embedder: hash)'
    assert "has not been embedded yet" in out


def test_the_index_is_opened_read_only(vault):
    _write_task(vault.root)
    _embed(vault.root)
    before = vault.db_path.read_bytes()

    recall.search(QUERY)

    assert vault.db_path.read_bytes() == before
    assert sorted(p.name for p in vault.memory.iterdir() if p.is_file()) == [
        "relations.sqlite"], "no journal or WAL file left beside the index"


def test_the_embedder_is_resolved_from_the_vault_not_the_cwd(vault, tmp_path, monkeypatch):
    """A stage's shell may stand in another repository's checkout; the index
    belongs to the vault, so the vault's config names the embedder."""
    _write_task(vault.root)
    _embed(vault.root)
    elsewhere = tmp_path / "another-checkout"
    (elsewhere / ".macp").mkdir(parents=True)
    (elsewhere / ".macp" / "config.toml").write_text('embedder = "hash-ko"\n', encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    assert gm.embedder_name() == "hash-ko"                  # control: the cwd says so

    assert recall.search(QUERY, k=1).embedder == "hash"

    (vault.root / ".macp").mkdir()
    (vault.root / ".macp" / "config.toml").write_text('embedder = "hash-ko"\n', encoding="utf-8")
    assert recall.search(QUERY, k=1).embedder == "hash-ko"


def test_an_unknown_embedder_name_is_reported_not_swallowed(vault, monkeypatch, capsys):
    _write_task(vault.root)
    _embed(vault.root)
    monkeypatch.setenv("MACP_EMBEDDER", "no-such-backend")

    assert recall.main([QUERY]) == 1

    assert "no-such-backend" in capsys.readouterr().err


def test_importing_the_module_resolves_no_vault():
    """``graph_memory`` resolves the vault at import; this module must not pull
    it in at the top, or ``main`` could not report a missing vault in one line."""
    tree = ast.parse(Path(recall.__file__).read_text(encoding="utf-8"))
    roots = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"top-level relative import at line {node.lineno}"
            roots.add((node.module or "").split(".")[0])

    assert roots <= set(sys.stdlib_module_names), roots - set(sys.stdlib_module_names)


@pytest.mark.skipif((REPO_ROOT / ".macp" / "config.toml").is_file(),
                    reason="this checkout is itself a vault; nothing is unresolved")
def test_a_missing_vault_is_one_line_not_a_traceback(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if k not in ("MACP_VAULT_ROOT", "PYTHONPATH", recall.LOG_ENV, recall.TASK_ENV)}
    env["PYTHONPATH"] = str(REPO_ROOT)

    out = subprocess.run([sys.executable, "-m", "todo_system.recall", QUERY], cwd=tmp_path,
                         env=env, capture_output=True, text=True, timeout=60)

    assert out.returncode == 1, (out.stdout, out.stderr)
    assert out.stdout == ""
    assert out.stderr.startswith("macp-recall: cannot resolve")
    assert "Traceback" not in out.stderr


# -- recall.log ----------------------------------------------------------------

def _log_lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_each_call_is_appended_to_the_log(vault, tmp_path, monkeypatch, capsys):
    _write_task(vault.root)
    _write_filler(vault.root)
    _embed(vault.root)
    log = tmp_path / "run" / recall.LOG_FILENAME
    log.parent.mkdir()
    monkeypatch.setenv(recall.LOG_ENV, str(log))

    assert recall.main([QUERY, "--k", "1"]) == 0
    assert recall.main(["pasta noodles", "-k", "2"]) == 0
    capsys.readouterr()

    first, second = _log_lines(log)
    assert set(first) == {"ts", "query", "k", "results"}
    assert (first["query"], first["k"], first["results"]) == (QUERY, 1, [TASK_PATH])
    assert (second["query"], second["k"]) == ("pasta noodles", 2)
    assert second["results"][0] == "Notes/pasta-recipe.md"
    # Paths and the question only: no title, no gist.
    raw = log.read_text(encoding="utf-8")
    assert TASK_TITLE not in raw and TASK_OUTCOME not in raw
    assert recall.read_log(log) == [first, second]


def test_nothing_is_logged_without_the_env(vault, tmp_path, capsys):
    _write_task(vault.root)
    _embed(vault.root)

    assert recall.log_path() is None
    assert recall.main([QUERY]) == 0
    capsys.readouterr()

    assert not list(tmp_path.rglob(recall.LOG_FILENAME))


def test_a_failed_call_is_logged_with_its_reason(vault, tmp_path, monkeypatch, capsys):
    log = tmp_path / recall.LOG_FILENAME
    monkeypatch.setenv(recall.LOG_ENV, str(log))

    assert recall.main([QUERY]) == 1                        # no index
    capsys.readouterr()

    (entry,) = _log_lines(log)
    assert entry["results"] == [] and entry["query"] == QUERY
    assert entry["error"].startswith("no memory index at ")


def test_an_unwritable_log_does_not_fail_the_lookup(vault, tmp_path, monkeypatch, capsys):
    _write_task(vault.root)
    _embed(vault.root)
    monkeypatch.setenv(recall.LOG_ENV, str(tmp_path / "no-such-dir" / recall.LOG_FILENAME))

    assert recall.main([QUERY]) == 0
    captured = capsys.readouterr()

    assert f"   path: {TASK_PATH}" in captured.out
    assert captured.err == "macp-recall: call not logged (FileNotFoundError)\n"
    assert not (tmp_path / "no-such-dir").exists()


def test_the_log_records_what_was_shown_not_what_was_cut(vault, tmp_path, monkeypatch, capsys):
    for i in range(30):
        _write_task(vault.root, f"gradle-daemon-task-{i:02d}", title="t " * 200,
                    outcome="다른 런의 gradle stop 에도 베타 빌드가 살아남는다 " * 12)
    _embed(vault.root)
    log = tmp_path / recall.LOG_FILENAME
    monkeypatch.setenv(recall.LOG_ENV, str(log))

    assert recall.main([QUERY, "--k", str(recall.MAX_K)]) == 0
    out = capsys.readouterr().out

    (entry,) = _log_lines(log)
    assert 0 < len(entry["results"]) < recall.MAX_K
    assert len(entry["results"]) == out.count("   path: ")
    assert all(f"   path: {p}\n" in out for p in entry["results"])


def test_the_running_tasks_own_document_is_left_out(vault, monkeypatch):
    _write_task(vault.root)
    _write_filler(vault.root)
    _embed(vault.root)
    assert _paths(recall.search(QUERY, k=1)) == [TASK_PATH]             # control

    assert TASK_PATH not in _paths(recall.search(QUERY, exclude=(TASK,)))

    monkeypatch.setenv(recall.TASK_ENV, TASK)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert recall.main([QUERY]) == 0
    assert TASK_PATH not in out.getvalue() and "Notes/pasta-recipe.md" in out.getvalue()


# -- usage: what a run did with recall -----------------------------------------

def _call(*results: str, **extra) -> dict:
    return {"ts": "2026-01-01T00:00:00+00:00", "query": "q", "k": 5,
            "results": list(results), **extra}


def test_a_run_that_never_called_recall_counts_zero(tmp_path):
    run = tmp_path / "run-without-a-log"
    run.mkdir()
    (run / "run.log").write_text("Todo/items/some-task.md\n", encoding="utf-8")

    assert recall.usage(run) == {"calls": 0, "results": 0, "cited": 0}
    assert recall.usage(tmp_path / "no-such-run") == {"calls": 0, "results": 0, "cited": 0}


def test_calls_results_and_citations_are_counted(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / recall.LOG_FILENAME).write_text("".join(json.dumps(c) + "\n" for c in (
        _call("Todo/items/alpha-task.md", "Memory/episodes/2026-01-02-worker-beta.md"),
        _call("Todo/items/alpha-task.md", "Notes/gamma-note.md"),
        _call(error="no memory index at x"),
    )) + "not json\n[1, 2]\n", encoding="utf-8")
    (run / "run.log").write_text(
        "fixed it the way Todo/items/alpha-task.md did\n", encoding="utf-8")
    (run / "verify.md").write_text(
        "see [[2026-01-02-worker-beta]] for the earlier failure\n", encoding="utf-8")

    assert recall.usage(run) == {"calls": 3, "results": 3, "cited": 2}


def test_a_result_named_only_by_the_engine_or_the_log_itself_is_not_a_citation(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / recall.LOG_FILENAME).write_text(
        json.dumps(_call("Notes/gamma-note.md")) + "\n", encoding="utf-8")
    (run / "related.md").write_text("- `gamma-note` — source: `Notes/gamma-note.md`\n",
                                    encoding="utf-8")
    (run / "report.md").write_text("Notes/gamma-note.md\n", encoding="utf-8")

    assert recall.usage(run) == {"calls": 1, "results": 1, "cited": 0}

    (run / "implementation_plan.md").write_text("per gamma-note.\n", encoding="utf-8")
    assert recall.usage(run)["cited"] == 1


def test_a_slug_inside_a_longer_slug_is_not_a_citation(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / recall.LOG_FILENAME).write_text(
        json.dumps(_call("Todo/items/alpha-task.md")) + "\n", encoding="utf-8")
    (run / "run.log").write_text(
        "branch worker/alpha-task-followup, file pre-alpha-task.md\n", encoding="utf-8")

    assert recall.usage(run)["cited"] == 0

    (run / "run.log").write_text("branch worker/alpha-task\n", encoding="utf-8")
    assert recall.usage(run)["cited"] == 1


def test_the_tasks_own_document_is_never_counted_as_cited(tmp_path):
    """Its slug is in every artifact (the branch, the run id) whether or not
    recall was used, so counting it would make the number say nothing."""
    run = tmp_path / "run"
    run.mkdir()
    (run / recall.LOG_FILENAME).write_text(
        json.dumps(_call("Todo/items/sample-task.md")) + "\n", encoding="utf-8")
    (run / "run.log").write_text("branch: worker/sample-task\n", encoding="utf-8")

    assert recall.usage(run)["cited"] == 1                              # control
    assert recall.usage(run, "sample-task") == {"calls": 1, "results": 1, "cited": 0}


# -- the wrapper ---------------------------------------------------------------

@pytest.mark.skipif(not WRAPPER.is_file(), reason="scripts/macp-recall not in this tree")
def test_the_wrapper_answers_from_any_cwd_and_logs_the_call(vault, tmp_path):
    """The whole chain a stage uses: PATH wrapper → engine interpreter →
    ``python -m todo_system.recall`` → the vault named by the environment."""
    assert os.access(WRAPPER, os.X_OK), "scripts/macp-recall must be 0755"
    _write_task(vault.root)
    _write_filler(vault.root)
    _embed(vault.root)
    # An "engine checkout" whose interpreter is this one and whose package is the
    # tree under test, so the wrapper cannot reach an engine installed on the host.
    engine = tmp_path / "engine"
    (engine / ".venv" / "bin").mkdir(parents=True)
    shim = engine / ".venv" / "bin" / "python"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    (engine / "todo_system").symlink_to(REPO_ROOT / "todo_system", target_is_directory=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    cwd = tmp_path / "some-other-checkout"
    cwd.mkdir()
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "MACP_REPO", "MACP_EMBEDDER", recall.TASK_ENV)}
    env.update({"MACP_ENGINE_ROOT": str(engine), "MACP_VAULT_ROOT": str(vault.root),
                recall.LOG_ENV: str(run_dir / recall.LOG_FILENAME)})

    out = subprocess.run(["bash", str(WRAPPER), QUERY, "--k", "1"], cwd=cwd, env=env,
                         capture_output=True, text=True, timeout=120)

    assert out.returncode == 0, (out.stdout, out.stderr)
    assert out.stdout.splitlines()[:4] == [
        f'recall: 1 result(s) for "{QUERY}" (embedder: hash)',
        f"1. {TASK_TITLE}",
        f"   path: {TASK_PATH}",
        f"   gist: {TASK_OUTCOME}",
    ]
    assert len(out.stdout.encode("utf-8")) <= recall.MAX_OUTPUT_BYTES
    (entry,) = _log_lines(run_dir / recall.LOG_FILENAME)
    assert (entry["query"], entry["k"], entry["results"]) == (QUERY, 1, [TASK_PATH])
    assert sorted(p.name for p in cwd.iterdir()) == [], "nothing written into the cwd"
