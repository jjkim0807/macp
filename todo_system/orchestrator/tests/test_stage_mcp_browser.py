"""macp-browser MCP server wiring: paths.browser_mcp_server + the browser stages.

Every test pins the knob itself (env popped, `start=` seam or a patched
resolver), so nothing here depends on the host's env or .macp/config.toml.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from todo_system import paths
from todo_system.orchestrator import stages

_KNOB_ENVS = ("MACP_BROWSER_MCP_SERVER", "MACP_BROWSER_NODE",
              *stages.BROWSER_MCP_PASSTHROUGH_ENVS)
_INSTANCE_ENV = {"MACP_BROWSER_INSTANCE": "s-test",
                 "MACP_BROWSER_DIR": "/tmp/macp-browser-test",
                 "MACP_BROWSER_CDP": "http://127.0.0.1:9399"}
_RUN_ID = "20260101T000000-t-slug"
_MCP_STAGES = ("implement", "verify", "quick", "delegate")
_NON_MCP_STAGES = ("research", "plan", "report", "discover")
_MCP_META_RE = re.compile(r"- \*\*Browser: MCP tools first[^\n]*\n")


def _clean_env():
    """patch.dict that drops the MCP knobs (server, node and the instance
    passthrough knobs — conftest always sets MACP_BROWSER_DIR) for the duration
    of a test."""
    env = {k: v for k, v in os.environ.items() if k not in _KNOB_ENVS}
    return mock.patch.dict(os.environ, env, clear=True)


def _vault(tmp: str, body: str) -> Path:
    root = Path(tmp)
    (root / ".macp").mkdir()
    (root / ".macp" / "config.toml").write_text(body, encoding="utf-8")
    return root


class BrowserMcpServerResolverTest(unittest.TestCase):
    def test_env_wins_over_config(self):
        with tempfile.TemporaryDirectory() as tmp, _clean_env():
            root = _vault(tmp, 'browser_mcp_server = "/cfg/server.mjs"\n')
            os.environ["MACP_BROWSER_MCP_SERVER"] = "/env/server.mjs"
            self.assertEqual(paths.browser_mcp_server(start=root),
                             Path("/env/server.mjs"))

    def test_config_key_read(self):
        with tempfile.TemporaryDirectory() as tmp, _clean_env():
            root = _vault(tmp, 'browser_mcp_server = "/cfg/server.mjs"\n')
            self.assertEqual(paths.browser_mcp_server(start=root / "sub"),
                             Path("/cfg/server.mjs"))

    def test_config_without_key_is_none(self):
        with tempfile.TemporaryDirectory() as tmp, _clean_env():
            root = _vault(tmp, 'worker_root = "/w"\n')
            self.assertIsNone(paths.browser_mcp_server(start=root))

    def test_no_config_is_none(self):
        with tempfile.TemporaryDirectory() as tmp, _clean_env():
            self.assertIsNone(paths.browser_mcp_server(start=Path(tmp)))

    def test_tilde_expands(self):
        with tempfile.TemporaryDirectory() as tmp, _clean_env():
            root = _vault(tmp, 'browser_mcp_server = "~/mcp/server.mjs"\n')
            self.assertEqual(paths.browser_mcp_server(start=root),
                             Path("~/mcp/server.mjs").expanduser())
            os.environ["MACP_BROWSER_MCP_SERVER"] = "~/e/server.mjs"
            self.assertEqual(paths.browser_mcp_server(start=root),
                             Path("~/e/server.mjs").expanduser())


def _argv(stage: str, server: Path | None, **kw) -> list[str]:
    """build_stage_argv with the MCP knob pinned: `server` set via env (on), or
    env popped and the resolver forced to None (off). uuid fixed so two calls
    compare byte-for-byte."""
    kw.setdefault("worker_root", Path("/nonexistent"))
    with _clean_env(), \
            mock.patch.object(stages.uuid, "uuid4", return_value=uuid.UUID(int=0)):
        if server is not None:
            os.environ["MACP_BROWSER_MCP_SERVER"] = str(server)
            return stages.build_stage_argv(
                stage, task_id="t-slug", run_id=_RUN_ID, run_dir=Path("/tmp/runs/x"),
                branch="worker/t-slug", **kw)
        with mock.patch.object(stages.paths, "browser_mcp_server", return_value=None):
            return stages.build_stage_argv(
                stage, task_id="t-slug", run_id=_RUN_ID, run_dir=Path("/tmp/runs/x"),
                branch="worker/t-slug", **kw)


def _mcp_cfg(argv: list[str]) -> dict:
    return json.loads(argv[argv.index("--mcp-config") + 1])


def _strip_mcp(argv: list[str]) -> list[str]:
    """`argv` with every MCP addition removed — what the off argv must equal."""
    out = list(argv)
    if "--mcp-config" in out:
        i = out.index("--mcp-config")
        del out[i:i + 3]          # --mcp-config <json> --strict-mcp-config
    out = [t for t in out if t != stages.BROWSER_MCP_TOOL_RULE]
    out[2] = _MCP_META_RE.sub("", out[2]).replace(
        f" {stages.BROWSER_MCP_TOOL_RULE}", "")
    return out


class StageMcpWiringTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.server = Path(self._tmp.name) / "mcp" / "server.mjs"
        self.server.parent.mkdir()
        self.server.write_text("// stub\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _assert_wired(self, argv: list[str]):
        self.assertIn("--mcp-config", argv)
        i = argv.index("--mcp-config")
        self.assertEqual(argv[i + 2], "--strict-mcp-config")
        srv = _mcp_cfg(argv)["mcpServers"]["macp-browser"]
        self.assertEqual(srv["args"], [str(self.server)])
        self.assertEqual(srv["env"], {"MACP_BROWSER_OWNER": _RUN_ID})
        a = argv.index("--allowed-tools")
        self.assertLess(i, a)
        self.assertIn("mcp__macp-browser__*", argv[a + 1:])
        self.assertIn("Bash(macp-browser:*)", argv[a + 1:])   # wrapper kept

    # AC1: configured → --mcp-config + rule + owner = run_id on the four stages.
    def test_browser_stages_get_mcp_config_rule_and_owner(self):
        for stage in _MCP_STAGES:
            with self.subTest(stage=stage), \
                    mock.patch.object(stages, "usage_enabled", return_value=True):
                argv = _argv(stage, self.server)
                self._assert_wired(argv)
                self.assertLess(argv.index("--mcp-config"),
                                argv.index(stages.SESSION_ID_FLAG))

    def test_wired_without_session_id(self):
        for stage in _MCP_STAGES:
            with self.subTest(stage=stage), \
                    mock.patch.object(stages, "usage_enabled", return_value=False):
                argv = _argv(stage, self.server)
                self.assertNotIn(stages.SESSION_ID_FLAG, argv)
                self._assert_wired(argv)

    def test_task_allowed_tools_keep_mcp_rule(self):
        argv = _argv("implement", self.server, allowed_tools="Read,Edit,Bash(git:*)")
        a = argv.index("--allowed-tools")
        tools = argv[a + 1:argv.index("--add-dir")] if "--add-dir" in argv else argv[a + 1:]
        self.assertEqual(tools[:3], ["Read", "Edit", "Bash(git:*)"])
        for rule in (stages.SMS_TOOL_RULE, stages.BROWSER_TOOL_RULE,
                     stages.REMOTE_GATE_TOOL_RULE, stages.EMULATOR_TOOL_RULE):
            self.assertIn(rule, tools)
        self.assertEqual(tools[-1], "mcp__macp-browser__*")

    def test_node_binary_knob(self):
        self.assertEqual(
            _mcp_cfg(_argv("verify", self.server))["mcpServers"]["macp-browser"]["command"],
            "node")
        with mock.patch.dict(os.environ, {"MACP_BROWSER_NODE": "/opt/node/bin/node"}):
            cfg = json.loads(stages.browser_mcp_config(self.server, _RUN_ID))
        self.assertEqual(cfg["mcpServers"]["macp-browser"]["command"],
                         "/opt/node/bin/node")

    def _server_env(self, env: dict[str, str]) -> dict:
        with _clean_env(), mock.patch.dict(os.environ, env):
            cfg = json.loads(stages.browser_mcp_config(self.server, _RUN_ID))
        return cfg["mcpServers"]["macp-browser"]["env"]

    # Instance knobs unset (or empty) → owner only, bytes as before (default 9277).
    def test_instance_knobs_absent_env_is_owner_only(self):
        before = json.dumps({"mcpServers": {"macp-browser": {
            "command": "node", "args": [str(self.server)],
            "env": {"MACP_BROWSER_OWNER": _RUN_ID}}}},
            sort_keys=True, separators=(",", ":"))
        for env in ({}, {k: "" for k in _INSTANCE_ENV}):
            with self.subTest(env=env), _clean_env(), mock.patch.dict(os.environ, env):
                self.assertEqual(stages.browser_mcp_config(self.server, _RUN_ID), before)
            self.assertEqual(self._server_env(env), {"MACP_BROWSER_OWNER": _RUN_ID})

    # Instance knobs set → copied verbatim into the server env next to the owner.
    def test_instance_knobs_pass_through(self):
        self.assertEqual(self._server_env(_INSTANCE_ENV),
                         {"MACP_BROWSER_OWNER": _RUN_ID, **_INSTANCE_ENV})

    def test_instance_only_passes_just_instance(self):
        self.assertEqual(self._server_env({"MACP_BROWSER_INSTANCE": "s-test"}),
                         {"MACP_BROWSER_OWNER": _RUN_ID,
                          "MACP_BROWSER_INSTANCE": "s-test"})

    def test_owner_env_never_overrides_run_id(self):
        env = self._server_env({"MACP_BROWSER_OWNER": "someone-else",
                                "MACP_BROWSER_INSTANCE": "s-test"})
        self.assertEqual(env["MACP_BROWSER_OWNER"], _RUN_ID)

    def test_instance_knobs_reach_stage_argv(self):
        for stage in _MCP_STAGES:
            with self.subTest(stage=stage), _clean_env(), \
                    mock.patch.dict(os.environ, _INSTANCE_ENV), \
                    mock.patch.object(stages.paths, "browser_mcp_server",
                                      return_value=self.server):
                argv = stages.build_stage_argv(
                    stage, task_id="t-slug", run_id=_RUN_ID,
                    run_dir=Path("/tmp/runs/x"), branch="worker/t-slug",
                    worker_root=Path("/nonexistent"))
                self.assertEqual(_mcp_cfg(argv)["mcpServers"]["macp-browser"]["env"],
                                 {"MACP_BROWSER_OWNER": _RUN_ID, **_INSTANCE_ENV})

    # AC2: unset → byte-identical to the pre-change argv.
    def test_off_argv_has_no_mcp_and_equals_on_minus_additions(self):
        for stage in stages.STAGE_TOOLS:
            for kw in ({}, {"repo": "https://example.invalid/r.git",
                            "working_dir": "/tmp/wd"}):
                with self.subTest(stage=stage, **kw):
                    off = _argv(stage, None, **kw)
                    self.assertFalse([t for t in off[3:] if "mcp" in t.lower()])
                    self.assertNotIn("mcp__", off[2])
                    self.assertNotIn("MCP tools first", off[2])
                    self.assertEqual(_strip_mcp(_argv(stage, self.server, **kw)), off)

    def test_missing_server_file_is_off(self):
        gone = Path(self._tmp.name) / "gone" / "server.mjs"
        for stage in _MCP_STAGES:
            with self.subTest(stage=stage):
                self.assertEqual(_argv(stage, gone), _argv(stage, None))

    def test_resolver_error_is_off(self):
        with mock.patch.object(stages.paths, "browser_mcp_server",
                               side_effect=RuntimeError("bad config")):
            self.assertIsNone(stages.browser_mcp_server("implement"))

    # AC3: research / plan (and report / discover) never get it.
    def test_non_browser_stages_untouched_even_when_configured(self):
        for stage in _NON_MCP_STAGES:
            with self.subTest(stage=stage):
                on = _argv(stage, self.server)
                self.assertNotIn("--mcp-config", on)
                self.assertNotIn("--strict-mcp-config", on)
                self.assertFalse([t for t in on if "mcp__" in t])
                self.assertNotIn("MCP tools first", on[2])
                self.assertEqual(on, _argv(stage, None))

    # AC4: prompt guidance "MCP tools first, wrapper for compatibility".
    def test_prompt_guidance_only_when_configured(self):
        for stage in _MCP_STAGES:
            with self.subTest(stage=stage):
                on = _argv(stage, self.server)[2]
                off = _argv(stage, None)[2]
                self.assertEqual(len(_MCP_META_RE.findall(on)), 1)
                self.assertIn("MCP tools first", on)
                self.assertIn("wrapper for compatibility", on)
                self.assertNotIn("MCP tools first", off)
                self.assertNotIn("wrapper for compatibility", off)

    def test_rule_is_space_free_and_not_forbidden(self):
        self.assertNotIn(" ", stages.BROWSER_MCP_TOOL_RULE)
        for stage in _MCP_STAGES:
            tools = stages._safe_tools(stage, browser_mcp=True)
            self.assertEqual(tools[-1], stages.BROWSER_MCP_TOOL_RULE)
            self.assertEqual(tools[:-1], stages._safe_tools(stage))
        self.assertEqual(stages._safe_tools("research", browser_mcp=True),
                         stages._safe_tools("research"))


if __name__ == "__main__":
    unittest.main()
