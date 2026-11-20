"""Noise-tier routing of the orchestrator's alerts (macp-kakao-alert-2-noise-tiers).

RED controls (AC 5): the immediate tier is pinned by asserting the injected
notifier actually RECEIVES the alert kinds a human must act on — 검토요청
(_notify_review), 격리 (_notify_poison), 커밋유실 (_notify_orphaned_commits).
Rerouting any of them to the digest (flip its `_notify_routed(...)` call to
`level=kakao_format.LEVEL_DIGEST`) makes its test here fail: the notifier is
only ever handed send-verdict deliveries. The flip→red→revert transcript for
one of them is recorded in the run log of the task that added this file.

The router is the REAL kakao_format.route pinned to tmp state files (clock left
real — each test performs at most one send per key, except the merge test which
injects its own timeline via cooldown maths on distinct calls).
"""

from __future__ import annotations

import functools
import shutil
import tempfile
import unittest
from pathlib import Path

from todo_system import kakao_format
from todo_system.kakao_format import read_digest_entries
from todo_system.orchestrator import message as m
from todo_system.orchestrator.assignment import Assignment, Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import (
    REVIEW_RENOTIFY_COOLDOWN_S,
    Orchestrator,
)
from todo_system.orchestrator.registry import Registry


class RoutedAlertTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([])
        self.sent: list[tuple[str, str | None]] = []
        self.digest = self.tmp / "kakao_digest.jsonl"
        self.now = 1_000_000.0
        route = functools.partial(
            kakao_format.route,
            digest_path=self.digest,
            dedup_path=self.tmp / "kakao_dedup.json",
            log_path=self.tmp / "kakao_route.log",
            now=self.now,
        )
        self.orch = Orchestrator(
            self.mb, self.reg, self.ledger,
            notify=lambda text, link=None: self.sent.append((text, link)),
            alert_route=route,
        )
        self.orch._task_title = lambda slug: "노이즈 등급 태스크"

    def _assign(self, **kw) -> Assignment:
        base = dict(task_id="macp-kakao-alert-2-noise-tiers",
                    agent_id="worker-default", corr_id="c1")
        base.update(kw)
        return Assignment(**base)

    def _queued(self):
        if not self.digest.exists():
            return []
        return read_digest_entries(self.digest.read_text(encoding="utf-8"))

    # -- RED controls: immediate kinds MUST reach the notifier ---------------

    def test_red_control_review_request_reaches_the_notifier(self):
        a = self._assign(blocked_on="human must arm the real-account unit")
        msg = m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, task_id=a.task_id, payload={})
        self.orch._notify_review(a, msg)
        self.assertEqual(len(self.sent), 1, "검토요청 must ring the phone, not the digest")
        self.assertIn("판단", self.sent[0][0].splitlines()[0])
        self.assertEqual(self._queued(), [])

    def test_red_control_poison_quarantine_reaches_the_notifier(self):
        a = self._assign(refail_count=3, last_note="same gate, three times")
        self.orch._notify_poison(a)
        self.assertEqual(len(self.sent), 1, "격리 must ring the phone, not the digest")
        self.assertIn("수동", self.sent[0][0].splitlines()[0])
        self.assertEqual(self._queued(), [])

    def test_red_control_orphaned_commits_reach_the_notifier(self):
        a = self._assign()
        msg = m.Envelope(to=m.ORCHESTRATOR, type=m.PROGRESS, task_id=a.task_id,
                         payload={"orphaned_commits": {"count": 2, "tip": "abcdef1234567890",
                                                       "shas": ["a" * 16, "b" * 16]}})
        self.orch._notify_orphaned_commits(a, msg)
        self.assertEqual(len(self.sent), 1, "커밋유실 must ring the phone, not the digest")
        self.assertIn("되감겼", self.sent[0][0].splitlines()[0])
        self.assertEqual(self._queued(), [])

    def test_timeout_park_stays_immediate_too(self):
        a = self._assign(timeout_streak=2)
        self.orch._notify_timeout_park(a)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self._queued(), [])

    # -- the merge window on repeated review pings ---------------------------

    def test_same_review_gate_re_ping_merges_within_the_cooldown(self):
        """The 2026-08-18 failure mode: the same task re-parked on the same gate
        pages on every re-dispatch. Inside the 6h cooldown the repeat is merged;
        the next delivery says how many were folded in."""
        a = self._assign(blocked_on="same blocker")
        msg = m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, task_id=a.task_id, payload={})

        def at(now):
            return functools.partial(
                kakao_format.route, digest_path=self.digest,
                dedup_path=self.tmp / "kakao_dedup.json",
                log_path=self.tmp / "kakao_route.log", now=now)

        self.orch.alert_route = at(self.now)
        self.orch._notify_review(a, msg)
        self.orch.alert_route = at(self.now + 3600)               # 1h later, same gate
        self.orch._notify_review(a, msg)
        self.assertEqual(len(self.sent), 1, "repeat inside the cooldown must merge")

        self.orch.alert_route = at(self.now + REVIEW_RENOTIFY_COOLDOWN_S + 1)
        self.orch._notify_review(a, msg)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("외 1건", self.sent[-1][0])            # the fold-in is visible

    def test_a_framing_change_alerts_immediately_despite_the_cooldown(self):
        """blocked → PR-open is a real state change: different event key, so the
        cooldown must NOT swallow it."""
        a = self._assign(blocked_on="blocker")
        self.orch._notify_review(
            a, m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, task_id=a.task_id, payload={}))
        a.blocked_on = ""
        self.orch._notify_review(
            a, m.Envelope(to=m.ORCHESTRATOR, type=m.RESULT, task_id=a.task_id,
                          payload={"pr_url": "https://github.com/x/y/pull/1"}))
        self.assertEqual(len(self.sent), 2)

    # -- digest tier ----------------------------------------------------------

    def test_propagate_downstream_alert_is_digest_not_a_send(self):
        self.orch._notify_routed(
            self.orch._compose_propagate_alert("platform-x", ["check-a", "check-b"]),
            None, level=kakao_format.LEVEL_DIGEST, event_key="propagate:platform-x")
        self.assertEqual(self.sent, [])                     # zero individual sends
        [entry] = self._queued()
        self.assertIn("platform-x", entry["msg"])           # ...and included downstream
        self.assertIn("platform-x", kakao_format.compose_digest(self._queued()))

    # -- fail-open -------------------------------------------------------------

    def test_router_failure_falls_open_to_a_send(self):
        def broken(*_a, **_k):
            raise RuntimeError("router down")
        self.orch.alert_route = broken
        self.orch._notify_poison(self._assign(refail_count=3))
        self.assertEqual(len(self.sent), 1, "a routing bug must not lose an alert")


if __name__ == "__main__":
    unittest.main()
