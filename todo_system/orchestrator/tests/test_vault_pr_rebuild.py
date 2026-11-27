"""A vault delegate PR that conflicts only because of the engine's own commits is
rebuilt and merged by the engine; a conflict in the run's own work still parks.

The 2026-11-27 incident: #433 and #434 stranded together, CONFLICTING on
`Worker/agents/…/inbox/*.json`, `Worker/assignments/*.json` and
`Memory/index.md` — files the runs never touched. The todo-sync daemon had
committed `chore: sync vault state` onto the checked-out worker branch while
main got the same state under other shas. A person moved the run's own commits
onto main by cherry-pick. These pin the engine doing that itself.

Every scenario runs against REAL git: a bare `origin`, a clone carrying the
engine's FF-only pre-push guard, and `gh pr merge` replaced by a fake that
answers the way GitHub would — mergeable iff `git merge-tree origin/main
origin/<branch>` is clean.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import message as m
from todo_system.orchestrator import repos, stages
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator import vault_pr_rebuild as vpr
from todo_system.orchestrator.registry import Agent
from todo_system.orchestrator.runner import StagedExecutor

ENGINE_ROOT = Path(__file__).resolve().parents[3]
BRANCH = "worker/t"
CONFLICT_NOTE = ("gh pr merge of worker/t failed: X Pull request #433 is not mergeable: "
                 "the merge commit cannot be cleanly created.")


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r.stdout.strip()


def _rc(cwd: Path, *args: str) -> int:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True).returncode


class _VaultRepo:
    """bare origin + a vault clone on `main`, push guard installed."""

    def __init__(self, root: Path):
        self.origin = root / "origin.git"
        self.vault = root / "vault"
        self.other = root / "other"           # the daemon's view of main
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)],
                       check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.vault)],
                       check=True, capture_output=True)
        for repo in (self.vault,):
            self._ident(repo)
        self.write(self.vault, "Worker/assignments/q.json", '{"state": "open"}\n')
        self.write(self.vault, "Memory/index.md", "last_synced: 0\n")
        self.write(self.vault, "Notes/base.md", "base\n")
        self.commit(self.vault, "init")
        _git(self.vault, "push", "-q", "origin", "main")
        _git(self.vault, "remote", "set-head", "origin", "main")
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.other)],
                       check=True, capture_output=True)
        self._ident(self.other)
        stages.install_push_guard(self.vault)

    @staticmethod
    def _ident(repo: Path) -> None:
        _git(repo, "config", "user.name", "t")
        _git(repo, "config", "user.email", "t@example.invalid")
        _git(repo, "config", "commit.gpgsign", "false")

    @staticmethod
    def write(repo: Path, rel: str, text: str) -> None:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    @staticmethod
    def commit(repo: Path, subject: str) -> str:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", subject)
        return _git(repo, "rev-parse", "HEAD")

    def main_advances_with_sync_state(self) -> None:
        """The daemon commits the live state to main (different sha, same/newer
        content than what landed on the branch)."""
        self.write(self.other, "Worker/assignments/q.json", '{"state": "running"}\n')
        self.write(self.other, "Memory/index.md", "last_synced: 1\n")
        self.commit(self.other, "chore: sync vault state (2 files)")
        self.write(self.other, "Worker/assignments/q.json", '{"state": "done"}\n')
        self.commit(self.other, "chore: sync vault state (1 files)")
        _git(self.other, "push", "-q", "origin", "main")

    def branch_with_mixed_commits(self, real_conflicts_with_main: bool = False) -> dict:
        """worker/t: real commit, sync commit, planner commit, real commit, and a
        real commit whose change main already has (→ empty, skipped)."""
        _git(self.vault, "checkout", "-q", "-b", BRANCH)
        shas = {}
        self.write(self.vault, "Notes/work.md", "work v1\n")
        shas["w1"] = self.commit(self.vault, "chore(worker): t phase 1 — add work note")
        self.write(self.vault, "Worker/assignments/q.json", '{"state": "running"}\n')
        shas["s1"] = self.commit(self.vault, "chore: sync vault state (1 files)")
        self.write(self.vault, "Memory/index.md", "last_synced: 9\n")
        shas["p1"] = self.commit(self.vault, "chore: planner — reprioritise")
        self.write(self.vault, "Notes/work.md", "work v2\n")
        shas["w2"] = self.commit(self.vault, "chore(worker): t phase 2 — refine work note")
        self.write(self.vault, "Notes/base.md", "base fixed\n")
        shas["w0"] = self.commit(self.vault, "docs: fix base typo")
        _git(self.vault, "push", "-q", "origin", BRANCH)
        # main meanwhile: daemon state + the same typo fix under another sha
        self.main_advances_with_sync_state()
        self.write(self.other, "Notes/base.md", "base fixed\n")
        if real_conflicts_with_main:
            self.write(self.other, "Notes/work.md", "someone else's work\n")
        self.commit(self.other, "docs: fix base typo (main)")
        _git(self.other, "push", "-q", "origin", "main")
        return shas

    def fetch(self) -> None:
        _git(self.vault, "fetch", "-q", "origin")

    def mergeable(self) -> bool:
        """What GitHub would answer for the PR worker/t → main."""
        self.fetch()
        return _rc(self.vault, "merge-tree", "--write-tree", "origin/main",
                   f"origin/{BRANCH}") == 0


class _GitHubMerge:
    """`stages.merge_pr` stand-in: refuses with a conflict note while the PR is
    not mergeable, otherwise 'squash-merges'. Records every call."""

    def __init__(self, repo: _VaultRepo):
        self.repo = repo
        self.calls: list[tuple] = []

    def __call__(self, repo_dir, branch, repo=None, progress=None):
        self.calls.append((Path(repo_dir), branch, repo))
        if self.repo.mergeable():
            return {"merged": True, "note": f"squash-merged {branch}"}
        return {"merged": False, "note": CONFLICT_NOTE}


class EngineCommitListTest(unittest.TestCase):
    def test_engine_subjects_are_recognised(self):
        for s in ("chore: sync vault state (13 files)", "chore: planner — x",
                  "chore(planner): reorder", "chore: beta watermark app -> 12",
                  "chore(mailbox): 01M8", "chore(session): p manual-work checkpoint",
                  "chore: bump macp pin abc -> def", "chore(health): weekly report"):
            self.assertTrue(vpr.is_engine_commit(s), s)

    def test_the_runs_own_and_human_commits_are_kept(self):
        for s in ("chore(worker): t phase 1 — x", "chore(worker): t WIP rescue after implement",
                  "chore: record decision for t (dashboard)", "feat: x", "fix: y",
                  "chore: sync todos from srv", ""):
            self.assertFalse(vpr.is_engine_commit(s), s)

    def test_list_tracks_the_producers_in_source(self):
        # One list, but the subjects are written elsewhere — fail here when a
        # producer is renamed, instead of silently keeping its commits.
        producers = {
            "todo_system/worker.py": ('f"chore: sync vault state ({n} files)"',
                                      "chore: sync vault state (3 files)"),
            "todo_system/orchestrator/beta_publish.py": (
                'f"chore: beta watermark {repo_name} -> {version_code}"',
                "chore: beta watermark app -> 7"),
            "todo_system/orchestrator/mailbox.py": ('f"chore(mailbox): {path.stem}"',
                                                    "chore(mailbox): 01ABC"),
            "todo_system/session_log.py": ('f"chore(session): {project} manual-work checkpoint"',
                                           "chore(session): p manual-work checkpoint"),
        }
        for rel, (literal, sample) in producers.items():
            text = (ENGINE_ROOT / rel).read_text(encoding="utf-8")
            self.assertIn(literal, text, rel)
            self.assertTrue(vpr.is_engine_commit(sample), sample)
        pin = (ENGINE_ROOT / "todo_system/worker.py").read_text(encoding="utf-8")
        self.assertIn('f"chore: bump macp pin ', pin)
        health = (ENGINE_ROOT / "scripts/health_report.py").read_text(encoding="utf-8")
        self.assertIn('f"chore(health): weekly report', health)
        # The planner's subjects: whatever planner_tick.sh calls the planner's own.
        tick = (ENGINE_ROOT / "scripts/planner_tick.sh").read_text(encoding="utf-8")
        pat = re.search(r"grep -qE '([^']+)'", tick)
        self.assertIsNotNone(pat)
        planner = re.compile(pat.group(1))
        for s in ("chore: planner — x", "chore(planner): y"):
            self.assertTrue(planner.search(s), s)
            self.assertTrue(vpr.is_engine_commit(s), s)

    def test_conflict_note_detection(self):
        self.assertTrue(vpr.looks_like_conflict(CONFLICT_NOTE))
        self.assertTrue(vpr.looks_like_conflict("Pull request is not mergeable: CONFLICTING"))
        self.assertFalse(vpr.looks_like_conflict("gh pr merge of x unavailable (OSError)"))
        self.assertFalse(vpr.looks_like_conflict("not mergeable: base branch policy"))
        self.assertFalse(vpr.looks_like_conflict(None))


class _RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.r = _VaultRepo(self.tmp)


class RebuildOntoBaseTest(_RepoCase):
    def test_drops_engine_commits_skips_empty_keeps_real(self):
        shas = self.r.branch_with_mixed_commits()
        self.r.fetch()
        self.assertFalse(self.r.mergeable())          # the stranded PR, reproduced
        rb = vpr.rebuild_onto_base(self.r.vault, BRANCH, "origin/main")
        self.assertTrue(rb["ok"], rb["note"])
        self.assertEqual(rb["dropped"], [shas["s1"], shas["p1"]])
        self.assertEqual(rb["kept"], [shas["w1"], shas["w2"]])
        self.assertEqual(rb["skipped_empty"], [shas["w0"]])
        # the rebuilt tip sits on origin/main and differs from it only in the work
        self.assertEqual(_rc(self.r.vault, "merge-base", "--is-ancestor",
                             "origin/main", rb["tip"]), 0)
        changed = _git(self.r.vault, "diff", "--name-only", "origin/main", rb["tip"])
        self.assertEqual(changed.split(), ["Notes/work.md"])
        self.assertEqual(_git(self.r.vault, "show", f"{rb['tip']}:Notes/work.md"), "work v2")
        # cherry-pick -x trail + original author kept
        body = _git(self.r.vault, "log", "-1", "--format=%B", rb["tip"])
        self.assertIn(f"(cherry picked from commit {shas['w2']})", body)
        # nothing moved: no ref, no checkout touched
        self.assertEqual(_git(self.r.vault, "rev-parse", BRANCH), shas["w0"])
        self.assertEqual(_git(self.r.vault, "status", "--porcelain"), "")

    def test_real_conflict_returns_files_and_writes_no_ref(self):
        self.r.branch_with_mixed_commits(real_conflicts_with_main=True)
        self.r.fetch()
        rb = vpr.rebuild_onto_base(self.r.vault, BRANCH, "origin/main")
        self.assertFalse(rb["ok"])
        self.assertEqual(rb["conflict_files"], ["Notes/work.md"])
        self.assertIsNone(rb["tip"])


class RebuildAndMergeTest(_RepoCase):
    def test_rebuild_pushes_fast_forward_and_merges(self):
        shas = self.r.branch_with_mixed_commits()
        gh = _GitHubMerge(self.r)
        out = vpr.rebuild_and_merge(self.r.vault, BRANCH, merge=gh, sleep=lambda _: None)
        self.assertTrue(out["merged"], out["note"])
        self.assertTrue(out["rebuilt"])
        self.assertEqual(len(gh.calls), 1)
        self.r.fetch()
        tip = _git(self.r.vault, "rev-parse", f"origin/{BRANCH}")
        # fast-forward over the old tip (the pre-push guard let it through)
        self.assertEqual(_rc(self.r.vault, "merge-base", "--is-ancestor", shas["w0"], tip), 0)
        self.assertEqual(_git(self.r.vault, "diff", "--name-only", "origin/main", tip).split(),
                         ["Notes/work.md"])
        # main itself was never pushed to
        self.assertEqual(_git(self.r.vault, "log", "-1", "--format=%s", "origin/main"),
                         "docs: fix base typo (main)")
        # local branch followed the pushed tip, worktree clean
        self.assertEqual(_git(self.r.vault, "rev-parse", BRANCH), tip)
        self.assertEqual(_git(self.r.vault, "status", "--porcelain"), "")

    def test_retries_merge_while_github_recomputes(self):
        self.r.branch_with_mixed_commits()
        answers = iter([{"merged": False, "note": "not mergeable yet (UNKNOWN)"},
                        {"merged": True, "note": "squash-merged"}])
        merge = mock.Mock(side_effect=lambda *a, **k: next(answers))
        slept = []
        out = vpr.rebuild_and_merge(self.r.vault, BRANCH, merge=merge, sleep=slept.append)
        self.assertTrue(out["merged"], out["note"])
        self.assertEqual(merge.call_count, 2)
        self.assertEqual(len(slept), 1)

    def test_real_conflict_pushes_nothing(self):
        self.r.branch_with_mixed_commits(real_conflicts_with_main=True)
        before = _git(self.r.vault, "ls-remote", "origin", BRANCH)
        gh = _GitHubMerge(self.r)
        out = vpr.rebuild_and_merge(self.r.vault, BRANCH, merge=gh, sleep=lambda _: None)
        self.assertFalse(out["merged"])
        self.assertFalse(out["rebuilt"])
        self.assertEqual(out["conflict_files"], ["Notes/work.md"])
        self.assertIn("Notes/work.md", out["note"])
        self.assertEqual(gh.calls, [])
        self.assertEqual(_git(self.r.vault, "ls-remote", "origin", BRANCH), before)

    def test_refuses_a_non_worker_branch(self):
        merge = mock.Mock()
        out = vpr.rebuild_and_merge(self.r.vault, "main", merge=merge)
        self.assertFalse(out["merged"])
        merge.assert_not_called()

    def test_no_force_admin_or_auto_anywhere_in_the_module(self):
        src = (ENGINE_ROOT / "todo_system/orchestrator/vault_pr_rebuild.py").read_text(
            encoding="utf-8")
        code = "\n".join(line for line in src.splitlines()
                         if not line.lstrip().startswith("#"))
        for flag in ('"--force', '"--admin"', '"--auto"', '"-f"', "+refs/", '"--no-verify"'):
            self.assertNotIn(flag, code)


def _agent(**kw) -> Agent:
    base = dict(id="w", kind="ai-local", capabilities=["git"], repo="~/Worker/x")
    base.update(kw)
    return Agent(**base)


class DelegateFinallyRebuildTest(_RepoCase):
    """The whole vault delegate run through StagedExecutor: the `finally` publish
    → merge refused (CONFLICTING) → rebuild → merge again. The worktree is the
    real clone above; only the stage subprocess, `gh pr create` (publish) and
    the account/lease plumbing are stubbed."""

    def setUp(self):
        super().setUp()
        self.runs = self.tmp / "runs"
        self.runs.mkdir()

    def _run_dir(self) -> Path:
        dirs = list(self.runs.iterdir())
        self.assertEqual(len(dirs), 1, dirs)
        return dirs[0]

    def _run(self, merge, payload_extra=None):
        vp = subprocess.CompletedProcess([], 0, "", "")
        vp.problem = None
        vp.orphaned_commits = None

        def fake_stage(argv, **kw):
            (self._run_dir() / stages.STAGE_ARTIFACT["delegate"]).write_text(
                "# delegate\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")

        publish = mock.Mock(return_value={"commits": 5, "pushed": True,
                                          "pr_url": "https://gh/pr/433", "note": "opened"})
        payload = {"worker_mode": "delegate", "auto_merge": True}
        payload.update(payload_extra or {})
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="t", payload=payload)
        progress: list[str] = []
        with contextlib.ExitStack() as stack:
            for p in (
                mock.patch.object(repos, "ensure_local", return_value=self.tmp / "clone"),
                mock.patch.object(stages, "restore_main"),
                mock.patch.object(stages, "install_push_guard"),
                mock.patch.object(stages, "default_branch", return_value="main"),
                mock.patch.object(stages, "prepare_vault_branch", return_value=vp),
                mock.patch.object(stages, "remote_branch_exists", return_value=False),
                mock.patch.object(stages, "RUNS_DIR", self.runs),
                mock.patch.object(stages, "build_stage_argv", return_value=["true"]),
                mock.patch.object(stages, "cleanup_build_daemons",
                                  return_value={"ran": True, "note": ""}),
                mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_stage),
                mock.patch.object(stages, "publish_branch", publish),
                mock.patch.object(stages, "merge_pr", merge),
                mock.patch.object(stages, "gh_pr_state", return_value="OPEN"),
                mock.patch.object(StagedExecutor, "_prepare_vault_worktree",
                                  return_value=(self.r.vault, None)),
                mock.patch.object(StagedExecutor, "_rescue_vault_worktree"),
                mock.patch.object(StagedExecutor, "_remove_vault_worktree"),
                mock.patch.object(StagedExecutor, "_vault_head", return_value=""),
                mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")),
                mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
                mock.patch.object(StagedExecutor, "_acquire_checkout_lease",
                                  return_value=None),
                mock.patch.object(StagedExecutor, "_pipeline_for", return_value=("delegate",)),
                mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
                mock.patch("todo_system.worker.runner_heartbeat_start"),
            ):
                stack.enter_context(p)
            out = StagedExecutor().execute(msg, _agent(), progress=progress.append)
        return out, publish, progress

    def test_sync_commit_conflict_is_rebuilt_and_merged_closes_done(self):
        shas = self.r.branch_with_mixed_commits()
        gh = _GitHubMerge(self.r)
        out, publish, progress = self._run(gh)
        publish.assert_called_once()
        self.assertEqual(len(gh.calls), 2, progress)     # refused, then merged
        self.assertTrue(out.ok, out.summary)
        self.assertFalse(out.payload.get("review_required"), out.summary)
        self.r.fetch()
        tip = _git(self.r.vault, "rev-parse", f"origin/{BRANCH}")
        self.assertEqual(_rc(self.r.vault, "merge-base", "--is-ancestor", shas["w0"], tip), 0)
        self.assertEqual(_git(self.r.vault, "diff", "--name-only", "origin/main", tip).split(),
                         ["Notes/work.md"])
        self.assertTrue(any("rebuild:" in p for p in progress), progress)

    def test_real_conflict_parks_with_the_conflicting_files(self):
        self.r.branch_with_mixed_commits(real_conflicts_with_main=True)
        before = _git(self.r.vault, "ls-remote", "origin", BRANCH)
        gh = _GitHubMerge(self.r)
        out, publish, progress = self._run(gh)
        self.assertEqual(len(gh.calls), 1)                # no second merge
        self.assertTrue(out.payload.get("review_required"))
        self.assertIn("미머지", out.summary)
        self.assertIn("Notes/work.md", out.summary)
        self.assertEqual(out.payload.get("pr_url"), "https://gh/pr/433")
        self.assertEqual(_git(self.r.vault, "ls-remote", "origin", BRANCH), before)

    def test_non_conflict_refusal_parks_without_rebuilding(self):
        self.r.branch_with_mixed_commits()
        merge = mock.Mock(return_value={"merged": False,
                                        "note": "gh pr merge of worker/t unavailable (OSError)"})
        with mock.patch.object(vpr, "rebuild_and_merge") as rebuild:
            out, _, _ = self._run(merge)
        rebuild.assert_not_called()
        self.assertTrue(out.payload.get("review_required"))


class ExternalRepoNeverRebuildsTest(unittest.TestCase):
    """A code repo's PR conflict is real: the rebuild path is vault-only."""

    def test_gate_refuses_an_external_repo_even_on_a_conflict(self):
        self.assertFalse(StagedExecutor._vault_pr_rebuild_applies(
            "https://github.com/o/r.git", True, CONFLICT_NOTE))
        self.assertTrue(StagedExecutor._vault_pr_rebuild_applies(None, True, CONFLICT_NOTE))
        self.assertFalse(StagedExecutor._vault_pr_rebuild_applies(None, False, CONFLICT_NOTE))

    def test_external_delegate_run_never_calls_the_rebuild(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        runs = tmp / "runs"
        runs.mkdir()
        clone = tmp / "clone"
        clone.mkdir()

        def fake_stage(argv, **kw):
            d = next(runs.iterdir())
            (d / stages.STAGE_ARTIFACT["delegate"]).write_text("# delegate\n",
                                                               encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "", "")

        merge = mock.Mock(return_value={"merged": False, "note": CONFLICT_NOTE})
        payload = {"worker_mode": "delegate", "auto_merge": True,
                   "repo": "https://github.com/o/r.git"}
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="t", payload=payload)
        with contextlib.ExitStack() as stack:
            for p in (
                mock.patch.object(repos, "ensure_worktree", return_value=clone),
                mock.patch.object(stages, "restore_main"),
                mock.patch.object(stages, "install_push_guard"),
                mock.patch.object(stages, "default_branch", return_value="main"),
                mock.patch.object(stages, "prepare_external_branch",
                                  return_value=subprocess.CompletedProcess([], 0, "", "")),
                mock.patch.object(stages, "remote_branch_exists", return_value=False),
                mock.patch.object(stages, "RUNS_DIR", runs),
                mock.patch.object(stages, "build_stage_argv", return_value=["true"]),
                mock.patch.object(stages, "cleanup_build_daemons",
                                  return_value={"ran": True, "note": ""}),
                mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_stage),
                mock.patch.object(stages, "checkpoint_push",
                                  return_value={"pushed": False, "tip": None, "commits": 0,
                                                "note": ""}),
                mock.patch.object(stages, "rescue_uncommitted",
                                  return_value={"committed": False, "sha": None, "note": ""}),
                mock.patch.object(stages, "remove_worktree"),
                mock.patch.object(stages, "publish_branch"),
                mock.patch.object(stages, "merge_pr", merge),
                mock.patch.object(stages, "merge_landed", return_value=False),
                mock.patch.object(stages, "no_op_run", return_value=False),
                mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")),
                mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
                mock.patch.object(StagedExecutor, "_acquire_checkout_lease",
                                  return_value=None),
                mock.patch.object(StagedExecutor, "_pipeline_for", return_value=("delegate",)),
                mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
                mock.patch("todo_system.worker.runner_heartbeat_start"),
                mock.patch.object(runner_mod.followups, "append_followups"),
            ):
                stack.enter_context(p)
            rebuild = stack.enter_context(mock.patch.object(vpr, "rebuild_and_merge"))
            StagedExecutor().execute(msg, _agent(), progress=lambda _: None)
        rebuild.assert_not_called()
        merge.assert_not_called()


if __name__ == "__main__":
    unittest.main()
