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

from todo_system.orchestrator import screen_check, stages

NOW = dt.datetime(2026, 11, 11, 3, 0, 0, tzinfo=dt.timezone.utc)


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


if __name__ == "__main__":
    unittest.main()
