"""Attention triage — deterministic diagnosis rules (todo_system/orchestrator/triage.py).

Phase 1 pins `diagnose()` on synthetic facts shaped like the engine's real
strings (runner._classify_redelivery's reason, git/GitHub push refusals, the
ledger's alert kinds and history types). Each of the eight acceptance cases gets
its own named test so a verifier can cite it by name; the real-case fixtures
(tmp vault -> gather() -> diagnose()) live further down.

Every test pins MACP_LANGUAGE itself (the reason/advice text is i18n).
"""
from __future__ import annotations

import os
import re
import unittest
from unittest import mock

from todo_system import i18n
from todo_system.orchestrator import triage

_HANGUL = re.compile("[가-힣]")
PR = "https://github.com/example/app/pull/12"
SLUG = "demo-task"
RUN = "20261120T101010-demo-task"
ORPHAN_NOTE = ("redelivered envelope, execution already started (orphan): pid 4242 on laptop "
               "is gone (signal-0 probe failed) — its stage subprocesses died with it (812s)")
WORKFLOW_PUSH = ("! [remote rejected] worker/demo-task -> worker/demo-task (refusing to allow "
                 "an OAuth App to create or update workflow `.github/workflows/build.yml` "
                 "without `workflow` scope)")
NONFF_PUSH = (" ! [rejected]        worker/demo-task -> worker/demo-task (non-fast-forward)\n"
              "hint: Updates were rejected because the tip of your current branch is behind")


def _facts(**kw) -> dict:
    """A parked local row with nothing notable — each test adds its signal."""
    f = {
        "slug": SLUG, "state": "awaiting_human", "deferred": False, "todo_status": "open",
        "md_exists": True, "md_worker_mode": "auto", "human_gate": False,
        "needs_decision": False, "auto_merge": True, "agent": "worker-default",
        "agent_kind": "ai-local", "agent_is_human": False, "human_pattern": "",
        "last_note": "", "last_alert_kind": "", "blocked_on": "", "timeout_streak": 0,
        "refail_count": 0, "history_types": ["ACCEPT", "PROGRESS", "RESULT"],
        "merged_park": False, "resume_reserved": False, "run": RUN, "verdict": None,
        "verify_blocked_on": None, "pr_url": None, "merged": None, "recommendation": None,
        "rebase_conflict": False, "result_text": "", "log_tail": "",
    }
    f.update(kw)
    return f


def _ids(t: dict) -> list[str]:
    return [a["id"] for a in t["actions"]]


class _Lang(unittest.TestCase):
    LANG = "ko"

    def setUp(self):
        p = mock.patch.dict(os.environ, {"MACP_LANGUAGE": self.LANG})
        p.start()
        self.addCleanup(p.stop)

    def assertCard(self, t, rule, actions, reason_has=(), advice_has=()):
        self.assertIsNotNone(t)
        self.assertEqual(t["rule"], rule)
        self.assertEqual(_ids(t), actions)
        for s in reason_has:
            self.assertIn(s, t["reason"])
        for s in advice_has:
            self.assertIn(s, t["advice"])
        return t


# --- the eight cases of the week (acceptance criterion #1), synthetic facts -----

# The facts each case is built from, shared with the en test below.
CASES = {
    "review_pr": _facts(last_alert_kind="review-pr", pr_url=PR, verdict="PASS",
                        merged=None, auto_merge=False, last_note="staged pipeline complete"),
    "nonff_push": _facts(last_alert_kind="result-no-pr", verdict="PASS", merged=False,
                         recommendation="review", result_text="push failed:\n" + NONFF_PUSH,
                         last_note="merge withheld for human review"),
    "workflow_scope": _facts(agent="macbook", agent_kind="ai-remote", run="",
                             last_alert_kind="result-no-pr", last_note=WORKFLOW_PUSH),
    "remote_no_pr": _facts(agent="macbook", agent_kind="ai-remote", run="",
                           last_alert_kind="result-no-pr",
                           last_note="staged pipeline complete (5 stages) — auto_merge did NOT "
                                     "land (conflict/red check/perm); needs review"),
    "orphan_redelivery": _facts(agent="desktop-wsl", agent_kind="ai-remote", run="",
                                last_note=ORPHAN_NOTE,
                                history_types=["ACCEPT", "PROGRESS", "NEEDS_INPUT"]),
    "human_misroute": _facts(agent="owner", agent_kind="human", agent_is_human=True,
                             human_pattern="prep-*", md_worker_mode="delegate", run="",
                             history_types=[]),
    "ui_merge_hold": _facts(last_alert_kind="ui-merge-hold", pr_url=PR, verdict="PASS",
                            last_note="staged pipeline complete (5 stages) — UI hold"),
    "device_unverified": _facts(last_alert_kind="blocked", verdict="FAIL",
                                blocked_on="AC 8 emulator check needs a human with adb",
                                verify_blocked_on="AC 8 emulator check needs a human with adb",
                                history_types=["ACCEPT", "PROGRESS", "FAIL"]),
    "reaped_off": _facts(state="failed", md_worker_mode="off", run="",
                         history_types=["reaped-stale"]),
}


