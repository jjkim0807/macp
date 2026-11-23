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
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import message as m
from todo_system.orchestrator import repos, stages, tasks
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator.assignment import Assignment, Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import (
    REMOTE_ACCEPT_TIMEOUT_ENV,
    REMOTE_ONLINE_ENV,
    Orchestrator,
    Task,
    is_heavy,
    needs_device,
)
from todo_system.orchestrator.registry import Agent, Registry
from todo_system.orchestrator.runner import ExecOutcome, Runner, StagedExecutor
from todo_system.tests.test_orchestrator_tasks import _make_db, _row

DEFAULT = "worker-default"
REMOTE = "desktop-runner"
REPO = "https://example.invalid/acme/heavy-app.git"

# The tick reads machine state through these; the routing/reroute logic is the
# subject here, so every other sweep is stubbed (same seam as
# test_orchestrator._hermetic_tick, plus a `now` the reroute clock needs).
_STUBBED = ("collect", "reconcile_merged", "reconcile_closed", "reconcile_queue",
            "reconcile_parents", "reap_stale",
            "reconcile_resumes", "reconcile_rescope", "reconcile_split_parents",
            "reconcile_human_ready",
            "_advote_bridge_tick", "_beta_sweep_tick")


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


def _types(envs):
    return [e.type for e in envs]


