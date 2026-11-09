"""Tests for the human agent adapter (P1). No real KakaoTalk is sent — the
notifier AND the noise-tier router are injected."""

from __future__ import annotations

import functools
import shutil
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from todo_system.kakao_format import read_digest_entries, route as kakao_route
from todo_system.orchestrator import human
from todo_system.orchestrator import message as m
from todo_system.orchestrator.assignment import Assignment, Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.registry import Agent


def _human(**kw) -> Agent:
    return Agent(id="alice", kind="human", display_name="홍길동",
                 projects=["withdrawal-*"], notify=["kakao"], **kw)


class ComposeTest(unittest.TestCase):
    def test_summary_and_limit(self):
        agent = _human()
        msg = m.Envelope(to="alice", type=m.TASK_ASSIGN, task_id="withdrawal-a2",
                         payload={"summary": "A2 최종결정 체크리스트 확인"})
        text = human.compose_kakao(msg, agent)
        # New template: an emoji headline naming the action, the task id on line 2,
        # then the summary as a word-clipped tail. The old "[할당] <slug>" prefix is
        # exactly what this task replaced (macp-kakao-alert-1-readable); identity
        # and content survive, length is bounded by KAKAO_TEXT_LIMIT (not 179).
        self.assertIn("할당", text)                       # action framing in headline
        self.assertIn("withdrawal-a2", text)             # task identity survives
        self.assertIn("A2 최종결정", text)                 # summary content survives
        self.assertLessEqual(len(text), human.KAKAO_TEXT_LIMIT)

    def test_falls_back_to_next_action(self):
        agent = _human()
        msg = m.Envelope(to="alice", type=m.TASK_ASSIGN, task_id="t1",
                         payload={"next_action": "업스테이지 2건 지원"})
        self.assertIn("업스테이지", human.compose_kakao(msg, agent))


class DeliverReplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mb = FileMailbox(self.tmp / "agents")
        self.agent = _human()
        self.sent: list[tuple[str, str | None]] = []
        # Real router pinned to tmp state files — deterministic, nothing shared.
        self.digest = self.tmp / "kakao_digest.jsonl"
        self.route = functools.partial(
            kakao_route, digest_path=self.digest,
            dedup_path=self.tmp / "kakao_dedup.json",
            log_path=self.tmp / "kakao_route.log")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _notify(self, text, link=None):
        self.sent.append((text, link))

    def _deliver(self, **kw):
        return human.deliver_pending(self.mb, self.agent, notify=self._notify,
                                     route=self.route, **kw)

    def _queued(self):
        if not self.digest.exists():
            return []
        return read_digest_entries(self.digest.read_text(encoding="utf-8"))

    def test_deliver_rings_the_assign_immediately_then_acks(self):
        """Reversal of the 2026-09-16 digest call (macp-human-action-ready-alerts,
        2026-11-09): that audit's premise was that every [할당] trails another
        alert about the same task. For a task assigned to a person because its
        dependency chain just closed, the TASK_ASSIGN is the ONLY signal that it
        is their turn — a digest would park it for up to a day."""
        assign = m.Envelope(to="alice", type=m.TASK_ASSIGN, task_id="withdrawal-a2",
                            payload={"summary": "A2 결정 점검"})
        self.mb.send(assign)

        delivered = self._deliver()
        self.assertEqual(len(delivered), 1)
        self.assertEqual(len(self.sent), 1)                 # rings the phone
        self.assertIn("withdrawal-a2", self.sent[0][0])
        self.assertEqual(self._queued(), [])                # nothing parked
        # acked -> not redelivered
        self.assertEqual(self._deliver(), [])
        self.assertEqual(len(self.sent), 1)

    def test_a_redelivery_of_the_same_assignment_merges_in_the_dedup_window(self):
        """What keeps the reversal from reintroducing noise: `event_key` is still
        `assign:<task>`, so a burst about ONE assignment is one buzz — while a
        different task rings on its own."""
        for _ in range(2):
            self.mb.send(m.Envelope(to="alice", type=m.TASK_ASSIGN,
                                    task_id="withdrawal-a2",
                                    payload={"summary": "A2 결정 점검"}))
            self._deliver()
        self.assertEqual(len(self.sent), 1)                 # second merged away
        self.mb.send(m.Envelope(to="alice", type=m.TASK_ASSIGN, task_id="other-task",
                                payload={"summary": "다른 건"}))
        self._deliver()
        self.assertEqual(len(self.sent), 2)                 # a different key rings

    def test_the_assign_delivery_is_immediate_tier(self):
        """Pinned on the call itself, not just its effect: a future refactor must
        not silently restore the digest level."""
        self.mb.send(m.Envelope(to="alice", type=m.TASK_ASSIGN, task_id="t1"))
        seen = []

        def spy(text, *, level=None, event_key=None, **kw):
            seen.append((level, event_key))
            return "send", text
        human.deliver_pending(self.mb, self.agent, notify=self._notify, route=spy)
        self.assertEqual(seen, [("immediate", "assign:t1")])

    def test_deliver_query_still_rings_immediately(self):
        """QUERY was already immediate and stays so (when in doubt, immediate)."""
        self.mb.send(m.Envelope(to="alice", type=m.QUERY, task_id="t1",
                                payload={"summary": "예산 확인 필요"}))
        self._deliver()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self._queued(), [])

    def test_deliver_routing_failure_falls_open_to_an_immediate_send(self):
        """A routing bug must never swallow a delivery."""
        def broken_route(*_a, **_k):
            raise OSError("disk full")
        self.mb.send(m.Envelope(to="alice", type=m.TASK_ASSIGN, task_id="t1"))
        human.deliver_pending(self.mb, self.agent, notify=self._notify,
                              route=broken_route)
        self.assertEqual(len(self.sent), 1)

    def test_dry_run_does_not_ack(self):
        self.mb.send(m.Envelope(to="alice", type=m.TASK_ASSIGN, task_id="t1"))
        self._deliver(dry_run=True)
        self.assertEqual(self.sent, [])                     # nothing sent
        self.assertEqual(self._queued(), [])                # nothing queued either
        # a real run afterwards still delivers it
        self.assertEqual(len(self._deliver()), 1)
        self.assertEqual(len(self.sent), 1)

    def test_reply_roundtrip_to_orchestrator(self):
        assign = m.Envelope(to="alice", type=m.TASK_ASSIGN, task_id="withdrawal-a2")
        self.mb.send(assign)
        [inbox] = self.mb.receive("alice")

        human.reply(self.mb, "alice", inbox.task_id, "done",
                    note="A2로 확정, 자퇴 진행", corr_id=inbox.corr_id)
        [back] = self.mb.receive(m.ORCHESTRATOR)
        self.assertEqual(back.type, m.RESULT)
        self.assertEqual(back.frm, "alice")
        self.assertEqual(back.task_id, "withdrawal-a2")
        self.assertEqual(back.corr_id, assign.corr_id)
        self.assertIn("A2로 확정", back.payload["note"])

    def test_reply_needs_input(self):
        env = human.reply(self.mb, "alice", "t1", "needs-input", note="예산 얼마?")
        self.assertEqual(env.type, m.NEEDS_INPUT)


