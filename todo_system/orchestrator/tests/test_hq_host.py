"""Tests for the HQ host guard — identity chain, check(), CLI (hq_host.py).

Every test injects `home=`/`local_id=`/env explicitly: both real hosts carry
`~/.macp/host_id` and may export MACP_HOST_ID, so an uninjected call would
leak the machine's real identity into the assertions.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from todo_system.orchestrator import hq_host
from todo_system.orchestrator import orchestrator as orch_mod
from todo_system.orchestrator import runner as runner_mod


class LocalHostIdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        (self.home / ".macp").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_file_id(self, text: str) -> None:
        (self.home / ".macp" / "host_id").write_text(text, encoding="utf-8")

    def test_env_wins_over_file(self):
        self._write_file_id("file-id\n")
        with mock.patch.dict(os.environ, {"MACP_HOST_ID": "env-id"}):
            self.assertEqual(hq_host.local_host_id(home=self.home), "env-id")

    def test_blank_env_ignored(self):
        self._write_file_id("file-id\n")
        with mock.patch.dict(os.environ, {"MACP_HOST_ID": "   "}):
            self.assertEqual(hq_host.local_host_id(home=self.home), "file-id")

    def test_file_wins_over_hostname_and_trims(self):
        self._write_file_id("  file-id\n")
        with mock.patch.dict(os.environ):
            os.environ.pop("MACP_HOST_ID", None)
            self.assertEqual(hq_host.local_host_id(home=self.home), "file-id")

    def test_hostname_fallback(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("MACP_HOST_ID", None)
            self.assertEqual(hq_host.local_host_id(home=self.home),
                             socket.gethostname())

    def test_unreadable_file_falls_back(self):
        # A directory makes read_text raise deterministically (IsADirectoryError)
        # without resorting to permission tricks that root would ignore.
        (self.home / ".macp" / "host_id").mkdir()
        with mock.patch.dict(os.environ):
            os.environ.pop("MACP_HOST_ID", None)
            self.assertEqual(hq_host.local_host_id(home=self.home),
                             socket.gethostname())


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "Worker").mkdir()
        self.marker = self.root / "Worker" / "hq_host"

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_no_marker_allows(self):
        ok, reason = hq_host.check(self.root, local_id="me")
        self.assertTrue(ok)
        self.assertIn("unguarded", reason)

    def test_match_allows_trimmed(self):
        self.marker.write_text("me\n", encoding="utf-8")
        ok, _ = hq_host.check(self.root, local_id="me")
        self.assertTrue(ok)

    def test_mismatch_refuses_with_both_ids(self):
        self.marker.write_text("other-host\n", encoding="utf-8")
        ok, reason = hq_host.check(self.root, local_id="me")
        self.assertFalse(ok)
        self.assertIn("'other-host'", reason)
        self.assertIn("'me'", reason)

    def test_marker_directory_fails_open(self):
        self.marker.mkdir()
        ok, reason = hq_host.check(self.root, local_id="me")
        self.assertTrue(ok)
        self.assertIn("warning", reason)

    def test_empty_marker_fails_open(self):
        self.marker.write_text("   \n", encoding="utf-8")
        ok, _ = hq_host.check(self.root, local_id="me")
        self.assertTrue(ok)

    def test_default_local_id_uses_identity_chain(self):
        self.marker.write_text("me\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"MACP_HOST_ID": "me"}):
            ok, _ = hq_host.check(self.root)
        self.assertTrue(ok)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "Worker").mkdir()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_check_allowed_exit_0(self):
        self.assertEqual(hq_host._main(["--check", "--root", str(self.root)]), 0)

    def test_check_mismatch_exit_3(self):
        (self.root / "Worker" / "hq_host").write_text("other-host\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"MACP_HOST_ID": "me"}):
            rc = hq_host._main(["--check", "--root", str(self.root)])
        self.assertEqual(rc, 3)

    def test_without_check_flag_usage_exit_2(self):
        self.assertEqual(hq_host._main([]), 2)


class OrchestratorGuardTest(unittest.TestCase):
    """_main-level: a mismatched marker exits 3 BEFORE the lease is created.

    `Worker/hq_host` is a git-tracked VAULT file, so `_main` reads it at
    `paths.vault_root_or_cwd()` — these cases point MACP_VAULT_ROOT at the
    fixture. They used to rely on the cwd alone, which silently disarmed the
    guard whenever the orchestrator ran from anywhere but the vault.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "Worker").mkdir()
        (self.tmp / "Worker" / "hq_host").write_text("other-host\n",
                                                     encoding="utf-8")
        self.env = {"MACP_HOST_ID": "me", "MACP_VAULT_ROOT": str(self.tmp)}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_auto_mismatch_exits_3_without_lease(self):
        lock = self.tmp / "orchestrator.lock"
        with contextlib.chdir(self.tmp), mock.patch.dict(os.environ, self.env):
            rc = orch_mod._main(["--lease", str(lock), "auto",
                                 "--db", str(self.tmp / "todo.db")])
        self.assertEqual(rc, 3)
        self.assertFalse(lock.exists())

    def test_dispatch_mismatch_exits_3(self):
        with contextlib.chdir(self.tmp), mock.patch.dict(os.environ, self.env):
            rc = orch_mod._main(["dispatch", "--task", "t"])
        self.assertEqual(rc, 3)

    def test_auto_mismatch_exits_3_from_a_cwd_outside_the_vault(self):
        """The point of reading the marker at the vault root: the guard now
        fires even when the orchestrator is started from somewhere else."""
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        with contextlib.chdir(elsewhere), mock.patch.dict(os.environ, self.env):
            rc = orch_mod._main(["auto", "--db", str(self.tmp / "todo.db")])
        self.assertEqual(rc, 3)


