"""The shared closed-status definition (`orchestrator.statuses`, re-exported as
`tasks.CLOSED_STATUSES` / `tasks.is_closed`): both cancel spellings are closed,
and the SQL fragments render exactly the literal the queries used before."""

from __future__ import annotations

import sqlite3

import pytest

from todo_system.orchestrator import statuses, tasks


@pytest.mark.parametrize("status", ["done", "cancelled", "canceled", " Canceled ", "DONE", "Cancelled\n"])
def test_is_closed_true(status):
    assert statuses.is_closed(status)


@pytest.mark.parametrize("status", ["open", "waiting", "blocked", "", "-", None])
def test_is_closed_false(status):
    assert not statuses.is_closed(status)


def test_is_cancelled_both_spellings_only():
    assert statuses.is_cancelled("cancelled")
    assert statuses.is_cancelled("Canceled")
    assert not statuses.is_cancelled("done")
    assert not statuses.is_cancelled(None)


def test_sql_fragments_match_previous_literals():
    assert statuses.CLOSED_STATUSES_SQL == "('done', 'cancelled', 'canceled')"
    assert statuses.CANCELLED_STATUSES_SQL == "('cancelled', 'canceled')"


def test_tasks_reexports_the_one_definition():
    assert tasks.CLOSED_STATUSES is statuses.CLOSED_STATUSES
    assert tasks.is_closed is statuses.is_closed
    assert tasks.is_cancelled is statuses.is_cancelled
    assert tasks.CLOSED_STATUSES == frozenset({"done", "cancelled", "canceled"})


def _db(tmp_path, rows):
    db = tmp_path / "todo.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE tasks (slug TEXT, title TEXT, status TEXT, parent_slug TEXT, is_parent INTEGER)"
    )
    con.executemany("INSERT INTO tasks VALUES (?, ?, ?, ?, ?)", rows)
    con.commit()
    con.close()
    return db


def test_task_queries_treat_both_spellings_as_closed(tmp_path):
    db = _db(
        tmp_path,
        [
            ("pa", "Parent A", "open", None, 1),
            ("pa-1", "a1", "done", "pa", 0),
            ("pa-2", "a2", "canceled", "pa", 0),
            ("pa-3", "a3", "Cancelled", "pa", 0),
            ("pb", "Parent B", "open", None, 1),
            ("pb-1", "b1", "canceled", "pb", 0),
            ("pb-2", "b2", "open", "pb", 0),
        ],
    )
    assert tasks.child_status_rollup("pa", db) == {"n": 3, "closed": 3, "all_closed": True}
    assert tasks.child_status_rollup("pb", db) == {"n": 2, "closed": 1, "all_closed": False}
    assert tasks.parents_with_open_children(db) == {"pb"}
    assert [r["slug"] for r in tasks.parents_needing_review(db)] == ["pa"]
