"""Unit tests for `orchestrator._main()`'s read-only subcommands and the
hq-host guard's rc=3 path.

`main()` in the task title is really `_main` — `__main__` does
`raise SystemExit(_main())`. Follows the `_main([...])` direct-call pattern
from test_trace.py's TraceCliTest.
"""

from __future__ import annotations

import io
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from todo_system import paths
from todo_system.orchestrator import orchestrator, stages
from todo_system.orchestrator.assignment import AWAITING_HUMAN, Assignment, Ledger


def _registry_yaml(mailbox_uri: str | None) -> str:
    # `mailbox:` must equal the test's `--uri`. Without it `Agent.mailbox`
    # falls back to the live `git://Worker/agents`, `_mailbox_for` opens it
    # against cwd, and `_main dispatch` commits a real TASK_ASSIGN into the
    # vault repo (`collect`/`serve` also `git pull --rebase` there).
    text = "agents:\n  - id: worker-default\n    kind: ai-local\n    projects: ['*']\n"
    if mailbox_uri is not None:
        text += f"    mailbox: '{mailbox_uri}'\n"
    return text


def _guarded_open_mailbox(tmp: Path):
    """Wrap `orchestrator.open_mailbox` so opening any mailbox outside `tmp`
    raises BEFORE a mailbox object (and its git side effects) is built."""
    real = orchestrator.open_mailbox
    opened: list[str] = []
    root = tmp.resolve()

    def guard(uri, *a, **k):
        path = Path(uri.split("://", 1)[-1])
        if not path.is_absolute() or not path.resolve().is_relative_to(root):
            raise AssertionError(f"test opened a mailbox outside tmp: {uri}")
        opened.append(uri)
        return real(uri, *a, **k)

    return guard, opened


class MainReadonlySubcommandsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ledger_dir = self.tmp / "assignments"
        self.registry_path = self.tmp / "registry.yaml"
        self.mailbox_uri = f"git://{self.tmp / 'mailbox'}"
        self.registry_path.write_text(_registry_yaml(self.mailbox_uri), encoding="utf-8")
        # GitMailbox._toplevel() needs the root to already exist, or it falls
        # back to that (nonexistent) path as repo_dir and every git subprocess
        # call then fails with FileNotFoundError instead of "not a repo".
        (self.tmp / "mailbox").mkdir()

    def _main(self, cmd_args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = orchestrator._main(
                ["--ledger", str(self.ledger_dir),
                 "--registry", str(self.registry_path),
                 "--uri", self.mailbox_uri,
                 *cmd_args])
        return rc, buf.getvalue()

    def test_status_lists_ledger_rows(self):
        Ledger(self.ledger_dir).save(Assignment(
            task_id="t1", agent_id="worker-default", corr_id="c1",
            state=AWAITING_HUMAN, nudge_index=2))
        rc, out = self._main(["status"])
        self.assertEqual(rc, 0)
        self.assertIn("t1", out)
        self.assertIn("worker-default", out)
        self.assertIn("nudge#2", out)

    def test_agents_shows_liveness(self):
        rc, out = self._main(["agents"])
        self.assertEqual(rc, 0)
        self.assertIn("worker-default", out)
        self.assertIn("unknown", out)

    def test_collect_on_empty_mailbox_is_noop(self):
        rc, out = self._main(["collect"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_collect_processes_a_reply(self):
        from todo_system.orchestrator import message as m

        Ledger(self.ledger_dir).save(Assignment(
            task_id="t2", agent_id="worker-default", corr_id="c2",
            state=orchestrator.DISPATCHED))
        inbox = self.tmp / "mailbox" / m.ORCHESTRATOR / "inbox"
        inbox.mkdir(parents=True)
        msg = m.Envelope(to=m.ORCHESTRATOR, type=m.ACCEPT, frm="worker-default",
                         task_id="t2", corr_id="c2")
        (inbox / f"{msg.msg_id}.json").write_text(msg.to_json(), encoding="utf-8")
        rc, out = self._main(["collect"])
        self.assertEqual(rc, 0)
        self.assertIn("t2 -> worker-default", out)

    def test_renudge_on_empty_ledger_is_noop(self):
        rc, out = self._main(["renudge"])
        self.assertEqual(rc, 0)
        self.assertEqual(out, "")

    def test_lease_held_by_other_blocks_command(self):
        from todo_system.orchestrator.lease import Lease

        lease_path = self.tmp / "orchestrator.lock"
        other = Lease(lease_path, holder="other-host:1")
        self.assertTrue(other.acquire())
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = orchestrator._main(
                ["--ledger", str(self.ledger_dir),
                 "--registry", str(self.registry_path),
                 "--uri", self.mailbox_uri,
                 "--lease", str(lease_path),
                 "status"])
        self.assertEqual(rc, 1)
        self.assertIn("error: orchestrator lease held by other-host:1", buf.getvalue())

    def test_trace_cli_prints_view(self):
        Ledger(self.ledger_dir).save(Assignment(
            task_id="t1", agent_id="worker-default", corr_id="c1",
            state=AWAITING_HUMAN))
        with mock.patch.object(orchestrator, "open_mailbox"), \
             mock.patch.object(orchestrator, "load_registry"), \
             mock.patch.object(stages, "RUNS_DIR", self.tmp / "runs"), \
             mock.patch.object(stages, "gh_pr_merged", lambda *a, **k: None), \
             mock.patch.object(stages, "remote_branch_exists", lambda *a, **k: False):
            rc, out = self._main(["trace", "--task", "t1"])
        self.assertEqual(rc, 0)
        self.assertIn("trace — ", out)

    def test_dispatch_routes_and_dedups(self):
        guard, _ = _guarded_open_mailbox(self.tmp)
        with mock.patch("todo_system.orchestrator.hq_host.check",
                        return_value=(True, "")), \
             mock.patch.object(orchestrator, "open_mailbox", guard):
            rc, out = self._main(["dispatch", "--task", "t-dispatch-1",
                                   "--payload", "{}"])
            self.assertEqual(rc, 0)
            self.assertIn("dispatched t-dispatch-1 -> worker-default", out)

            rc2, out2 = self._main(["dispatch", "--task", "t-dispatch-1",
                                     "--payload", "{}"])
            self.assertEqual(rc2, 0)
            self.assertIn(
                "not dispatched (already active or unroutable): t-dispatch-1",
                out2)

    def test_dispatch_never_opens_mailbox_outside_tmp(self):
        guard, opened = _guarded_open_mailbox(self.tmp)
        with mock.patch("todo_system.orchestrator.hq_host.check",
                        return_value=(True, "")), \
             mock.patch.object(orchestrator, "open_mailbox", guard):
            rc, out = self._main(["dispatch", "--task", "t-fixture-isolation",
                                  "--payload", "{}"])
        self.assertEqual(rc, 0)
        self.assertIn("dispatched t-fixture-isolation -> worker-default", out)
        self.assertTrue(opened)
        self.assertEqual(set(opened), {self.mailbox_uri})
        inbox = self.tmp / "mailbox" / "worker-default" / "inbox"
        self.assertEqual(len(list(inbox.glob("*.json"))), 1)

    def test_registry_without_mailbox_is_caught_by_guard(self):
        # RED control: the pre-fix fixture shape (no `mailbox:`) must trip the
        # guard. It raises before the real open_mailbox runs, so the live
        # vault mailbox is never touched.
        self.registry_path.write_text(_registry_yaml(None), encoding="utf-8")
        guard, _ = _guarded_open_mailbox(self.tmp)
        with mock.patch("todo_system.orchestrator.hq_host.check",
                        return_value=(True, "")), \
             mock.patch.object(orchestrator, "open_mailbox", guard), \
             self.assertRaises(AssertionError) as ctx:
            self._main(["dispatch", "--task", "t-fixture-isolation",
                        "--payload", "{}"])
        self.assertIn("outside tmp", str(ctx.exception))
        self.assertIn("Worker/agents", str(ctx.exception))
        inbox = self.tmp / "mailbox" / "worker-default" / "inbox"
        self.assertEqual(list(inbox.glob("*.json")) if inbox.exists() else [], [])

    def test_dashboard_writes_file(self):
        out_path = self.tmp / "d.html"
        rc, out = self._main(
            ["dashboard", "--out", str(out_path),
             "--db", str(self.tmp / "todo.db"),
             "--decisions-dir", str(self.tmp / "Decisions"),
             "--items-dir", str(self.tmp / "items")])
        self.assertEqual(rc, 0)
        self.assertTrue(out_path.exists())
        self.assertIn(f"wrote {out_path}", out)

    def test_serve_on_empty_ledger(self):
        out_path = self.tmp / "s.html"
        rc, out = self._main(
            ["serve", "--out", str(out_path), "--db", str(self.tmp / "todo.db")])
        self.assertEqual(rc, 0)
        self.assertIn("serve: collected=0 renudged=0", out)
        self.assertTrue(out_path.exists())

    def test_unknown_cmd_is_unreachable_via_argparse(self):
        # `sub = p.add_subparsers(dest="cmd", required=True)` rejects any value
        # outside the known choices before `_main()`'s body runs, so the
        # `return 2` fallback (orchestrator.py:2013) cannot be exercised without
        # adding a bypass to production code — which requirements.md forbids.
        # This asserts the unreachability itself: argparse raises SystemExit(2).
        with self.assertRaises(SystemExit) as ctx:
            self._main(["bogus-cmd"])
        self.assertEqual(ctx.exception.code, 2)


class MainHqHostGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ledger_dir = self.tmp / "assignments"
        self.registry_path = self.tmp / "registry.yaml"
        self.registry_path.write_text(
            _registry_yaml(f"git://{self.tmp / 'mailbox'}"), encoding="utf-8")

    def test_dispatch_blocked_by_hq_host_guard_returns_3(self):
        reason = "hq_host is 'other-host' but this host is 'me' — refusing to dispatch"
        buf = io.StringIO()
        with mock.patch("todo_system.orchestrator.hq_host.check",
                        return_value=(False, reason)), \
             mock.patch.object(orchestrator, "Orchestrator") as orch_cls, \
             redirect_stdout(buf):
            rc = orchestrator._main(
                ["--ledger", str(self.ledger_dir),
                 "--registry", str(self.registry_path),
                 "dispatch", "--task", "no-such-task"])
        self.assertEqual(rc, 3)
        self.assertIn("hq-host guard:", buf.getvalue())
        self.assertIn(reason, buf.getvalue())
        orch_cls.assert_not_called()


class MainAutoSubcommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.ledger_dir = self.tmp / "assignments"
        self.registry_path = self.tmp / "registry.yaml"
        self.mailbox_uri = f"git://{self.tmp / 'mailbox'}"
        self.registry_path.write_text(_registry_yaml(self.mailbox_uri), encoding="utf-8")
        (self.tmp / "mailbox").mkdir()

    def _main(self, cmd_args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = orchestrator._main(
                ["--ledger", str(self.ledger_dir),
                 "--registry", str(self.registry_path),
                 "--uri", self.mailbox_uri,
                 *cmd_args])
        return rc, buf.getvalue()

    def test_auto_prints_full_tick_summary(self):
        dispatched_a = mock.Mock(task_id="t-auto-1", agent_id="worker-default")
        tick_result = {
            "reaped": [], "dispatched": [dispatched_a], "updated": [],
            "reconciled": [], "closed": [], "unblocked": [], "unparked": [],
            "resumed": [], "split_parents": [], "parents": [], "requeued": [],
            "renudged": [],
            "budget_reached": False, "paused": False, "usage_hold": None,
        }
        with mock.patch("todo_system.orchestrator.hq_host.check",
                        return_value=(True, "")), \
             mock.patch.object(orchestrator, "_pull_main"), \
             mock.patch.object(orchestrator, "_check_divergence"), \
             mock.patch("todo_system.orchestrator.tasks.load_dispatchable",
                        return_value=[orchestrator.Task("t-auto-1")]), \
             mock.patch.object(orchestrator.Orchestrator, "tick",
                                return_value=tick_result):
            rc, out = self._main(["auto", "--db", "Todo/todo.db"])
        self.assertEqual(rc, 0)
        self.assertIn(
            "candidates=1 dispatched=1 updated=0 renudged=0 reaped=0 "
            "reconciled=0 closed=0 unblocked=0 unparked=0 split_parents=0 "
            "parents=0 requeued=0",
            out)
        self.assertNotIn("BUDGET-REACHED", out)
        self.assertNotIn("PAUSED", out)
        self.assertNotIn("USAGE-HOLD", out)
        self.assertIn("  dispatched t-auto-1 -> worker-default", out)

    def test_auto_prints_hold_suffixes(self):
        tick_result = {
            "reaped": [], "dispatched": [], "updated": [],
            "reconciled": [], "closed": [], "unblocked": [], "unparked": [],
            "resumed": [], "parents": [], "requeued": [], "renudged": [],
            "budget_reached": True, "paused": True, "usage_hold": "weekly cap",
        }
        with mock.patch("todo_system.orchestrator.hq_host.check",
                        return_value=(True, "")), \
             mock.patch.object(orchestrator, "_pull_main"), \
             mock.patch.object(orchestrator, "_check_divergence"), \
             mock.patch("todo_system.orchestrator.tasks.load_dispatchable",
                        return_value=[orchestrator.Task("t-auto-1")]), \
             mock.patch.object(orchestrator.Orchestrator, "tick",
                                return_value=tick_result), \
             mock.patch.object(orchestrator.Orchestrator, "_runs_today",
                                return_value=5):
            rc, out = self._main(["auto", "--db", "Todo/todo.db"])
        self.assertEqual(rc, 0)
        self.assertIn("BUDGET-REACHED (runs today=5/", out)
        self.assertIn("PAUSED", out)
        self.assertIn("USAGE-HOLD (weekly cap)", out)

    def test_auto_usage_hold_line_carries_release_eta(self):
        eta = "2026-10-30T03:00:00+00:00"
        tick_result = {
            "reaped": [], "dispatched": [], "updated": [],
            "reconciled": [], "closed": [], "unblocked": [], "unparked": [],
            "resumed": [], "parents": [], "requeued": [], "renudged": [],
            "budget_reached": False, "paused": False,
            "usage_hold": "session 70% ahead of pace", "usage_hold_eta": eta,
        }
        with mock.patch("todo_system.orchestrator.hq_host.check",
                        return_value=(True, "")), \
             mock.patch.object(orchestrator, "_pull_main"), \
             mock.patch.object(orchestrator, "_check_divergence"), \
             mock.patch("todo_system.orchestrator.tasks.load_dispatchable",
                        return_value=[]), \
             mock.patch.object(orchestrator.Orchestrator, "tick",
                                return_value=tick_result):
            rc, out = self._main(["auto", "--db", "Todo/todo.db"])
        self.assertEqual(rc, 0)
        self.assertIn(f"USAGE-HOLD (session 70% ahead of pace; eta {eta})", out)

    def test_usage_hold_suffix_helper(self):
        eta = "2026-10-30T03:00:00+00:00"
        self.assertEqual(orchestrator._usage_hold_suffix(
            {"usage_hold": "pace", "usage_hold_eta": eta}), f" USAGE-HOLD (pace; eta {eta})")
        self.assertEqual(orchestrator._usage_hold_suffix(
            {"usage_hold": "reason", "usage_hold_eta": None}), " USAGE-HOLD (reason)")
        self.assertEqual(orchestrator._usage_hold_suffix({"usage_hold": "reason"}),
                         " USAGE-HOLD (reason)")
        self.assertEqual(orchestrator._usage_hold_suffix({"usage_hold": None}), "")

    def test_auto_applies_profile_daily_run_budget(self):
        # Covers orchestrator.py:1932 (`budget = prof.daily_run_budget`) — the
        # macp-profile branch that only fires when load_profile() returns a
        # real Profile, which the sandbox's ~/.macp normally does not have.
        from todo_system.macp.config import Profile

        tick_result = {
            "reaped": [], "dispatched": [], "updated": [],
            "reconciled": [], "closed": [], "unblocked": [], "unparked": [],
            "resumed": [], "parents": [], "requeued": [], "renudged": [],
            "budget_reached": True, "paused": False, "usage_hold": None,
        }
        with mock.patch("todo_system.orchestrator.hq_host.check",
                        return_value=(True, "")), \
             mock.patch.object(orchestrator, "_pull_main"), \
             mock.patch.object(orchestrator, "_check_divergence"), \
             mock.patch("todo_system.orchestrator.tasks.load_dispatchable",
                        return_value=[]), \
             mock.patch("todo_system.macp.load_profile",
                        return_value=Profile(name="test", daily_run_budget=7)), \
             mock.patch.object(orchestrator.Orchestrator, "tick",
                                return_value=tick_result), \
             mock.patch.object(orchestrator.Orchestrator, "_runs_today",
                                return_value=5):
            rc, out = self._main(["auto", "--db", "Todo/todo.db"])
        self.assertEqual(rc, 0)
        self.assertIn("BUDGET-REACHED (runs today=5/7)", out)


def make_fake_vault(root: Path) -> Path:
    """A minimal vault: the `.macp` marker plus the three trees the orchestrator's
    relative CLI defaults name (`Worker/agents`, `Worker/assignments`, `Todo`).

    The registry's `mailbox:` is deliberately the RELATIVE `file://Worker/agents`
    — the same shape the live units pass — so these tests fail if a relative uri
    stops resolving against the vault.
    """
    vault = root / "vault"
    (vault / ".macp").mkdir(parents=True)
    (vault / ".macp" / "config.toml").write_text("profile = 'test'\n", encoding="utf-8")
    (vault / "Worker" / "agents").mkdir(parents=True)
    (vault / "Worker" / "assignments").mkdir(parents=True)
    (vault / "Worker" / "agents" / "registry.yaml").write_text(
        _registry_yaml("file://Worker/agents"), encoding="utf-8")
    (vault / "Todo").mkdir()
    from todo_system import cli
    con = sqlite3.connect(vault / "Todo" / "todo.db")
    try:
        cli.init_db(con)
        con.commit()
    finally:
        con.close()
    return vault


class MainVaultRelativePathsTest(unittest.TestCase):
    """AC1: `status` / `auto` work with the cwd OUTSIDE the vault.

    Every path argument the units rely on defaults to a vault-relative string
    (`--ledger Worker/assignments`, `--registry Worker/agents/registry.yaml`,
    `--db Todo/todo.db`, `--uri …Worker/agents`). They used to resolve against
    the cwd, which is why the orchestrator had to be started from the vault
    directory. These cases run from a sibling directory with MACP_VAULT_ROOT set.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.vault = make_fake_vault(self.tmp)
        self.elsewhere = self.tmp / "elsewhere"
        self.elsewhere.mkdir()
        cwd = Path.cwd()
        self.addCleanup(os.chdir, cwd)

    def _run(self, cmd_args, *, cwd: Path, vault_env: Path | None):
        os.chdir(cwd)
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False):
            if vault_env is None:
                os.environ.pop("MACP_VAULT_ROOT", None)
            else:
                os.environ["MACP_VAULT_ROOT"] = str(vault_env)
            with redirect_stdout(buf):
                rc = orchestrator._main(cmd_args)
        return rc, buf.getvalue()

    def test_status_reads_the_vault_ledger_from_an_outside_cwd(self):
        Ledger(self.vault / "Worker" / "assignments").save(Assignment(
            task_id="t-relative", agent_id="worker-default", corr_id="c1",
            state=AWAITING_HUMAN, nudge_index=1))
        # No --ledger / --registry: the relative defaults must find the vault.
        rc, out = self._run(["--uri", "file://Worker/agents", "status"],
                            cwd=self.elsewhere, vault_env=self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("t-relative", out)
        self.assertIn("worker-default", out)

    def test_status_output_is_identical_from_the_vault_and_from_outside(self):
        """Behaviour invariance: the cwd-at-the-vault invocation the units make
        must produce exactly what it produced before this change."""
        Ledger(self.vault / "Worker" / "assignments").save(Assignment(
            task_id="t-same", agent_id="worker-default", corr_id="c1",
            state=AWAITING_HUMAN))
        argv = ["--uri", "file://Worker/agents", "status"]
        # cwd == vault, no env: resolution comes from the .macp marker.
        rc_in, out_in = self._run(argv, cwd=self.vault, vault_env=None)
        rc_out, out_out = self._run(argv, cwd=self.elsewhere, vault_env=self.vault)
        self.assertEqual(rc_in, 0)
        self.assertEqual(rc_out, 0)
        self.assertEqual(out_in, out_out)

    def test_auto_opens_the_vault_db_and_guards_the_vault_repo(self):
        tick_result = {
            "reaped": [], "dispatched": [], "updated": [], "reconciled": [],
            "closed": [], "unblocked": [], "unparked": [], "resumed": [],
            "split_parents": [], "parents": [], "requeued": [], "renudged": [],
            "budget_reached": False, "paused": False, "usage_hold": None,
        }
        with mock.patch("todo_system.orchestrator.hq_host.check",
                        return_value=(True, "")) as check, \
             mock.patch.object(orchestrator, "_pull_main") as pull, \
             mock.patch.object(orchestrator, "_check_divergence") as diverge, \
             mock.patch.object(orchestrator.Orchestrator, "tick",
                               return_value=tick_result):
            rc, out = self._run(
                ["--uri", "file://Worker/agents", "auto", "--db", "Todo/todo.db"],
                cwd=self.elsewhere, vault_env=self.vault)
        self.assertEqual(rc, 0)
        # candidates=0 means the relative --db actually opened the vault's
        # sqlite file; a cwd-based resolution raises OperationalError instead
        # (see test_auto_with_the_old_cwd_base_cannot_open_the_db).
        self.assertIn("candidates=0", out)
        # The three former Path.cwd() call sites now name the vault repo. From a
        # cwd outside it they inspected — and git-pulled — the wrong tree.
        self.assertEqual(check.call_args.args[0], self.vault)
        self.assertEqual(pull.call_args.args[0], self.vault)
        self.assertEqual(diverge.call_args.args[0], self.vault)

    def test_auto_with_the_old_cwd_base_cannot_open_the_db(self):
        """RED control: pin the pre-change behaviour by restoring the old base.

        With `base = Path.cwd()` — exactly what the code did before — the same
        argv cannot open `Todo/todo.db`, because the cwd is not the vault.
        """
        with mock.patch("todo_system.orchestrator.hq_host.check",
                        return_value=(True, "")), \
             mock.patch.object(orchestrator, "_pull_main"), \
             mock.patch.object(orchestrator, "_check_divergence"), \
             mock.patch.object(paths, "vault_root_or_cwd", lambda *a, **k: Path.cwd()), \
             redirect_stderr(io.StringIO()) as err:
            out = self._run(
                ["--registry", str(self.vault / "Worker" / "agents" / "registry.yaml"),
                 "--ledger", str(self.vault / "Worker" / "assignments"),
                 "--uri", f"file://{self.vault / 'Worker' / 'agents'}",
                 "auto", "--db", "Todo/todo.db"],
                cwd=self.elsewhere, vault_env=self.vault)
        # Since the missing-db message (2026-10-21) this no longer raises: the old
        # base misses the vault's todo.db, so the tick reports no candidates.
        self.assertIn("no task database", err.getvalue())
        self.assertIn(str(self.elsewhere), err.getvalue())
        self.assertIn("candidates=0", str(out))

    def test_status_with_the_old_cwd_base_cannot_find_the_registry(self):
        """RED control for the second resolution site: with the old cwd base the
        relative `--registry` default misses the vault's registry.yaml."""
        with mock.patch.object(paths, "vault_root_or_cwd", lambda *a, **k: Path.cwd()), \
             self.assertRaises(FileNotFoundError):
            self._run(["--uri", "file://Worker/agents", "status"],
                      cwd=self.elsewhere, vault_env=self.vault)

    def test_dashboard_writes_relative_out_into_the_vault(self):
        rc, out = self._run(
            ["--uri", "file://Worker/agents", "dashboard",
             "--out", "Todo/Orchestrator.md", "--db", "Todo/todo.db"],
            cwd=self.elsewhere, vault_env=self.vault)
        self.assertEqual(rc, 0)
        written = self.vault / "Todo" / "Orchestrator.md"
        self.assertTrue(written.exists())
        # The printed path names the file actually written, not the raw argument.
        self.assertIn(f"wrote {written}", out)
        self.assertFalse((self.elsewhere / "Todo" / "Orchestrator.md").exists())

    def test_agent_mailbox_uri_resolves_against_the_vault(self):
        """`_mailbox_for` opens each agent's own (possibly relative) `mailbox:`
        uri; it takes the same base, or a dispatch would write the envelope into
        a `Worker/agents` directory under the cwd."""
        with mock.patch("todo_system.orchestrator.hq_host.check",
                        return_value=(True, "")):
            rc, out = self._run(
                ["--uri", f"file://{self.vault / 'Worker' / 'agents'}",
                 "dispatch", "--task", "t-mb", "--payload", "{}"],
                cwd=self.elsewhere, vault_env=self.vault)
        self.assertEqual(rc, 0)
        self.assertIn("dispatched t-mb -> worker-default", out)
        inbox = self.vault / "Worker" / "agents" / "worker-default" / "inbox"
        self.assertEqual(len(list(inbox.glob("*.json"))), 1)
        self.assertFalse((self.elsewhere / "Worker").exists())


if __name__ == "__main__":
    unittest.main()


class AutoWithoutTaskDatabaseTest(unittest.TestCase):
    """A fresh vault has no Todo/todo.db until the first `cli import-md`; `auto`
    used to die with sqlite3.OperationalError. It now says what to run and still
    completes the tick with no candidates."""

    setUp = MainReadonlySubcommandsTest.setUp
    _main = MainReadonlySubcommandsTest._main

    def test_auto_explains_missing_db_and_ticks(self):
        from contextlib import redirect_stderr
        err = io.StringIO()
        with redirect_stderr(err):
            rc, out = self._main(["auto", "--db", str(self.tmp / "no-such" / "todo.db")])
        self.assertIn(rc, (0, None))
        self.assertIn("candidates=0", out)
        self.assertIn("cli import-md", err.getvalue())
