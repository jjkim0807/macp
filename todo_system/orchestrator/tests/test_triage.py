"""Attention triage — deterministic diagnosis rules (todo_system/orchestrator/triage.py).

Phase 1 pins `diagnose()` on synthetic facts shaped like the engine's real
strings (runner._classify_redelivery's reason, git/GitHub push refusals, the
ledger's alert kinds and history types). Each of the eight acceptance cases gets
its own named test so a verifier can cite it by name; the real-case fixtures
(tmp vault -> gather() -> diagnose()) live further down.

Every test pins MACP_LANGUAGE itself (the reason/advice text is i18n).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system import i18n
from todo_system.orchestrator import resume, triage
from todo_system.orchestrator.registry import Agent, Registry

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


# --- the real cases of the week: tmp vault -> gather() -> diagnose() --------------
# triage_cases/*.json are the eight parked/failed rows of 2026-11-17..20 rebuilt
# from the vault's git history at park time (ledger row, md fields, the run
# artifacts this host had), anonymised. Each names its origin in `source`.

_CASES_DIR = Path(__file__).resolve().parent / "triage_cases"
_CASE_FILES = ("review_pr.json", "nonff_push.json", "remote_workflow.json",
               "orphan_redelivery.json", "human_misroute.json", "ui_merge_hold.json",
               "device_unverified.json", "reaped_off.json")


def _load_case(name: str) -> dict:
    return json.loads((_CASES_DIR / name).read_text(encoding="utf-8"))


class _TmpVault(_Lang):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        for d in ("Worker/assignments", "Todo/items", "Worker/runs", "Worker/_state"):
            (self.root / d).mkdir(parents=True)
        self.registry = None
        triage._CACHE.clear()
        self.addCleanup(triage._CACHE.clear)

    def install(self, case: dict) -> str:
        slug = case["slug"]
        self.write_row(slug, case["ledger"])
        (self.root / "Todo/items" / f"{slug}.md").write_text(case["task_md"], encoding="utf-8")
        if case.get("run"):
            rd = self.root / "Worker/runs" / case["run"]["name"]
            rd.mkdir()
            for name, text in case["run"]["files"].items():
                (rd / name).write_text(text, encoding="utf-8")
        reg = case.get("registry")
        self.registry = Registry([Agent.from_dict(a) for a in reg["agents"]]) if reg else None
        return slug

    def write_row(self, slug: str, row) -> Path:
        p = self.root / "Worker/assignments" / f"{slug}.json"
        p.write_text(row if isinstance(row, str) else json.dumps(row, ensure_ascii=False),
                     encoding="utf-8")
        return p

    def gather(self, slug: str, **kw):
        kw.setdefault("registry", self.registry)
        return triage.gather(slug, assign_dir=self.root / "Worker/assignments",
                             items_dir=self.root / "Todo/items",
                             runs_dir=self.root / "Worker/runs", vault_root=self.root, **kw)


class RealCasesKo(_TmpVault):
    def check(self, name: str) -> dict:
        case = _load_case(name)
        self.assertTrue(case["source"].endswith("anonymised"), name)
        self.assertIn("vault ledger @", case["source"], name)
        slug = self.install(case)
        t = triage.diagnose(self.gather(slug))
        exp = case["expect"]
        self.assertCard(t, exp["rule"], exp["actions"], exp.get("reason_has", ()),
                        exp.get("advice_has", ()))
        for s in exp.get("evidence_has", ()):
            self.assertTrue(any(s in e for e in t["evidence"]), (s, t["evidence"]))
        # the dashboard path lists runs_dir once and passes the name in
        latest = triage.latest_runs(self.root / "Worker/runs")
        self.assertEqual(triage.diagnose(self.gather(slug, latest_run=latest.get(slug, ""))), t)
        return t

    def test_case1_review_pr_auto_merge_off_real(self):
        t = self.check("review_pr.json")
        self.assertIn(i18n.t("triage.ev.pr", pr="https://github.com/example/app/pull/312"),
                      t["evidence"])

    def test_case2_nonff_push_real(self):
        t = self.check("nonff_push.json")
        self.assertIn("worker/app-171-preview-skip-r2", t["advice"])

    def test_case3_remote_runner_workflow_scope_real(self):
        t = self.check("remote_workflow.json")
        self.assertIn(i18n.t("triage.ev.remote_artifacts"), t["evidence"])

    def test_case3_remote_workflow_text_when_the_note_carries_it(self):
        # the same row once the refusal reaches the ledger (a remote FAIL reason)
        case = _load_case("remote_workflow.json")
        case["ledger"]["last_note"] = WORKFLOW_PUSH
        t = triage.diagnose(self.gather(self.install(case)))
        self.assertEqual(t["rule"], "workflow_scope")

    def test_case4_orphan_redelivery_real(self):
        self.check("orphan_redelivery.json")

    def test_case5_human_glob_misroute_real(self):
        t = self.check("human_misroute.json")
        self.assertIn(i18n.t("triage.ev.agent_human", agent="owner"), t["evidence"])

    def test_case6_ui_merge_hold_real(self):
        self.check("ui_merge_hold.json")

    def test_case7_emulator_ac_not_run_real(self):
        self.check("device_unverified.json")

    def test_case8_reaped_stale_while_off_real(self):
        self.check("reaped_off.json")

    def test_every_fixture_has_a_named_test(self):
        self.assertEqual(sorted(p.name for p in _CASES_DIR.glob("*.json")), sorted(_CASE_FILES))
        cases = {_load_case(n)["case"] for n in _CASE_FILES}
        self.assertEqual(cases, set("①②③④⑤⑥⑦⑧"))


class GatherKo(_TmpVault):
    def test_facts_of_a_real_row(self):
        slug = self.install(_load_case("review_pr.json"))
        f = self.gather(slug)
        self.assertEqual((f["state"], f["agent"], f["agent_kind"], f["last_alert_kind"]),
                         ("awaiting_human", "worker-default", "ai-local", "review-pr"))
        self.assertEqual((f["todo_status"], f["md_exists"], f["md_worker_mode"], f["auto_merge"]),
                         ("open", True, "auto", False))
        self.assertEqual((f["run"], f["verdict"], f["merged"]),
                         ("20261120T154935-demo-review-pr", "PASS", None))
        self.assertEqual(f["pr_url"], "https://github.com/example/app/pull/312")
        self.assertFalse(f["deferred"] or f["merged_park"] or f["resume_reserved"])
        self.assertEqual(set(f), {
            "slug", "state", "deferred", "todo_status", "md_exists", "md_worker_mode",
            "human_gate", "needs_decision", "auto_merge", "agent", "agent_kind",
            "agent_is_human", "human_pattern", "last_note", "last_alert_kind", "blocked_on",
            "timeout_streak", "refail_count", "history_types", "merged_park",
            "resume_reserved", "run", "verdict", "verify_blocked_on", "pr_url", "merged",
            "recommendation", "rebase_conflict", "result_text", "log_tail"})

    def test_cache_rereads_a_changed_file(self):
        case = _load_case("orphan_redelivery.json")
        slug = self.install(case)
        self.assertEqual(self.gather(slug)["last_note"], case["ledger"]["last_note"])
        p = self.write_row(slug, dict(case["ledger"], last_note="changed"))
        st = p.stat()
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        self.assertEqual(self.gather(slug)["last_note"], "changed")

    def test_cache_skips_an_unchanged_file(self):
        slug = self.install(_load_case("orphan_redelivery.json"))
        calls = []
        real = triage._load_json

        def counting(path):
            calls.append(path)
            return real(path)

        with mock.patch.object(triage, "_load_json", counting):
            self.gather(slug)
            self.gather(slug)
        self.assertEqual(len(calls), 1)

    def test_corrupt_or_missing_ledger_row_is_none(self):
        self.assertIsNone(self.gather("no-such-task"))
        for bad in ("{not json", "[1, 2]", ""):
            triage._CACHE.clear()
            self.write_row("broken", bad)
            self.assertIsNone(self.gather("broken"), bad)
        self.assertIsNone(triage.triage_for(
            "broken", assign_dir=self.root / "Worker/assignments",
            items_dir=self.root / "Todo/items", runs_dir=self.root / "Worker/runs",
            vault_root=self.root))

    def test_unreadable_md_leaves_the_md_facts_empty(self):
        case = _load_case("orphan_redelivery.json")
        slug = case["slug"]
        self.write_row(slug, case["ledger"])
        (self.root / "Todo/items" / f"{slug}.md").mkdir()        # a directory: read fails
        f = self.gather(slug)
        self.assertEqual((f["md_exists"], f["todo_status"], f["human_gate"]), (False, None, False))
        self.assertEqual(f["md_worker_mode"], "auto")               # from the ledger payload
        t = triage.diagnose(f)
        self.assertEqual(t["actions"], [])
        self.assertIn(i18n.t("triage.ev.md_missing"), t["evidence"])

    def test_latest_runs_keeps_suffix_slugs_apart(self):
        runs = self.root / "Worker/runs"
        for name in ("20261119T101010-poc", "20261120T101010-api-poc",
                     "20261118T101010-poc", "notes.txt", "20261120T1010-poc"):
            (runs / name).mkdir()
        got = triage.latest_runs(runs)
        self.assertEqual(got["poc"], "20261119T101010-poc")
        self.assertEqual(got["api-poc"], "20261120T101010-api-poc")
        self.assertEqual(triage.latest_runs(self.root / "missing"), {})

    def test_a_run_older_than_the_dispatch_is_not_this_park(self):
        case = _load_case("review_pr.json")
        case["run"]["name"] = "20261120T091500-demo-review-pr"   # created_ts is 10:42:44
        slug = self.install(case)
        f = self.gather(slug)
        self.assertEqual((f["run"], f["pr_url"], f["verdict"]), ("", None, None))
        t = triage.diagnose(f)
        self.assertEqual((t["rule"], _ids(t)), ("review_pr", ["instruct", "resume", "cancel"]))

    def test_run_freshness_reads_the_name_in_the_row_offset(self):
        self.assertTrue(triage._run_is_current("20261120T104300-x", "2026-11-20T10:42:44+09:00"))
        self.assertTrue(triage._run_is_current("20261120T104100-x", "2026-11-20T10:42:44+09:00"))
        self.assertFalse(triage._run_is_current("20261120T103000-x", "2026-11-20T10:42:44+09:00"))
        self.assertTrue(triage._run_is_current("20261120T103000-x", "garbage"))
        self.assertTrue(triage._run_is_current("garbage", "2026-11-20T10:42:44+09:00"))
        self.assertTrue(triage._run_is_current("20261120T104300-x", "2026-11-20T10:42:44"))

    def test_resume_reservation_is_seen(self):
        slug = self.install(_load_case("orphan_redelivery.json"))
        resume.schedule(self.root, slug, note="t")
        t = triage.diagnose(self.gather(slug))
        self.assertEqual(_ids(t), ["cancel"])
        self.assertIn(i18n.t("triage.ev.resume_reserved"), t["evidence"])

    def test_merged_but_blocked_history(self):
        case = _load_case("review_pr.json")
        case["ledger"]["history"].append({"type": "merged-but-blocked", "from": "orchestrator"})
        case["ledger"]["blocked_on"] = "publish the beta by hand"
        t = triage.diagnose(self.gather(self.install(case)))
        self.assertEqual((t["rule"], t["actions"]), ("merged_blocked", []))

    def test_closed_md_drops_the_card(self):
        case = _load_case("review_pr.json")
        case["task_md"] = case["task_md"].replace("status: open", "status: done")
        self.assertIsNone(triage.diagnose(self.gather(self.install(case))))

    def test_md_flags(self):
        case = _load_case("orphan_redelivery.json")
        case["task_md"] = ("---\ntags:\n  - todo\n  - decision\nstatus: open\n"
                           "human_gate: yes\nauto_merge: maybe\n---\n\n# x\n")
        f = self.gather(self.install(case))
        self.assertTrue(f["needs_decision"] and f["human_gate"])
        self.assertEqual(f["md_worker_mode"], "auto")          # md has none: payload's
        self.assertTrue(f["auto_merge"])                       # unparseable md: payload's
        case["task_md"] = "---\ntags: todo, x\nstatus: open\nneeds_decision: true\n---\n"
        triage._CACHE.clear()
        self.assertTrue(self.gather(self.install(case))["needs_decision"])

    def test_delegate_artifact_is_the_result_when_there_is_no_report(self):
        case = _load_case("review_pr.json")
        files = case["run"]["files"]
        files["delegate.md"] = files.pop("report.md").replace("stage: report", "stage: delegate")
        f = self.gather(self.install(case))
        self.assertEqual(f["pr_url"], "https://github.com/example/app/pull/312")

    def test_registry_names_the_matching_glob(self):
        slug = self.install(_load_case("human_misroute.json"))
        f = self.gather(slug)
        self.assertEqual((f["agent_is_human"], f["human_pattern"]), (True, "prep-*"))
        self.assertEqual(self.gather(slug, registry=None)["human_pattern"], "")
        self.assertTrue(self.gather(slug, registry=None)["agent_is_human"])   # ledger kind

    def test_triage_for_is_fail_soft(self):
        slug = self.install(_load_case("review_pr.json"))
        kw = dict(assign_dir=self.root / "Worker/assignments", items_dir=self.root / "Todo/items",
                  runs_dir=self.root / "Worker/runs", vault_root=self.root)
        self.assertEqual(triage.triage_for(slug, **kw)["rule"], "review_pr")
        with mock.patch.object(triage, "gather", side_effect=RuntimeError("boom")):
            self.assertIsNone(triage.triage_for(slug, **kw))


if __name__ == "__main__":
    unittest.main()