class EightCasesKo(_Lang):
    def test_case1_review_pr_auto_merge_off_verify_pass(self):
        t = self.assertCard(triage.diagnose(CASES["review_pr"]), "review_pr",
                            ["merge", "instruct", "cancel"],
                            reason_has=["머지만 남았"], advice_has=["[머지]"])
        self.assertIn(i18n.t("triage.ev.auto_merge_off"), t["evidence"])
        self.assertIn(i18n.t("triage.ev.pr", pr=PR), t["evidence"])

    def test_case2_nonff_push_withheld(self):
        t = self.assertCard(triage.diagnose(CASES["nonff_push"]), "nonff_push",
                            ["instruct", "resume", "cancel"],
                            reason_has=["non-fast-forward"], advice_has=["-r2"])
        self.assertIn(f"worker/{SLUG}-r2", t["advice"])
        self.assertIn("force push", t["advice"])

    def test_case3_remote_runner_workflow_scope_refused(self):
        self.assertCard(triage.diagnose(CASES["workflow_scope"]), "workflow_scope",
                        ["resume", "instruct", "cancel"],
                        reason_has=["workflow 권한"], advice_has=["본부(HQ)"])

    def test_case3_remote_runner_no_pr_ledger_only(self):
        t = self.assertCard(triage.diagnose(CASES["remote_no_pr"]), "remote_no_pr",
                            ["resume", "instruct", "cancel"],
                            reason_has=["원격 러너", "macbook"],
                            advice_has=["workflow 권한", "본부(HQ)"])
        self.assertIn(i18n.t("triage.ev.remote_artifacts"), t["evidence"])

    def test_case4_orphan_redelivery_game_mode(self):
        t = self.assertCard(triage.diagnose(CASES["orphan_redelivery"]), "orphan_redelivery",
                            ["resume", "cancel"],
                            reason_has=["러너가 재시작"], advice_has=["[재개]"])
        self.assertIn(i18n.t("triage.ev.redelivered", verdict="orphan"), t["evidence"])

    def test_case5_human_glob_misroute(self):
        t = self.assertCard(triage.diagnose(CASES["human_misroute"]), "human_misroute",
                            ["resume", "cancel"],
                            reason_has=["사람 담당 패턴"], advice_has=["registry.yaml"])
        self.assertIn(i18n.t("triage.ev.pattern", pattern="prep-*"), t["evidence"])

    def test_case6_ui_merge_hold_is_abolished(self):
        self.assertCard(triage.diagnose(CASES["ui_merge_hold"]), "ui_merge_hold",
                        ["merge", "cancel"], reason_has=["폐지"], advice_has=["[머지]"])

    def test_case7_emulator_ac_not_run(self):
        self.assertCard(triage.diagnose(CASES["device_unverified"]), "device_unverified",
                        ["instruct", "resume", "cancel"], reason_has=["머지 조건이 아니"],
                        advice_has=["확인 못 함"])

    def test_case7_emulator_ac_with_a_mergeable_pr(self):
        f = dict(CASES["device_unverified"], pr_url=PR, verdict="PASS")
        self.assertCard(triage.diagnose(f), "device_unverified", ["merge", "instruct", "cancel"],
                        reason_has=["머지 조건이 아니"], advice_has=["[머지]"])

    def test_case8_reaped_stale_while_off(self):
        t = self.assertCard(triage.diagnose(CASES["reaped_off"]), "reaped_off", ["cancel"],
                            reason_has=["실행된 적 없음", "off"], advice_has=["[취소]"])
        self.assertNotIn(i18n.t("triage.ev.not_dispatchable", mode="off"), t["evidence"])


