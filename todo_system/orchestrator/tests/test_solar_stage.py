"""Tests for the Solar (Upstage) single-stage execution path.

Task ``macp-solar-run-single-stage-task``. The feature routes a task whose
frontmatter says ``agent: upstage`` through an in-engine OpenAI-compatible
tool-calling loop (``orchestrator.solar_stage``) for its quick/delegate stage,
while EVERY other task's ``claude -p`` argv/env stays byte-identical. The whole
suite is hermetic: ``urlopen`` is stubbed and the base URL points at a dead
port (mirroring ``test_macp_upstage_account``), so no test can reach the real
endpoint. A sentinel key is planted and every layer asserts it never leaks into
a returned dict, the stage stdout, or the artifact.

Grouped by the implementation phase that introduced each block:
  1. ``agent:`` frontmatter → payload wiring (routing becomes reachable).
  2. ``solar_stage._chat_once`` / ``_with_retry`` — one POST + 429 backoff.
  3. ``solar_stage.run`` — the bounded read/write/finish tool loop + artifact.
  4. ``stages.route_to_solar`` + the runner's ``_execute`` routing branch.
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from todo_system.macp import config
from todo_system.orchestrator import message as m
from todo_system.orchestrator import repos, solar_stage, stages, tasks
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator.registry import Agent
from todo_system.orchestrator.runner import StagedExecutor

SENTINEL = "up_SENTINELKEY0123456789"
DEAD = "http://127.0.0.1:1/v1"          # a port nothing listens on, as a second belt

_COLS = "slug, title, path, outcome, next_action, tags, worker_mode, status, is_parent"


class _Resp:
    """The context-manager shape ``urllib.request.urlopen`` returns."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(status: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://x/v1/chat/completions", status, "err", {}, io.BytesIO(body.encode()))


# ---- Phase 1: agent frontmatter → payload ------------------------------------

class AgentFromFrontmatterTest(unittest.TestCase):
    def test_lowercased_and_placeholder_is_none(self):
        self.assertEqual(repos.task_agent_from_fm({"agent": "Upstage"}), "upstage")
        self.assertEqual(repos.task_agent_from_fm({"agent": "claude"}), "claude")
        self.assertIsNone(repos.task_agent_from_fm({"agent": "-"}))
        self.assertIsNone(repos.task_agent_from_fm({}))
        self.assertIsNone(repos.task_agent_from_fm({"agent": "   "}))


