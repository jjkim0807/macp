"""One-line YAML flow lists (``key: [a, b]``) read the same as block lists.

Before this, every hand-rolled ``parse_frontmatter`` kept ``tags: [todo, work]``
as the single string ``"[todo, work]"`` and the dashboard rendered a ``[todo``
chip. The shared rule lives in ``todo_system.frontmatter_lists``.
"""

from __future__ import annotations

import pytest

from todo_system import cli, graph_memory, session_log, worker
from todo_system.frontmatter_lists import parse_inline_list
from todo_system.orchestrator import repos


# ---------------------------------------------------------------------------
# Helper unit table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("[todo, work, dripcoffee]", ["todo", "work", "dripcoffee"]),
        ("[\"a\", 'b']", ["a", "b"]),
        ("[]", []),
        ("[ ]", []),
        ("  [a, b]  ", ["a", "b"]),
        ("[a, , b,]", ["a", "b"]),
        ('["a, b", c]', ["a, b", "c"]),
        ('["[[a]]", "[[b]]"]', ["[[a]]", "[[b]]"]),
        ('[" a ", b]', ["a", "b"]),
        ("[single]", ["single"]),
    ],
)
def test_parse_inline_list_lists(raw: str, expected: list[str]) -> None:
    assert parse_inline_list(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        '"[a, b]"',  # quoted → a YAML string
        "'[a, b]'",
        "[WIP] title",
        '"[WIP] title"',
        '"a [b] c"',
        "a [b] c",
        "[[note]] read",  # wikilink scalar
        "[[x]]",
        "[[[a]]]",  # documented: unquoted wikilink list stays a string
        "plain",
        "",
        "[",
        "]",
        "[a, b] # comment",  # inline comments are out of scope
    ],
)
def test_parse_inline_list_scalars_return_none(raw: str) -> None:
    assert parse_inline_list(raw) is None


# ---------------------------------------------------------------------------
# Parser table — every frontmatter parser path, one shared table
# ---------------------------------------------------------------------------


def _repos_yaml(content: str) -> dict:
    block = repos._frontmatter_block(content)
    assert block is not None
    fm, reason = repos._parse_frontmatter_block(block)
    assert reason is None, reason
    return fm


PARSERS = [
    pytest.param(lambda c: cli.parse_frontmatter(c)[0], id="cli"),
    pytest.param(lambda c: worker.parse_frontmatter(c)[0], id="worker"),
    pytest.param(lambda c: session_log.parse_frontmatter(c)[0], id="session_log"),
    pytest.param(lambda c: graph_memory.parse_frontmatter(c)[0], id="graph_memory"),
    pytest.param(_repos_yaml, id="repos_yaml"),
]
HAND_PARSERS = PARSERS[:4]


def _doc(*lines: str) -> str:
    # The hand-rolled parsers need a leading "---\n" and split on "\n---\n";
    # repos._frontmatter_block accepts the same shape.
    return "---\n" + "\n".join(lines) + "\n---\n# T\n"


# Block fixtures use UNQUOTED items on purpose: cli does not
# strip quotes off block-list items, so a quoted block item would differ from
# the inline form for reasons unrelated to this change.
LIST_CASES = [
    pytest.param(
        "tags",
        "tags: [todo, work, dripcoffee]",
        ["tags:", "  - todo", "  - work", "  - dripcoffee"],
        ["todo", "work", "dripcoffee"],
        id="tags",
    ),
    pytest.param(
        "depends_on",
        "depends_on: [\"a\", 'b']",
        ["depends_on:", "  - a", "  - b"],
        ["a", "b"],
        id="depends_on-quoted",
    ),
]


@pytest.mark.parametrize("key, inline, block, expected", LIST_CASES)
@pytest.mark.parametrize("parse", PARSERS)
def test_inline_list_equals_block_list(parse, key, inline, block, expected) -> None:
    inline_fm = parse(_doc(inline, "status: open"))
    block_fm = parse(_doc(*block, "status: open"))
    assert inline_fm[key] == block_fm[key] == expected
    # The key after a one-line list must still be read.
    assert inline_fm["status"] == "open"
    assert block_fm["status"] == "open"