class RuleOrderKo(_Lang):
    def test_workflow_beats_nonff(self):
        f = _facts(last_note=WORKFLOW_PUSH, result_text=NONFF_PUSH)
        self.assertEqual(triage.diagnose(f)["rule"], "workflow_scope")

    def test_push_rules_need_no_pr(self):
        # once a PR exists, a stale push refusal in the logs is not the reason
        f = _facts(pr_url=PR, verdict="PASS", last_alert_kind="review-pr", log_tail=NONFF_PUSH)
        self.assertEqual(triage.diagnose(f)["rule"], "review_pr")

    def test_device_beats_blocked(self):
        f = _facts(blocked_on="on-device check not run (emulator)")
        self.assertEqual(triage.diagnose(f)["rule"], "device_unverified")
        f = _facts(blocked_on="upstream PR not merged yet")
        self.assertEqual(triage.diagnose(f)["rule"], "blocked")

    def test_ui_merge_hold_beats_review_pr(self):
        f = _facts(last_alert_kind="ui-merge-hold", pr_url=PR, verdict="PASS")
        self.assertEqual(triage.diagnose(f)["rule"], "ui_merge_hold")

    def test_merged_blocked_never_offers_resume(self):
        f = _facts(merged_park=True, blocked_on="publish the beta by hand",
                   last_alert_kind="review-pr", pr_url=PR, verdict="PASS")
        t = self.assertCard(triage.diagnose(f), "merged_blocked", [],
                            reason_has=["이미 머지"], advice_has=["status: done"])
        self.assertNotIn("resume", _ids(t))

    def test_reaped_but_dispatchable_is_not_reaped_off(self):
        f = _facts(state="failed", history_types=["reaped-stale"])
        self.assertEqual(triage.diagnose(f)["rule"], "fallback")

    def test_reaped_after_running_is_not_never_ran(self):
        f = _facts(state="failed", md_worker_mode="off",
                   history_types=["ACCEPT", "PROGRESS", "reaped-stale"])
        self.assertEqual(triage.diagnose(f)["rule"], "fallback")

    def test_reconciled_history_does_not_hide_reaped(self):
        f = _facts(state="failed", md_worker_mode="off",
                   history_types=["reaped-stale", "reconciled-ledger-tidy"])
        self.assertEqual(triage.diagnose(f)["rule"], "reaped_off")

    def test_human_misroute_skips_decisions_and_manual(self):
        f = dict(CASES["human_misroute"], needs_decision=True)
        self.assertNotEqual(triage.diagnose(f)["rule"], "human_misroute")
        f = dict(CASES["human_misroute"], md_worker_mode="manual")
        self.assertNotEqual(triage.diagnose(f)["rule"], "human_misroute")

    def test_remaining_rules(self):
        self.assertCard(triage.diagnose(_facts(last_alert_kind="rescope-pending")),
                        "rescope_pending", ["cancel"], advice_has=["자동으로 나눠"])
        t = self.assertCard(triage.diagnose(_facts(timeout_streak=2)), "timeout_park",
                            ["instruct", "resume", "cancel"], reason_has=["시간 초과", "2"])
        self.assertIn(i18n.t("triage.ev.timeouts", n=2), t["evidence"])
        self.assertEqual(triage.diagnose(_facts(last_alert_kind="timeout-park"))["rule"],
                         "timeout_park")
        self.assertCard(triage.diagnose(_facts(blocked_on="needs an API key")), "blocked",
                        ["resume", "instruct", "cancel"], reason_has=["선행 조건"])
        self.assertCard(triage.diagnose(_facts(last_alert_kind="result-no-pr")),
                        "result_no_pr", ["instruct", "cancel"], reason_has=["PR이 없어요"])
        t = self.assertCard(triage.diagnose(_facts(last_note="boom", refail_count=3)),
                            "fallback", ["resume", "instruct", "cancel"],
                            reason_has=["멈춘 이유", "boom"])
        self.assertIn(i18n.t("triage.ev.refail", n=3), t["evidence"])
        self.assertIn("멈춘 이유", triage.diagnose(_facts())["reason"])

    def test_rules_tuple_is_the_evaluation_order(self):
        self.assertEqual(triage.RULES, (
            "merged_blocked", "orphan_redelivery", "workflow_scope", "nonff_push",
            "remote_no_pr", "human_misroute", "reaped_off", "ui_merge_hold",
            "device_unverified", "review_pr", "rescope_pending", "timeout_park", "blocked",
            "result_no_pr", "fallback"))


