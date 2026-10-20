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

import io
import json
import shutil
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import repos, solar_stage, tasks

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


if __name__ == "__main__":
    unittest.main()
