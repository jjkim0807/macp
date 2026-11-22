"""Installed-wheel smoke for 13e: the monitor and autoswitch LaunchAgent
installers' installed-mode ProgramArguments resolve their scripts from
site-packages and keep the script's own argv.

Extends the 13a-13d smokes without touching them. The wheel comes from the
session-scoped ``installed_wheel`` in ``conftest.py`` (built once per pytest
session, offline with ``UV_OFFLINE=1``), everything under pytest's tmp dirs --
the venv, the vault and ``HOME`` handed to the installers are tmp, the real vault
is never named.

What it pins:

* Each converted installer (a copy, run with ``zsh`` against a fake
  ``launchctl`` and a fake ``plutil``) with ``MACP_ENGINE_PYTHON=<venv python>``
  writes a plist whose ProgramArguments are
  ``<venv python> -P -m todo_system.engine_script <name> [extra argv]``. That
  exact argv, with ``--dry-run`` inserted before the name and run from the
  plist's own WorkingDirectory with its EnvironmentVariables, exits 0, resolves
  the script under ``site-packages/todo_system/_engine/scripts`` and reports the
  extra argv (the monitor's ``scan --daemon``) unchanged. The dry run executes
  nothing, so every case here runs unconditionally; zsh is asserted, not
  skipped on.
* The WorkingDirectory (the tmp vault) holds a decoy ``todo_system`` package and
  no ``scripts/``: the installed mode must neither import the decoy nor need the
  vault's copy of the script.

No literal from the identifier gate's pattern may appear in this file: it sits
inside the scanned tree.
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

REPO = Path(__file__).resolve().parents[2]
# Env that would point uv or the children back at this checkout / the live vault.
_SCRUB = (
    "PYTHONPATH",
    "PYTHONSAFEPATH",
    "VIRTUAL_ENV",
    "UV_PROJECT_ENVIRONMENT",
    "WORKER_HOST",
    "MAESTRO_VAULT_DIR",
    "KAKAO_VAULT_DIR",
    "MACP_VAULT_ROOT",
    "MACP_PYTHON",
    "MACP_REPO_SLUG",
)
# installer -> (plist label, engine script name, extra argv after the name)
CONVERTED = {
    "install_macp_monitor_launchagent.sh": ("com.macp.secondbrain.macp-monitor",
                                            "macp_monitor.py", ("scan", "--daemon")),
    "install_claude_model_autoswitch_launchagent.sh": (
        "com.macp.secondbrain.claude-model-autoswitch", "claude_model_autoswitch.py", ()),
}
_DECOY_EXIT = 3
_DECOY = f"""print("DECOY engine_script from the cwd checkout")
raise SystemExit({_DECOY_EXIT})
"""
_FAKE_LAUNCHCTL = f'''#!{sys.executable}
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["LAUNCHCTL_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")
sys.exit(1 if argv and argv[0] == "print" else 0)
'''
_FAKE_PLUTIL = f'''#!{sys.executable}
import plistlib, sys
with open(sys.argv[-1], "rb") as fh:
    plistlib.loads(fh.read())
print(sys.argv[-1] + ": OK")
'''


def _base_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _SCRUB}


def _run(args, *, cwd: Path, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(a) for a in args], cwd=str(cwd), env=env,
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )


def _ok(proc: subprocess.CompletedProcess, what: str) -> subprocess.CompletedProcess:
    assert proc.returncode == 0, (
        f"{what}: rc={proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
    return proc


def _dry_run(proc: subprocess.CompletedProcess) -> dict[str, str]:
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == 3, proc.stdout
    return dict(ln.split("=", 1) for ln in lines)


def _write_decoy(root: Path) -> None:
    pkg = root / "todo_system"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "engine_script.py").write_text(_DECOY, encoding="utf-8")


@pytest.fixture(scope="module")
def installed(installed_wheel):
    """The session's single wheel install (conftest ``installed_wheel``)."""
    return installed_wheel


