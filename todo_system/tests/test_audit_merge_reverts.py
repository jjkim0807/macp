"""The merge-revert audit has to find a real dropped merge and nothing else.

An audit that over-reports is worse than none: every ordinary conflict
resolution picks one side over the other, and if those were flagged the real
losses would drown. So these build actual git repositories — the thing being
judged is what a merge commit's tree contains, which mocks cannot show — and
cover both directions: the loss shapes are found, the legitimate shapes are not.

Driven through the CLI with `python3 -I`, the way it is meant to be run, so the
exit-code contract (0 clean / 1 losses found) is covered too.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_merge_reverts.py"


def _git(repo: Path, *args: str, when: str | None = None):
    env = dict(os.environ)
    if when:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, env=env)
    return proc


def _init(repo: Path) -> None:
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _commit(repo: Path, message: str, when: str | None = None) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message, when=when)


class AuditMergeRevertsTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        _init(self.repo)
        # base: the file the audit will judge, plus one the other side moves so
        # the merge below cannot fast-forward.
        _write(self.repo, "items/task.md", "base content\n")
        _write(self.repo, "other.txt", "o0\n")
        _commit(self.repo, "base")
        _git(self.repo, "branch", "side")
        # own side moves an unrelated file -> the branches have really diverged
        _write(self.repo, "other.txt", "o1\n")
        _commit(self.repo, "own side moves on")

    def _side_changes(self, *, add_file: bool = False) -> None:
        """On `side`: rewrite the judged file (and optionally add a new one)."""
        _git(self.repo, "checkout", "-q", "side")
        _write(self.repo, "items/task.md", "base content\nincoming line\n")
        if add_file:
            _write(self.repo, "items/added.md", "brand new\n")
        _commit(self.repo, "incoming change")
        _git(self.repo, "checkout", "-q", "main")

    def _audit(self, *extra: str):
        proc = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--repo", str(self.repo), *extra],
            capture_output=True, text=True)
        return proc

    # ---- shapes that ARE losses -------------------------------------------

    def test_a_merge_that_reverts_the_incoming_file_is_reported(self):
        """The incident shape: the merge lands, then stale working-tree bytes are
        committed as its result, so the merge's tree equals its own side."""
        self._side_changes()
        _git(self.repo, "merge", "--no-commit", "--no-ff", "side")
        _git(self.repo, "checkout", "HEAD", "--", "items/task.md")  # revert it
        _commit(self.repo, "chore: sync vault state (2 files)")

        proc = self._audit()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("merges=1 losses=1 files=1", proc.stdout)
        self.assertIn("items/task.md", proc.stdout)

    def test_a_dropped_addition_counts_too(self):
        """Two files lost in one merge — a modification reverted and an addition
        dropped — which is the size the live incident had."""
        self._side_changes(add_file=True)
        _git(self.repo, "merge", "--no-commit", "--no-ff", "side")
        _git(self.repo, "checkout", "HEAD", "--", "items/task.md")
        _git(self.repo, "rm", "-q", "-f", "items/added.md")
        _commit(self.repo, "chore: sync vault state (2 files)")

        proc = self._audit()
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("merges=1 losses=1 files=2", proc.stdout)
        self.assertIn("items/added.md", proc.stdout)

    def test_the_loss_is_invisible_to_git_show_stat(self):
        """Why this audit exists at all: the combined diff a merge prints omits
        files matching a parent, which is exactly the damaged ones."""
        self._side_changes()
        _git(self.repo, "merge", "--no-commit", "--no-ff", "side")
        _git(self.repo, "checkout", "HEAD", "--", "items/task.md")
        _commit(self.repo, "chore: sync vault state (2 files)")

        shown = _git(self.repo, "show", "--stat", "HEAD").stdout
        self.assertNotIn("items/task.md", shown)          # the trap
        self.assertIn("items/task.md", self._audit().stdout)

    # ---- shapes that are NOT losses ---------------------------------------

    def test_an_ordinary_merge_reports_nothing(self):
        self._side_changes()
        _git(self.repo, "merge", "--no-edit", "--no-ff", "-q", "side")

        proc = self._audit()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("merges=1 losses=0 files=0", proc.stdout)

    def test_a_real_conflict_resolution_is_not_a_loss(self):
        """Both sides changed the same file and a human picked one. That is a
        decision, not a dropped merge — flagging it would make the audit useless.
        """
        _git(self.repo, "checkout", "-q", "side")
        _write(self.repo, "items/task.md", "incoming version\n")
        _commit(self.repo, "incoming edit")
        _git(self.repo, "checkout", "-q", "main")
        _write(self.repo, "items/task.md", "our version\n")
        _commit(self.repo, "our edit")
        self.assertNotEqual(
            _git(self.repo, "merge", "--no-edit", "--no-ff", "side").returncode, 0)
        _git(self.repo, "checkout", "--ours", "--", "items/task.md")
        _commit(self.repo, "resolve: keep ours")

        proc = self._audit()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("losses=0", proc.stdout)

    def test_a_repo_with_no_merges_is_clean(self):
        proc = self._audit()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("merges=0 losses=0 files=0", proc.stdout)

    # ---- range and path filters -------------------------------------------

    def test_since_actually_cuts_the_range(self):
        self._side_changes()
        _git(self.repo, "merge", "--no-commit", "--no-ff", "side")
        _git(self.repo, "checkout", "HEAD", "--", "items/task.md")
        _commit(self.repo, "chore: sync vault state (2 files)",
                when="2020-01-02T00:00:00+00:00")

        found = self._audit("--since", "2020-01-01")
        self.assertEqual(found.returncode, 1, found.stderr)
        self.assertIn("losses=1", found.stdout)

        cut = self._audit("--since", "2021-01-01")
        self.assertEqual(cut.returncode, 0, cut.stderr)
        self.assertIn("merges=0 losses=0 files=0", cut.stdout)

    def test_path_filter_restricts_what_is_judged(self):
        self._side_changes()
        _git(self.repo, "merge", "--no-commit", "--no-ff", "side")
        _git(self.repo, "checkout", "HEAD", "--", "items/task.md")
        _commit(self.repo, "chore: sync vault state (2 files)")

        inside = self._audit("--path", "items")
        self.assertEqual(inside.returncode, 1, inside.stderr)
        self.assertIn("losses=1", inside.stdout)

        outside = self._audit("--path", "nowhere")
        self.assertEqual(outside.returncode, 0, outside.stderr)
        self.assertIn("losses=0", outside.stdout)

    def test_a_bad_repo_path_is_a_usage_error_not_a_clean_bill(self):
        proc = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--repo", str(self.repo / "nope")],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("losses=0", proc.stdout)


if __name__ == "__main__":
    unittest.main()
