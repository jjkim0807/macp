"""Worker stages may drive the HQ's logged-in Chrome (CLAUDE.md §12) without a
task `allowed_tools` edit.

The whitelist splits on spaces, so the tools are granted through one
space-free PATH wrapper rule, stages.BROWSER_TOOL_RULE == `Bash(macp-browser:*)`.
These tests pin which stages get it, that delegate gets nothing broader, that
research/plan stay read-only and that a task's own list keeps it. The wrapper —
not the rule — is what narrows it to `browser.sh status|up` and the fixed node
tools, so it is run against a stub folder to pin what it dispatches and what it
refuses — including the advisory lease that keeps one owner on the one Chrome
profile at a time (exit 75).
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from todo_system import task_validate
from todo_system.orchestrator import stages

REPO_ROOT = Path(stages.__file__).resolve().parents[2]
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
WRAPPER = REPO_ROOT / "scripts" / "macp-browser"
RULE = stages.BROWSER_TOOL_RULE
OWNER_ENV = "MACP_BROWSER_OWNER"


def _section_12(text: str) -> str:
    body = text[text.index("\n## 12.") + 1:]
    end = body.find("\n## ")
    return body if end == -1 else body[:end]


class BrowserToolWiringTest(unittest.TestCase):

    def test_write_stages_grant_the_rule_once(self):
        self.assertEqual(set(stages.BROWSER_TOOL_STAGES),
                         {"implement", "verify", "quick", "delegate"})
        for stage in stages.BROWSER_TOOL_STAGES:
            with self.subTest(stage=stage):
                self.assertEqual(stages._safe_tools(stage).count(RULE), 1)

    def test_other_stages_do_not_get_it(self):
        for stage in ("research", "plan", "report", "discover"):
            with self.subTest(stage=stage):
                self.assertNotIn(RULE, stages._safe_tools(stage))
        self.assertNotIn(RULE, stages.FALLBACK_RUNNER_TOOLS)

    def test_research_and_plan_stay_read_only(self):
        self.assertEqual(stages.STAGE_TOOLS["research"], "Read Write")
        self.assertEqual(stages.STAGE_TOOLS["plan"], "Read Write")

    def test_delegate_gets_no_broad_runner(self):
        granted = stages.STAGE_TOOLS["delegate"].split()
        for broad in ("Bash(node:*)", "Bash(npx:*)", "Bash(bash:*)", "Bash(uv:*)",
                      "Bash(python3:*)", "Bash(python:*)"):
            with self.subTest(rule=broad):
                self.assertNotIn(broad, granted)

    def test_rule_is_one_space_free_token(self):
        self.assertEqual(RULE, "Bash(macp-browser:*)")
        self.assertEqual(stages.BROWSER_CLI, WRAPPER.name)
        self.assertNotIn(" ", RULE)
        for bad in stages.FORBIDDEN_TOOL_SUBSTRINGS:
            self.assertNotIn(bad, RULE)
        self.assertEqual(task_validate._check_bash_patterns(RULE), [])

    def test_task_override_keeps_the_rule(self):
        self.assertEqual(stages._safe_tools("implement", "Read,Bash(gcloud:*)"),
                         ["Read", "Bash(gcloud:*)", stages.SMS_TOOL_RULE, RULE,
                          stages.REMOTE_GATE_TOOL_RULE, stages.EMULATOR_TOOL_RULE,
                          stages.MAIL_TOOL_RULE])
        # Already listed by the task: exactly once.
        self.assertEqual(stages._safe_tools("implement", f"Read,{RULE}"),
                         ["Read", RULE, stages.SMS_TOOL_RULE,
                          stages.REMOTE_GATE_TOOL_RULE, stages.EMULATOR_TOOL_RULE,
                          stages.MAIL_TOOL_RULE])
        # Stages a task cannot widen still return their own default untouched.
        self.assertEqual(stages._safe_tools("verify", "Read,Bash(gcloud:*)"),
                         stages.STAGE_TOOLS["verify"].split())

    def test_owner_env_constant(self):
        self.assertEqual(stages.BROWSER_OWNER_ENV, OWNER_ENV)

    def test_with_browser_owner_sets_only_browser_stages(self):
        base = {"PATH": "/bin", OWNER_ENV: "stale-owner"}
        snapshot = dict(base)
        for stage in stages.BROWSER_TOOL_STAGES:
            with self.subTest(stage=stage):
                env = stages.with_browser_owner(stage, base, "RUNID-x")
                self.assertEqual(env[OWNER_ENV], "RUNID-x")
                self.assertEqual(env["PATH"], "/bin")
        for stage in ("research", "plan", "report", "discover"):
            with self.subTest(stage=stage):
                env = stages.with_browser_owner(stage, base, "RUNID-x")
                self.assertNotIn(OWNER_ENV, env)
                self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(base, snapshot)

    def test_stage_prompt_names_the_lease_only_where_granted(self):
        with tempfile.TemporaryDirectory() as tmp:
            def prompt(stage):
                return stages.build_stage_argv(
                    stage, task_id="t", run_id="RUNID-x", run_dir=Path(tmp),
                    branch="worker/t", worker_root=Path(tmp) / "none")[2]
            for stage in stages.BROWSER_TOOL_STAGES:
                with self.subTest(stage=stage):
                    text = prompt(stage)
                    for needle in (f"{stages.BROWSER_CLI} lease acquire",
                                   f"{stages.BROWSER_CLI} lease release",
                                   f"{stages.BROWSER_CLI} scratch acquire",
                                   f"`{OWNER_ENV}` is already `RUNID-x`", "exit 75"):
                        self.assertIn(needle, text)
            for stage in ("research", "plan", "report"):
                with self.subTest(stage=stage):
                    self.assertNotIn("lease acquire", prompt(stage))
                    self.assertNotIn("scratch acquire", prompt(stage))

    def test_engine_code_does_not_hardcode_the_folder(self):
        source = Path(stages.__file__).read_text(encoding="utf-8")
        self.assertNotIn(".config/macp-browser", source)

    @unittest.skipUnless(CLAUDE_MD.is_file(),
                         "vault-only: the engine-only export tree has no vault CLAUDE.md")
    def test_claude_md_section_12_names_the_wrapper(self):
        section = _section_12(CLAUDE_MD.read_text(encoding="utf-8"))
        for needle in (RULE, f"{stages.BROWSER_CLI} status", f"{stages.BROWSER_CLI} up",
                       f"{stages.BROWSER_CLI} login-check", f"{stages.BROWSER_CLI} probe",
                       f"{stages.BROWSER_CLI} steps", f"{stages.BROWSER_CLI} text",
                       "console.mjs", "text.mjs", "connectOverCDP",
                       "launchPersistentContext", "reset-vnc", "vnc.html",
                       "명시적으로 허용",
                       # the lease: one owner at a time, humans and one-off
                       # CDP scripts included, plus the tab etiquette
                       f"{stages.BROWSER_CLI} lease acquire",
                       f"{stages.BROWSER_CLI} lease release",
                       f"{stages.BROWSER_CLI} lease status", "exit 75",
                       "--owner human-vnc", "bringToFront", stages.BROWSER_OWNER_ENV,
                       # the install step survives every wrapper change
                       "cp scripts/macp-browser ~/.local/bin/macp-browser"):
            with self.subTest(needle=needle):
                self.assertIn(needle, section)


_STUB_NODE = '#!/bin/sh\necho "node $*" >> "$MARK"\nexit "${STUB_RC:-0}"\n'
_STUB_BROWSER_SH = 'echo "browser.sh $*" >> "$MARK"\n'


class _WrapperFixture:
    """Stub browser folder + stub node; MARK records what the wrapper ran."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.dir = self.tmp / "browser"
        self.dir.mkdir()
        (self.dir / "browser.sh").write_text(_STUB_BROWSER_SH, encoding="utf-8")
        for name in ("console.mjs", "text.mjs", "login-check.mjs", "mma.mjs"):
            (self.dir / name).write_text("", encoding="utf-8")
        self.node = self.tmp / "node"
        self.node.write_text(_STUB_NODE, encoding="utf-8")
        self.node.chmod(0o755)
        self.mark = self.tmp / "mark"

    def tearDown(self):
        self._tmp.cleanup()

    def wrapper_env(self, browser_dir=None, rc=None, owner=None) -> dict:
        env = {**os.environ, "MACP_BROWSER_DIR": str(browser_dir or self.dir),
               "MACP_BROWSER_NODE": str(self.node), "MARK": str(self.mark)}
        # Worker stages now carry an owner in their env; keep the tests hermetic.
        env.pop(OWNER_ENV, None)
        env.pop("STUB_RC", None)
        if rc is not None:
            env["STUB_RC"] = str(rc)
        if owner is not None:
            env[OWNER_ENV] = owner
        return env

    def run_wrapper(self, *args, browser_dir=None, rc=None, owner=None):
        env = self.wrapper_env(browser_dir, rc, owner)
        self.mark.unlink(missing_ok=True)
        return subprocess.run(["bash", str(WRAPPER), *args], cwd=self.tmp, env=env,
                              capture_output=True, text=True, timeout=30)

    def marked(self) -> str:
        return self.mark.read_text(encoding="utf-8") if self.mark.exists() else ""


