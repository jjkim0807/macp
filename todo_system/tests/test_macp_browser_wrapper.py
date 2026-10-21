"""The `macp-browser` wrapper is instance-aware (CLAUDE.md §12, leaf 2).

`--instance <name>` / MACP_BROWSER_INSTANCE pick a Chrome instance; `default`
(unset too) stays byte-identical to the single-profile wrapper — the historic
`$DIR/lease.json` and no CDP injection. A named/scratch instance gets its own
`lease.<name>.json` and its CDP address injected as MACP_BROWSER_CDP from that
instance's ports.env, so two sessions on different instances never wait on each
other. `scratch acquire|release|gc` manage throwaway login-less instances via
browser.sh scratch-up/scratch-down.

All Chrome is stubbed: a fake `node` that echoes its argv + MACP_BROWSER_CDP to
$MARK, and a fake `browser.sh` whose scratch-up/scratch-down create/remove a
`scratch/<name>/ports.env` the way leaf 1's real browser.sh does. No real
browser, no network.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from todo_system.orchestrator import stages

REPO_ROOT = Path(stages.__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "macp-browser"
OWNER_ENV = "MACP_BROWSER_OWNER"

# node stub: record argv AND the injected CDP address so the tests can assert
# the default path carries none and a named instance carries its port.
_STUB_NODE = '#!/bin/sh\necho "node $* cdp=${MACP_BROWSER_CDP:-}" >> "$MARK"\nexit "${STUB_RC:-0}"\n'

# browser.sh stub: echo to MARK, and make scratch-up/scratch-down create/remove a
# scratch/<name>/ports.env exactly where leaf 1's browser.sh writes it, so the
# wrapper's resolve_instance finds a CDP port. scratch-* on `default` is refused.
_STUB_BROWSER_SH = """#!/bin/sh
echo "browser.sh $*" >> "$MARK"
case "$1" in
  scratch-up)
    [ "$2" = default ] && exit 1
    mkdir -p "$MACP_BROWSER_DIR/scratch/$2"
    printf 'CDP_PORT=9278\\n' > "$MACP_BROWSER_DIR/scratch/$2/ports.env"
    ;;
  scratch-down)
    [ "$2" = default ] && exit 1
    rm -rf "$MACP_BROWSER_DIR/scratch/$2"
    ;;
esac
exit 0
"""

# browser.sh stub in the REAL format: browser.sh write_ports_env writes lowercase
# `name= kind= display= cdp= vnc= novnc= profile=` keys, and scratch-up ends by
# cat-ing that ports.env to stdout (the wrapper must keep it off its own stdout).
_STUB_BROWSER_SH_REAL = """#!/bin/sh
echo "browser.sh $*" >> "$MARK"
case "$1" in
  scratch-up)
    [ "$2" = default ] && exit 1
    d="$MACP_BROWSER_DIR/scratch/$2"
    mkdir -p "$d"
    {
      echo "name=$2"
      echo "kind=scratch"
      echo "display=:120"
      echo "cdp=${STUB_CDP:-9286}"
      echo "vnc=5990"
      echo "novnc=6170"
      echo "profile=$d/profile"
    } > "$d/ports.env"
    cat "$d/ports.env"
    ;;
  scratch-down)
    [ "$2" = default ] && exit 1
    rm -rf "$MACP_BROWSER_DIR/scratch/$2"
    ;;
esac
exit 0
"""


class _WrapperHarness:
    """setUp + helpers shared by the wrapper test classes; STUB picks browser.sh."""

    URL = "https://example.com"
    STUB = _STUB_BROWSER_SH

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.dir = self.tmp / "browser"
        self.dir.mkdir()
        (self.dir / "browser.sh").write_text(self.STUB, encoding="utf-8")
        for name in ("console.mjs", "text.mjs", "login-check.mjs"):
            (self.dir / name).write_text("", encoding="utf-8")
        self.node = self.tmp / "node"
        self.node.write_text(_STUB_NODE, encoding="utf-8")
        self.node.chmod(0o755)
        self.mark = self.tmp / "mark"

    def tearDown(self):
        self._tmp.cleanup()

    # --- helpers ----------------------------------------------------------
    def wrapper_env(self) -> dict:
        env = {**os.environ, "MACP_BROWSER_DIR": str(self.dir),
               "MACP_BROWSER_NODE": str(self.node), "MARK": str(self.mark)}
        for key in (OWNER_ENV, "STUB_RC", "MACP_BROWSER_INSTANCE", "STUB_BG", "STUB_CDP"):
            env.pop(key, None)
        return env

    def run_wrapper(self, *args, rc=None, owner=None, instance=None):
        env = self.wrapper_env()
        if rc is not None:
            env["STUB_RC"] = str(rc)
        if owner is not None:
            env[OWNER_ENV] = owner
        if instance is not None:
            env["MACP_BROWSER_INSTANCE"] = instance
        self.mark.unlink(missing_ok=True)
        return subprocess.run(["bash", str(WRAPPER), *args], cwd=self.tmp, env=env,
                              capture_output=True, text=True, timeout=30)

    def marked(self) -> str:
        return self.mark.read_text(encoding="utf-8") if self.mark.exists() else ""

    def make_instance(self, name, port):
        """A named (non-scratch) instance the way leaf 1's browser.sh would."""
        d = self.dir / "instances" / name
        d.mkdir(parents=True)
        (d / "ports.env").write_text(f"CDP_PORT={port}\n", encoding="utf-8")

    def lease_path(self, name=None) -> Path:
        return self.dir / ("lease.json" if name is None else f"lease.{name}.json")

    def acquire(self, owner, *extra, instance=None, rc=0):
        args = []
        if instance is not None:
            args += ["--instance", instance]
        args += ["lease", "acquire", "--owner", owner, *extra]
        out = self.run_wrapper(*args)
        self.assertEqual(out.returncode, rc, out.stderr)
        return out

    def expire(self, path: Path):
        text = path.read_text(encoding="utf-8")
        past = int(time.time()) - 60
        path.write_text(re.sub(r'"expires_epoch":\d+', f'"expires_epoch":{past}', text),
                        encoding="utf-8")