class TickRerouteTest(_Base):
    """The reroute sweep runs at the top of the real tick."""

    def _dispatch_remote(self, slug="app-build") -> Assignment:
        self.seen(REMOTE, 60)
        _tick(self.orch, [self.heavy_task(slug)])
        row = self.ledger.load(slug)
        self.assertEqual(row.agent_id, REMOTE)
        return row

    def _reply(self, row: Assignment, typ: str, payload=None, corr=None):
        self.mb.send(m.Envelope(to=m.ORCHESTRATOR, type=typ, frm=REMOTE,
                                task_id=row.task_id, corr_id=corr or row.corr_id,
                                payload=payload or {}))

    def _assert_rerouted(self, before: Assignment, reason: str) -> Assignment:
        row = self.ledger.load(before.task_id)
        self.assertEqual(row.agent_id, DEFAULT)
        self.assertEqual(row.kind, "ai-local")
        self.assertEqual(row.state, A.DISPATCHED)
        self.assertEqual(row.rerouted_from["agent"], REMOTE)
        self.assertEqual(row.rerouted_from["reason"], reason)
        self.assertEqual(row.rerouted_from["corr_id"], before.corr_id)
        self.assertTrue(row.rerouted_from["ts"])
        self.assertNotEqual(row.corr_id, before.corr_id)
        self.assertEqual(row.fallback_agent, "")
        # not the task's fault
        self.assertEqual(row.refail_count, before.refail_count)
        self.assertEqual(row.attempts, before.attempts)
        self.assertEqual(row.timeout_streak, before.timeout_streak)
        self.assertEqual(row.capacity_retry_count, before.capacity_retry_count)
        self.assertEqual(row.payload, before.payload)
        self.assertEqual(row.history[-1]["type"], "rerouted")
        # remote gets a CANCEL for the OLD run
        cancels = [e for e in self.inbox(REMOTE) if e.type == m.CANCEL]
        self.assertEqual(len(cancels), 1)
        self.assertEqual(cancels[0].corr_id, before.corr_id)
        # default gets the task, with its own payload (no `requires`)
        [env] = [e for e in self.inbox(DEFAULT) if e.type == m.TASK_ASSIGN]
        self.assertEqual(env.corr_id, row.corr_id)
        self.assertNotIn("requires", env.payload)
        return row

    def test_accept_timeout_reroutes_to_default(self):
        row = self._dispatch_remote()
        res = _tick(self.orch, [self.heavy_task()], now=dt.datetime.now().timestamp() + 599)
        self.assertEqual(res["rerouted"], [])
        self.assertEqual(self.ledger.load("app-build").agent_id, REMOTE)
        res = _tick(self.orch, [self.heavy_task()], now=dt.datetime.now().timestamp() + 601)
        self.assertEqual([a.task_id for a in res["rerouted"]], ["app-build"])
        self.assertEqual(res["dispatched"], [])          # same tick: row is busy
        self._assert_rerouted(row, "accept-timeout")

    def test_accept_timeout_comes_from_env(self):
        os.environ[REMOTE_ACCEPT_TIMEOUT_ENV] = "60"
        row = self._dispatch_remote()
        _tick(self.orch, [self.heavy_task()], now=dt.datetime.now().timestamp() + 61)
        self._assert_rerouted(row, "accept-timeout")

    def test_stale_after_accept_reroutes(self):
        row = self._dispatch_remote()
        self._reply(row, m.ACCEPT)
        _tick(self.orch, [self.heavy_task()], live=("collect",))
        self.assertEqual(self.ledger.load("app-build").state, A.ACCEPTED)
        # still online → stays put
        self.seen(REMOTE, 60)
        res = _tick(self.orch, [self.heavy_task()])
        self.assertEqual(res["rerouted"], [])
        self.assertEqual(self.ledger.load("app-build").agent_id, REMOTE)
        # goes stale with no RESULT/FAIL → back to default
        self.seen(REMOTE, 1000)
        before = self.ledger.load("app-build")
        _tick(self.orch, [self.heavy_task()])
        self._assert_rerouted(before, "agent-stale")

    def test_reject_reroutes_without_refail_and_bars_that_remote(self):
        row = self._dispatch_remote()
        self._reply(row, m.REJECT, {"reason": "missing capability"})
        _tick(self.orch, [self.heavy_task()], live=("collect",))
        before = self.ledger.load("app-build")
        self.assertEqual(before.state, A.REJECTED)
        _tick(self.orch, [self.heavy_task()])
        after = self._assert_rerouted(before, "rejected")
        self.assertEqual(after.refail_count, 0)
        # The default run later fails; after the cooldown the task re-dispatches —
        # never back to the remote that refused it, even though it is online.
        after.state = A.FAILED
        after.updated_ts = _iso_ago(10 * 3600)
        self.ledger.save(after)
        self.seen(REMOTE, 60)
        _tick(self.orch, [self.heavy_task()])
        again = self.ledger.load("app-build")
        self.assertEqual(again.agent_id, DEFAULT)
        self.assertEqual(again.rerouted_from["reason"], "rejected")
        self.assertEqual(_types(self.inbox(REMOTE)).count(m.TASK_ASSIGN), 1)

    def test_timeout_rerouted_task_returns_to_remote_once_online(self):
        self._dispatch_remote()
        _tick(self.orch, [self.heavy_task()], now=dt.datetime.now().timestamp() + 601)
        row = self.ledger.load("app-build")
        self.assertEqual(row.agent_id, DEFAULT)
        # Same tick and following ticks: the rerouted row is busy → no bounce.
        self.seen(REMOTE, 60)
        _tick(self.orch, [self.heavy_task()])
        self.assertEqual(self.ledger.load("app-build").agent_id, DEFAULT)
        # Default run fails; after the cooldown a re-dispatch sees the remote
        # online again and sends it there (accept-timeout is not a bar).
        row = self.ledger.load("app-build")
        row.state = A.FAILED
        row.updated_ts = _iso_ago(10 * 3600)
        self.ledger.save(row)
        _tick(self.orch, [self.heavy_task()])
        again = self.ledger.load("app-build")
        self.assertEqual(again.agent_id, REMOTE)
        self.assertEqual(again.fallback_agent, DEFAULT)
        self.assertEqual(_types(self.inbox(REMOTE)).count(m.TASK_ASSIGN), 2)

    def test_project_owned_remote_row_is_never_rerouted(self):
        self.ledger.save(Assignment(task_id="owned", agent_id=REMOTE, corr_id="c1",
                                    kind="ai-remote", updated_ts=_iso_ago(86400),
                                    payload={"repo": REPO}))
        self.seen(REMOTE, None)
        res = _tick(self.orch, [], now=dt.datetime.now().timestamp() + 86400)
        self.assertEqual(res["rerouted"], [])
        row = self.ledger.load("owned")
        self.assertEqual((row.agent_id, row.state, row.rerouted_from),
                         (REMOTE, A.DISPATCHED, {}))
        self.assertEqual(self.inbox(REMOTE), [])

    def test_paused_moves_nothing(self):
        self._dispatch_remote()
        with mock.patch.object(self.orch, "paused", return_value=True):
            self.assertEqual(
                self.orch.reroute_remote(dt.datetime.now().timestamp() + 601), [])
        self.assertEqual(self.ledger.load("app-build").agent_id, REMOTE)

    def test_late_messages_from_the_old_remote_change_nothing(self):
        row = self._dispatch_remote()
        _tick(self.orch, [self.heavy_task()], now=dt.datetime.now().timestamp() + 601)
        moved = self.ledger.load("app-build")
        for typ in (m.ACCEPT, m.PROGRESS, m.RESULT, m.FAIL):
            self._reply(row, typ, {"summary": "late"}, corr=row.corr_id)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _tick(self.orch, [self.heavy_task()], live=("collect",))
        now_row = self.ledger.load("app-build")
        self.assertEqual(now_row.state, A.DISPATCHED)
        self.assertEqual(now_row.agent_id, DEFAULT)
        self.assertEqual(now_row.corr_id, moved.corr_id)
        self.assertEqual(now_row.history, moved.history)
        late = [ln for ln in out.getvalue().splitlines() if "ignoring late" in ln]
        self.assertEqual(len(late), 4, out.getvalue())
        self.assertTrue(all("rerouted to worker-default" in ln for ln in late))

    def test_reap_is_not_shielded_by_the_old_remote_envelope(self):
        self._dispatch_remote()
        _tick(self.orch, [self.heavy_task()], now=dt.datetime.now().timestamp() + 601)
        # The default runner picked its envelope up; the remote's stale copy of
        # the OLD dispatch is still unacked in the remote inbox.
        self.mb.ack(DEFAULT, self.inbox(DEFAULT))
        self.assertIn(m.TASK_ASSIGN, _types(self.inbox(REMOTE)))
        row = self.ledger.load("app-build")
        row.updated_ts = _iso_ago(2 * 86400)
        self.ledger.save(row)
        res = _tick(self.orch, [], live=("reap_stale",))
        self.assertEqual([a.task_id for a in res["reaped"]], ["app-build"])

    def test_reap_still_skips_a_queued_non_rerouted_row(self):
        self.seen(REMOTE, None)
        _tick(self.orch, [self.heavy_task()])            # → default, unacked
        row = self.ledger.load("app-build")
        row.updated_ts = _iso_ago(2 * 86400)
        self.ledger.save(row)
        res = _tick(self.orch, [], live=("reap_stale",))
        self.assertEqual(res["reaped"], [])


