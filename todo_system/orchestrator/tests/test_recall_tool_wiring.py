"""Worker stages may search the vault's memory mid-run (`macp-recall`) without a
task `allowed_tools` edit, and what they looked up reaches the report.

The whitelist splits on spaces, so the CLI is granted through one space-free
PATH-wrapper rule, stages.RECALL_TOOL_RULE == `Bash(macp-recall:*)`. Three
things are pinned here, each on the path production takes:

* **who holds the rule** — implement / verify / quick / delegate, in the argv
  `build_stage_argv` builds and the claude backend hands to the subprocess;
  report and discover do not, and a task's own `allowed_tools` neither drops it
  nor carries it to a stage a task cannot widen;
* **what the stage is told and given** — the usage line on the run metadata,
  and the env the runner's stage loop passes (`MACP_RECALL_LOG` =
  `<run_dir>/recall.log`, `MACP_RECALL_TASK` = the task slug), stripped from
  every other stage;
* **what the report stage is handed** — the engine's count of this run's calls
  and cited results, in the report's own argv, zero included. The last test
  joins the three: a lookup made with the env the runner gave a stage lands in
  that run's log and comes out in that run's report argv.

The CLI itself (format, privacy boundary, the 4 KB cap, the log) is pinned in
todo_system/tests/test_recall.py.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system import graph_memory as gm
from todo_system import recall, task_validate
from todo_system.orchestrator import agent_backend, stages
from todo_system.orchestrator import message as m
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator.registry import Agent
from todo_system.orchestrator.runner import StagedExecutor

REPO_ROOT = Path(stages.__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "macp-recall"
RULE = stages.RECALL_TOOL_RULE
GRANTED = ("implement", "verify", "quick", "delegate")
HINT_MARK = "**Past failures and decisions (macp-recall)**"
TASK = "demo-task"
RUN_ID = "20260101T000000-demo-task"


def _others() -> list[str]:
    return [stage for stage in stages.STAGE_TOOLS if stage not in GRANTED]


def _argv(stage: str, run_dir: Path, **kw) -> list[str]:
    return stages.build_stage_argv(
        stage, task_id=TASK, run_id=RUN_ID, run_dir=run_dir, branch=f"worker/{TASK}",
        worker_root=run_dir / "no-worker-root", **kw)


def _allowed(argv: list[str]) -> list[str]:
    tools = argv[argv.index("--allowed-tools") + 1:]
    return tools[:tools.index("--add-dir")] if "--add-dir" in tools else tools


class RecallToolRuleTest(unittest.TestCase):

    def test_four_stages_grant_the_rule_once(self):
        self.assertEqual(stages.RECALL_TOOL_STAGES, GRANTED)
        for stage in GRANTED:
            with self.subTest(stage=stage):
                self.assertEqual(stages._safe_tools(stage).count(RULE), 1)

    def test_no_other_stage_gets_it(self):
        self.assertEqual(sorted(_others()), ["discover", "report"])
        for stage in _others():
            with self.subTest(stage=stage):
                self.assertNotIn(RULE, stages._safe_tools(stage))
        # research and plan are no longer stages of their own (implement plans),
        # so there is no row left that could hold it.
        self.assertNotIn("research", stages.STAGE_TOOLS)
        self.assertNotIn("plan", stages.STAGE_TOOLS)
        self.assertNotIn(RULE, stages.FALLBACK_RUNNER_TOOLS)
        self.assertNotIn(RULE, stages.INSPECT_TOOLS)

    def test_is_a_standing_rule(self):
        self.assertIn((RULE, stages.RECALL_TOOL_STAGES), stages._STANDING_RULES)

    def test_rule_is_one_space_free_safe_token(self):
        self.assertEqual(RULE, "Bash(macp-recall:*)")
        self.assertEqual(stages.RECALL_CLI, "macp-recall")
        self.assertEqual(WRAPPER.name, stages.RECALL_CLI)
        self.assertNotIn(" ", RULE)
        for bad in stages.FORBIDDEN_TOOL_SUBSTRINGS:
            self.assertNotIn(bad, RULE)
        self.assertEqual(task_validate._check_bash_patterns(RULE), [])

    def test_delegate_gets_no_broad_runner_with_it(self):
        granted = stages.STAGE_TOOLS["delegate"].split()
        for broad in ("Bash(uv:*)", "Bash(python3:*)", "Bash(python:*)", "Bash(bash:*)"):
            with self.subTest(rule=broad):
                self.assertNotIn(broad, granted)
        self.assertIn(RULE, granted)

    def test_verify_stays_read_only_with_the_rule(self):
        self.assertNotIn("Edit", stages._safe_tools("verify"))

    def test_task_allowed_tools_keep_the_rule(self):
        default = stages.STAGE_TOOLS["implement"].split()
        self.assertEqual(stages._safe_tools("implement", "Read,Bash(gcloud:*)"),
                         [*default, "Bash(gcloud:*)"])
        # Already listed by the task: exactly once.
        self.assertEqual(stages._safe_tools("implement", f"Read,{RULE}"), default)
        # A stage a task cannot widen keeps its own default, rule included.
        self.assertEqual(stages._safe_tools("verify", "Read,Bash(gcloud:*)"),
                         stages.STAGE_TOOLS["verify"].split())

    def test_a_task_cannot_hand_the_rule_to_another_stage(self):
        for stage in _others():
            with self.subTest(stage=stage):
                self.assertNotIn(RULE, stages._safe_tools(stage, f"Read,{RULE}"))


class RecallStageArgvTest(unittest.TestCase):
    """The argv a stage is really started with."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name) / RUN_ID
        self.run_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def test_argv_grants_the_rule_to_the_four_stages(self):
        for stage in GRANTED:
            with self.subTest(stage=stage):
                argv = _argv(stage, self.run_dir)
                self.assertEqual(_allowed(argv).count(RULE), 1)
                # … and the authoritative whitelist line in the prompt says so too.
                line = next(ln for ln in argv[2].splitlines()
                            if ln.startswith("- allowed tools (authoritative"))
                self.assertIn(RULE, line.split())

    def test_argv_of_every_other_stage_does_not(self):
        for stage in _others():
            with self.subTest(stage=stage):
                argv = _argv(stage, self.run_dir)
                self.assertNotIn(RULE, argv)
                self.assertNotIn("Bash(macp-recall", argv[2])
                self.assertNotIn(HINT_MARK, argv[2])

    def test_a_task_list_does_not_drop_it_from_the_argv(self):
        argv = _argv("implement", self.run_dir, allowed_tools="Read,Edit,Bash(gcloud:*)")
        tools = _allowed(argv)
        self.assertEqual(tools.count(RULE), 1)
        self.assertIn("Bash(gcloud:*)", tools)
        verify = _allowed(_argv("verify", self.run_dir, allowed_tools="Read,Bash(gcloud:*)"))
        self.assertEqual(verify.count(RULE), 1)
        self.assertNotIn("Bash(gcloud:*)", verify)
        # A task that lists the rule itself does not get it into the report.
        report = _argv("report", self.run_dir, allowed_tools=f"Read,{RULE}")
        self.assertNotIn(RULE, report)

    def test_the_claude_backend_starts_the_stage_with_that_argv(self):
        """One level up from build_stage_argv: what the runner's backend hands to
        the subprocess launcher."""
        started: dict[str, list[str]] = {}

        def fake_run(argv, **kw):
            started[stage] = list(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        backend = agent_backend.ClaudeAgentBackend()
        with mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run):
            for stage in (*GRANTED, *_others()):
                backend.run_stage(stage, task_id=TASK, run_id=RUN_ID, run_dir=self.run_dir,
                                  branch=f"worker/{TASK}",
                                  worker_root=self.run_dir / "no-worker-root",
                                  allowed_tools="Read,Bash(gcloud:*)")
        for stage in GRANTED:
            with self.subTest(stage=stage):
                self.assertEqual(_allowed(started[stage]).count(RULE), 1)
        for stage in _others():
            with self.subTest(stage=stage):
                self.assertNotIn(RULE, started[stage])

    def test_the_stage_is_told_to_look_it_up_before_retrying(self):
        for stage in GRANTED:
            with self.subTest(stage=stage):
                prompt = _argv(stage, self.run_dir)[2]
                meta = prompt[prompt.index("# Run metadata (injected by orchestrator)"):]
                (line,) = [ln for ln in meta.splitlines() if HINT_MARK in ln]
                self.assertIn("when you are stuck", line)
                self.assertIn("BEFORE trying again", line)
                self.assertIn('`macp-recall "<the symptom or question>" [--k 5]`', line)
                self.assertIn("title, path and a one-line gist", line)
                # where to name what it used: the stage's own artifact
                self.assertIn(f"name its path in `{stages.STAGE_ARTIFACT[stage]}`", line)
                # a host without an index must not cost the stage its turns
                self.assertIn("A non-zero exit means this host has no index to search: "
                              "carry on without it.", line)
                # in the metadata block, not in the stage prompt file
                self.assertNotIn(HINT_MARK, stages.load_stage_prompt(stage))

    def test_the_hint_is_the_same_line_recall_hint_returns(self):
        for stage in GRANTED:
            with self.subTest(stage=stage):
                self.assertIn(stages.recall_hint(stage), _argv(stage, self.run_dir)[2])


