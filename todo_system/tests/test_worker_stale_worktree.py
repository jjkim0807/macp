"""`stale_worktree_actions` reports merged, clean `wt-*` worktrees — and removes nothing.

Live runs execute in a linked worktree at `<worker root>/wt-<slug>`; nothing
cleans them up when the PR merges, so they pile up (22 on the host that
prompted this). The reporter names the ones that look safe to drop, and stops
there — removal is a human call, because a worktree can hold the only copy of
uncommitted work.

Three things these tests exist to stop.

**The vacuous pass.** Worker PRs close with `gh pr merge --squash`, so a merged
branch's tip is *not* an ancestor of main. A `merge-base --is-ancestor`-only
implementation passes a test built on a real merge commit while reporting
almost nothing in production — measured on the real vault, signal A alone found
1 of the 8 actual candidates and `merge-tree --write-tree` found the other 7.
`test_squash_merged_worktree_is_reported` is the one that fails if signal B is
dropped.

**The all-empty pass.** A helper that returned `[]` unconditionally would
satisfy every "not reported" assertion here. So each exclusion test also plants
a clean merged worktree as a positive control and asserts *exactly* it comes
back.

**The over-suppressed scan.** `STALE_WORKTREE_BUDGET_S` stops a scan that would
otherwise outlast the 5-minute maintenance tick, and a budget that trips too
early reports nothing while every budget assertion still passes.
`test_default_budget_does_not_suppress_a_fast_scan` is the control that fails if
the deadline fires on an ordinary host.

Everything runs against a throwaway repo whose layout mirrors production —
the main checkout sits *inside* the worktree parent (`~/Worker/MaestroACP`
next to `~/Worker/wt-*`), so `test_main_checkout_is_never_reported` exercises
the real reason it is excluded: the `wt-` prefix, not its parent directory.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system import worker

# `-c` overrides rather than a configured repo: CI hosts have no global git
# identity, and a developer host may have commit signing on, which would make
# every commit below fail for reasons that have nothing to do with the feature.
_GIT_IDENT = (
    "-c", "user.email=worker@test.invalid",
    "-c", "user.name=worker-test",
    "-c", "commit.gpgsign=false",
)
_TIMEOUT = 30

_VERSION_OUT = subprocess.run(
    ["git", "--version"], capture_output=True, text=True, timeout=_TIMEOUT
).stdout
_GIT_VERSION = tuple(int(n) for n in re.findall(r"\d+", _VERSION_OUT)[:3])
# `git merge-tree --write-tree` landed in 2.38; on anything older only the
# ancestor signal is available and the squash case genuinely cannot work.
_HAS_WRITE_TREE = _GIT_VERSION >= (2, 38)


class _Fixture:
    """A tiny git repo plus a worktree parent, laid out like the real host."""

    def __init__(self, tmp: Path) -> None:
        self.worker_root = tmp / "worker"
        self.repo = self.worker_root / "MaestroACP"
        self.repo.mkdir(parents=True)
        self.git("-c", "init.defaultBranch=main", "init", "-q")
        self.commit("base.txt", "base\n", "C0")

    def git(self, *args: str) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            ["git", "-C", str(self.repo), *_GIT_IDENT, *args],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
        assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
        return proc

    def commit(self, rel: str, content: str, message: str) -> None:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.git("add", rel)
        self.git("commit", "-q", "-m", message)

    def merged_branch(self, slug: str) -> str:
        """A branch whose commit is merged into main by a real merge commit."""
        branch = f"worker/{slug}"
        self.git("checkout", "-q", "-b", branch)
        self.commit(f"{slug}.txt", f"{slug}\n", f"work on {slug}")
        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--no-ff", "-m", f"merge {slug}", branch)
        return branch

    def squashed_branch(self, slug: str) -> str:
        """A branch whose *content* reached main as an unrelated commit.

        This is what `gh pr merge --squash` leaves behind: the branch tip is not
        an ancestor of main, but the branch contributes nothing main lacks.
        """
        branch = f"worker/{slug}"
        self.git("checkout", "-q", "-b", branch)
        self.commit(f"{slug}.txt", f"{slug}\n", f"work on {slug} (1/2)")
        self.commit(f"{slug}-more.txt", f"{slug} more\n", f"work on {slug} (2/2)")
        self.git("checkout", "-q", "main")
        self.git("checkout", branch, "--", f"{slug}.txt", f"{slug}-more.txt")
        self.git("commit", "-q", "-m", f"squashed {slug} (#1)")
        return branch

    def unmerged_branch(self, slug: str) -> str:
        branch = f"worker/{slug}"
        self.git("checkout", "-q", "-b", branch)
        self.commit(f"{slug}.txt", f"{slug}\n", f"unfinished {slug}")
        self.git("checkout", "-q", "main")
        return branch

    def add_worktree(self, slug: str, branch: str, *extra: str) -> Path:
        path = self.worker_root / f"wt-{slug}"
        self.git("worktree", "add", "-q", *extra, str(path), branch)
        return path

    def reported_path(self, slug: str) -> str:
        """The path git itself prints for `wt-<slug>`.

        Read out of porcelain rather than rebuilt from `self.worker_root`: git
        reports the *real* path, and on macOS a temp dir arrives as
        `/var/folders/...` but comes back as `/private/var/folders/...`.
        """
        listing = self.git("worktree", "list", "--porcelain").stdout
        for record in worker._parse_worktree_porcelain(listing):
            path = record.get("worktree", "")
            if path and Path(path).name == f"wt-{slug}":
                return path
        raise AssertionError(f"wt-{slug} missing from worktree list")

    def expected_action(self, slug: str) -> str:
        path = self.reported_path(slug)
        return f"정리 후보: {path} (git worktree remove {path})"

    def actions(self) -> list[str]:
        return worker.stale_worktree_actions(
            repo_root=self.repo, worktree_parent=self.worker_root
        )


class _StepClock:
    """A `worker._monotonic` stand-in that advances a fixed step per call.

    `worker.time.monotonic` is deliberately *not* what gets patched:
    `subprocess.run(timeout=…)` reads that same function, so a fake clock there
    turns the real git calls into bogus `TimeoutExpired`, the fail-open path
    swallows them, and the test passes on an empty result having proven nothing.
    The module-level `_monotonic` alias exists as the narrow seam instead.
    """

    def __init__(self, step: float) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        value = self.now
        self.now += self.step
        return value


def _clock_tripping_on_record(index: int) -> _StepClock:
    """A clock whose budget runs out exactly as record `index` (0-based) is entered.

    `stale_worktree_actions` reads the clock once to anchor the deadline and then
    once per record on loop entry, so the step size alone decides where the scan
    breaks — the test never has to know how many git subprocesses a record costs.
    Reading n (1-based) is `n * step`; with `step = BUDGET / (index + 0.5)` that
    is under the budget for every earlier record and over it at `index`.
    """
    return _StepClock(worker.STALE_WORKTREE_BUDGET_S / (index + 0.5))


class StaleWorktreeReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fx = _Fixture(Path(self._tmp.name))

    def test_merged_clean_worktree_is_reported(self):
        """Pins the `if_then` string verbatim: name plus the removal command."""
        branch = self.fx.merged_branch("done-task")
        self.fx.add_worktree("done-task", branch)

        self.assertEqual(self.fx.actions(), [self.fx.expected_action("done-task")])

    @unittest.skipUnless(_HAS_WRITE_TREE, f"git {_GIT_VERSION} has no merge-tree --write-tree")
    def test_squash_merged_worktree_is_reported(self):
        """The case that actually occurs in production, and that signal A misses."""
        branch = self.fx.squashed_branch("squashed-task")
        self.fx.add_worktree("squashed-task", branch)

        # Guard the premise: if this branch were an ancestor of main the test
        # would pass on signal A alone and prove nothing about squashes.
        ancestor = subprocess.run(
            ["git", "-C", str(self.fx.repo), "merge-base", "--is-ancestor", branch, "main"],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
        self.assertNotEqual(ancestor.returncode, 0, "premise broken: branch is an ancestor")

        self.assertEqual(self.fx.actions(), [self.fx.expected_action("squashed-task")])

    def test_dirty_worktree_is_not_reported(self):
        """Uncommitted work outranks a merged branch — it may be the only copy."""
        self.fx.add_worktree("clean-task", self.fx.merged_branch("clean-task"))
        dirty = self.fx.add_worktree("dirty-task", self.fx.merged_branch("dirty-task"))
        (dirty / "scratch.txt").write_text("unsaved\n", encoding="utf-8")

        self.assertEqual(self.fx.actions(), [self.fx.expected_action("clean-task")])

    def test_worktree_with_modified_tracked_file_is_not_reported(self):
        self.fx.add_worktree("clean-task", self.fx.merged_branch("clean-task"))
        modified = self.fx.add_worktree("edited-task", self.fx.merged_branch("edited-task"))
        (modified / "base.txt").write_text("locally edited\n", encoding="utf-8")

        self.assertEqual(self.fx.actions(), [self.fx.expected_action("clean-task")])

    def test_unmerged_worktree_is_not_reported(self):
        self.fx.add_worktree("clean-task", self.fx.merged_branch("clean-task"))
        self.fx.add_worktree("wip-task", self.fx.unmerged_branch("wip-task"))

        self.assertEqual(self.fx.actions(), [self.fx.expected_action("clean-task")])

    def test_detached_worktree_is_not_reported(self):
        """No branch to judge "merged" against, so it can never be a candidate."""
        self.fx.add_worktree("clean-task", self.fx.merged_branch("clean-task"))
        path = self.fx.worker_root / "wt-detached-task"
        self.fx.git("worktree", "add", "-q", "--detach", str(path), "main")

        self.assertEqual(self.fx.actions(), [self.fx.expected_action("clean-task")])

    def test_locked_worktree_is_not_reported(self):
        """`locked` means a human deliberately pinned it; do not suggest removal."""
        self.fx.add_worktree("clean-task", self.fx.merged_branch("clean-task"))
        locked = self.fx.add_worktree("locked-task", self.fx.merged_branch("locked-task"))
        self.fx.git("worktree", "lock", str(locked))

        self.assertEqual(self.fx.actions(), [self.fx.expected_action("clean-task")])

    def test_main_checkout_is_never_reported(self):
        """It sits in the worktree parent too — only the `wt-` prefix excludes it."""
        self.fx.add_worktree("clean-task", self.fx.merged_branch("clean-task"))

        self.assertEqual(self.fx.repo.parent, self.fx.worker_root)
        actions = self.fx.actions()
        self.assertEqual(actions, [self.fx.expected_action("clean-task")])
        self.assertNotIn(str(self.fx.repo), " ".join(actions))

    def test_non_wt_sibling_worktree_is_not_reported(self):
        """A linked worktree that is not a worker run stays out of scope."""
        self.fx.add_worktree("clean-task", self.fx.merged_branch("clean-task"))
        sibling = self.fx.worker_root / "scratch-checkout"
        self.fx.git("worktree", "add", "-q", str(sibling), self.fx.merged_branch("scratch"))

        self.assertEqual(self.fx.actions(), [self.fx.expected_action("clean-task")])

    def test_worktree_outside_the_worker_root_is_not_reported(self):
        """Scope is `<worker root>/wt-*`, not "any wt-* anywhere"."""
        self.fx.add_worktree("clean-task", self.fx.merged_branch("clean-task"))
        elsewhere = self.fx.worker_root.parent / "elsewhere" / "wt-stray-task"
        elsewhere.parent.mkdir(parents=True, exist_ok=True)
        self.fx.git("worktree", "add", "-q", str(elsewhere),
                    self.fx.merged_branch("stray-task"))

        self.assertEqual(self.fx.actions(), [self.fx.expected_action("clean-task")])

    def test_overflow_is_summarised(self):
        """22 candidates must not mean 22 log lines every 5-minute tick."""
        count = worker.MAX_STALE_WORKTREE_ACTIONS + 2
        for i in range(count):
            self.fx.add_worktree(f"task-{i}", self.fx.merged_branch(f"task-{i}"))

        actions = self.fx.actions()

        self.assertEqual(len(actions), worker.MAX_STALE_WORKTREE_ACTIONS + 1)
        for line in actions[:worker.MAX_STALE_WORKTREE_ACTIONS]:
            self.assertRegex(line, r"^정리 후보: .+ \(git worktree remove .+\)$")
        self.assertEqual(
            actions[-1],
            "정리 후보 2개 더 있음 (git worktree list --porcelain으로 확인)",
        )

    def test_unscanned_worktrees_are_declared_not_silently_dropped(self):
        """Truncating the scan must say so, or it reads as "nothing to clean"."""
        self.fx.add_worktree("clean-task", self.fx.merged_branch("clean-task"))

        with mock.patch.object(worker, "MAX_SCANNED_WORKTREES", 1):
            actions = self.fx.actions()

        # Record 1 of 2 is the main checkout, so nothing is reported — but the
        # skipped worktree is still announced.
        self.assertEqual(actions, ["worktree 1개는 검사하지 않음 (상한 1개)"])

    def test_time_budget_breaks_scan_and_is_declared(self):
        """The count cap bounds records, not seconds; this bounds seconds.

        50 records x 4 git subprocesses x the 10s per-call timeout is ~33
        minutes, and `except Exception` fails open on a git that *breaks*, never
        on one that *crawls* — so without a deadline the pass that runs it
        can be swallowed whole.
        """
        for slug in ("task-a", "task-b", "task-c"):
            self.fx.add_worktree(slug, self.fx.merged_branch(slug))
        expected = {self.fx.expected_action(s) for s in ("task-a", "task-b", "task-c")}

        # Control: on a real clock this same fixture reports all three, so the
        # single candidate below is the break, not a fixture that found nothing.
        self.assertEqual(set(self.fx.actions()), expected)

        # Records are [main checkout, wt-*, wt-*, wt-*]; trip entering record 2,
        # i.e. after exactly one candidate has been confirmed.
        with mock.patch.object(worker, "_monotonic", _clock_tripping_on_record(2)):
            actions = self.fx.actions()

        # Work already done survives the break — a truncated report still names
        # what it found, and it is the *rest* that gets declared unscanned.
        self.assertEqual(len(actions), 2)
        self.assertIn(actions[0], expected)
        self.assertEqual(
            actions[-1],
            f"worktree 2개는 검사하지 않음 (시간 예산 {worker.STALE_WORKTREE_BUDGET_S}초 초과)",
        )
        self.assertNotIn("상한", actions[-1], "count cap did not fire; do not blame it")

    def test_cap_and_budget_causes_are_summed_in_one_line(self):
        """Both limits firing stays one line — summed count, both causes named.

        A second line per cause would defeat the reason the cap exists (log
        volume on the 5-minute tick), and a single cause would misdirect the fix:
        raising `MAX_SCANNED_WORKTREES` does nothing about a budget overrun.
        """
        for slug in ("task-a", "task-b", "task-c"):
            self.fx.add_worktree(slug, self.fx.merged_branch(slug))

        # 4 records, 2 scanned → 2 over the cap; trip entering record 1 → 1 more
        # of the scanned slice left untouched.
        with mock.patch.object(worker, "MAX_SCANNED_WORKTREES", 2), \
                mock.patch.object(worker, "_monotonic", _clock_tripping_on_record(1)):
            actions = self.fx.actions()

        self.assertEqual(
            actions,
            [f"worktree 3개는 검사하지 않음 "
             f"(상한 2개 + 시간 예산 {worker.STALE_WORKTREE_BUDGET_S}초 초과)"],
        )
        self.assertEqual(len([a for a in actions if "검사하지 않음" in a]), 1)

    def test_default_budget_does_not_suppress_a_fast_scan(self):
        """Over-suppression control: an ordinary scan finishes inside the budget.

        Without this, a deadline of 0 — or one anchored at the wrong moment —
        would pass every assertion above while reporting nothing in production.
        """
        self.fx.add_worktree("task-a", self.fx.merged_branch("task-a"))
        self.fx.add_worktree("task-b", self.fx.merged_branch("task-b"))

        actions = self.fx.actions()  # real clock, unpatched constants

        self.assertNotIn("검사하지 않음", "\n".join(actions))
        self.assertEqual(
            sorted(actions),
            sorted([self.fx.expected_action("task-a"), self.fx.expected_action("task-b")]),
        )


class ReadOnlyGuaranteeTest(unittest.TestCase):
    """The acceptance criterion is explicitly "reports, does not remove"."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fx = _Fixture(Path(self._tmp.name))

    def test_no_mutating_git_command_is_ever_issued(self):
        self.fx.add_worktree("clean-task", self.fx.merged_branch("clean-task"))
        self.fx.add_worktree("wip-task", self.fx.unmerged_branch("wip-task"))

        issued: list[list[str]] = []
        real_run = subprocess.run

        def spy(cmd, *args, **kwargs):
            issued.append(list(cmd))
            return real_run(cmd, *args, **kwargs)

        with mock.patch("todo_system.worker.subprocess.run", side_effect=spy):
            self.fx.actions()

        self.assertTrue(issued, "helper issued no git commands at all")
        # `merge-tree --write-tree` writes unreachable objects and no ref, index
        # or working tree; everything else is a plain read.
        allowed = {"worktree", "rev-parse", "merge-base", "merge-tree", "status"}
        for cmd in issued:
            self.assertEqual(cmd[:2], ["git", "-C"], f"non-git command issued: {cmd}")
            self.assertIn(cmd[3], allowed, f"unexpected git verb: {cmd}")
            self.assertNotIn("remove", cmd, f"mutating command issued: {cmd}")
            self.assertNotIn("prune", cmd, f"mutating command issued: {cmd}")
            self.assertNotIn("-d", cmd, f"mutating command issued: {cmd}")
            self.assertNotIn("-D", cmd, f"mutating command issued: {cmd}")
        self.assertEqual([c for c in issued if c[3] == "worktree"][0][4], "list")

    def test_every_worktree_survives_the_report(self):
        clean = self.fx.add_worktree("clean-task", self.fx.merged_branch("clean-task"))
        before = self.fx.git("worktree", "list", "--porcelain").stdout

        self.assertEqual(self.fx.actions(), [self.fx.expected_action("clean-task")])

        self.assertTrue(clean.is_dir(), "a reported worktree was removed")
        self.assertEqual(self.fx.git("worktree", "list", "--porcelain").stdout, before)