class MergeConditionKo(_Lang):
    def test_verify_fail_means_no_merge(self):
        t = triage.diagnose(dict(CASES["review_pr"], verdict="FAIL"))
        self.assertEqual(_ids(t), ["instruct", "resume", "cancel"])
        self.assertIn("머지 버튼을 두지 않았어요", t["advice"])

    def test_unknown_verdict_means_no_merge(self):
        t = triage.diagnose(dict(CASES["review_pr"], verdict=None))
        self.assertNotIn("merge", _ids(t))

    def test_rebase_conflict_means_no_merge(self):
        t = triage.diagnose(dict(CASES["review_pr"], rebase_conflict=True))
        self.assertNotIn("merge", _ids(t))
        self.assertIn(i18n.t("triage.ev.rebase_conflict"), t["evidence"])

    def test_unparseable_pr_url_means_no_merge(self):
        for url in ("https://example.com/pr/1", "github.com/o/r/pull/1",
                    "https://github.com/o/r/pull/x", "https://github.com/o/r/issues/3"):
            t = triage.diagnose(dict(CASES["review_pr"], pr_url=url))
            self.assertNotIn("merge", _ids(t), url)

    def test_review_pr_without_any_pr_url(self):
        t = triage.diagnose(dict(CASES["review_pr"], pr_url=None))
        self.assertEqual(_ids(t), ["instruct", "resume", "cancel"])
        self.assertIn("PR 주소", t["advice"])

    def test_ui_hold_without_a_mergeable_pr(self):
        t = triage.diagnose(dict(CASES["ui_merge_hold"], verdict="FAIL"))
        self.assertEqual(_ids(t), ["resume", "cancel"])


class PostFiltersKo(_Lang):
    def test_missing_md_offers_nothing(self):
        t = triage.diagnose(dict(CASES["orphan_redelivery"], md_exists=False))
        self.assertEqual(t["actions"], [])
        self.assertIn(i18n.t("triage.ev.md_missing"), t["evidence"])

    def test_non_dispatchable_md_drops_resume_and_instruct(self):
        t = triage.diagnose(dict(CASES["orphan_redelivery"], md_worker_mode="manual"))
        self.assertEqual(_ids(t), ["cancel"])
        self.assertIn(i18n.t("triage.ev.not_dispatchable", mode="manual"), t["evidence"])
        t = triage.diagnose(dict(CASES["review_pr"], md_worker_mode=None))
        self.assertEqual(_ids(t), ["merge", "cancel"])

    def test_reserved_resume_is_not_offered_twice(self):
        t = triage.diagnose(dict(CASES["orphan_redelivery"], resume_reserved=True))
        self.assertEqual(_ids(t), ["cancel"])
        self.assertIn(i18n.t("triage.ev.resume_reserved"), t["evidence"])
        t = triage.diagnose(dict(CASES["nonff_push"], resume_reserved=True))
        self.assertEqual(_ids(t), ["instruct", "cancel"])

    def test_human_gate_keeps_only_cancel(self):
        for name, f in CASES.items():
            t = triage.diagnose(dict(f, human_gate=True))
            self.assertTrue(set(_ids(t)) <= {"cancel"}, name)
            self.assertEqual(t["advice"], i18n.t("triage.gate.advice"), name)
            self.assertIn(i18n.t("triage.ev.human_gate"), t["evidence"], name)
        t = triage.diagnose(dict(CASES["review_pr"], human_gate=True))
        self.assertEqual(_ids(t), ["cancel"])

    def test_evidence_is_capped(self):
        f = dict(CASES["review_pr"], recommendation="review", rebase_conflict=True,
                 resume_reserved=True, human_gate=True, md_worker_mode="manual")
        t = triage.diagnose(f)
        self.assertLessEqual(len(t["evidence"]), 6)
        # the post-filter lines survive the cap — they explain the missing buttons
        self.assertIn(i18n.t("triage.ev.human_gate"), t["evidence"])
        for f in CASES.values():
            self.assertLessEqual(len(triage.diagnose(f)["evidence"]), 6)

    def test_labels_come_from_i18n(self):
        t = triage.diagnose(CASES["review_pr"])
        self.assertEqual([a["label"] for a in t["actions"]], ["머지", "지시 추가", "취소"])
        self.assertEqual(list(t), ["rule", "reason", "evidence", "advice", "actions"])
        for a in t["actions"]:
            self.assertEqual(sorted(a), ["id", "label"])


