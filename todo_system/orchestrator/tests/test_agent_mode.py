"""Game mode: a heavy remote in `game` is treated as offline by heavy routing.

State file unit tests first; then every routing case through the real
auto-tick entry point (`Orchestrator.tick`), the `agent-mode` CLI through
`_main`, and the dashboard POST through a real HTTP server.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import agent_mode
from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import message as m
from todo_system.orchestrator import orchestrator as orch_mod
from todo_system.orchestrator.assignment import Assignment
# `_Base` has no test methods, so importing it collects nothing twice; the
# TestCase subclasses of test_heavy_routing are deliberately NOT imported.
from todo_system.orchestrator.tests.test_heavy_routing import (
    DEFAULT,
    REMOTE,
    REPO,
    _Base,
    _iso_ago,
    _tick,
    _types,
)


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


# --- routing through the real tick ---------------------------------------------

OTHER_REPO = "https://example.invalid/acme/other-app.git"


class _GameBase(_Base):
    def setUp(self):
        super().setUp()
        agent_mode._warned.clear()
        self.mode_path = self.tmp / "agents" / "_state" / "agent_mode.json"

    def mode(self, agent_id: str, mode: str) -> dict:
        return agent_mode.set_mode(self.mode_path, agent_id, mode, "test")

    def _dispatch_remote(self, slug="app-build") -> Assignment:
        self.seen(REMOTE, 60)
        _tick(self.orch, [self.heavy_task(slug)])
        row = self.ledger.load(slug)
        self.assertEqual(row.agent_id, REMOTE)
        return row

    def _assert_rerouted(self, before: Assignment, reason: str) -> Assignment:
        row = self.ledger.load(before.task_id)
        self.assertEqual(row.agent_id, DEFAULT)
        self.assertEqual(row.kind, "ai-local")
        self.assertEqual(row.state, A.DISPATCHED)
        self.assertEqual(row.rerouted_from["agent"], REMOTE)
        self.assertEqual(row.rerouted_from["reason"], reason)
        self.assertEqual(row.rerouted_from["corr_id"], before.corr_id)
        self.assertNotEqual(row.corr_id, before.corr_id)
        self.assertEqual(row.fallback_agent, "")
        self.assertEqual(row.refail_count, before.refail_count)
        self.assertEqual(row.attempts, before.attempts)
        self.assertEqual(row.timeout_streak, before.timeout_streak)
        self.assertEqual(row.capacity_retry_count, before.capacity_retry_count)
        self.assertEqual(row.payload, before.payload)
        cancels = [e for e in self.inbox(REMOTE) if e.type == m.CANCEL]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0].corr_id, before.corr_id)
        self.assertIn("paused:game", cancels[0].payload["reason"])
        [env] = [e for e in self.inbox(DEFAULT) if e.type == m.TASK_ASSIGN]
        self.assertEqual(env.corr_id, row.corr_id)
        self.assertNotIn("requires", env.payload)
        return row


class GameModeTickTest(_GameBase):
    def test_game_remote_is_skipped_like_offline(self):
        self.seen(REMOTE, 60)
        self.mode(REMOTE, "game")
        _tick(self.orch, [self.heavy_task()])
        [env] = self.inbox(DEFAULT)
        self.assertNotIn("requires", env.payload)
        self.assertEqual(self.inbox(REMOTE), [])
        row = self.ledger.load("app-build")
        self.assertEqual((row.agent_id, row.fallback_agent), (DEFAULT, ""))

    def test_switch_to_game_reroutes_dispatched_row_next_tick(self):
        row = self._dispatch_remote()
        # well inside the accept timeout: only the game switch can move it
        res = _tick(self.orch, [self.heavy_task()])
        self.assertEqual(res["rerouted"], [])
        self.mode(REMOTE, "game")
        res = _tick(self.orch, [self.heavy_task()])
        self.assertEqual([a.task_id for a in res["rerouted"]], ["app-build"])
        self._assert_rerouted(row, "paused:game")

    def test_switch_to_game_reroutes_accepted_and_in_progress(self):
        for state in (A.ACCEPTED, A.IN_PROGRESS):
            with self.subTest(state=state):
                self.setUp()
                row = self._dispatch_remote()
                if state == A.ACCEPTED:
                    self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.ACCEPT, frm=REMOTE,
                                            task_id=row.task_id, corr_id=row.corr_id,
                                            payload={}))
                    _tick(self.orch, [self.heavy_task()], live=("collect",))
                else:
                    row.state = A.IN_PROGRESS
                    self.ledger.save(row)
                before = self.ledger.load("app-build")
                self.assertEqual(before.state, state)
                self.seen(REMOTE, 60)                     # still online
                self.mode(REMOTE, "game")
                res = _tick(self.orch, [self.heavy_task()])
                self.assertEqual([a.task_id for a in res["rerouted"]], ["app-build"])
                self._assert_rerouted(before, "paused:game")

    def test_counters_not_bumped(self):
        row = self._dispatch_remote()
        row.refail_count, row.capacity_retry_count, row.timeout_streak = 1, 2, 1
        self.ledger.save(row)
        before = self.ledger.load("app-build")
        self.mode(REMOTE, "game")
        _tick(self.orch, [self.heavy_task()])
        after = self._assert_rerouted(before, "paused:game")
        self.assertEqual((after.refail_count, after.capacity_retry_count,
                          after.timeout_streak), (1, 2, 1))

    def test_work_again_makes_remote_a_candidate(self):
        self._dispatch_remote()
        self.mode(REMOTE, "game")
        _tick(self.orch, [self.heavy_task()])
        self.assertEqual(self.ledger.load("app-build").agent_id, DEFAULT)
        self.mode(REMOTE, "work")
        self.seen(REMOTE, 60)
        # (a) a new heavy task goes to the remote again (another repo, so the
        # per-repo serial cap — app-build is in flight on default — is not hit)
        _tick(self.orch, [self.heavy_task("app-build-2", repo=OTHER_REPO)])
        new = self.ledger.load("app-build-2")
        self.assertEqual((new.agent_id, new.fallback_agent), (REMOTE, DEFAULT))
        # (b) the rerouted task, after its default run fails and the cooldown
        # passes, goes back to the remote — paused:game is no bar. The remote's
        # concurrency is 1, so free it first.
        new.state = A.DONE
        self.ledger.save(new)
        row = self.ledger.load("app-build")
        row.state = A.FAILED
        row.updated_ts = _iso_ago(10 * 3600)
        self.ledger.save(row)
        _tick(self.orch, [self.heavy_task()])
        again = self.ledger.load("app-build")
        self.assertEqual((again.agent_id, again.fallback_agent), (REMOTE, DEFAULT))

    def test_game_without_work_back_keeps_rerouted_task_off_remote(self):
        self._dispatch_remote()
        self.mode(REMOTE, "game")
        _tick(self.orch, [self.heavy_task()])
        row = self.ledger.load("app-build")
        row.state = A.FAILED
        row.updated_ts = _iso_ago(10 * 3600)
        self.ledger.save(row)
        self.seen(REMOTE, 60)
        _tick(self.orch, [self.heavy_task()])
        again = self.ledger.load("app-build")
        self.assertEqual(again.agent_id, DEFAULT)
        self.assertEqual(_types(self.inbox(REMOTE)).count(m.TASK_ASSIGN), 1)

    def test_broken_mode_file_fails_open_to_work(self):
        self.mode_path.parent.mkdir(parents=True, exist_ok=True)
        self.mode_path.write_text("{not json", encoding="utf-8")
        self.seen(REMOTE, 60)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _tick(self.orch, [self.heavy_task()])
        self.assertEqual(self.ledger.load("app-build").agent_id, REMOTE)
        lines = [ln for ln in err.getvalue().splitlines() if "[agent_mode]" in ln]
        self.assertEqual(len(lines), 1, err.getvalue())

    def test_project_owned_remote_row_is_not_rerouted_in_game(self):
        self.ledger.save(Assignment(task_id="owned", agent_id=REMOTE, corr_id="c1",
                                    kind="ai-remote", updated_ts=_iso_ago(60),
                                    payload={"repo": REPO}))
        self.seen(REMOTE, 60)
        self.mode(REMOTE, "game")
        res = _tick(self.orch, [])
        self.assertEqual(res["rerouted"], [])
        self.assertEqual(self.ledger.load("owned").agent_id, REMOTE)
        self.assertEqual(self.inbox(REMOTE), [])

    def test_paused_moves_nothing_in_game(self):
        self._dispatch_remote()
        self.mode(REMOTE, "game")
        with mock.patch.object(self.orch, "paused", return_value=True):
            self.assertEqual(self.orch.reroute_remote(dt.datetime.now().timestamp()), [])
        self.assertEqual(self.ledger.load("app-build").agent_id, REMOTE)

    def test_ledger_without_root_fails_open(self):
        class _NoRoot:
            pass
        orch = orch_mod.Orchestrator(self.mb, self.reg, _NoRoot(),
                                     notify=lambda *a, **k: None, default_uri=self.uri)
        with mock.patch.object(orch_mod.paths, "vault_root_or_cwd", return_value=self.tmp):
            self.assertEqual(orch._agent_mode_path(),
                             self.tmp / "Worker" / "agents" / "_state" / "agent_mode.json")
            self.assertEqual(orch.agent_modes(), {})


# --- the one-line CLI -----------------------------------------------------------

def _cli_registry(tmp: Path, uri: str) -> Path:
    p = tmp / "registry.yaml"
    p.write_text(
        "agents:\n"
        f"  - id: {DEFAULT}\n    kind: ai-local\n    projects: ['*']\n    mailbox: '{uri}'\n"
        f"  - id: {REMOTE}\n    kind: ai-remote\n    capabilities: [heavy]\n"
        f"    mailbox: '{uri}'\n"
        "  - id: owner\n    kind: human\n    notify: []\n",
        encoding="utf-8")
    return p


class AgentModeCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.uri = f"file://{self.tmp / 'agents'}"
        self.reg = _cli_registry(self.tmp, self.uri)
        self.state = self.tmp / "agents" / "_state" / "agent_mode.json"
        agent_mode._warned.clear()

    def _main(self, *cmd):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(orch_mod.paths, "vault_root_or_cwd",
                                  return_value=self.tmp):
            rc = orch_mod._main(["--registry", str(self.reg),
                                 "--ledger", str(self.tmp / "assignments"),
                                 "--uri", self.uri, *cmd])
        return rc, out.getvalue(), err.getvalue()

    def test_set_then_show(self):
        rc, out, _ = self._main("agent-mode", REMOTE, "game", "--reason", "playing")
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertEqual(len(lines), 1, out)
        self.assertIn(f"agent-mode {REMOTE}: work -> game since ", lines[0])
        self.assertIn("(reason: playing)", lines[0])
        data = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(data[REMOTE]["mode"], "game")
        self.assertEqual(data[REMOTE]["reason"], "playing")

        rc, out, _ = self._main("agent-mode", REMOTE)
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn(f"agent-mode {REMOTE}: game since ", out)

        rc, out, _ = self._main("agent-mode", REMOTE, "game")
        self.assertIn("game (unchanged since ", out)

        rc, out, _ = self._main("agent-mode", REMOTE, "work")
        self.assertEqual(rc, 0)
        self.assertIn("game -> work since ", out)

    def test_show_default_is_work(self):
        rc, out, _ = self._main("agent-mode", REMOTE)
        self.assertEqual((rc, out.strip()), (0, f"agent-mode {REMOTE}: work since -"))
        self.assertFalse(self.state.exists())

    def test_unknown_and_human_rejected(self):
        rc, _, err = self._main("agent-mode", "nope", "game")
        self.assertEqual(rc, 2)
        self.assertIn("unknown agent", err)
        rc, _, err = self._main("agent-mode", "owner", "game")
        self.assertEqual(rc, 2)
        self.assertIn("human", err)
        self.assertFalse(self.state.exists())

    def test_bad_mode_is_argparse_error(self):
        with self.assertRaises(SystemExit) as cm:
            self._main("agent-mode", REMOTE, "sleep")
        self.assertEqual(cm.exception.code, 2)

    def test_agents_shows_mode_only_for_game(self):
        rc, before, _ = self._main("agents")
        self.assertEqual(rc, 0)
        self.assertNotIn("mode:", before)
        self._main("agent-mode", REMOTE, "game")
        rc, out, _ = self._main("agents")
        [remote_line] = [ln for ln in out.splitlines() if REMOTE in ln]
        [default_line] = [ln for ln in out.splitlines() if DEFAULT in ln]
        self.assertIn(" mode: game since ", remote_line)
        self.assertNotIn("mode:", default_line)
        self.assertIn(default_line, before.splitlines())   # byte-identical


if __name__ == "__main__":
    unittest.main()