@unittest.skipUnless(WRAPPER.is_file(), "scripts/macp-browser not in this tree")
class BrowserWrapperTest(_WrapperFixture, unittest.TestCase):

    def test_wrapper_is_named_and_executable(self):
        self.assertEqual(WRAPPER.name, "macp-browser")
        self.assertTrue(os.access(WRAPPER, os.X_OK), "scripts/macp-browser must be 0755")

    def test_browser_sh_status_and_up(self):
        for cmd in ("status", "up"):
            with self.subTest(cmd=cmd):
                out = self.run_wrapper(cmd)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(self.marked(), f"browser.sh {cmd}\n")

    def test_node_tools_dispatch(self):
        steps = '[{"click":"x"}]'
        cases = [
            (("probe", "https://example.com"),
             f"node {self.dir}/console.mjs probe https://example.com\n"),
            (("steps", "https://example.com", steps),
             f"node {self.dir}/console.mjs steps https://example.com {steps}\n"),
            (("text", "https://example.com"),
             f"node {self.dir}/text.mjs https://example.com\n"),
            (("text", "https://example.com", "3000"),
             f"node {self.dir}/text.mjs https://example.com 3000\n"),
            (("login-check",), f"node {self.dir}/login-check.mjs\n"),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                out = self.run_wrapper(*args)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(self.marked(), expected)

    def test_login_check_exit_code_passes_through(self):
        for rc in (0, 1, 2, 3):
            with self.subTest(rc=rc):
                self.assertEqual(self.run_wrapper("login-check", rc=rc).returncode, rc)

    def test_rejected_commands_run_nothing(self):
        rejected = [
            ("stop",), ("reset-vnc",), ("start",), ("vnc-off",), ("chrome-off",),
            ("mma.mjs",), ("console.mjs",), ("../x",), (), ("",),
            ("status", "extra"), ("up", "extra"), ("probe",), ("probe", "a", "b"),
            ("steps", "u"), ("steps", "u", "[]", "x"), ("text",),
            ("text", "u", "notanumber"), ("text", "u", "1", "2"),
            ("login-check", "extra"),
        ]
        for args in rejected:
            with self.subTest(args=args):
                out = self.run_wrapper(*args)
                self.assertEqual(out.returncode, 64, out.stderr)
                self.assertIn("usage: macp-browser", out.stderr)
                self.assertEqual(self.marked(), "")

    def test_help_exits_zero(self):
        out = self.run_wrapper("--help")
        self.assertEqual(out.returncode, 0)
        self.assertIn("login-check", out.stderr)

    def test_missing_folder_exits_69(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        for cmd, tool in (("status", "browser.sh"), ("login-check", "login-check.mjs"),
                          ("probe", "console.mjs")):
            with self.subTest(cmd=cmd):
                args = (cmd, "https://example.com") if cmd == "probe" else (cmd,)
                out = self.run_wrapper(*args, browser_dir=empty)
                self.assertEqual(out.returncode, 69)
                self.assertIn(tool, out.stderr)
                self.assertEqual(self.marked(), "")


@unittest.skipUnless(WRAPPER.is_file(), "scripts/macp-browser not in this tree")
class BrowserLeaseTest(_WrapperFixture, unittest.TestCase):
    """One Chrome profile, one owner at a time: the advisory lease."""

    URL = "https://example.com"

    def lease_path(self) -> Path:
        return self.dir / "lease.json"

    def lease_file(self) -> dict:
        return json.loads(self.lease_path().read_text(encoding="utf-8"))

    def acquire(self, owner, *extra, rc=0):
        out = self.run_wrapper("lease", "acquire", "--owner", owner, *extra)
        self.assertEqual(out.returncode, rc, out.stderr)
        return out

    def status(self) -> dict:
        out = self.run_wrapper("lease", "status")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.count("\n"), 1)
        return json.loads(out.stdout)

    def expire(self):
        text = self.lease_path().read_text(encoding="utf-8")
        past = int(time.time()) - 60
        self.lease_path().write_text(
            re.sub(r'"expires_epoch":\d+', f'"expires_epoch":{past}', text),
            encoding="utf-8")

    def test_acquire_writes_0600_lease(self):
        out = self.acquire("run-a", "--purpose", "hometax login")
        shown = json.loads(out.stdout)
        self.assertEqual((shown["state"], shown["owner"], shown["purpose"]),
                         ("held", "run-a", "hometax login"))
        self.assertEqual(stat.S_IMODE(self.lease_path().stat().st_mode), 0o600)
        data = self.lease_file()
        for key in ("owner", "purpose", "acquired_at", "expires_at"):
            self.assertIn(key, data)
        iso = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
        self.assertRegex(data["expires_at"], iso)
        self.assertRegex(data["acquired_at"], iso)
        self.assertEqual(shown["expires_at"], data["expires_at"])
        self.assertAlmostEqual(data["expires_epoch"], time.time() + 30 * 60, delta=30)
        self.assertEqual(self.marked(), "")

    def test_same_owner_reacquire_extends_only(self):
        self.acquire("run-a", "--purpose", "first", "--ttl", "10")
        before = self.lease_file()
        self.acquire("run-a", "--ttl", "60")
        after = self.lease_file()
        self.assertGreater(after["expires_epoch"], before["expires_epoch"])
        self.assertEqual(after["acquired_at"], before["acquired_at"])
        self.assertEqual(after["purpose"], "first")
        self.assertEqual(after["owner"], "run-a")
        # A new purpose on re-acquire replaces the old one.
        self.acquire("run-a", "--purpose", "second")
        self.assertEqual(self.lease_file()["purpose"], "second")

    def test_other_owner_is_refused_with_one_line(self):
        self.acquire("run-a", "--purpose", "hometax login")
        before = self.lease_path().read_bytes()
        out = self.acquire("run-b", rc=75)
        self.assertEqual(out.stderr.count("\n"), 1, out.stderr)
        for needle in ("run-a", "hometax login", self.lease_file()["expires_at"]):
            self.assertIn(needle, out.stderr)
        self.assertEqual(out.stdout, "")
        self.assertEqual(self.lease_path().read_bytes(), before)

    def test_wait_times_out_then_75(self):
        self.acquire("run-a")
        start = time.monotonic()
        self.acquire("run-b", "--wait", "1", rc=75)
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 1.0)
        self.assertLess(elapsed, 10.0)
        self.assertEqual(self.lease_file()["owner"], "run-a")

    def test_wait_succeeds_when_holder_releases(self):
        self.acquire("run-a")
        waiter = subprocess.Popen(
            ["bash", str(WRAPPER), "lease", "acquire", "--owner", "run-b", "--wait", "10"],
            cwd=self.tmp, env=self.wrapper_env(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(0.5)
            self.assertEqual(self.run_wrapper("lease", "release", "--owner", "run-a")
                             .returncode, 0)
            out, err = waiter.communicate(timeout=30)
        finally:
            if waiter.poll() is None:
                waiter.kill()
                waiter.communicate()
        self.assertEqual(waiter.returncode, 0, err)
        self.assertEqual(json.loads(out)["owner"], "run-b")
        self.assertEqual(self.lease_file()["owner"], "run-b")

    def test_release_only_own(self):
        self.acquire("run-a")
        before = self.lease_path().read_bytes()
        out = self.run_wrapper("lease", "release", "--owner", "run-b")
        self.assertEqual(out.returncode, 75)
        self.assertIn("run-a", out.stderr)
        self.assertEqual(self.lease_path().read_bytes(), before)
        # The env owner counts the same as --owner.
        self.assertEqual(self.run_wrapper("lease", "release", owner="run-b").returncode, 75)
        self.assertEqual(self.run_wrapper("lease", "release", owner="run-a").returncode, 0)
        self.assertFalse(self.lease_path().exists())
        self.assertEqual(self.status()["state"], "free")
        # Releasing nothing is fine.
        self.assertEqual(self.run_wrapper("lease", "release", "--owner", "run-a")
                         .returncode, 0)

    def test_expired_lease_counts_as_free(self):
        self.acquire("run-a", "--purpose", "old")
        self.expire()
        self.assertEqual(self.status(),
                         {"state": "free", "owner": None, "purpose": None,
                          "expires_at": None})
        self.acquire("run-b")
        data = self.lease_file()
        self.assertEqual(data["owner"], "run-b")
        self.assertEqual(data["purpose"], "")  # nothing inherited from run-a
        self.expire()
        out = self.run_wrapper("probe", self.URL)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.marked(), f"node {self.dir}/console.mjs probe {self.URL}\n")

    def test_corrupt_lease_counts_as_free(self):
        self.lease_path().write_text("garbage", encoding="utf-8")
        self.assertEqual(self.status()["state"], "free")
        self.acquire("run-b")
        self.assertEqual(self.lease_file()["owner"], "run-b")

    def test_probe_steps_text_gated(self):
        self.acquire("run-a", "--purpose", "hometax login")
        steps = '[{"click":"x"}]'
        cases = [
            (("probe", self.URL), f"node {self.dir}/console.mjs probe {self.URL}\n"),
            (("steps", self.URL, steps),
             f"node {self.dir}/console.mjs steps {self.URL} {steps}\n"),
            (("text", self.URL), f"node {self.dir}/text.mjs {self.URL}\n"),
        ]
        for args, expected in cases:
            for prefix, owner in (((), None), ((), "run-b"), (("--owner", "run-b"), None)):
                with self.subTest(args=args, prefix=prefix, owner=owner):
                    out = self.run_wrapper(*prefix, *args, owner=owner)
                    self.assertEqual(out.returncode, 75, out.stderr)
                    self.assertIn("run-a", out.stderr)
                    self.assertEqual(self.marked(), "")
            for prefix, owner in (((), "run-a"), (("--owner", "run-a"), None),
                                  (("--owner", "run-a"), "run-b")):
                with self.subTest(args=args, prefix=prefix, owner=owner):
                    out = self.run_wrapper(*prefix, *args, owner=owner)
                    self.assertEqual(out.returncode, 0, out.stderr)
                    self.assertEqual(self.marked(), expected)

    def test_read_only_commands_ignore_lease(self):
        self.acquire("run-a")
        for cmd in ("status", "up"):
            with self.subTest(cmd=cmd):
                out = self.run_wrapper(cmd)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(self.marked(), f"browser.sh {cmd}\n")
        for rc in (0, 1, 2, 3):
            with self.subTest(cmd="login-check", rc=rc):
                out = self.run_wrapper("login-check", rc=rc)
                self.assertEqual(out.returncode, rc, out.stderr)
                self.assertEqual(self.marked(), f"node {self.dir}/login-check.mjs\n")

    def test_no_lease_no_owner_runs_as_before(self):
        self.assertFalse(self.lease_path().exists())
        out = self.run_wrapper("probe", self.URL)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.marked(), f"node {self.dir}/console.mjs probe {self.URL}\n")
        self.assertFalse(self.lease_path().exists())

    def test_purpose_json_round_trip(self):
        purpose = 'say "hi" \\ 홈택스 & done'
        out = self.acquire("run-a", "--purpose", purpose)
        self.assertEqual(json.loads(out.stdout)["purpose"], purpose)
        self.assertEqual(self.lease_file()["purpose"], purpose)
        self.assertEqual(self.status()["purpose"], purpose)
        # Control characters are dropped, not written raw into the JSON line.
        self.acquire("run-a", "--purpose", "line1\nline2\ttab")
        self.assertEqual(self.lease_file()["purpose"], "line1line2tab")
        self.assertEqual(self.lease_path().read_text(encoding="utf-8").count("\n"), 1)
        # Long purposes are capped without splitting an escape.
        self.acquire("run-a", "--purpose", "\\" * 300)
        self.assertEqual(self.lease_file()["purpose"], "\\" * 200)

    def test_lease_usage_errors_exit_64(self):
        rejected = [
            ("lease",), ("lease", "bogus"), ("lease", "acquire"),
            ("lease", "acquire", "--owner", "a b"),
            ("lease", "acquire", "--owner", "x", "--ttl", "0"),
            ("lease", "acquire", "--owner", "x", "--ttl", "abc"),
            ("lease", "acquire", "--owner", "x", "--ttl", "1441"),
            ("lease", "acquire", "--owner", "x", "--wait", "-1"),
            ("lease", "acquire", "--owner", "x", "--wait", "99999"),
            ("lease", "acquire", "--owner", "x", "--bogus", "1"),
            ("lease", "status", "extra"), ("lease", "status", "--owner", "x"),
            ("lease", "release"), ("lease", "release", "--owner", "x", "--purpose", "p"),
            ("lease", "acquire", "--owner"),
            ("--owner",), ("--owner", "run-a"), ("--owner", "bad owner", "probe", "u"),
        ]
        for args in rejected:
            with self.subTest(args=args):
                out = self.run_wrapper(*args)
                self.assertEqual(out.returncode, 64, out.stderr)
                self.assertIn("usage: macp-browser", out.stderr)
                self.assertFalse(self.lease_path().exists())
                self.assertEqual(self.marked(), "")

    def test_lease_missing_folder_exits_69(self):
        missing = self.tmp / "missing"
        for args in (("lease", "status"), ("lease", "acquire", "--owner", "x"),
                     ("lease", "release", "--owner", "x")):
            with self.subTest(args=args):
                out = self.run_wrapper(*args, browser_dir=missing)
                self.assertEqual(out.returncode, 69, out.stderr)
                self.assertIn("not found", out.stderr)
                self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