class AgentPayloadWiringTest(unittest.TestCase):
    """`load_dispatchable` carries `agent` ONLY when the markdown sets it — a
    task without it yields a byte-identical payload (AC #1's claude invariant)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "todo.db"
        con = sqlite3.connect(self.db)
        con.execute(f"CREATE TABLE tasks ({_COLS})")
        con.executemany(
            f"INSERT INTO tasks ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
            [("t-plain", "Plain", "", "", "", "", "auto", "open", 0)])
        con.commit()
        con.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add(self, slug: str, body: str, mode: str = "delegate"):
        md = self.tmp / f"{slug}.md"
        md.write_text(body, encoding="utf-8")
        con = sqlite3.connect(self.db)
        con.execute(f"INSERT INTO tasks ({_COLS}) VALUES (?,?,?,?,?,?,?,?,?)",
                    (slug, slug, str(md), "", "", "", mode, "open", 0))
        con.commit()
        con.close()

    def test_agent_rides_the_payload_when_set(self):
        self._add("t-solar",
                  "---\nstatus: open\nworker_mode: delegate\nagent: Upstage\n---\n# t\n")
        by_slug = {t.slug: t for t in tasks.load_dispatchable(self.db)}
        self.assertEqual(by_slug["t-solar"].payload["agent"], "upstage")

    def test_absent_agent_leaves_the_payload_unchanged(self):
        [t] = [t for t in tasks.load_dispatchable(self.db) if t.slug == "t-plain"]
        self.assertNotIn("agent", t.payload)


# ---- Phase 2: _chat_once + _with_retry ---------------------------------------

_TOOLS = [{"type": "function",
           "function": {"name": "finish",
                        "parameters": {"type": "object", "properties": {}}}}]

OK_MESSAGE = {
    "model": "solar-pro4-260806",
    "choices": [{"message": {"role": "assistant", "content": "done",
                             "tool_calls": [{"id": "c1", "type": "function",
                                             "function": {"name": "finish",
                                                          "arguments": "{}"}}]}}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15},
}

RATE = solar_stage.UP.KIND_RATE_LIMIT


class ChatOnceTest(unittest.TestCase):
    def test_sends_openai_shape_with_key_in_header_only(self):
        seen = {}

        def fake(req, timeout=None):
            seen["url"] = req.full_url
            seen["headers"] = dict(req.headers)
            seen["body"] = json.loads(req.data.decode())
            return _Resp(OK_MESSAGE)

        with mock.patch.object(solar_stage.urllib.request, "urlopen", side_effect=fake):
            res = solar_stage._chat_once(
                [{"role": "user", "content": "hi"}], _TOOLS,
                model="solar-pro4-260806", api_key=SENTINEL, base=DEAD)

        self.assertTrue(res["ok"])
        self.assertEqual(seen["url"], "http://127.0.0.1:1/v1/chat/completions")
        self.assertEqual(seen["body"]["model"], "solar-pro4-260806")
        self.assertEqual(seen["body"]["messages"], [{"role": "user", "content": "hi"}])
        self.assertIn("tools", seen["body"])
        self.assertEqual(seen["body"]["tool_choice"], "auto")
        self.assertEqual(seen["headers"]["Authorization"], f"Bearer {SENTINEL}")
        self.assertNotIn(SENTINEL, json.dumps(seen["body"]))   # key only in the header
        self.assertEqual(res["tool_calls"][0]["function"]["name"], "finish")
        self.assertEqual(res["usage"]["total_tokens"], 15)

    def test_classifies_http_failures(self):
        for status, kind in [(401, "auth"), (403, "auth"), (429, "rate_limit"),
                             (500, "http"), (404, "http")]:
            with self.subTest(status=status), mock.patch.object(
                    solar_stage.urllib.request, "urlopen",
                    side_effect=_http_error(status, '{"error":"no"}')):
                res = solar_stage._chat_once([{"role": "user", "content": "x"}], None,
                                             model="m", api_key=SENTINEL, base=DEAD)
            self.assertFalse(res["ok"])
            self.assertEqual(res["error_kind"], kind)
            self.assertEqual(res["message"], None)

    def test_scrubs_the_key_from_an_echoing_server(self):
        # a failure body that quotes the rejected credential
        with mock.patch.object(solar_stage.urllib.request, "urlopen",
                               side_effect=_http_error(
                                   401, json.dumps({"error": f"bad key {SENTINEL}"}))):
            res = solar_stage._chat_once([{"role": "user", "content": "x"}], None,
                                         model="m", api_key=SENTINEL, base=DEAD)
        self.assertEqual(res["error_kind"], "auth")
        self.assertNotIn(SENTINEL, json.dumps(res))
        self.assertIn(solar_stage.UP.REDACTED, res["error"])
        # a success body that echoes the key in content AND a tool-call argument
        echo = {"choices": [{"message": {"content": f"key={SENTINEL}", "tool_calls": [
            {"id": "c", "type": "function", "function": {
                "name": "write_file",
                "arguments": json.dumps({"path": "p", "content": SENTINEL})}}]}}],
            "usage": {}}
        with mock.patch.object(solar_stage.urllib.request, "urlopen",
                               side_effect=lambda req, timeout=None: _Resp(echo)):
            res = solar_stage._chat_once([{"role": "user", "content": "x"}], None,
                                         model="m", api_key=SENTINEL, base=DEAD)
        self.assertNotIn(SENTINEL, json.dumps(res))


class WithRetryTest(unittest.TestCase):
    def test_two_429s_then_200_is_three_calls_and_two_sleeps(self):
        calls = {"n": 0}

        def fake(req, timeout=None):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise _http_error(429, '{"error":"too many requests"}')
            return _Resp(OK_MESSAGE)

        slept: list[float] = []
        with mock.patch.object(solar_stage.urllib.request, "urlopen", side_effect=fake):
            out = solar_stage._with_retry(
                lambda: solar_stage._chat_once(
                    [{"role": "user", "content": "hi"}], None,
                    model="m", api_key=SENTINEL, base=DEAD),
                sleep=slept.append)

        self.assertTrue(out["ok"])
        self.assertEqual(calls["n"], 3)        # exactly 3 HTTP calls
        self.assertEqual(len(slept), 2)        # exactly 2 backoff sleeps
        self.assertEqual(slept, [0.5, 1.0])    # base_delay * 2**attempt

    def test_persistent_429_gives_up_after_retries(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return solar_stage._fail(RATE, 429, "HTTP 429")

        slept: list[float] = []
        out = solar_stage._with_retry(fn, retries=5, sleep=slept.append)
        self.assertEqual(out["error_kind"], "rate_limit")
        self.assertEqual(calls["n"], 6)        # 1 initial + 5 retries
        self.assertEqual(len(slept), 5)

    def test_a_non_429_failure_is_not_retried(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return solar_stage._fail(solar_stage.UP.KIND_AUTH, 401, "HTTP 401")

        out = solar_stage._with_retry(fn, sleep=lambda _: None)
        self.assertEqual(out["error_kind"], "auth")
        self.assertEqual(calls["n"], 1)        # a 401 is not transient


# ---- Phase 3: the tool-calling loop (solar_stage.run) ------------------------

def _tool_resp(name, args_dict, call_id="c", total=7):
    """A scripted assistant response carrying exactly one tool call."""
    return {"choices": [{"message": {"role": "assistant", "content": None,
            "tool_calls": [{"id": call_id, "type": "function", "function": {
                "name": name, "arguments": json.dumps(args_dict)}}]}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": total}}


def _script(responses):
    """A urlopen stub that returns each scripted response in turn."""
    it = iter(responses)

    def fake(req, timeout=None):
        return _Resp(next(it))

    return fake


class RunLoopTest(unittest.TestCase):
    def setUp(self):
        self.wd = Path(tempfile.mkdtemp())
        self.art = self.wd / "delegate.md"

    def tearDown(self):
        shutil.rmtree(self.wd, ignore_errors=True)

    def _run(self, responses, **kw):
        with mock.patch.object(solar_stage.urllib.request, "urlopen",
                               side_effect=_script(responses)):
            return solar_stage.run(
                "delegate", task_prompt="edit the file", working_dir=str(self.wd),
                artifact_path=str(self.art), api_key=SENTINEL, base=DEAD,
                sleep=lambda _: None, **kw)

    def test_write_then_finish_edits_the_file_and_writes_the_artifact(self):
        r = self._run([
            _tool_resp("write_file", {"path": "README.md", "content": "new line\n"}),
            _tool_resp("finish", {"summary": "changed README"}),
        ])
        self.assertEqual(r.returncode, 0)                                   # finished
        self.assertEqual((self.wd / "README.md").read_text(encoding="utf-8"),
                         "new line\n")                                      # (a)
        self.assertTrue(self.art.exists())                                  # (b)
        art = self.art.read_text(encoding="utf-8")
        self.assertIn("README.md", art)
        self.assertIn("total=14", art)                 # 7 + 7 across the two calls
        self.assertIn("finish_summary: changed README", art)

    def test_the_key_never_appears_in_stdout_or_artifact(self):
        # a server that echoes the key in a tool-call argument
        r = self._run([
            _tool_resp("write_file", {"path": "f.txt", "content": f"k={SENTINEL}"}),
            _tool_resp("finish", {"summary": f"done {SENTINEL}"}),
        ])
        self.assertNotIn(SENTINEL, r.stdout)                                # (d)
        self.assertNotIn(SENTINEL, self.art.read_text(encoding="utf-8"))

    def test_a_path_escape_is_refused_and_not_written(self):
        outside = self.wd.parent / "escaped-by-solar.txt"
        r = self._run([
            _tool_resp("write_file", {"path": "../escaped-by-solar.txt",
                                      "content": "nope"}),
            _tool_resp("finish", {"summary": "tried to escape"}),
        ])
        self.assertFalse(outside.exists())                                  # (c)
        self.assertIn("escapes the working directory", r.stdout)
        self.assertIn("files_written: none", self.art.read_text(encoding="utf-8"))

    def test_max_iters_exhaustion_is_incomplete(self):
        # the model keeps calling read_file and never finishes → bound trips
        responses = [_tool_resp("read_file", {"path": "x"}, call_id=str(i))
                     for i in range(10)]
        r = self._run(responses, max_iters=3)
        self.assertEqual(r.returncode, 1)                                   # (e)
        art = self.art.read_text(encoding="utf-8")
        self.assertIn("status: incomplete", art)
        self.assertIn("error_kind: max_iters", art)
        self.assertIn("iterations: 3", art)

    def test_a_persistent_429_is_recorded_not_crashed(self):
        def fake(req, timeout=None):
            raise _http_error(429, '{"error":"slow down"}')

        with mock.patch.object(solar_stage.urllib.request, "urlopen", side_effect=fake):
            r = solar_stage.run("delegate", task_prompt="x", working_dir=str(self.wd),
                                artifact_path=str(self.art), api_key=SENTINEL,
                                base=DEAD, sleep=lambda _: None, max_iters=2)
        self.assertEqual(r.returncode, 1)
        art = self.art.read_text(encoding="utf-8")
        self.assertIn("error_kind: rate_limit", art)
        # the raw server body must NOT reach stdout (the runner scans it for a
        # claude capacity marker; a literal "rate limit" would mis-defer the run)
        self.assertNotIn("rate limit", r.stdout.lower())

    def test_no_key_returns_rc1_without_an_artifact(self):
        r = solar_stage.run("delegate", task_prompt="x", working_dir=str(self.wd),
                            artifact_path=str(self.art), api_key="", base=DEAD)
        self.assertEqual(r.returncode, 1)
        self.assertIn("UPSTAGE_API_KEY", r.stdout)
        self.assertFalse(self.art.exists())      # caller records blocked_on


# ---- Phase 4: stages.route_to_solar + runner _execute routing (AC #1) --------

class RouteToSolarTest(unittest.TestCase):
    def test_truth_table(self):
        self.assertTrue(stages.route_to_solar("upstage", "delegate"))
        self.assertTrue(stages.route_to_solar("Upstage", "quick"))     # case-insensitive
        self.assertFalse(stages.route_to_solar("upstage", "implement"))  # 5-stage lane
        self.assertFalse(stages.route_to_solar("upstage", "verify"))
        self.assertFalse(stages.route_to_solar("claude", "delegate"))   # claude untouched
        self.assertFalse(stages.route_to_solar(None, "quick"))
        self.assertFalse(stages.route_to_solar("", "delegate"))

    def test_solar_stages_are_the_single_stage_lanes_only(self):
        self.assertEqual(set(stages.SOLAR_STAGES), {"quick", "delegate"})


class BuildArgvUnchangedTest(unittest.TestCase):
    """AC #1 (structural half): build_stage_argv still assembles the claude
    invocation for quick/delegate — the function is not modified by the feature."""

    def test_quick_and_delegate_still_build_claude_argv(self):
        from todo_system.orchestrator import model_select
        with mock.patch.object(model_select, "resolve_model", return_value="m-fixed"):
            for stage in ("delegate", "quick"):
                argv = stages.build_stage_argv(
                    stage, task_id="t", run_id="r",
                    run_dir=Path("/v/Worker/runs/x"), branch="worker/t")
                self.assertEqual(argv[0], "claude")
                self.assertEqual(argv[1], "-p")
                self.assertIn("--model", argv)
                self.assertIn("m-fixed", argv)
                self.assertIn("--max-turns", argv)


def _agent() -> Agent:
    return Agent(id="w", kind="ai-local", capabilities=["git"], repo="~/Worker/x")


class ExecuteRoutingTest(unittest.TestCase):
    """Drive a full VAULT delegate run through StagedExecutor and prove routing:
    an `agent: upstage` task calls `solar_stage.run` and NOT
    `run_stage_subprocess`; a claude task does the inverse (the claude argv/env
    path is byte-identical because that branch is simply never taken for solar).
    Mirrors test_delegate_merge's mocking — no real gh/push/subprocess."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runs = self.tmp / "runs"
        self.runs.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_dir(self) -> Path:
        dirs = list(self.runs.iterdir())
        self.assertEqual(len(dirs), 1, dirs)
        return dirs[0]

    def _drive(self, *, agent, solar_acct):
        vp = subprocess.CompletedProcess([], 0, "", "")
        vp.problem = None
        vp.orphaned_commits = None

        def write_art(*a, **kw):
            # both the solar and claude stubs write delegate.md so the stage loop
            # reaches the success return; the ASSERTION is which stub ran.
            (self._run_dir() / stages.STAGE_ARTIFACT["delegate"]).write_text(
                "# delegate\n", encoding="utf-8")
            return subprocess.CompletedProcess(["x"], 0, "stage stdout", "")

        solar_run = mock.Mock(side_effect=write_art)
        claude_run = mock.Mock(side_effect=write_art)
        payload = {"worker_mode": "delegate", "auto_merge": True}
        if agent:
            payload["agent"] = agent
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="t", payload=payload)
        with contextlib.ExitStack() as stack:
            for p in (
                mock.patch.object(stages, "restore_main"),
                mock.patch.object(stages, "install_push_guard"),
                mock.patch.object(stages, "default_branch", return_value="main"),
                mock.patch.object(stages, "prepare_vault_branch", return_value=vp),
                mock.patch.object(stages, "remote_branch_exists", return_value=False),
                mock.patch.object(stages, "RUNS_DIR", self.runs),
                mock.patch.object(stages, "build_stage_argv", return_value=["true"]),
                mock.patch.object(stages, "cleanup_build_daemons",
                                  return_value={"ran": True, "note": ""}),
                mock.patch.object(stages, "run_stage_subprocess", claude_run),
                mock.patch.object(solar_stage, "run", solar_run),
                mock.patch.object(stages, "publish_branch",
                                  mock.Mock(return_value={"pr_url": None, "note": ""})),
                mock.patch.object(stages, "merge_pr",
                                  mock.Mock(return_value={"merged": True, "note": ""})),
                mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")),
                mock.patch.object(StagedExecutor, "_solar_account", return_value=solar_acct),
                mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
                mock.patch.object(StagedExecutor, "_acquire_checkout_lease", return_value=None),
                mock.patch.object(StagedExecutor, "_pipeline_for", return_value=("delegate",)),
                mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
                mock.patch("todo_system.worker.runner_heartbeat_start"),
            ):
                stack.enter_context(p)
            out = StagedExecutor().execute(msg, _agent(), progress=lambda _: None)
        return out, solar_run, claude_run

    def test_upstage_delegate_routes_to_solar(self):
        acct = config.Account(name="solar", agent="upstage")
        out, solar_run, claude_run = self._drive(agent="upstage", solar_acct=acct)
        self.assertTrue(out.ok, out.summary)
        solar_run.assert_called_once()
        claude_run.assert_not_called()
        # solar_stage.run is handed the delegate artifact path + the account
        _, kw = solar_run.call_args
        self.assertEqual(kw["account"], acct)
        self.assertEqual(Path(kw["artifact_path"]).name, "delegate.md")

    def test_claude_delegate_still_uses_the_subprocess_path(self):
        out, solar_run, claude_run = self._drive(agent=None, solar_acct=None)
        self.assertTrue(out.ok, out.summary)
        claude_run.assert_called_once()        # the unchanged claude argv path
        solar_run.assert_not_called()

    def test_upstage_agent_but_no_account_falls_back_to_claude(self):
        # `agent: upstage` with NO upstage account resolved (solar_acct=None) must
        # NOT route to solar — the runner gates on an actual account too.
        out, solar_run, claude_run = self._drive(agent="upstage", solar_acct=None)
        self.assertTrue(out.ok, out.summary)
        claude_run.assert_called_once()
        solar_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
