"""AC1 for `runner._main()`: the runner works with its cwd OUTSIDE the vault.

The live unit runs

    python -m todo_system.orchestrator.runner --agent worker-default
        --mode live-staged --uri file://Worker/agents --daemon

— a vault-relative `--uri`, and a `--registry` that defaults to the relative
`Worker/agents/registry.yaml`. Both used to resolve against the cwd, so the
runner had to be started from the vault directory; now they resolve against
`paths.vault_root_or_cwd()`. `file://` (not `git://`) because the fake vault is
not a git repo, and because that is the uri the real unit passes.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from todo_system import paths
from todo_system.orchestrator import runner
from todo_system.orchestrator.tests.test_orchestrator_cli import make_fake_vault

ARGV = ["--agent", "worker-default", "--uri", "file://Worker/agents",
        "--mode", "shadow", "--once"]


class RunnerMainVaultRelativePathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.vault = make_fake_vault(self.tmp)
        self.elsewhere = self.tmp / "elsewhere"
        self.elsewhere.mkdir()
        self.addCleanup(os.chdir, Path.cwd())

    def _run(self, argv, *, cwd: Path, vault_env: Path | None):
        os.chdir(cwd)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False):
            if vault_env is None:
                os.environ.pop("MACP_VAULT_ROOT", None)
            else:
                os.environ["MACP_VAULT_ROOT"] = str(vault_env)
            with redirect_stdout(buf):
                rc = runner._main(argv)
        return rc, buf.getvalue()

    def test_once_poll_from_an_outside_cwd_finds_registry_and_mailbox(self):
        with mock.patch("todo_system.orchestrator.hq_host.check",
                        return_value=(True, "")) as check:
            rc, out = self._run(ARGV, cwd=self.elsewhere, vault_env=self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("processed 0 message(s) for worker-default", out)
        # The ai-local HQ guard reads the vault's marker, not the cwd's.
        self.assertEqual(check.call_args.args[0], self.vault)

    def test_output_is_identical_from_the_vault_and_from_outside(self):
        """Behaviour invariance for the cwd-at-the-vault invocation the unit makes."""
        with mock.patch("todo_system.orchestrator.hq_host.check",
                        return_value=(True, "")):
            rc_in, out_in = self._run(ARGV, cwd=self.vault, vault_env=None)
            rc_out, out_out = self._run(ARGV, cwd=self.elsewhere, vault_env=self.vault)
        self.assertEqual(rc_in, 0)
        self.assertEqual(rc_out, 0)
        self.assertEqual(out_in, out_out)

    def test_old_cwd_base_cannot_find_the_registry(self):
        """RED control: with `base = Path.cwd()` — the pre-change behaviour —
        the relative `--registry` default misses the vault's registry.yaml."""
        with mock.patch("todo_system.orchestrator.hq_host.check",
                        return_value=(True, "")), \
             mock.patch.object(paths, "vault_root_or_cwd", lambda *a, **k: Path.cwd()), \
             self.assertRaises(FileNotFoundError):
            self._run(ARGV, cwd=self.elsewhere, vault_env=self.vault)

    def test_inline_agent_id_still_loads_no_registry(self):
        """The bare-python3 remote path must stay registry-free: `resolve_agent`
        takes the new `base` argument but only the --agent branch uses it."""
        with mock.patch.object(runner, "load_registry") as load:
            rc, out = self._run(
                ["--agent-id", "remote-1", "--kind", "ai-remote",
                 "--uri", f"file://{self.vault / 'Worker' / 'agents'}",
                 "--mode", "shadow", "--once"],
                cwd=self.elsewhere, vault_env=self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("processed 0 message(s) for remote-1", out)
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
