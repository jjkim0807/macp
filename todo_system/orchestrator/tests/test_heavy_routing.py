"""Heavy-task routing: heavy tasks go to an online heavy-capable remote and come
back to the default agent when that remote goes quiet.

Every routing / reroute case below runs through the real auto-tick entry point
(`Orchestrator.tick`) with an injected registry and injected `agent_seen.json`
times — not through the parts in isolation.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import message as m
from todo_system.orchestrator import repos, tasks
from todo_system.orchestrator.assignment import Assignment, Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import (
    REMOTE_ACCEPT_TIMEOUT_ENV,
    REMOTE_ONLINE_ENV,
    Orchestrator,
    Task,
    is_heavy,
)
from todo_system.orchestrator.registry import Agent, Registry
from todo_system.tests.test_orchestrator_tasks import _make_db, _row

DEFAULT = "worker-default"
REMOTE = "desktop-runner"
REPO = "https://example.invalid/acme/heavy-app.git"

# The tick reads machine state through these; the routing/reroute logic is the
# subject here, so every other sweep is stubbed (same seam as
# test_orchestrator._hermetic_tick, plus a `now` the reroute clock needs).
_STUBBED = ("collect", "reconcile_merged", "reconcile_unblocked",
            "reconcile_unparked", "reconcile_closed", "reconcile_queue",
            "reconcile_parents", "renudge_due", "reap_stale",
            "reconcile_resumes", "reconcile_rescope", "reconcile_split_parents",
            "reconcile_merge_holds", "reconcile_human_ready",
            "_advote_bridge_tick", "_beta_sweep_tick", "_project_stall_tick")


def _tick(orch, task_list, *, now=None, live=()):
    with contextlib.ExitStack() as st:
        for name in _STUBBED:
            if name not in live:
                st.enter_context(mock.patch.object(orch, name, return_value=[]))
        for name, value in (("paused", False), ("budget_exceeded", False),
                            ("usage_hold", None)):
            st.enter_context(mock.patch.object(orch, name, return_value=value))
        return orch.tick(task_list, now)


def _iso_ago(seconds: float) -> str:
    return (dt.datetime.now().astimezone()
            - dt.timedelta(seconds=seconds)).isoformat(timespec="seconds")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.uri = f"file://{self.tmp / 'agents'}"
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([
            Agent(id=DEFAULT, kind="ai-local", projects=["*"], mailbox=self.uri),
            Agent(id=REMOTE, kind="ai-remote", projects=[], capabilities=["heavy"],
                  concurrency=1, mailbox=self.uri),
        ])
        self.orch = Orchestrator(self.mb, self.reg, self.ledger,
                                 notify=lambda *a, **k: None,
                                 default_uri=self.uri)
        # A shared clone root with a gradlew → the repo task is heavy.
        self.clone = self.tmp / "clone"
        self.clone.mkdir()
        (self.clone / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
        p = mock.patch.object(repos, "local_path", return_value=self.clone)
        p.start()
        self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in (REMOTE_ONLINE_ENV, REMOTE_ACCEPT_TIMEOUT_ENV):
            os.environ.pop(k, None)

    # -- fixtures -------------------------------------------------------------
    def seen(self, agent_id: str, age_s: float | None) -> None:
        p = self.tmp / "_state" / "agent_seen.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        if age_s is None:
            data.pop(agent_id, None)
        else:
            data[agent_id] = _iso_ago(age_s)
        p.write_text(json.dumps(data), encoding="utf-8")

    def heavy_task(self, slug: str = "app-build", **extra) -> Task:
        return Task(slug, {"summary": slug, "branch": f"worker/{slug}",
                           "repo": REPO, **extra})

    def inbox(self, agent_id: str) -> list[m.Envelope]:
        return self.mb.receive(agent_id)


class IsHeavyTableTest(unittest.TestCase):
    def test_table(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        gradle = tmp / "g"
        gradle.mkdir()
        (gradle / "gradlew").write_text("", encoding="utf-8")
        plain = tmp / "p"
        plain.mkdir()
        cases = [
            # (payload, repo_dir, expected)
            ({"heavy": True}, None, True),
            ({"heavy": "true"}, None, True),
            ({"heavy": False}, None, False),
            ({"heavy": False}, gradle, False),                     # opt-out wins
            ({"heavy": False, "long_job": "x"}, None, False),
            ({"heavy": False, "heavy_signals": ["emulator"]}, None, False),
            ({"heavy_signals": ["emulator"]}, None, True),
            ({"heavy_signals": ["screen_check"]}, None, True),
            ({"emulator": True}, None, True),
            ({"screen_check": "true"}, None, True),
            ({"screen_check": False}, None, False),
            ({"long_job": "python3 collect.py"}, None, True),
            ({"repo": REPO}, gradle, True),                        # gradlew at root
            ({"repo": REPO}, plain, False),                        # no gradlew
            ({"summary": "vault task"}, None, False),              # vault, no repo
            ({}, None, False),
            (None, None, False),
        ]
        for payload, repo_dir, want in cases:
            with self.subTest(payload=payload, repo_dir=repo_dir):
                self.assertIs(is_heavy(payload, repo_dir), want)


class LoaderWiringTest(unittest.TestCase):
    """`heavy` / emulator / screen_check reach the dispatch payload through the
    REAL loader (load_dispatchable), and an untouched task's payload has no key."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _md(self, name: str, fm: str) -> str:
        p = self.tmp / f"{name}.md"
        p.write_text(f"---\nstatus: open\nworker_mode: auto\n{fm}---\n# {name}\n",
                     encoding="utf-8")
        return str(p)

    def test_loader_carries_heavy_only_when_present(self):
        rows = [
            _row("on", self._md("on", "heavy: true\n")),
            _row("off", self._md("off", "heavy: false\n")),
            _row("none", self._md("none", "")),
            _row("emu", self._md("emu", "emulator: true\nscreen_check: true\n")),
        ]
        got = {t.slug: t.payload
               for t in tasks.load_dispatchable(_make_db(self.tmp, rows))}
        self.assertIs(got["on"]["heavy"], True)
        self.assertIs(got["off"]["heavy"], False)
        self.assertNotIn("heavy", got["none"])
        self.assertNotIn("heavy_signals", got["none"])
        self.assertEqual(got["emu"]["heavy_signals"], ["emulator", "screen_check"])
        # screen_check is NOT put under its own name: the runner's screen check
        # reads payload["screen_check"] and routing must not change what it does.
        self.assertNotIn("screen_check", got["emu"])
        self.assertTrue(is_heavy(got["on"], None))
        self.assertFalse(is_heavy(got["off"], None))
        self.assertFalse(is_heavy(got["none"], None))
        self.assertTrue(is_heavy(got["emu"], None))


