"""Public split 13e: the two LaunchAgent installers 13d left out (macp_monitor,
claude_model_autoswitch) can render an installed-mode plist that runs
their engine script from the wheel.

Notes/macp-public-split-inventory.md, leaf 13e (13d's pattern applied to the
remaining Mac installers).

The default mode is pinned byte for byte: with MACP_ENGINE_PYTHON unset or empty
each installer writes exactly the plist text it wrote before 13e. The expected
texts below were captured from the pre-13e installers (tmp paths tokenised as
@HOME@ / @ENGINE@ / @VAULT@), not typed by hand.

Same harness as 13d (kept self-contained so the leaf stands alone): each case
runs a *copy* of the installer under tmp_path with HOME pointed at tmp and a
fake launchctl first on PATH. The monitor and autoswitch installers also call
`plutil -lint`, which only exists on macOS, so a fake plutil that parses the
file with plistlib stands in for it. They also check that their script exists
under the vault's scripts/, so the harness drops empty stand-ins there. The real
launchctl is never called.
"""
from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REAL_ENGINE = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(shutil.which("zsh") is None,
                                reason="the LaunchAgent installers are zsh scripts")

# installer -> (label, engine script name, extra argv after the script)
_CASES = {
    "macp_monitor": ("install_macp_monitor_launchagent.sh",
                     "com.macp.secondbrain.macp-monitor", "macp_monitor.py",
                     ("scan", "--daemon")),
    "claude_model_autoswitch": ("install_claude_model_autoswitch_launchagent.sh",
                                "com.macp.secondbrain.claude-model-autoswitch",
                                "claude_model_autoswitch.py", ()),
}
# The installers that lint the plist and check the vault's scripts/ for their target.
_VAULT_SCRIPT_CASES = ("macp_monitor", "claude_model_autoswitch")

# `print` fails so the bootout branch is skipped (nothing loaded yet); every
# other call is recorded and succeeds.
_FAKE_LAUNCHCTL = f'''#!{sys.executable}
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["LAUNCHCTL_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")
sys.exit(1 if argv and argv[0] == "print" else 0)
'''
# Records its argv and really lints: a malformed plist raises and exits non-zero.
_FAKE_PLUTIL = f'''#!{sys.executable}
import json, os, plistlib, sys
argv = sys.argv[1:]
with open(os.environ["PLUTIL_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")
with open(argv[-1], "rb") as fh:
    plistlib.loads(fh.read())
print(argv[-1] + ": OK")
'''

_MACP_MONITOR = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.macp.secondbrain.macp-monitor</string>

  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>@VAULT@/scripts/macp_monitor.py</string>
    <string>scan</string>
    <string>--daemon</string>
  </array>

  <key>WorkingDirectory</key>
  <string>@VAULT@</string>

  <key>StartInterval</key>
  <integer>1800</integer>

  <key>RunAtLoad</key>
  <false/>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:@HOME@/.local/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>

  <key>StandardOutPath</key>
  <string>@VAULT@/logs/macp-monitor.out.log</string>

  <key>StandardErrorPath</key>
  <string>@VAULT@/logs/macp-monitor.err.log</string>
</dict>
</plist>
"""

_CLAUDE_MODEL_AUTOSWITCH = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.macp.secondbrain.claude-model-autoswitch</string>

  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>@VAULT@/scripts/claude_model_autoswitch.py</string>
  </array>

  <key>WorkingDirectory</key>
  <string>@VAULT@</string>

  <key>StartInterval</key>
  <integer>1800</integer>

  <key>RunAtLoad</key>
  <false/>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:@HOME@/.local/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>

  <key>StandardOutPath</key>
  <string>@VAULT@/logs/claude-model-autoswitch.out.log</string>

  <key>StandardErrorPath</key>
  <string>@VAULT@/logs/claude-model-autoswitch.err.log</string>
</dict>
</plist>
"""

_EXPECTED = {
    "macp_monitor": _MACP_MONITOR,
    "claude_model_autoswitch": _CLAUDE_MODEL_AUTOSWITCH,
}


class _Harness:
    def __init__(self, tmp: Path, case: str):
        self.case = case
        self.installer, self.label, self.name, self.extra = _CASES[case]
        self.tmp = tmp.resolve()
        self.log = self.tmp / "launchctl.jsonl"
        self.plutil_log = self.tmp / "plutil.jsonl"
        self.home = self.tmp / "home"
        self.bin = self.tmp / "bin"
        self.engine = self.tmp / "engine"
        self.vault = self.tmp / "vault"
        for d in (self.home, self.bin, self.engine / "scripts", self.vault / "scripts"):
            d.mkdir(parents=True)
        for tool, body in (("launchctl", _FAKE_LAUNCHCTL), ("plutil", _FAKE_PLUTIL)):
            (self.bin / tool).write_text(body, encoding="utf-8")
            (self.bin / tool).chmod(0o755)
        for case_name in _VAULT_SCRIPT_CASES:
            (self.vault / "scripts" / _CASES[case_name][2]).write_text("", encoding="utf-8")
        self.script = self.engine / "scripts" / self.installer
        shutil.copy2(_REAL_ENGINE / "scripts" / self.installer, self.script)

    @property
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{self.label}.plist"

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        # From scratch: no inherited MACP_ENGINE_PYTHON / MACP_LABEL_PREFIX.
        full = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.home),
            "LAUNCHCTL_TEST_LOG": str(self.log),
            "PLUTIL_TEST_LOG": str(self.plutil_log),
            "MACP_VAULT_ROOT": str(self.vault),
            **env,
        }
        return subprocess.run(["zsh", str(self.script), *args], env=full,
                              capture_output=True, text=True, timeout=60)

    def expected_text(self) -> str:
        return (_EXPECTED[self.case].replace("@HOME@", str(self.home))
                .replace("@ENGINE@", str(self.engine)).replace("@VAULT@", str(self.vault)))

    def plist(self) -> dict:
        return plistlib.loads(self.plist_path.read_bytes())

    def calls(self) -> list[list[str]]:
        return _jsonl(self.log)

    def plutil_calls(self) -> list[list[str]]:
        return _jsonl(self.plutil_log)


