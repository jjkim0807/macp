"""A vault run must not move the live vault checkout off its default branch.

A task with no `repo:` runs against the vault itself. The runner used to prepare
its branch by checking `worker/<slug>` out IN the live vault and keeping HEAD
there for the whole pipeline. The live vault is also where a person, the planner
and the sync daemon commit, so everything they committed in that window landed
on the worker branch and dropped out of the working tree when the run switched
back (2026-11-17: a discovery run held the checkout for 24 minutes and eight
commits left the default branch that way).

Every test here drives `StagedExecutor.execute` over a real temporary repository
with a fake agent, and asks the one question that incident turns on: what does
`git symbolic-ref HEAD` say in the live vault WHILE a stage is running?

Only symbols that predate the fix are used, on purpose: against the old runner
each test must fail on its assertion, not on an import or a missing fixture.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system import worker
from todo_system.orchestrator import beta_publish, stages
from todo_system.orchestrator import message as m
from todo_system.orchestrator.runner import StagedExecutor

MAIN_REF = "refs/heads/main"


class LiveVaultHeadTest(unittest.TestCase):
    """The live vault's HEAD during a vault run, observed from inside each stage."""

    def _git(self, *args, check=True):
        return subprocess.run(["git", "-C", str(self.vault), *args],
                              check=check, capture_output=True, text=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        self.vault = self.tmp / "vault"
        subprocess.run(["git", "clone", str(self.origin), str(self.vault)],
                       check=True, capture_output=True)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            self._git("config", k, v)
        (self.vault / "main.txt").write_text("v1", encoding="utf-8")
        (self.vault / "Worker" / "assignments").mkdir(parents=True)
        (self.vault / "Worker" / "assignments" / "a.json").write_text(
            "{}\n", encoding="utf-8")
        (self.vault / "Todo" / "items").mkdir(parents=True)
        (self.vault / "Todo" / "items" / "t.md").write_text(
            "---\nstatus: open\n---\n\n# t\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-m", "init")
        self._git("push", "origin", "main")
        self._orig = (stages._VAULT_DATA_ROOT, stages.RUNS_DIR)
        stages._VAULT_DATA_ROOT = self.vault
        # OUTSIDE the vault, so run artifacts never show up as vault changes.
        stages.RUNS_DIR = self.tmp / "runs"
        stages.RUNS_DIR.mkdir(parents=True)
        self.heads: list[tuple[str, str]] = []     # (stage, live HEAD while it ran)
        self.gh: list[list[str]] = []

    def tearDown(self):
        stages._VAULT_DATA_ROOT, stages.RUNS_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _live_head(self) -> str:
        return self._git("symbolic-ref", "HEAD", check=False).stdout.strip()

    def _run(self, payload=None, during=None):
        """One pipeline run with a fake agent.

        Each stage records the live vault's HEAD, writes its artifact and — for
        `implement` — commits where the runner told it to work (`working_dir`).
        `during` maps a stage name to a callable run from inside that stage: the
        "another process" of the incident.
        """
        real_run = subprocess.run
        real_argv = stages.build_stage_argv
        pending: dict = {}

        def spy_argv(stage, **kw):
            pending["stage"], pending["kw"] = stage, kw
            return real_argv(stage, **kw)

        def fake_run(argv, **kw):
            argv = list(argv)
            if argv and argv[0] == "git":
                return real_run(argv, **kw)         # every real git goes through
            if argv and argv[0] == "gh":
                self.gh.append(argv)
                return subprocess.CompletedProcess(argv, 1, "", "no pull requests found")
            stage, skw = pending["stage"], pending["kw"]
            self.heads.append((stage, self._live_head()))
            if stage == "implement":
                # The agent's own commit, made where the run metadata points it.
                wd = Path(skw["working_dir"])
                (wd / "agent.md").write_text("agent work", encoding="utf-8")
                real_run(["git", "-C", str(wd), "add", "agent.md"],
                         check=True, capture_output=True)
                real_run(["git", "-C", str(wd), "commit", "-m", "agent work"],
                         check=True, capture_output=True)
            else:
                # implement's artifact is run.log, which the runner writes itself.
                (Path(skw["run_dir"]) / stages.STAGE_ARTIFACT[stage]).write_text(
                    "overall_status: pass\nmerged: no\n", encoding="utf-8")
            hook = (during or {}).get(stage)
            if hook is not None:
                hook()
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        msg = m.Envelope(to="w", type="TASK_ASSIGN", task_id="t", corr_id="C",
                         payload=payload or {"worker_mode": "auto"})
        with mock.patch.object(stages, "build_stage_argv", side_effect=spy_argv), \
             mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run), \
             mock.patch("subprocess.run", side_effect=fake_run):
            return StagedExecutor().execute(msg, mock.Mock(), lambda *_: None)

    def test_live_head_is_main_in_every_stage_of_an_auto_run(self):
        out = self._run()
        self.assertTrue(out.ok, out.summary)
        self.assertEqual([s for s, _ in self.heads], list(stages.STAGES_AUTO))
        self.assertEqual(
            self.heads, [(s, MAIN_REF) for s in stages.STAGES_AUTO],
            "a vault run moved the live vault's HEAD off the default branch while "
            "its stages ran")
        self.assertEqual(self._live_head(), MAIN_REF)

    def test_a_commit_made_in_the_live_vault_during_the_run_stays_on_main(self):
        """The incident itself: someone else commits in the live vault mid-run."""
        made: dict = {}

        def someone_else_commits():
            (self.vault / "note.md").write_text("written by a person", encoding="utf-8")
            self._git("add", "note.md")
            self._git("commit", "-m", "a person's commit")
            made["sha"] = self._git("rev-parse", "HEAD").stdout.strip()

        out = self._run(during={"implement": someone_else_commits})
        self.assertTrue(out.ok, out.summary)
        sha = made["sha"]
        self.assertEqual(
            self._git("merge-base", "--is-ancestor", sha, "main", check=False).returncode, 0,
            "a commit made in the live vault during a run did not land on main")
        self.assertEqual(
            self._git("merge-base", "--is-ancestor", sha, "worker/t",
                      check=False).returncode, 1,
            "the commit rode along on the run's worker branch")
        self.assertEqual((self.vault / "note.md").read_text(encoding="utf-8"),
                         "written by a person",
                         "the committed file left the live working tree with the run")

    def test_a_discover_run_leaves_the_live_head_alone(self):
        out = self._run(payload={"worker_mode": "discover"})
        self.assertTrue(out.ok, out.summary)
        self.assertEqual(self.heads, [("discover", MAIN_REF)],
                         "a discover run moved the live vault's HEAD")
        self.assertEqual(self._live_head(), MAIN_REF)

    def test_vault_sync_commits_on_main_while_a_run_is_in_flight(self):
        """The sync daemon's own guard ("HEAD is not main — deferring") is left as
        it is; a run in flight simply no longer trips it."""
        seen: dict = {}

        def sync_tick():
            (self.vault / "Worker" / "assignments" / "a.json").write_text(
                '{"state": "running"}\n', encoding="utf-8")
            buf = io.StringIO()
            with mock.patch.object(worker, "ROOT", self.vault), \
                 contextlib.redirect_stdout(buf):
                seen["n"] = worker.sync_vault_state()
            seen["out"] = buf.getvalue()

        out = self._run(during={"implement": sync_tick})
        self.assertTrue(out.ok, out.summary)
        self.assertNotIn("not main — deferring", seen["out"])
        self.assertGreaterEqual(seen["n"], 1, seen["out"])
        subjects = self._git("log", "--format=%s", "main").stdout
        self.assertIn("chore: sync vault state", subjects,
                      "the sync tick's commit is not on main")
        self.assertNotIn("chore: sync vault state",
                         self._git("log", "--format=%s", "main..worker/t").stdout)

    def test_beta_sweep_gate_is_open_while_a_run_is_in_flight(self):
        """Same for the beta sweep's "vault HEAD is main" gate: untouched, and no
        longer closed by a run."""
        config = self.tmp / "beta_publish_config.json"
        config.write_text(json.dumps(
            {"repos": {"demo-app": {"build_cmd": ["./gradlew"]}}}), encoding="utf-8")
        lines: list[str] = []
        seen: dict = {}

        def sweep_tick():
            seen["started"] = beta_publish.spawn_sweep_if_due(
                state_path=self.tmp / "beta_sweep.json", vault_root=self.vault,
                config_path=config, spawn=lambda: 4242, is_alive=lambda _p: False,
                progress=lines.append)

        out = self._run(during={"implement": sweep_tick})
        self.assertTrue(out.ok, out.summary)
        self.assertEqual([ln for ln in lines if "not main" in ln], [])
        self.assertIs(seen["started"], True, lines)


if __name__ == "__main__":
    unittest.main()
