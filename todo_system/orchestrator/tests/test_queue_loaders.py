"""Read-only loaders behind /api/status queue_upcoming / queue_blocked."""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from todo_system.orchestrator import tasks

_COLS = ("slug, title, path, outcome, next_action, tags, worker_mode, status, "
         "is_parent, not_before, priority, updated")


class QueueLoadersTest(unittest.TestCase):
    NOW = dt.datetime(2026, 5, 1, 9, 0)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "todo.db"
        con = sqlite3.connect(self.db)
        con.execute(f"CREATE TABLE tasks ({_COLS})")
        con.commit()
        con.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add(self, slug, mode="auto", status="open", not_before=None, fm="",
             priority=None, updated=None):
        path = ""
        if fm is not None:
            p = self.tmp / f"{slug}.md"
            p.write_text(f"---\nworker_mode: {mode}\nstatus: {status}\n{fm}---\n\n# {slug}\n",
                         encoding="utf-8")
            path = str(p)
        con = sqlite3.connect(self.db)
        con.execute(f"INSERT INTO tasks ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (slug, slug.title(), path, "", "", "", mode, status, 0,
                     not_before, priority, updated))
        con.commit()
        con.close()

    def test_future_not_before_is_upcoming_not_dispatchable(self):
        self._add("later", not_before="2026-05-02 10:00")
        self._add("past", not_before="2026-04-30")
        disp = {t.slug for t in tasks.load_dispatchable(self.db, now=self.NOW)}
        up = [r["slug"] for r in tasks.load_upcoming(self.db, now=self.NOW)]
        self.assertEqual(disp, {"past"})
        self.assertEqual(up, ["later"])

    def test_upcoming_is_time_sorted_and_keeps_the_written_value(self):
        self._add("c", not_before="2026-05-03")
        self._add("a", not_before="2026-05-01T12:30")
        self._add("b", not_before="2026-05-02 08:00")
        rows = tasks.load_upcoming(self.db, now=self.NOW)
        self.assertEqual([r["slug"] for r in rows], ["a", "b", "c"])
        self.assertEqual(rows[0]["not_before"], "2026-05-01T12:30")
        self.assertEqual(set(rows[0]), {"slug", "title", "path", "not_before"})

    def test_upcoming_skips_non_dispatchable_modes(self):
        self._add("man", mode="manual", not_before="2026-06-01")
        self._add("blk", mode="blocked", not_before="2026-06-01")
        self.assertEqual(tasks.load_upcoming(self.db, now=self.NOW), [])

    def test_upcoming_skips_md_closed_while_db_open(self):
        self._add("closed", status="done", not_before="2026-06-01")   # md says done …
        self._add("open-later", not_before="2026-06-01")
        con = sqlite3.connect(self.db)
        con.execute("UPDATE tasks SET status='open' WHERE slug='closed'")  # … db lags
        con.commit()
        con.close()
        up = [r["slug"] for r in tasks.load_upcoming(self.db, now=self.NOW)]
        self.assertEqual(up, ["open-later"])

    def test_blocked_waiting_on_lists_only_undone_predecessors(self):
        self._add("done-one", status="done")
        self._add("open-one")
        self._add("md-done", status="done")       # md says done …
        con = sqlite3.connect(self.db)
        con.execute("UPDATE tasks SET status='open' WHERE slug='md-done'")   # … db lags
        con.commit()
        con.close()
        self._add("child", mode="blocked",
                  fm='depends_on:\n  - "[[done-one]]"\n  - open-one\n  - md-done\n  - ghost\n')
        [row] = tasks.load_blocked(self.db)
        self.assertEqual(row["slug"], "child")
        self.assertEqual(row["waiting_on"], ["open-one", "ghost"])

    def test_blocked_without_depends_on_is_absent(self):
        self._add("lonely", mode="blocked")
        self._add("autoish", mode="auto", fm="depends_on:\n  - x\n")
        self.assertEqual(tasks.load_blocked(self.db), [])

    def test_blocked_all_done_keeps_an_empty_list(self):
        self._add("up", status="done")
        self._add("child", mode="blocked", fm="depends_on:\n  - up\n")
        [row] = tasks.load_blocked(self.db)
        self.assertEqual(row["waiting_on"], [])

    def test_task_meta(self):
        self._add("p", priority=70.0, updated="2026-04-30")
        self._add("q")
        meta = tasks.task_meta(self.db)
        self.assertEqual(meta["p"], {"priority": 70.0, "updated": "2026-04-30"})
        self.assertEqual(meta["q"], {"priority": None, "updated": ""})

    def test_task_meta_tolerates_an_old_schema(self):
        old = self.tmp / "old.db"
        con = sqlite3.connect(old)
        con.execute("CREATE TABLE tasks (slug, title)")
        con.execute("INSERT INTO tasks VALUES ('s', 'S')")
        con.commit()
        con.close()
        self.assertEqual(tasks.task_meta(old), {"s": {"priority": None, "updated": ""}})


if __name__ == "__main__":
    unittest.main()
