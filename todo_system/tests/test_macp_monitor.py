"""Tests for scripts/macp_monitor.py — usage-hold awareness + path constants.

Two things are pinned here:

1. **Path constants.** The sensor was promoted from `Worker/_state/macp_monitor/`
   into tracked `scripts/`, which changes `__file__`'s depth. If `ROOT` or
   `STATE_DIR` regress, the monitor silently starts writing `state.json` /
   `latest.json` into tracked `scripts/` instead of the git-ignored state dir —
   a no-symptom failure that nothing else would catch.

2. **USAGE-HOLD demotion.** When the orchestrator's most recent tick was held by
   the usage guard, a long-pending-with-no-assignment task is explained by
   pacing, not by a routing failure — so the finding is demoted to `info` and
   annotated, but NOT dropped (dropping it would erase the signal).

⚠ Never call `scan()` from a test. It writes the real `state.json` via
`save_state()` and shells out to `gh issue list`. The checks are called directly.

scripts/ has no __init__.py, so the module is loaded by path via importlib (the
module top level is stdlib imports + constants, so import is side-effect free).
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import unittest
from pathlib import Path

VAULT = Path(__file__).resolve().parents[2]
_SRC = VAULT / "scripts" / "macp_monitor.py"

# The real orchestrator line this parser has to survive: the hold reason itself
# contains parentheses, so a `\(([^)]*)\)` regex would truncate at "pace (66%".
REAL_HOLD_LINE = (
    "candidates=5 dispatched=0 updated=0 renudged=0 reaped=0 reconciled=0 "
    "unblocked=0 parents=0 requeued=0 BUDGET-REACHED (runs today=40/40) "
    "USAGE-HOLD (weekly 78% ahead of pace (66% elapsed + 12pp allowance))"
)
REAL_HOLD_REASON = "weekly 78% ahead of pace (66% elapsed + 12pp allowance)"
UNHELD_LINE = (
    "candidates=5 dispatched=1 updated=0 renudged=0 reaped=0 reconciled=0 "
    "unblocked=0 parents=0 requeued=0"
)


def _load():
    spec = importlib.util.spec_from_file_location("macp_monitor", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load()


class PathConstantTest(unittest.TestCase):
    """Guards the promotion from Worker/_state/ into scripts/."""

    @unittest.skipUnless(
        (VAULT / ".macp" / "config.toml").is_file(),
        "vault-only: ROOT==vault (holding pyproject.toml) is a combined-layout fact; "
        "an engine-only export tree resolves ROOT to a separate vault dir with no "
        "pyproject.toml (public-split-10 AC2/AC4). The live vault has the marker and runs it.",
    )
    def test_root_is_the_vault(self):
        self.assertTrue((M.ROOT / "pyproject.toml").exists(),
                        f"ROOT should be the vault root, got {M.ROOT}")

    def test_state_dir_stays_under_worker_state(self):
        # Must NOT follow __file__ into scripts/ — state stays git-ignored.
        self.assertEqual(M.STATE_DIR, M.ROOT / "Worker" / "_state" / "macp_monitor")
        self.assertEqual(M.STATE_DIR.parent.name, "_state")
        self.assertNotEqual(M.STATE_DIR, _SRC.parent)

    def test_state_files_live_beside_the_state_dir(self):
        self.assertEqual(M.STATE_FILE.parent, M.STATE_DIR)
        self.assertEqual(M.LATEST_FILE.parent, M.STATE_DIR)


class UsageHoldReasonTest(unittest.TestCase):
    """Parsing the hold reason off the most recent tick line."""

    def _log(self, *lines: str) -> Path:
        import tempfile
        p = Path(tempfile.mkdtemp()) / "orchestrator.out.log"
        p.write_text("\n".join(lines) + "\n")
        return p

    def test_nested_parens_survive(self):
        self.assertEqual(M.usage_hold_reason(self._log(REAL_HOLD_LINE)),
                         REAL_HOLD_REASON)

    def test_hold_followed_by_unheld_tick_reports_no_hold(self):
        # Only the LAST tick counts: once the hold clears, findings must go
        # straight back to `bug` rather than staying suppressed for N ticks.
        self.assertIsNone(M.usage_hold_reason(self._log(REAL_HOLD_LINE, UNHELD_LINE)))

    def test_hold_on_the_last_tick_is_found_behind_earlier_ticks(self):
        self.assertEqual(
            M.usage_hold_reason(self._log(UNHELD_LINE, UNHELD_LINE, REAL_HOLD_LINE)),
            REAL_HOLD_REASON)

    def test_non_tick_trailing_lines_do_not_hide_the_hold(self):
        # The orchestrator prints "  dispatched <task> -> <agent>" after a tick.
        self.assertEqual(
            M.usage_hold_reason(self._log(REAL_HOLD_LINE, "  dispatched foo -> bar")),
            REAL_HOLD_REASON)

    def test_missing_file_is_not_an_error(self):
        self.assertIsNone(M.usage_hold_reason(Path("/nonexistent/orchestrator.out.log")))

    def test_log_without_tick_lines(self):
        self.assertIsNone(M.usage_hold_reason(self._log("some unrelated log line")))


class StalePendingDemotionTest(unittest.TestCase):
    """check_stale_pending called directly — never via scan()."""

    def _stale_run(self):
        created = (M._now() - dt.timedelta(hours=60)).isoformat(timespec="seconds")
        return {"slug": "dripcoffee-bean-dialing-explorer", "status": "pending",
                "created_at": created, "mode": "auto"}

    def _findings(self, hold_reason):
        findings: list = []
        M.check_stale_pending(findings, [self._stale_run()], {}, hold_reason)
        return findings

    def test_without_hold_it_is_a_bug(self):
        findings = self._findings(None)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "bug")
        self.assertNotIn("USAGE-HOLD", findings[0]["detail"])

    def test_with_hold_it_is_demoted_to_info_and_explains_why(self):
        findings = self._findings(REAL_HOLD_REASON)
        self.assertEqual(len(findings), 1, "the finding must survive, not be dropped")
        self.assertEqual(findings[0]["severity"], "info")
        self.assertIn(REAL_HOLD_REASON, findings[0]["detail"])

    def test_demotion_leaves_fingerprint_title_and_evidence_identical(self):
        # Hold state must not enter the fingerprint, or a toggling hold would
        # re-fire the same stuck task as a "new" finding and defeat filed-dedup.
        plain, held = self._findings(None)[0], self._findings(REAL_HOLD_REASON)[0]
        for key in ("fingerprint", "title", "evidence"):
            self.assertEqual(plain[key], held[key], f"{key} must not change")

    def test_fresh_pending_is_never_reported(self):
        fresh = self._stale_run()
        fresh["created_at"] = M._now().isoformat(timespec="seconds")
        findings: list = []
        M.check_stale_pending(findings, [fresh], {}, REAL_HOLD_REASON)
        self.assertEqual(findings, [])

    def test_pending_with_an_assignment_is_never_reported(self):
        run = self._stale_run()
        findings: list = []
        M.check_stale_pending(findings, [run], {run["slug"]: {"state": "dispatched"}}, None)
        self.assertEqual(findings, [])


class PrePoisonTest(unittest.TestCase):
    """The window `check_poison` cannot see: still `failed`, not yet quarantined.

    Modelled on the real 2026-09-01 mulkko-5-ladder-editor ledger entry, whose
    "missing verify.md" note was actually a misclassified quota stall.
    """

    def _assign(self, **over):
        a = {"task_id": "mulkko-5-ladder-editor", "state": "failed",
             "refail_count": M.POISON_THRESHOLD - 1,
             "last_note": "stage verify: missing verify.md"}
        a.update(over)
        return {a["task_id"]: a}

    def _findings(self, **over):
        findings: list = []
        M.check_pre_poison(findings, self._assign(**over))
        return findings

    def test_one_refail_from_quarantine_is_reported(self):
        findings = self._findings()
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "triage")
        self.assertEqual(findings[0]["evidence"]["refail"], M.POISON_THRESHOLD - 1)

    def test_last_note_rides_along_because_it_is_the_diagnosis(self):
        self.assertIn("missing verify.md", self._findings()[0]["title"])

    def test_healthy_and_early_failures_are_quiet(self):
        for over in ({"state": "done"}, {"state": "dispatched"}, {"refail_count": 0}):
            self.assertEqual(self._findings(**over), [], over)

    def test_deferred_states_never_fire(self):
        # These never escalate to poison, so warning about it would be a lie.
        for flag in ("capacity_limited", "job_running", "checkout_busy"):
            self.assertEqual(self._findings(**{flag: True}), [], flag)

    def test_already_quarantined_is_left_to_check_poison(self):
        self.assertEqual(self._findings(state="awaiting_human"), [])


class ErrorRegexTest(unittest.TestCase):
    """Lines that were live blind spots on 2026-09-01."""

    def test_runner_fail_prefix_and_quota_notice_match(self):
        for line in ("  FAIL: stage verify: missing verify.md",
                     "You've reached your Fable 5 limit. Run /usage-credits to continue",
                     "You're out of usage credits · resets 6pm"):
            self.assertTrue(M.ERR_RE.search(line), line)

    def test_ordinary_lines_still_do_not_match(self):
        for line in ("wrote Todo/Orchestrator.md", "usage guard cleared — resuming"):
            self.assertFalse(M.ERR_RE.search(line), line)


class ReleaseFreshnessTest(unittest.TestCase):
    """check_release_freshness called directly — never via scan().

    Both sides of the comparison are injected: the release side as a watermark
    JSON passed via `config_path`, the merge side as reflog fixtures written in
    git's real on-disk format (tab-separated, `+0900` tz). Nothing here touches
    the vault's real config or any real checkout.
    """

    RELEASED = "2026-07-22"     # fixture watermark — the DanceMirror freeze date
    _SHA_A, _SHA_B = "a" * 40, "b" * 40

    def _tmp(self) -> Path:
        import tempfile
        return Path(tempfile.mkdtemp())

    def _epoch(self, days_after_release: float) -> int:
        base = dt.datetime.fromisoformat(self.RELEASED)
        return int((base + dt.timedelta(days=days_after_release)).timestamp())

    def _write_reflog(self, repo: Path, branch: str, epochs: list) -> Path:
        log = repo / ".git" / "logs" / "refs" / "heads" / branch
        log.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{self._SHA_A} {self._SHA_B} Jane Doe <jj@example.com> {e} +0900\tcommit: work"
                 for e in epochs]
        log.write_text("\n".join(lines) + "\n")
        return repo

    def _app(self, repo_dirs: list, **over) -> dict:
        a = {"slug": "dancemirror-app", "repo_dirs": [str(d) for d in repo_dirs],
             "branch": "main", "channel": "firebase:beta-testers",
             "released_at": self.RELEASED}
        a.update(over)
        return a

    def _check(self, apps: list) -> list:
        import json
        config = self._tmp() / "release_channels.json"
        config.write_text(json.dumps({"apps": apps}))
        findings: list = []
        M.check_release_freshness(findings, config_path=config)
        return findings

    def _assert_contract(self, finding: dict) -> None:
        # The exact 5-key contract every check_* emits — nothing more, nothing less.
        self.assertEqual(sorted(finding),
                         ["detail", "evidence", "fingerprint", "severity", "title"])

    def test_merge_eight_days_past_release_is_triage(self):
        repo = self._write_reflog(self._tmp() / "DanceMirror", "main", [self._epoch(8)])
        findings = self._check([self._app([repo])])
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self._assert_contract(f)
        self.assertEqual(f["severity"], "triage")
        # released_at date is part of the fingerprint: a NEW episode against a
        # newer watermark must re-fire even after this one was filed+resolved.
        self.assertEqual(f["fingerprint"], "release-freshness:dancemirror-app:2026-07-22")
        self.assertEqual(f["evidence"]["lead_days"], 8)
        self.assertEqual(f["evidence"]["channel"], "firebase:beta-testers")
        self.assertEqual(f["evidence"]["released_at"], "2026-07-22T00:00:00")

    def test_exactly_threshold_days_fires(self):
        # "N일 이상" is inclusive — the boundary day already alarms.
        repo = self._write_reflog(self._tmp() / "r", "main",
                                  [self._epoch(M.RELEASE_FRESHNESS_STALE_DAYS)])
        findings = self._check([self._app([repo])])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "triage")
        self.assertEqual(findings[0]["evidence"]["lead_days"],
                         M.RELEASE_FRESHNESS_STALE_DAYS)

    def test_under_threshold_is_quiet(self):
        repo = self._write_reflog(self._tmp() / "r", "main",
                                  [self._epoch(M.RELEASE_FRESHNESS_STALE_DAYS - 1)])
        self.assertEqual(self._check([self._app([repo])]), [])

    def test_release_newer_than_merge_is_quiet(self):
        repo = self._write_reflog(self._tmp() / "r", "main", [self._epoch(-3)])
        self.assertEqual(self._check([self._app([repo])]), [])

    def test_missing_released_at_is_info_not_triage(self):
        # Even with a very stale-looking reflog available, a missing/unparseable
        # watermark must yield the backfill nag, never a staleness verdict.
        repo = self._write_reflog(self._tmp() / "r", "main", [self._epoch(30)])
        for released_at in (None, "not-a-date"):
            findings = self._check([self._app([repo], released_at=released_at)])
            self.assertEqual(len(findings), 1, released_at)
            f = findings[0]
            self._assert_contract(f)
            self.assertEqual(f["severity"], "info")
            self.assertEqual(f["fingerprint"],
                             "release-freshness:no-release:dancemirror-app")

    def test_missing_or_malformed_config_is_quiet(self):
        findings: list = []
        M.check_release_freshness(findings, config_path=self._tmp() / "absent.json")
        self.assertEqual(findings, [])
        bad = self._tmp() / "release_channels.json"
        for payload in ("[]", '{"apps": "nope"}', "not json at all"):
            bad.write_text(payload)
            findings = []
            M.check_release_freshness(findings, config_path=bad)
            self.assertEqual(findings, [], payload)

    def test_unreadable_merge_source_never_alarms(self):
        # A months-stale watermark with no readable reflog stays silent — the
        # no-false-positive rule outranks staleness suspicion.
        ghost = self._tmp() / "nonexistent"
        wrong_branch = self._write_reflog(self._tmp() / "r", "main", [self._epoch(100)])
        for app in (self._app([ghost]),                          # checkout gone
                    self._app([]),                               # gutenberg-reader seed shape
                    self._app([wrong_branch], branch="release")):  # branch has no reflog
            self.assertEqual(self._check([app]), [], app)

    def test_max_epoch_across_repo_dirs_and_last_reflog_line_win(self):
        # Multi-line reflogs must be read at their LAST line, and with several
        # configured checkouts the freshest one decides. branch=master also pins
        # that the per-app branch is honored (a main-only reader would skip both
        # fixtures and stay silent).
        tmp = self._tmp()
        older = self._write_reflog(tmp / "older", "master",
                                   [self._epoch(-30), self._epoch(2)])
        newer = self._write_reflog(tmp / "newer", "master",
                                   [self._epoch(1), self._epoch(9)])
        findings = self._check([self._app([older, newer], branch="master")])
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["evidence"]["lead_days"], 9)

    def test_config_constant_points_at_tracked_worker_file(self):
        # Worker/_state/ is git-ignored wholesale; the human-edited watermark
        # must live in the tracked Worker/ top level to keep its edit history.
        self.assertEqual(M.RELEASE_CHANNELS, M.ROOT / "Worker" / "release_channels.json")

    @unittest.skipUnless(
        M.RELEASE_CHANNELS.is_file(),
        "vault-only: the release_channels.json seed is vault data, not in the engine "
        "code export set — absent in an engine-only tree (public-split-10 AC2). The "
        "live vault ships the seed and runs this validation.",
    )
    def test_seed_config_is_valid(self):
        # The real tracked seed must stay loadable and well-formed: a typo in
        # this human-edited file fails *silently* at runtime (skip-by-design),
        # so the test suite is where it has to turn red instead.
        import json
        cfg = json.loads(M.RELEASE_CHANNELS.read_text())
        apps = cfg["apps"]
        self.assertIsInstance(apps, list)
        self.assertTrue(apps)
        slugs = [a["slug"] for a in apps]
        self.assertEqual(len(slugs), len(set(slugs)), "slugs must be unique")
        for a in apps:
            self.assertTrue(a["slug"])
            self.assertIsInstance(a["repo_dirs"], list, a["slug"])
            if a.get("released_at") is not None:
                self.assertIsNotNone(M._parse_ts(a["released_at"]),
                                     f"unparseable released_at for {a['slug']}")


# "caller did not pass open_prs" — distinct from an explicit `open_prs=None`,
# which is the module's own default and means "go do the real lazy lookup".
_NO_PR_ARG = object()


class UnmergedDelegateBranchTest(unittest.TestCase):
    """check_unmerged_delegate_branch called directly — never via scan().

    Pins the 2026-09-09 stranding: three `delegate` vault tasks wrote real files
    (e.g. `Notes/cover-letter-research-staff.md`), committed them on
    `worker/<slug>`, and the runner's `finally: restore_main` walked away. The
    sensor stayed silent and a human found all three by hand.

    Real git and the network are kept out by patching the module's own seams:
    `_now` (clock), `_branch_ref_exists` (git rev-parse) and `_landed_on_main`
    (git merge-base / log). The check is pure once those three are injected.

    The open-PR lookup is a fourth seam, but a *parameter* rather than a patch —
    `_check` passes `{}` ("looked, no PR") by default, so `gh` is never executed
    from this class. Pass `open_prs=` explicitly to exercise a branch→PR map,
    `M.PR_LOOKUP_FAILED`, or `None` (the production default = lazy real lookup).
    """

    NOW = dt.datetime(2026, 9, 9, 17, 0, 0)

    def _assign(self, task_id, *, hours_ago=2.0, repo=None, **payload_over):
        payload = {"branch": f"worker/{task_id}", "worker_mode": "delegate",
                   "auto_merge": True}
        if repo:
            payload["repo"] = repo
        payload.update(payload_over)
        updated = (self.NOW - dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")
        return {"task_id": task_id, "state": "done", "updated_ts": updated,
                "payload": payload}

    def _check(self, assigns_list, *, landed=False, open_prs=_NO_PR_ARG, settled=None):
        """Run the check with git/clock/network seams stubbed. `landed` is what
        `_landed_on_main` reports for every branch.

        `open_prs` defaults to the *sentinel*, not to None, so that omitting it
        injects `{}` (no `gh`) while an explicit `open_prs=None` still reaches
        the production default and triggers the lazy real lookup — which is what
        the "at most once per scan" / "no candidate, no lookup" tests need.
        """
        from unittest import mock
        assigns = {a["task_id"]: a for a in assigns_list}
        findings: list = []
        with mock.patch.object(M, "_now", return_value=self.NOW), \
             mock.patch.object(M, "_branch_ref_exists", side_effect=lambda b: b), \
             mock.patch.object(M, "_landed_on_main", return_value=landed), \
             mock.patch.object(M, "settled_pr_for_branch", return_value=settled):
            M.check_unmerged_delegate_branch(
                findings, assigns,
                {} if open_prs is _NO_PR_ARG else open_prs)
        return findings

    # -- A. fingerprint carries the slug ------------------------------------
    def test_two_strandings_produce_two_separately_fingerprinted_findings(self):
        # RED control: with the old constant fingerprint these collapsed into a
        # single aggregate finding, so filing one muted the other forever.
        findings = self._check([self._assign("research-staff-cover-letter-draft"),
                                self._assign("worldgraph-19-if-design-proposal")])
        self.assertEqual(len(findings), 2)
        fps = [f["fingerprint"] for f in findings]
        self.assertEqual(len(set(fps)), 2, f"fingerprints must differ, got {fps}")
        for f in findings:
            task_id = f["evidence"]["task_id"]
            self.assertEqual(f["fingerprint"], f"unmerged-delegate-branch:{task_id}")
            self.assertIn(task_id, f["title"],
                          "the slug must be in the title too — scan() also dedups "
                          "against open ISSUE TITLES, not just state.filed")

    # -- B. vault gets a tighter threshold than repo -------------------------
    def test_vault_branch_is_reported_after_hours_not_after_half_a_day(self):
        # The live miss: updated 15:07, found by a human ~16:00. A 12h threshold
        # would not have fired until 03:07 the next day.
        findings = self._check([self._assign("research-staff-cover-letter-draft",
                                             hours_ago=2)])
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0]["evidence"]["is_vault"])
        self.assertEqual(findings[0]["evidence"]["threshold_hours"],
                         M.UNMERGED_VAULT_BRANCH_STALE_HOURS)

    def test_repo_branch_at_the_same_age_stays_quiet(self):
        # A repo task's PR is legitimately waiting on CI at 2h — firing here
        # would be the noise that trains the human to ignore the check.
        self.assertEqual(
            self._check([self._assign("dripcoffee-ios-klib-executed-gate",
                                      hours_ago=2, repo="example-org/DripCoffee")]),
            [])

    def test_repo_branch_past_the_repo_threshold_still_fires(self):
        findings = self._check([self._assign("dripcoffee-ios-klib-executed-gate",
                                             hours_ago=13, repo="example-org/DripCoffee")])
        self.assertEqual(len(findings), 1)
        self.assertFalse(findings[0]["evidence"]["is_vault"])
        self.assertEqual(findings[0]["evidence"]["threshold_hours"],
                         M.UNMERGED_BRANCH_STALE_HOURS)

    def test_vault_threshold_is_tighter_than_the_repo_one(self):
        self.assertLess(M.UNMERGED_VAULT_BRANCH_STALE_HOURS,
                        M.UNMERGED_BRANCH_STALE_HOURS)

    def test_a_vault_branch_younger_than_the_threshold_is_quiet(self):
        self.assertEqual(
            self._check([self._assign("research-staff-cover-letter-draft",
                                      hours_ago=0.2)]),
            [])

    # -- C. one filed slug must not mute the next one ------------------------
    def test_a_filed_sibling_does_not_swallow_a_new_stranding(self):
        # Reproduces scan()'s filter inline rather than calling scan(), which
        # writes the real state.json and shells out to `gh`.
        findings = self._check([self._assign("research-staff-cover-letter-draft")])
        filed = {"unmerged-delegate-branch:worldgraph-19-if-design-proposal": {
            "url": "https://example.invalid/1", "at": "2026-09-07T21:10:23"}}
        titles = {"[macp-monitor] unmerged-delegate-branch:"
                  "worldgraph-19-if-design-proposal 좌초"}
        survivors = [f for f in findings
                     if f["fingerprint"] not in filed
                     and not any(M.ISSUE_MARKER in t and f["fingerprint"] in t
                                 for t in titles)]
        self.assertEqual(len(survivors), 1,
                         "a previously filed slug must not mute a different one")

    def test_the_same_slug_is_still_deduped(self):
        # The flip side: per-task fingerprints must not defeat dedup for the
        # task that was actually filed.
        findings = self._check([self._assign("worldgraph-19-if-design-proposal")])
        filed = {"unmerged-delegate-branch:worldgraph-19-if-design-proposal": {}}
        self.assertEqual([f for f in findings if f["fingerprint"] not in filed], [])

    # -- D. recovery is carried in the finding -------------------------------
    def test_detail_carries_the_pgrep_precondition_and_the_merge_command(self):
        # A finding that only says "it is stranded" made the human re-derive the
        # recovery each time; the pgrep check is a hard precondition (touching a
        # live worker's checkout races it).
        detail = self._check([self._assign("research-staff-cover-letter-draft")])[0]["detail"]
        self.assertIn("pgrep -fl research-staff-cover-letter-draft", detail)
        self.assertIn("merge --no-ff worker/research-staff-cover-letter-draft", detail)

    # -- E. negative controls ------------------------------------------------
    def test_landed_branches_are_never_reported(self):
        self.assertEqual(
            self._check([self._assign("research-staff-romin-cover-paragraph")],
                        landed=True),
            [])

    def test_auto_merge_false_is_excluded(self):
        # `false` means the human deliberately holds the merge — not a stranding.
        self.assertEqual(
            self._check([self._assign("macp-planner-guardrail", auto_merge=False)]),
            [])

    def test_unfinished_tasks_and_branchless_payloads_are_quiet(self):
        for over in ({"state": "dispatched"}, {"state": "failed"}):
            a = self._assign("research-staff-cover-letter-draft")
            a.update(over)
            self.assertEqual(self._check([a]), [], over)
        branchless = self._assign("research-staff-cover-letter-draft")
        branchless["payload"].pop("branch")
        self.assertEqual(self._check([branchless]), [])

    def test_a_vanished_ref_is_not_a_stranding(self):
        # `_branch_ref_exists` returning None means the branch is gone (deleted
        # after merge) or belongs to another repo — neither is vault stranding.
        from unittest import mock
        findings: list = []
        assigns = {a["task_id"]: a for a in
                   [self._assign("research-staff-cover-letter-draft")]}
        with mock.patch.object(M, "_now", return_value=self.NOW), \
             mock.patch.object(M, "_branch_ref_exists", return_value=None), \
             mock.patch.object(M, "_landed_on_main", return_value=False):
            M.check_unmerged_delegate_branch(findings, assigns)
        self.assertEqual(findings, [])

    # -- F. an open PR on the branch is not a stranding ----------------------
    # Regression guard for the follow-on to `stages.publish_branch()` (merged
    # dfc16b95a): once delegate vault branches get pushed with a PR opened, the
    # "waiting for nobody" premise of section B is false for exactly those, and
    # a correctly-delivered output started alarming as `bug` one hour later.
    PR = {"number": 71, "url": "https://github.com/example-org/MaestroACP/pull/71"}

    def test_open_pr_on_the_branch_demotes_to_info(self):
        findings = self._check(
            [self._assign("macp-vault-delegate-branch-push-pr")],
            open_prs={"worker/macp-vault-delegate-branch-push-pr": self.PR})
        # Demoted, NOT dropped — the module convention is that an explained
        # signal stays visible (see check_stale_pending's hold demotion).
        self.assertEqual(len(findings), 1)
        f = findings[0]
        self.assertEqual(f["severity"], "info")
        self.assertEqual(f["evidence"]["pr_number"], 71)
        self.assertEqual(f["evidence"]["pr_url"], self.PR["url"])
        self.assertIn("리뷰 대기", f["title"])
        self.assertNotIn("좌초", f["title"])
        self.assertNotIn("좌초", f["detail"])
        self.assertIn(self.PR["url"], f["detail"])
        # Fingerprint is unchanged so scan()'s dedup does not read the demotion
        # as a brand-new finding every time a PR opens or closes.
        self.assertEqual(f["fingerprint"],
                         "unmerged-delegate-branch:macp-vault-delegate-branch-push-pr")

    def test_a_pr_on_a_different_branch_does_not_demote(self):
        # Pins the index key as `payload.branch`: a PR that happens to be open
        # on some *other* branch must not explain this stranding away.
        findings = self._check(
            [self._assign("research-staff-cover-letter-draft")],
            open_prs={"worker/some-other-task": self.PR})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "bug")
        self.assertNotIn("pr_number", findings[0]["evidence"])

    def test_no_open_pr_keeps_the_bug_finding(self):
        # `{}` means "looked, and nothing covers it" — the original alarm must
        # survive the new seam verbatim, fingerprint included.
        findings = self._check([self._assign("research-staff-cover-letter-draft")],
                               open_prs={})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "bug")
        self.assertIn("좌초", findings[0]["title"])
        self.assertEqual(findings[0]["fingerprint"],
                         "unmerged-delegate-branch:research-staff-cover-letter-draft")

    def test_pr_lookup_failure_does_not_demote(self):
        # fail-loud: a down/absent `gh` must never mute a real stranding. The
        # sentinel is what the real helper returns on failure, so this injects
        # the production failure value, not a test-only stand-in.
        findings = self._check([self._assign("research-staff-cover-letter-draft")],
                               open_prs=M.PR_LOOKUP_FAILED)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["severity"], "bug")
        self.assertNotIn("pr_number", findings[0]["evidence"])
        self.assertNotIn("pr_url", findings[0]["evidence"])

    def test_the_sentinel_is_not_confused_with_an_empty_map(self):
        # The whole point of a sentinel: both are falsy, so any `if prs:` style
        # test would collapse them and silently route failures down the `{}` path.
        self.assertIsNot(M.PR_LOOKUP_FAILED, {})
        self.assertFalse(bool({}))

    def test_the_pr_lookup_runs_at_most_once_per_scan(self):
        # open_prs=None is the production default; two candidates must still
        # cost exactly one `gh` invocation.
        from unittest import mock
        with mock.patch.object(M, "open_prs_by_branch", return_value={}) as lookup:
            findings = self._check([self._assign("research-staff-cover-letter-draft"),
                                    self._assign("worldgraph-19-if-design-proposal")],
                                   open_prs=None)
        self.assertEqual(len(findings), 2)
        self.assertEqual(lookup.call_count, 1)

    def test_no_candidate_means_no_lookup_at_all(self):
        # Laziness is load-bearing twice over: the common scan (nothing
        # stranded) must not shell out, and it is why the vanished-ref test
        # above can call the check with the real default without hitting `gh`.
        from unittest import mock
        with mock.patch.object(M, "open_prs_by_branch", return_value={}) as lookup:
            findings = self._check([self._assign("research-staff-cover-letter-draft")],
                                   landed=True, open_prs=None)
        self.assertEqual(findings, [])
        self.assertEqual(lookup.call_count, 0)

    # -- G. a PR open for days is a stalled review, not a wait ---------------
    # Section F's demotion shipped with no age axis, which made it a permanent
    # mute: a PR open for five minutes and one open for five days both read
    # `info`, so "리뷰 대기" was the last thing this check ever said about a
    # task and the stranding it exists to catch went quiet for good. These pin
    # the age axis that reopens it.

    def _pr(self, *, hours_open, number=71):
        """The section-F `PR` fixture plus a `createdAt` exactly `hours_open`
        old relative to `NOW`, in `gh`'s UTC (`...Z`) form.

        Deliberately NOT folded into the `PR` constant above: giving that one a
        `createdAt` would age every section-F PR at once and flip
        `test_open_pr_on_the_branch_demotes_to_info` to `warn`.

        `NOW` is local naive because it stands in for `_now()`. Calling
        `.astimezone(utc)` on it therefore reads it as local wall-clock and
        converts — the exact inverse of the module's `_parse_pr_ts`, so the
        round trip returns `hours_open` on any machine timezone rather than
        only in KST.
        """
        created = ((self.NOW - dt.timedelta(hours=hours_open))
                   .astimezone(dt.timezone.utc))
        return {**self.PR, "number": number,
                "createdAt": created.isoformat().replace("+00:00", "Z")}

    def _findings_for(self, task_id, pr):
        return self._check([self._assign(task_id)],
                           open_prs={f"worker/{task_id}": pr})

    def test_a_freshly_opened_pr_still_reads_as_a_plain_wait(self):
        # Under the threshold nothing about section F's behaviour may move.
        f = self._findings_for("macp-vault-delegate-branch-push-pr",
                               self._pr(hours_open=1))[0]
        self.assertEqual(f["severity"], "info")
        self.assertIn("리뷰 대기", f["title"])
        self.assertNotIn("정체", f["title"])
        self.assertNotIn("좌초", f["title"])
        self.assertEqual(f["evidence"]["pr_open_hours"], 1.0)

    def test_a_pr_open_for_days_warns_and_says_how_long(self):
        f = self._findings_for("macp-vault-delegate-branch-push-pr",
                               self._pr(hours_open=72))[0]
        self.assertEqual(f["severity"], "warn")
        self.assertIn("3일", f["title"])
        self.assertIn("#71", f["title"])
        # A stalled review is not a stranding: the branch is published and the
        # PR is right there. Borrowing the `bug` path's word would mis-route
        # the human to `merge --no-ff` on a branch that has a PR open.
        self.assertNotIn("좌초", f["title"])
        self.assertNotIn("좌초", f["detail"])
        self.assertIn(self.PR["url"], f["detail"])
        self.assertEqual(f["evidence"]["pr_open_hours"], 72.0)

    def test_an_age_between_one_and_two_days_is_reported_in_hours(self):
        # "1일" would round away the whole difference between 24h and 47h.
        f = self._findings_for("macp-vault-delegate-branch-push-pr",
                               self._pr(hours_open=25))[0]
        self.assertEqual(f["severity"], "warn")
        self.assertIn("25시간", f["title"])
        self.assertNotIn("1일", f["title"])

    def test_the_threshold_is_inclusive_at_its_own_value(self):
        # "OPEN_PR_STALE_HOURS 이상" — exactly the threshold warns, a hair
        # under it does not. Written against the constant so retuning it moves
        # both sides of the boundary together.
        task_id = "macp-vault-delegate-branch-push-pr"
        at = self._findings_for(task_id,
                                self._pr(hours_open=M.OPEN_PR_STALE_HOURS))
        self.assertEqual(at[0]["severity"], "warn")
        under = self._findings_for(
            task_id, self._pr(hours_open=M.OPEN_PR_STALE_HOURS - 0.1))
        self.assertEqual(under[0]["severity"], "info")

    def test_the_severity_climb_does_not_move_the_fingerprint(self):
        # scan() dedups on `fingerprint` alone. If it moved with severity, a
        # task would re-fire as a brand-new finding the moment its PR crossed
        # the threshold — and filing the `info` would stop muting anything.
        task_id = "macp-vault-delegate-branch-push-pr"
        fresh = self._findings_for(task_id, self._pr(hours_open=1))[0]
        stale = self._findings_for(task_id, self._pr(hours_open=72))[0]
        self.assertEqual(fresh["severity"], "info")
        self.assertEqual(stale["severity"], "warn")
        self.assertEqual(fresh["fingerprint"], stale["fingerprint"])
        self.assertEqual(fresh["fingerprint"],
                         f"unmerged-delegate-branch:{task_id}")
        # Same one the `bug` path uses, so a stranding that later acquires a PR
        # (or loses one) does not re-fire either.
        bug = self._check([self._assign(task_id)], open_prs={})[0]
        self.assertEqual(bug["severity"], "bug")
        self.assertEqual(bug["fingerprint"], fresh["fingerprint"])

    def test_an_unusable_created_at_stays_info_rather_than_guessing(self):
        # fail-soft, and deliberately the opposite of the fail-loud lookup
        # failure in section F: there nothing explained the branch at all,
        # whereas here an open PR already does and only its age is unknown.
        # Warning on a missing field would invent an alarm, not surface one.
        task_id = "macp-vault-delegate-branch-push-pr"
        for over in ({}, {"createdAt": None}, {"createdAt": "not-a-date"},
                     {"createdAt": ""}):
            findings = self._findings_for(task_id, {**self.PR, **over})
            self.assertEqual(len(findings), 1, over)
            self.assertEqual(findings[0]["severity"], "info", over)
            self.assertIsNone(findings[0]["evidence"]["pr_open_hours"], over)

    def test_pr_timestamps_are_read_in_the_clock_frame_now_uses(self):
        # RED control for the tz skew: `gh` stamps `createdAt` in UTC, but
        # `_parse_ts` discards the offset while `_now()` is local naive, so
        # feeding one to the other ages every PR by the UTC offset — in KST a
        # PR opened 15h ago would already measure 24h and warn 9h early.
        s = "2026-09-09T08:00:00Z"
        aware = dt.datetime(2026, 9, 9, 8, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(M._parse_pr_ts(s),
                         aware.astimezone().replace(tzinfo=None))
        # Stated machine-independently: the old parser reads exactly one UTC
        # offset older than the new one (a no-op only where that offset is 0).
        self.assertEqual(M._parse_pr_ts(s) - M._parse_ts(s),
                         aware.astimezone().utcoffset())

    def test_an_offset_less_timestamp_is_left_to_the_original_parser(self):
        # The ledger's own `updated_ts` carries no offset and must keep parsing
        # exactly as it always has: `_parse_pr_ts` is an addition, not a
        # replacement for `_parse_ts`.
        s = "2026-09-09T17:00:00"
        self.assertEqual(M._parse_pr_ts(s), M._parse_ts(s))
        self.assertEqual(M._parse_pr_ts(s), dt.datetime(2026, 9, 9, 17, 0))
        for bad in (None, "", "not-a-date"):
            self.assertIsNone(M._parse_pr_ts(bad), bad)



    # -- settled PR (2026-10-21) --------------------------------------------
    def test_merged_pr_counts_as_landed(self):
        # RED control: 6 squash-merged branches whose squash subject lacked the
        # slug were escalated as `bug` in the 10-21 brief.
        merged = {"state": "MERGED", "number": 85, "url": "u", "repo": "o/r"}
        self.assertEqual(self._check([self._assign("t-merged")], settled=merged), [])

    def test_closed_pr_demotes_to_info_with_same_fingerprint(self):
        closed = {"state": "CLOSED", "number": 331, "url": "u", "repo": "o/r"}
        (f,) = self._check([self._assign("t-closed")], settled=closed)
        self.assertEqual(f["severity"], "info")
        self.assertEqual(f["fingerprint"], "unmerged-delegate-branch:t-closed")
        self.assertEqual(f["evidence"]["pr_state"], "CLOSED")

    def test_settled_lookup_failure_stays_bug(self):
        (f,) = self._check([self._assign("t-fail")], settled=M.PR_LOOKUP_FAILED)
        self.assertEqual(f["severity"], "bug")

    def test_no_settled_pr_stays_bug(self):
        (f,) = self._check([self._assign("t-none")], settled=None)
        self.assertEqual(f["severity"], "bug")

    def test_open_pr_wins_over_settled_lookup(self):
        from unittest import mock
        pr = {"number": 1, "url": "u", "createdAt": None}
        with mock.patch.object(M, "settled_pr_for_branch") as look:
            findings = []
            with mock.patch.object(M, "_now", return_value=self.NOW), \
                 mock.patch.object(M, "_branch_ref_exists", side_effect=lambda b: b), \
                 mock.patch.object(M, "_landed_on_main", return_value=False):
                M.check_unmerged_delegate_branch(findings, {"t": self._assign("t")}, {"worker/t": pr})
            look.assert_not_called()
        self.assertEqual(findings[0]["severity"], "info")

    def test_repo_slug_from_url(self):
        self.assertEqual(M._repo_slug_from_url("https://github.com/o/r.git"), "o/r")
        self.assertEqual(M._repo_slug_from_url("git@github.com:o/r"), "o/r")
        self.assertIsNone(M._repo_slug_from_url(None))
        self.assertIsNone(M._repo_slug_from_url("https://example.com/o/r"))


class LandedOnMainPatchEquivalenceTest(unittest.TestCase):
    """`_landed_on_main` against a real throwaway git repo: a branch whose only
    commit was cherry-picked onto main counts as landed (10-21: two of the 11
    false strandings); a branch with one un-picked commit does not."""

    def _repo(self):
        import subprocess
        import tempfile
        d = Path(tempfile.mkdtemp())
        def g(*a):
            return subprocess.run(["git", *a], cwd=d, check=True, capture_output=True)
        g("init", "-q", "-b", "main")
        g("config", "user.email", "t@t")
        g("config", "user.name", "t")
        for branch, name, body, msg in ((None, "a", "1", "base"), ("worker/x", "b", "x", "work"),
                                        ("main", "c", "y", "other")):
            if branch:
                g("checkout", "-q", *(["-b"] if branch != "main" else []), branch)
            (d / name).write_text(body + "\n")
            g("add", name)
            g("commit", "-qm", msg)
        return d, g

    def test_cherry_picked_branch_is_landed(self):
        from unittest import mock
        d, g = self._repo()
        g("cherry-pick", "worker/x")
        with mock.patch.object(M, "ROOT", d):
            self.assertTrue(M._landed_on_main("unrelated-slug", "worker/x"))

    def test_unpicked_commit_is_not_landed(self):
        from unittest import mock
        d, g = self._repo()
        with mock.patch.object(M, "ROOT", d):
            self.assertFalse(M._landed_on_main("unrelated-slug", "worker/x"))

if __name__ == "__main__":
    unittest.main()
