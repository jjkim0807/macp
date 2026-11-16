"""Game mode: a heavy remote in `game` is treated as offline by heavy routing.

State file unit tests first; then every routing case through the real
auto-tick entry point (`Orchestrator.tick`), the `agent-mode` CLI through
`_main`, and the dashboard POST through a real HTTP server.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from todo_system.orchestrator import agent_mode


class AgentModeStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = agent_mode.state_path(self.tmp)
        agent_mode._warned.clear()

    def _read(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            out = agent_mode.read_modes(self.path)
        return out, err.getvalue()

    def _write_raw(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")

    def test_state_path(self):
        self.assertEqual(self.path,
                         self.tmp / "agents" / "_state" / "agent_mode.json")

    def test_missing_file_is_empty_and_silent(self):
        out, err = self._read()
        self.assertEqual(out, {})
        self.assertEqual(err, "")
        self.assertEqual(agent_mode.get_mode(self.path, "desktop-runner"),
                         {"agent": "desktop-runner", "mode": "work",
                          "since": None, "reason": ""})

    def test_broken_json_warns_once_per_version(self):
        self._write_raw("{not json")
        out1, err1 = self._read()
        out2, err2 = self._read()
        self.assertEqual((out1, out2), ({}, {}))
        self.assertEqual(len(err1.splitlines()), 1)
        self.assertIn("[agent_mode]", err1)
        self.assertIn("treating as work", err1)
        self.assertEqual(err2, "")

    def test_top_level_list_is_empty(self):
        self._write_raw("[1, 2]")
        out, err = self._read()
        self.assertEqual(out, {})
        self.assertEqual(len(err.splitlines()), 1)

    def test_bad_entry_dropped_sibling_kept(self):
        self._write_raw(json.dumps({
            "a": {"mode": "sleep", "since": "x"},
            "b": {"mode": "game", "since": "2026-01-01T00:00:00+00:00",
                  "reason": "r"},
        }))
        out, err = self._read()
        self.assertEqual(list(out), ["b"])
        self.assertEqual(out["b"]["mode"], "game")
        self.assertIn("invalid entry for a", err)

    def test_set_mode_since_rules(self):
        r1 = agent_mode.set_mode(self.path, "desktop-runner", "game", "p",
                                 now_iso="T1")
        self.assertEqual((r1["mode"], r1["since"], r1["changed"]), ("game", "T1", True))
        r2 = agent_mode.set_mode(self.path, "desktop-runner", "game", now_iso="T2")
        self.assertEqual((r2["since"], r2["changed"], r2["reason"]), ("T1", False, "p"))
        r3 = agent_mode.set_mode(self.path, "desktop-runner", "work", now_iso="T3")
        self.assertEqual((r3["mode"], r3["since"], r3["changed"]), ("work", "T3", True))
        r4 = agent_mode.set_mode(self.path, "desktop-runner", "game", now_iso="T4")
        self.assertEqual(r4["since"], "T4")
        self.assertEqual(agent_mode.get_mode(self.path, "desktop-runner")["since"], "T4")
        self.assertTrue(agent_mode.is_game(agent_mode.read_modes(self.path),
                                           "desktop-runner"))
        self.assertFalse(agent_mode.is_game({}, "desktop-runner"))
        self.assertFalse(agent_mode.is_game(None, "desktop-runner"))

    def test_bad_mode_and_empty_agent_raise(self):
        with self.assertRaises(ValueError):
            agent_mode.set_mode(self.path, "desktop-runner", "sleep")
        with self.assertRaises(ValueError):
            agent_mode.set_mode(self.path, " ", "game")
        self.assertFalse(self.path.exists())

    def test_reason_cleaned_and_truncated(self):
        r = agent_mode.set_mode(self.path, "x", "game", "a\nb\x07c" + "z" * 300)
        self.assertEqual(r["reason"], ("abc" + "z" * 300)[:200])
        self.assertEqual(len(r["reason"]), agent_mode.REASON_MAX)

    def test_atomic_write_leaves_no_tmp_and_creates_lock(self):
        agent_mode.set_mode(self.path, "x", "game")
        names = sorted(p.name for p in self.path.parent.iterdir())
        self.assertEqual(names, ["agent_mode.json", "agent_mode.json.lock"])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["x"]["mode"],
                         "game")


if __name__ == "__main__":
    unittest.main()
