"""One-line YAML flow lists (``key: [a, b]``) for the hand-rolled frontmatter parsers.

The engine has several small line-based ``parse_frontmatter`` functions
(``cli``, ``worker``, ``session_log``, ``graph_memory``, ``trackers.base``).
They all understand block lists::

    tags:
      - todo
      - work

but used to keep a one-line list ``tags: [todo, work]`` as the single string
``"[todo, work]"`` — which then surfaced as a ``[todo`` chip on the dashboard.
Every one of those parsers calls :func:`parse_inline_list` on the raw scalar
value before it strips quotes, so they all share one rule.

This module is a leaf: it imports nothing from ``todo_system`` (several callers
are stdlib-only / decoupled modules), only the standard library.
"""

from __future__ import annotations

_QUOTES = "\"'"


def _split_outside_quotes(inner: str) -> list[str]:
    """Split ``inner`` on commas that are not inside a ``"…"`` / ``'…'`` pair.

    An unterminated quote swallows the rest of the text as literal content.
    """
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in inner:
        if quote is not None:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in _QUOTES:
            quote = ch
            buf.append(ch)
        elif ch == ",":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def parse_inline_list(value: str) -> list[str] | None:
    """Return the items of a one-line flow list, or ``None`` if ``value`` is a scalar.

    Rule (applied to the raw value, BEFORE any quote stripping): the whole
    value, after trimming surrounding whitespace, must start with ``[`` and end
    with ``]``, and must NOT start with ``[[``. Only then is it a list;
    everything else returns ``None`` and the caller keeps its scalar handling.

    - ``[[`` is excluded so wikilink scalars stay strings: ``parent: [[x]]``,
      ``next_action: [[note]] read``.  A list of wikilinks must quote its
      items: ``depends_on: ["[[a]]", "[[b]]"]``.  (An unquoted ``[[[a]]]``
      therefore stays a string — intended.)
    - A quoted value is a YAML string, so ``tags: "[a, b]"`` stays the string
      ``[a, b]`` (the check sees the leading ``"``).
    - Items are split on commas outside quotes (``["a, b", c]`` → ``['a, b', 'c']``),
      trimmed, and one matching pair of surrounding quotes is removed, so the
      result equals what an unquoted block list yields.  Empty items
      (``[a, , b]``, a trailing comma) are dropped; ``[]`` / ``[ ]`` → ``[]``.
    - Not supported (out of scope): nested flow lists (``[a, [b]]``), flow
      mappings, inline ``# comments`` after the closing bracket (such a value
      does not end with ``]`` and stays a string).

    Examples::

        "[todo, work]"   -> ['todo', 'work']
        '["a", \\'b\\']'    -> ['a', 'b']
        "[]"             -> []
        '"[a, b]"'       -> None   (quoted → string)
        "[WIP] title"    -> None   (does not end with ']')
        '"a [b] c"'      -> None
        "[[note]] read"  -> None   (starts with '[[')
        "[[x]]"          -> None   (starts with '[[')
    """
    v = value.strip()
    if len(v) < 2 or not v.startswith("[") or not v.endswith("]") or v.startswith("[["):
        return None
    items: list[str] = []
    for raw in _split_outside_quotes(v[1:-1]):
        s = raw.strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in _QUOTES:
            s = s[1:-1].strip()
        if s:
            items.append(s)
    return items
