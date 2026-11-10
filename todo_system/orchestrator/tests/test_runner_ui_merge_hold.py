"""Verify-confidence guard + UI-unseen auto-merge hold (runner-enforced).

macp#131 put the rule — "an app UI change with no device run must not be
`confidence: high`, and must not auto-merge" — in the verify stage's PROMPT only.
A prompt is advisory: the stage can still write `high` and the runner would still
auto-merge. This suite pins the ENGINE enforcement that replaces the prompt rule:

* Phase 1 — pure readers: `stages.parse_verify_confidence`,
  `screen_check.has_device_pass`.
* Phase 2 — the host-local hold record module `merge_hold` (schedule / evaluate /
  scan_device_evidence), modelled on `resume.py`.
* Phase 3 — the runner guard: a `high` verify for an app-UI diff with no device
  evidence is recorded `medium` and parked (auto_merge withheld, hold scheduled).
* Phase 4 — the release tick: a later same-sha device PASS releases the merge; one
  alert; regression: non-app repos and app runs WITH a device pass are untouched.

Everything stays inside a `tempfile.mkdtemp()` vault/clone skeleton, and every
git / gh / node effect is faked — no real `Worker/_state/`, no real git, no
emulator. (The `_run` / seam discipline is `test_resume.py`'s.)
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import agent_backend, merge_hold, repos
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator import screen_check, stages
from todo_system.orchestrator import message as m
from todo_system.orchestrator.assignment import AWAITING_HUMAN, DONE, FAILED
from todo_system.orchestrator.registry import Agent
from todo_system.orchestrator.runner import StagedExecutor

NOW = dt.datetime(2026, 11, 11, 3, 0, 0, tzinfo=dt.timezone.utc)
HELD_SHA = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"


# ---------------------------------------------------------------------------
# Phase 1 — pure readers
# ---------------------------------------------------------------------------

class ParseVerifyConfidenceTest(unittest.TestCase):
    def test_reads_each_level(self):
        for level in ("high", "medium", "low"):
            with self.subTest(level=level):
                self.assertEqual(
                    stages.parse_verify_confidence(
                        f"---\noverall_status: pass\nconfidence: {level}\n---\n"),
                    level)

    def test_case_insensitive_and_lowercased(self):
        self.assertEqual(
            stages.parse_verify_confidence("confidence: HIGH\n"), "high")

    def test_absent_is_none(self):
        self.assertIsNone(stages.parse_verify_confidence(
            "---\noverall_status: pass\n---\n"))
        self.assertIsNone(stages.parse_verify_confidence(""))
        self.assertIsNone(stages.parse_verify_confidence(None))

    def test_first_match_wins(self):
        # The frontmatter line is authoritative; a later body mention is ignored.
        self.assertEqual(
            stages.parse_verify_confidence(
                "confidence: medium\n\n## body\nI was confidence: high earlier\n"),
            "medium")


class HasDevicePassTest(unittest.TestCase):
    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.run_dir, ignore_errors=True)

    def _write(self, note: str | None):
        text = "---\noverall_status: pass\nconfidence: high\n---\n\n# verify\n"
        (self.run_dir / "verify.md").write_text(text, encoding="utf-8")
        if note is not None:
            screen_check.append_note(self.run_dir, note)

    def test_success_note_is_evidence(self):
        self._write(f"{screen_check.OK_NOTE_PREFIX} — screens/app-1.png")
        self.assertTrue(screen_check.has_device_pass(self.run_dir))

    def test_failure_note_is_not_evidence(self):
        self._write("화면 확인 불가(앱 리포 아님 — gradlew 없음)")
        self.assertFalse(screen_check.has_device_pass(self.run_dir))

    def test_no_note_is_not_evidence(self):
        self._write(None)
        self.assertFalse(screen_check.has_device_pass(self.run_dir))

    def test_missing_file_is_not_evidence(self):
        self.assertFalse(screen_check.has_device_pass(self.run_dir / "nope"))

    def test_ok_note_prefix_is_the_one_written(self):
        # Writer and reader share the constant — a drift would make the guard
        # pass vacuously (never see the real note).
        note = screen_check._ok_note("/x/screens/a.png", self.run_dir)
        self.assertTrue(note.startswith(screen_check.OK_NOTE_PREFIX))


class DowngradeVerifyConfidenceTest(unittest.TestCase):
    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.run_dir, ignore_errors=True)

    def _verify(self, text: str) -> Path:
        p = self.run_dir / "verify.md"
        p.write_text(text, encoding="utf-8")
        return p

    def test_high_becomes_medium_with_a_guard_note(self):
        p = self._verify("---\noverall_status: pass\nconfidence: high\n---\n\n# v\n")
        self.assertTrue(stages.downgrade_verify_confidence(self.run_dir, "no device run"))
        out = p.read_text(encoding="utf-8")
        self.assertIn("confidence: medium", out)
        self.assertNotIn("confidence: high", out)
        self.assertIn(stages._CONFIDENCE_GUARD_HEADER, out)
        self.assertIn("no device run", out)

    def test_verdict_is_untouched(self):
        p = self._verify("---\noverall_status: pass\nconfidence: high\n---\n")
        stages.downgrade_verify_confidence(self.run_dir, "note")
        self.assertEqual(stages.parse_verify_verdict(p.read_text(encoding="utf-8")), "PASS")

    def test_first_match_only(self):
        p = self._verify("confidence: high\n\nbody says confidence: high too\n")
        stages.downgrade_verify_confidence(self.run_dir, "n")
        out = p.read_text(encoding="utf-8")
        self.assertEqual(out.count("confidence: high"), 1, "only the body one remains")
        self.assertIn("confidence: medium", out)

    def test_missing_file_is_false(self):
        self.assertFalse(
            stages.downgrade_verify_confidence(self.run_dir / "gone", "n"))


# ---------------------------------------------------------------------------
# Phase 2 — the hold record module
# ---------------------------------------------------------------------------

class MergeHoldScheduleTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_round_trips_the_documented_schema(self):
        path = merge_hold.schedule(
            self.root, "task-x", held_sha=HELD_SHA, branch="worker/task-x",
            repo="git@github.com:o/r.git", pr_url="https://pr/1",
            note="UI 미확인 보류", now=NOW)
        self.assertEqual(path, merge_hold.record_path(self.root, "task-x"))
        self.assertEqual(path.parent,
                         self.root / "Worker" / "_state" / "pending_merge_holds")
        rec = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(rec["slug"], "task-x")
        self.assertEqual(rec["held_sha"], HELD_SHA)
        self.assertEqual(rec["branch"], "worker/task-x")
        self.assertEqual(rec["repo"], "git@github.com:o/r.git")
        self.assertEqual(rec["pr_url"], "https://pr/1")
        self.assertEqual(rec["created_ts"], NOW.isoformat())
        self.assertEqual(rec["expires_ts"],
                         (NOW + dt.timedelta(hours=48)).isoformat())

    def test_empty_held_sha_is_refused(self):
        with self.assertRaises(merge_hold.MergeHoldError):
            merge_hold.schedule(self.root, "task-x", held_sha="  ",
                                branch="worker/task-x", repo=None, now=NOW)

    def test_empty_slug_is_refused(self):
        with self.assertRaises(merge_hold.MergeHoldError):
            merge_hold.schedule(self.root, "  ", held_sha=HELD_SHA,
                                branch="b", repo=None, now=NOW)


class MergeHoldEvaluateTest(unittest.TestCase):
    """`evaluate` is pure — no vault, no git, no ledger."""

    def record(self, *, hours_left=1.0):
        return {"slug": "s", "held_sha": HELD_SHA,
                "created_ts": NOW.isoformat(),
                "expires_ts": (NOW + dt.timedelta(hours=hours_left)).isoformat()}

    def verdict(self, *, device_pass=True, device_pass_sha=HELD_SHA,
                state=AWAITING_HUMAN, hours_left=1.0):
        return merge_hold.evaluate(
            self.record(hours_left=hours_left), device_pass=device_pass,
            device_pass_sha=device_pass_sha, state=state, now=NOW)

    def test_pass_for_held_sha_releases(self):
        self.assertEqual(self.verdict(), merge_hold.RELEASE)

    def test_failed_state_also_releases(self):
        self.assertEqual(self.verdict(state=FAILED), merge_hold.RELEASE)

    def test_no_device_pass_waits(self):
        self.assertEqual(self.verdict(device_pass=False), merge_hold.WAIT)

    def test_different_sha_waits(self):
        self.assertEqual(self.verdict(device_pass_sha="deadbeef"), merge_hold.WAIT)

    def test_missing_sha_waits(self):
        self.assertEqual(self.verdict(device_pass_sha=None), merge_hold.WAIT)

    def test_resolved_state_is_stale(self):
        self.assertEqual(self.verdict(state=DONE), merge_hold.STALE)

    def test_expired_is_discarded_even_with_a_pass(self):
        self.assertEqual(self.verdict(hours_left=-1.0), merge_hold.EXPIRED)


class ScanDeviceEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.runs = self.root / "Worker" / "runs"
        self.runs.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, name: str, *, head_sha: str | None, device_note: str | None):
        d = self.runs / name
        d.mkdir()
        (d / "verify.md").write_text(
            "---\noverall_status: pass\n---\n", encoding="utf-8")
        if device_note is not None:
            screen_check.append_note(d, device_note)
        if head_sha is not None:
            (d / "head_sha.txt").write_text(head_sha + "\n", encoding="utf-8")
        return d

    def test_newest_app_run_with_a_pass(self):
        self._run("20261111T020000-task-x", head_sha="old",
                  device_note="화면 확인 불가(노드 오류)")
        self._run("20261111T030000-task-x", head_sha=HELD_SHA,
                  device_note=f"{screen_check.OK_NOTE_PREFIX} — screens/a.png")
        ok, sha = merge_hold.scan_device_evidence(self.root, "task-x")
        self.assertTrue(ok)
        self.assertEqual(sha, HELD_SHA)

    def test_failed_check_run_is_reported_but_not_a_pass(self):
        self._run("20261111T030000-task-x", head_sha=HELD_SHA,
                  device_note="화면 확인 불가(앱 리포 아님)")
        ok, sha = merge_hold.scan_device_evidence(self.root, "task-x")
        self.assertFalse(ok)
        self.assertEqual(sha, HELD_SHA)

    def test_run_without_head_sha_is_skipped(self):
        self._run("20261111T030000-task-x", head_sha=None,
                  device_note=f"{screen_check.OK_NOTE_PREFIX} — a.png")
        ok, sha = merge_hold.scan_device_evidence(self.root, "task-x")
        self.assertFalse(ok)
        self.assertIsNone(sha)

    def test_no_run_at_all(self):
        self.assertEqual(
            merge_hold.scan_device_evidence(self.root, "ghost"), (False, None))

    def test_after_ts_drops_older_runs(self):
        self._run("20261111T020000-task-x", head_sha=HELD_SHA,
                  device_note=f"{screen_check.OK_NOTE_PREFIX} — a.png")
        # cutoff after the only run → nothing qualifies.
        after = (NOW + dt.timedelta(hours=1)).isoformat()
        self.assertEqual(
            merge_hold.scan_device_evidence(self.root, "task-x", after_ts=after),
            (False, None))


# ---------------------------------------------------------------------------
# Phase 3 — the runner guard (decision unit + full _execute wiring)
# ---------------------------------------------------------------------------

class UiMergeHoldFiresTest(unittest.TestCase):
    """The pure-ish decision: all four conditions must hold. git/config are mocked
    so only the AND is under test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "clone"
        self.repo.mkdir()
        (self.repo / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")  # app repo
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()
        (self.run_dir / "verify.md").write_text(
            "---\noverall_status: pass\nconfidence: high\n---\n", encoding="utf-8")
        self.ex = StagedExecutor()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @contextlib.contextmanager
    def _patched(self, *, changed=("app/src/Main.kt",)):
        with mock.patch.object(StagedExecutor, "_changed_paths", return_value=list(changed)), \
             mock.patch.object(StagedExecutor, "_screen_check_config", return_value=None):
            yield

    def _fires(self, verify_text=None):
        if verify_text is not None:
            (self.run_dir / "verify.md").write_text(verify_text, encoding="utf-8")
        text = (self.run_dir / "verify.md").read_text(encoding="utf-8")
        return self.ex._ui_merge_hold_fires(
            self.repo, "git@github.com:o/r.git", {}, self.run_dir, "worker/x",
            text, lambda _m: None)

    def test_all_four_conditions_fire(self):
        with self._patched():
            self.assertTrue(self._fires())

    def test_confidence_not_high_does_not_fire(self):
        with self._patched():
            self.assertFalse(self._fires(
                "---\noverall_status: pass\nconfidence: medium\n---\n"))

    def test_non_app_repo_does_not_fire(self):
        (self.repo / "gradlew").unlink()
        with self._patched():
            self.assertFalse(self._fires())

    def test_diff_misses_app_module_does_not_fire(self):
        with self._patched(changed=("docs/readme.md",)):
            self.assertFalse(self._fires())

    def test_device_pass_present_does_not_fire(self):
        screen_check.append_note(
            self.run_dir, f"{screen_check.OK_NOTE_PREFIX} — screens/a.png")
        with self._patched():
            self.assertFalse(self._fires())

    def test_no_repo_url_does_not_fire(self):
        with self._patched():
            text = (self.run_dir / "verify.md").read_text(encoding="utf-8")
            self.assertFalse(self.ex._ui_merge_hold_fires(
                self.repo, None, {}, self.run_dir, "worker/x", text, lambda _m: None))


def _agent(**kw) -> Agent:
    base = dict(id="w", kind="ai-local", capabilities=["git"], repo="~/Worker/x")
    base.update(kw)
    return Agent(**base)


def _prepared(rc_code: int = 0) -> subprocess.CompletedProcess:
    cp = subprocess.CompletedProcess(args=["git", "checkout", "-B"], returncode=rc_code,
                                     stdout="", stderr="")
    cp.orphaned_commits = None
    return cp


class ExecuteWiringTest(unittest.TestCase):
    """AC#1/#2/#3 at the real `_execute` seam: the guard is WIRED into the verify
    block and the merge-hold path, not merely a helper that nothing calls
    (the 'parts green, wiring missing' failure this guards against)."""

    VERIFY_HIGH = "---\noverall_status: pass\nconfidence: high\n---\n\n# verify\n"
    REPORT_OK = ("---\noverall_status: pass\nmerged: no\n"
                 "pr_url: https://github.com/o/r/pull/1\n---\n")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "clone"
        self.repo.mkdir()
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        self.vault = self.tmp / "vault"
        (self.vault / "Worker").mkdir(parents=True)
        self.captured_auto_merge: dict[str, object] = {}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_run_stage(this_test):
        def run_stage(self, stage, **kw):
            this_test.captured_auto_merge[stage] = kw.get("auto_merge")
            art = Path(kw["run_dir"]) / stages.STAGE_ARTIFACT[stage]
            if stage == "verify":
                art.write_text(this_test.VERIFY_HIGH, encoding="utf-8")
            elif stage == "report":
                art.write_text(this_test.REPORT_OK, encoding="utf-8")
            else:
                art.write_text("ok\n", encoding="utf-8")
            return subprocess.CompletedProcess(args=["claude"], returncode=0,
                                               stdout="", stderr="")
        return run_stage

    @contextlib.contextmanager
    def _sandbox(self, *, app_repo=True, device_pass=False):
        if app_repo:
            (self.repo / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")

        def fake_screen_check(self, repo, payload, run_dir, progress, branch=None):
            note = (f"{screen_check.OK_NOTE_PREFIX} — screens/a.png" if device_pass
                    else "화면 확인 불가(노드 오류)")
            screen_check.append_note(run_dir, note)

        patches = [
            mock.patch.object(repos, "ensure_worktree", return_value=self.repo),
            mock.patch.object(stages, "restore_main"),
            mock.patch.object(stages, "install_push_guard"),
            mock.patch.object(stages, "default_branch", return_value="main"),
            mock.patch.object(stages, "prepare_external_branch", return_value=_prepared()),
            mock.patch.object(stages, "remove_worktree"),
            mock.patch.object(stages, "RUNS_DIR", self.runs),
            mock.patch.object(stages, "_VAULT_DATA_ROOT", self.vault),
            mock.patch.object(stages, "merge_landed", return_value=True),
            mock.patch.object(runner_mod, "_stop_build_daemons"),
            mock.patch.object(StagedExecutor, "_publish_beta", return_value=""),
            mock.patch.object(StagedExecutor, "_head_sha", return_value=HELD_SHA),
            mock.patch.object(StagedExecutor, "_changed_paths",
                              return_value=["app/src/Main.kt"]),
            mock.patch.object(StagedExecutor, "_screen_check_config", return_value=None),
            mock.patch.object(StagedExecutor, "_app_screen_check", fake_screen_check),
            mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")),
            mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
            mock.patch.object(StagedExecutor, "_pipeline_for", return_value=("verify", "report")),
            mock.patch.object(agent_backend.ClaudeAgentBackend, "run_stage",
                              self._fake_run_stage()),
            mock.patch.object(agent_backend.ClaudeAgentBackend, "report_usage", return_value=None),
            mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
        ]
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            yield

    def _run(self, *, payload_extra=None, **sandbox_kw):
        payload = {"repo": "git@github.com:o/r.git", "summary": "an app UI change"}
        payload.update(payload_extra or {})
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="task-x", payload=payload)
        lines: list[str] = []
        with self._sandbox(**sandbox_kw):
            out = StagedExecutor().execute(msg, _agent(), progress=lines.append)
        return out

    def _run_dir(self) -> Path:
        dirs = [d for d in self.runs.iterdir() if d.is_dir()]
        self.assertEqual(len(dirs), 1, f"expected one run dir, got {dirs}")
        return dirs[0]

    def test_ac1_guard_downgrades_confidence(self):
        out = self._run()
        self.assertTrue(out.ok, out.summary)
        verify = (self._run_dir() / "verify.md").read_text(encoding="utf-8")
        self.assertIn("confidence: medium", verify)
        self.assertNotIn("confidence: high", verify)
        self.assertIn(stages._CONFIDENCE_GUARD_HEADER, verify)

    def test_ac2_report_gets_auto_merge_false_and_a_hold_is_scheduled(self):
        out = self._run()
        self.assertFalse(self.captured_auto_merge["report"],
                         "report stage must be told NOT to auto-merge")
        rec_path = merge_hold.record_path(self.vault, "task-x")
        self.assertTrue(rec_path.is_file(), "a hold record must be written")
        rec = json.loads(rec_path.read_text(encoding="utf-8"))
        self.assertEqual(rec["held_sha"], HELD_SHA)
        self.assertEqual(rec["pr_url"], "https://github.com/o/r/pull/1")
        self.assertTrue(out.payload.get("ui_merge_hold"))
        self.assertTrue(out.payload.get("review_required"))

    def test_ac3_regression_app_run_with_device_pass_is_untouched(self):
        out = self._run(device_pass=True)
        self.assertTrue(out.ok, out.summary)
        verify = (self._run_dir() / "verify.md").read_text(encoding="utf-8")
        self.assertIn("confidence: high", verify, "confidence left as the stage wrote it")
        self.assertNotIn(stages._CONFIDENCE_GUARD_HEADER, verify)
        self.assertFalse(merge_hold.record_path(self.vault, "task-x").exists())
        self.assertNotIn("ui_merge_hold", out.payload)
        self.assertNotEqual(self.captured_auto_merge["report"], False,
                            "a device-passing app run auto-merges as before")

    def test_ac3_regression_non_app_repo_is_untouched(self):
        out = self._run(app_repo=False)
        self.assertTrue(out.ok, out.summary)
        verify = (self._run_dir() / "verify.md").read_text(encoding="utf-8")
        self.assertIn("confidence: high", verify)
        self.assertFalse(merge_hold.record_path(self.vault, "task-x").exists())
        self.assertNotIn("ui_merge_hold", out.payload)

    def test_ac3_regression_auto_merge_false_task_is_not_held(self):
        # A task the user set auto_merge:false already follows the review path;
        # the guard must not hold it (or reconcile_merge_holds would later merge a
        # PR the user explicitly gated).
        out = self._run(payload_extra={"auto_merge": False})
        self.assertTrue(out.ok, out.summary)
        verify = (self._run_dir() / "verify.md").read_text(encoding="utf-8")
        self.assertIn("confidence: high", verify)
        self.assertNotIn(stages._CONFIDENCE_GUARD_HEADER, verify)
        self.assertFalse(merge_hold.record_path(self.vault, "task-x").exists())
        self.assertNotIn("ui_merge_hold", out.payload)


# ---------------------------------------------------------------------------
# Phase 4 — release tick + one-time alert (orchestrator)
# ---------------------------------------------------------------------------

class ReconcileMergeHoldsTest(unittest.TestCase):
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

        def route(text, *, level=None, event_key=None, cooldown_s=0.0):
            self.routed.append((text, level, event_key))
            return "send", text

        self.orch = Orchestrator(
            FileMailbox(self.root / "Worker" / "mb" / "orchestrator"),
            Registry([]), self.ledger,
            notify=lambda text, link=None: None,
            alert_route=route,
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _seed(self, slug="task-x", *, state=AWAITING_HUMAN):
        (self.root / "Todo" / "items" / f"{slug}.md").write_text(
            f"---\nstatus: open\n---\n\n# {slug}\n", encoding="utf-8")
        a = self._Assignment(task_id=slug, agent_id="ai-1", corr_id="c1",
                             kind="ai-local", state=state)
        self.ledger.save(a)
        return a

    def _hold(self, slug="task-x", *, now=None):
        return merge_hold.schedule(
            self.root, slug, held_sha=HELD_SHA, branch=f"worker/{slug}",
            repo="git@github.com:o/r.git", pr_url="https://pr/1",
            note="UI 미확인", now=now)

    def _run(self, slug="task-x", *, name="20261111T040000", sha=HELD_SHA,
             device_pass=True):
        d = self.root / "Worker" / "runs" / f"{name}-{slug}"
        d.mkdir()
        (d / "verify.md").write_text("---\noverall_status: pass\n---\n",
                                     encoding="utf-8")
        note = (f"{screen_check.OK_NOTE_PREFIX} — screens/a.png" if device_pass
                else "화면 확인 불가(노드 오류)")
        screen_check.append_note(d, note)
        if sha is not None:
            (d / "head_sha.txt").write_text(sha + "\n", encoding="utf-8")
        return d

    def test_release_merges_and_closes_on_same_sha_pass(self):
        self._seed()
        self._hold()
        self._run()
        with mock.patch.object(stages, "merge_pr",
                               return_value={"merged": True, "note": "ok"}) as mp:
            released = self.orch.reconcile_merge_holds()
        self.assertEqual(released, ["task-x"])
        mp.assert_called_once()
        self.assertEqual(self.ledger.load("task-x").state, DONE)
        self.assertFalse(merge_hold.record_path(self.root, "task-x").exists())

    def test_different_sha_waits_without_merging(self):
        self._seed()
        self._hold()
        self._run(sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
        with mock.patch.object(stages, "merge_pr") as mp:
            released = self.orch.reconcile_merge_holds()
        self.assertEqual(released, [])
        mp.assert_not_called()
        self.assertTrue(merge_hold.record_path(self.root, "task-x").exists())
        self.assertEqual(self.ledger.load("task-x").state, AWAITING_HUMAN)

    def test_no_device_pass_waits(self):
        self._seed()
        self._hold()
        self._run(device_pass=False)
        with mock.patch.object(stages, "merge_pr") as mp:
            self.assertEqual(self.orch.reconcile_merge_holds(), [])
        mp.assert_not_called()
        self.assertTrue(merge_hold.record_path(self.root, "task-x").exists())

    def test_unconfirmed_merge_keeps_the_hold(self):
        self._seed()
        self._hold()
        self._run()
        with mock.patch.object(stages, "merge_pr",
                               return_value={"merged": False, "note": "no perm"}):
            self.assertEqual(self.orch.reconcile_merge_holds(), [])
        self.assertTrue(merge_hold.record_path(self.root, "task-x").exists(),
                        "an unconfirmed merge never closes on an open PR")
        self.assertEqual(self.ledger.load("task-x").state, AWAITING_HUMAN)

    def test_expired_discards_the_record_and_alerts(self):
        self._seed()
        self._hold(now=merge_hold._now() - dt.timedelta(hours=72))  # expires_ts in the past
        with mock.patch.object(stages, "merge_pr") as mp:
            self.assertEqual(self.orch.reconcile_merge_holds(), [])
        mp.assert_not_called()
        self.assertFalse(merge_hold.record_path(self.root, "task-x").exists())
        keys = [k for _, _, k in self.routed]
        self.assertIn("ui-merge-hold-expired:task-x", keys)

    def test_never_raises_into_the_tick(self):
        self._seed()
        self._hold()
        self._run()
        with mock.patch.object(merge_hold, "load_records",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(self.orch.reconcile_merge_holds(), [])


class NotifyUiMergeHoldTest(unittest.TestCase):
    def setUp(self):
        from todo_system.orchestrator.assignment import Assignment, Ledger
        from todo_system.orchestrator.mailbox import FileMailbox
        from todo_system.orchestrator.orchestrator import (
            ALERT_UI_MERGE_HOLD, Orchestrator)
        from todo_system.orchestrator.registry import Registry
        from todo_system import kakao_format
        self._Assignment = Assignment
        self._ALERT = ALERT_UI_MERGE_HOLD
        self._LEVEL_IMMEDIATE = kakao_format.LEVEL_IMMEDIATE
        self.root = Path(tempfile.mkdtemp())
        (self.root / "Worker" / "assignments").mkdir(parents=True)
        self.ledger = Ledger(self.root / "Worker" / "assignments")
        self.routed: list[tuple[str, object, object]] = []
        self.links: list[object] = []

        def route(text, *, level=None, event_key=None, cooldown_s=0.0):
            self.routed.append((text, level, event_key))
            return "send", text

        self.orch = Orchestrator(
            FileMailbox(self.root / "Worker" / "mb" / "orchestrator"),
            Registry([]), self.ledger,
            notify=lambda text, link=None: self.links.append(link),
            alert_route=route,
        )

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_dedicated_alert_is_immediate_keyed_and_links_the_pr(self):
        a = self._Assignment(task_id="task-x", agent_id="ai-1", corr_id="c1",
                             kind="ai-local", state=AWAITING_HUMAN)
        msg = m.Envelope(to="o", type=m.RESULT, task_id="task-x",
                         payload={"ui_merge_hold": True, "pr_url": "https://pr/1"})
        self.orch._notify_ui_merge_hold(a, msg)
        self.assertEqual(a.last_alert_kind, self._ALERT)
        self.assertEqual(len(self.routed), 1)
        _text, level, key = self.routed[0]
        self.assertEqual(level, self._LEVEL_IMMEDIATE)
        self.assertEqual(key, "ui-merge-hold:task-x")
        self.assertEqual(self.links, ["https://pr/1"])


if __name__ == "__main__":
    unittest.main()
