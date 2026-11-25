"""orchestrator_tick.sh exit status: a failed auto leg must reach the exit code.

The tick runs two legs (auto, then dashboard) under `set -uo pipefail`
without `set -e`. It used to end on the dashboard leg, so the script's
exit code was the dashboard's and a failed auto dispatch still exited 0 — the
oneshot unit recorded success. Both legs must always run (the dashboard is how
a failed tick gets seen) and the auto failure outranks the dashboard's.

Each case runs a *copy* of the script under tmp_path with a fake uv and a stub
orchestrator that exits 3 on `auto` / 4 on `dashboard` when named in
TICK_FAIL_ON, so the tests can tell precedence from mere non-zero.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REAL_ENGINE = Path(__file__).resolve().parents[2]
_SCRIPT_REL = Path("scripts") / "orchestrator_tick.sh"

pytestmark = pytest.mark.skipif(shutil.which("zsh") is None,
                                reason="orchestrator_tick.sh is a zsh script")

# Records the call, then runs the real interpreter; its exit code is the stub's.
_FAKE_UV = f'''#!{sys.executable}
import json, os, subprocess, sys
argv = sys.argv[1:]
with open(os.environ["TICK_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"tool": "uv", "argv": argv}}) + "\\n")
assert argv and argv[0] == "run", argv
rest = argv[1:]
while rest and rest[0].startswith("--"):
    rest = rest[2:]
assert rest and rest[0] == "python", rest
sys.exit(subprocess.run([sys.executable, *rest[1:]], env=os.environ).returncode)
'''

_STUB_ORCH = '''import json, os, sys
argv = sys.argv[1:]
with open(os.environ["TICK_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tool": "orch", "argv": argv}) + "\\n")
print("stub-orch", argv)
fail = set(filter(None, os.environ.get("TICK_FAIL_ON", "").split(",")))
if "auto" in argv and "auto" in fail:
    sys.exit(3)
if "dashboard" in argv and "dashboard" in fail:
    sys.exit(4)
'''


def _combined(root: Path) -> Path:
    """Engine == vault: a script copy plus a stub orchestrator package."""
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(_REAL_ENGINE / _SCRIPT_REL, root / _SCRIPT_REL)
    pkg = root / "todo_system" / "orchestrator"
    pkg.mkdir(parents=True)
    (root / "todo_system" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "orchestrator.py").write_text(_STUB_ORCH, encoding="utf-8")
    return root


def _run(tmp: Path, **env: str) -> tuple[subprocess.CompletedProcess, Path, Path]:
    root = _combined(tmp / "combined")
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(_FAKE_UV, encoding="utf-8")
    (bin_dir / "uv").chmod(0o755)
    (tmp / "home").mkdir()
    log = tmp / "calls.jsonl"
    full = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(tmp / "home"),
        "TICK_TEST_LOG": str(log),
        "PYTHONUTF8": "1",
        **env,
    }
    r = subprocess.run(["zsh", str(root / _SCRIPT_REL)], env=full, capture_output=True,
                       text=True, timeout=60)
    return r, root, log


def _orch_calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    return [r["argv"] for r in rows if r["tool"] == "orch"]


def _assert_both_legs_ran(log: Path, r: subprocess.CompletedProcess) -> None:
    calls = _orch_calls(log)
    assert len(calls) == 2, (calls, r.stdout, r.stderr)
    assert "auto" in calls[0] and "dashboard" not in calls[0], calls
    assert "dashboard" in calls[1] and "auto" not in calls[1], calls


def test_auto_failure_reaches_exit_code(tmp_path):
    r, _, log = _run(tmp_path, TICK_FAIL_ON="auto")
    _assert_both_legs_ran(log, r)
    assert r.returncode == 3, (r.stdout, r.stderr)


def test_dashboard_failure_reaches_exit_code(tmp_path):
    r, _, log = _run(tmp_path, TICK_FAIL_ON="dashboard")
    _assert_both_legs_ran(log, r)
    assert r.returncode == 4, (r.stdout, r.stderr)


def test_both_fail_auto_code_wins(tmp_path):
    r, _, log = _run(tmp_path, TICK_FAIL_ON="auto,dashboard")
    _assert_both_legs_ran(log, r)
    assert r.returncode == 3, (r.stdout, r.stderr)


def test_success_path_exit_zero_and_output_on_stdout(tmp_path):
    # The service manager writes the tick's stdout to the log; the script no
    # longer tees a second copy into logs/orchestrator.out.log.
    r, root, log = _run(tmp_path)
    _assert_both_legs_ran(log, r)
    assert r.returncode == 0, (r.stdout, r.stderr)
    lines = r.stdout.splitlines()
    assert len(lines) == 2, lines
    assert lines[0].startswith("stub-orch") and "'auto'" in lines[0], lines
    assert lines[1].startswith("stub-orch") and "'dashboard'" in lines[1], lines
    assert not (root / "logs" / "orchestrator.out.log").exists()


def test_tick_script_has_no_set_e_and_dashboard_unconditional():
    text = (_REAL_ENGINE / _SCRIPT_REL).read_text(encoding="utf-8")
    assert re.search(r"^\s*set\s+-[a-z]*e", text, re.M) is None
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    dash = [ln for ln in code if "dashboard" in ln]
    assert len(dash) == 1, dash
    assert not any(tok in dash[0] for tok in ("&&", "||", "if ")), dash
