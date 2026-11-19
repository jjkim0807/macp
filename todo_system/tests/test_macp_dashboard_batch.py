"""/api/status and the web page show batch verify (docs/batch-verify.md).

* every ledger row carries a bool `batch_ready` next to its display `state`;
* the response has a top-level `batch_ready[]` — open rows in state
  `batch_ready`, each with `group` — kept out of `active` and `attention`;
* the page has a `c-batch` card (hidden when empty) and `badge()` labels the
  state in the page language.

Runs through the real collect_status() / render_html() over the hermetic tmp
fixture of test_macp_dashboard (no vault, no network).
"""
from __future__ import annotations

import json
import sqlite3
import unittest

from todo_system.tests.test_macp_dashboard import M, _install_status_fixture

REPO = "https://example.test/acme/app.git"

# collect_status() top-level keys before batch verify existed.
_PRE_BATCH_KEYS = {
    "account", "active", "agents", "attention", "deps", "human", "human_later",
    "now", "paused", "projects", "queue", "queue_blocked", "queue_upcoming",
    "recent", "runs", "runs_today", "todo", "usage", "worker_busy",
    "worker_state", "worker_state_detail",
}


def _ledger(tmp, slug, **extra):
    (tmp / "assign" / f"{slug}.json").write_text(json.dumps(
        {"task_id": slug, "agent_id": "w1", "payload": {"repo": REPO}, **extra}),
        encoding="utf-8")


def _todo(tmp, slug, status="open"):
    con = sqlite3.connect(tmp / "todo.db")
    con.execute("INSERT INTO tasks VALUES (?,?,?,'-',NULL,'',?,NULL,0,?)",
                (slug, f"{slug} title", status, "todo,work", "auto"))
    con.commit()
    con.close()


class BatchStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = _install_status_fixture(self)
        # The fixture already has an in_progress and a failed row; add a
        # batch_ready row, an awaiting_human row and a row the batch gate sent back.
        _todo(self.tmp, "fx-batch-5-ready")
        _ledger(self.tmp, "fx-batch-5-ready", state="batch_ready",
                last_note="묶음 검증 대기 — quick Verify 통과, 통합 게이트 뒤 머지",
                updated_ts="2026-01-01T00:00:05",
                history=[{"ts": "2026-01-01T00:00:05", "type": "batch-ready"}])
        _todo(self.tmp, "fx-batch-6-park")
        _ledger(self.tmp, "fx-batch-6-park", state="awaiting_human",
                updated_ts="2026-01-01T00:00:06")
        _todo(self.tmp, "fx-batch-7-back")
        _ledger(self.tmp, "fx-batch-7-back", state="failed",
                updated_ts="2026-01-01T00:00:07",
                history=[{"ts": "2026-01-01T00:00:07", "type": "batch-returned",
                          "reason": "gate-failed"}])

    # (a) every row carries the bool; only the batch_ready row is true
    def test_rows_carry_batch_ready_bool(self):
        rows = {r["task"]: r for r in M.collect_assignments()}
        self.assertEqual(rows["fx-batch-5-ready"]["state"], "batch_ready")
        self.assertIs(rows["fx-batch-5-ready"]["batch_ready"], True)
        for slug in ("fx-active-1-run", "fx-attn-2-fail", "fx-batch-6-park",
                     "fx-batch-7-back"):
            self.assertIs(rows[slug]["batch_ready"], False, slug)
        # the returned row is a deferral, not a failure and not batch_ready
        self.assertEqual(rows["fx-batch-7-back"]["state"], "deferred")

    # (b) the top-level list, its row shape, and its separation from active/attention
    def test_status_lists_batch_ready_apart(self):
        d = M.collect_status()
        self.assertEqual([r["task"] for r in d["batch_ready"]], ["fx-batch-5-ready"])
        row = d["batch_ready"][0]
        for key in ("task", "state", "batch_ready", "note", "age", "title",
                    "project", "group"):
            self.assertIn(key, row)
        self.assertEqual(row["state"], "batch_ready")
        self.assertIs(row["batch_ready"], True)
        self.assertEqual(row["title"], "fx-batch-5-ready title")
        self.assertIsInstance(row["group"], str)
        self.assertNotIn("fx-batch-5-ready", {r["task"] for r in d["active"]})
        self.assertNotIn("fx-batch-5-ready", {r["task"] for r in d["attention"]})
        # recent rows carry the bool too
        recent = {r["task"]: r for r in d["recent"]}
        self.assertIs(recent["fx-batch-5-ready"]["batch_ready"], True)
        self.assertIs(recent["fx-active-1-run"]["batch_ready"], False)
        # the returned row is waiting, not in attention, not in batch_ready
        self.assertNotIn("fx-batch-7-back", {r["task"] for r in d["attention"]})
        # the other rows still land where they did
        self.assertIn("fx-active-1-run", {r["task"] for r in d["active"]})
        self.assertIn("fx-attn-2-fail", {r["task"] for r in d["attention"]})
        self.assertIn("fx-batch-6-park", {r["task"] for r in d["attention"]})

    # (c) a batch_ready row whose todo is closed is dropped
    def test_closed_todo_dropped(self):
        con = sqlite3.connect(self.tmp / "todo.db")
        con.execute("UPDATE tasks SET status='done' WHERE slug='fx-batch-5-ready'")
        con.commit()
        con.close()
        self.assertEqual(M.collect_status()["batch_ready"], [])


class NoBatchStatusTest(unittest.TestCase):
    # (d) no batch rows: empty list, and the only new top-level key is batch_ready
    def test_empty_and_key_set(self):
        _install_status_fixture(self)
        d = M.collect_status()
        self.assertEqual(d["batch_ready"], [])
        self.assertEqual(set(d), _PRE_BATCH_KEYS | {"batch_ready"})
        for r in d["recent"]:
            self.assertIs(r["batch_ready"], False)