class RecallEnvTest(unittest.TestCase):

    def test_env_names_are_the_ones_the_cli_reads(self):
        self.assertEqual(stages.RECALL_LOG_ENV, recall.LOG_ENV)
        self.assertEqual(stages.RECALL_TASK_ENV, recall.TASK_ENV)
        self.assertEqual(stages.RECALL_LOG_FILENAME, recall.LOG_FILENAME)

    def test_with_recall_env_sets_only_the_granted_stages(self):
        base = {"PATH": "/bin", stages.RECALL_LOG_ENV: "/stale/recall.log",
                stages.RECALL_TASK_ENV: "stale-task"}
        snapshot = dict(base)
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / RUN_ID
            for stage in GRANTED:
                with self.subTest(stage=stage):
                    env = stages.with_recall_env(stage, base, run_dir, TASK)
                    self.assertEqual(env[stages.RECALL_LOG_ENV], str(run_dir / "recall.log"))
                    self.assertEqual(env[stages.RECALL_TASK_ENV], TASK)
                    self.assertEqual(env["PATH"], "/bin")
            for stage in _others():
                with self.subTest(stage=stage):
                    env = stages.with_recall_env(stage, base, run_dir, TASK)
                    self.assertNotIn(stages.RECALL_LOG_ENV, env)
                    self.assertNotIn(stages.RECALL_TASK_ENV, env)
                    self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(base, snapshot)            # never mutates its input

    def test_a_relative_run_dir_is_made_absolute(self):
        """The stage's shell moves between directories (`cd <checkout>`), so a
        relative log path would land somewhere else on the second call."""
        env = stages.with_recall_env("implement", {}, Path("Worker/runs") / RUN_ID, TASK)
        self.assertTrue(os.path.isabs(env[stages.RECALL_LOG_ENV]))
        self.assertEqual(env[stages.RECALL_LOG_ENV],
                         os.path.abspath(Path("Worker/runs") / RUN_ID / "recall.log"))