@unittest.skipUnless(WRAPPER.is_file(), "scripts/macp-browser not in this tree")
class MacpBrowserWrapperTest(_WrapperHarness, unittest.TestCase):
    # --- (a) default back-compat -----------------------------------------
    def test_default_path_and_port_back_compat(self):
        # probe/text with no --instance exec node with NO MACP_BROWSER_CDP.
        out = self.run_wrapper("probe", self.URL)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.marked(), f"node {self.dir}/console.mjs probe {self.URL} cdp=\n")
        out = self.run_wrapper("text", self.URL)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.marked(), f"node {self.dir}/text.mjs {self.URL} cdp=\n")
        # The default lease is still lease.json (not lease.default.json).
        self.acquire("run-a", "--purpose", "p")
        self.assertTrue(self.lease_path().exists())
        self.assertFalse(self.lease_path("default").exists())

    # --- (b) two-instance independence -----------------------------------
    def test_two_instances_are_independent(self):
        self.make_instance("a", 9278)
        self.make_instance("b", 9279)
        self.acquire("x", instance="a")
        # b acquires while a is held — both succeed, each its own file.
        self.acquire("y", instance="b")
        self.assertEqual(json.loads(self.lease_path("a").read_text())["owner"], "x")
        self.assertEqual(json.loads(self.lease_path("b").read_text())["owner"], "y")
        self.assertFalse(self.lease_path().exists())  # default untouched
        # a's holder (x) can probe a while b is held by y — independence — and
        # the probe carries a's own CDP port.
        out = self.run_wrapper("--instance", "a", "--owner", "x", "probe", self.URL)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(
            self.marked(),
            f"node {self.dir}/console.mjs probe {self.URL} cdp=http://127.0.0.1:9278\n")
        # The instance flag can also come from the env; b's holder (y) drives b.
        out = self.run_wrapper("probe", self.URL, instance="b", owner="y")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(
            self.marked(),
            f"node {self.dir}/console.mjs probe {self.URL} cdp=http://127.0.0.1:9279\n")

    def test_default_probe_runs_while_named_instance_is_held(self):
        self.make_instance("a", 9278)
        self.acquire("x", instance="a")
        out = self.run_wrapper("probe", self.URL)  # default lease is free
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.marked(), f"node {self.dir}/console.mjs probe {self.URL} cdp=\n")

    # --- (c) scratch acquire -> release ----------------------------------
    def test_scratch_acquire_then_release(self):
        out = self.run_wrapper("scratch", "acquire", "--owner", "x", "--purpose", "read a page")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.count("\n"), 1, out.stdout)
        shown = json.loads(out.stdout)
        name = shown["instance"]
        self.assertTrue(name.startswith("s-x-"), name)
        self.assertEqual(shown["cdp"], "http://127.0.0.1:9278")
        self.assertRegex(shown["expires_at"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
        self.assertIn(f"browser.sh scratch-up {name}", self.marked())
        self.assertTrue(self.lease_path(name).exists())
        self.assertTrue((self.dir / "scratch" / name / "ports.env").exists())
        # Release drops the lease and tears the SAME instance down.
        out = self.run_wrapper("scratch", "release", "--owner", "x")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(f"browser.sh scratch-down {name}", self.marked())
        self.assertNotIn("scratch-down default", self.marked())
        self.assertFalse(self.lease_path(name).exists())
        self.assertFalse((self.dir / "scratch" / name).exists())

    def test_scratch_release_by_instance_name(self):
        out = self.run_wrapper("scratch", "acquire", "--owner", "x")
        name = json.loads(out.stdout)["instance"]
        out = self.run_wrapper("scratch", "release", "--instance", name)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(f"browser.sh scratch-down {name}", self.marked())
        self.assertFalse((self.dir / "scratch" / name).exists())

    def test_scratch_release_nothing_is_fine(self):
        out = self.run_wrapper("scratch", "release", "--owner", "nobody")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("scratch-down", self.marked())

    # --- scratch gc ------------------------------------------------------
    def test_scratch_gc_tears_down_expired_only(self):
        dead = json.loads(self.run_wrapper("scratch", "acquire", "--owner", "x").stdout)["instance"]
        live = json.loads(self.run_wrapper("scratch", "acquire", "--owner", "y").stdout)["instance"]
        self.assertNotEqual(dead, live)
        self.expire(self.lease_path(dead))
        out = self.run_wrapper("scratch", "gc")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(f"browser.sh scratch-down {dead}", self.marked())
        self.assertNotIn(f"scratch-down {live}", self.marked())
        self.assertNotIn("scratch-down default", self.marked())
        self.assertFalse((self.dir / "scratch" / dead).exists())
        self.assertFalse(self.lease_path(dead).exists())
        self.assertTrue((self.dir / "scratch" / live).exists())
        self.assertTrue(self.lease_path(live).exists())

    # --- (d) login-check default only ------------------------------------
    def test_login_check_is_default_only(self):
        out = self.run_wrapper("--instance", "s1", "login-check")
        self.assertEqual(out.returncode, 64, out.stderr)
        self.assertEqual(self.marked(), "")
        out = self.run_wrapper("login-check", instance="other")
        self.assertEqual(out.returncode, 64, out.stderr)
        self.assertEqual(self.marked(), "")
        # default login-check still passes the tool's rc through.
        for rc in (0, 1, 2, 3):
            with self.subTest(rc=rc):
                out = self.run_wrapper("login-check", rc=rc)
                self.assertEqual(out.returncode, rc, out.stderr)
                self.assertEqual(self.marked(), f"node {self.dir}/login-check.mjs cdp=\n")

    # --- lease status --all ----------------------------------------------
    def test_lease_status_all_lists_every_instance(self):
        self.make_instance("a", 9278)
        self.acquire("d")                      # default -> lease.json
        self.acquire("x", instance="a")        # -> lease.a.json
        out = self.run_wrapper("lease", "status", "--all")
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = [json.loads(ln) for ln in out.stdout.splitlines() if ln.strip()]
        by_inst = {ln["instance"]: ln for ln in lines}
        self.assertEqual(set(by_inst), {"default", "a"})
        self.assertEqual(by_inst["default"]["owner"], "d")
        self.assertEqual(by_inst["a"]["owner"], "x")
        for ln in lines:
            self.assertEqual(ln["state"], "held")

    # --- usage rejection --------------------------------------------------
    def test_scratch_usage_errors_exit_64(self):
        rejected = [
            ("scratch",), ("scratch", "bogus"),
            ("scratch", "acquire"),
            ("scratch", "acquire", "--owner", "a b"),
            ("scratch", "acquire", "--owner", "x", "--ttl", "0"),
            ("scratch", "acquire", "--owner", "x", "--instance", "y"),
            ("scratch", "release", "--owner", "x", "--purpose", "p"),
            ("scratch", "gc", "--owner", "x"),
            ("lease", "acquire", "--owner", "x", "--all"),
        ]
        for args in rejected:
            with self.subTest(args=args):
                out = self.run_wrapper(*args)
                self.assertEqual(out.returncode, 64, out.stderr)
                self.assertIn("usage: macp-browser", out.stderr)
                self.assertEqual(self.marked(), "")


@unittest.skipUnless(WRAPPER.is_file(), "scripts/macp-browser not in this tree")
class MacpBrowserRealPortsEnvTest(_WrapperHarness, unittest.TestCase):
    """ports.env as the real browser.sh writes it: lowercase `cdp=<port>`."""

    STUB = _STUB_BROWSER_SH_REAL

    def test_scratch_acquire_cdp_from_real_ports_env(self):
        out = self.run_wrapper("scratch", "acquire", "--owner", "x")
        self.assertEqual(out.returncode, 0, out.stderr)
        # The ports.env that scratch-up cats goes to stderr, never into the JSON.
        self.assertEqual(out.stdout.count("\n"), 1, out.stdout)
        shown = json.loads(out.stdout)
        self.assertEqual(shown["cdp"], "http://127.0.0.1:9286")
        self.assertIn("cdp=9286", out.stderr)

    def test_named_instance_lowercase_cdp_is_injected(self):
        d = self.dir / "instances" / "a"
        d.mkdir(parents=True)
        (d / "ports.env").write_text("name=a\nkind=named\ncdp=9281\n", encoding="utf-8")
        self.acquire("x", instance="a")
        out = self.run_wrapper("--instance", "a", "--owner", "x", "probe", self.URL)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(
            self.marked(),
            f"node {self.dir}/console.mjs probe {self.URL} cdp=http://127.0.0.1:9281\n")

    def test_ports_env_bad_cdp_leaves_cdp_empty(self):
        for bad in ("abc", "123456"):
            with self.subTest(cdp=bad):
                d = self.dir / "instances" / f"bad{len(bad)}"
                d.mkdir(parents=True)
                (d / "ports.env").write_text(f"name=a\ncdp={bad}\n", encoding="utf-8")
                out = self.run_wrapper("--instance", d.name, "probe", self.URL)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(
                    self.marked(), f"node {self.dir}/console.mjs probe {self.URL} cdp=\n")


if __name__ == "__main__":
    unittest.main()
