"""Tests for the superseded verdict (orchestrator/superseded.py).

Two layers, like test_merge_state.py: the pure functions are driven with
already-collected values, and `branch_landed` is driven against a real bare
origin + two clones under a tempdir (`origin` is a local path, so every fetch
works offline). The real-git half exists because the pure half cannot show that
the collector actually sees a squash merge.

Every negative case sits beside a positive one built from the same fixture: a
"not superseded" that only holds because nothing was measured would pass just
as green.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import repos
from todo_system.orchestrator import superseded as sup

BRANCH = "worker/t"

# Captured at import: some tests below patch `subprocess.run` globally to prove
# the pure functions never reach it, and the fixture must keep working then.
_REAL_RUN = subprocess.run


def _git(cwd, *args):
    """Drive the fixture repos directly rather than through `sup._git`, so a bug
    in the module under test cannot quietly build a broken fixture and pass."""
    return _REAL_RUN(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _lines(**changed) -> str:
    """Nine numbered lines; `_lines(l2="x")` replaces line 2. Far-apart lines
    matter: git treats edits to adjacent lines as one conflicting hunk."""
    rows = [f"line{i}" for i in range(1, 10)]
    for key, text in changed.items():
        rows[int(key[1:]) - 1] = text
    return "\n".join(rows) + "\n"


def _md(status: str | None) -> str:
    rows = ["---", "tags:", "  - todo"]
    if status is not None:
        rows.append(f"status: {status}")
    rows += ["---", "", "# t", "", "- [ ] step", ""]
    return "\n".join(rows)


class WireFormatTest(unittest.TestCase):
    def test_wire_strings_are_pinned(self):
        # These land in RESULT payloads and the git-tracked assignment JSON.
        self.assertEqual(sup.RESULT_KEY, "superseded")
        self.assertEqual(sup.HISTORY_TYPE, "superseded")
        self.assertEqual(sup.HISTORY_UNCONFIRMED, "superseded-unconfirmed")
        self.assertEqual(sup.REASON_MD_CLOSED, "md-closed")
        self.assertEqual(sup.REASON_MERGED, "merged")

    def test_module_imports_only_leaf_siblings(self):
        # `tasks` imports `orchestrator.orchestrator`, which would import this
        # module back; `stages` is what the _execute suites mock. Neither may be
        # imported here (statuses.py docstring has the cycle).
        tree = ast.parse(Path(sup.__file__).read_text(encoding="utf-8"))
        relative = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                if node.module:
                    relative.add(node.module)
                else:
                    relative.update(alias.name for alias in node.names)
        self.assertEqual(relative, {"repos", "statuses"})


class JudgeTest(unittest.TestCase):
    def test_closed_md_is_superseded_whatever_git_says(self):
        for status in ("done", "cancelled", "canceled", " Done ", "CANCELLED"):
            for landed in (True, False, None):
                with self.subTest(status=status, landed=landed):
                    v = sup.judge(status, landed)
                    self.assertTrue(v.superseded)
                    self.assertEqual(v.reason, sup.REASON_MD_CLOSED)
                    self.assertEqual(v.detail, status.strip().lower())

    def test_open_md_is_superseded_only_by_a_measured_merge(self):
        for status in ("open", "in_progress", "", None):
            with self.subTest(status=status):
                v = sup.judge(status, True)
                self.assertEqual(v, sup.Verdict(True, sup.REASON_MERGED))
                for landed in (False, None):
                    self.assertEqual(sup.judge(status, landed), sup.NOT_SUPERSEDED)

    def test_truthy_but_not_true_is_not_a_merge(self):
        # Only the measured `True` counts; a stray "yes" or 1 is not a verdict.
        for landed in ("yes", 1, "merged", [True]):
            with self.subTest(landed=landed):
                self.assertFalse(sup.judge("open", landed).superseded)


class ClassifyLandedTest(unittest.TestCase):
    T1 = "a" * 40
    T2 = "b" * 40

    def test_table(self):
        t1, t2 = self.T1, self.T2
        rows = [
            # own, net,   rc,   merged, base -> expected
            ((1, True, 0, t1, t1), True),         # the squash case
            ((5, True, 0, t1, t1), True),
            ((1, True, 0, t1, t2), False),        # the branch still adds something
            ((0, True, 0, t1, t1), False),        # the empty-branch trap
            ((0, None, None, None, None), False),
            ((-1, True, 0, t1, t1), False),
            ((1, False, 0, t1, t1), False),       # commits that cancel out
            ((1, True, 1, t1, t1), False),        # a real conflict
            ((None, True, 0, t1, t1), None),      # own commits unread
            ((True, True, 0, t1, t1), None),      # a bool is not a count
            (("1", True, 0, t1, t1), None),
            ((1, None, 0, t1, t1), None),         # net change unread
            ((1, True, None, t1, t1), None),      # trial merge not run
            ((1, True, 128, t1, t1), None),       # trial merge errored
            ((1, True, 129, t1, t1), None),
            ((1, True, 0, "", t1), None),         # tree ids missing
            ((1, True, 0, t1, ""), None),
            ((1, True, 0, None, None), None),
        ]
        for args, expected in rows:
            with self.subTest(args=args):
                self.assertIs(sup.classify_landed(*args), expected)

    def test_zero_own_commits_is_false_before_anything_else_is_read(self):
        # A freshly cut branch merges "cleanly into an unchanged tree" too. If
        # that read as merged, every run would stop after its first stage.
        self.assertIs(sup.classify_landed(0, True, 0, self.T1, self.T1), False)

    def test_pure_functions_touch_no_subprocess(self):
        boom = AssertionError("a pure function reached a subprocess")
        with mock.patch("subprocess.run", side_effect=boom), \
                mock.patch.object(sup, "_run", side_effect=boom):
            self.assertIs(sup.classify_landed(1, True, 0, self.T1, self.T1), True)
            self.assertTrue(sup.judge("done", None).superseded)
            self.assertEqual(sup.normalize_reason("merged"), sup.REASON_MERGED)
            self.assertIn("superseded", sup.note(sup.REASON_MERGED))


class NormalizeReasonTest(unittest.TestCase):
    def test_table(self):
        for value in (None, "", 0, False, [], {}):
            self.assertEqual(sup.normalize_reason(value), "", repr(value))
        self.assertEqual(sup.normalize_reason("merged"), sup.REASON_MERGED)
        self.assertEqual(sup.normalize_reason("md-closed"), sup.REASON_MD_CLOSED)
        # Anything unrecognised takes the reason the orchestrator double-checks.
        for value in (True, 1, "something-newer", "Merged", ["merged"]):
            self.assertEqual(sup.normalize_reason(value), sup.REASON_MD_CLOSED,
                             repr(value))


class NoteTest(unittest.TestCase):
    def test_lines(self):
        self.assertEqual(sup.note(sup.REASON_MD_CLOSED, "cancelled"),
                         "superseded — task md status: cancelled")
        self.assertEqual(sup.note(sup.REASON_MD_CLOSED),
                         "superseded — task md status: closed")
        self.assertEqual(sup.note(sup.REASON_MERGED, "ignored"),
                         "superseded — branch work already in the default branch")


class MdClosedStatusTest(unittest.TestCase):
    def setUp(self):
        self.vault = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.vault, ignore_errors=True)
        self.items = self.vault / "Todo" / "items"
        self.items.mkdir(parents=True)
        self.md = self.items / "t.md"
        self.rel = {"task_path": "Todo/items/t.md"}

    def test_closed_statuses_relative_and_absolute(self):
        for written, expected in (("done", "done"), ("cancelled", "cancelled"),
                                  ("canceled", "canceled"), ("Done", "done"),
                                  ('" Done "', "done")):
            self.md.write_text(_md(written), encoding="utf-8")
            with self.subTest(written=written):
                self.assertEqual(sup.md_closed_status(self.rel, self.vault), expected)
                self.assertEqual(
                    sup.md_closed_status({"task_path": str(self.md)}, None), expected)
                self.assertEqual(
                    sup.md_closed_status({"task_path": str(self.md)},
                                         self.vault / "elsewhere"), expected)

    def test_open_or_unreadable_is_none(self):
        closed = _md("done")
        cases = {
            "open": _md("open"),
            "no status": _md(None),
            "placeholder": _md('"-"'),
            "no frontmatter": "# t\n\nstatus: done\n",
            "unclosed frontmatter": "---\nstatus: done\n# t\n",
            "broken yaml": "---\nstatus: done\ntags: [a, b\n---\n# t\n",
            "non-dict yaml": "---\n- status: done\n---\n",
            "status is a mapping": "---\nstatus:\n  value: done\n---\n",
        }
        for name, text in cases.items():
            self.md.write_text(text, encoding="utf-8")
            with self.subTest(name=name):
                self.assertIsNone(sup.md_closed_status(self.rel, self.vault))
        # Positive control on the same path: the file IS read when it is closed.
        self.md.write_text(closed, encoding="utf-8")
        self.assertEqual(sup.md_closed_status(self.rel, self.vault), "done")

    def test_no_usable_task_path_is_none(self):
        self.md.write_text(_md("done"), encoding="utf-8")
        for payload in (None, {}, {"task_path": ""}, {"task_path": None},
                        {"task_path": 123}, {"task_path": ["Todo/items/t.md"]},
                        {"task_path": "Todo/items/missing.md"},
                        {"task_path": "Todo/items"}):          # a directory
            with self.subTest(payload=payload):
                self.assertIsNone(sup.md_closed_status(payload, self.vault))
        # relative path with no root to resolve it against
        self.assertIsNone(sup.md_closed_status(self.rel, None))

    def test_the_path_is_never_guessed_from_the_slug(self):
        # Most _execute tests dispatch without `task_path`; guessing
        # Todo/items/<slug>.md would make them read the real vault.
        self.md.write_text(_md("done"), encoding="utf-8")
        for payload in ({"slug": "t"}, {"task_id": "t"}, {"summary": "t"}):
            self.assertIsNone(sup.md_closed_status(payload, self.vault))
            self.assertIsNone(sup.task_md_path(payload, self.vault))

    def test_a_reader_that_raises_is_none(self):
        # A bare remote runner has no PyYAML: repos._frontmatter raises there.
        self.md.write_text(_md("done"), encoding="utf-8")
        for exc in (ModuleNotFoundError("No module named 'yaml'"),
                    OSError("unreadable"), RuntimeError("anything")):
            with self.subTest(exc=type(exc).__name__), \
                    mock.patch.object(repos, "_frontmatter", side_effect=exc):
                self.assertIsNone(sup.md_closed_status(self.rel, self.vault))
        self.assertEqual(sup.md_closed_status(self.rel, self.vault), "done")

    def test_task_md_path_resolution(self):
        self.md.write_text(_md("open"), encoding="utf-8")
        self.assertEqual(sup.task_md_path(self.rel, self.vault), self.md)
        self.assertEqual(sup.task_md_path({"task_path": str(self.md)}, None), self.md)
        self.assertIsNone(sup.task_md_path(self.rel, self.vault / "other"))


class _GitFixture(unittest.TestCase):
    """A bare origin and two clones.

    `clone` is the run's checkout: it cuts `worker/t` and ends every setup step
    clean with HEAD on that branch, which is the state the runner calls from.
    `human` is where "a person" lands things on main, the way a squash merge on
    the forge does. The run's clone learns of it only through a fetch, so its
    `origin/main` is stale until `branch_landed` itself fetches.
    """

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.origin = self.root / "origin.git"
        self.clone = self.root / "clone"
        self.human = self.root / "human"
        self._ok(self.root, "init", "--bare", "-q", "-b", "main", "origin.git")
        self._ok(self.root, "clone", "-q", str(self.origin), "clone")
        self._identity(self.clone)
        # An empty origin gives the clone no branch to be on; name it explicitly.
        self._ok(self.clone, "symbolic-ref", "HEAD", "refs/heads/main")
        (self.clone / "a.txt").write_text(_lines(), encoding="utf-8")
        (self.clone / "b.txt").write_text("b\n", encoding="utf-8")
        self._ok(self.clone, "add", "-A")
        self._ok(self.clone, "commit", "-qm", "seed")
        self._ok(self.clone, "push", "-q", "origin", "main")
        self._ok(self.root, "clone", "-q", str(self.origin), "human")
        self._identity(self.human)
        self._ok(self.clone, "checkout", "-q", "-b", BRANCH)

    # -- helpers ----------------------------------------------------------
    def _ok(self, cwd, *args):
        result = _git(cwd, *args)
        self.assertEqual(result.returncode, 0,
                         f"fixture step failed: git {' '.join(args)}\n{result.stderr}")
        return result

    def _identity(self, repo):
        self._ok(repo, "config", "user.email", "t@example.invalid")
        self._ok(repo, "config", "user.name", "t")
        self._ok(repo, "config", "commit.gpgsign", "false")

    def _rev(self, repo, ref):
        return self._ok(repo, "rev-parse", ref).stdout.strip()

    def _branch_commit(self, path="a.txt", text=None, message="work"):
        """One commit on worker/t in the run's clone, pushed like a checkpoint."""
        (self.clone / path).write_text(
            _lines(l2="branch-2") if text is None else text, encoding="utf-8")
        self._ok(self.clone, "add", "-A")
        self._ok(self.clone, "commit", "-qm", message)
        self._ok(self.clone, "push", "-q", "origin", BRANCH)

    def _land_squash(self):
        """Squash worker/t into origin main from the other clone."""
        self._ok(self.human, "fetch", "-q", "origin")
        self._ok(self.human, "checkout", "-q", "main")
        self._ok(self.human, "merge", "--squash", "-q", f"origin/{BRANCH}")
        self._ok(self.human, "commit", "-qm", "squash worker/t")
        self._ok(self.human, "push", "-q", "origin", "main")

    def _human_commit(self, path, text, message="main moves"):
        self._ok(self.human, "checkout", "-q", "main")
        (self.human / path).write_text(text, encoding="utf-8")
        self._ok(self.human, "add", "-A")
        self._ok(self.human, "commit", "-qm", message)
        self._ok(self.human, "push", "-q", "origin", "main")

    def _landed(self):
        return sup.branch_landed(self.clone, BRANCH)