def _agent() -> Agent:
    return Agent(id="worker-sample", kind="ai-local", capabilities=["git"],
                 repo="~/Worker/sample")


class RecallEnvRunnerTest(unittest.TestCase):
    """The stage loop itself: each run stops at its first stage through the
    timeout path, the cheapest way out with every host seam patched off."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runs = self.tmp / "runs"
        self.runs.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stage_env(self, stage: str, *, keep: bool = False) -> tuple[dict, Path]:
        seen: dict = {}

        def fake_run(argv, **kw):
            seen["env"] = kw.get("env")
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout"))

        vp = subprocess.CompletedProcess([], 0, "", "")
        vp.problem = None
        vp.orphaned_commits = None
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id=TASK,
                         payload={"auto_merge": False, "model_tier": "heavy"})
        with contextlib.ExitStack() as stack:
            for p in (
                mock.patch.dict(os.environ, {stages.RECALL_LOG_ENV: "/stale/recall.log",
                                             stages.RECALL_TASK_ENV: "stale-task"}),
                mock.patch.object(stages, "restore_main"),
                mock.patch.object(stages, "default_branch", return_value="main"),
                mock.patch.object(stages, "prepare_vault_branch", return_value=vp),
                mock.patch.object(stages, "RUNS_DIR", self.runs),
                mock.patch.object(stages, "build_stage_argv", return_value=["true"]),
                mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")),
                mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
                mock.patch.object(StagedExecutor, "_acquire_checkout_lease", return_value=None),
                mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
                mock.patch.object(StagedExecutor, "_pipeline_for", return_value=(stage,)),
                mock.patch("todo_system.worker.runner_heartbeat_start"),
                mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run),
                mock.patch("subprocess.run", side_effect=fake_run),
            ):
                stack.enter_context(p)
            out = StagedExecutor().execute(msg, _agent(), progress=lambda _: None)
        self.assertTrue(out.payload.get("timed_out"), out.payload)
        (run_dir,) = list(self.runs.iterdir())
        if not keep:            # the next call must find one run directory again
            shutil.rmtree(self.runs)
            self.runs.mkdir()
        return seen["env"], run_dir

    def test_granted_stages_get_this_runs_log_and_task(self):
        for stage in GRANTED:
            with self.subTest(stage=stage):
                env, run_dir = self._stage_env(stage)
                self.assertEqual(env[stages.RECALL_LOG_ENV], str(run_dir / "recall.log"))
                self.assertTrue(run_dir.name.endswith(f"-{TASK}"))
                self.assertEqual(env[stages.RECALL_TASK_ENV], TASK)
                # the layers under it are still there
                self.assertEqual(env[stages.BROWSER_OWNER_ENV], run_dir.name)
                self.assertEqual(env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "1")

    def test_other_stages_get_neither_even_from_the_runners_own_env(self):
        for stage in ("report",):
            with self.subTest(stage=stage):
                env, _run_dir = self._stage_env(stage)
                self.assertNotIn(stages.RECALL_LOG_ENV, env)
                self.assertNotIn(stages.RECALL_TASK_ENV, env)

    def test_a_lookup_made_with_the_stage_env_reaches_the_report_argv(self):
        """The three parts joined: the env the runner's loop gave implement →
        the real CLI → this run's recall.log → the count in the report's argv."""
        env, run_dir = self._stage_env("implement", keep=True)
        vault = self.tmp / "vault"
        items = vault / "Todo" / "items"
        items.mkdir(parents=True)
        (items / "gradle-daemon-task.md").write_text(
            "---\noutcome: the build survives a gradle stop\n---\n\n# Gradle daemon task\n\n"
            "gradle daemon disappeared during the build\n", encoding="utf-8")
        (items / f"{TASK}.md").write_text(        # the running task's own document
            "---\noutcome: the demo\n---\n\n# Demo task\n\n"
            "gradle daemon disappeared during the build\n", encoding="utf-8")
        embed, dim, _name = gm.resolve_embedder(name="hash")
        con = gm.init_relations_db(vault / "Memory" / "relations.sqlite")
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                gm.embed_vault(con, vault_root=vault, embedder=embed, dim=dim)
        finally:
            con.close()

        handed = {k: env[k] for k in (stages.RECALL_LOG_ENV, stages.RECALL_TASK_ENV)}
        out = io.StringIO()
        with mock.patch.dict(os.environ, handed), contextlib.redirect_stdout(out):
            os.environ.pop("MACP_EMBEDDER", None)
            rc = recall.main(["gradle daemon disappeared", "--vault-root", str(vault)])

        self.assertEqual(rc, 0)
        self.assertIn("   path: Todo/items/gradle-daemon-task.md", out.getvalue())
        self.assertNotIn(f"Todo/items/{TASK}.md", out.getvalue())   # its own doc is left out
        (entry,) = recall.read_log(run_dir / "recall.log")          # in THIS run's directory
        self.assertEqual(entry["results"], ["Todo/items/gradle-daemon-task.md"])
        self.assertIn("`recall_calls: 1` and `recall_cited: 0`", _argv("report", run_dir)[2])
        # The stage names what it used in its artifact; the next count sees it.
        (run_dir / "run.log").write_text(
            "fixed the way Todo/items/gradle-daemon-task.md did\n", encoding="utf-8")
        self.assertIn("`recall_calls: 1` and `recall_cited: 1`", _argv("report", run_dir)[2])


class RecallReportTest(unittest.TestCase):
    """What the report stage is started with."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name) / RUN_ID
        self.run_dir.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def _log(self, *calls: dict) -> None:
        (self.run_dir / "recall.log").write_text(
            "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in calls), encoding="utf-8")

    def _line(self, argv: list[str]) -> str:
        (line,) = [ln for ln in argv[2].splitlines() if "**Recall usage" in ln]
        return line

    def test_a_run_that_never_called_recall_is_still_given_zeros(self):
        self.assertFalse((self.run_dir / "recall.log").exists())

        line = self._line(_argv("report", self.run_dir))

        self.assertIn("`recall_calls: 0` and `recall_cited: 0`", line)
        self.assertIn("zero included", line)
        self.assertIn("into the report.md frontmatter", line)

    def test_the_count_is_the_engines_over_this_runs_log_and_artifacts(self):
        self._log(
            {"ts": "t", "query": "gradle daemon", "k": 5,
             "results": ["Todo/items/alpha-task.md", "Notes/beta-note.md"]},
            {"ts": "t", "query": "room migration", "k": 5,
             "results": ["Notes/beta-note.md", "Memory/episodes/2026-01-02-worker-gamma.md",
                         f"Todo/items/{TASK}.md"]},
            {"ts": "t", "query": "broken", "k": 5, "results": [], "error": "no memory index"})
        (self.run_dir / "run.log").write_text(
            f"branch worker/{TASK}\nused Todo/items/alpha-task.md\n", encoding="utf-8")
        (self.run_dir / "verify.md").write_text(
            "the earlier failure is [[2026-01-02-worker-gamma]]\n", encoding="utf-8")

        line = self._line(_argv("report", self.run_dir))

        # 3 calls (the failed one included), 4 distinct records, 2 cited — the
        # task's own document is not one of them although run.log names its slug.
        self.assertIn("`recall_calls: 3` and `recall_cited: 2`", line)
        self.assertIn("called `macp-recall` 3 time(s) and were shown 4 distinct record(s)", line)
        self.assertEqual(recall.usage(self.run_dir, TASK),
                         {"calls": 3, "results": 4, "cited": 2})

    def test_only_the_report_stage_is_handed_the_count(self):
        self._log({"ts": "t", "query": "q", "k": 5, "results": ["Notes/beta-note.md"]})
        for stage in stages.STAGE_TOOLS:
            with self.subTest(stage=stage):
                prompt = _argv(stage, self.run_dir)[2]
                self.assertEqual("**Recall usage" in prompt, stage == "report")
        # … and it sits in the metadata block, after the report prompt.
        prompt = _argv("report", self.run_dir)[2]
        self.assertLess(prompt.index("# Run metadata (injected by orchestrator)"),
                        prompt.index("**Recall usage"))

    def test_the_claude_backend_starts_report_with_the_count(self):
        self._log({"ts": "t", "query": "q", "k": 5, "results": ["Notes/beta-note.md"]})
        (self.run_dir / "run.log").write_text("see beta-note\n", encoding="utf-8")
        started: list[list[str]] = []

        def fake_run(argv, **kw):
            started.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run):
            agent_backend.ClaudeAgentBackend().run_stage(
                "report", task_id=TASK, run_id=RUN_ID, run_dir=self.run_dir,
                branch=f"worker/{TASK}", worker_root=self.run_dir / "no-worker-root")

        (argv,) = started
        self.assertIn("`recall_calls: 1` and `recall_cited: 1`", self._line(argv))
        self.assertNotIn(RULE, argv)                # counted for it, not granted to it

    def test_a_count_that_cannot_be_taken_is_unknown_not_zero(self):
        self._log({"ts": "t", "query": "q", "k": 5, "results": ["Notes/beta-note.md"]})
        with mock.patch.object(recall, "usage", side_effect=RuntimeError("broken")):
            line = self._line(_argv("report", self.run_dir))

        self.assertIn("`recall_calls: unknown` and `recall_cited: unknown`", line)
        self.assertNotIn("recall_calls: 0", line)

    def test_the_report_template_has_both_keys_in_its_frontmatter(self):
        prompt = stages.load_stage_prompt("report")
        template = prompt[prompt.index("```markdown\n---\nstage: report"):]
        frontmatter = template[:template.index("\n---\n", len("```markdown\n---\n"))]
        for key in stages.RECALL_REPORT_KEYS:
            with self.subTest(key=key):
                self.assertIn(f"\n{key}: <number>", frontmatter)
                # the same key the metadata line tells the agent to write
                self.assertIn(f"`{key}: 0`", self._line(_argv("report", self.run_dir)))
        self.assertEqual(stages.RECALL_REPORT_KEYS, ("recall_calls", "recall_cited"))
        self.assertIn("`0` included", prompt)


if __name__ == "__main__":
    unittest.main()
