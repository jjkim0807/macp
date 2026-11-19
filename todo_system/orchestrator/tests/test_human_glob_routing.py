"""A dispatchable worker_mode beats a human GLOB match.

A person's registry glob (`foo-*`) used to capture every task under it, so a
worker prep task (`worker_mode: delegate`) under a human-owned prefix was parked
as a kind-human AWAITING_HUMAN row ("검토대기") instead of running. Now, with a
payload, such a task goes to the most specific NON-human agent — unless the human
lists the slug exactly, the task is a decision task (`decision` tag /
`needs_decision: true`), or its mode is not one the queue dispatches.

Unit table through `Orchestrator.route` / `Registry.for_project`, loader wiring
through the real `tasks.load_dispatchable`, and the end-to-end path through the
real `Orchestrator.tick()`.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import message as m
from todo_system.orchestrator import repos, tasks
from todo_system.orchestrator.assignment import Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import (
    REMOTE_ACCEPT_TIMEOUT_ENV,
    REMOTE_ONLINE_ENV,
    Orchestrator,
    _worker_mode_beats_human_glob,
)
from todo_system.orchestrator.registry import Agent, Registry
from todo_system.orchestrator.tests.test_heavy_routing import _tick
from todo_system.tests.test_orchestrator_tasks import _make_db, _row

HUMAN = "human"
DEFAULT = "worker-default"
REMOTE = "desktop-runner"
SLUG = "foo-1"


def _payload(mode="delegate", tags=("todo",), **extra) -> dict:
    return {"summary": SLUG, "worker_mode": mode,
            "tags": tags if isinstance(tags, str) else list(tags),
            "branch": f"worker/{SLUG}", **extra}


class _Base(unittest.TestCase):
    human_projects = ("foo-*",)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.uri = f"file://{self.tmp / 'agents'}"
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.notes: list = []
        self.orch = self._orch(self.human_projects)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in (REMOTE_ONLINE_ENV, REMOTE_ACCEPT_TIMEOUT_ENV):
            os.environ.pop(k, None)

    def _reg(self, human_projects, *, with_default=True) -> Registry:
        agents = [Agent(id=HUMAN, kind="human", projects=list(human_projects),
                        mailbox=self.uri)]
        if with_default:
            agents.append(Agent(id=DEFAULT, kind="ai-local", projects=["*"],
                                mailbox=self.uri))
        return Registry(agents)

    def _orch(self, human_projects, *, with_default=True) -> Orchestrator:
        return Orchestrator(self.mb, self._reg(human_projects, with_default=with_default),
                            self.ledger,
                            notify=lambda *a, **k: self.notes.append((a, k)),
                            default_uri=self.uri)


class PredicateTableTest(unittest.TestCase):
    def test_table(self):
        cases = [
            (_payload(), True),
            (_payload("auto"), True),
            (_payload("quick"), True),
            (_payload("discover"), True),
            (_payload(" Delegate "), True),
            (_payload(tags="todo,work"), True),                 # comma string
            (_payload(needs_decision=False), True),
            (_payload(needs_decision="no"), True),
            (_payload(tags=("todo", "decision")), False),
            (_payload(tags=("todo", " Decision ")), False),
            (_payload(tags="todo, DECISION"), False),
            (_payload(needs_decision=True), False),
            (_payload(needs_decision="yes"), False),
            (_payload(needs_decision="TRUE"), False),
            (_payload("manual"), False),
            (_payload("blocked"), False),
            (_payload("review"), False),
            ({"summary": SLUG}, False),                          # no worker_mode
            ({"worker_mode": 123}, False),
            ({"worker_mode": "delegate", "tags": None}, True),
            ({"worker_mode": "delegate", "tags": 5}, False),     # odd → fail closed
            (None, False),
            ("delegate", False),
        ]
        for payload, want in cases:
            with self.subTest(payload=payload):
                self.assertIs(_worker_mode_beats_human_glob(payload), want)


class RouteTableTest(_Base):
    """AC1: the unit table through Orchestrator.route."""

    def test_glob_human_dispatchable_task_goes_to_ai(self):
        self.assertEqual(self.orch.route(SLUG, _payload()).id, DEFAULT)

    def test_decision_tag_stays_with_human(self):
        self.assertEqual(
            self.orch.route(SLUG, _payload(tags=("todo", " Decision "))).id, HUMAN)

    def test_needs_decision_stays_with_human(self):
        for v in (True, "yes", "true"):
            with self.subTest(needs_decision=v):
                self.assertEqual(
                    self.orch.route(SLUG, _payload(needs_decision=v)).id, HUMAN)

    def test_exact_human_slug_stays_with_human(self):
        orch = self._orch(("foo-1",))
        self.assertEqual(orch.route(SLUG, _payload()).id, HUMAN)
        orch = self._orch(("foo-*", "foo-1"))
        self.assertEqual(orch.route(SLUG, _payload()).id, HUMAN)

    def test_non_dispatchable_mode_is_unaffected(self):
        for mode in ("manual", "blocked", "review", None):
            with self.subTest(mode=mode):
                p = _payload()
                p["worker_mode"] = mode
                self.assertEqual(self.orch.route(SLUG, p).id, HUMAN)

    def test_slug_only_route_keeps_the_plain_owner(self):
        self.assertEqual(self.orch.route(SLUG).id, HUMAN)

    def test_no_non_human_match_keeps_the_human(self):
        orch = self._orch(("foo-*",), with_default=False)
        self.assertEqual(orch.route(SLUG, _payload()).id, HUMAN)

    def test_malformed_payload_keeps_the_human(self):
        for p in ({"worker_mode": 123, "tags": None}, {"worker_mode": "delegate", "tags": 7}):
            with self.subTest(payload=p):
                self.assertEqual(self.orch.route(SLUG, p).id, HUMAN)

    def test_unrelated_slug_is_unchanged(self):
        self.assertEqual(self.orch.route("bar-1", _payload()).id, DEFAULT)
        self.assertEqual(self.orch.route("bar-1").id, DEFAULT)

    def test_more_specific_ai_glob_wins_over_catch_all(self):
        reg = Registry([
            Agent(id=HUMAN, kind="human", projects=["foo-*"], mailbox=self.uri),
            Agent(id=DEFAULT, kind="ai-local", projects=["*"], mailbox=self.uri),
            Agent(id="foo-bot", kind="ai-local", projects=["foo-1*"], mailbox=self.uri),
        ])
        # foo-1* is more specific than the human's foo-* → it already won; and a
        # less specific AI glob vs. a more specific human one also lands on AI.
        orch = Orchestrator(self.mb, reg, self.ledger, notify=lambda *a, **k: None,
                            default_uri=self.uri)
        self.assertEqual(orch.route(SLUG, _payload()).id, "foo-bot")
        reg2 = Registry([
            Agent(id=HUMAN, kind="human", projects=["foo-1*"], mailbox=self.uri),
            Agent(id=DEFAULT, kind="ai-local", projects=["*"], mailbox=self.uri),
            Agent(id="foo-bot", kind="ai-local", projects=["foo-*"], mailbox=self.uri),
        ])
        orch2 = Orchestrator(self.mb, reg2, self.ledger, notify=lambda *a, **k: None,
                             default_uri=self.uri)
        self.assertEqual(orch2.route(SLUG, _payload()).id, "foo-bot")
        self.assertEqual(orch2.route(SLUG).id, HUMAN)


class RegistrySkipHumanGlobsTest(_Base):
    def test_for_project_flag(self):
        reg = self._reg(("foo-*",))
        self.assertEqual(reg.for_project(SLUG).id, HUMAN)                 # default
        self.assertEqual(reg.for_project(SLUG, skip_human_globs=True).id, DEFAULT)
        exact = self._reg(("foo-1",))
        self.assertEqual(exact.for_project(SLUG, skip_human_globs=True).id, HUMAN)
        alone = self._reg(("foo-*",), with_default=False)
        self.assertIsNone(alone.for_project(SLUG, skip_human_globs=True))
        self.assertEqual(alone.for_project(SLUG).id, HUMAN)

    def test_matches_exactly(self):
        a = Agent(id=HUMAN, kind="human", projects=["foo-*", "bar-2"])
        self.assertFalse(a.matches_exactly(SLUG))
        self.assertTrue(a.matches_exactly("bar-2"))
        self.assertTrue(a.handles(SLUG))


class LoaderNeedsDecisionTest(unittest.TestCase):
    """`needs_decision` reaches the payload through the REAL loader only when true."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _md(self, name: str, fm: str) -> str:
        p = self.tmp / f"{name}.md"
        p.write_text(f"---\nstatus: open\nworker_mode: delegate\n{fm}---\n# {name}\n",
                     encoding="utf-8")
        return str(p)

    def test_loader_carries_needs_decision_only_when_true(self):
        rows = [
            _row("yes", self._md("yes", "needs_decision: true\n"), "delegate"),
            _row("str", self._md("str", 'needs_decision: "yes"\n'), "delegate"),
            _row("no", self._md("no", "needs_decision: false\n"), "delegate"),
            _row("none", self._md("none", ""), "delegate"),
        ]
        got = {t.slug: t.payload
               for t in tasks.load_dispatchable(_make_db(self.tmp, rows))}
        self.assertIs(got["yes"]["needs_decision"], True)
        self.assertIs(got["str"]["needs_decision"], True)
        self.assertNotIn("needs_decision", got["no"])
        self.assertNotIn("needs_decision", got["none"])
        self.assertFalse(_worker_mode_beats_human_glob(got["yes"]))
        self.assertTrue(_worker_mode_beats_human_glob(got["none"]))


