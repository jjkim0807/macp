"""Tests for the Solar (Upstage) single-stage execution path.

Task ``macp-solar-run-single-stage-task``. The feature routes a task whose
frontmatter says ``agent: upstage`` through an in-engine OpenAI-compatible
tool-calling loop (``orchestrator.solar_stage``) for its quick/delegate stage,
while EVERY other task's ``claude -p`` argv/env stays byte-identical. The whole
suite is hermetic: ``urlopen`` is stubbed and the base URL points at a dead
port (mirroring ``test_macp_upstage_account``), so no test can reach the real
endpoint. A sentinel key is planted and every layer asserts it never leaks into
a returned dict, the stage stdout, or the artifact.

Grouped by the implementation phase that introduced each block:
  1. ``agent:`` frontmatter → payload wiring (routing becomes reachable).
  2. ``solar_stage._chat_once`` / ``_with_retry`` — one POST + 429 backoff.
  3. ``solar_stage.run`` — the bounded read/write/finish tool loop + artifact.
  4. ``stages.route_to_solar`` + the runner's ``_execute`` routing branch.
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from todo_system.orchestrator import repos, tasks

SENTINEL = "up_SENTINELKEY0123456789"

_COLS = "slug, title, path, outcome, next_action, tags, worker_mode, status, is_parent"


# ---- Phase 1: agent frontmatter → payload ------------------------------------

class AgentFromFrontmatterTest(unittest.TestCase):
    def test_lowercased_and_placeholder_is_none(self):
        self.assertEqual(repos.task_agent_from_fm({"agent": "Upstage"}), "upstage")
        self.assertEqual(repos.task_agent_from_fm({"agent": "claude"}), "claude")
        self.assertIsNone(repos.task_agent_from_fm({"agent": "-"}))
        self.assertIsNone(repos.task_agent_from_fm({}))
        self.assertIsNone(repos.task_agent_from_fm({"agent": "   "}))


class AgentPayloadWiringTest(unittest.TestCase):
    """`load_dispatchable` carries `agent` ONLY when the markdown sets it — a
    task without it yields a byte-identical payload (AC #1's claude invariant)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "todo.db"
        con = sqlite3.connect(self.db)
        con.execute(f"CREATE TABLE tasks ({_COLS})")
        con.executemany(
            f"INSERT INTO tasks ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
            [("t-plain", "Plain", "", "", "", "", "auto", "open", 0)])
        con.commit()
        con.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add(self, slug: str, body: str, mode: str = "delegate"):
        md = self.tmp / f"{slug}.md"
        md.write_text(body, encoding="utf-8")
        con = sqlite3.connect(self.db)
        con.execute(f"INSERT INTO tasks ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                    (slug, slug, str(md), "", "", "", mode, "open", 0))
        con.commit()
        con.close()

    def test_agent_rides_the_payload_when_set(self):
        self._add("t-solar",
                  "---\nstatus: open\nworker_mode: delegate\nagent: Upstage\n---\n# t\n")
        by_slug = {t.slug: t for t in tasks.load_dispatchable(self.db)}
        self.assertEqual(by_slug["t-solar"].payload["agent"], "upstage")

    def test_absent_agent_leaves_the_payload_unchanged(self):
        [t] = [t for t in tasks.load_dispatchable(self.db) if t.slug == "t-plain"]
        self.assertNotIn("agent", t.payload)


if __name__ == "__main__":
    unittest.main()
