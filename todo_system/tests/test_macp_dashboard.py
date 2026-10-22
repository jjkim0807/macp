"""Tests for scripts/macp_dashboard.py — the human-list dev / 현생(life) split.

The dashboard sorts people-owned open tasks into '🛠 개발 프로젝트' (dev) and
'🎓 현생' (life). Classification is a 3-signal hybrid (see `_category`): a `repo:`
anywhere in the ancestor chain → dev; else a registry human-owner → life; else the
DEV_TAGS tag fallback. These tests pin that truth table, the ancestor-repo resolver
(cache / cycle / depth), and a few real-vault oracles including the reported bug
class (a worker-made review-* child inheriting its project's repo).

scripts/ has no __init__.py, so the module is loaded by path via importlib (its
top level is stdlib-only + a lazy todo_system import, so import is side-effect free
— the server only starts under `__main__`).
"""
from __future__ import annotations

import base64
import datetime as dt
import http.client
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import types
import unittest
import xml.etree.ElementTree as ET
from unittest import mock
from pathlib import Path

VAULT = Path(__file__).resolve().parents[2]
_SRC = VAULT / "scripts" / "macp_dashboard.py"


def _load():
    spec = importlib.util.spec_from_file_location("macp_dashboard", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()

# docs/brand/macp-logo.svg and docs/dashboard-parity.md are vault docs, not part of
# the engine code export set (AC1 = 3 code paths: todo_system, scripts, .claude/
# commands). In an engine-only export tree they are absent, so the tests that read
# them skip there (public-split-10 AC2 vault-only); the live vault ships them and runs.
_HAVE_BRAND_DOCS = (
    (VAULT / "docs" / "brand" / "macp-logo.svg").is_file()
    and (VAULT / "docs" / "dashboard-parity.md").is_file()
)

try:                                     # read_task_repo needs PyYAML to parse frontmatter
    import yaml  # noqa: F401
    _HAVE_YAML = True
except Exception:
    _HAVE_YAML = False

_REPO_URL = "https://github.com/x/y.git"


class CategoryTruthTable(unittest.TestCase):
    """Pure _category(tags, repo_present, human_owned) — no file/db/yaml deps."""

    def test_repo_signal_wins_over_nondev_tags(self):
        # a declared coding project → dev even with worldgraph-style non-dev tags
        self.assertEqual(M._category("todo,work,worldgraph", repo_present=True), "dev")

    def test_repo_beats_human_owner(self):
        self.assertEqual(M._category("todo", repo_present=True, human_owned=True), "dev")

    def test_human_owner_is_life_even_with_dev_tag(self):
        # personal admin (withdrawal/job/email) stays 현생 even if a stray dev tag exists
        self.assertEqual(M._category("app", human_owned=True), "life")

    def test_tag_fallback_dev(self):
        # repo-less dev leaves (dancemirror/tennis-macro/ai-stock) stay dev via tags
        for t in ("side-project", "app", "game", "bug", "sideproject", "project", "meta"):
            self.assertEqual(M._category(t), "dev", t)

    def test_tag_fallback_life(self):
        self.assertEqual(M._category("todo,private,cv"), "life")
        self.assertEqual(M._category(""), "life")
        self.assertEqual(M._category(None), "life")

    def test_worldgraph_tags_without_repo_are_life(self):
        # documents the residual: repo-less, non-human worldgraph tags fall to life
        self.assertEqual(M._category("todo,work,worldgraph"), "life")


@unittest.skipUnless(_HAVE_YAML, "PyYAML required for the repo-frontmatter resolver")
class RepoInChain(unittest.TestCase):
    """_repo_in_chain / _task_repo over a tmp Todo/items — child inherits an
    ancestor's repo, with cycle guard, depth cap, and (path,mtime) caching."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_items = M.ITEMS
        M.ITEMS = self.tmp                         # point the resolver at the fixture dir
        M._REPO_CACHE.clear()

    def tearDown(self):
        M.ITEMS = self._orig_items
        M._REPO_CACHE.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _w(self, slug, repo=None):
        fm = ["---", "status: open"]
        if repo:
            fm.append('repo: "%s"' % repo)
        fm += ["---", "", "# t", ""]
        (self.tmp / ("%s.md" % slug)).write_text("\n".join(fm), encoding="utf-8")

    def test_child_inherits_parent_repo(self):
        self._w("parent", repo=_REPO_URL)
        self._w("child")                          # no own repo — the review-* case
        pm = {"child": "parent", "parent": ""}
        self.assertIsNone(M._task_repo("child"))  # child itself declares none
        self.assertTrue(M._repo_in_chain("child", pm))

    def test_no_repo_anywhere_is_false(self):
        self._w("a")
        self._w("b")
        self.assertFalse(M._repo_in_chain("a", {"a": "b", "b": ""}))

    def test_cycle_guard_terminates(self):
        self._w("a")
        self._w("b")                              # a <-> b cycle, no repo
        self.assertFalse(M._repo_in_chain("a", {"a": "b", "b": "a"}))

    def test_depth_cap_bounds_the_walk(self):
        chain = {}
        for i in range(12):                       # repo only at n11, beyond the cap of 8
            self._w("n%d" % i, repo=_REPO_URL if i == 11 else None)
            chain["n%d" % i] = "n%d" % (i + 1) if i < 11 else ""
        self.assertFalse(M._repo_in_chain("n0", chain))

    def test_missing_file_is_none(self):
        self.assertIsNone(M._task_repo("does-not-exist"))
        self.assertFalse(M._repo_in_chain("does-not-exist", {}))

    def test_mtime_cache_refreshes_on_change(self):
        p = self.tmp / "c.md"
        self._w("c")                              # no repo
        os.utime(p, (1000, 1000))
        M._REPO_CACHE.clear()
        self.assertIsNone(M._task_repo("c"))      # caches (mtime=1000, None)
        p.write_text('---\nstatus: open\nrepo: "%s"\n---\n' % _REPO_URL, encoding="utf-8")
        os.utime(p, (2000, 2000))                 # distinct mtime invalidates the cache
        self.assertEqual(M._task_repo("c"), _REPO_URL)


@unittest.skipUnless(_HAVE_YAML, "PyYAML required for the repo-frontmatter resolver")
class VaultOracle(unittest.TestCase):
    """Structural signal against the real Todo/items. Each assertion is skipped if
    its fixture file is gone (vault content evolves) so the suite never goes red on
    content drift — the mechanism itself is proven hermetically by RepoInChain."""

    def setUp(self):
        patcher = mock.patch.object(M, "ITEMS", VAULT / "Todo" / "items")
        patcher.start()
        self.addCleanup(patcher.stop)
        M._REPO_CACHE.clear()

    def test_setup_restores_items_after_other_tests_patch_it(self):
        self.assertEqual(M.ITEMS, VAULT / "Todo" / "items")

    def _need(self, *slugs):
        for s in slugs:
            if not (M.ITEMS / ("%s.md" % s)).exists():
                self.skipTest("fixture absent: %s" % s)

    def test_declared_project_has_repo(self):
        self._need("agent-commander-ai-arena-ladder")
        self.assertIsNotNone(M._task_repo("agent-commander-ai-arena-ladder"))

    def test_life_admin_tasks_have_no_repo(self):
        for s in ("cv-content-refresh", "withdrawal-b1-final-decision"):
            self._need(s)
            self.assertIsNone(M._task_repo(s))

    def test_review_child_inherits_project_repo(self):
        # the reported bug class: a worker-made review-* child (no own repo) must
        # resolve dev via its parent project's declared repo.
        child = "review-worldgraph-15-knowledge-state-pov-check-20260725T1412"
        parent = "worldgraph-15-knowledge-state-pov-check"
        self._need(child, parent)
        self.assertIsNone(M._task_repo(child))    # child declares none of its own
        self.assertIsNotNone(M._task_repo(parent))
        self.assertTrue(M._repo_in_chain(child, {child: parent, parent: ""}))
        self.assertEqual(
            M._category("todo,work,worldgraph",
                        repo_present=M._repo_in_chain(child, {child: parent, parent: ""})),
            "dev")


def _install_status_fixture(tc: unittest.TestCase) -> Path:
    """Point every path collect_status() reads at a temp tree, restored on cleanup.

    Todo/todo.db is a gitignored derived cache, so a worker worktree or fresh clone
    has none — tests that read the real DB were silently skipped at the PR gate.
    The seeded rows reach each triage section: fx-active-1-run (ledger in_progress)
    → active, fx-attn-2-fail (ledger failed, todo open) → attention, fx-dev-3-manual
    (manual, DEV_TAGS `app`) → human/dev, fx-life-4-admin (no worker_mode, private)
    → human/life. Dev comes from the tag fallback, not a `repo:` item file, so the
    result does not depend on PyYAML. Memory/sessions points at an empty tmp dir so
    project last_activity never reads the real vault journals."""
    tmp = Path(tempfile.mkdtemp())
    tc.addCleanup(shutil.rmtree, tmp, True)
    for d in ("assign", "runs", "state", "items", "sessions"):
        (tmp / d).mkdir()
    db = tmp / "todo.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE tasks (slug TEXT, title TEXT, status TEXT, due TEXT, "
                "not_before TEXT, next_action TEXT, tags TEXT, parent_slug TEXT, "
                "is_parent INTEGER, worker_mode TEXT)")
    rows = [  # slug, tags, worker_mode
        ("fx-active-1-run", "todo,work", "auto"),
        ("fx-attn-2-fail", "todo,work", "auto"),
        ("fx-dev-3-manual", "todo,app", "manual"),
        ("fx-life-4-admin", "todo,private", None),
    ]
    con.executemany("INSERT INTO tasks VALUES (?,?,'open','-',NULL,'',?,NULL,0,?)",
                    [(s, s, tags, wm) for s, tags, wm in rows])
    con.commit()
    con.close()
    for slug, extra in (("fx-active-1-run", {"state": "in_progress",
                                             "updated_ts": "2026-01-01T00:00:00"}),
                        ("fx-attn-2-fail", {"state": "failed", "refail_count": 1,
                                            "last_note": "boom",
                                            "updated_ts": "2026-01-01T00:00:01"})):
        (tmp / "assign" / ("%s.json" % slug)).write_text(json.dumps(
            {"task_id": slug, "agent_id": "w1", **extra}), encoding="utf-8")
    for p in (mock.patch.object(M, "ASSIGN", tmp / "assign"),
              mock.patch.object(M, "RUNS", tmp / "runs"),
              mock.patch.object(M, "STATE", tmp / "state"),
              mock.patch.object(M, "TODO_DB", db),
              mock.patch.object(M, "ITEMS", tmp / "items"),
              mock.patch.object(M, "SESSIONS", tmp / "sessions"),
              mock.patch.object(M, "REGISTRY", tmp / "missing.yaml"),
              mock.patch.object(M, "collect_deps", return_value=[]),
              mock.patch.object(M, "_worker_running", return_value=[]),
              mock.patch.dict(M._REPO_CACHE, clear=True),
              mock.patch.dict(M._SESSION_CACHE, clear=True)):
        p.start()
        tc.addCleanup(p.stop)
    return tmp


class Smoke(unittest.TestCase):
    """End-to-end: the JSON the page polls builds, and every human item is dev|life.
    Runs over the hermetic tmp fixture (_install_status_fixture), not the vault DB."""

    def setUp(self):
        _install_status_fixture(self)

    def test_collect_status_renders_and_categorizes(self):
        d = M.collect_status()
        for key in ("human", "active", "runs", "todo"):
            self.assertIn(key, d)
        for it in d["human"]:
            self.assertIn(it["category"], ("dev", "life"), it.get("slug"))
        # positive coverage — an empty human list would pass the loop vacuously
        self.assertEqual({it["category"] for it in d["human"]}, {"dev", "life"})



class ProjectsSectionTests(unittest.TestCase):
    """Top-level parents (is_parent=1 or named as someone's parent_slug) get their own
    `projects` rows and leave the human list — e.g. advote-voting (manual, is_parent=0,
    kids linked by parent:) used to sit among the hand-dev todos."""

    def setUp(self):
        tmp = _install_status_fixture(self)
        con = sqlite3.connect(tmp / "todo.db")
        con.executemany(
            "INSERT INTO tasks VALUES (?,?,?,'-',NULL,'',?,?,?,?)", [
                # slug, title, status, tags, parent, is_parent, worker_mode
                ("fxp", "FX 프로젝트 (부모)", "open", "todo,fxp", None, 0, "manual"),
                ("fxp-mid", "mid", "open", "todo", "fxp", 1, None),
                ("fxp-a", "a", "done", "todo", "fxp-mid", 0, "auto"),
                ("fxp-b", "b", "open", "todo", "fxp-mid", 0, "auto"),
                ("fxp-c", "c", "cancelled", "todo", "fxp", 0, None),
                ("fxp-d", "d", "open", "todo", "fxp", 0, "manual"),
                ("fxq", "waiting umbrella", "waiting", "todo", None, 1, None),
                ("fxq-a", "qa", "open", "todo", "fxq", 0, None),
                ("fxdone", "closed umbrella", "done", "todo", None, 1, None),
                ("fxdone-a", "x", "done", "todo", "fxdone", 0, None),
            ])
        con.commit()
        con.close()
        (tmp / "assign" / "fxp-b.json").write_text(json.dumps(
            {"task_id": "fxp-b", "agent_id": "w1", "state": "in_progress",
             "updated_ts": "2026-01-01T00:00:02"}), encoding="utf-8")

    def test_top_level_parents_listed_with_leaf_progress(self):
        d = M.collect_status()
        by = {p["slug"]: p for p in d["projects"]}
        self.assertEqual(set(by), {"fxp", "fxq"})      # no mid-level, no closed one
        p = by["fxp"]
        self.assertEqual(p["title"], "FX 프로젝트")       # "(부모)" suffix stripped
        # leaves a(done), b(open), d(open); c cancelled and mid (has kids) not counted
        self.assertEqual((p["leaves_done"], p["leaves_total"]), (1, 3))
        self.assertEqual((p["active"], p["attention"]), (1, 0))
        self.assertEqual(by["fxq"]["status"], "waiting")
        self.assertEqual(d["projects"][0]["slug"], "fxp")  # running work sorts first

    def test_order_running_then_priority_then_recent(self):
        tmp = Path(M.TODO_DB).parent
        (tmp / "assign" / "fxp-b.json").unlink()             # nothing running now
        con = sqlite3.connect(M.TODO_DB)
        con.execute("ALTER TABLE tasks ADD COLUMN priority REAL")
        con.executemany(
            "INSERT INTO tasks (slug,title,status,due,next_action,tags,parent_slug,"
            "is_parent,worker_mode,priority) VALUES (?,?,'open','-','','todo',?,0,NULL,?)",
            [("fxr", "r", None, 90), ("fxr-a", "ra", "fxr", None)])
        con.execute("UPDATE tasks SET priority=10 WHERE slug='fxq'")
        con.execute("UPDATE tasks SET priority=50 WHERE slug='fxp'")
        con.commit()
        con.close()
        today = dt.datetime.now().strftime("%Y%m%d")
        for n in ("20200101T000000-fxp-a",                    # fxp: prio 50, old
                  today + "T000100-fxq-a",                    # fxq: prio 10, today
                  today + "T000000-fxr-a"):                   # fxr: prio 90, today
            (tmp / "runs" / n).mkdir()
        order = [p["slug"] for p in M.collect_status()["projects"]]
        self.assertEqual(order, ["fxr", "fxp", "fxq"])        # priority beats recency
        con = sqlite3.connect(M.TODO_DB)
        con.execute("UPDATE tasks SET priority=50 WHERE slug='fxq'")
        con.commit()
        con.close()
        order = [p["slug"] for p in M.collect_status()["projects"]]
        self.assertEqual(order, ["fxr", "fxq", "fxp"])        # tie → newer activity
        by = {p["slug"]: p for p in M.collect_status()["projects"]}
        self.assertTrue(by["fxq"]["last_activity"].startswith(today[:4]))
        self.assertTrue(by["fxq"]["last_age"])

    def test_split_umbrella_with_open_children_is_not_waiting_on_a_person(self):
        # fxp-mid has an open child (fxp-b). Its own ledger row reads awaiting_human —
        # the shape a 0-commit run leaves when its only output was the split
        # (mulkko-95, 2026-10-13) — but nobody has anything to do on it yet.
        tmp = Path(M.TODO_DB).parent
        for slug in ("fxp-mid", "fxp-d"):          # fxp-d: a real leaf, the control
            (tmp / "assign" / f"{slug}.json").write_text(json.dumps(
                {"task_id": slug, "agent_id": "w1", "state": "awaiting_human",
                 "last_note": "merge withheld for human review",
                 "updated_ts": "2026-01-01T00:00:03"}), encoding="utf-8")
        d = M.collect_status()
        attention = {r["task"] for r in d["attention"]}
        human = {r["slug"] for r in d["human"] + d["human_later"] if r["why"] == "parked"}
        self.assertNotIn("fxp-mid", attention)
        self.assertNotIn("fxp-mid", human)
        self.assertIn("fxp-d", attention)          # a parked leaf still surfaces
        self.assertIn("fxp-d", human)
        by = {p["slug"]: p for p in d["projects"]}
        self.assertEqual(by["fxp"]["attention"], 1)   # fxp-d only, not the umbrella

    def test_split_umbrella_surfaces_again_once_children_are_done(self):
        # With every child closed it is no longer in progress: if the orchestrator
        # has not closed it yet, it is a person's to look at again.
        tmp = Path(M.TODO_DB).parent
        con = sqlite3.connect(M.TODO_DB)
        con.execute("UPDATE tasks SET status='done' WHERE slug='fxp-b'")
        con.commit()
        con.close()
        (tmp / "assign" / "fxp-mid.json").write_text(json.dumps(
            {"task_id": "fxp-mid", "agent_id": "w1", "state": "awaiting_human",
             "updated_ts": "2026-01-01T00:00:03"}), encoding="utf-8")
        attention = {r["task"] for r in M.collect_status()["attention"]}
        self.assertIn("fxp-mid", attention)

    def test_projects_leave_the_human_list(self):
        d = M.collect_status()
        human = {r["slug"] for r in d["human"] + d["human_later"]}
        self.assertNotIn("fxp", human)
        self.assertIn("fxp-d", human)                    # its manual leaf still shows
        self.assertIn("fxq-a", human)

    def test_chip_is_the_root_project_everywhere(self):
        # the card chip comes from the root slug (not a tag), and every descendant
        # shown elsewhere carries the same chip so the colours line up.
        d = M.collect_status()
        by = {p["slug"]: p for p in d["projects"]}
        self.assertEqual(by["fxp"]["project"], "fxp")
        human = {r["slug"]: r for r in d["human"] + d["human_later"]}
        self.assertEqual(human["fxp-d"]["project"], "fxp")
        self.assertEqual(human["fxq-a"]["project"], "fxq")
        active = {r["task"]: r for r in d["active"]}
        self.assertEqual(active["fxp-b"]["project"], "fxp")

    def test_parent_under_closed_root_is_a_project_not_a_human_todo(self):
        # mulkko-121-block-editor (2026-10-19): a hand-split parent (is_parent=0,
        # worker_mode off, three open leaves) whose own root is already done. It is
        # an umbrella, so it belongs in the 프로젝트 card — not in "사람 몫 · 미설정".
        con = sqlite3.connect(M.TODO_DB)
        con.executemany(
            "INSERT INTO tasks VALUES (?,?,?,'-',NULL,'',?,?,?,?)", [
                ("fxz-project", "closed root", "done", "todo", None, 0, "off"),
                ("fxz-121", "블록 편집기 (부모)", "open", "todo", "fxz-project", 0, "off"),
                ("fxz-121a", "core", "open", "todo", "fxz-121", 0, "auto"),
                ("fxz-121b", "ui", "done", "todo", "fxz-121", 0, "auto"),
                ("fxz-120", "sibling leaf", "open", "todo", "fxz-project", 0, "manual"),
            ])
        con.commit()
        con.close()
        d = M.collect_status()
        by = {p["slug"]: p for p in d["projects"]}
        self.assertIn("fxz-121", by)
        self.assertNotIn("fxz-project", by)              # closed root stays out
        p = by["fxz-121"]
        self.assertEqual(p["title"], "블록 편집기")
        self.assertEqual((p["leaves_done"], p["leaves_total"]), (1, 2))
        self.assertEqual(p["project"], "fxz")           # chip = top ancestor's label
        human = {r["slug"]: r for r in d["human"] + d["human_later"]}
        self.assertNotIn("fxz-121", human)
        self.assertIn("fxz-120", human)                  # a plain leaf under the closed root still shows
        self.assertEqual(human["fxz-120"]["project"], "fxz")

    def test_mid_level_parent_with_open_children_leaves_the_human_list(self):
        # A hand-split parent under a LIVE root is not a project row (its root is),
        # and not a person's todo either — its open children carry the work.
        con = sqlite3.connect(M.TODO_DB)
        con.executemany(
            "INSERT INTO tasks VALUES (?,?,?,'-',NULL,'',?,?,?,?)", [
                ("fxp-hand", "hand split (부모)", "open", "todo", "fxp", 0, "off"),
                ("fxp-hand-a", "ha", "open", "todo", "fxp-hand", 0, "auto"),
            ])
        con.commit()
        con.close()
        d = M.collect_status()
        self.assertEqual({p["slug"] for p in d["projects"]}, {"fxp", "fxq"})
        human = {r["slug"] for r in d["human"] + d["human_later"]}
        self.assertNotIn("fxp-hand", human)
        self.assertIn("fxp-d", human)                    # control: a real leaf stays

    def test_root_label_strips_generic_parent_suffix(self):
        self.assertEqual(M._root_label("ai-stock-trading-agent"), "ai-stock-trading")
        self.assertEqual(M._root_label("worldgraph-project"), "worldgraph")
        self.assertEqual(M._root_label("advote-voting"), "advote-voting")

    def test_page_has_projects_card(self):
        self.assertIn('id="c-projects"', M.HTML)
        self.assertIn("fill('projects'", M.HTML)

    def test_waiting_blocked_projects_are_on_hold(self):
        # on_hold (additive, server-decided): waiting/blocked umbrellas carry it so the
        # client folds them under a collapsed 보류 toggle; open ones stay on screen.
        con = sqlite3.connect(M.TODO_DB)
        con.executemany(
            "INSERT INTO tasks VALUES (?,?,?,'-',NULL,'',?,?,?,?)", [
                # slug, title, status, tags, parent, is_parent, worker_mode
                ("fxb", "blocked umbrella", "blocked", "todo", None, 1, None),
                ("fxb-a", "ba", "open", "todo", "fxb", 0, None),
            ])
        con.commit()
        con.close()
        by = {p["slug"]: p for p in M.collect_status()["projects"]}
        self.assertTrue(by["fxq"]["on_hold"])     # waiting
        self.assertTrue(by["fxb"]["on_hold"])     # blocked
        self.assertFalse(by["fxp"]["on_hold"])    # open


class ProjectLastActivityTests(unittest.TestCase):
    """A project row's last_activity/last_age is the newest of run dir, ledger
    update, the subtree's task md `updated` and linked Memory/sessions journal
    `updated` — so a project worked by hand today reads as today even when its
    worker runs are weeks old (research-staff-job-search showed 「41d 전」)."""

    def setUp(self):
        self.tmp = _install_status_fixture(self)
        con = sqlite3.connect(M.TODO_DB)
        con.execute("ALTER TABLE tasks ADD COLUMN updated TEXT")
        con.executemany(
            "INSERT INTO tasks (slug,title,status,due,next_action,tags,parent_slug,"
            "is_parent,worker_mode) VALUES (?,?,'open','-','','todo',?,?,NULL)",
            [("fxm", "m", None, 1), ("fxm-a", "ma", "fxm", 0), ("fxm-b", "mb", "fxm", 0)])
        con.commit()
        con.close()
        self.now = dt.datetime.now().replace(microsecond=0)
        self.today = self.now.date()

    def _set_updated(self, slug, value):
        con = sqlite3.connect(M.TODO_DB)
        con.execute("UPDATE tasks SET updated=? WHERE slug=?", (value, slug))
        con.commit()
        con.close()

    def _ledger(self, slug, when: dt.datetime):
        (self.tmp / "assign" / f"{slug}.json").write_text(json.dumps(
            {"task_id": slug, "agent_id": "w1", "state": "awaiting_human",
             "updated_ts": when.astimezone().isoformat(timespec="seconds")}),
            encoding="utf-8")

    def _journal(self, name, text):
        p = M.SESSIONS / name
        p.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return p

    def _row(self, slug="fxm"):
        return {p["slug"]: p for p in M.collect_status()["projects"]}[slug]

    def _assert_same_day_age(self, row):
        age = row["last_age"]
        self.assertTrue(age, row)
        self.assertFalse(age.startswith("-"), row)
        self.assertFalse(age.endswith("d"), row)

    def test_md_updated_today_without_runs_or_ledger(self):
        self._set_updated("fxm-a", self.today.isoformat())
        r = self._row()
        self.assertTrue(r["last_activity"].startswith(self.today.isoformat()), r)
        self._assert_same_day_age(r)

    def test_md_today_beats_ledger_yesterday(self):
        yesterday = dt.datetime.combine(self.today - dt.timedelta(days=1), dt.time(12))
        self._ledger("fxm-b", yesterday)
        self.assertEqual(self._row()["last_activity"],
                         yesterday.isoformat(timespec="seconds"))    # ledger alone
        self._set_updated("fxm-a", self.today.isoformat())
        r = self._row()
        self.assertTrue(r["last_activity"].startswith(self.today.isoformat()), r)
        self._assert_same_day_age(r)

    def test_no_activity_is_empty(self):
        self._set_updated("fxm", "-")
        self._set_updated("fxm-a", "")
        r = self._row()                                  # fxm-b stays NULL
        self.assertEqual((r["last_activity"], r["last_age"]), ("", ""))

    def test_run_time_is_not_lowered_by_same_day_md(self):
        run = dt.datetime.combine(self.today, dt.time(0, 0, 1))
        (self.tmp / "runs" / (run.strftime("%Y%m%dT%H%M%S") + "-fxm-a")).mkdir()
        self._set_updated("fxm-a", self.today.isoformat())   # md reads as 00:00:00
        self.assertEqual(self._row()["last_activity"], run.isoformat(timespec="seconds"))

    def test_future_md_updated_ignored(self):
        self._set_updated("fxm-a", (self.today + dt.timedelta(days=1)).isoformat())
        self.assertEqual(self._row()["last_activity"], "")

    def test_session_journal_updated_joins_max(self):
        t1 = self.now - dt.timedelta(minutes=10)
        t2 = self.now - dt.timedelta(minutes=20)
        p1 = self._journal(f"{t1.date()}-fxm.testhost.md",
                           f"---\nproject: fxm\nupdated: {t1.astimezone().isoformat()}\n"
                           "status: active\n---\n\n# fxm\n")
        self._journal(f"{t2.date()}-other.md",            # legacy name, linked by task
                      f"---\nproject: unrelated\nupdated: {t2.astimezone().isoformat()}\n"
                      'related_tasks:\n  - "[[fxm-b]]"\n---\n\n# other\n')
        self._set_updated("fxm-a", (self.today - dt.timedelta(days=3)).isoformat())
        r = self._row()
        self.assertEqual(r["last_activity"], t1.isoformat(timespec="seconds"))
        self.assertTrue(r["last_age"].endswith("m"), r)
        p1.unlink()                                       # related_tasks mapping alone
        self.assertEqual(self._row()["last_activity"], t2.isoformat(timespec="seconds"))

    def test_session_journal_fail_soft(self):
        future = (self.now + dt.timedelta(days=1)).astimezone().isoformat()
        self._journal("2026-01-01-garbage.md", "no frontmatter at all\n")
        self._journal("2026-01-01-baddate.md", "---\nproject: fxm\nupdated: not-a-date\n---\n")
        self._journal("2026-01-01-future.md", f"---\nproject: fxm\nupdated: {future}\n---\n")
        self._journal("2026-01-01-binary.md", b"---\nproject: fxm\nupdated: \xff\xfe\n---\n")
        self.assertEqual(self._row()["last_activity"], "")
        with mock.patch.object(M, "SESSIONS", self.tmp / "no-such-dir"):
            self.assertEqual(self._row()["last_activity"], "")

    def test_projects_row_keys_unchanged(self):
        self._set_updated("fxm-a", self.today.isoformat())
        self.assertEqual(set(self._row()), {
            "slug", "title", "project", "status", "worker_mode", "on_hold", "action",
            "due", "leaves_done", "leaves_total", "active", "attention", "priority",
            "last_activity", "last_age"})

    def test_tooltip_names_sources(self):
        self.assertIn("최근 활동 ${esca(r.last_activity)} (run·ledger·md·session 중 최신)",
                      M.HTML)
        self.assertNotIn('title="최근 실행', M.HTML)


class ActiveAgentDefaultTests(unittest.TestCase):
    """active[].agent_is_default (additive, server-decided): the client hides the
    'agent …' line when the runner is the default one. True when the agent is empty
    or equals DEFAULT_RUNNER_AGENT; False for a named non-default agent."""

    def setUp(self):
        self.tmp = _install_status_fixture(self)

    def _active(self, slug, agent):
        d = {"task_id": slug, "state": "in_progress", "updated_ts": "2026-01-01T00:00:05"}
        if agent is not None:
            d["agent_id"] = agent                    # absent key → collect_assignments ''
        (self.tmp / "assign" / ("%s.json" % slug)).write_text(
            json.dumps(d), encoding="utf-8")

    def test_default_or_missing_agent_is_default_true(self):
        self._active("fx-def", M.DEFAULT_RUNNER_AGENT)
        self._active("fx-empty", "")
        self._active("fx-none", None)                 # no agent_id key at all
        active = {r["task"]: r for r in M.collect_status()["active"]}
        for slug in ("fx-def", "fx-empty", "fx-none"):
            self.assertTrue(active[slug]["agent_is_default"], slug)

    def test_named_non_default_agent_is_not_default(self):
        # the base fixture's fx-active-1-run runs under agent 'w1'
        active = {r["task"]: r for r in M.collect_status()["active"]}
        self.assertFalse(active["fx-active-1-run"]["agent_is_default"])


class InfoDietShell(unittest.TestCase):
    """Info-diet contract (2026-10-17) pinned on the static HTML string, mirroring
    DashboardShellTests/GroupFoldShell. Live heights/button-count/390px-no-scroll are
    gated by the run's playwright after-metrics.json, not here."""

    HTML = M.HTML

    def test_copy_button_left_the_list_rows(self):
        # tlabel (list rows) no longer appends the ⧉ id button; tlabelCp (detail only) does.
        self.assertNotIn("</span> ${copyBtn(slug)}", self.HTML)
        self.assertIn("function tlabelCp(", self.HTML)
        self.assertIn("tlabelCp(d.slug,d.title,d.project)", self.HTML)  # run/task detail

    def test_active_agent_line_is_gated(self):
        # the 'agent …' line renders only for a named non-default agent.
        self.assertIn("r.agent&&!r.agent_is_default", self.HTML)

    def test_project_row_has_no_action_prose(self):
        # projRow main is just the title (no next_action .note second line).
        self.assertIn(
            'prj" data-open="task:${esca(r.slug)}">'
            '<div class="main"><div>${tlabel(r.slug,r.title,r.project)}</div></div>',
            self.HTML)

    def test_system_detail_card_and_unified_history(self):
        self.assertIn('id="d-sys"', self.HTML)                     # collapsed 시스템 상세
        self.assertIn("function renderHistory(", self.HTML)        # runs ∪ recent
        # unified row keeps both click targets (run→runRow, task→recentRow).
        self.assertIn("h.k==='run'?runRow(h.row):recentRow(h.row)", self.HTML)
        # runs id anchors preserved (absorbed into 이력, kept as hidden placeholder).
        for anchor in ('id="d-runs"', 'id="cnt-runs"', 'id="recent"', 'id="d-recent"',
                       'id="d-agents"', 'id="d-mode"', 'id="d-deps"'):
            self.assertIn(anchor, self.HTML, anchor)

    def test_projects_split_open_and_hold(self):
        self.assertIn("fill('projects'", self.HTML)                # open projects
        self.assertIn("fill('projects-hold'", self.HTML)           # 보류
        self.assertIn('id="d-projhold"', self.HTML)

    def test_first_screen_lists_are_capped_and_one_line(self):
        # open projects + 진행 중 are bounded (top N + 더보기) so the first screen height
        # stays under the metric thresholds regardless of data volume.
        self.assertIn("PROJLIM", self.HTML)
        self.assertIn("ACTLIM", self.HTML)
        self.assertIn('data-sec="projects"', self.HTML)
        self.assertIn('data-sec="active"', self.HTML)
        self.assertIn("foldFill('active','active'", self.HTML)     # fold kept on the slice
        # project rows are one physical line (no 2-line wrap on 390px).
        self.assertIn(".prj .main>div{white-space:nowrap", self.HTML)

    def test_status_card_three_counts_and_moved_metrics(self):
        for label in ("<span>주의</span>", "<span>결정</span>", "<span>진행 중</span>"):
            self.assertIn(label, self.HTML, label)
        # the 3 action counts stay on the first screen (before 시스템 상세); runs today /
        # usage / slots moved into it (after d-sys).
        d_sys = self.HTML.index('id="d-sys"')
        self.assertLess(self.HTML.index('id="mv-attn"'), d_sys)
        self.assertLess(d_sys, self.HTML.index('id="mv-runs"'))
        self.assertLess(d_sys, self.HTML.index('id="usage"'))
        self.assertIn('id="sys-slots"', self.HTML)

    def test_empty_sections_fold_away(self):
        self.assertIn("$('c-attention').hidden=", self.HTML)
        for sub in ("d-humanlife", "d-humandev", "d-humanblocked", "d-humanlater"):
            self.assertIn("$('%s').hidden=" % sub, self.HTML, sub)

    def test_no_external_urls_in_diet(self):
        self.assertNotIn("https://", self.HTML)
        self.assertNotIn("http://", self.HTML)


if __name__ == "__main__":
    unittest.main()


# ---- remote-access gate: --host + MACP_DASHBOARD_TOKEN -----------------------
# The dashboard used to be loopback-only. Making it reachable from a phone
# (tailscale serve / any other exposure) means the token gate is the only thing
# between the public and the vault, so pin its exact behaviour end to end over
# a real socket — not by poking Handler internals.


class _LiveServer:
    """Bind on an ephemeral loopback port with a fixed token, serve in a thread."""

    def __init__(self, token):
        self.srv = M.serve("127.0.0.1", 0, token)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def __enter__(self):
        self.t.start()
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()

    def get(self, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, dict(r.getheaders()), body


class RemoteAccessGateTests(unittest.TestCase):
    TOKEN = "s3cret-token"

    def setUp(self):
        # /api/status walks the real vault; stub it so the test is hermetic + fast.
        p = mock.patch.object(M, "collect_status", return_value={"ok": True})
        p.start()
        self.addCleanup(p.stop)

    def test_non_loopback_bind_without_token_refused(self):
        with self.assertRaises(SystemExit):
            M.serve("0.0.0.0", 0, "")
        with self.assertRaises(SystemExit):
            M.serve("0.0.0.0", 0, None)

    def test_loopback_without_token_stays_open(self):
        srv = M.serve("127.0.0.1", 0, "")
        try:
            self.assertIsNone(srv.RequestHandlerClass.token)
        finally:
            srv.server_close()

    def test_no_credential_is_401_for_page_and_api(self):
        with _LiveServer(self.TOKEN) as s:
            for path in ("/", "/api/status"):
                status, _, body = s.get(path)
                self.assertEqual(status, 401, path)
                self.assertNotIn(b"MaestroACP", body[:200] if path == "/" else b"")

    def test_wrong_token_is_401_and_sets_no_cookie(self):
        with _LiveServer(self.TOKEN) as s:
            status, hdrs, _ = s.get("/?token=nope")
            self.assertEqual(status, 401)
            self.assertNotIn("Set-Cookie", hdrs)
            status, _, _ = s.get("/api/status", {"Cookie": f"{M.COOKIE}=nope"})
            self.assertEqual(status, 401)
            status, _, _ = s.get("/api/status", {"Authorization": "Bearer nope"})
            self.assertEqual(status, 401)

    def test_query_token_serves_page_and_sets_httponly_cookie(self):
        with _LiveServer(self.TOKEN) as s:
            status, hdrs, body = s.get(f"/?token={self.TOKEN}")
            self.assertEqual(status, 200)
            self.assertIn(b"<html", body[:100].lower())
            # the favicon rides in the page itself (data URI) — no separate authed request
            self.assertIn(b'<link rel="icon" type="image/svg+xml" href="data:', body)
            ck = hdrs.get("Set-Cookie", "")
            self.assertIn(f"{M.COOKIE}={self.TOKEN}", ck)
            self.assertIn("HttpOnly", ck)
            self.assertIn("SameSite=Strict", ck)
            self.assertIn("Path=/", ck)

    def test_cookie_and_bearer_reach_api_without_resetting_cookie(self):
        with _LiveServer(self.TOKEN) as s:
            status, hdrs, body = s.get("/api/status", {"Cookie": f"{M.COOKIE}={self.TOKEN}"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"ok": True})
            self.assertNotIn("Set-Cookie", hdrs)   # only the ?token= landing sets it
            status, _, body = s.get("/api/status", {"Authorization": f"Bearer {self.TOKEN}"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"ok": True})

    def test_fresh_query_token_overrides_stale_cookie(self):
        with _LiveServer(self.TOKEN) as s:
            status, hdrs, _ = s.get(f"/?token={self.TOKEN}", {"Cookie": f"{M.COOKIE}=stale"})
            self.assertEqual(status, 200)
            self.assertIn(f"{M.COOKIE}={self.TOKEN}", hdrs.get("Set-Cookie", ""))

    # -- public exposure + auth-failure rate limit (macp-dashboard-public-access) --

    def test_public_exposure_without_token_refused(self):
        # A public tunnel (Funnel/cloudflared) forwards to loopback, so the
        # non-loopback guard can't see it — public_exposure must refuse a tokenless
        # bind even on 127.0.0.1. Both "" and None count as "no token".
        with self.assertRaises(SystemExit):
            M.serve("127.0.0.1", 0, "", public_exposure=True)
        with self.assertRaises(SystemExit):
            M.serve("127.0.0.1", 0, None, public_exposure=True)

    def test_public_exposure_with_token_binds(self):
        srv = M.serve("127.0.0.1", 0, self.TOKEN, public_exposure=True)
        try:
            self.assertEqual(srv.RequestHandlerClass.token, self.TOKEN)
        finally:
            srv.server_close()

    def test_auth_failures_rate_limited_to_429(self):
        n = M.Handler.RL_MAX_FAILS + 1
        with _LiveServer(self.TOKEN) as s:
            results = [s.get("/?token=nope") for _ in range(n)]
        codes = [r[0] for r in results]
        self.assertEqual(codes[0], 401)                    # early failures: plain 401
        self.assertEqual(codes[-1], 429)                   # past the threshold: throttled
        self.assertEqual(set(codes), {401, 429})           # nothing else leaks through
        self.assertEqual(codes.count(429), n - (M.Handler.RL_MAX_FAILS - 1))
        self.assertIn("retry-after", {k.lower() for k in results[-1][1]})   # 429 carries Retry-After

    def test_auth_failure_is_logged_without_the_secret(self):
        secret_guess = "sup3r-secret-guess"
        with self.assertLogs("macp.dashboard", "WARNING") as cm:
            with _LiveServer(self.TOKEN) as s:
                s.get(f"/?token={secret_guess}")
        joined = "\n".join(cm.output)
        self.assertIn("auth failure", joined)
        self.assertNotIn(secret_guess, joined)             # presented secret never logged


class UnknownApiRoutingTests(unittest.TestCase):
    """Unknown /api/* paths must 404 as JSON, not fall through to the HTML page
    (macp-dashboard-unknown-api-404-json); /api/status must match exactly."""

    TOKEN = "s3cret-token"

    def setUp(self):
        p = mock.patch.object(M, "collect_status", return_value={"ok": True})
        p.start()
        self.addCleanup(p.stop)

    def test_unknown_api_path_is_404_json(self):
        with _LiveServer(self.TOKEN) as s:
            status, hdrs, body = s.get(f"/api/nope?token={self.TOKEN}")
        self.assertEqual(status, 404)
        self.assertEqual(hdrs.get("Content-Type"), "application/json")
        self.assertIn("error", json.loads(body))

    def test_status_prefix_without_exact_match_is_404(self):
        with _LiveServer(self.TOKEN) as s:
            status, _, _ = s.get(f"/api/statusXYZ?token={self.TOKEN}")
        self.assertEqual(status, 404)

    def test_exact_status_path_is_200_json(self):
        with _LiveServer(self.TOKEN) as s:
            status, hdrs, body = s.get(f"/api/status?token={self.TOKEN}")
        self.assertEqual(status, 200)
        self.assertEqual(hdrs.get("Content-Type"), "application/json")
        self.assertEqual(json.loads(body), {"ok": True})

    def test_root_path_is_200_html(self):
        with _LiveServer(self.TOKEN) as s:
            status, hdrs, body = s.get(f"/?token={self.TOKEN}")
        self.assertEqual(status, 200)
        self.assertIn(b"<html", body[:100].lower())


# ---- usage card: "when does the HOLD lift?" -----------------------------------

class UsageHoldTimingTests(unittest.TestCase):
    """collect_usage must surface the guard's absolute blocked_until AND a
    server-computed countdown, so the card can say "해제 2시간 10분 뒤" instead of
    only OPEN/HOLD. Reads a synthetic latest.json under a temp STATE dir."""

    def _with_state(self, decision, fetched_ago=60):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (Path(d) / "usage_guard").mkdir()
        now = M.dt.datetime.now().timestamp()
        (Path(d) / "usage_guard" / "latest.json").write_text(json.dumps(
            {"fetched_at": now - fetched_ago, "decision": decision}))
        p = mock.patch.object(M, "STATE", Path(d))
        p.start()
        self.addCleanup(p.stop)
        return now

    def test_hold_exposes_blocked_until_and_countdown(self):
        now = self._with_state({
            "allowed": False, "reason": "weekly ahead of pace",
            "blocked_until": None, "windows": [
                {"name": "session", "utilization": 8.0, "resets_at": None, "blocked": False},
                {"name": "weekly", "utilization": 52.0, "resets_at": None, "blocked": True}]})
        # fill epochs relative to the same clock the collector uses
        until, s_reset, w_reset = now + 2 * 3600 + 600, now + 1800, now + 4 * 86400
        st = json.loads((M.STATE / "usage_guard" / "latest.json").read_text())
        st["decision"]["blocked_until"] = until
        st["decision"]["windows"][0]["resets_at"] = s_reset
        st["decision"]["windows"][1]["resets_at"] = w_reset
        (M.STATE / "usage_guard" / "latest.json").write_text(json.dumps(st))

        u = M.collect_usage()
        self.assertFalse(u["allowed"])
        self.assertAlmostEqual(u["blocked_until"], until, places=3)
        self.assertTrue(2 * 3600 + 590 <= u["blocked_in_s"] <= 2 * 3600 + 600, u["blocked_in_s"])
        self.assertTrue(1790 <= u["session_resets_in_s"] <= 1800)
        self.assertTrue(4 * 86400 - 10 <= u["weekly_resets_in_s"] <= 4 * 86400)
        self.assertAlmostEqual(u["weekly_resets_at"], w_reset, places=3)

    def test_open_has_no_hold_but_still_reports_resets(self):
        now = self._with_state({"allowed": True, "reason": None, "blocked_until": None,
                                "windows": [{"name": "session", "utilization": 3.0,
                                             "resets_at": 0}]})
        st = json.loads((M.STATE / "usage_guard" / "latest.json").read_text())
        st["decision"]["windows"][0]["resets_at"] = now + 900
        (M.STATE / "usage_guard" / "latest.json").write_text(json.dumps(st))
        u = M.collect_usage()
        self.assertTrue(u["allowed"])
        self.assertIsNone(u["blocked_until"])
        self.assertIsNone(u["blocked_in_s"])
        self.assertTrue(890 <= u["session_resets_in_s"] <= 900)
        self.assertIsNone(u["weekly_resets_at"])

    def test_expired_hold_reports_negative_countdown(self):
        # guard tick hasn't run yet; the card must say "예정 시각 지남", not a bogus wait
        now = self._with_state({"allowed": False, "reason": "x", "blocked_until": 1, "windows": []})
        st = json.loads((M.STATE / "usage_guard" / "latest.json").read_text())
        st["decision"]["blocked_until"] = now - 120
        (M.STATE / "usage_guard" / "latest.json").write_text(json.dumps(st))
        u = M.collect_usage()
        self.assertLess(u["blocked_in_s"], 0)

    def test_garbage_blocked_until_is_none_not_crash(self):
        self._with_state({"allowed": False, "reason": "x", "blocked_until": "soon", "windows": []})
        u = M.collect_usage()
        self.assertIsNone(u["blocked_until"])
        self.assertIsNone(u["blocked_in_s"])


# ---- usage card: idle / HOLD / blind must stay distinguishable on 429/no-data --


class UsageNoDataVisibilityTests(unittest.TestCase):
    """collect_usage must still surface last-good utilization + staleness, the
    last fetch error, and the next-retry time when the decision has no fresh
    windows (429/no-data) — reading the state file only, never the network."""

    def _with_state(self, state):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (Path(d) / "usage_guard").mkdir()
        (Path(d) / "usage_guard" / "latest.json").write_text(json.dumps(state))
        p = mock.patch.object(M, "STATE", Path(d))
        p.start()
        self.addCleanup(p.stop)

    def test_collect_usage_exposes_stale_error_next_retry(self):
        now = M.dt.datetime.now().timestamp()
        self._with_state({
            "fetched_at": now - 7200,
            "usage": {"five_hour": {"utilization": 42.0},
                      "seven_day": {"utilization": 63.0}},
            "error": "HTTPError: HTTP Error 429: Too Many Requests",
            "next_retry_at": now + 300,
            "decision": {                       # no-data decision -> empty windows
                "allowed": True,
                "reason": "no usage data (HTTPError: 429) — fail-open",
                "windows": [], "stale": True,
            },
        })
        u = M.collect_usage()
        # last-good utilization survives the empty decision windows
        self.assertEqual(u["session"], 42.0)
        self.assertEqual(u["weekly"], 63.0)
        self.assertTrue(u["stale"])
        self.assertIn("429", u["last_error"])
        self.assertAlmostEqual(u["next_retry_at"], now + 300, places=3)
        self.assertTrue(250 <= u["next_retry_in_s"] <= 300, u["next_retry_in_s"])

    def test_fresh_window_util_wins_over_snapshot_fallback(self):
        # a fresh decision window is authoritative over the stored snapshot
        now = M.dt.datetime.now().timestamp()
        self._with_state({
            "fetched_at": now - 30,
            "usage": {"five_hour": {"utilization": 42.0}},
            "decision": {"allowed": True, "reason": None, "stale": False,
                         "windows": [{"name": "session", "utilization": 7.0,
                                      "resets_at": None, "blocked": False}]},
        })
        u = M.collect_usage()
        self.assertEqual(u["session"], 7.0)     # fresh window, not the 42 snapshot
        self.assertFalse(u["stale"])
        self.assertIsNone(u["last_error"])


# ---- redesigned shell: mobile-first triage, no full re-render, offline-capable --
# The 2026-09-13 UX redesign is a front-end rewrite of the `HTML` constant only —
# these pin the acceptance criteria that need no browser (the height / scroll /
# contrast criteria are gated by the run's playwright shots, recorded in run.log).
# All assertions read the static string, so they pass with or without a vault.


class DashboardShellTests(unittest.TestCase):
    HTML = M.HTML

    def test_doctype_and_html_lead_the_document(self):
        # the token gate reads `<html` in the first bytes — keep it leading, no
        # preceding comment (RemoteAccessGateTests asserts this over a socket too).
        self.assertIn(b"<html", self.HTML.encode("utf-8")[:100].lower())

    def test_no_external_urls(self):
        # AC#5: offline-near — no CDN, external font, or external script/style ref.
        self.assertNotIn("https://", self.HTML)
        self.assertNotIn("http://", self.HTML)
        self.assertNotIn("<script src", self.HTML)
        # the only <link> is the inline data-URI favicon — data: is not external.
        links = re.findall(r"<link\b[^>]*>", self.HTML)
        self.assertEqual(len(links), 1, links)
        self.assertIn('rel="icon"', links[0])
        self.assertIn('href="data:image/svg+xml;base64,', links[0])

    def test_full_rerender_removed(self):
        # AC#9: the 3s poll must not wipe #app wholesale (that reset every open
        # <details> / page scroll / 더보기). Both quote styles, plus the positive
        # signal that the mount()/update() split exists.
        self.assertNotIn("app.innerHTML=''", self.HTML)
        self.assertNotIn('app.innerHTML=""', self.HTML)
        self.assertIn("function mount(", self.HTML)
        self.assertIn("function update(", self.HTML)

    def test_required_section_ids(self):
        # first-screen triage anchors the JS updates in place.
        for anchor in ('id="app"', 'id="c-status"', 'id="c-human"',
                       'id="c-attention"', 'id="c-recent"'):
            self.assertIn(anchor, self.HTML, anchor)

    def test_header_and_footer_anchors(self):
        for anchor in ('id="hstate"', 'id="hdot"', 'id="clock"', 'id="upd"'):
            self.assertIn(anchor, self.HTML, anchor)

    def test_base_font_ge_13px(self):
        # AC#8: base font >=13px (the body shorthand font size).
        m = re.search(r"body\{[^}]*font:(\d+)px", self.HTML)
        self.assertIsNotNone(m, "body{...font:NNpx...} rule not found")
        self.assertGreaterEqual(int(m.group(1)), 13)

    def test_status_top_level_keys_preserved(self):
        # AC#3: /api/status contract — the front-end rewrite adds no backend change,
        # so every documented top-level key must still be produced.
        _install_status_fixture(self)
        d = M.collect_status()
        for key in ("now", "worker_busy", "paused", "runs_today", "active",
                    "attention", "human", "deps", "recent", "runs", "agents",
                    "todo", "usage"):
            self.assertIn(key, d, key)


class ThemeShellTests(unittest.TestCase):
    """Light/dark theme (docs/dashboard-parity.md §"테마"): default auto follows
    prefers-color-scheme, a header button cycles auto→light→dark, the choice is
    stored under localStorage['mdash.theme'] and applied pre-paint to
    <html data-theme> so an explicit light choice never flashes the dark default."""

    HTML = M.HTML

    # Contract §2 light palette — 11 tokens, exact hex.
    LIGHT_HEX = ("#f6f8fa", "#ffffff", "#d0d7de", "#eaeef2", "#1f2328",
                 "#59636e", "#1a7f37", "#cf222e", "#9a6700", "#0969da", "#8250df")

    def test_theme_plumbing_strings_present(self):
        # storage key, the data-theme attribute selector, the auto media query,
        # and the color-scheme meta all exist.
        self.assertIn("mdash.theme", self.HTML)
        self.assertIn("data-theme", self.HTML)
        self.assertIn("prefers-color-scheme", self.HTML)
        self.assertIn('<meta name="color-scheme"', self.HTML)

    def test_prepaint_script_runs_before_css(self):
        # AC: the saved theme is applied before first paint — the inline <script>
        # that reads mdash.theme must precede <style> (and have no src: offline).
        style_at = self.HTML.index("<style>")
        head = self.HTML[:style_at]
        self.assertIn("mdash.theme", head)
        self.assertIn("dataset.theme", head)
        self.assertIn("<script>", head)
        self.assertNotIn("<script src", head)

    def test_light_palette_hex_present(self):
        for hexv in self.LIGHT_HEX:
            self.assertIn(hexv, self.HTML, hexv)

    def test_dark_palette_preserved(self):
        # the dark :root defaults stay byte-for-byte (contract §2 다크 현행).
        for hexv in ("#0d1117", "#161b22", "#30363d", "#242b34", "#e6edf3",
                     "#9ba5b0", "#3fb950", "#f85149", "#d29922", "#58a6ff", "#bc8cff"):
            self.assertIn(hexv, self.HTML, hexv)

    def test_hardcoded_surface_hex_removed(self):
        # the dark-only hardcoded surfaces/badge hex are gone (replaced by tokens /
        # color-mix) so light has no black-background residue (parity §2 하드코딩 금지).
        for stale in ("#21262d", "#1c2129", "#12321f", "#56d364", "#3d1a1a",
                      "#ff9a92", "#122b40", "#79c0ff", "#3a2d10", "#e3b341",
                      "#262c34", "#1f2a36", "#a5b4c7", "#2a2140", "#d2b4ff", "#132a3d"):
            self.assertNotIn(stale, self.HTML, stale)

    def test_header_toggle_button(self):
        # one header button, cyclable, with an aria-label and the 3 state labels.
        self.assertIn('id="theme"', self.HTML)
        m = re.search(r'<button id="theme"[^>]*aria-label="[^"]+"', self.HTML)
        self.assertIsNotNone(m, "header #theme button with aria-label not found")
        self.assertIn("'auto','light','dark'", self.HTML)
        for lbl in ("자동", "라이트", "다크"):
            self.assertIn(lbl, self.HTML, lbl)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _path_points(d):
    """Vertices of an SVG path built from line commands only (M L H V Z, both
    cases, implicit repeats). Any other command fails the test, so a future curve
    forces this walker to be updated rather than silently passing."""
    tokens = re.findall(r"[A-Za-z]|-?(?:\d+\.?\d*|\.\d+)", d)
    pts, x, y, sx, sy, cmd, i = [], 0.0, 0.0, 0.0, 0.0, None, 0
    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
            if cmd in "Zz":
                x, y = sx, sy
                continue
        if cmd is None or cmd not in "MmLlHhVv":
            raise AssertionError(f"unsupported path command {cmd!r} in {d!r}")
        rel = cmd.islower()
        if cmd in "Hh":
            v = float(tokens[i])
            x = x + v if rel else v
            i += 1
        elif cmd in "Vv":
            v = float(tokens[i])
            y = y + v if rel else v
            i += 1
        else:
            px, py = float(tokens[i]), float(tokens[i + 1])
            x, y = (x + px, y + py) if rel else (px, py)
            i += 2
            if cmd in "Mm":
                sx, sy = x, y
                cmd = "l" if rel else "L"  # implicit pairs after a moveto are linetos
        pts.append((x, y))
    return pts


@unittest.skipUnless(
    _HAVE_BRAND_DOCS,
    "vault-only: the canonical logo (docs/brand/macp-logo.svg) and the parity doc "
    "(docs/dashboard-parity.md) are vault docs, not in the engine code export set "
    "(AC1 = 3 code paths) — absent in an engine-only tree (public-split-10 AC2).",
)
class LogoFaviconTests(unittest.TestCase):
    """The canonical logo (docs/brand/macp-logo.svg) — web favicon and the app's
    adaptive-icon source. See docs/dashboard-parity.md "로고"."""

    LOGO = VAULT / "docs" / "brand" / "macp-logo.svg"
    PARITY = VAULT / "docs" / "dashboard-parity.md"

    def _root(self):
        return ET.fromstring(self.LOGO.read_text(encoding="utf-8"))

    def test_canonical_svg_matches_embedded_constant(self):
        self.assertEqual(self.LOGO.read_text(encoding="utf-8"), M.LOGO_SVG)

    def test_head_favicon_decodes_to_canonical_svg(self):
        m = re.search(r'<link rel="icon" type="image/svg\+xml" '
                      r'href="data:image/svg\+xml;base64,([A-Za-z0-9+/=]+)">', M.HTML)
        self.assertIsNotNone(m, "favicon <link> not found")
        self.assertLess(m.start(), M.HTML.index("</head>"))
        self.assertEqual(base64.b64decode(m.group(1)).decode("utf-8"), M.LOGO_SVG)

    def test_svg_is_pure_vector_two_colors(self):
        root = self._root()
        self.assertEqual(_local(root.tag), "svg")
        self.assertEqual(root.get("viewBox"), "0 0 24 24")
        colors = set()
        for el in root.iter():
            self.assertIn(_local(el.tag), {"svg", "rect", "path", "circle"}, el.tag)
            for attr, val in el.attrib.items():
                self.assertNotEqual(_local(attr), "href", el.tag)
                if _local(attr) in ("fill", "stroke") and val != "none":
                    colors.add(val.lower())
        self.assertEqual(colors, {"#0d1117", "#58a6ff"})

    def test_foreground_within_adaptive_safe_zone(self):
        # adaptive icon: 66dp safe zone of the 108dp canvas, centred → 61% of 24.
        lo = 24 * (1 - 66 / 108) / 2
        hi = 24 - lo
        checked = 0
        for el in self._root().iter():
            tag = _local(el.tag)
            half = float(el.get("stroke-width", "0")) / 2
            if tag == "circle":
                cx, cy, r = (float(el.get(k)) for k in ("cx", "cy", "r"))
                box = (cx - r - half, cy - r - half, cx + r + half, cy + r + half)
            elif tag == "path":
                pts = _path_points(el.get("d"))
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                box = (min(xs) - half, min(ys) - half, max(xs) + half, max(ys) + half)
            else:
                continue  # svg root + the full-canvas background rect
            checked += 1
            for edge in box:
                self.assertGreaterEqual(edge, lo, (tag, box))
                self.assertLessEqual(edge, hi, (tag, box))
        self.assertGreaterEqual(checked, 2)

    def test_strokes_are_bold(self):
        # 16px legibility proxy: stroke >= 2 of 24 units renders >= ~1.3px.
        widths = [float(el.get("stroke-width")) for el in self._root().iter()
                  if el.get("stroke-width") is not None]
        self.assertTrue(widths, "no foreground stroke")
        for w in widths:
            self.assertGreaterEqual(w, 2)

    def test_path_walker_rejects_curves(self):
        self.assertEqual(_path_points("M1 2h3v4l-1-1z"), [(1, 2), (4, 2), (4, 6), (3, 5)])
        with self.assertRaises(AssertionError):
            _path_points("M0 0C1 1 2 2 3 3")

    def test_parity_doc_has_logo_contract(self):
        doc = self.PARITY.read_text(encoding="utf-8")
        for needle in ("로고", "docs/brand/macp-logo.svg", "#0d1117", "#58a6ff",
                       "vector drawable", "macp-dashboard-android-app-logo"):
            self.assertIn(needle, doc, needle)


class WorkerRunningDetection(unittest.TestCase):
    """`_worker_running()` — the source of the header's 워커 실행중 / 유휴 badge.

    2026-09-20: the badge read "유휴 (idle)" while an Implement stage had been running
    for 28 minutes. Cause: `pgrep -fl` prints the process NAME only (procps-ng), so
    every line was "1696592 claude" and the `"claude -p" in line` filter matched
    nothing — `worker_busy` could never be True. These tests pin the flags and both
    output shapes so a revert to `-l` fails here instead of silently on the dashboard.
    """

    # What `pgrep -af "claude -p"` actually prints (argv joined, prompt truncated here).
    AF_LINE = ("1696592 claude -p You are the **Implement agent (I)** of an autonomous "
               "implementation worker pipeline.")
    # What `pgrep -fl "claude -p"` printed instead — name only, no flags, no prompt.
    FL_LINE = "1696592 claude"

    def _run_with(self, stdout):
        captured = {}

        def fake_run(argv, **kw):
            captured["argv"] = argv
            return types.SimpleNamespace(stdout=stdout)

        with mock.patch.object(M.subprocess, "run", side_effect=fake_run):
            return M._worker_running(), captured["argv"]

    def test_uses_a_not_l_so_the_full_command_line_is_matched(self):
        _, argv = self._run_with("")
        self.assertEqual(argv[0], "pgrep")
        flags = argv[1]
        self.assertIn("a", flags, f"pgrep needs -a to print full command lines, got {flags!r}")
        self.assertIn("f", flags, f"pgrep needs -f to match the full command line, got {flags!r}")
        self.assertNotIn("l", flags, "-l prints the process NAME only — the 2026-09-20 idle-badge bug")

    def test_running_stage_is_detected(self):
        found, _ = self._run_with(self.AF_LINE + "\n")
        self.assertTrue(found, "a live `claude -p` stage must make worker_busy truthy")
        self.assertEqual(found, ["claude -p"], "prompt text must not leak into the return value")

    def test_two_stages_counted_separately(self):
        found, _ = self._run_with(self.AF_LINE + "\n" + self.AF_LINE.replace("1696592", "1696593") + "\n")
        self.assertEqual(len(found), 2)

    def test_name_only_output_does_not_count(self):
        # The shape the old flags produced. Kept as documentation of the failure mode:
        # if the flags regress, test_uses_a_not_l fails and this one shows why.
        found, _ = self._run_with(self.FL_LINE + "\n")
        self.assertEqual(found, [])

    def test_no_process_is_idle(self):
        found, _ = self._run_with("")
        self.assertEqual(found, [])

    def test_pgrep_failure_is_not_busy(self):
        with mock.patch.object(M.subprocess, "run", side_effect=OSError("pgrep missing")):
            self.assertEqual(M._worker_running(), [])


# ---- /api/run: one run's tap-through detail ------------------------------------


class RunDetailTests(unittest.TestCase):
    """Hermetic: RUNS/ASSIGN/TODO_DB point at a temp tree, never the live vault."""

    RUN = "20490102T030000-demo-task"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name, val in (("RUNS", self.tmp / "runs"), ("ASSIGN", self.tmp / "assign"),
                          ("TODO_DB", self.tmp / "todo.db")):
            p = mock.patch.object(M, name, val)
            p.start()
            self.addCleanup(p.stop)
        rd = self.tmp / "runs" / self.RUN
        rd.mkdir(parents=True)
        (rd / "run.log").write_text(
            "x" * 9000 + "\n=== research (rc=0) ===\n=== implement (rc=1) ===\ntail-marker\n",
            encoding="utf-8")
        (rd / "report.md").write_text("# report", encoding="utf-8")
        (rd / "requirements.md").write_text("# req", encoding="utf-8")
        (rd / "notes.md").write_text("n", encoding="utf-8")
        (rd / "stages.json").write_text("{}", encoding="utf-8")
        (self.tmp / "assign").mkdir()
        (self.tmp / "assign" / "demo-task.json").write_text(json.dumps(
            {"task_id": "demo-task", "state": "failed", "agent_id": "w1",
             "refail_count": 2, "last_note": "boom"}), encoding="utf-8")

    def test_detail_shape_and_artifact_order(self):
        d = M.collect_run_detail(self.RUN)
        self.assertEqual(d["slug"], "demo-task")
        self.assertEqual(d["date"], "2049-01-02")
        self.assertEqual(d["when"], "030000")
        self.assertEqual(d["stages"], [{"name": "research", "rc": 0},
                                       {"name": "implement", "rc": 1}])
        self.assertEqual([a["name"] for a in d["artifacts"]],
                         ["requirements.md", "report.md", "notes.md"])
        self.assertEqual(d["artifacts"][0]["content"], "# req")
        self.assertEqual(d["ledger"]["state"], "failed")
        self.assertEqual(d["ledger"]["refail"], 2)
        self.assertEqual(d["task"], {})          # no todo.db → empty, not a crash

    def test_log_tail_keeps_the_end(self):
        d = M.collect_run_detail(self.RUN)
        self.assertTrue(d["log_truncated"])
        self.assertEqual(len(d["log_tail"]), M.LOG_TAIL_MAX)
        self.assertTrue(d["log_tail"].rstrip().endswith("tail-marker"))

    def test_large_artifact_is_capped(self):
        (self.tmp / "runs" / self.RUN / "verify.md").write_text(
            "v" * (M.ARTIFACT_MAX + 10), encoding="utf-8")
        a = {x["name"]: x for x in M.collect_run_detail(self.RUN)["artifacts"]}["verify.md"]
        self.assertTrue(a["truncated"])
        self.assertEqual(len(a["content"]), M.ARTIFACT_MAX)
        self.assertEqual(a["size"], M.ARTIFACT_MAX + 10)

    def test_malformed_or_traversal_ids_are_rejected(self):
        (self.tmp / "secret").mkdir()
        for bad in ("", "../secret", "20490102T030000-../../secret", "demo-task",
                    "20490102T030000-a/b", "20490102T030000-.hidden",
                    "20490102T030000-missing"):
            self.assertIsNone(M.collect_run_detail(bad), bad)

    def test_endpoint_200_404_and_token_gate(self):
        with _LiveServer("tok") as s:
            status, _, _ = s.get(f"/api/run?id={self.RUN}")
            self.assertEqual(status, 401)
            auth = {"Authorization": "Bearer tok"}
            status, _, body = s.get(f"/api/run?id={self.RUN}", auth)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["run"], self.RUN)
            status, _, _ = s.get("/api/run?id=..%2Fsecret", auth)
            self.assertEqual(status, 404)
            status, _, _ = s.get("/api/run", auth)
            self.assertEqual(status, 404)


# ---- /api/task: one task's tap-through detail ----------------------------------


class TaskDetailTests(unittest.TestCase):
    """Hermetic like RunDetailTests; ITEMS points at a temp tree too."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name, val in (("RUNS", self.tmp / "runs"), ("ASSIGN", self.tmp / "assign"),
                          ("TODO_DB", self.tmp / "todo.db"), ("ITEMS", self.tmp / "items")):
            p = mock.patch.object(M, name, val)
            p.start()
            self.addCleanup(p.stop)
        for d in ("runs", "assign", "items"):
            (self.tmp / d).mkdir()
        (self.tmp / "items" / "demo.md").write_text(
            "---\nstatus: open\n---\n\n# Demo\n\n- [ ] step\n", encoding="utf-8")
        for run in ("20490101T010000-demo", "20490102T020000-demo",
                    "20490103T030000-other-demo"):     # suffix-alike, NOT demo's
            (self.tmp / "runs" / run).mkdir()
        (self.tmp / "runs" / "20490102T020000-demo" / "run.log").write_text(
            "=== quick (rc=0) ===\n", encoding="utf-8")
        (self.tmp / "assign" / "demo.json").write_text(json.dumps(
            {"task_id": "demo", "state": "done", "last_note": "ok"}), encoding="utf-8")

    def test_detail_shape_runs_exact_and_newest_first(self):
        d = M.collect_task_detail("demo")
        self.assertEqual(d["slug"], "demo")
        self.assertEqual(d["body"], "# Demo\n\n- [ ] step")    # frontmatter stripped
        self.assertFalse(d["body_truncated"])
        self.assertEqual([r["run"] for r in d["runs"]],
                         ["20490102T020000-demo", "20490101T010000-demo"])
        self.assertEqual(d["runs"][0]["stages"], [{"name": "quick", "rc": 0}])
        self.assertEqual(d["ledger"]["state"], "done")
        self.assertEqual(d["task"], {})

    def test_ledger_only_task_still_resolves(self):
        (self.tmp / "assign" / "orphan.json").write_text(
            json.dumps({"state": "failed"}), encoding="utf-8")
        self.assertEqual(M.collect_task_detail("orphan")["ledger"]["state"], "failed")

    def test_unknown_malformed_and_traversal_slugs_are_none(self):
        for bad in ("", "nope", "../items/demo", "a/b", ".hidden", "demo.md/.."):
            self.assertIsNone(M.collect_task_detail(bad), bad)

    def test_endpoint_200_404_and_token_gate(self):
        with _LiveServer("tok") as s:
            self.assertEqual(s.get("/api/task?slug=demo")[0], 401)
            auth = {"Authorization": "Bearer tok"}
            status, _, body = s.get("/api/task?slug=demo", auth)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["slug"], "demo")
            self.assertEqual(s.get("/api/task?slug=nope", auth)[0], 404)
            self.assertEqual(s.get("/api/task", auth)[0], 404)


# ---- task grouping (fold): server _group_of + collect_status stamp + client shell --
# 2026-09-25 feature: a section with many rows of the same project (e.g. 21 "사람 몫"
# rows of research-staff-job-search) collapses same-project rows under one folded
# header. The grouping key is computed by ONE server helper (_group_of); the web
# client folds and persists the open/closed state. These pin the server contract and
# the static-HTML client contract; the live fold/expand + 390px no-scroll are gated by
# the run's playwright shots (recorded in run.log). Appended — no existing test touched.


class GroupOf(unittest.TestCase):
    """Pure _group_of(row): the single grouping-key rule — row['project'] or ''."""

    def test_project_present_is_the_group(self):
        self.assertEqual(M._group_of({"project": "advote"}), "advote")

    def test_missing_project_is_no_group(self):
        self.assertEqual(M._group_of({}), "")

    def test_empty_string_project_is_no_group(self):
        self.assertEqual(M._group_of({"project": ""}), "")

    def test_none_project_is_no_group(self):
        self.assertEqual(M._group_of({"project": None}), "")

    def test_only_project_matters(self):
        self.assertEqual(M._group_of({"project": "x", "slug": "y", "state": "z"}), "x")


class GroupStampingHermetic(unittest.TestCase):
    """collect_status() stamps a str `group` (computed by _group_of) on EVERY
    human/attention/active row. Injected rows guarantee the three sections are
    non-empty so this can't pass vacuously, and prove the add is additive (top-level
    keys + existing row fields survive). Hermetic: every collector is stubbed, so it
    runs with or without a vault DB."""

    def test_group_stamped_on_all_three_sections_from_helper(self):
        active_row = {"task": "advote-1-x", "state": "in_progress", "agent": "w1", "age": "1m"}
        attn_row = {"task": "moksori-2-y", "state": "failed", "note": "boom", "age": "2m"}
        human_row = {"slug": "moksori-3-z", "category": "dev", "why": "backlog",
                     "action": "do it", "project": "moksori"}
        pmap = {"advote-1-x": "advote", "moksori-2-y": "moksori"}
        with mock.patch.object(M, "collect_assignments", return_value=[active_row, attn_row]), \
                mock.patch.object(M, "_todo_status_map", return_value={}), \
                mock.patch.object(M, "_title_map", return_value={}), \
                mock.patch.object(M, "_project_map", return_value=pmap), \
                mock.patch.object(M, "collect_human", return_value=[human_row]), \
                mock.patch.object(M, "collect_runs", return_value=[]), \
                mock.patch.object(M, "collect_deps", return_value=[]), \
                mock.patch.object(M, "collect_agents", return_value=[]), \
                mock.patch.object(M, "collect_todo", return_value={}), \
                mock.patch.object(M, "collect_usage", return_value=None), \
                mock.patch.object(M, "_worker_running", return_value=[]):
            d = M.collect_status()
        # positive coverage — each triage section actually has a row
        self.assertEqual(len(d["active"]), 1)
        self.assertEqual(len(d["attention"]), 1)
        self.assertEqual(len(d["human"]), 1)
        for sec in ("active", "attention", "human"):
            for r in d[sec]:
                self.assertIsInstance(r["group"], str, sec)
                self.assertEqual(r["group"], M._group_of(r), sec)   # one helper computes it
        self.assertEqual(d["active"][0]["group"], "advote")
        self.assertEqual(d["attention"][0]["group"], "moksori")
        self.assertEqual(d["human"][0]["group"], "moksori")
        # additive: existing row fields untouched
        self.assertEqual(d["active"][0]["task"], "advote-1-x")
        self.assertEqual(d["human"][0]["category"], "dev")
        # top-level contract preserved
        for key in ("now", "worker_busy", "paused", "runs_today", "active",
                    "attention", "human", "deps", "recent", "runs", "agents",
                    "todo", "usage"):
            self.assertIn(key, d, key)


class GroupStampingLive(unittest.TestCase):
    """Same contract through the real collectors (not stubbed, unlike
    GroupStampingHermetic) over the hermetic tmp todo.db/ledger fixture, so the
    field lands on rows the real SQL/ledger path produced; runs without a vault DB."""

    def setUp(self):
        _install_status_fixture(self)

    def test_three_sections_carry_helper_computed_group(self):
        d = M.collect_status()
        for sec in ("human", "attention", "active"):
            self.assertTrue(d[sec], f"{sec} empty — loop would pass vacuously")
        for sec in ("human", "attention", "active"):
            for r in d[sec]:
                self.assertIn("group", r, f"{sec}: {r.get('slug') or r.get('task')}")
                self.assertIsInstance(r["group"], str)
                self.assertEqual(r["group"], M._group_of(r))


class GroupFoldShell(unittest.TestCase):
    """Client fold contract pinned on the static HTML string (no browser), mirroring
    DashboardShellTests. The live fold/expand + 390px no-scroll are gated by the run's
    playwright shots."""

    HTML = M.HTML

    def test_foldfill_wraps_all_four_sections(self):
        self.assertIn("function foldFill(", self.HTML)
        for call in ("foldFill('human-life','human-life'",
                     "foldFill('human-dev','human-dev'",
                     "foldFill('attention','attention'",
                     "foldFill('active','active'"):
            self.assertIn(call, self.HTML, call)

    def test_fold_threshold_is_two(self):
        # only groups with >=2 rows in a section collapse; singletons stay standalone.
        self.assertIn("cnt[g]>=2", self.HTML)

    def test_group_header_markup_and_default_collapsed(self):
        for tok in ("data-grp=", "data-gsec=", "grphead", "grpkids", "건"):
            self.assertIn(tok, self.HTML, tok)
        self.assertIn("op?'▾':'▸'", self.HTML)        # arrow reflects open state
        self.assertIn("op?'':' hidden'", self.HTML)   # kids hidden when op=false → default collapsed

    def test_localstorage_state_is_section_scoped(self):
        # keyed per <section>.<group> so human-life / human-dev / attention / active
        # never share fold state, and it survives a reload.
        self.assertIn("'mdash.grp.'+sec+'.'+g", self.HTML)
        self.assertIn("localStorage", self.HTML)
        for fn in ("grpOpen", "grpSet"):
            self.assertIn(fn, self.HTML, fn)

    def test_toggle_is_delegated_so_it_survives_rerender(self):
        # a delegated document handler on data-grp (not a per-node listener) lives
        # through every 3s leaf innerHTML swap.
        self.assertIn("closest('[data-grp]')", self.HTML)

    def test_section_counts_stay_task_based(self):
        # AC: header counts are task counts, not group counts — unchanged by folding.
        self.assertIn("$('cnt-attn').textContent=d.attention.length", self.HTML)
        self.assertIn("$('cnt-active').textContent=d.active.length", self.HTML)

    def test_no_external_urls_in_fold_additions(self):
        self.assertNotIn("https://", self.HTML)
        self.assertNotIn("http://", self.HTML)

    @unittest.skipUnless(
        _HAVE_BRAND_DOCS,
        "vault-only: docs/dashboard-parity.md is a vault doc, not in the engine code "
        "export set (AC1 = 3 code paths) — absent in an engine-only tree (public-split-10 AC2).",
    )
    def test_parity_doc_exists_and_documents_the_contract(self):
        p = VAULT / "docs" / "dashboard-parity.md"
        self.assertTrue(p.exists(), "docs/dashboard-parity.md missing")
        txt = p.read_text(encoding="utf-8")
        self.assertIn("group", txt)
        self.assertIn("mdash.grp.", txt)                            # localStorage key contract
        self.assertIn("macp-dashboard-android-4-task-grouping", txt)  # app parity task link


# ---- "사람 몫" triage: discover excluded, not_before → human_later, urgent flag ----
# 2026-09-25: the section showed 23 rows — 11 had a future not_before (job-search
# leaves not actionable until 09-28) and one was a worker `discover` task.


class HumanTriage(unittest.TestCase):
    """Hermetic: TODO_DB is a temp sqlite; registry load is stubbed out."""

    TODAY = dt.date(2049, 1, 10)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        db = self.tmp / "todo.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE tasks (slug TEXT, title TEXT, status TEXT, due TEXT, "
                    "not_before TEXT, next_action TEXT, tags TEXT, parent_slug TEXT, "
                    "is_parent INTEGER, worker_mode TEXT)")
        rows = [  # slug, due, not_before, worker_mode
            ("disc", "-", None, "discover"),
            ("auto1", "-", None, "auto"),
            ("soon", "2049-01-12", None, "manual"),       # today+2 → urgent
            ("overdue", "2049-01-01", None, None),          # overdue → urgent
            ("far", "2049-02-01", None, "manual"),          # not urgent
            ("nodue", "-", None, "blocked"),                # not urgent
            ("blockedsoon", "2049-01-11", None, "blocked"), # blocked never urgent
            ("snoozed", "2049-01-11", "2049-01-15", None),  # future not_before → later
            ("started", "-", "2049-01-10", None),           # not_before today → shown
        ]
        con.executemany("INSERT INTO tasks VALUES (?,?,'open',?,?,'',?,NULL,0,?)",
                        [(s, s, d, nb, "todo", wm) for s, d, nb, wm in rows])
        con.commit()
        con.close()
        for p in (mock.patch.object(M, "TODO_DB", db),
                  mock.patch.object(M, "REGISTRY", self.tmp / "missing.yaml"),
                  mock.patch.object(M, "_repo_in_chain", return_value=False)):
            p.start()
            self.addCleanup(p.stop)

    def _rows(self):
        return {r["slug"]: r for r in M.collect_human([], today=self.TODAY)}

    def test_worker_dispatched_modes_are_not_human(self):
        rows = self._rows()
        self.assertNotIn("disc", rows)
        self.assertNotIn("auto1", rows)

    def test_urgent_is_due_within_window_or_overdue(self):
        rows = self._rows()
        self.assertTrue(rows["soon"]["urgent"])
        self.assertTrue(rows["overdue"]["urgent"])
        self.assertFalse(rows["far"]["urgent"])
        self.assertFalse(rows["nodue"]["urgent"])
        self.assertFalse(rows["blockedsoon"]["urgent"])
        self.assertEqual(rows["nodue"]["due"], "")          # "-" normalised away

    def test_future_not_before_is_later(self):
        rows = self._rows()
        self.assertTrue(rows["snoozed"]["later"])
        self.assertFalse(rows["started"]["later"])
        self.assertEqual(rows["snoozed"]["not_before"], "2049-01-15")

    def test_urgent_rows_sort_first(self):
        order = [r["slug"] for r in M.collect_human([], today=self.TODAY)]
        n_urgent = sum(1 for s in order if s in ("soon", "overdue", "snoozed"))
        self.assertEqual(set(order[:n_urgent]), {"soon", "overdue", "snoozed"})

    def test_parked_is_urgent(self):
        a = [{"task": "p1", "state": "awaiting_human", "note": "", "project": "x"}]
        self.assertTrue(M.collect_human(a, today=self.TODAY)[0]["urgent"])

    def test_status_splits_later_out_of_human(self):
        with mock.patch.object(M, "collect_human",
                               return_value=[{"slug": "a", "later": False},
                                             {"slug": "b", "later": True}]):
            d = M.collect_status()
        self.assertEqual([r["slug"] for r in d["human"]], ["a"])
        self.assertEqual([r["slug"] for r in d["human_later"]], ["b"])

    def test_infra_and_ops_tags_are_dev(self):
        for t in ("todo,private,infra,macp", "todo,private,ai-stock-trading,ops"):
            self.assertEqual(M._category(t), "dev", t)

    def test_shell_renders_urgent_and_later_sections(self):
        for tok in ("foldFill('human-urgent','human-urgent'",
                    "foldFill('human-later','human-later'",
                    "d.human_later", "r.urgent"):
            self.assertIn(tok, M.HTML, tok)


class HumanBlockedBeatsOwned(HumanTriage):
    """A human-owned task marked worker_mode: blocked shows as 블록, not 결정필요."""

    def test_blocked_wins_over_owned(self):
        reg = types.SimpleNamespace(
            agents=[types.SimpleNamespace(id="me", kind="human")],
            for_project=lambda slug: types.SimpleNamespace(id="me"))
        with mock.patch("todo_system.orchestrator.registry.load_registry", return_value=reg):
            rows = self._rows()
        self.assertEqual(rows["nodue"]["why"], "blocked")
        self.assertEqual(rows["soon"]["why"], "owned")


class HumanTriageTimeOfDay(unittest.TestCase):
    """`not_before` with a time: `later` flips at that minute and the wire field
    keeps the time (`YYYY-MM-DDTHH:MM`) for the web and the app."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        db = self.tmp / "todo.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE tasks (slug TEXT, title TEXT, status TEXT, due TEXT, "
                    "not_before TEXT, next_action TEXT, tags TEXT, parent_slug TEXT, "
                    "is_parent INTEGER, worker_mode TEXT)")
        rows = [("timed", "2049-01-10 17:00"), ("timedT", "2049-01-10T17:00"),
                ("dated", "2049-01-11")]
        con.executemany("INSERT INTO tasks VALUES (?,?,'open','-',?,'',?,NULL,0,NULL)",
                        [(s, s, nb, "todo") for s, nb in rows])
        con.commit()
        con.close()
        for p in (mock.patch.object(M, "TODO_DB", db),
                  mock.patch.object(M, "REGISTRY", self.tmp / "missing.yaml"),
                  mock.patch.object(M, "_repo_in_chain", return_value=False)):
            p.start()
            self.addCleanup(p.stop)

    def _rows(self, **kw):
        return {r["slug"]: r for r in M.collect_human([], **kw)}

    def test_later_until_the_minute(self):
        before = self._rows(now=dt.datetime(2049, 1, 10, 16, 59))
        at = self._rows(now=dt.datetime(2049, 1, 10, 17, 0))
        for slug in ("timed", "timedT"):
            self.assertTrue(before[slug]["later"], slug)
            self.assertFalse(at[slug]["later"], slug)
        self.assertTrue(at["dated"]["later"])

    def test_wire_field_keeps_the_time(self):
        rows = self._rows(now=dt.datetime(2049, 1, 10, 16, 59))
        self.assertEqual(rows["timed"]["not_before"], "2049-01-10T17:00")
        self.assertEqual(rows["timedT"]["not_before"], "2049-01-10T17:00")
        self.assertEqual(rows["dated"]["not_before"], "2049-01-11")

    def test_today_alone_means_midnight(self):
        rows = self._rows(today=dt.date(2049, 1, 10))
        self.assertTrue(rows["timed"]["later"])

    def test_web_md_renders_the_time(self):
        self.assertIn("const md=s=>s?String(s).slice(5).replace('-','/').replace('T',' '):'';",
                      M.HTML)


class HumanBlockedSectionShell(unittest.TestCase):
    """Blocked rows render in their own folded ⛔ section, out of life/dev."""

    def test_blocked_section_present_and_split(self):
        for tok in ("id=\"d-humanblocked\"", "foldFill('human-blocked','human-blocked'",
                    "r.why==='blocked'", "r.why!=='blocked'"):
            self.assertIn(tok, M.HTML, tok)


class AgentsExcludeHumans(unittest.TestCase):
    """`/api/status` `agents` carries runners only — a `kind: human` registry entry is
    never a row, because humans send no heartbeat and a stale `10d 전` read as "did a
    person die?". The ai rows keep every key/value they had."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_state, self._orig_registry = M.STATE, M.REGISTRY
        M.STATE = self.tmp
        M.REGISTRY = self.tmp / "registry.yaml"
        fresh = dt.datetime.now(dt.timezone.utc).isoformat()
        # alice is the stale human key left in agent_seen.json by the old behaviour
        # (2026-09-14) — it must stay harmless without any migration.
        (self.tmp / "agent_seen.json").write_text(
            json.dumps({"alice": fresh, "worker-default": fresh}), encoding="utf-8")
        M.REGISTRY.write_text(
            "agents:\n"
            "  - id: alice\n"
            "    kind: human\n"
            "    host: mac\n"
            "  - id: worker-default\n"
            "    kind: ai-local\n"
            "    host: acme-hq\n"
            "    projects: [macp, dripcoffee]\n", encoding="utf-8")

    def tearDown(self):
        M.STATE, M.REGISTRY = self._orig_state, self._orig_registry
        shutil.rmtree(self.tmp, ignore_errors=True)

    @unittest.skipUnless(_HAVE_YAML, "PyYAML required to parse the agent registry")
    def test_human_row_is_absent_even_with_a_stale_seen_key(self):
        ids = {a["id"] for a in M.collect_agents()}
        self.assertEqual(ids, {"worker-default"})

    @unittest.skipUnless(_HAVE_YAML, "PyYAML required to parse the agent registry")
    def test_ai_row_keys_and_values_are_unchanged(self):
        rows = M.collect_agents()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(set(row), {"id", "kind", "host", "projects",
                                    "last_seen", "age", "alive"})
        self.assertEqual(row["kind"], "ai-local")
        self.assertEqual(row["host"], "acme-hq")
        self.assertEqual(row["projects"], ["macp", "dripcoffee"])
        self.assertTrue(row["last_seen"])
        self.assertIs(row["alive"], True)

    def test_registry_unreadable_falls_back_to_every_seen_key(self):
        # No registry -> load_registry raises -> we cannot tell who is human, so the
        # fallback exposes every seen key (runner liveness beats hiding one human row).
        M.REGISTRY = self.tmp / "does-not-exist.yaml"
        rows = M.collect_agents()
        self.assertEqual({a["id"] for a in rows}, {"alice", "worker-default"})


class RoConnHelper(unittest.TestCase):
    """`_ro_conn` backs all 6 read-only sqlite call sites in macp_dashboard.py.
    It must (a) yield None instead of connecting when the db file is absent, and
    (b) always close the connection, even when the caller's query raises —
    the bug the dedup was meant to fix (the old per-site `con.close()` sat after
    the query, so an exception mid-query left the connection open)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_missing_db_yields_none_without_connecting(self):
        missing = self.tmp / "no-such.db"
        with M._ro_conn(missing) as con:
            self.assertIsNone(con)

    def test_connection_closes_on_normal_exit(self):
        db = self.tmp / "todo.db"
        sqlite3.connect(db).close()
        with M._ro_conn(db) as con:
            captured = con
        with self.assertRaises(sqlite3.ProgrammingError):
            captured.execute("SELECT 1")  # closed connections raise on use

    def test_connection_closes_even_if_query_raises(self):
        db = self.tmp / "todo.db"
        sqlite3.connect(db).close()
        captured = {}
        with self.assertRaises(sqlite3.OperationalError):
            with M._ro_conn(db) as con:
                captured["con"] = con
                con.execute("SELECT * FROM no_such_table")  # raises mid-query
        with self.assertRaises(sqlite3.ProgrammingError):
            captured["con"].execute("SELECT 1")  # still closed despite the raise


# ---- Queue (docs/dashboard-parity.md "큐") ----------------------------------

_QUEUE_ROW_KEYS = ["slug", "title", "project", "group", "worker_mode", "priority",
                   "checkout", "wait", "wait_text", "holder", "since"]
_QUEUE_REPO = "https://github.com/example-org/app.git"


def _install_queue_fixture(tc: unittest.TestCase) -> Path:
    """A real-schema todo.db (cli.init_db) + registry + ledger under one tmp vault:
    two auto tasks on one repo (the 2nd is held behind the 1st in the same tick),
    one in flight, one with a future not_before, one blocked on an open task."""
    from todo_system import cli
    tmp = Path(tempfile.mkdtemp())
    tc.addCleanup(shutil.rmtree, tmp, True)
    for d in ("Todo/items", "Worker/assignments", "Worker/agents", "Worker/runs",
              "Worker/_state"):
        (tmp / d).mkdir(parents=True)
    (tmp / "Worker/agents/registry.yaml").write_text(
        "agents:\n  - id: w\n    kind: ai-local\n    projects: ['*']\n", encoding="utf-8")
    items = tmp / "Todo" / "items"

    def md(slug, extra=""):
        p = items / f"{slug}.md"
        p.write_text(f"---\nstatus: open\n{extra}---\n\n# {slug}\n", encoding="utf-8")
        return str(p)

    db = tmp / "Todo" / "todo.db"
    con = cli.db_connect(db)
    cli.init_db(con)
    rows = [
        ("qa-1-first", "First", "auto", md("qa-1-first", f"repo: {_QUEUE_REPO}\n"),
         None, 70.0, "2026-01-02"),
        ("qa-2-second", "Second", "auto", md("qa-2-second", f"repo: {_QUEUE_REPO}\n"),
         None, None, "2026-01-03"),
        ("qa-3-running", "Running", "auto", md("qa-3-running"), None, None, None),
        ("qa-4-later", "Later", "auto", md("qa-4-later"), "2999-01-01 09:00", None, None),
        ("qa-5-dep", "Dep", "manual", md("qa-5-dep"), None, None, None),
        ("qa-6-blocked", "Blocked", "blocked",
         md("qa-6-blocked", "worker_mode: blocked\ndepends_on:\n  - qa-5-dep\n"),
         None, None, None),
    ]
    for slug, title, mode, path, nb, prio, upd in rows:
        con.execute("INSERT INTO tasks (slug, title, path, status, worker_mode, not_before, "
                    "priority, updated, imported_at, tags) "
                    "VALUES (?,?,?,'open',?,?,?,?,'2026-01-01T00:00:00','todo')",
                    (slug, title, path, mode, nb, prio, upd))
    con.commit()
    con.close()
    (tmp / "Worker/assignments/qa-3-running.json").write_text(json.dumps(
        {"task_id": "qa-3-running", "agent_id": "w", "corr_id": "c",
         "state": "in_progress", "payload": {}}), encoding="utf-8")
    for p in (mock.patch.object(M, "ASSIGN", tmp / "Worker/assignments"),
              mock.patch.object(M, "RUNS", tmp / "Worker/runs"),
              mock.patch.object(M, "STATE", tmp / "Worker/_state"),
              mock.patch.object(M, "TODO_DB", db),
              mock.patch.object(M, "ITEMS", items),
              mock.patch.object(M, "SESSIONS", tmp / "Worker/_state"),
              mock.patch.object(M, "REGISTRY", tmp / "Worker/agents/registry.yaml"),
              mock.patch.object(M, "collect_deps", return_value=[]),
              mock.patch.object(M, "_worker_running", return_value=[]),
              mock.patch("todo_system.orchestrator.orchestrator.resolve_dispatch_limits",
                         return_value=(40, 2, 0))):
        p.start()
        tc.addCleanup(p.stop)
    return tmp


class QueueCollectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = _install_queue_fixture(self)

    def test_rows_carry_exactly_the_contract_fields(self):
        q = M.collect_queue()
        self.assertEqual(sorted(q), sorted(M.QUEUE_KEYS))
        self.assertEqual([r["slug"] for r in q["queue"]], ["qa-1-first", "qa-2-second"])
        for r in q["queue"]:
            self.assertEqual(list(r), _QUEUE_ROW_KEYS)
        first, second = q["queue"]
        self.assertEqual((first["wait"], first["priority"], first["since"]),
                         ("next_tick", 70.0, "2026-01-02"))
        self.assertEqual((second["wait"], second["holder"], second["checkout"]),
                         ("checkout_busy", "qa-1-first", "app"))
        self.assertEqual((first["project"], first["group"]), ("qa", "qa"))
        self.assertEqual(q["queue_upcoming"], [
            {"slug": "qa-4-later", "title": "Later", "project": "qa", "group": "qa",
             "not_before": "2999-01-01 09:00"}])
        self.assertEqual(q["queue_blocked"], [
            {"slug": "qa-6-blocked", "title": "Blocked", "project": "qa", "group": "qa",
             "waiting_on": ["qa-5-dep"]}])

    def test_in_flight_task_is_not_in_the_queue(self):
        slugs = {r["slug"] for r in M.collect_queue()["queue"]}
        self.assertNotIn("qa-3-running", slugs)

    def test_paused_and_usage_hold_come_from_state_reads(self):
        (self.tmp / "Worker/_state/PAUSED").write_text("", encoding="utf-8")
        self.assertEqual({r["wait"] for r in M.collect_queue()["queue"]}, {"paused"})
        (self.tmp / "Worker/_state/PAUSED").unlink()
        q = M.collect_queue(usage={"allowed": False, "reason": "weekly 97%"})
        self.assertEqual({r["wait"] for r in q["queue"]}, {"usage_hold"})
        self.assertIn("weekly 97%", q["queue"][0]["wait_text"])

    def test_status_carries_the_three_keys_and_keeps_dispatchable(self):
        d = M.collect_status()
        for k in M.QUEUE_KEYS:
            self.assertIsInstance(d[k], list)
        self.assertEqual(len(d["queue"]), 2)
        # todo.dispatchable keeps its meaning: load_dispatchable's count, in-flight included
        self.assertEqual(d["todo"]["dispatchable"], 3)
        for k in ("active", "attention", "human", "runs", "usage", "account"):
            self.assertIn(k, d)

    def test_exception_yields_empty_lists_for_all_three(self):
        with mock.patch.object(M, "_compute_queue", side_effect=RuntimeError("boom")):
            self.assertEqual(M.collect_queue(), {k: [] for k in M.QUEUE_KEYS})
            d = M.collect_status()
        for k in M.QUEUE_KEYS:
            self.assertEqual(d[k], [])
        self.assertIn("todo", d)
        self.assertIn("active", d)

    def test_queue_html_section_shell(self):
        html = M.HTML
        self.assertIn('id="c-queue"', html)
        # right under 진행 중, before the 시스템 상세 card
        self.assertLess(html.index('id="c-active"'), html.index('id="c-queue"'))
        self.assertLess(html.index('id="c-queue"'), html.index('id="c-recent"'))
        # collapsed by default: none of the three <details> carries `open`
        for did in ("d-queue", "d-queue-upcoming", "d-queue-blocked"):
            tag = re.search(r'<details id="%s"[^>]*>' % did, html).group(0)
            self.assertNotIn(" open", tag, tag)
        for sub in ("d-queue-upcoming", "d-queue-blocked"):
            self.assertIn("$('%s').hidden=" % sub, html, sub)      # hidden when empty
        for key in ("'queue'", "'queue-upcoming'", "'queue-blocked'"):
            self.assertIn(key, html)
        self.assertIn("'mdash.sec.'+s", html)
        self.assertIn("fill('queue',q,queueRow,'큐 비어 있음')", html)   # no foldFill
        self.assertNotIn("foldFill('queue'", html)
        self.assertIn('data-open="task:${esca(r.slug)}"', html)
        for w in ("next_tick", "checkout_busy", "gradle_cap", "usage_hold", "paused",
                  "budget", "cooldown", "capacity"):
            self.assertIn(w + ":[", html, w)                          # one badge per wait
        self.assertNotIn("https://", html)
        self.assertNotIn("http://", html)

    def test_collect_queue_writes_nothing(self):
        def tree():
            return sorted((p.relative_to(self.tmp).as_posix(),
                           p.read_bytes() if p.is_file() else b"")
                          for p in self.tmp.rglob("*"))
        before = tree()
        M.collect_queue()
        self.assertEqual(tree(), before)
