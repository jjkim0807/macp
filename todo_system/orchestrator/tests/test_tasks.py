"""Tests for the TaskGraph loader (P5)."""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from todo_system.orchestrator import tasks

_COLS = "slug, title, path, outcome, next_action, tags, worker_mode, status, is_parent"


def _make_db(dirpath: Path) -> Path:
    p = dirpath / "todo.db"
    con = sqlite3.connect(p)
    con.execute(f"CREATE TABLE tasks ({_COLS})")
    con.executemany(
        f"INSERT INTO tasks ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("t-auto", "Auto leaf", "", "ship X", "do next", "todo,work", "auto", "open", 0),
            ("t-manual", "Manual", "", "", "", "", "manual", "open", 0),
            ("t-parent", "Parent", "", "", "", "", "auto", "open", 1),
            ("t-done", "Closed", "", "", "", "", "auto", "done", 0),
        ],
    )
    con.commit()
    con.close()
    return p


class LoadDispatchableTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = _make_db(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_only_open_nonparent_auto(self):
        got = tasks.load_dispatchable(self.db)
        self.assertEqual([t.slug for t in got], ["t-auto"])  # manual/parent/done excluded

    def test_payload_fields(self):
        [t] = tasks.load_dispatchable(self.db)
        self.assertEqual(t.payload["summary"], "Auto leaf")
        self.assertEqual(t.payload["outcome"], "ship X")
        self.assertEqual(t.payload["branch"], "worker/t-auto")
        self.assertEqual(t.payload["tags"], ["todo", "work"])

    def test_only_auto_false_includes_manual(self):
        slugs = {t.slug for t in tasks.load_dispatchable(self.db, only_auto=False)}
        self.assertEqual(slugs, {"t-auto", "t-manual"})  # still excludes parent/done

    def test_delegate_mode_is_dispatchable(self):
        con = sqlite3.connect(self.db)
        con.execute(f"INSERT INTO tasks ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("t-deleg", "Research task", "", "", "", "", "delegate", "open", 0))
        con.commit()
        con.close()
        by_slug = {t.slug: t for t in tasks.load_dispatchable(self.db)}
        self.assertIn("t-deleg", by_slug)                       # delegate dispatched
        self.assertIn("t-auto", by_slug)
        self.assertEqual(by_slug["t-deleg"].payload["worker_mode"], "delegate")

    def test_quick_mode_is_dispatchable(self):
        # quick is a real single-stage pipeline; it must auto-dispatch or quick
        # tasks (e.g. lint cleanups) orphan forever and the worker starves.
        con = sqlite3.connect(self.db)
        con.execute(f"INSERT INTO tasks ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("t-quick", "Lint cleanup", "", "", "", "", "quick", "open", 0))
        con.commit()
        con.close()
        by_slug = {t.slug: t for t in tasks.load_dispatchable(self.db)}
        self.assertIn("t-quick", by_slug)
        self.assertEqual(by_slug["t-quick"].payload["worker_mode"], "quick")

    def test_external_repo_pulled_from_frontmatter(self):
        # a task whose markdown declares an external repo carries it in payload
        md = self.tmp / "ext.md"
        md.write_text('---\nstatus: open\nworker_mode: auto\n'
                      'repo: "https://h/x/ext-proj.git"\n---\n# ext\n')
        con = sqlite3.connect(self.db)
        con.execute(f"INSERT INTO tasks ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("ext", "Ext task", str(md), "", "", "", "auto", "open", 0))
        con.commit()
        con.close()
        by_slug = {t.slug: t for t in tasks.load_dispatchable(self.db)}
        self.assertEqual(by_slug["ext"].payload["repo"], "https://h/x/ext-proj.git")
        self.assertNotIn("repo", by_slug["t-auto"].payload)  # vault task, no repo

    def test_model_tier_and_estimate_pulled_from_frontmatter(self):
        # the tier classifier's inputs ride the payload; absent fields stay absent
        md = self.tmp / "tiered.md"
        md.write_text('---\nstatus: open\nworker_mode: auto\nmodel_tier: Light\n'
                      'estimate_p90: 4h\n---\n# t\n')
        con = sqlite3.connect(self.db)
        con.execute(f"INSERT INTO tasks ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("tiered", "Tiered", str(md), "", "", "", "auto", "open", 0))
        con.commit()
        con.close()
        by_slug = {t.slug: t for t in tasks.load_dispatchable(self.db)}
        self.assertEqual(by_slug["tiered"].payload["model_tier"], "light")
        self.assertEqual(by_slug["tiered"].payload["estimate_p90"], "4h")
        self.assertNotIn("model_tier", by_slug["t-auto"].payload)
        self.assertNotIn("estimate_p90", by_slug["t-auto"].payload)

    def test_md_blocked_overrides_db_auto(self):
        # DISCRIMINATOR: md<->DB drift. The markdown was hand-edited to `blocked`
        # but todo.db still carries the stale `auto` row (the todo-sync daemon
        # hasn't re-synced yet). The loader must re-read the markdown and skip.
        # WITHOUT the guard this row dispatches (db says auto), so this is the
        # only test that fails on merge-base — the fail-open cases below pass
        # vacuously via the None fall-through and cannot prove the guard fires.
        md = self.tmp / "drift.md"
        md.write_text("---\nstatus: open\nworker_mode: blocked\n---\n# drift\n")
        con = sqlite3.connect(self.db)
        con.execute(f"INSERT INTO tasks ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("drift", "Drifted task", str(md), "", "", "", "auto", "open", 0))
        con.commit()
        con.close()
        slugs = {t.slug for t in tasks.load_dispatchable(self.db)}
        self.assertNotIn("drift", slugs)   # md `blocked` wins over stale db `auto`
        self.assertIn("t-auto", slugs)     # normal path unaffected

    def test_md_bare_off_overrides_db_auto(self):
        # An unquoted `off` loads as YAML boolean False; it used to read as
        # "absent" here, so the stale db `auto` row dispatched anyway.
        md = self.tmp / "off.md"
        md.write_text("---\nstatus: open\nworker_mode: off\n---\n# off\n")
        con = sqlite3.connect(self.db)
        con.execute(f"INSERT INTO tasks ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("bare-off", "Bare off task", str(md), "", "", "", "auto", "open", 0))
        con.commit()
        con.close()
        slugs = {t.slug for t in tasks.load_dispatchable(self.db)}
        self.assertNotIn("bare-off", slugs)
        self.assertIn("t-auto", slugs)

    def test_md_auto_agrees_db_auto_dispatched(self):
        # Positive control: md and DB both say `auto` — the guard must not touch
        # the normal path.
        md = self.tmp / "agree.md"
        md.write_text("---\nstatus: open\nworker_mode: auto\n---\n# agree\n")
        con = sqlite3.connect(self.db)
        con.execute(f"INSERT INTO tasks ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("agree", "Agreeing task", str(md), "", "", "", "auto", "open", 0))
        con.commit()
        con.close()
        slugs = {t.slug for t in tasks.load_dispatchable(self.db)}
        self.assertIn("agree", slugs)

    def test_missing_md_fails_open(self):
        # Fail-open: path is set but the file does not exist → read_task_worker_mode
        # returns None → trust the DB, do NOT skip. (Passes vacuously pre-guard.)
        con = sqlite3.connect(self.db)
        con.execute(f"INSERT INTO tasks ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("ghost", "Missing md", str(self.tmp / "nope.md"),
                     "", "", "", "auto", "open", 0))
        con.commit()
        con.close()
        slugs = {t.slug for t in tasks.load_dispatchable(self.db)}
        self.assertIn("ghost", slugs)   # an unreadable md never blocks dispatch

    def test_md_without_worker_mode_fails_open(self):
        # Fail-open: md exists but names no worker_mode → None → trust the DB.
        md = self.tmp / "nomode.md"
        md.write_text("---\nstatus: open\n---\n# no mode\n")
        con = sqlite3.connect(self.db)
        con.execute(f"INSERT INTO tasks ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                    ("nomode", "No mode md", str(md), "", "", "", "auto", "open", 0))
        con.commit()
        con.close()
        slugs = {t.slug for t in tasks.load_dispatchable(self.db)}
        self.assertIn("nomode", slugs)

    def _insert_md(self, slug: str, fm: str) -> None:
        md = self.tmp / f"{slug}.md"
        md.write_text(f"---\n{fm}\n---\n# {slug}\n")
        con = sqlite3.connect(self.db)
        con.execute(f"INSERT INTO tasks ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                    (slug, slug, str(md), "", "", "", "auto", "open", 0))
        con.commit()
        con.close()

    def test_md_done_overrides_db_open(self):
        # DISCRIMINATOR: md<->DB status drift. The markdown says `status: done`
        # (closed by a /focus session — 2026-10-30 app-plugins-11) but todo.db
        # still carries the stale `open` row. WITHOUT the md status gate this
        # row dispatches, since db status and worker_mode both pass.
        for i, status in enumerate(("done", "cancelled", "canceled", '"Done"')):
            self._insert_md(f"closed-{i}", f"status: {status}\nworker_mode: auto")
        for only_auto in (True, False):     # closed is closed in every mode
            with self.subTest(only_auto=only_auto):
                slugs = {t.slug for t in
                         tasks.load_dispatchable(self.db, only_auto=only_auto)}
                self.assertFalse({s for s in slugs if s.startswith("closed-")}, slugs)
                self.assertIn("t-auto", slugs)   # normal path unaffected

    def test_md_without_status_fails_open(self):
        # Fail-open: md names no status → trust the DB's `open`.
        self._insert_md("nostatus", "worker_mode: auto")
        slugs = {t.slug for t in tasks.load_dispatchable(self.db)}
        self.assertIn("nostatus", slugs)


class MarkDoneTest(unittest.TestCase):
    """Closing a task must land in BOTH the markdown and todo.db.

    Regression: mark_done wrote the markdown but silently rolled back the DB.
    cli.db_connect leaves sqlite3's default isolation_level, so refresh_single_task
    ran inside an implicit transaction that was never committed — dropping the
    connection rolled it back. todo.db is the v2 dispatch source, so the task stayed
    `open` there and was re-dispatched onto an empty diff. The un-closed connection
    also kept a write lock, failing the next writer with "database is locked".
    """

    def setUp(self):
        from todo_system import cli

        self.tmp = Path(tempfile.mkdtemp())
        self.items = self.tmp / "items"
        self.items.mkdir()
        (self.items / "t-close.md").write_text(
            "---\ntags:\n  - todo\nstatus: open\nupdated: 2026-07-01\n"
            'outcome: "ship it"\nnext_action: "go"\n---\n'
            "# Test task\n- [ ] step 1\n- [ ] step 2\n", encoding="utf-8")
        self.db = self.tmp / "todo.db"
        # parse_task_file stores path.relative_to(cli.ROOT); point ROOT at the
        # sandbox so the fixture task looks like a vault task.
        self._root_patch = patch.object(cli, "ROOT", self.tmp)
        self._root_patch.start()

    def tearDown(self):
        self._root_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _status(self):
        con = sqlite3.connect(self.db)
        try:
            row = con.execute("SELECT status FROM tasks WHERE slug='t-close'").fetchone()
            return row[0] if row else None
        finally:
            con.close()

    def test_marks_markdown_done(self):
        self.assertTrue(tasks.mark_done("t-close", items_dir=self.items, db_path=self.db))
        text = (self.items / "t-close.md").read_text(encoding="utf-8")
        self.assertIn("status: done", text)
        self.assertIn("- [x]", text)            # checkboxes completed
        self.assertNotIn("- [ ]", text)

    def test_mark_done_persists_status_to_db(self):
        self.assertTrue(tasks.mark_done("t-close", items_dir=self.items, db_path=self.db))
        self.assertEqual(self._status(), "done")   # was 'open': silently rolled back

    def test_mark_done_releases_the_write_lock(self):
        tasks.mark_done("t-close", items_dir=self.items, db_path=self.db)
        con = sqlite3.connect(self.db, timeout=2)   # raised "database is locked"
        try:
            con.execute("UPDATE tasks SET priority=1 WHERE slug='t-close'")
            con.commit()
        finally:
            con.close()

    def test_missing_task_is_noop(self):
        self.assertFalse(tasks.mark_done("nope", items_dir=self.items, db_path=self.db))

    def test_close_trace_lands_with_the_status(self):
        # The trace is what lets reconcile_closed tell a human reopen from a lost
        # close when the reopened bytes would otherwise equal the pre-close ones
        # (no boxes to un-check, `updated:` already today). It rides the SAME
        # write as `status: done`, so a lost close loses both together.
        self.assertTrue(tasks.mark_done("t-close", items_dir=self.items, db_path=self.db))
        text = (self.items / "t-close.md").read_text(encoding="utf-8")
        self.assertIn("## Progress Log", text)           # created: the fixture had none
        self.assertEqual(text.splitlines()[-1],
                         f"- {dt.date.today().isoformat()}: closed by orchestrator")
        self.assertEqual(self._status(), "done")

    def test_an_already_done_task_gets_no_trace(self):
        # Re-closing what a human already closed ("todo already closed") must not
        # claim the orchestrator closed it.
        path = self.items / "t-close.md"
        path.write_text(path.read_text(encoding="utf-8").replace("status: open", "status: done"),
                        encoding="utf-8")
        self.assertTrue(tasks.mark_done("t-close", items_dir=self.items, db_path=self.db))
        self.assertNotIn("closed by orchestrator", path.read_text(encoding="utf-8"))

    def test_an_md_without_frontmatter_gets_no_trace(self):
        # No frontmatter means no status was written, so there is no close to trace.
        path = self.items / "t-close.md"
        path.write_text("# bare task\n", encoding="utf-8")
        tasks.mark_done("t-close", items_dir=self.items, db_path=self.db)
        self.assertNotIn("closed by orchestrator", path.read_text(encoding="utf-8"))

    def test_status_lands_even_if_the_checkbox_pass_fails(self):
        # Status + trace go down in one write BEFORE the boxes, so a failure in
        # between can no longer leave "boxes checked, status open" — bytes that
        # match no pre-close state and would read as a human edit, never healed.
        from todo_system import cli
        with patch.object(cli, "update_markdown_checkboxes", side_effect=OSError("disk")):
            self.assertTrue(tasks.mark_done("t-close", items_dir=self.items, db_path=self.db))
        self.assertEqual(tasks.task_status("t-close", items_dir=self.items), "done")
        self.assertEqual(self._status(), "done")


class MdFingerprintTest(unittest.TestCase):
    """The close evidence reconcile_closed compares against: raw bytes in, "" for
    anything unreadable — never an exception, never a false match."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_missing_file_is_no_evidence(self):
        self.assertEqual(tasks.md_fingerprint("nope", items_dir=self.tmp), "")

    def test_same_bytes_same_value_one_byte_differs(self):
        path = self.tmp / "t.md"
        path.write_bytes(b"---\nstatus: open\n---\n# t\n")
        first = tasks.md_fingerprint("t", items_dir=self.tmp)
        self.assertTrue(first)
        self.assertEqual(tasks.md_fingerprint("t", items_dir=self.tmp), first)
        path.write_bytes(b"---\nstatus: open\n---\n# t \n")
        self.assertNotEqual(tasks.md_fingerprint("t", items_dir=self.tmp), first)

    def test_unreadable_path_is_no_evidence_not_an_exception(self):
        (self.tmp / "d.md").mkdir()                      # a directory where the md should be
        self.assertEqual(tasks.md_fingerprint("d", items_dir=self.tmp), "")

    def test_a_failing_cli_import_is_no_evidence_not_an_exception(self):
        # A tick mid-code-update can fail `from todo_system import cli` (the
        # 2026-09-17 kakao_format ImportError); _retry's DONE path calls this bare.
        (self.tmp / "t.md").write_bytes(b"---\nstatus: done\n---\n# t\n")
        real_import = __import__

        def broken(name, *args, **kwargs):
            fromlist = kwargs.get("fromlist") or (args[2] if len(args) > 2 else ())
            if name == "todo_system" and "cli" in (fromlist or ()):
                raise ImportError("cannot import name 'LEVEL_DIGEST'")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=broken):
            self.assertEqual(tasks.md_fingerprint("t", items_dir=self.tmp), "")


if __name__ == "__main__":
    unittest.main()