class BranchLandedRealGitTest(_GitFixture):
    def test_squash_merged_branch_is_landed(self):
        self._branch_commit()
        self._land_squash()
        # The run's clone has not fetched: its origin/main still predates the
        # merge, so a True here is the probe's own fetch at work.
        self.assertNotEqual(self._rev(self.clone, "origin/main"),
                            self._rev(self.human, "main"))
        self.assertIs(self._landed(), True)
        self.assertEqual(self._rev(self.clone, "origin/main"),
                         self._rev(self.human, "main"))

    def test_squash_then_main_moves_on_elsewhere_is_still_landed(self):
        self._branch_commit()
        self._land_squash()
        self._human_commit("b.txt", "b changed on main\n")
        self._human_commit("a.txt", _lines(l2="branch-2", l8="main-8"))
        self.assertIs(self._landed(), True)

    def test_main_rewriting_the_same_line_afterwards_is_not_landed(self):
        # Control for the test above, same fixture: the difference between them
        # is only WHERE main moved, so that test's True is a measurement.
        self._branch_commit()
        self._land_squash()
        self._human_commit("a.txt", _lines(l2="main rewrote line 2"))
        self.assertIs(self._landed(), False)

    def test_fresh_branch_with_no_commit_is_not_landed(self):
        # The empty-branch trap, against real git: nothing of its own to land.
        self.assertIs(self._landed(), False)
        self._human_commit("b.txt", "main moved after the branch was cut\n")
        self.assertIs(self._landed(), False)

    def test_branch_with_work_main_lacks_is_not_landed(self):
        self._branch_commit()
        self.assertIs(self._landed(), False)
        self._human_commit("b.txt", "unrelated\n")
        self.assertIs(self._landed(), False)

    def test_a_commit_added_after_the_squash_is_not_landed(self):
        self._branch_commit()
        self._land_squash()
        self.assertIs(self._landed(), True)              # control: landed so far
        self._branch_commit(path="b.txt", text="more work\n", message="more")
        self.assertIs(self._landed(), False)

    def test_commit_then_revert_is_not_landed(self):
        # The trial merge leaves main's tree unchanged here too, so without the
        # net-change guard this would read as merged with nothing ever merged.
        self._branch_commit()
        self._ok(self.clone, "revert", "--no-edit", "HEAD")
        self.assertEqual(
            self._ok(self.clone, "rev-list", "--count", f"main..{BRANCH}").stdout.strip(),
            "2")
        self.assertIs(self._landed(), False)

    def test_uncommitted_or_untracked_work_is_not_landed(self):
        self._branch_commit()
        self._land_squash()
        self.assertIs(self._landed(), True)              # control: clean tree
        (self.clone / "b.txt").write_text("uncommitted edit\n", encoding="utf-8")
        self.assertIs(self._landed(), False)
        self._ok(self.clone, "checkout", "-q", "--", "b.txt")
        self.assertIs(self._landed(), True)
        (self.clone / "new.txt").write_text("untracked\n", encoding="utf-8")
        self.assertIs(self._landed(), False)

    def test_head_elsewhere_is_unmeasured(self):
        self._branch_commit()
        self._land_squash()
        self.assertIs(self._landed(), True)              # control
        self._ok(self.clone, "checkout", "-q", "main")
        self.assertIsNone(self._landed())
        self._ok(self.clone, "checkout", "-q", "--detach", BRANCH)
        self.assertIsNone(self._landed())
        self._ok(self.clone, "checkout", "-q", BRANCH)
        self.assertIs(self._landed(), True)

    def test_failed_fetch_is_unmeasured_even_with_fresh_local_refs(self):
        self._branch_commit()
        self._land_squash()
        self.assertIs(self._landed(), True)              # refs are now up to date
        self._ok(self.clone, "remote", "set-url", "origin", str(self.root / "gone.git"))
        # The local refs would still say "landed"; without a fetch nothing is said.
        self.assertIsNone(self._landed())

    def test_unknown_branch_name_is_unmeasured(self):
        self._branch_commit()
        self._land_squash()
        self.assertIsNone(sup.branch_landed(self.clone, "worker/other"))
        self.assertIsNone(sup.branch_landed(self.clone, ""))

    def test_default_branch_is_read_from_origin_head(self):
        # A repo whose default branch is not `main`, and which has no `main` at
        # all: the fallback could not even fetch here.
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self._ok(root, "init", "--bare", "-q", "-b", "trunk", "origin.git")
        self._ok(root, "clone", "-q", str(root / "origin.git"), "seed")
        seed = root / "seed"
        self._identity(seed)
        self._ok(seed, "symbolic-ref", "HEAD", "refs/heads/trunk")
        (seed / "a.txt").write_text(_lines(), encoding="utf-8")
        self._ok(seed, "add", "-A")
        self._ok(seed, "commit", "-qm", "seed")
        self._ok(seed, "push", "-q", "origin", "trunk")
        # Cloned from a non-empty origin, so refs/remotes/origin/HEAD exists.
        self._ok(root, "clone", "-q", str(root / "origin.git"), "run")
        run = root / "run"
        self._identity(run)
        self.assertEqual(
            self._ok(run, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
            .stdout.strip(), "origin/trunk")
        self._ok(run, "checkout", "-q", "-b", BRANCH)
        (run / "a.txt").write_text(_lines(l2="branch-2"), encoding="utf-8")
        self._ok(run, "commit", "-qam", "work")
        self._ok(run, "push", "-q", "origin", BRANCH)
        self.assertIs(sup.branch_landed(run, BRANCH), False)      # not landed yet
        self._ok(seed, "fetch", "-q", "origin")
        self._ok(seed, "merge", "--squash", "-q", f"origin/{BRANCH}")
        self._ok(seed, "commit", "-qm", "squash")
        self._ok(seed, "push", "-q", "origin", "trunk")
        self.assertIs(sup.branch_landed(run, BRANCH), True)

    def test_probe_leaves_the_checkout_untouched(self):
        self._branch_commit()
        for landed in (False, True):
            if landed:
                self._land_squash()
            before = (self._rev(self.clone, "HEAD"),
                      self._ok(self.clone, "branch", "--show-current").stdout,
                      self._rev(self.clone, f"refs/heads/{BRANCH}"),
                      self._rev(self.clone, "refs/heads/main"),
                      self._ok(self.clone, "status", "--porcelain").stdout,
                      (self.clone / "a.txt").read_bytes(),
                      (self.clone / "b.txt").read_bytes())
            with self.subTest(landed=landed):
                self.assertIs(self._landed(), landed)
                after = (self._rev(self.clone, "HEAD"),
                         self._ok(self.clone, "branch", "--show-current").stdout,
                         self._rev(self.clone, f"refs/heads/{BRANCH}"),
                         self._rev(self.clone, "refs/heads/main"),
                         self._ok(self.clone, "status", "--porcelain").stdout,
                         (self.clone / "a.txt").read_bytes(),
                         (self.clone / "b.txt").read_bytes())
                self.assertEqual(before, after)


class BranchLandedFailOpenTest(_GitFixture):
    def setUp(self):
        super().setUp()
        self._branch_commit()
        self._land_squash()

    def _with_fake(self, fake):
        with mock.patch.object(sup, "_run", side_effect=fake) as spy:
            return sup.branch_landed(self.clone, BRANCH), spy

    def test_control_the_fixture_is_landed(self):
        # Everything below starts from a branch that really is landed, so each
        # "not True" is the failure being injected and not an unmerged fixture.
        self.assertIs(self._landed(), True)

    def test_not_a_checkout_spawns_nothing(self):
        spy = mock.Mock(side_effect=AssertionError("no subprocess expected"))
        with mock.patch.object(sup, "_run", spy):
            self.assertIsNone(sup.branch_landed(self.root / "no-such-clone", BRANCH))
            self.assertIsNone(sup.branch_landed(self.root, BRANCH))   # a dir, no .git
            self.assertIsNone(sup.branch_landed(None, BRANCH))
            self.assertIsNone(sup.branch_landed(self.clone, ""))
        spy.assert_not_called()

    def test_git_that_cannot_run_is_unmeasured(self):
        for exc in (OSError("no git"), FileNotFoundError("git"),
                    subprocess.TimeoutExpired(cmd="git", timeout=1)):
            with self.subTest(exc=type(exc).__name__):
                got, spy = self._with_fake(exc)
                self.assertIsNone(got)
                self.assertGreaterEqual(spy.call_count, 1)

    def test_an_unexpected_exception_is_unmeasured_not_raised(self):
        got, _ = self._with_fake(RuntimeError("anything"))
        self.assertIsNone(got)

    def test_blank_success_from_every_command_is_never_landed(self):
        # The shape a mocked suite gives every subprocess call: rc 0, no output.
        # It must not be able to forge a merge.
        got, spy = self._with_fake(
            lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
        self.assertIsNot(got, True)
        self.assertIsNone(got)
        self.assertGreaterEqual(spy.call_count, 1)

    def test_mock_objects_as_results_are_never_landed(self):
        got, _ = self._with_fake(lambda argv, **kw: mock.Mock())
        self.assertIsNot(got, True)

    def _rewriting(self, verb, result):
        def fake(argv, **kw):
            if verb in argv:
                return result(argv)
            return _REAL_RUN(argv, **kw)
        return fake

    def test_fetch_failure_is_unmeasured(self):
        got, _ = self._with_fake(self._rewriting(
            "fetch", lambda argv: subprocess.CompletedProcess(argv, 128, "", "fatal")))
        self.assertIsNone(got)

    def test_trial_merge_that_never_started_is_unmeasured(self):
        # rc 129 = a git too old for --write-tree; rc 1 with no stdout = git's
        # answer for an unresolvable ref (the code that otherwise means conflict).
        for rc in (129, 128, 1):
            with self.subTest(rc=rc):
                got, _ = self._with_fake(self._rewriting(
                    "merge-tree",
                    lambda argv, rc=rc: subprocess.CompletedProcess(argv, rc, "", "x")))
                self.assertIsNone(got)

    def test_trial_merge_printing_no_tree_id_is_unmeasured(self):
        got, _ = self._with_fake(self._rewriting(
            "merge-tree",
            lambda argv: subprocess.CompletedProcess(argv, 0, "not-a-tree-id\n", "")))
        self.assertIsNone(got)

    def test_unreadable_count_or_diff_is_unmeasured(self):
        for verb, result in (
                ("rev-list", lambda argv: subprocess.CompletedProcess(argv, 0, "many\n", "")),
                ("rev-list", lambda argv: subprocess.CompletedProcess(argv, 128, "", "x")),
                ("merge-base", lambda argv: subprocess.CompletedProcess(argv, 1, "", "")),
                ("diff", lambda argv: subprocess.CompletedProcess(argv, 129, "", "x")),
                ("status", lambda argv: subprocess.CompletedProcess(argv, 128, "", "x"))):
            with self.subTest(verb=verb):
                got, _ = self._with_fake(self._rewriting(verb, result))
                self.assertIsNone(got)

    def test_git_calls_are_bounded_and_never_read_stdin(self):
        seen = []

        def fake(argv, **kw):
            seen.append((argv, kw))
            return _REAL_RUN(argv, **kw)

        got, _ = self._with_fake(fake)
        self.assertIs(got, True)
        self.assertTrue(seen)
        for argv, kw in seen:
            self.assertEqual(argv[:3], ["git", "-C", str(self.clone)])
            self.assertIs(kw.get("stdin"), subprocess.DEVNULL)
            self.assertIn(kw.get("timeout"),
                          (sup.GIT_TIMEOUT_LOCAL_S, sup.GIT_TIMEOUT_FETCH_S))
            # No credential prompt, no index refresh; the rest of the env is kept.
            self.assertEqual(kw["env"]["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(kw["env"]["GIT_OPTIONAL_LOCKS"], "0")
            self.assertEqual(kw["env"].get("PATH"), os.environ.get("PATH"))
        verbs = [argv[3] for argv, _ in seen]
        # Nothing that moves HEAD, the index, the working tree or a local branch.
        self.assertFalse(
            {"checkout", "switch", "reset", "merge", "commit", "push", "pull",
             "rebase", "stash", "clean", "branch", "add"} & set(verbs), verbs)
        self.assertLess(verbs.index("fetch"), verbs.index("rev-list"))


class CheckRunTest(_GitFixture):
    def setUp(self):
        super().setUp()
        self.vault = self.root / "vault"
        self.md = self.vault / "Todo" / "items" / "t.md"
        self.md.parent.mkdir(parents=True)
        self.md.write_text(_md("open"), encoding="utf-8")
        self.payload = {"task_path": "Todo/items/t.md"}

    def _check(self, **kw):
        kw.setdefault("vault_root", self.vault)
        return sup.check_run(self.payload, **kw)

    def test_closed_md_is_superseded_without_touching_git(self):
        boom = mock.Mock(side_effect=AssertionError("git must not run"))
        for status in ("done", "cancelled", "canceled"):
            self.md.write_text(_md(status), encoding="utf-8")
            with self.subTest(status=status), mock.patch.object(sup, "_run", boom):
                v = self._check(repo_dir=self.clone, branch=BRANCH, probe_git=True)
                self.assertEqual(v, sup.Verdict(True, sup.REASON_MD_CLOSED, status))
        boom.assert_not_called()

    def test_open_md_without_the_probe_touches_no_git(self):
        self._branch_commit()
        self._land_squash()
        boom = mock.Mock(side_effect=AssertionError("git must not run"))
        with mock.patch.object(sup, "_run", boom):
            self.assertEqual(self._check(), sup.NOT_SUPERSEDED)
            self.assertEqual(
                self._check(repo_dir=self.clone, branch=BRANCH, probe_git=False),
                sup.NOT_SUPERSEDED)
            # the probe needs a checkout and a branch to look at
            self.assertEqual(self._check(probe_git=True), sup.NOT_SUPERSEDED)
            self.assertEqual(self._check(repo_dir=self.clone, probe_git=True),
                             sup.NOT_SUPERSEDED)
        boom.assert_not_called()

    def test_open_md_and_a_squash_merged_branch_is_merged(self):
        self._branch_commit()
        self.assertEqual(
            self._check(repo_dir=self.clone, branch=BRANCH, probe_git=True),
            sup.NOT_SUPERSEDED)                           # not landed yet
        self._land_squash()
        self.assertEqual(
            self._check(repo_dir=self.clone, branch=BRANCH, probe_git=True),
            sup.Verdict(True, sup.REASON_MERGED))

    def test_no_md_at_all_still_lets_git_decide(self):
        self._branch_commit()
        self._land_squash()
        v = sup.check_run({}, vault_root=self.vault, repo_dir=self.clone,
                          branch=BRANCH, probe_git=True)
        self.assertEqual(v, sup.Verdict(True, sup.REASON_MERGED))

    def test_any_internal_failure_is_not_superseded(self):
        self.md.write_text(_md("done"), encoding="utf-8")
        self.assertTrue(self._check().superseded)          # control
        for target in ("md_closed_status", "judge"):
            with self.subTest(target=target), \
                    mock.patch.object(sup, target, side_effect=RuntimeError("boom")):
                self.assertEqual(self._check(), sup.NOT_SUPERSEDED)
        self.md.write_text(_md("open"), encoding="utf-8")
        with mock.patch.object(sup, "branch_landed", side_effect=RuntimeError("boom")):
            self.assertEqual(
                self._check(repo_dir=self.clone, branch=BRANCH, probe_git=True),
                sup.NOT_SUPERSEDED)


if __name__ == "__main__":
    unittest.main()
