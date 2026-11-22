"""Triage card actions (todo_system/orchestrator/triage_actions.py).

Each action must go through a path that already exists — `gh pr merge --squash`
on the task's own PR, `resume.schedule` (the body of `cli resume`), a committed
`status: cancelled`, a committed 「사용자 지시」 line + resume — and must never
leave a half-written vault behind. The md tests run against a real git repo in a
tmp dir; gh is always a fake (no test reaches GitHub).

`answer` (the decision card's action) is held to one more rule: it only records.
AnswerTests pins that a choice is written to the md and, for a human assignment,
replied to the orchestrator once — and that nothing else runs, whatever the
choice says.
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

from todo_system.orchestrator import message, resume, triage_actions
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


# --- answer: record a decision ---------------------------------------------------------

OPTION_B = "(b) split a separate budget: API only"
DECISION_MD = ("---\ntags:\n  - todo\n  - decision\nstatus: open\nupdated: 2026-11-20\n"
               "worker_mode: manual\nneeds_decision: true\n"
               'next_action: "Pick a budget plan"\n'
               'scheduled: "2026-11-24 10:00~10:30"\n'
               "decision_options:\n"
               '  - "(a) raise the cap to 30"\n'
               f'  - "{OPTION_B}"\n'
               '  - "(c) hold"\n'
               "---\n\n# demo\n\n- [ ] pick\n\n## Progress Log\n\n- 2026-11-20: created\n")


class AnswerTests(_GitVault):
    """`answer` records what the person chose on a decision task — and does
    nothing else, whatever the choice says."""

    def setUp(self):
        super().setUp()
        self.inbox = self.root / "Worker" / "agents" / "orchestrator" / "inbox"
        self.ledger_json = self.root / "Worker" / "assignments" / f"{SLUG}.json"
        self.write_md(DECISION_MD, "decision task")

    def commit_all(self, subject: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", subject)

    def write_md(self, text: str, subject: str) -> None:
        self.md.write_text(text, encoding="utf-8")
        self.commit_all(subject)

    def assign_to_human(self, **kw) -> bytes:
        row = dict(task_id=SLUG, agent_id="owner", corr_id="c-7", state=AWAITING_HUMAN,
                   kind="human")
        row.update(kw)
        Ledger(self.root / "Worker" / "assignments").save(Assignment(**row))
        self.commit_all("assign")
        return self.ledger_json.read_bytes()

    def envelopes(self) -> list[message.Envelope]:
        return [message.Envelope.from_json(p.read_text(encoding="utf-8"))
                for p in sorted(self.inbox.glob("*.json"))]

    def text(self) -> str:
        return self.md.read_text(encoding="utf-8")

    def test_answer_records_the_choice_and_lowers_the_marker(self):
        ok, msg = self.act("answer", text=OPTION_B)
        self.assertTrue(ok, msg)
        self.assertIn("결정을 기록했어요", msg)
        self.assertIn(OPTION_B, msg)
        self.assertSentence(msg)
        text = self.text()
        self.assertEqual(text, (
            "---\ntags:\n  - todo\nstatus: open\nupdated: 2026-11-21\n"
            "worker_mode: manual\nneeds_decision: false\n"
            f'next_action: "결정 반영 — {OPTION_B}"\n'
            'scheduled: "2026-11-24 10:00~10:30"\n'
            "---\n\n# demo\n\n- [ ] pick\n\n## Progress Log\n\n- 2026-11-20: created\n"
            f"- {TODAY}: 결정(대시보드): {OPTION_B}\n"))
        self.assertEqual(self.head_files(), [f"Todo/items/{SLUG}.md"])
        self.assertEqual(self.git("log", "-1", "--format=%s").strip(),
                         f"chore: record decision for {SLUG} (dashboard)")
        self.assertEqual(self.git("status", "--porcelain"), "")
        # the md it wrote is no longer a decision anyone can answer
        from todo_system.orchestrator import triage
        triage._CACHE.clear()
        self.assertIsNone(triage.decision_card(SLUG, items_dir=self.items))

    def test_answer_never_closes_merges_or_resumes(self):
        # The boundary: a choice is recorded, never carried out. Wording that
        # names an irreversible step changes nothing about what runs.
        ledger_before = self.ledger_json.read_bytes()
        choices = ("(a) delete it now", "proceed with the payment",
                   "sign the 1-year commitment", "지금 삭제", "결제 진행", "1년 약정",
                   "merge and deploy to production")
        for choice in choices:
            self.write_md(DECISION_MD, f"reset for {choice}")
            head = self.git("rev-parse", "HEAD").strip()
            ran: list[list[str]] = []

            def recording(argv, **kw):
                ran.append(list(argv))
                return subprocess.run(argv, **kw)

            schedule = mock.Mock(side_effect=AssertionError("answer must not reserve a resume"))
            reply = mock.Mock(side_effect=AssertionError("no human assignment to answer"))
            ok, msg = self.act("answer", text=choice, run=recording, schedule=schedule,
                               reply=reply)
            self.assertTrue(ok, (choice, msg))
            # only git ran, and only to check the checkout and commit the one md
            self.assertTrue(ran, choice)
            for argv in ran:
                self.assertEqual(argv[:3], ["git", "-C", str(self.root)], argv)
                self.assertIn(argv[3], ("rev-parse", "ls-files", "commit"), argv)
            self.assertEqual([a[3] for a in ran].count("commit"), 1, choice)
            schedule.assert_not_called()
            reply.assert_not_called()
            self.assertFalse(resume.record_path(self.root, SLUG).exists(), choice)
            self.assertEqual(self.ledger_json.read_bytes(), ledger_before, choice)
            text = self.text()
            self.assertIn("\nstatus: open\n", text)                 # never closed
            self.assertTrue(text.endswith(f"- {TODAY}: 결정(대시보드): {choice}\n"), choice)
            self.assertEqual(self.git("rev-parse", "HEAD~1").strip(), head)   # exactly one commit
            self.assertEqual(self.head_files(), [f"Todo/items/{SLUG}.md"])
            self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertFalse(self.inbox.exists())

    def test_tags_in_every_form_lose_the_decision_tag(self):
        head = "status: open\nneeds_decision: true\n---\n\n# demo\n"
        cases = (("tags: [todo, decision]\n", "tags:\n  - todo\n"),
                 ("tags: todo, Decision, work\n", "tags:\n  - todo\n  - work\n"),
                 ("tags:\n  - Decision\n  - todo\n", "tags:\n  - todo\n"),
                 ("tags:\n  - decision\n", ""),
                 ("tags:\n  - todo\n  - decisions-log\n", "tags:\n  - todo\n  - decisions-log\n"))
        for before, after in cases:
            self.write_md(f"---\n{before}{head}", f"tags {before!r}")
            ok, msg = self.act("answer", text="yes")
            self.assertTrue(ok, (before, msg))
            self.assertTrue(self.text().startswith(f"---\n{after}status: open\n"),
                            (before, self.text()))

    def test_a_tag_only_decision_gets_the_flag_written(self):
        self.write_md("---\ntags:\n  - todo\n  - decision\nstatus: open\n---\n\n# demo\n", "tag only")
        ok, msg = self.act("answer", text="go with the second one")
        self.assertTrue(ok, msg)
        self.assertEqual(self.text(), (
            "---\ntags:\n  - todo\nstatus: open\nneeds_decision: false\n"
            'next_action: "결정 반영 — go with the second one"\n'
            f"updated: {TODAY}\n---\n\n# demo\n\n## Progress Log\n\n"
            f"- {TODAY}: 결정(대시보드): go with the second one\n"))

    def test_the_answer_is_one_line_and_next_action_stays_parseable(self):
        from todo_system.session_log import parse_frontmatter
        raw = '  say "no"\tto [[plan-b]] \\ and: # move on  '
        ok, msg = self.act("answer", text=raw)
        self.assertTrue(ok, msg)
        line = 'say "no" to [[plan-b]] \\ and: # move on'
        text = self.text()
        self.assertTrue(text.endswith(f"- {TODAY}: 결정(대시보드): {line}\n"))     # verbatim
        fm, _ = parse_frontmatter(text)
        self.assertEqual(fm["next_action"], "결정 반영 — say 'no' to [[plan-b]]  and: # move on")
        self.assertEqual(fm["needs_decision"], "false")
        long = "x" * 500
        self.write_md(DECISION_MD, "reset")
        ok, msg = self.act("answer", text=long)
        self.assertTrue(ok, msg)
        fm, _ = parse_frontmatter(self.text())
        self.assertLessEqual(len(fm["next_action"]), 200)
        self.assertTrue(self.text().endswith(f"결정(대시보드): {long}\n"))
        self.assertLess(len(msg), 200)                      # the reply quotes a short form

    def test_human_assignment_gets_one_reply(self):
        ledger_before = self.assign_to_human()
        ok, msg = self.act("answer", text="(c) hold")
        self.assertTrue(ok, msg)
        (env,) = self.envelopes()
        self.assertEqual((env.type, env.to, env.frm, env.task_id, env.corr_id),
                         (message.NEEDS_INPUT, message.ORCHESTRATOR, "owner", SLUG, "c-7"))
        self.assertEqual(env.payload, {"note": "answered via dashboard: (c) hold"})
        self.assertEqual(self.ledger_json.read_bytes(), ledger_before)      # never hand-edited
        self.assertIn("결정(대시보드): (c) hold", self.text())
        # a second click finds no decision to answer: no second reply, no second line
        ok, msg = self.act("answer", text="(a) raise the cap to 30")
        self.assertFalse(ok)
        self.assertIn("지금 상태에서는 할 수 없는", msg)
        self.assertEqual(len(self.envelopes()), 1)
        self.assertEqual(self.text().count("결정(대시보드)"), 1)

    def test_the_reply_parks_the_row_again_and_closes_nothing(self):
        # What the orchestrator does with that reply (the real collect()): the
        # row is awaiting_human again with the answer as its note, and the todo
        # stays open. `done` would close the task; `progress` would hold its
        # checkout in flight for good.
        from todo_system.orchestrator.mailbox import FileMailbox
        from todo_system.orchestrator.orchestrator import Orchestrator
        from todo_system.orchestrator.registry import Agent, Registry
        self.assign_to_human()
        self.assertTrue(self.act("answer", text="(c) hold")[0])
        sent: list = []
        ledger = Ledger(self.root / "Worker" / "assignments")
        orch = Orchestrator(
            FileMailbox(self.root / "Worker" / "agents"),
            Registry([Agent(id="owner", kind="human", projects=["demo-*"])]), ledger,
            notify=lambda text, link=None: sent.append(text),
            alert_route=lambda text, **k: ("send", text))
        updated = orch.collect()
        self.assertEqual([a.task_id for a in updated], [SLUG])
        row = ledger.load(SLUG)
        self.assertEqual((row.state, row.kind, row.agent_id), (AWAITING_HUMAN, "human", "owner"))
        self.assertEqual(row.last_note, "answered via dashboard: (c) hold")
        self.assertEqual(row.history[-1]["type"], message.NEEDS_INPUT)
        self.assertIn("\nstatus: open\n", self.text())
        self.assertEqual(sent, [])
        self.assertEqual(self.envelopes(), [])              # consumed, not redelivered

    def test_no_reply_without_a_waiting_human_assignment(self):
        rows = (dict(agent_id="worker-default", kind="ai-local"),          # a worker's row
                dict(kind="human", state="done"),                          # already answered
                dict(kind="human", state="in_progress"),
                None)                                                      # no row at all
        for row in rows:
            if row is None:
                self.ledger_json.unlink()
                self.commit_all("no row")
            else:
                self.assign_to_human(**row)
            self.write_md(DECISION_MD + f"\n<!-- {row} -->\n", f"reset for {row}")
            ok, msg = self.act("answer", text="(c) hold")
            self.assertTrue(ok, (row, msg))
            self.assertFalse(self.inbox.exists(), row)

    def test_a_failed_reply_is_said_and_the_record_stays(self):
        self.assign_to_human()

        def broken(*a, **k):
            raise OSError("disk full at /tmp/x")

        with self.assertLogs("macp.dashboard", level="WARNING") as logs:
            ok, msg = self.act("answer", text="(c) hold", reply=broken)
        self.assertFalse(ok)
        self.assertIn("회신은 보내지 못했어요", msg)
        self.assertSentence(msg)
        self.assertIn("결정(대시보드): (c) hold", self.text())            # committed all the same
        self.assertEqual(self.head_files(), [f"Todo/items/{SLUG}.md"])
        self.assertTrue(any("reply not sent" in line and SLUG in line for line in logs.output),
                        logs.output)

    def test_a_task_that_is_not_a_decision_is_refused(self):
        for text in (MD, DECISION_MD.replace("needs_decision: true", "needs_decision: false")
                     .replace("  - decision\n", "")):
            self.write_md(text, "not a decision")
            ok, msg = self.act("answer", text="(c) hold")
            self.assertFalse(ok)
            self.assertIn("지금 상태에서는 할 수 없는", msg)
            self.assertSentence(msg)
            self.assertEqual(self.text(), text)
            self.assertEqual(self.git("status", "--porcelain"), "")
        ok, msg = triage_actions.run_action("answer", "gone", None, vault_root=self.root,
                                            items_dir=self.items, text="x", today=TODAY)
        self.assertFalse(ok)
        self.assertIn("태스크 파일이 없어", msg)

    def test_a_blank_answer_is_refused_before_any_write(self):
        for blank in (None, "", "  \t "):
            ok, msg = self.act("answer", text=blank)
            self.assertFalse(ok)
            self.assertIn("한 줄", msg)
        self.assertEqual(self.text(), DECISION_MD)

    def test_refused_off_main_and_rolled_back_on_a_failed_commit(self):
        self.assign_to_human()
        self.git("checkout", "-q", "-b", "worker/other")
        ok, msg = self.act("answer", text="(c) hold")
        self.assertFalse(ok)
        self.assertIn("main이 아니라", msg)
        self.assertEqual(self.text(), DECISION_MD)
        self.git("checkout", "-q", "main")
        hook = self.root / ".git" / "hooks" / "pre-commit"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        with self.assertLogs("macp.dashboard", level="WARNING"):
            ok, msg = self.act("answer", text="(c) hold")
        self.assertFalse(ok)
        self.assertIn("되돌렸어요", msg)
        self.assertEqual(self.text(), DECISION_MD)
        self.assertFalse(self.inbox.exists())               # no reply for a record that failed

    def test_an_answer_that_lands_inside_the_window_is_not_written_twice(self):
        # another writer answered between the click-time check and the lock
        answered = (DECISION_MD.replace("needs_decision: true", "needs_decision: false")
                    .replace("  - decision\n", ""))
        real = triage_actions._md_commit

        def racing(slug, vault_root, items_dir, run, patch, message):
            self.write_md(answered, "someone else answered")
            return real(slug, vault_root, items_dir, run, patch, message)

        with mock.patch.object(triage_actions, "_md_commit", racing), \
                self.assertLogs("macp.dashboard", level="WARNING"):
            ok, msg = self.act("answer", text="(c) hold")
        self.assertFalse(ok)
        self.assertIn("지금 상태에서는 할 수 없는", msg)
        self.assertEqual(self.text(), answered)
        self.assertFalse(self.inbox.exists())


if __name__ == "__main__":
    unittest.main()
