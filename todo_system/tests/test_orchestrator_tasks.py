"""load_dispatchable: whole-payload characterization, fail-open paths, drift guard.

Pins the payload dict exactly (keys, values, insertion order) so the parse-once
refactor of the loop can be checked against the pre-refactor output, and counts
how often each task's markdown is parsed.
"""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from todo_system import not_before as nb_gate
from todo_system.orchestrator import repos, tasks

_COLS = ("slug, title, path, outcome, next_action, tags, worker_mode, "
         "status, is_parent, auto_merge, not_before")


def _make_db(dirpath: Path, rows: list[tuple]) -> Path:
    p = dirpath / "todo.db"
    con = sqlite3.connect(p)
    con.execute(f"CREATE TABLE tasks ({_COLS})")
    con.executemany(f"INSERT INTO tasks ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return p


def _row(slug: str, path: str = "", worker_mode: str = "auto", *, title: str = "",
         outcome: str = "", next_action: str = "", tags: str = "",
         auto_merge: int = 1) -> tuple:
    return (slug, title or slug, path, outcome, next_action, tags, worker_mode,
            "open", 0, auto_merge, None)


_EXT_MD = (
    "---\n"
    "status: open\n"
    "worker_mode: auto\n"
    'repo: "https://h/x/ext-proj.git"\n'
    "working_dir: sub/dir\n"
    'allowed_tools: "Read,Edit"\n'
    'long_job: "python3 collect.py"\n'
    "bypass_guard: true\n"
    "model_tier: Heavy\n"
    "estimate_p90: 4h\n"
    "touches:\n"
    '  - "app/src/main/Foo.kt"\n'
    '  - "shared/src/commonMain/Bar.kt"\n'
    "depends_on:\n"
    "  - other-task\n"
    "---\n"
    "# ext\n"
)


def _base(slug: str, path: str, *, worker_mode: str = "auto", auto_merge: bool = True) -> dict:
    return {
        "summary": slug,
        "outcome": "",
        "next_action": "",
        "tags": [],
        "worker_mode": worker_mode,
        "branch": f"worker/{slug}",
        "task_path": path,
        "auto_merge": auto_merge,
    }


class LoadDispatchablePayloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _md(self, name: str, text: str) -> str:
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return str(p)

    def _load(self, rows: list[tuple], **kw) -> dict:
        got = tasks.load_dispatchable(_make_db(self.tmp, rows), **kw)
        return {t.slug: t.payload for t in got}

    def test_full_payload_external_and_vault(self):
        ext = self._md("ext.md", _EXT_MD)
        rows = [
            _row("ext", ext, title="Ext task", outcome="ship X", next_action="do next",
                 tags="todo,work", auto_merge=0),
            _row("vault", "", title="Vault task", outcome="o", next_action="n",
                 auto_merge=1),
        ]
        got = tasks.load_dispatchable(_make_db(self.tmp, rows))
        self.assertEqual([t.slug for t in got], ["ext", "vault"])
        by_slug = {t.slug: t.payload for t in got}

        expected_ext = {
            "summary": "Ext task",
            "outcome": "ship X",
            "next_action": "do next",
            "tags": ["todo", "work"],
            "worker_mode": "auto",
            "branch": "worker/ext",
            "task_path": ext,
            "auto_merge": False,
            "long_job": "python3 collect.py",
            "bypass_guard": True,
            "allowed_tools": "Read,Edit",
            "model_tier": "heavy",
            "estimate_p90": "4h",
            "repo": "https://h/x/ext-proj.git",
            "working_dir": "sub/dir",
            "touches": ["app/src/main/Foo.kt", "shared/src/commonMain/Bar.kt"],
            "depends_on": ["other-task"],
        }
        self.assertEqual(by_slug["ext"], expected_ext)
        self.assertEqual(list(by_slug["ext"].keys()), list(expected_ext.keys()))

        expected_vault = {
            "summary": "Vault task",
            "outcome": "o",
            "next_action": "n",
            "tags": [],
            "worker_mode": "auto",
            "branch": "worker/vault",
            "task_path": "",
            "auto_merge": True,
        }
        self.assertEqual(by_slug["vault"], expected_vault)
        self.assertEqual(list(by_slug["vault"].keys()), list(expected_vault.keys()))

    # Fail-open: an md that cannot yield a frontmatter dict leaves every md-sourced
    # field absent, but the task still dispatches on the DB row.

    def test_fail_open_missing_file(self):
        path = str(self.tmp / "nope.md")
        self.assertEqual(self._load([_row("ghost", path)]), {"ghost": _base("ghost", path)})

    def test_fail_open_no_frontmatter(self):
        path = self._md("bare.md", "# just a heading\nworker_mode: blocked\n")
        self.assertEqual(self._load([_row("bare", path)]), {"bare": _base("bare", path)})

    def test_broken_yaml_is_excluded(self):
        # Reversal of the former `test_fail_open_broken_yaml`: a frontmatter that
        # yaml.safe_load rejects used to fail open and dispatch on the DB row,
        # which silently dropped `repo`/`allowed_tools`/`touches` and ran an
        # external-repo task as a vault task with auto_merge in force (2026-10-19
        # macp-gen-units-split15-ops-paths). This task treats that fail-open as
        # the hazard and excludes the broken task instead. Unlike the drift guard,
        # the gate is NOT conditioned on only_auto — a broken payload is unsafe in
        # any mode — so the exclusion holds with only_auto=False too.
        path = self._md("broken.md", "---\nworker_mode: blocked\nrepo: [unclosed\n---\n# x\n")
        db = _make_db(self.tmp, [_row("broken", path)])
        with patch("builtins.print") as p:
            got = {t.slug: t.payload for t in tasks.load_dispatchable(db)}
        self.assertEqual(got, {})
        reasons = [c.args[0] for c in p.call_args_list
                   if c.args and "frontmatter YAML error" in c.args[0]]
        self.assertEqual(len(reasons), 1, p.call_args_list)
        self.assertIn("broken", reasons[0])
        # excluded under only_auto=False too — a broken payload is unsafe in any mode
        self.assertEqual({t.slug for t in tasks.load_dispatchable(db, only_auto=False)}, set())

    def test_fail_open_unclosed_frontmatter(self):
        path = self._md("open.md", "---\nworker_mode: blocked\nrepo: https://h/x/y.git\n# x\n")
        self.assertEqual(self._load([_row("open", path)]), {"open": _base("open", path)})

    def test_fail_open_frontmatter_yaml_is_string(self):
        path = self._md("string.md", "---\njust text\n---\n# x\n")
        self.assertEqual(self._load([_row("string", path)]), {"string": _base("string", path)})

    def test_fail_open_frontmatter_yaml_is_list(self):
        path = self._md("list.md", "---\n- a\n---\n# x\n")
        self.assertEqual(self._load([_row("list", path)]), {"list": _base("list", path)})

    def test_fail_open_frontmatter_yaml_is_int(self):
        path = self._md("int.md", "---\n42\n---\n# x\n")
        self.assertEqual(self._load([_row("int", path)]), {"int": _base("int", path)})

    # md<->DB drift guard.

    def test_md_blocked_overrides_db_auto(self):
        path = self._md("drift.md", "---\nworker_mode: blocked\n---\n# d\n")
        self.assertEqual(self._load([_row("drift", path)]), {})

    def test_md_mode_absent_trusts_db(self):
        path = self._md("nomode.md", "---\nstatus: open\nmodel_tier: light\n---\n# n\n")
        expected = _base("nomode", path)
        expected["model_tier"] = "light"
        self.assertEqual(self._load([_row("nomode", path)]), {"nomode": expected})

    def test_md_mode_capitalized_auto_dispatches(self):
        path = self._md("cap.md", "---\nworker_mode: Auto\n---\n# c\n")
        self.assertEqual(self._load([_row("cap", path)]), {"cap": _base("cap", path)})

    def test_only_auto_false_ignores_md_mode(self):
        path = self._md("man.md", "---\nworker_mode: blocked\nestimate_p90: 90m\n---\n# m\n")
        expected = _base("man", path, worker_mode="manual")
        expected["estimate_p90"] = "90m"
        self.assertEqual(self._load([_row("man", path, "manual")], only_auto=False),
                         {"man": expected})

    # `touches` / `depends_on`: the same-repo concurrency inputs. Only a real
    # YAML list is carried; a bare string or an empty list leaves the key absent
    # (matching orchestrator._as_glob_list, which reads both as "none declared").

    def test_touches_bare_string_and_empty_list_are_absent(self):
        bare = self._md("bare.md", '---\ntouches: "app/**"\ndepends_on: other\n---\n# b\n')
        empty = self._md("empty.md", "---\ntouches: []\ndepends_on: []\n---\n# e\n")
        got = self._load([_row("bare", bare), _row("empty", empty)])
        self.assertEqual(got, {"bare": _base("bare", bare), "empty": _base("empty", empty)})

    def test_touches_list_entries_are_stripped_and_non_strings_dropped(self):
        path = self._md("mixed.md", '---\ntouches:\n  - " a/b.py "\n  - 3\n  - ""\n---\n# m\n')
        expected = _base("mixed", path)
        expected["touches"] = ["a/b.py"]
        self.assertEqual(self._load([_row("mixed", path)]), {"mixed": expected})

    def test_loaded_payloads_feed_the_concurrency_predicate(self):
        # End-to-end seam: two same-repo tasks whose markdown declares disjoint
        # `touches` must come out of the loader as concurrency-eligible. Before
        # 2026-10-17 the loader dropped the field and this was always False.
        from todo_system.orchestrator.orchestrator import Orchestrator
        a = self._md("a.md", '---\nrepo: "https://h/x/p.git"\ntouches:\n  - "ui/App.kt"\n---\n# a\n')
        b = self._md("b.md", '---\nrepo: "https://h/x/p.git"\ntouches:\n  - "care/Model.kt"\n---\n# b\n')
        c = self._md("c.md", '---\nrepo: "https://h/x/p.git"\n---\n# c (no touches)\n')
        got = self._load([_row("a", a), _row("b", b), _row("c", c)])
        self.assertTrue(Orchestrator._can_run_concurrently("a", got["a"], "b", got["b"]))
        self.assertFalse(Orchestrator._can_run_concurrently("a", got["a"], "c", got["c"]))

    def test_reads_each_md_once(self):
        # Every md-sourced field and the drift guard share one parse per task.
        # Before the refactor each read_task_* re-parsed: 7 per task, 14 here.
        rows = [_row("a", self._md("a.md", _EXT_MD)), _row("b", self._md("b.md", _EXT_MD))]
        db = _make_db(self.tmp, rows)
        with patch.object(repos, "frontmatter_with_error",
                          wraps=repos.frontmatter_with_error) as spy:
            got = tasks.load_dispatchable(db)
        self.assertEqual([t.slug for t in got], ["a", "b"])
        self.assertEqual(spy.call_count, 2)

        real_read_text = Path.read_text
        with patch.object(Path, "read_text", autospec=True,
                          side_effect=real_read_text) as reads:
            tasks.load_dispatchable(db)
        self.assertEqual(reads.call_count, 2)


class LoadDispatchableNotBeforeTest(unittest.TestCase):
    """`not_before` gate: a time-of-day value holds until that minute."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        rows = [
            _row("timed-space")[:-1] + ("2026-10-12 17:00",),
            _row("timed-t")[:-1] + ("2026-10-12T17:00",),
            _row("future-date")[:-1] + ("2999-01-01",),
            _row("past-date")[:-1] + ("2000-01-01",),
            _row("dash")[:-1] + ("-",),
            _row("none"),
        ]
        self.db = _make_db(self.tmp, rows)

    def _slugs(self, now: dt.datetime) -> list[str]:
        return [t.slug for t in tasks.load_dispatchable(self.db, now=now)]

    def test_deferred_one_minute_before(self):
        self.assertEqual(self._slugs(dt.datetime(2026, 10, 12, 16, 59)),
                         ["past-date", "dash", "none"])

    def test_dispatchable_at_the_minute(self):
        self.assertEqual(self._slugs(dt.datetime(2026, 10, 12, 17, 0)),
                         ["timed-space", "timed-t", "past-date", "dash", "none"])

    def test_default_clock_is_the_shared_helper(self):
        with patch.object(nb_gate, "_now", return_value=dt.datetime(2026, 10, 12, 16, 59)):
            got = [t.slug for t in tasks.load_dispatchable(self.db)]
        self.assertNotIn("timed-space", got)
        with patch.object(nb_gate, "_now", return_value=dt.datetime(2026, 10, 12, 17, 0)):
            got = [t.slug for t in tasks.load_dispatchable(self.db)]
        self.assertIn("timed-space", got)


class DispatchPriorityOrderTest(unittest.TestCase):
    """load_dispatchable walks open leaves by priority (high first, NULL last),
    then insertion order — the order tick() fills its parallel slots in."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _db(self, rows):
        p = self.tmp / "todo.db"
        con = sqlite3.connect(p)
        con.execute(f"CREATE TABLE tasks ({_COLS}, priority)")
        con.executemany(
            f"INSERT INTO tasks ({_COLS}, priority) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        con.close()
        return p

    def test_higher_priority_dispatches_first_regardless_of_import_order(self):
        db = self._db([
            _row("old-low") + (40.0,),
            _row("old-none") + (None,),
            _row("mid") + (60.0,),
            _row("new-urgent") + (95.0,),
            _row("tie-a") + (60.0,),
        ])
        got = [t.slug for t in tasks.load_dispatchable(db)]
        self.assertEqual(got, ["new-urgent", "mid", "tie-a", "old-low", "old-none"])

    def test_db_without_priority_column_keeps_insertion_order(self):
        db = _make_db(self.tmp, [_row("a"), _row("b"), _row("c")])
        self.assertEqual([t.slug for t in tasks.load_dispatchable(db)], ["a", "b", "c"])
