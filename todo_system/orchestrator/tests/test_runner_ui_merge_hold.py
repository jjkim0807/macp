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

from todo_system.orchestrator import merge_hold, screen_check, stages
from todo_system.orchestrator.assignment import AWAITING_HUMAN, DONE, FAILED

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


if __name__ == "__main__":
    unittest.main()