def _jsonl(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _assert_installed(h: _Harness, r: subprocess.CompletedProcess) -> None:
    assert r.returncode == 0, (r.stdout, r.stderr)
    # An empty log means a real launchctl ran instead of the fake.
    uid = os.getuid()
    assert h.calls() == [
        ["print", f"gui/{uid}/{h.label}"],
        ["bootstrap", f"gui/{uid}", str(h.plist_path)],
        ["enable", f"gui/{uid}/{h.label}"],
    ], (h.calls(), r.stdout, r.stderr)
    expected_lint = [["-lint", str(h.plist_path)]] if h.case in _VAULT_SCRIPT_CASES else []
    assert h.plutil_calls() == expected_lint, h.plutil_calls()


@pytest.mark.parametrize("knob", ["unset", "empty"])
@pytest.mark.parametrize("case", sorted(_CASES))
def test_default_mode_plist_is_byte_identical(tmp_path, case, knob):
    h = _Harness(tmp_path, case)
    env = {"MACP_ENGINE_PYTHON": ""} if knob == "empty" else {}

    r = h.run(**env)

    _assert_installed(h, r)
    assert h.plist_path.read_bytes() == h.expected_text().encode("utf-8")
    h.plist()  # and it still parses


@pytest.mark.parametrize("case", sorted(_CASES))
def test_installed_mode_program_arguments(tmp_path, case):
    h = _Harness(tmp_path, case)
    _assert_installed(h, h.run())
    default = h.plist()
    h.log.unlink()
    h.plutil_log.unlink(missing_ok=True)
    py = str(h.tmp / "venv" / "bin" / "python")

    r = h.run(MACP_ENGINE_PYTHON=py)

    _assert_installed(h, r)
    plist = h.plist()
    # The script's own argv follows the name in the same order.
    assert plist["ProgramArguments"] == [py, "-P", "-m", "todo_system.engine_script", h.name,
                                         *h.extra]
    env = plist["EnvironmentVariables"]
    assert env["MACP_VAULT_ROOT"] == str(h.vault) == plist["WorkingDirectory"], plist
    assert "MACP_PYTHON" not in env, env  # engine_script sets it before the exec
    # Only ProgramArguments changes, plus MACP_VAULT_ROOT where it was missing.
    assert env == {**default["EnvironmentVariables"], "MACP_VAULT_ROOT": str(h.vault)}, env
    assert set(env) == {"PATH", "MACP_VAULT_ROOT"}, env
    assert {**plist, "ProgramArguments": None, "EnvironmentVariables": None} == \
        {**default, "ProgramArguments": None, "EnvironmentVariables": None}


@pytest.mark.parametrize("case", sorted(_CASES))
def test_relative_engine_python_is_refused(tmp_path, case):
    h = _Harness(tmp_path, case)

    r = h.run(MACP_ENGINE_PYTHON="venv/bin/python")

    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "absolute" in r.stderr, r.stderr
    assert not h.plist_path.exists()
    assert not (h.home / "Library").exists()
    assert h.calls() == [] and h.plutil_calls() == []


@pytest.mark.parametrize("case", _VAULT_SCRIPT_CASES)
def test_installed_mode_needs_no_vault_scripts_dir(tmp_path, case):
    h = _Harness(tmp_path, case)
    for stand_in in (h.vault / "scripts").iterdir():
        stand_in.unlink()
    (h.vault / "scripts").rmdir()

    # Control: the default mode still refuses a vault without the script.
    r = h.run()
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert "not found" in r.stderr, r.stderr
    assert not h.plist_path.exists()
    assert h.calls() == [] and h.plutil_calls() == []

    r = h.run(MACP_ENGINE_PYTHON=str(h.tmp / "venv" / "bin" / "python"))

    _assert_installed(h, r)
    assert h.plist()["ProgramArguments"][4] == h.name


def test_monitor_dry_run_installed_mode_skips_launchctl(tmp_path):
    h = _Harness(tmp_path, "macp_monitor")
    py = str(h.tmp / "venv" / "bin" / "python")

    r = h.run("--dry-run", MACP_ENGINE_PYTHON=py)

    assert r.returncode == 0, (r.stdout, r.stderr)
    line = next(ln for ln in r.stdout.splitlines() if ln.startswith("DRY_RUN: plist written to "))
    path = Path(line[len("DRY_RUN: plist written to "):].split(" (", 1)[0])
    try:
        plist = plistlib.loads(path.read_bytes())
        assert plist["ProgramArguments"] == [py, "-P", "-m", "todo_system.engine_script",
                                             "macp_monitor.py", "scan", "--daemon"]
        assert plist["EnvironmentVariables"]["MACP_VAULT_ROOT"] == str(h.vault)
        assert h.plutil_calls() == [["-lint", str(path)]]
    finally:
        shutil.rmtree(path.parent)
    assert h.calls() == []
    assert not h.plist_path.exists()
