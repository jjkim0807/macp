"""CLI + auth-watch regression tests for the multi-account layer (leaf ⑤).

The CLI tests drive ``macp.__main__.main(argv)`` so the argparse wiring is
exercised too. Every run is hermetic: ``MACP_HOME``, the runtime-state file
(``MACP_CLAUDE_ACCOUNTS_STATE``), the usage-guard state dir
(``USAGE_GUARD_STATE_DIR``) and ``MACP_VAULT_ROOT`` all point at a temp dir, and
cwd is a temp dir that is not a vault. No real ``claude``, no network, no
personal identifiers: account names are ``school`` / ``dev``, paths are tmp.
"""
from __future__ import annotations

import importlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from todo_system.macp import __main__ as M
from todo_system.macp import accounts as ACC
from todo_system.macp import config as C


def _load_auth_watch():
    """Load scripts/claude_auth_watch.py by path (scripts/ has no __init__.py),
    as the existing test_claude_auth_watch.py does. Account resolution happens at
    call time, so one load serves every case."""
    src = Path(__file__).resolve().parents[2] / "scripts" / "claude_auth_watch.py"
    spec = importlib.util.spec_from_file_location("claude_auth_watch", src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


A = _load_auth_watch()


class AccountCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self._env = {k: os.environ.get(k) for k in (
            "MACP_HOME", "MACP_CLAUDE_ACCOUNTS_STATE",
            "USAGE_GUARD_STATE_DIR", "MACP_VAULT_ROOT")}
        self._cwd = os.getcwd()
        self.addCleanup(self._restore)
        os.environ["MACP_HOME"] = str(self.tmp / "home")
        self.state_file = self.tmp / "state" / "claude_accounts.json"
        self.state_file.parent.mkdir(parents=True)
        os.environ["MACP_CLAUDE_ACCOUNTS_STATE"] = str(self.state_file)
        self.usage_dir = self.tmp / "usage"
        self.usage_dir.mkdir()
        os.environ["USAGE_GUARD_STATE_DIR"] = str(self.usage_dir)
        os.environ["MACP_VAULT_ROOT"] = str(self.tmp / "vault")
        importlib.reload(C)   # config caches HOME/PROFILES at import
        self.school_dir = self.tmp / "accts" / "school"
        self.dev_dir = self.tmp / "accts" / "dev"
        for d in (self.school_dir, self.dev_dir):
            d.mkdir(parents=True)
        self.outside = self.tmp / "outside"
        self.outside.mkdir()

    def _restore(self):
        os.chdir(self._cwd)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(C)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cli(self, *argv) -> tuple[int, str]:
        os.chdir(self.outside)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = M.main(list(argv))
        return rc, out.getvalue()

    def _profile(self, *, active="school"):
        C.write_profile(C.Profile(name="work", active_account=active, accounts=[
            C.Account(name="school", agent="claude", claude_config_dir=str(self.school_dir)),
            C.Account(name="dev", agent="claude", claude_config_dir=str(self.dev_dir)),
        ]))

    def _write_state(self, data):
        self.state_file.write_text(json.dumps(data), encoding="utf-8")

    def _write_usage(self, data):
        (self.usage_dir / "latest.json").write_text(json.dumps(data), encoding="utf-8")

    @staticmethod
    def _line(out, needle):
        return next(ln for ln in out.splitlines() if needle in ln)

    # ---- list -------------------------------------------------------------

    def test_list_detailed_line_per_account(self):
        self._profile()
        # auth material present for school only; secret VALUES must never print
        (self.school_dir / ".credentials.json").write_text(
            '{"accessToken": "SECRET-access"}', encoding="utf-8")
        (self.school_dir / "automation-token").write_text("SECRET-token-body", encoding="utf-8")
        self._write_usage({"accounts": {"school": {"usage": {
            "five_hour": {"utilization": 42.0}, "seven_day": {"utilization": 13.0}}}}})
        # dev is sticky-unavailable (auth), stamped with its dir, no mtime keys
        self._write_state({"unavailable": {"dev": {
            "reason": "auth", "since": "2026-10-16T00:00:00+00:00",
            "until": None, "config_dir": str(self.dev_dir)}}})

        rc, out = self.cli("account", "list", "--profile", "work")
        self.assertEqual(rc, 0)

        school = self._line(out, "school [claude]")
        self.assertTrue(school.startswith("* "))                 # active mark
        self.assertIn(f"school [claude]: {self.school_dir}", school)
        self.assertIn("priority=100", school)
        self.assertIn("creds=yes", school)
        self.assertIn("token=yes", school)
        self.assertIn("status=available", school)
        self.assertIn("session=42%", school)
        self.assertIn("weekly=13%", school)

        dev = self._line(out, "dev [claude]")
        self.assertFalse(dev.startswith("* "))                   # not active
        self.assertIn("creds=no", dev)
        self.assertIn("token=no", dev)
        self.assertIn("status=auth", dev)                        # reason surfaced
        self.assertIn("session=-", dev)
        self.assertIn("weekly=-", dev)

        self.assertNotIn("SECRET", out)                          # no token/credential values

    def test_list_empty_profile(self):
        C.write_profile(C.Profile(name="work"))
        rc, out = self.cli("account", "list", "--profile", "work")
        self.assertEqual(rc, 0)
        self.assertIn("(no accounts)", out)

    def test_list_missing_profile(self):
        rc, out = self.cli("account", "list", "--profile", "nope")
        self.assertEqual(rc, 2)
        self.assertIn("error: profile 'nope' not found", out)

    def test_list_non_claude_account_keeps_short_form(self):
        C.write_profile(C.Profile(name="work", accounts=[
            C.Account(name="cx", agent="codex", command="codex exec {prompt}"),
        ]))
        rc, out = self.cli("account", "list", "--profile", "work")
        self.assertEqual(rc, 0)
        self.assertIn("cx [codex]: codex exec {prompt}", out)

    def test_list_shows_parallel_slots_from_runner_snapshot(self):
        self._profile()
        slots = self.tmp / "runner_slots.json"
        os.environ["RUNNER_SLOTS_STATE"] = str(slots)
        self.addCleanup(lambda: os.environ.pop("RUNNER_SLOTS_STATE", None))
        slots.write_text(json.dumps({"total_used": 1, "total_max": 4, "per_account": {
            "school": {"used": 1, "max": 3}, "dev": {"used": 0, "max": 1}}}), encoding="utf-8")
        rc, out = self.cli("account", "list", "--profile", "work")
        self.assertEqual(rc, 0)
        self.assertIn("slots=1/3", self._line(out, "school [claude]"))
        self.assertIn("slots=0/1", self._line(out, "dev [claude]"))

    def test_list_slots_are_dashes_without_a_runner_snapshot(self):
        self._profile()
        os.environ["RUNNER_SLOTS_STATE"] = str(self.tmp / "absent.json")
        self.addCleanup(lambda: os.environ.pop("RUNNER_SLOTS_STATE", None))
        rc, out = self.cli("account", "list", "--profile", "work")
        self.assertEqual(rc, 0)
        self.assertIn("slots=-/-", self._line(out, "school [claude]"))

    # ---- status -----------------------------------------------------------

    def test_status_reports_active_and_runtime(self):
        self._profile(active="school")
        self._write_state({
            "paused_since": None,
            "last_switch": {"from": "school", "to": "dev", "reason": "exhausted",
                            "ts": "2026-10-16T01:00:00+00:00"},
        })
        rc, out = self.cli("account", "status", "--profile", "work")
        self.assertEqual(rc, 0)
        self.assertIn("active account: school", out)
        self.assertIn("source: profile", out)
        self.assertIn("paused: no", out)
        self.assertIn("last switch: school → dev (exhausted) at 2026-10-16T01:00:00+00:00", out)
        # a bare tmp dir is not the shared layout → drift is reported, not "intact"
        self.assertIn("layout drift:", out)

    def test_status_no_claude_account(self):
        C.write_profile(C.Profile(name="work"))
        rc, out = self.cli("account", "status", "--profile", "work")
        self.assertEqual(rc, 0)
        self.assertIn("active account: -", out)
        self.assertIn("source: none", out)
        self.assertIn("layout drift: n/a", out)

    def test_status_missing_profile(self):
        rc, out = self.cli("account", "status", "--profile", "nope")
        self.assertEqual(rc, 2)
        self.assertIn("error: profile 'nope' not found", out)

    # ---- remove -----------------------------------------------------------

    def test_remove_refuses_active_account(self):
        self._profile(active="school")
        rc, out = self.cli("account", "remove", "school", "--profile", "work")
        self.assertEqual(rc, 2)
        self.assertIn("is the active account", out)
        self.assertEqual({x.name for x in C.load_profile("work").accounts}, {"school", "dev"})
        self.assertTrue(self.school_dir.is_dir())

    def test_remove_nonactive_keeps_dir_and_profile_only(self):
        self._profile(active="school")
        rc, out = self.cli("account", "remove", "dev", "--profile", "work")
        self.assertEqual(rc, 0)
        self.assertIn("removed account 'dev'", out)
        self.assertIn("config dir left in place", out)
        self.assertEqual([x.name for x in C.load_profile("work").accounts], ["school"])
        self.assertTrue(self.dev_dir.is_dir())          # directory never deleted

    def test_remove_unknown_account(self):
        self._profile()
        rc, out = self.cli("account", "remove", "ghost", "--profile", "work")
        self.assertEqual(rc, 2)
        self.assertIn("no account 'ghost'", out)
        self.assertEqual(len(C.load_profile("work").accounts), 2)

    def test_remove_missing_name_prints_usage(self):
        self._profile()
        rc, out = self.cli("account", "remove", "--profile", "work")
        self.assertEqual(rc, 2)
        self.assertIn("usage: macp account remove", out)

    def test_remove_missing_profile(self):
        rc, out = self.cli("account", "remove", "x", "--profile", "nope")
        self.assertEqual(rc, 2)
        self.assertIn("error: profile 'nope' not found", out)


class AuthWatchTest(unittest.TestCase):
    """scripts/claude_auth_watch.py iterates the profile's claude accounts and
    marks only the ones whose login is dead. All file-based (IS_DARWIN forced
    False), tmp config dirs, tmp runtime state — no real claude, no network."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self._env = {k: os.environ.get(k) for k in
                     ("MACP_HOME", "MACP_CLAUDE_ACCOUNTS_STATE", "MACP_VAULT_ROOT")}
        self._cwd = os.getcwd()
        self._darwin = A.IS_DARWIN
        self.addCleanup(self._restore)
        os.environ["MACP_HOME"] = str(self.tmp / "home")
        self.state_file = self.tmp / "claude_accounts.json"
        os.environ["MACP_CLAUDE_ACCOUNTS_STATE"] = str(self.state_file)
        os.environ["MACP_VAULT_ROOT"] = str(self.tmp / "vault")
        importlib.reload(C)
        A.IS_DARWIN = False                 # exercise the Linux file-based path
        self.school_dir = self.tmp / "accts" / "school"
        self.dev_dir = self.tmp / "accts" / "dev"
        for d in (self.school_dir, self.dev_dir):
            d.mkdir(parents=True)
        self.outside = self.tmp / "outside"   # cwd must not be inside any .macp project
        self.outside.mkdir()
        os.chdir(self.outside)

    def _restore(self):
        os.chdir(self._cwd)
        A.IS_DARWIN = self._darwin
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(C)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _profile(self):
        C.write_profile(C.Profile(name="work", active_account="school", accounts=[
            C.Account(name="school", agent="claude", claude_config_dir=str(self.school_dir)),
            C.Account(name="dev", agent="claude", claude_config_dir=str(self.dev_dir)),
        ]))
        C.set_default_profile("work")

    def _creds(self, d, *, access="access", refresh="refresh", exp_days=60):
        exp = int((time.time() + exp_days * 86400) * 1000)
        (d / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
            "accessToken": access, "refreshToken": refresh,
            "refreshTokenExpiresAt": exp}}), encoding="utf-8")

    def _acct(self, name):
        return next(a for a in C.load_profile("work").accounts if a.name == name)

    def test_watch_visits_both_dirs_marks_only_logged_out(self):
        self._profile()
        self._creds(self.school_dir)                        # healthy
        self._creds(self.dev_dir, access="", refresh="")    # blanked → logged out
        watched = A._watched_accounts()
        self.assertEqual([n for n, _ in watched], ["school", "dev"])  # both accounts
        problems, notes = [], []
        A.check_accounts(watched, problems, notes)
        self.assertIsNone(ACC.unavailable_entry(self._acct("school")))      # not marked
        self.assertEqual(ACC.unavailable_reason(self._acct("dev")), "auth")  # marked
        self.assertTrue(any("/dev] " in p for p in problems))    # both dirs visited
        self.assertTrue(any("/school] " in n for n in notes))

    def test_expired_refresh_token_marks_account(self):
        self._profile()
        self._creds(self.school_dir)
        self._creds(self.dev_dir, exp_days=-1)              # refresh already expired
        A.check_accounts(A._watched_accounts(), [], [])
        self.assertEqual(ACC.unavailable_reason(self._acct("dev")), "auth")
        self.assertIsNone(ACC.unavailable_entry(self._acct("school")))

    def test_soon_to_expire_is_warned_not_marked(self):
        self._profile()
        self._creds(self.school_dir)
        self._creds(self.dev_dir, exp_days=3)              # within WARN_DAYS but valid
        problems = []
        A.check_accounts(A._watched_accounts(), problems, [])
        self.assertIsNone(ACC.unavailable_entry(self._acct("dev")))   # still valid → no mark
        self.assertTrue(any("/dev] " in p and "expires in" in p for p in problems))

    def test_no_profile_falls_back_to_single_legacy_and_never_marks(self):
        watched = A._watched_accounts()               # no profile written
        self.assertEqual(len(watched), 1)
        self.assertIsNone(watched[0][0])              # name None = legacy single dir
        A._mark_auth(None, self.dev_dir)              # a legacy account is never marked
        self.assertEqual(ACC.load_runtime_state(), {})


if __name__ == "__main__":
    unittest.main()
