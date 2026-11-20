"""Worst-input render tests for every orchestrator KakaoTalk alert kind.

The notifier is injected (captures ``(text, link)``), so no real Kakao send ever
happens ([[tests-reach-real-alert-sender]]). Each alert kind is composed from a
worst-case input (a long slug, hundreds of chars of English blocked_on/last_note,
many shas, a long PR URL) and checked for the three invariants the task fixes:

  * ``len(text) <= KAKAO_TEXT_LIMIT`` — self-limiting, no reliance on the blind
    kakao_send.sh cut;
  * the human's action lives in the HEADLINE (line 1), so it can never be cut off;
  * a URL / sha is never bisected — the PR URL rides in the ``link`` argument
    (the "열기" button), shas ride as whole lines.

``test_review_blocked_1942_is_the_red_control`` is the AC RED control: it uses the
verbatim 2026-09-16 19:42 [검토요청] macp-dashboard-public-access blocker and fails
on the pre-fix orchestrator (the composed text is ~300 chars).
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from todo_system.orchestrator import human
from todo_system.orchestrator import message as m
from todo_system.orchestrator.assignment import Assignment, Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import Orchestrator
from todo_system.orchestrator.registry import Registry

# The 2026-09-16 19:42 [검토요청] blocker, verbatim (the render that lost its tail).
BLOCKER_1942 = (
    "AC4/AC5 need a human on acme-hq: sudo-enable Tailscale Funnel (operator/sudo "
    "required — a forbidden hard rule, and tailscale is not in the verify whitelist), "
    "then from the phone with Tailscale OFF confirm the dashboard 401s without a token "
    "and 200s with one."
)
LONG_PR_URL = "https://github.com/example-org/MaestroACP/pull/123456"
LIMIT = human.KAKAO_TEXT_LIMIT


class KakaoAlertRenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.mb = FileMailbox(self.tmp / "agents")
        self.ledger = Ledger(self.tmp / "assignments")
        self.reg = Registry([])
        self.sent: list[tuple[str, str | None]] = []
        self.orch = Orchestrator(
            self.mb, self.reg, self.ledger,
            notify=lambda text, link=None: self.sent.append((text, link)),
        )
        # Never read the real vault for the title line — pin a deterministic one.
        self.orch._task_title = lambda slug: "대시보드 공개 접근"

    def _assign(self, **kw) -> Assignment:
        base = dict(task_id="macp-dashboard-public-access",
                    agent_id="worker-default", corr_id="c1")
        base.update(kw)
        return Assignment(**base)

    def _last(self) -> tuple[str, str | None]:
        self.assertTrue(self.sent, "notify was never called")
        return self.sent[-1]

    # -- _notify_review (4 branches) ---------------------------------------

    def test_review_blocked_1942_is_the_red_control(self):
        a = self._assign(blocked_on=BLOCKER_1942, last_note=BLOCKER_1942)
        msg = m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, task_id=a.task_id, payload={})
        self.orch._notify_review(a, msg)
        text, _ = self._last()
        self.assertLessEqual(len(text), LIMIT)          # <-- FAILS on pre-fix code
        self.assertIn("판단", text.splitlines()[0])       # the ask is in the headline
        self.assertIn("대시보드 공개 접근", text)            # task is identifiable
        self.assertTrue(text.splitlines()[0].startswith("⛔"))

    def test_review_pr_open_carries_url_in_link_not_text(self):
        a = self._assign()
        msg = m.Envelope(to=m.ORCHESTRATOR, type=m.RESULT, task_id=a.task_id,
                         payload={"pr_url": LONG_PR_URL})
        self.orch._notify_review(a, msg)
        text, link = self._last()
        self.assertLessEqual(len(text), LIMIT)
        self.assertEqual(link, LONG_PR_URL)             # goes to the button, not the body
        self.assertIn("머지", text.splitlines()[0])

    def test_review_result_no_pr_asks_to_close(self):
        a = self._assign()
        msg = m.Envelope(to=m.ORCHESTRATOR, type=m.RESULT, task_id=a.task_id,
                         payload={"run_dir": "20260916T222704-x", "stages": ["report"]})
        self.orch._notify_review(a, msg)
        text, _ = self._last()
        self.assertLessEqual(len(text), LIMIT)
        self.assertIn("닫", text.splitlines()[0])

    def test_review_failure_suspected_asks_to_check(self):
        a = self._assign()
        msg = m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, task_id=a.task_id, payload={})
        self.orch._notify_review(a, msg)
        text, _ = self._last()
        self.assertLessEqual(len(text), LIMIT)
        self.assertIn("확인", text.splitlines()[0])

    # -- other alert kinds --------------------------------------------------

    def test_orphaned_commits_keeps_the_tip_sha_whole(self):
        shas = [f"deadbeef{i:08d}cafef00d" for i in range(9)]
        a = self._assign()
        msg = m.Envelope(to=m.ORCHESTRATOR, type=m.PROGRESS, task_id=a.task_id,
                         payload={"orphaned_commits": {"count": 9, "tip": "abcdef1234567890",
                                                       "shas": shas},
                                  "branch": "worker/x", "run_dir": "20260916T-x"})
        self.orch._notify_orphaned_commits(a, msg)
        text, _ = self._last()
        self.assertLessEqual(len(text), LIMIT)
        self.assertIn("tip abcdef12", text)             # 8-char short sha, whole
        self.assertIn("9개", text.splitlines()[0])

    def test_poison_worst_input_stays_bounded(self):
        a = self._assign(refail_count=3, last_note="failure detail " * 40)
        self.orch._notify_poison(a)
        text, _ = self._last()
        self.assertLessEqual(len(text), LIMIT)
        self.assertIn("수동", text.splitlines()[0])

    def test_timeout_park_bounded_and_actionable(self):
        a = self._assign(timeout_streak=2)
        self.orch._notify_timeout_park(a)
        text, _ = self._last()
        self.assertLessEqual(len(text), LIMIT)
        self.assertIn("long_job", text.splitlines()[0])

    def test_parent_complete_worst_input_bounded(self):
        rows = [{"slug": f"parent-task-{i}", "title": "상위 태스크 제목 " * 3,
                 "n_children": 4} for i in range(20)]
        self.orch._notify_parent_complete(rows)
        text, _ = self._last()
        self.assertLessEqual(len(text), LIMIT)
        self.assertIn("20건", text.splitlines()[0])

    def test_propagate_downstream_alert_bounded(self):
        created = [f"integcheck-downstream-consumer-{i}" for i in range(15)]
        text = self.orch._compose_propagate_alert("some-platform-project", created)
        self.assertLessEqual(len(text), LIMIT)
        self.assertIn("some-platform-project", text)


if __name__ == "__main__":
    unittest.main()
