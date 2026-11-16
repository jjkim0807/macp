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


if __name__ == "__main__":
    unittest.main()
