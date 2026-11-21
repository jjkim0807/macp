"""Triage card actions (todo_system/orchestrator/triage_actions.py).

Each action must go through a path that already exists — `gh pr merge --squash`
on the task's own PR, `resume.schedule` (the body of `cli resume`), a committed
`status: cancelled`, a committed 「사용자 지시」 line + resume — and must never
leave a half-written vault behind. The md tests run against a real git repo in a
tmp dir; gh is always a fake (no test reaches GitHub).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import resume, triage_actions
from todo_system.orchestrator.assignment import AWAITING_HUMAN, Assignment, Ledger
from todo_system.vault_git_lock import vault_git_lock

SLUG = "demo-task"
PR = "https://github.com/example/app/pull/12"
TODAY = "2026-11-21"
_HANGUL = re.compile("[가-힣]")
_RAW = re.compile(r"HTTP \d{3}|http_\d|rc=|Traceback|GraphQL|stderr|/tmp/|\.git\b")
MD = ("---\ntags:\n  - todo\nstatus: open\nupdated: 2026-11-20\nworker_mode: auto\n---\n\n"
      "# demo\n\n- [ ] step\n\n## Progress Log\n\n- 2026-11-20: created\n")


class FakeGh:
    """Records every argv; answers `gh pr view` / `gh pr merge` like gh does."""

    def __init__(self, state="OPEN", head=f"worker/{SLUG}", view_rc=0, view_out=None,
                 merge_rc=0, merge_err=""):
        self.calls: list[list[str]] = []
        self.state, self.head, self.view_rc, self.view_out = state, head, view_rc, view_out
        self.merge_rc, self.merge_err = merge_rc, merge_err

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:3] == ["gh", "pr", "view"]:
            out = self.view_out if self.view_out is not None else json.dumps(
                {"state": self.state, "headRefName": self.head})
            return subprocess.CompletedProcess(argv, self.view_rc, out,
                                               "gh: HTTP 404 Not Found" if self.view_rc else "")
        if argv[:3] == ["gh", "pr", "merge"]:
            return subprocess.CompletedProcess(argv, self.merge_rc, "", self.merge_err)
        raise AssertionError(f"unexpected command {argv}")


class _Base(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {"MACP_LANGUAGE": "ko"})
        p.start()
        self.addCleanup(p.stop)

    def assertSentence(self, message: str):
        self.assertTrue(_HANGUL.search(message), message)
        self.assertIsNone(_RAW.search(message), message)


class MergeTests(_Base):
    def merge(self, gh, facts=None):
        return triage_actions.run_action("merge", SLUG, {"pr_url": PR} if facts is None else facts,
                                         vault_root="/nonexistent", items_dir="/nonexistent",
                                         run=gh)

    def test_merge_is_exactly_a_squash_of_the_tasks_own_pr(self):
        gh = FakeGh()
        ok, msg = self.merge(gh)
        self.assertTrue(ok)
        self.assertIn("머지했어요", msg)
        self.assertEqual(gh.calls, [
            ["gh", "pr", "view", "12", "--repo", "example/app", "--json", "state,headRefName"],
            ["gh", "pr", "merge", "12", "--repo", "example/app", "--squash"]])
        for argv in gh.calls:
            for flag in ("--admin", "--auto", "--delete-branch", "--merge", "--rebase"):
                self.assertNotIn(flag, argv)

    def test_a_retry_branch_counts_as_the_tasks_own(self):
        gh = FakeGh(head=f"worker/{SLUG}-r2")
        self.assertTrue(self.merge(gh)[0])
        self.assertEqual(len(gh.calls), 2)

    def test_someone_elses_pr_is_not_merged(self):
        for head in ("feature/x", f"worker/{SLUG}-other", f"worker/{SLUG}x", "worker/demo"):
            gh = FakeGh(head=head)
            ok, msg = self.merge(gh)
            self.assertFalse(ok, head)
            self.assertIn("이 태스크의 PR이 아니라서", msg)
            self.assertEqual(len(gh.calls), 1, head)        # view only, never merge

    def test_already_merged_and_closed(self):
        gh = FakeGh(state="MERGED")
        ok, msg = self.merge(gh)
        self.assertTrue(ok)
        self.assertIn("이미 머지된 PR", msg)
        self.assertEqual(len(gh.calls), 1)
        gh = FakeGh(state="CLOSED")
        ok, msg = self.merge(gh)
        self.assertFalse(ok)
        self.assertIn("닫혀 있어", msg)
        self.assertEqual(len(gh.calls), 1)

    def test_lookup_failures(self):
        for gh in (FakeGh(view_rc=1), FakeGh(view_out="not json"), FakeGh(view_out="[1]")):
            ok, msg = self.merge(gh)
            self.assertFalse(ok)
            self.assertIn("PR 상태를 확인하지 못했어요", msg)
            self.assertSentence(msg)

        def missing_gh(argv, **kw):
            raise FileNotFoundError("gh")

        ok, msg = self.merge(missing_gh)
        self.assertFalse(ok)
        self.assertSentence(msg)

    def test_merge_errors_become_sentences_and_stay_in_the_log(self):
        conflict = ("GraphQL: Pull Request is not mergeable: the merge commit cannot be cleanly "
                    "created. (mergePullRequest)")
        with self.assertLogs("macp.dashboard", level="WARNING") as logs:
            ok, msg = self.merge(FakeGh(merge_rc=1, merge_err=conflict))
        self.assertFalse(ok)
        self.assertIn("충돌이 있어", msg)
        self.assertSentence(msg)
        self.assertTrue(any("cannot be cleanly created" in line for line in logs.output))
        for err in ("HTTP 401: Bad credentials (https://api.github.com/graphql)",
                    "GraphQL: Pull Request is not mergeable: the base branch policy prohibits "
                    "the merge. (mergePullRequest)"):
            with self.assertLogs("macp.dashboard", level="WARNING"):
                ok, msg = self.merge(FakeGh(merge_rc=1, merge_err=err))
            self.assertFalse(ok)
            self.assertIn("머지하지 못했어요", msg)
            self.assertNotIn("충돌", msg)
            self.assertSentence(msg)

    def test_no_pr_url_never_calls_gh(self):
        gh = FakeGh()
        for facts in ({}, {"pr_url": "https://example.com/pr/1"}):
            ok, msg = self.merge(gh, facts)
            self.assertFalse(ok)
            self.assertSentence(msg)
        self.assertEqual(gh.calls, [])

    def test_unknown_action(self):
        ok, msg = triage_actions.run_action("deploy", SLUG, {}, vault_root="/x", items_dir="/x",
                                            run=FakeGh())
        self.assertFalse(ok)
        self.assertSentence(msg)


class _GitVault(_Base):
    """A real git repo shaped like the vault: one task md + ledger row on main."""

    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.items = self.root / "Todo" / "items"
        self.items.mkdir(parents=True)
        self.md = self.items / f"{SLUG}.md"
        self.md.write_text(MD, encoding="utf-8")
        Ledger(self.root / "Worker" / "assignments").save(
            Assignment(task_id=SLUG, agent_id="worker-default", corr_id="c", state=AWAITING_HUMAN))
        (self.root / ".gitignore").write_text("Worker/_state/\n", encoding="utf-8")
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "commit.gpgsign", "false")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "init")

    def git(self, *args) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True,
                              text=True, check=True).stdout

    def act(self, action, text=None, **kw):
        return triage_actions.run_action(action, SLUG, {}, vault_root=self.root,
                                         items_dir=self.items, text=text, today=TODAY, **kw)

    def head_files(self) -> list[str]:
        return [f for f in self.git("log", "-1", "--format=", "--name-only").splitlines() if f]


class CancelTests(_GitVault):
    def test_cancel_commits_status_cancelled_and_nothing_else(self):
        other = self.root / "Notes.md"
        other.write_text("staged elsewhere\n", encoding="utf-8")
        self.git("add", "Notes.md")                                  # staged, not ours
        (self.root / "scratch.txt").write_text("untracked\n", encoding="utf-8")
        before_head = self.git("rev-parse", "HEAD").strip()
        ok, msg = self.act("cancel")
        self.assertTrue(ok, msg)
        self.assertIn("취소했어요", msg)
        self.assertSentence(msg)
        text = self.md.read_text(encoding="utf-8")
        self.assertIn("\nstatus: cancelled\n", text)
        self.assertIn(f"\nupdated: {TODAY}\n", text)
        self.assertTrue(text.endswith(f"- 2026-11-20: created\n- {TODAY}: 대시보드에서 취소(사용자)\n"))
        self.assertEqual(self.head_files(), [f"Todo/items/{SLUG}.md"])
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(),
                         f"chore: cancel {SLUG} (dashboard)")
        self.assertEqual(self.git("rev-parse", "HEAD~1").strip(), before_head)
        status = self.git("status", "--porcelain").splitlines()
        self.assertIn("A  Notes.md", status)                          # still staged, uncommitted
        self.assertIn("?? scratch.txt", status)
        self.assertNotIn(f"Todo/items/{SLUG}.md", "\n".join(status))  # committed clean

    def test_a_task_without_progress_log_gets_one(self):
        self.md.write_text("---\nstatus: open\n---\n\n# demo\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "no log")
        self.assertTrue(self.act("cancel")[0])
        self.assertTrue(self.md.read_text(encoding="utf-8").endswith(
            f"# demo\n\n## Progress Log\n\n- {TODAY}: 대시보드에서 취소(사용자)\n"))

    def test_refused_off_main(self):
        self.git("checkout", "-q", "-b", "worker/other")
        ok, msg = self.act("cancel")
        self.assertFalse(ok)
        self.assertIn("main이 아니라", msg)
        self.assertEqual(self.md.read_text(encoding="utf-8"), MD)

    def test_refused_while_the_vault_git_lock_is_held(self):
        with mock.patch.object(triage_actions, "LOCK_TIMEOUT_S", 0.1), \
                vault_git_lock(self.root, 5) as held:
            self.assertTrue(held)
            ok, msg = self.act("cancel")
        self.assertFalse(ok)
        self.assertIn("다른 git 작업 중", msg)
        self.assertEqual(self.md.read_text(encoding="utf-8"), MD)

    def test_a_failed_commit_rolls_the_md_back(self):
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\necho 'refused by hook' >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        head = self.git("rev-parse", "HEAD").strip()
        with self.assertLogs("macp.dashboard", level="WARNING") as logs:
            ok, msg = self.act("cancel")
        self.assertFalse(ok)
        self.assertIn("되돌렸어요", msg)
        self.assertSentence(msg)
        self.assertEqual(self.md.read_text(encoding="utf-8"), MD)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), head)
        self.assertTrue(any("refused by hook" in line for line in logs.output))

    def test_an_untracked_or_missing_md_is_refused(self):
        new = self.items / "new-task.md"
        new.write_text(MD, encoding="utf-8")
        ok, msg = triage_actions.run_action("cancel", "new-task", {}, vault_root=self.root,
                                            items_dir=self.items, today=TODAY)
        self.assertFalse(ok)
        self.assertIn("아직 커밋되지 않았어요", msg)
        self.assertEqual(new.read_text(encoding="utf-8"), MD)
        ok, msg = triage_actions.run_action("cancel", "gone", {}, vault_root=self.root,
                                            items_dir=self.items, today=TODAY)
        self.assertFalse(ok)
        self.assertIn("태스크 파일이 없어", msg)

    def test_a_moving_md_is_never_overwritten(self):
        # every re-read differs from the read the patch was derived from
        reads = iter([f"{MD}- edit {i}\n" for i in range(6)])
        with mock.patch.object(triage_actions, "_read_text", lambda p: next(reads)):
            ok, msg = self.act("cancel")
        self.assertFalse(ok)
        self.assertIn("계속 바뀌는 중", msg)
        self.assertEqual(self.md.read_text(encoding="utf-8"), MD)


class ResumeTests(_GitVault):
    def test_resume_is_cli_resume(self):
        ok, msg = self.act("resume")
        self.assertTrue(ok, msg)
        self.assertIn("재개를 예약했어요", msg)
        record = json.loads(resume.record_path(self.root, SLUG).read_text(encoding="utf-8"))
        self.assertEqual((record["slug"], record["after_commit"], record["note"]),
                         (SLUG, "", "dashboard"))
        self.assertEqual(self.git("status", "--porcelain"), "")       # nothing committed/dirtied

    def test_resume_without_a_ledger_row(self):
        (self.root / "Worker" / "assignments" / f"{SLUG}.json").unlink()
        with self.assertLogs("macp.dashboard", level="WARNING"):
            ok, msg = self.act("resume")
        self.assertFalse(ok)
        self.assertIn("재개 예약을 만들지 못했어요", msg)
        self.assertSentence(msg)
        self.assertFalse(resume.record_path(self.root, SLUG).exists())


class InstructTests(_GitVault):
    def test_instruction_line_commit_and_resume_after_that_commit(self):
        ok, msg = self.act("instruct", text="옛 브랜치는 두고 worker/demo-task-r2로 push해")
        self.assertTrue(ok, msg)
        self.assertIn("지시를 남기고", msg)
        text = self.md.read_text(encoding="utf-8")
        self.assertTrue(text.endswith(
            f"- {TODAY}: 사용자 지시(대시보드): 옛 브랜치는 두고 worker/demo-task-r2로 push해\n"))
        self.assertIn("\nstatus: open\n", text)
        self.assertIn(f"\nupdated: {TODAY}\n", text)
        self.assertEqual(self.head_files(), [f"Todo/items/{SLUG}.md"])
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(),
                         f"chore: user instruction for {SLUG} (dashboard)")
        record = json.loads(resume.record_path(self.root, SLUG).read_text(encoding="utf-8"))
        self.assertEqual(record["after_commit"], self.git("rev-parse", "HEAD").strip())
        self.assertEqual(record["note"], "dashboard: user instruction")
        self.assertIn("patch_id", record)              # survives the sync rebase (resume.py)

    def test_resume_refused_after_the_commit(self):
        def refuse(*a, **k):
            raise resume.ResumeError("no ledger row")

        with self.assertLogs("macp.dashboard", level="WARNING"):
            ok, msg = self.act("instruct", text="다시 해", schedule=refuse)
        self.assertFalse(ok)
        self.assertIn("재개 예약은 못 했어요", msg)
        self.assertIn("사용자 지시(대시보드): 다시 해", self.md.read_text(encoding="utf-8"))
        self.assertEqual(self.head_files(), [f"Todo/items/{SLUG}.md"])

    def test_blank_instruction_is_refused_before_any_write(self):
        ok, msg = self.act("instruct", text="   ")
        self.assertFalse(ok)
        self.assertIn("한 줄", msg)
        self.assertEqual(self.md.read_text(encoding="utf-8"), MD)


if __name__ == "__main__":
    unittest.main()