class LedgerCorrDefaultTest(unittest.TestCase):
    """A reply with a fresh corr_id is DROPPED by collect() (corr_id mismatch) and
    the sender is told nothing, so the CLI must default to the row's own corr_id."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # _ledger_corr reads the Worker/assignments ledger from _VAULT_DATA_ROOT
        # (the vault-data root; formerly the single REPO_ROOT). Patch that.
        self._real_root = human._VAULT_DATA_ROOT
        human._VAULT_DATA_ROOT = self.tmp
        self.addCleanup(setattr, human, "_VAULT_DATA_ROOT", self._real_root)
        Ledger(self.tmp / "Worker" / "assignments").save(
            Assignment(task_id="parked-task", agent_id="worker-default",
                       corr_id="CORR-FROM-LEDGER", state="awaiting_human"))
        # _main loads the agent registry from a cwd-relative Worker/agents/registry.yaml
        # by default; an engine-only checkout (or any non-vault cwd) has none, so seed a
        # minimal one and pass it via --registry so the reply path is hermetic and runs
        # in the engine export tree too (public-split-10 §11.7 ③). reply itself does not
        # consult the registry, so one agent row is enough to satisfy load_registry.
        self.registry = self.tmp / "registry.yaml"
        self.registry.write_text(
            "agents:\n  - id: worker-default\n    kind: ai-local\n    host: test-host\n",
            encoding="utf-8")

    def test_defaults_to_the_rows_corr_id(self):
        self.assertEqual(human._ledger_corr("parked-task"), "CORR-FROM-LEDGER")

    def test_unknown_task_yields_none(self):
        self.assertIsNone(human._ledger_corr("no-such-task"))

    def test_cli_reply_without_corr_uses_the_ledger(self):
        mb = FileMailbox(self.tmp / "agents")
        with unittest.mock.patch.object(human, "open_mailbox", return_value=mb):
            rc = human._main(["--registry", str(self.registry),
                              "reply", "--agent", "worker-default",
                              "--task", "parked-task", "--status", "reject"])
        self.assertEqual(rc, 0)
        [back] = mb.receive(m.ORCHESTRATOR)
        self.assertEqual(back.corr_id, "CORR-FROM-LEDGER")

    def test_explicit_corr_still_wins(self):
        mb = FileMailbox(self.tmp / "agents")
        with unittest.mock.patch.object(human, "open_mailbox", return_value=mb):
            human._main(["--registry", str(self.registry),
                         "reply", "--agent", "worker-default", "--task", "parked-task",
                         "--status", "reject", "--corr", "EXPLICIT"])
        [back] = mb.receive(m.ORCHESTRATOR)
        self.assertEqual(back.corr_id, "EXPLICIT")


if __name__ == "__main__":
    unittest.main()
