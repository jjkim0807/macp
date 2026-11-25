"""Repo playbooks reach the stages that need them, and no others.

Two halves of the real path, end to end with only the process boundary faked:

* the runner rebuilds ``Memory/playbooks/<repo>.md`` before the first stage of an
  external-repo run (``stages.record_playbook``, next to ``record_related``) —
  and a refresh that blows up changes nothing about the run;
* the claude backend's ``run_stage`` → ``stages.build_stage_argv`` puts that
  file into the implement and verify prompts of THAT repo's tasks. Another
  repo's task, a vault task (no repo) and the report stage get nothing, so their
  argv is byte-identical to what it was before playbooks existed.

Harness: ``test_runner_related.py``'s sandbox (every vault-touching call mocked,
``RUNS_DIR`` in a tmp dir) and ``stages.run_stage_subprocess`` replaced by a
recorder, so no ``claude`` process is started.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system import playbooks as pb
from todo_system.orchestrator import agent_backend
from todo_system.orchestrator import message as m
from todo_system.orchestrator import repos, stages
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator.registry import Agent
from todo_system.orchestrator.runner import StagedExecutor

REPO = "git@github.com:example/acme-app.git"
OTHER = "https://github.com/example/other-svc.git"
SLUG = "acme-12-sample"
FAIL_VERIFY = """---
overall_status: fail
---

## Conclusion

