"""The one definition of "this task is closed".

A task md may record a cancelled task as `cancelled` (two l's, the engine's own
spelling) or `canceled` (one l, the US spelling a human or an external tracker
writes). Both mean closed. Before this module every caller kept its own copy of
the set, and several copies knew only `cancelled` — so a `canceled` task's failed
queue entry was auto-retried, and dashboard/metrics counts left it out.

This is a dependency-free leaf on purpose: `tasks` imports `repos` and
`orchestrator.orchestrator`, and `orchestrator.orchestrator` imports
`project_stall`, so none of those can import `tasks` for the set without a cycle.
They (and `worker`, `metrics`, `state_store`, the dashboard) import this module
instead; `tasks` re-exports the names as the public `tasks.CLOSED_STATUSES` /
`tasks.is_closed`.

`done`-only checks ("did it finish?", the `done` count) are a different question
and do not use this.
"""

from __future__ import annotations

# Ordered tuples are the source: a frozenset has no stable iteration order, and
# the SQL fragments below must render byte-identically every run.
_CLOSED_ORDER = ("done", "cancelled", "canceled")
_CANCELLED_ORDER = ("cancelled", "canceled")

CLOSED_STATUSES = frozenset(_CLOSED_ORDER)
CANCELLED_STATUSES = frozenset(_CANCELLED_ORDER)


def _sql_list(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"


# For `LOWER(status) IN <...>` — "('done', 'cancelled', 'canceled')".
CLOSED_STATUSES_SQL = _sql_list(_CLOSED_ORDER)
CANCELLED_STATUSES_SQL = _sql_list(_CANCELLED_ORDER)


def _norm(status: object) -> str:
    return str(status or "").strip().lower()


def is_closed(status: object) -> bool:
    """True for done / cancelled / canceled (case- and whitespace-insensitive).
    None, "" and any open state are not closed."""
    return _norm(status) in CLOSED_STATUSES


def is_cancelled(status: object) -> bool:
    """True for cancelled / canceled (case- and whitespace-insensitive)."""
    return _norm(status) in CANCELLED_STATUSES