class FailOpenTest(unittest.TestCase):
    """The caller must survive a broken git."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_non_repo_returns_empty(self):
        self.assertEqual(
            worker.stale_worktree_actions(
                repo_root=self.tmp, worktree_parent=self.tmp / "worker"
            ),
            [],
        )

    def test_timeout_returns_empty(self):
        fx = _Fixture(self.tmp)
        fx.add_worktree("clean-task", fx.merged_branch("clean-task"))
        # Control: this same fixture reports a candidate when git behaves, so
        # the [] below is the timeout being swallowed, not an empty fixture.
        self.assertEqual(len(fx.actions()), 1)

        with mock.patch(
            "todo_system.worker.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=1),
        ):
            self.assertEqual(fx.actions(), [])

    def test_missing_git_binary_returns_empty(self):
        fx = _Fixture(self.tmp)
        fx.add_worktree("clean-task", fx.merged_branch("clean-task"))

        with mock.patch(
            "todo_system.worker.subprocess.run",
            side_effect=FileNotFoundError("git"),
        ):
            self.assertEqual(fx.actions(), [])

    def test_repo_without_a_main_branch_returns_empty(self):
        """No base ref to judge against — report nothing rather than guess."""
        fx = _Fixture(self.tmp)
        fx.add_worktree("clean-task", fx.merged_branch("clean-task"))
        fx.git("branch", "-m", "main", "trunk")

        self.assertEqual(fx.actions(), [])


class PorcelainParserTest(unittest.TestCase):
    """`_parse_worktree_porcelain` is a pure function — pin the record shapes."""

    SAMPLE = (
        "worktree /repo\n"
        "HEAD abc\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /w/wt-a\n"
        "HEAD def\n"
        "detached\n"
        "\n"
        "worktree /w/wt-b\n"
        "HEAD 111\n"
        "branch refs/heads/worker/b\n"
        "locked worktree is pinned by hand\n"
        "\n"
        "worktree /w/wt-c\n"
        "HEAD 222\n"
        "branch refs/heads/worker/c\n"
        "prunable gitdir file points to non-existent location\n"
        "\n"
        "worktree /bare\n"
        "bare\n"
    )

    def test_records_are_split_on_blank_lines(self):
        records = worker._parse_worktree_porcelain(self.SAMPLE)
        self.assertEqual(
            [r["worktree"] for r in records],
            ["/repo", "/w/wt-a", "/w/wt-b", "/w/wt-c", "/bare"],
        )

    def test_bare_flags_and_reasoned_flags_are_both_keys(self):
        """`locked` arrives bare *or* with a reason; presence is the test."""
        records = worker._parse_worktree_porcelain(self.SAMPLE)
        self.assertEqual(records[1]["detached"], "")
        self.assertEqual(records[2]["locked"], "worktree is pinned by hand")
        self.assertIn("prunable", records[3])
        self.assertEqual(records[4]["bare"], "")

    def test_trailing_record_without_a_blank_line_is_kept(self):
        records = worker._parse_worktree_porcelain("worktree /only\nHEAD abc\n")
        self.assertEqual(records, [{"worktree": "/only", "HEAD": "abc"}])

    def test_empty_input_is_no_records(self):
        self.assertEqual(worker._parse_worktree_porcelain(""), [])


if __name__ == "__main__":
    unittest.main()