@pytest.fixture(scope="module")
def decoy_vault(tmp_path_factory) -> Path:
    """The vault the plists are rendered for, still carrying a checkout's
    todo_system/ but no scripts/."""
    root = tmp_path_factory.mktemp("smoke-vault-13e").resolve()
    _write_decoy(root)
    return root


@pytest.fixture(scope="module")
def plists(tmp_path_factory, installed, decoy_vault) -> dict[str, dict]:
    """Run each converted installer copy in installed mode; return the parsed plists."""
    zsh = shutil.which("zsh")
    assert zsh, "zsh is required to run the LaunchAgent installers (unconditional by design)"
    base = tmp_path_factory.mktemp("installers-13e").resolve()
    home, fake_bin, engine = base / "home", base / "bin", base / "engine"
    for d in (home, fake_bin, engine / "scripts"):
        d.mkdir(parents=True)
    for tool, body in (("launchctl", _FAKE_LAUNCHCTL), ("plutil", _FAKE_PLUTIL)):
        (fake_bin / tool).write_text(body, encoding="utf-8")
        (fake_bin / tool).chmod(0o755)
    log = base / "launchctl.jsonl"
    out: dict[str, dict] = {}
    for installer, (label, _name, _extra) in CONVERTED.items():
        shutil.copy2(REPO / "scripts" / installer, engine / "scripts" / installer)
        env = {
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(home),
            "LAUNCHCTL_TEST_LOG": str(log),
            "MACP_ENGINE_PYTHON": str(installed.python),
            "MACP_VAULT_ROOT": str(decoy_vault),
        }
        _ok(_run([zsh, engine / "scripts" / installer], cwd=base, env=env, timeout=60),
            installer)
        plist_path = home / "Library" / "LaunchAgents" / f"{label}.plist"
        out[installer] = plistlib.loads(plist_path.read_bytes())
    # Every launchctl call went to the fake: print / bootstrap / enable per installer.
    calls = [json.loads(ln) for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert [c[0] for c in calls] == ["print", "bootstrap", "enable"] * len(CONVERTED), calls
    return out


@pytest.mark.parametrize("installer", sorted(CONVERTED))
def test_installed_program_arguments_dry_run_resolves_into_site_packages(
        installer, plists, installed, decoy_vault):
    _label, name, extra = CONVERTED[installer]
    plist = plists[installer]
    argv = list(plist["ProgramArguments"])
    assert argv[:4] == [str(installed.python), "-P", "-m", "todo_system.engine_script"], argv
    assert argv[4:] == [name, *extra], argv
    plist_env = plist["EnvironmentVariables"]
    assert plist_env["MACP_VAULT_ROOT"] == str(decoy_vault), plist_env
    cwd = Path(plist["WorkingDirectory"])
    assert (cwd / "todo_system" / "engine_script.py").is_file(), cwd  # the decoy is in the way
    assert not (cwd / "scripts").exists(), cwd

    argv.insert(4, "--dry-run")
    proc = _run(argv, cwd=cwd, env=_base_env() | plist_env, timeout=60)
    print(f"--- {installer}: {argv[4:]} cwd={cwd.name} ---\n{proc.stdout}")  # -rA shows it
    _ok(proc, f"{installer} ProgramArguments --dry-run")
    assert "DECOY" not in proc.stdout, proc.stdout

    got = _dry_run(proc)
    script = Path(got["script"])
    assert script.parts[-4:] == ("todo_system", "_engine", "scripts", name), script
    assert "site-packages" in script.parts, script
    assert script.is_relative_to(installed.venv.resolve()), (script, installed.venv)
    exec_argv = json.loads(got["argv"])
    if name.endswith(".py"):
        # The venv's own interpreter runs it, so the script sees the wheel's site-packages.
        assert exec_argv[1:] == [str(script), *extra], exec_argv
        assert Path(exec_argv[0]).is_relative_to(installed.venv), exec_argv
    else:
        assert exec_argv == [str(script), *extra], exec_argv
