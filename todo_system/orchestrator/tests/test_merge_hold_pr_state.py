"""UI-unseen merge hold vs. a PR already resolved outside the hold.

A person can merge (or close) a held PR by hand. Before this fix the hold record
stayed until expiry, then `reconcile_merge_holds` sent an immediate
"⌛ UI 미확인 보류 만료 … (PR은 열려 있음)" alert about a PR that was long merged.

The sweep now asks gh for the PR state first:

* MERGED / CLOSED → record deleted, one log line, NO alert, ledger untouched.
* OPEN            → existing behaviour (wait; on expiry alert "PR은 열려 있음").
* None (unknown)  → existing behaviour; on expiry the alert says "PR 상태 미확인".

gh is always faked; everything lives in a temp vault.
"""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system.kakao_format import LEVEL_IMMEDIATE
from todo_system.orchestrator import merge_hold, stages
from todo_system.orchestrator.assignment import AWAITING_HUMAN

NOW = dt.datetime(2026, 11, 11, 3, 0, 0, tzinfo=dt.timezone.utc)
HELD_SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
SLUG = "demo-task"
REPO = "owner/repo"


class ResolvedOutsideTest(unittest.TestCase):
    def test_definite_merged_or_closed_is_true(self):
        for state in ("MERGED", "CLOSED", "merged", "Closed"):
            with self.subTest(state=state):
                self.assertTrue(merge_hold.resolved_outside(state))

    def test_open_unknown_or_other_is_false(self):
        for state in ("OPEN", None, "", "DRAFT"):
            with self.subTest(state=state):
                self.assertFalse(merge_hold.resolved_outside(state))


class GhPrStateTimeoutTest(unittest.TestCase):
    def _run(self, **kwargs):
        seen: dict = {}

        def fake_run(args, **kw):
            seen.update(kw)
            seen["args"] = args
            return subprocess.CompletedProcess(args, 0, stdout='{"state":"merged"}',
                                               stderr="")

        with mock.patch.object(stages.subprocess, "run", side_effect=fake_run):
            state = stages.gh_pr_state("worker/t", REPO, **kwargs)
        return state, seen

    def test_default_timeout_is_unchanged(self):
        state, seen = self._run()
        self.assertEqual(state, "MERGED")
        self.assertEqual(seen["timeout"], 30)
        self.assertEqual(seen["args"][-2:], ["--repo", REPO])

    def test_timeout_kwarg_is_passed_through(self):
        state, seen = self._run(timeout=15)
        self.assertEqual(state, "MERGED")
        self.assertEqual(seen["timeout"], 15)