class PullMainTest(unittest.TestCase):
    """_pull_main via an injected `_run` — no test here executes a real git."""

    def _recording_run(self, head_rc=0, head_out="main\n", fetch_rc=0, merge_rc=0):
        calls = []

        def run(cmd, **kw):
            calls.append(list(cmd))
            if cmd[:2] == ["git", "rev-parse"]:
                return SimpleNamespace(returncode=head_rc, stdout=head_out,
                                       stderr="")
            if cmd[:2] == ["git", "fetch"]:
                return SimpleNamespace(returncode=fetch_rc, stdout="",
                                       stderr="fetch boom")
            if cmd[:2] == ["git", "merge"]:
                return SimpleNamespace(returncode=merge_rc, stdout="",
                                       stderr="merge boom")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        return calls, run

    def test_head_on_main_fetches_then_fast_forwards(self):
        # pure-behind (the common case): a fetch then a --ff-only merge, and it
        # stops there — no merge-commit fallback, and no rebasing under a HEAD
        # the sync daemon may be moving. (Was a single `pull --rebase`.)
        calls, run = self._recording_run()
        orch_mod._pull_main(Path("."), _run=run, _sleep=lambda *_a: None)
        self.assertEqual(calls[1], ["git", "fetch", "origin", "main"])
        self.assertEqual(calls[2], ["git", "merge", "--ff-only", "--autostash",
                                    "FETCH_HEAD"])
        self.assertEqual(len(calls), 3)

    def test_head_not_main_skips_pull(self):
        calls, run = self._recording_run(head_out="worker/x\n")
        orch_mod._pull_main(Path("."), _run=run)
        self.assertEqual(calls, [["git", "rev-parse", "--abbrev-ref", "HEAD"]])

    def test_rev_parse_failure_skips_pull(self):
        calls, run = self._recording_run(head_rc=1)
        orch_mod._pull_main(Path("."), _run=run)
        self.assertEqual(len(calls), 1)

    def test_a_persistent_merge_failure_is_non_fatal(self):
        # fetch keeps succeeding but every merge fails across all retries: the
        # loop must exhaust and return quietly, never raising into the tick.
        calls, run = self._recording_run(merge_rc=1)
        orch_mod._pull_main(Path("."), _run=run, _sleep=lambda *_a: None)  # no raise
        self.assertGreaterEqual(
            len([c for c in calls if c[:2] == ["git", "fetch"]]), 2)
        self.assertTrue(any(c[:3] == ["git", "merge", "--ff-only"] for c in calls))

    def test_runner_exception_swallowed(self):
        def run(cmd, **kw):
            raise RuntimeError("no git here")

        orch_mod._pull_main(Path("."), _run=run)  # must not raise


class RunnerGuardTest(unittest.TestCase):
    """runner._main: ai-local is gated on the marker, ai-remote is not.

    Same vault-root rule as OrchestratorGuardTest — MACP_VAULT_ROOT points at
    the fixture holding `Worker/hq_host`.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "Worker").mkdir()
        (self.tmp / "Worker" / "hq_host").write_text("other-host\n",
                                                     encoding="utf-8")
        self.env = {"MACP_HOST_ID": "me", "MACP_VAULT_ROOT": str(self.tmp)}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ai_local_mismatch_exits_3(self):
        with contextlib.chdir(self.tmp), mock.patch.dict(os.environ, self.env):
            rc = runner_mod._main(["--agent-id", "x", "--kind", "ai-local",
                                   "--uri", f"file://{self.tmp}/agents",
                                   "--once"])
        self.assertEqual(rc, 3)

    def test_ai_local_mismatch_exits_3_from_a_cwd_outside_the_vault(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        with contextlib.chdir(elsewhere), mock.patch.dict(os.environ, self.env):
            rc = runner_mod._main(["--agent-id", "x", "--kind", "ai-local",
                                   "--uri", f"file://{self.tmp}/agents",
                                   "--once"])
        self.assertEqual(rc, 3)

    def test_ai_remote_skips_guard(self):
        # Same mismatched marker; rc 0 (one poll of an empty FileMailbox, the
        # test_runner.py offline path) proves the guard never fired.
        with contextlib.chdir(self.tmp), mock.patch.dict(os.environ, self.env):
            rc = runner_mod._main(["--agent-id", "x", "--kind", "ai-remote",
                                   "--uri", f"file://{self.tmp}/agents",
                                   "--once"])
        self.assertEqual(rc, 0)