class TickPathTest(_Base):
    """AC2: through the real Orchestrator.tick() with tasks built by the real loader."""

    def _load(self, *, mode="delegate", tags="todo,work", fm_extra="") -> list:
        p = self.tmp / f"{SLUG}.md"
        p.write_text(f"---\nstatus: open\nworker_mode: {mode}\n{fm_extra}---\n# {SLUG}\n",
                     encoding="utf-8")
        db = _make_db(self.tmp, [_row(SLUG, str(p), mode, tags=tags)])
        return tasks.load_dispatchable(db)

    def test_delegate_task_goes_to_ai_inbox_not_human(self):
        loaded = self._load()
        self.assertEqual([t.slug for t in loaded], [SLUG])
        res = _tick(self.orch, loaded)
        self.assertEqual([a.task_id for a in res["dispatched"]], [SLUG])
        [env] = self.mb.receive(DEFAULT)
        self.assertEqual((env.type, env.task_id), (m.TASK_ASSIGN, SLUG))
        self.assertEqual(self.mb.receive(HUMAN), [])
        row = self.ledger.load(SLUG)
        self.assertEqual((row.agent_id, row.kind), (DEFAULT, "ai-local"))
        self.assertNotEqual(row.state, A.AWAITING_HUMAN)
        self.assertEqual(row.fallback_agent, "")
        self.assertEqual(self.notes, [])                  # no human nudge

    def _assert_human(self):
        row = self.ledger.load(SLUG)
        self.assertEqual((row.agent_id, row.kind, row.state),
                         (HUMAN, "human", A.AWAITING_HUMAN))
        self.assertEqual(self.mb.receive(DEFAULT), [])

    def test_decision_tag_still_parks_with_human(self):
        _tick(self.orch, self._load(tags="todo,decision"))
        self._assert_human()

    def test_needs_decision_md_still_parks_with_human(self):
        _tick(self.orch, self._load(fm_extra="needs_decision: true\n"))
        self._assert_human()

    def test_exact_human_slug_still_parks_with_human(self):
        orch = self._orch(("foo-1",))
        _tick(orch, self._load())
        self._assert_human()

    def test_manual_task_is_not_loaded_or_dispatched(self):
        loaded = self._load(mode="manual")
        self.assertEqual(loaded, [])
        res = _tick(self.orch, loaded)
        self.assertEqual(res["dispatched"], [])
        self.assertIsNone(self.ledger.load(SLUG))
        self.assertEqual(self.mb.receive(DEFAULT), [])
        self.assertEqual(self.mb.receive(HUMAN), [])

    def test_heavy_task_under_human_glob_takes_the_heavy_branch(self):
        # The AI base then runs the unchanged heavy router: an online heavy remote
        # gets it, with the AI-local agent as fallback.
        reg = Registry([
            Agent(id=HUMAN, kind="human", projects=["foo-*"], mailbox=self.uri),
            Agent(id=DEFAULT, kind="ai-local", projects=["*"], mailbox=self.uri),
            Agent(id=REMOTE, kind="ai-remote", projects=[], capabilities=["heavy"],
                  concurrency=1, mailbox=self.uri),
        ])
        orch = Orchestrator(self.mb, reg, self.ledger, notify=lambda *a, **k: None,
                            default_uri=self.uri)
        clone = self.tmp / "clone"
        clone.mkdir()
        (clone / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
        p = mock.patch.object(repos, "local_path", return_value=clone)
        p.start()
        self.addCleanup(p.stop)
        seen = self.tmp / "_state" / "agent_seen.json"
        seen.parent.mkdir(parents=True, exist_ok=True)
        seen.write_text('{"%s": "%s"}' % (REMOTE, m.now_iso()), encoding="utf-8")
        from todo_system.orchestrator.orchestrator import Task
        task = Task(SLUG, {**_payload(), "repo": "https://example.invalid/acme/foo.git"})
        _tick(orch, [task])
        row = self.ledger.load(SLUG)
        self.assertEqual((row.agent_id, row.fallback_agent), (REMOTE, DEFAULT))
        self.assertEqual(self.mb.receive(HUMAN), [])


if __name__ == "__main__":
    unittest.main()