class ReconcileMergeHoldsPrStateTest(unittest.TestCase):
    def setUp(self):
        from todo_system.orchestrator.assignment import Assignment, Ledger
        from todo_system.orchestrator.mailbox import FileMailbox
        from todo_system.orchestrator.orchestrator import Orchestrator
        from todo_system.orchestrator.registry import Registry
        self._Assignment = Assignment
        self.root = Path(tempfile.mkdtemp())
        (self.root / "Todo" / "items").mkdir(parents=True)
        (self.root / "Worker" / "assignments").mkdir(parents=True)
        (self.root / "Worker" / "runs").mkdir(parents=True)
        (self.root / "Worker" / "_state").mkdir(parents=True)
        self.ledger = Ledger(self.root / "Worker" / "assignments")
        self.routed: list[tuple[str, object, object]] = []
        self.gh_calls: list[dict] = []
        self.pr_state: str | None = None

        def route(text, *, level=None, event_key=None, cooldown_s=0.0):
            self.routed.append((text, level, event_key))
            return "send", text

        self.orch = Orchestrator(
            FileMailbox(self.root / "Worker" / "mb" / "orchestrator"),
            Registry([]), self.ledger,
            notify=lambda text, link=None: None,
            alert_route=route,
        )

        def fake_gh(branch, repo=None, cwd=None, *, timeout=30):
            self.gh_calls.append({"branch": branch, "repo": repo, "cwd": cwd,
                                  "timeout": timeout})
            return self.pr_state

        clock = mock.patch.object(merge_hold, "_now", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        gh =mock.patch.object(stages, "gh_pr_state", side_effect=fake_gh)
        gh.start()
        self.addCleanup(gh.stop)
        merge = mock.patch.object(
            stages, "merge_pr",
            side_effect=AssertionError("merge_pr must not run in these cases"))
        self.merge_pr = merge.start()
        self.addCleanup(merge.stop)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # -- fixtures -----------------------------------------------------------

    def _seed(self):
        (self.root / "Todo" / "items" / f"{SLUG}.md").write_text(
            f"---\nstatus: open\n---\n\n# {SLUG}\n", encoding="utf-8")
        a = self._Assignment(task_id=SLUG, agent_id="ai-1", corr_id="c1",
                             kind="ai-local", state=AWAITING_HUMAN)
        self.ledger.save(a)

    def _hold(self, *, expired: bool):
        when = NOW - dt.timedelta(hours=72) if expired else NOW
        return merge_hold.schedule(
            self.root, SLUG, held_sha=HELD_SHA, branch=f"worker/{SLUG}",
            repo=REPO, pr_url="https://pr/1", note="UI 미확인", now=when)

    def _sweep(self, state: str | None, *, expired: bool) -> tuple[list[str], str]:
        self.pr_state = state
        self._seed()
        self._hold(expired=expired)
        with mock.patch("builtins.print") as pr:
            released = self.orch.reconcile_merge_holds()
        out = "\n".join(" ".join(str(x) for x in c.args) for c in pr.call_args_list)
        return released, out

    def _record_exists(self) -> bool:
        return merge_hold.record_path(self.root, SLUG).exists()

    def _assert_gh_asked_short(self):
        self.assertEqual(len(self.gh_calls), 1)
        call = self.gh_calls[0]
        self.assertEqual(call["branch"], f"worker/{SLUG}")
        self.assertEqual(call["repo"], REPO)
        self.assertLessEqual(call["timeout"], 15)

    # -- MERGED / CLOSED: quiet discard -------------------------------------

    def _assert_quiet_discard(self, state: str):
        released, out = self._sweep(state, expired=True)
        self.assertEqual(released, [])
        self.assertFalse(self._record_exists())
        self.assertEqual(self.routed, [], "no alert for a resolved PR")
        self.assertIn("merged outside the hold", out)
        self.assertIn(f"state={state}", out)
        self.assertEqual(self.ledger.load(SLUG).state, AWAITING_HUMAN)
        self.merge_pr.assert_not_called()
        self._assert_gh_asked_short()

    def test_merged_expired_hold_is_dropped_without_alert(self):
        self._assert_quiet_discard("MERGED")

    def test_closed_expired_hold_is_dropped_without_alert(self):
        self._assert_quiet_discard("CLOSED")

    def test_merged_hold_is_dropped_on_an_ordinary_tick_before_expiry(self):
        released, _ = self._sweep("MERGED", expired=False)
        self.assertEqual(released, [])
        self.assertFalse(self._record_exists())
        self.assertEqual(self.routed, [])
        self.assertEqual(self.ledger.load(SLUG).state, AWAITING_HUMAN)

    # -- OPEN: existing behaviour -------------------------------------------

    def test_open_unexpired_hold_waits_without_alert(self):
        released, out = self._sweep("OPEN", expired=False)
        self.assertEqual(released, [])
        self.assertTrue(self._record_exists())
        self.assertEqual(self.routed, [])
        self.assertIn("대기 유지", out)
        self._assert_gh_asked_short()

    def test_open_expired_hold_alerts_pr_is_open(self):
        released, _ = self._sweep("OPEN", expired=True)
        self.assertEqual(released, [])
        self.assertFalse(self._record_exists())
        self.assertEqual(len(self.routed), 1)
        text, level, key = self.routed[0]
        self.assertIn("PR은 열려 있음", text)
        self.assertEqual(level, LEVEL_IMMEDIATE)
        self.assertEqual(key, f"ui-merge-hold-expired:{SLUG}")

    # -- lookup failed (None): existing behaviour, honest wording -----------

    def test_unknown_unexpired_hold_waits_without_alert(self):
        released, _ = self._sweep(None, expired=False)
        self.assertEqual(released, [])
        self.assertTrue(self._record_exists())
        self.assertEqual(self.routed, [])

    def test_unknown_expired_hold_alerts_state_unconfirmed(self):
        released, _ = self._sweep(None, expired=True)
        self.assertEqual(released, [])
        self.assertFalse(self._record_exists())
        self.assertEqual(len(self.routed), 1)
        text, level, key = self.routed[0]
        self.assertIn("PR 상태 미확인", text)
        self.assertNotIn("PR은 열려 있음", text)
        self.assertEqual(level, LEVEL_IMMEDIATE)
        self.assertEqual(key, f"ui-merge-hold-expired:{SLUG}")

    # -- no ledger row: STALE without asking gh -----------------------------

    def test_rowless_record_is_stale_and_gh_is_not_called(self):
        self.pr_state = "OPEN"
        self._hold(expired=False)
        self.assertEqual(self.orch.reconcile_merge_holds(), [])
        self.assertFalse(self._record_exists())
        self.assertEqual(self.gh_calls, [])
        self.assertEqual(self.routed, [])


if __name__ == "__main__":
    unittest.main()