@pytest.mark.parametrize("parse", PARSERS)
def test_empty_inline_list_all_parsers(parse) -> None:
    # Not compared against an empty block (`tags:` with no items): YAML reads
    # that as None, the hand-rolled parsers as []. Only `[]` has a contract.
    fm = parse(_doc("tags: []", "status: open"))
    assert fm["tags"] == []
    assert fm["status"] == "open"


SCALAR_CASES_ALL = [
    pytest.param("title", 'title: "[WIP] 제목"', "[WIP] 제목", id="wip-title"),
    pytest.param("outcome", 'outcome: "a [b] c"', "a [b] c", id="bracket-mid"),
    pytest.param("tags", 'tags: "[a, b]"', "[a, b]", id="quoted-flow"),
]


@pytest.mark.parametrize("key, line, expected", SCALAR_CASES_ALL)
@pytest.mark.parametrize("parse", PARSERS)
def test_bracket_scalars_stay_strings(parse, key, line, expected) -> None:
    fm = parse(_doc(line, "status: open"))
    assert isinstance(fm[key], str)
    assert fm[key] == expected
    assert fm["status"] == "open"


# Hand-rolled parsers only: in YAML `[[링크]] 읽기` is a parse error and an
# unquoted `[[x]]` is a nested list, so repos_yaml has no contract for these.
SCALAR_CASES_HAND = [
    pytest.param(
        "next_action", "next_action: [[링크]] 읽기", "[[링크]] 읽기", id="wikilink-lead"
    ),
    pytest.param("parent", "parent: [[x]]", "[[x]]", id="wikilink-bare"),
    pytest.param("parent", 'parent: "[[x]]"', "[[x]]", id="wikilink-quoted"),
]


@pytest.mark.parametrize("key, line, expected", SCALAR_CASES_HAND)
@pytest.mark.parametrize("parse", HAND_PARSERS)
def test_wikilink_scalars_stay_strings(parse, key, line, expected) -> None:
    fm = parse(_doc(line, "status: open"))
    assert isinstance(fm[key], str)
    assert fm[key] == expected
    assert fm["status"] == "open"


# ---------------------------------------------------------------------------
# Per-path wiring: the parsed list reaches the real consumer
# ---------------------------------------------------------------------------


def test_cli_parse_task_file_inline_tags_equal_block(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    items = tmp_path / "Todo" / "items"
    items.mkdir(parents=True)
    inline = items / "x.md"
    inline.write_text(
        _doc("tags: [todo, work, dripcoffee]", "status: open"), encoding="utf-8"
    )
    block = items / "y.md"
    block.write_text(
        _doc("tags:", "  - todo", "  - work", "  - dripcoffee", "status: open"),
        encoding="utf-8",
    )
    inline_tags = cli.parse_task_file(inline).tags
    block_tags = cli.parse_task_file(block).tags
    assert inline_tags == block_tags == ["todo", "work", "dripcoffee"]
    for tag in inline_tags:
        assert not tag.startswith("[") and not tag.endswith("]")


def test_session_log_round_trip_writes_block_list() -> None:
    doc = _doc("project: demo", "tags: [todo, work]", "status: open")
    fm, body = session_log.parse_frontmatter(doc)
    assert fm["tags"] == ["todo", "work"]
    text = session_log._serialize_frontmatter(fm)
    assert "  - todo" in text
    assert "[todo" not in text
    fm2, body2 = session_log.parse_frontmatter(text + body)
    assert fm2 == fm
    assert body2 == body


def test_graph_memory_inline_depends_on_makes_edges(tmp_path) -> None:
    note = tmp_path / "task.md"
    note.write_text(_doc("depends_on: [a, b]", "status: open"), encoding="utf-8")
    edges = graph_memory.edges_from_file(note, vault_root=tmp_path)
    targets = {e["to_slug"] for e in edges if e["edge_type"] == "depends_on"}
    assert targets == {"a", "b"}


def test_repos_depends_on_from_inline_yaml() -> None:
    assert repos.task_depends_on_from_fm(_repos_yaml(_doc("depends_on: [a, b]"))) == [
        "a",
        "b",
    ]
    # Bare strings stay "nothing declared" (the dispatcher/validator contract).
    assert repos._str_list("[a, b]") == []
    assert repos._str_list("a") == []