**FAIL.** `./gradlew :app:testDebugUnitTest` fails to compile: `FakeBackend.kt:36` declares
`vararg results: Result<Reply>`, which Kotlin prohibits for a value class.
"""
PASS_VERIFY = "---\noverall_status: pass\n---\n\n## Conclusion\n\nPASS. green.\n"
PASS_LOG = "- commit: dc0efb8 — fix(app): use List instead of value-class vararg in FakeBackend\n"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed(root: Path) -> None:
    """One acme-app task with one FAIL→PASS pair (a recent one, so it never expires)."""
    _write(root, f"Todo/items/{SLUG}.md",
           f"---\ntags:\n  - todo\nstatus: done\nrepo: \"{REPO}\"\n---\n\n# acme 12\n")
    today = dt.date.today().strftime("%Y%m%d")
    _write(root, f"Worker/runs/{today}T000001-{SLUG}/verify.md", FAIL_VERIFY)
    _write(root, f"Worker/runs/{today}T000002-{SLUG}/verify.md", PASS_VERIFY)
    _write(root, f"Worker/runs/{today}T000002-{SLUG}/run.log", PASS_LOG)


def _agent() -> Agent:
    return Agent(id="worker-sample", kind="ai-local", capabilities=["git"],
                 repo="~/Worker/sample")


def _prepared() -> subprocess.CompletedProcess:
    cp = subprocess.CompletedProcess(args=["git", "checkout", "-B"], returncode=0,
                                     stdout="", stderr="")
    cp.orphaned_commits = None
    return cp


class PlaybookWiringTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.vault = self.tmp / "vault"
        self.vault.mkdir()
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        self.clone = self.tmp / "clone"
        self.clone.mkdir()
        _seed(self.vault)
        patcher = mock.patch.object(pb, "VAULT_ROOT", self.vault)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- the prompt half: ClaudeAgentBackend.run_stage → build_stage_argv --------

    def _prompt(self, stage: str, repo: str | None) -> str:
        seen: dict = {}

        def record(argv, **_kw):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(stages, "run_stage_subprocess", side_effect=record):
            agent_backend.ClaudeAgentBackend().run_stage(
                stage, task_id=SLUG, run_id=f"20261125T000000-{SLUG}",
                run_dir=self.runs / "r", branch=f"worker/{SLUG}", repo=repo,
                working_dir=str(self.clone) if repo else None, worker_root=self.tmp / "none")
        argv = seen["argv"]
        self.assertEqual(argv[:2], ["claude", "-p"])
        return argv[2]

    def test_implement_and_verify_prompts_carry_this_repos_playbook(self):
        self.assertTrue(pb.refresh(REPO))
        for stage in stages.PLAYBOOK_STAGES:
            with self.subTest(stage=stage):
                prompt = self._prompt(stage, REPO)
                self.assertIn("## Repo playbook — acme-app", prompt)
                self.assertIn("[compile]", prompt)
                self.assertIn("use List instead of value-class vararg", prompt)
                section = prompt[prompt.index("## Repo playbook"):]
                self.assertLessEqual(len(section.split("\n\n## ")[0].encode("utf-8")), pb.MAX_BYTES)
        self.assertEqual(stages.PLAYBOOK_STAGES, ("implement", "verify"))

    def test_another_repo_a_vault_task_and_the_report_stage_get_nothing(self):
        self.assertTrue(pb.refresh(REPO))
        for stage, repo in (("implement", OTHER), ("verify", OTHER), ("implement", None),
                            ("report", REPO)):
            with self.subTest(stage=stage, repo=repo):
                self.assertNotIn("Repo playbook", self._prompt(stage, repo))

    def test_without_a_playbook_file_the_prompt_is_byte_identical(self):
        with_nothing = self._prompt("implement", REPO)
        self.assertNotIn("Repo playbook", with_nothing)
        with mock.patch.object(stages, "PLAYBOOK_STAGES", ()):
            self.assertEqual(self._prompt("implement", REPO), with_nothing)

    # -- the refresh half: the runner, before the first stage -------------------

    @contextlib.contextmanager
    def _sandbox(self):
        with mock.patch.object(repos, "ensure_worktree", return_value=self.clone), \
             mock.patch.object(stages, "restore_main"), \
             mock.patch.object(stages, "install_push_guard"), \
             mock.patch.object(stages, "default_branch", return_value="main"), \
             mock.patch.object(stages, "prepare_external_branch", return_value=_prepared()), \
             mock.patch.object(stages, "remove_worktree"), \
             mock.patch.object(stages, "RUNS_DIR", self.runs), \
             mock.patch.object(stages, "record_related", return_value=None), \
             mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")), \
             mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None), \
             mock.patch.object(StagedExecutor, "_pipeline_for", return_value=()), \
             mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}):
            yield

    def _execute(self, **payload) -> tuple[object, list[str]]:
        base = {"repo": REPO, "auto_merge": False, "summary": "acme 12"}
        base.update(payload)
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id=SLUG, payload=base)
        lines: list[str] = []
        with self._sandbox():
            out = StagedExecutor().execute(msg, _agent(), progress=lines.append)
        return out, lines

    def test_the_runner_rebuilds_the_playbook_before_the_first_stage(self):
        path = self.vault / "Memory" / "playbooks" / "acme-app.md"
        self.assertFalse(path.exists())
        out, lines = self._execute()
        self.assertTrue(out.ok, out.summary)
        self.assertTrue(path.is_file())
        self.assertIn("[compile]", path.read_text(encoding="utf-8"))
        self.assertIn("playbook: acme-app — 1 line(s) → Memory/playbooks/acme-app.md", lines)
        # an unchanged playbook is not rewritten and says nothing the second time
        out, lines = self._execute()
        self.assertTrue(out.ok, out.summary)
        self.assertFalse([ln for ln in lines if ln.startswith("playbook:")])

    def test_a_refresh_that_raises_changes_nothing_about_the_run(self):
        with mock.patch.object(pb, "refresh", side_effect=RuntimeError("boom")):
            out, lines = self._execute()
        self.assertTrue(out.ok, out.summary)
        self.assertFalse([ln for ln in lines if ln.startswith("playbook:")])

    def test_the_refresh_is_asked_for_the_runs_repo_only(self):
        calls: list = []
        with mock.patch.object(stages, "record_playbook", side_effect=calls.append):
            self._execute()
        self.assertEqual(calls, [REPO])


if __name__ == "__main__":
    unittest.main()
