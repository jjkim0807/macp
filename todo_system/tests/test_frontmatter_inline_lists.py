"""One-line YAML flow lists (``key: [a, b]``) read the same as block lists.

Before this, every hand-rolled ``parse_frontmatter`` kept ``tags: [todo, work]``
as the single string ``"[todo, work]"`` and the dashboard rendered a ``[todo``
chip. The shared rule lives in ``todo_system.frontmatter_lists``.
"""

from __future__ import annotations

import pytest

from todo_system.frontmatter_lists import parse_inline_list


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