_DASH_SRC = Path(__file__).resolve().parents[3] / "scripts" / "macp_dashboard.py"


def _load_dashboard():
    import importlib.util
    spec = importlib.util.spec_from_file_location("macp_dashboard_heavy", _DASH_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ObservabilityTest(_Base):
    """status · trace · /api/status show the assigned agent and rerouted_from."""

    def _rerouted(self):
        self.seen(REMOTE, 60)
        _tick(self.orch, [self.heavy_task()])
        _tick(self.orch, [self.heavy_task()], now=dt.datetime.now().timestamp() + 601)
        return self.ledger.load("app-build")

    def test_status_line(self):
        from todo_system.orchestrator.orchestrator import status_line
        self.seen(REMOTE, 60)
        _tick(self.orch, [self.heavy_task()])
        heavy = status_line(self.ledger.load("app-build"))
        self.assertIn(REMOTE, heavy)
        self.assertIn(f"(heavy; fallback {DEFAULT})", heavy)
        row = self._rerouted_from_existing()
        line = status_line(row)
        self.assertIn(DEFAULT, line)
        self.assertIn(f"rerouted from {REMOTE} (accept-timeout @ ", line)
        # an ordinary row keeps the historical format exactly
        plain = Assignment(task_id="t", agent_id=DEFAULT, corr_id="c")
        self.assertEqual(status_line(plain), f"{'dispatched':14s} {'t':40s} {DEFAULT}")

    def _rerouted_from_existing(self):
        _tick(self.orch, [self.heavy_task()], now=dt.datetime.now().timestamp() + 601)
        return self.ledger.load("app-build")

    def test_trace_shows_agent_and_rerouted_from(self):
        from todo_system.orchestrator import trace
        row = self._rerouted()
        md = trace.render("app-build", self.ledger, runs_dir=self.tmp / "runs",
                          repo_dir=self.tmp, pr_lookup=lambda *a, **k: None,
                          branch_lookup=lambda *a, **k: None)
        self.assertIn(f"(agent: `{DEFAULT}`)", md)
        self.assertIn(f"`{REMOTE}`", md)
        self.assertIn("accept-timeout", md)
        self.assertIn(row.rerouted_from["ts"], md)

    def test_api_status_rows_carry_agent_and_rerouted_from(self):
        self._rerouted()
        M = _load_dashboard()
        patches = [mock.patch.object(M, "ASSIGN", self.ledger.root),
                   mock.patch.object(M, "RUNS", self.tmp / "runs"),
                   mock.patch.object(M, "STATE", self.tmp / "_state"),
                   mock.patch.object(M, "TODO_DB", self.tmp / "todo.db")]
        for name, val in (("collect_human", []), ("collect_deps", []),
                          ("collect_agents", []), ("collect_todo", {}),
                          ("collect_usage", None), ("_worker_running", []),
                          ("_todo_status_map", {}), ("_title_map", {}),
                          ("_project_map", {})):
            patches.append(mock.patch.object(M, name, return_value=val))
        with contextlib.ExitStack() as st:
            for p in patches:
                st.enter_context(p)
            d = M.collect_status()
            detail = M._ledger_row("app-build")
        [row] = [r for r in d["recent"] if r["task"] == "app-build"]
        self.assertEqual(row["agent"], DEFAULT)
        self.assertEqual(row["rerouted_from"]["agent"], REMOTE)
        self.assertEqual(row["rerouted_from"]["reason"], "accept-timeout")
        self.assertEqual(detail["agent"], DEFAULT)
        self.assertEqual(detail["rerouted_from"]["agent"], REMOTE)


# --- remote runner side: CANCEL + busy heartbeat ------------------------------

class _RecordingExecutor:
    mode = "fake"

    def __init__(self, during=None):
        self.calls: list[str] = []
        self.pushed: list[str] = []
        self._during = during

    def execute(self, msg, agent, progress, **_):
        self.calls.append(msg.task_id)
        progress("stage implement …")
        if self._during is not None:
            self._during(msg)
        # A staged run checks at every stage boundary before pushing anything.
        if runner_mod._cancel_requested_now():
            return ExecOutcome(False, "cancelled", {"cancelled": True})
        self.pushed.append(msg.task_id)
        return ExecOutcome(True, "ok", {})


class RunnerCancelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mb = FileMailbox(self.tmp / "agents")
        self.agent = Agent(id=REMOTE, kind="ai-remote", capabilities=["heavy"])
        hb = mock.patch("todo_system.worker.runner_heartbeat_start")
        hb.start()
        self.addCleanup(hb.stop)
        hb2 = mock.patch("todo_system.worker.runner_heartbeat_clear")
        hb2.start()
        self.addCleanup(hb2.stop)
        env = mock.patch.dict(os.environ, {"RUNNER_BUSY_HEARTBEAT_S": "3600"})
        env.start()
        self.addCleanup(env.stop)

    def _assign(self) -> m.Envelope:
        msg = m.Envelope(to=REMOTE, type=m.TASK_ASSIGN, task_id="app-build",
                         payload={"task_path": "", "requires": ["heavy"]})
        self.mb.send(msg)
        return msg

    def _cancel(self, msg: m.Envelope) -> None:
        self.mb.send(m.Envelope(to=REMOTE, type=m.CANCEL, task_id=msg.task_id,
                                corr_id=msg.corr_id, payload={"reason": "rerouted"}))

    def _replies(self, kinds=None):
        out = [r for r in self.mb.receive(m.ORCHESTRATOR) if r.type != m.HEARTBEAT]
        return [r.type for r in out]

    def _runner(self, ex) -> Runner:
        return Runner(self.mb, self.agent, ex, log=lambda *_: None,
                      ledger_root=self.tmp / "assignments")

    def test_queued_assignment_with_cancel_never_runs_serial(self):
        msg = self._assign()
        self._cancel(msg)
        ex = _RecordingExecutor()
        self._runner(ex).poll_once()
        self.assertEqual(ex.calls, [])
        self.assertEqual(self._replies(), [])                 # not even ACCEPT
        self.assertEqual(self.mb.receive(REMOTE), [])        # consumed

    def test_queued_assignment_with_cancel_never_runs_parallel(self):
        msg = self._assign()
        self._cancel(msg)
        ex = _RecordingExecutor()
        r = self._runner(ex)
        r.poll_parallel(1)
        for t in [t for t in threading.enumerate() if t.name.startswith("run-")]:
            t.join(5)
        self.assertEqual(ex.calls, [])
        self.assertEqual(self._replies(), [])
        self.assertEqual(self.mb.receive(REMOTE), [])

    def test_cancel_mid_run_stops_without_push_or_reply(self):
        msg = self._assign()
        ex = _RecordingExecutor(during=lambda _m: self._cancel(msg))
        self._runner(ex).poll_once()
        self.assertEqual(ex.calls, ["app-build"])
        self.assertEqual(ex.pushed, [])
        replies = self._replies()
        self.assertNotIn(m.RESULT, replies)
        self.assertNotIn(m.FAIL, replies)

    def test_no_cancel_runs_normally(self):
        self._assign()
        ex = _RecordingExecutor()
        self._runner(ex).poll_once()
        self.assertEqual(ex.pushed, ["app-build"])
        self.assertIn(m.RESULT, self._replies())

    def test_cancel_hook_is_cleared_after_the_run(self):
        self._assign()
        self._runner(_RecordingExecutor()).poll_once()
        self.assertIsNone(getattr(runner_mod._CANCEL, "check", None))
        self.assertFalse(runner_mod._cancel_requested_now())

    def test_skip_reason_refuses_an_envelope_rerouted_away(self):
        msg = self._assign()
        led = Ledger(self.tmp / "assignments")
        led.save(Assignment(task_id="app-build", agent_id=DEFAULT, corr_id="new",
                            rerouted_from={"agent": REMOTE, "reason": "accept-timeout",
                                           "corr_id": msg.corr_id, "ts": "x"}))
        r = self._runner(_RecordingExecutor())
        self.assertIn("rerouted away", r._skip_reason(msg) or "")


class StagedCancelTest(unittest.TestCase):
    """The real StagedExecutor stops at a stage boundary on CANCEL: no checkpoint
    push, no report stage (so no PR / merge), no delegate publish. This is what
    keeps one worker/<slug> from ending up with two PRs or a double merge."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        (self.tmp / "clone").mkdir()
        self.addCleanup(setattr, runner_mod._CANCEL, "check", None)

    def _run(self, pipeline, *, cancel_after: str, repo_url="git@example.invalid:x/y.git",
             worker_mode="auto"):
        ran: list[str] = []
        calls = {"checkpoint": 0, "publish": 0, "merge": 0}
        state = {"cancel": False}
        runner_mod._CANCEL.check = lambda: state["cancel"]

        def fake_stage(argv, **kw):
            stage = pipeline[len(ran)]
            ran.append(stage)
            run_dir = next(self.runs.iterdir())
            art = run_dir / stages.STAGE_ARTIFACT[stage]
            if not art.exists():
                art.write_text("ok\n", encoding="utf-8")
            if stage == cancel_after:
                state["cancel"] = True
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        def count(name, ret):
            def _f(*a, **k):
                calls[name] += 1
                return ret
            return _f

        vp = subprocess.CompletedProcess([], 0, "", "")
        vp.problem = None
        vp.orphaned_commits = None
        payload = {"auto_merge": True, "model_tier": "heavy", "worker_mode": worker_mode}
        if repo_url:
            payload["repo"] = repo_url
        msg = m.Envelope(to=REMOTE, type=m.TASK_ASSIGN, task_id="t", payload=payload)
        with contextlib.ExitStack() as stack:
            for p in (
                mock.patch.object(repos, "ensure_worktree", return_value=self.tmp / "clone"),
                mock.patch.object(stages, "restore_main"),
                mock.patch.object(stages, "install_push_guard"),
                mock.patch.object(stages, "default_branch", return_value="main"),
                mock.patch.object(stages, "prepare_external_branch",
                                  return_value=subprocess.CompletedProcess([], 0, "", "")),
                mock.patch.object(stages, "prepare_vault_branch", return_value=vp),
                mock.patch.object(stages, "remote_branch_exists", return_value=True),
                mock.patch.object(stages, "RUNS_DIR", self.runs),
                mock.patch.object(stages, "build_stage_argv", return_value=["true"]),
                mock.patch.object(stages, "checkpoint_push",
                                  side_effect=count("checkpoint", {"pushed": True})),
                mock.patch.object(stages, "rescue_uncommitted", return_value={}),
                mock.patch.object(stages, "publish_branch",
                                  side_effect=count("publish", {"pr_url": "u"})),
                mock.patch.object(stages, "merge_pr",
                                  side_effect=count("merge", {"merged": True})),
                mock.patch.object(stages, "invalidate_model_cache_if_unavailable"),
                mock.patch.object(stages, "record_stage_usage", return_value=None),
                mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")),
                mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
                mock.patch.object(StagedExecutor, "_acquire_checkout_lease", return_value=None),
                mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
                mock.patch.object(StagedExecutor, "_pipeline_for", return_value=pipeline),
                mock.patch("todo_system.worker.runner_heartbeat_start"),
                mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_stage),
                mock.patch("subprocess.run",
                           side_effect=lambda a, **k: subprocess.CompletedProcess(a, 0, "", "")),
            ):
                stack.enter_context(p)
            out = StagedExecutor().execute(msg, Agent(id=REMOTE, kind="ai-remote"),
                                           progress=lambda _: None)
        return out, ran, calls

    def test_cancel_after_verify_stops_before_report(self):
        pipeline = ("implement", "verify", "report")
        out, ran, calls = self._run(pipeline, cancel_after="verify")
        self.assertEqual(ran, ["implement", "verify"])       # report never ran
        self.assertTrue(out.payload.get("cancelled"))
        self.assertFalse(out.ok)
        # implement's own checkpoint ran before the cancel; nothing after it
        self.assertEqual(calls, {"checkpoint": 1, "publish": 0, "merge": 0})

    def test_cancel_during_implement_skips_its_checkpoint(self):
        pipeline = ("implement", "verify", "report")
        out, ran, calls = self._run(pipeline, cancel_after="implement")
        self.assertEqual(ran, ["implement"])
        self.assertTrue(out.payload.get("cancelled"))
        self.assertEqual(calls["checkpoint"], 0)             # nothing pushed

    def test_uncancelled_implement_still_checkpoints(self):
        pipeline = ("implement",)
        _out, ran, calls = self._run(pipeline, cancel_after="never")
        self.assertEqual(ran, ["implement"])
        self.assertEqual(calls["checkpoint"], 1)

    def test_cancelled_vault_delegate_is_not_published(self):
        _out, ran, calls = self._run(("delegate",), cancel_after="delegate",
                                     repo_url=None, worker_mode="delegate")
        self.assertEqual(ran, ["delegate"])
        self.assertEqual(calls["publish"], 0)
        self.assertEqual(calls["merge"], 0)

    def test_uncancelled_vault_delegate_is_published(self):
        # Control: the same harness does reach the publish block.
        _out, _ran, calls = self._run(("delegate",), cancel_after="never",
                                      repo_url=None, worker_mode="delegate")
        self.assertEqual(calls["publish"], 1)


class BusyHeartbeatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mb = FileMailbox(self.tmp / "agents")
        for target in ("todo_system.worker.runner_heartbeat_start",
                       "todo_system.worker.runner_heartbeat_clear"):
            p = mock.patch(target)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"RUNNER_BUSY_HEARTBEAT_S": "0.05"})
        env.start()
        self.addCleanup(env.stop)

    def _beats(self):
        return [r for r in self.mb.receive(m.ORCHESTRATOR)
                if r.type == m.HEARTBEAT and r.payload.get("status") == "busy"]

    def test_remote_runner_beats_while_busy_and_stops_after(self):
        r = Runner(self.mb, Agent(id=REMOTE, kind="ai-remote", capabilities=["heavy"]),
                   _RecordingExecutor(), log=lambda *_: None)
        r._work_begin()
        time.sleep(0.3)
        r._work_end()
        r._busy_beat_thread.join(2)
        self.assertFalse(r._busy_beat_thread.is_alive())
        n = len(self._beats())
        self.assertGreaterEqual(n, 2)
        self.assertEqual(self._beats()[0].frm, REMOTE)
        time.sleep(0.2)
        self.assertEqual(len(self._beats()), n)              # stopped

    def test_local_runner_sends_no_busy_heartbeat(self):
        r = Runner(self.mb, Agent(id=DEFAULT, kind="ai-local"),
                   _RecordingExecutor(), log=lambda *_: None)
        r._work_begin()
        time.sleep(0.2)
        r._work_end()
        self.assertIsNone(r._busy_beat_thread)
        self.assertEqual(self._beats(), [])

    def test_orchestrator_sees_a_busy_remote_as_online(self):
        reg = Registry([Agent(id=REMOTE, kind="ai-remote", capabilities=["heavy"])])
        orch = Orchestrator(self.mb, reg, Ledger(self.tmp / "assignments"),
                            notify=lambda *a, **k: None)
        r = Runner(self.mb, reg.get(REMOTE), _RecordingExecutor(), log=lambda *_: None)
        r._work_begin()
        r._work_end()
        r._busy_beat_thread.join(2)
        orch.collect()
        self.assertEqual(orch.agent_liveness()[REMOTE]["status"], "online")


if __name__ == "__main__":
    unittest.main()


class NeedsDeviceTableTest(unittest.TestCase):
    def test_table(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        app = tmp / "app-repo"
        (app / "app").mkdir(parents=True)
        (app / "gradlew").write_text("", encoding="utf-8")
        server = tmp / "server-repo"
        (server / "src").mkdir(parents=True)
        (server / "gradlew").write_text("", encoding="utf-8")
        kmp = tmp / "kmp-repo"
        (kmp / "composeApp").mkdir(parents=True)
        (kmp / "gradlew").write_text("", encoding="utf-8")
        nogradle = tmp / "py-repo"
        (nogradle / "app").mkdir(parents=True)
        r = {"repo": REPO}
        cases = [
            # (payload, repo_dir, app_paths, expected)
            ({**r, "heavy_signals": ["emulator"]}, server, ("app/",), True),
            ({**r, "heavy_signals": ["screen_check"]}, None, ("app/",), True),
            ({**r, "emulator": True}, server, ("app/",), True),
            ({"heavy": True}, None, ("app/",), False),            # vault task
            ({"long_job": "x"}, None, ("app/",), False),          # vault long job
            ({**r, "long_job": "x"}, None, ("app/",), True),      # clone unknown
            ({**r}, tmp / "missing", ("app/",), True),            # not cloned yet
            ({**r}, app, ("app/",), True),                        # app module
            ({**r}, server, ("app/",), False),                    # Gradle server
            ({**r}, kmp, ("app/",), False),                       # default paths
            ({**r}, kmp, ("composeApp/",), True),                 # widened by config
            ({**r}, nogradle, ("app/",), False),                  # not an app repo
            # screen_check: false never reaches the payload; even if it did, an
            # app diff would still get the runner's check, so it is no exemption.
            ({**r, "screen_check": False}, app, ("app/",), True),
        ]
        for payload, repo_dir, paths, want in cases:
            with self.subTest(payload=payload, repo_dir=repo_dir, paths=paths):
                self.assertIs(needs_device(payload, repo_dir, paths), want)


class DeviceGateTickTest(_Base):
    """Through Orchestrator.tick: a task that may need a device check only goes to
    a heavy remote that also advertises `device`."""

    DEVICE_REMOTE = "device-remote"

    def _with_device_remote(self) -> None:
        self.reg = Registry([
            Agent(id=DEFAULT, kind="ai-local", projects=["*"], mailbox=self.uri),
            Agent(id=REMOTE, kind="ai-remote", projects=[], capabilities=["heavy"],
                  concurrency=1, mailbox=self.uri),
            Agent(id=self.DEVICE_REMOTE, kind="ai-remote", projects=[],
                  capabilities=["heavy", "device"], concurrency=1, mailbox=self.uri),
        ])
        self.orch = Orchestrator(self.mb, self.reg, self.ledger,
                                 notify=lambda *a, **k: None, default_uri=self.uri)

    def _app_module(self) -> None:
        (self.clone / "app").mkdir()

    def test_app_repo_task_stays_home_without_a_device_remote(self):   # ①
        self._app_module()
        self.seen(REMOTE, 60)
        res = _tick(self.orch, [self.heavy_task()])
        self.assertEqual(len(res["dispatched"]), 1)
        self.assertEqual(self.inbox(REMOTE), [])
        [env] = self.inbox(DEFAULT)
        self.assertEqual(env.type, m.TASK_ASSIGN)
        self.assertEqual(self.ledger.load("app-build").agent_id, DEFAULT)

    def test_app_repo_task_goes_to_a_device_remote(self):              # ②
        self._app_module()
        self._with_device_remote()
        self.seen(REMOTE, 60)
        self.seen(self.DEVICE_REMOTE, 60)
        _tick(self.orch, [self.heavy_task()])
        self.assertEqual(self.inbox(REMOTE), [])
        self.assertEqual(self.inbox(DEFAULT), [])
        [env] = self.inbox(self.DEVICE_REMOTE)
        self.assertEqual(env.type, m.TASK_ASSIGN)
        self.assertEqual(self.ledger.load("app-build").agent_id, self.DEVICE_REMOTE)

    def test_gradle_server_repo_goes_to_a_plain_heavy_remote(self):    # ③
        self.seen(REMOTE, 60)                     # clone has gradlew, no app/
        _tick(self.orch, [self.heavy_task()])
        [env] = self.inbox(REMOTE)
        self.assertEqual(env.type, m.TASK_ASSIGN)
        self.assertEqual(self.inbox(DEFAULT), [])

    def test_screen_check_off_still_counts_as_device(self):            # ④
        # The opt-out never reaches the payload (and the runner would check an app
        # diff anyway), so the app-repo task still stays home.
        self._app_module()
        self.seen(REMOTE, 60)
        _tick(self.orch, [self.heavy_task(screen_check=False)])
        self.assertEqual(self.inbox(REMOTE), [])
        self.assertEqual(len(self.inbox(DEFAULT)), 1)

    def test_emulator_task_without_app_dir_stays_home(self):           # ⑤
        self.seen(REMOTE, 60)
        _tick(self.orch, [self.heavy_task(heavy_signals=["emulator"])])
        self.assertEqual(self.inbox(REMOTE), [])
        self.assertEqual(len(self.inbox(DEFAULT)), 1)

    def test_device_remote_offline_falls_back_home(self):
        self._app_module()
        self._with_device_remote()
        self.seen(REMOTE, 60)                     # only the non-device one online
        _tick(self.orch, [self.heavy_task()])
        self.assertEqual(self.inbox(REMOTE), [])
        self.assertEqual(self.inbox(self.DEVICE_REMOTE), [])
        self.assertEqual(len(self.inbox(DEFAULT)), 1)



class RemoteSkipsLocalCheckoutGatesTest(_Base):
    """This host's per-repo checkout cap and global gradle cap guard ITS clones
    and memory. A task routed to a remote runs in the remote's clone, so through
    Orchestrator.tick it is not deferred by them, and a remote's in-flight row
    does not count toward them."""

    OTHER = "https://example.invalid/acme/other-gradle.git"

    def setUp(self):
        super().setUp()
        self.orch = Orchestrator(self.mb, self.reg, self.ledger,
                                 notify=lambda *a, **k: None, default_uri=self.uri,
                                 max_parallel_gradle_builds=1)
        p = mock.patch.object(self.orch, "_is_gradle_checkout",
                              side_effect=lambda key: key in (REPO, self.OTHER))
        p.start()
        self.addCleanup(p.stop)

    def _live(self, slug: str, agent: str, repo: str) -> None:
        self.ledger.save(Assignment(task_id=slug, agent_id=agent, corr_id=f"c-{slug}",
                                    state=A.IN_PROGRESS, kind="ai-local",
                                    payload={"repo": repo}))

    def test_remote_bound_task_is_not_held_by_the_local_gradle_cap(self):
        self._live("hq-build", DEFAULT, self.OTHER)       # HQ at its gradle cap (1)
        self.seen(REMOTE, 60)
        res = _tick(self.orch, [self.heavy_task()])
        self.assertEqual(len(res["dispatched"]), 1)
        [env] = self.inbox(REMOTE)
        self.assertEqual(env.type, m.TASK_ASSIGN)
        self.assertEqual(self.ledger.load("app-build").agent_id, REMOTE)

    def test_remote_bound_task_is_not_held_by_the_local_checkout_cap(self):
        # Same repo already busy on HQ (cap per repo = 1 via overlap) — the remote
        # has its own clone, so the task still goes out.
        self.orch.max_parallel_per_repo = 1
        self._live("hq-same-repo", DEFAULT, REPO)
        self.seen(REMOTE, 60)
        _tick(self.orch, [self.heavy_task()])
        self.assertEqual(len(self.inbox(REMOTE)), 1)

    def test_remote_inflight_row_does_not_count_toward_local_caps(self):
        # The remote is busy (concurrency 1) with a gradle build; the next heavy
        # task falls back to HQ, and HQ's gradle cap (1) is still free.
        self.seen(REMOTE, 60)
        self._live("remote-build", REMOTE, self.OTHER)
        res = _tick(self.orch, [self.heavy_task()])
        self.assertEqual(len(res["dispatched"]), 1)
        self.assertEqual(self.inbox(REMOTE), [])
        self.assertEqual(len(self.inbox(DEFAULT)), 1)

    def test_local_task_is_still_held_by_the_local_gradle_cap(self):
        self._live("hq-build", DEFAULT, self.OTHER)
        self.seen(REMOTE, 600_000)                         # remote stale → stays home
        res = _tick(self.orch, [self.heavy_task()])
        self.assertEqual(res["dispatched"], [])
        self.assertEqual(self.inbox(DEFAULT), [])

    def test_queue_view_does_not_show_a_remote_bound_task_as_capped(self):
        self._live("hq-build", DEFAULT, self.OTHER)
        self.seen(REMOTE, 60)
        [row] = self.orch.queue_snapshot([self.heavy_task()])
        self.assertNotEqual(row["wait"], "gradle_cap")


class UnpublishableGitMailboxTest(_Base):
    """A git:// remote whose mailbox checkout is off its branch cannot receive a
    TASK_ASSIGN (the commit is refused / lands off main), so heavy routing treats
    it as unavailable and the task stays home — through Orchestrator.tick."""

    def _block_remote_mailbox(self, why: str | None):
        real = self.orch._mailbox_for

        def fake(agent):
            mb = real(agent)
            if agent.id != REMOTE:
                return mb
            stub = mock.Mock(wraps=mb)
            stub.can_send.return_value = why
            return stub
        p = mock.patch.object(self.orch, "_mailbox_for", side_effect=fake)
        p.start()
        self.addCleanup(p.stop)

    def test_unpublishable_remote_is_skipped(self):
        self.seen(REMOTE, 60)
        self._block_remote_mailbox("git mailbox checkout /v is on 'worker/x', not 'main'")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            res = _tick(self.orch, [self.heavy_task()])
        self.assertEqual(len(res["dispatched"]), 1)
        self.assertEqual(self.inbox(REMOTE), [])
        self.assertEqual(len(self.inbox(DEFAULT)), 1)
        self.assertIn("skipping desktop-runner", out.getvalue())

    def test_publishable_remote_still_gets_it(self):
        self.seen(REMOTE, 60)
        self._block_remote_mailbox(None)
        _tick(self.orch, [self.heavy_task()])
        self.assertEqual(len(self.inbox(REMOTE)), 1)


class GitMailboxCanSendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        g = ["git", "-C", str(self.tmp)]
        subprocess.run(g[:1] + ["init", "-q", "-b", "main", str(self.tmp)], check=True)
        (self.tmp / "Worker" / "agents").mkdir(parents=True)
        (self.tmp / "Worker" / "agents" / ".keep").write_text("", encoding="utf-8")
        subprocess.run(g + ["add", "-A"], check=True)
        subprocess.run(g + ["-c", "user.name=t", "-c", "user.email=t@example.invalid",
                            "commit", "-q", "-m", "init"], check=True)
        self.g = g

    def test_on_main_can_send_off_main_cannot(self):
        from todo_system.orchestrator.mailbox import GitMailbox
        mb = GitMailbox(self.tmp / "Worker" / "agents")
        self.assertIsNone(mb.can_send())
        subprocess.run(self.g + ["checkout", "-q", "-b", "worker/x"], check=True)
        mb = GitMailbox(self.tmp / "Worker" / "agents")    # fresh: no cached answer
        why = mb.can_send()
        self.assertIn("worker/x", why)
        self.assertIn("'main'", why)

    def test_file_mailbox_always_can_send(self):
        self.assertIsNone(FileMailbox(self.tmp / "fm").can_send())
