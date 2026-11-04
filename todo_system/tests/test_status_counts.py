"""metrics._db_counts and GitTodoCacheReader.counts count both cancel spellings
(`cancelled` / `canceled`, any case) as cancelled, and agree with each other."""

from __future__ import annotations

import sqlite3
from unittest import mock

from todo_system.orchestrator import metrics
from todo_system.orchestrator.state_store import GitTodoCacheReader


def _db(tmp_path):
    db = tmp_path / "todo.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tasks (slug TEXT, status TEXT, worker_mode TEXT)")
    con.executemany(
        "INSERT INTO tasks VALUES (?, ?, ?)",
        [
            ("t-done", "done", "auto"),
            ("t-c2", "cancelled", "auto"),
            ("t-c1", "canceled", "manual"),
            ("t-c1-cap", "Canceled", "auto"),
            ("t-open", "open", "auto"),
        ],
    )
    con.commit()
    con.close()
    return db


def test_cache_reader_counts_both_spellings(tmp_path):
    counts = GitTodoCacheReader(_db(tmp_path)).counts()
    assert counts == {"done": 1, "auto_done": 1, "cancelled": 3}


def test_metrics_counts_both_spellings_and_match_cache_reader(tmp_path):
    db = _db(tmp_path)
    with mock.patch.object(metrics, "DB", db):
        counts = metrics._db_counts()
    assert counts["cancelled"] == 3
    assert counts == GitTodoCacheReader(db).counts()
