"""macp-browser MCP server wiring: paths.browser_mcp_server + the browser stages.

Every test pins the knob itself (env popped, `start=` seam or a patched
resolver), so nothing here depends on the host's env or .macp/config.toml.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system import paths

_KNOB_ENVS = ("MACP_BROWSER_MCP_SERVER", "MACP_BROWSER_NODE")


def _clean_env():
    """patch.dict that drops the MCP knobs for the duration of a test."""
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


if __name__ == "__main__":
    unittest.main()