class NotATargetKo(_Lang):
    def test_states_outside_attention(self):
        for state in ("dispatched", "in_progress", "done", "rejected", "needs_input", ""):
            self.assertIsNone(triage.diagnose(dict(CASES["orphan_redelivery"], state=state)),
                              state)

    def test_deferred_rows(self):
        self.assertIsNone(triage.diagnose(_facts(state="failed", deferred=True)))

    def test_closed_md(self):
        for status in ("done", "cancelled", "canceled", "Done "):
            self.assertIsNone(triage.diagnose(_facts(todo_status=status)), status)
        self.assertIsNotNone(triage.diagnose(_facts(todo_status=None)))

    def test_not_a_dict(self):
        self.assertIsNone(triage.diagnose(None))
        self.assertIsNone(triage.diagnose("x"))


class FixedValues(unittest.TestCase):
    def test_action_ids_are_a_closed_set(self):
        self.assertEqual(triage.ACTIONS, ("merge", "resume", "cancel", "instruct"))
        self.assertEqual(triage.TARGET_STATES, ("awaiting_human", "failed"))

    def test_dispatchable_mirror(self):
        from todo_system.orchestrator import tasks
        self.assertEqual(triage._DISPATCHABLE, tasks.DISPATCHABLE_MODES)

    def test_pr_ref(self):
        self.assertEqual(triage.pr_ref("https://github.com/o/r/pull/12"), ("o/r", 12))
        self.assertEqual(triage.pr_ref(" https://github.com/o-x/r.y_z/pull/7/ "),
                         ("o-x/r.y_z", 7))
        for bad in (None, "", "https://github.com/o/r/pull/", "http://github.com/o/r/pull/1",
                    "https://github.com/o/r/pull/1/files", "https://gitlab.com/o/r/pull/1",
                    "https://github.com/o/r/pull/1 extra"):
            self.assertIsNone(triage.pr_ref(bad), bad)

    def test_every_rule_has_reason_and_advice_in_both_languages(self):
        for lang in ("ko", "en"):
            table = i18n.MESSAGES[lang]
            for rule in triage.RULES:
                self.assertIn(f"triage.{rule}.reason", table, (lang, rule))
                self.assertIn(f"triage.{rule}.advice", table, (lang, rule))
            for a in triage.ACTIONS:
                self.assertIn(f"triage.action.{a}", table, (lang, a))


class EnglishCards(_Lang):
    LANG = "en"

    def test_en_cards_carry_no_hangul(self):
        extra = {"timeout": _facts(timeout_streak=3), "blocked": _facts(blocked_on="x"),
                 "rescope": _facts(last_alert_kind="rescope-pending"),
                 "merged": _facts(merged_park=True, blocked_on="x"),
                 "fallback": _facts(), "fallback_note": _facts(last_note="boom"),
                 "gate": dict(CASES["review_pr"], human_gate=True, resume_reserved=True,
                              md_worker_mode="manual"),
                 "missing": dict(CASES["review_pr"], md_exists=False),
                 "nopr": dict(CASES["review_pr"], pr_url=None),
                 "fail": dict(CASES["review_pr"], verdict="FAIL")}
        for name, f in {**CASES, **extra}.items():
            t = triage.diagnose(f)
            texts = [t["reason"], t["advice"], *t["evidence"], *(a["label"] for a in t["actions"])]
            for s in texts:
                self.assertIsNone(_HANGUL.search(s), (name, s))
                self.assertNotIn("triage.", s, (name, s))      # no missing key


if __name__ == "__main__":
    unittest.main()