class TickRoutingTest(_Base):
    def test_heavy_task_goes_to_online_heavy_remote(self):
        self.seen(REMOTE, 60)
        res = _tick(self.orch, [self.heavy_task()])
        self.assertEqual(len(res["dispatched"]), 1)
        [env] = self.inbox(REMOTE)
        self.assertEqual(env.type, m.TASK_ASSIGN)
        self.assertEqual(env.payload["requires"], ["heavy"])
        self.assertEqual(self.inbox(DEFAULT), [])
        row = self.ledger.load("app-build")
        self.assertEqual(row.agent_id, REMOTE)
        self.assertEqual(row.kind, "ai-remote")
        self.assertEqual(row.fallback_agent, DEFAULT)
        self.assertNotIn("requires", row.payload)      # envelope only

    def _assert_default(self, slug="app-build"):
        [env] = self.inbox(DEFAULT)
        self.assertNotIn("requires", env.payload)
        self.assertEqual(self.inbox(REMOTE), [])
        row = self.ledger.load(slug)
        self.assertEqual(row.agent_id, DEFAULT)
        self.assertEqual(row.fallback_agent, "")

    def test_stale_remote_falls_back_to_default(self):
        self.seen(REMOTE, 1000)
        _tick(self.orch, [self.heavy_task()])
        self._assert_default()

    def test_dead_remote_falls_back_to_default(self):
        self.seen(REMOTE, 5000)
        _tick(self.orch, [self.heavy_task()])
        self._assert_default()

    def test_unknown_remote_falls_back_to_default(self):
        self.seen(REMOTE, None)
        _tick(self.orch, [self.heavy_task()])
        self._assert_default()

    def test_online_threshold_comes_from_env(self):
        self.seen(REMOTE, 60)
        os.environ[REMOTE_ONLINE_ENV] = "30"
        _tick(self.orch, [self.heavy_task()])
        self._assert_default()

    def test_non_heavy_task_routing_is_unchanged(self):
        self.seen(REMOTE, 60)
        (self.clone / "gradlew").unlink()                 # no gradle → not heavy
        t = self.heavy_task()
        _tick(self.orch, [t])
        [env] = self.inbox(DEFAULT)
        self.assertEqual(env.payload, t.payload)          # byte-identical payload
        self.assertEqual(self.inbox(REMOTE), [])
        row = self.ledger.load("app-build")
        self.assertEqual((row.agent_id, row.fallback_agent), (DEFAULT, ""))

    def test_heavy_false_opt_out_stays_on_default(self):
        self.seen(REMOTE, 60)
        _tick(self.orch, [self.heavy_task(heavy=False)])
        self._assert_default()

    def test_remote_at_concurrency_sends_heavy_task_to_default(self):
        self.seen(REMOTE, 60)
        self.ledger.save(Assignment(task_id="other-heavy", agent_id=REMOTE,
                                    corr_id="c0", state=A.IN_PROGRESS,
                                    kind="ai-remote",
                                    payload={"repo": "https://example.invalid/o.git"}))
        _tick(self.orch, [self.heavy_task()])
        self._assert_default()

    def test_slug_only_route_is_the_plain_owner(self):
        self.seen(REMOTE, 60)
        self.assertEqual(self.orch.route("app-build").id, DEFAULT)

    def test_project_owned_remote_is_never_bypassed(self):
        # A remote that OWNS a project keeps it, heavy or not.
        reg = Registry([
            Agent(id=DEFAULT, kind="ai-local", projects=["*"], mailbox=self.uri),
            Agent(id="win-only", kind="ai-remote", projects=["winapp-*"],
                  mailbox=self.uri),
            Agent(id=REMOTE, kind="ai-remote", capabilities=["heavy"], mailbox=self.uri),
        ])
        orch = Orchestrator(self.mb, reg, self.ledger, notify=lambda *a, **k: None,
                            default_uri=self.uri)
        self.seen(REMOTE, 60)
        _tick(orch, [self.heavy_task("winapp-1")])
        self.assertEqual(len(self.inbox("win-only")), 1)
        self.assertEqual(self.ledger.load("winapp-1").fallback_agent, "")


if __name__ == "__main__":
    unittest.main()
