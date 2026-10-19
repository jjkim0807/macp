"""Installed-wheel smoke for 13c: gen_units' installed-mode ExecStart lines resolve
their scripts from site-packages.

Extends the 13a/13b smokes without touching them. The wheel comes from the
session-scoped ``installed_wheel`` in ``conftest.py`` (built once per pytest
session, offline with ``UV_OFFLINE=1``), everything under pytest's tmp dirs — the
venv, the vault and ``HOME`` handed to gen_units are tmp, the real vault is never
named.

What it pins:

* ``scripts/systemd/gen_units.py`` rendered with ``build(<venv python>)`` gives
  every engine-script unit an ExecStart of
  ``<venv python> -P -m todo_system.engine_script <name>``. That exact command
  line, with ``--dry-run`` inserted before the name and run from the unit's own
  ``WorkingDirectory`` with the unit's ``Environment=`` lines, exits 0 and
  resolves the script under ``site-packages/todo_system/_engine/scripts``. The
  dry run executes nothing, so no shell or system interpreter is needed and
  every case here runs unconditionally.
* The unit cwd (the tmp vault, or ``HOME`` for claude-auth-watch) holds a decoy
  ``todo_system`` package, standing in for the vault checkout that still carries
  the engine during the parallel-validation step. The negative control drops
  ``-P`` from the same command line and lands in the decoy, which is why the
  generated ExecStart carries ``-P``.
* Vault-only units are not routed through the wheel.

No literal from the identifier gate's pattern may appear in this file: it sits
inside the scanned tree.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
from pathlib import Path
from unittest import mock

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
# gen_units knobs that would make the rendered units depend on the host. Includes
# the vault-root vars: the fixture pins the vault explicitly below, and the stage
# runs with an ambient MACP_VAULT_ROOT (now gen_units' primary) pointing at the real
# vault, which would otherwise leak into the rendered units.
_GEN_UNITS_SCRUB = ("MACP_ENGINE_PYTHON", "MACP_CALENDAR_ID", "MACP_LABEL_PREFIX",
                    "MACP_HOST_ID", "MACP_CLAUDE_RC_NAME", "MACP_VAULT_ROOT",
                    "MAESTRO_VAULT_DIR", "MACP_ENGINE_ROOT")
# Same classification as scripts/systemd/test_gen_units.py (not importable from here).
# Engine-core only: the author-only engine-script units (kakao-refresh/digest,
# claude-auth-watch) and the vault-only units (Remote Control stack, usage monitor)
# were dropped from gen_units, so only these four engine-script units remain.
CONVERTED = {
    "macp-orchestrator": "orchestrator_tick.sh",
    "macp-planner": "planner_tick.sh",
    "macp-brief": "brief.sh",
    "macp-health": "health_report.py",
}
_DECOY_EXIT = 3
_DECOY = f"""print("DECOY engine_script from the cwd checkout")
raise SystemExit({_DECOY_EXIT})
"""


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


def _unit_fields(text: str) -> tuple[str, Path, dict[str, str]]:
    """ExecStart value, WorkingDirectory and the [Service] ``Environment=`` pairs."""
    service = text.split("[Service]", 1)[1].split("[Install]", 1)[0]
    exec_start = cwd = None
    env: dict[str, str] = {}
    for line in service.splitlines():
        if line.startswith("ExecStart="):
            exec_start = line[len("ExecStart="):]
        elif line.startswith("WorkingDirectory="):
            cwd = Path(line[len("WorkingDirectory="):])
        elif line.startswith("Environment="):
            key, _, value = line[len("Environment="):].partition("=")
            env[key] = value
    assert exec_start and cwd, text
    return exec_start, cwd, env


@pytest.fixture(scope="module")
def installed(installed_wheel):
    """The session's single wheel install (conftest ``installed_wheel``)."""
    return installed_wheel


@pytest.fixture(scope="module")
def decoy_vault(tmp_path_factory) -> Path:
    """The vault the units are rendered for, still carrying a checkout's todo_system/."""
    root = tmp_path_factory.mktemp("smoke-vault-13c")
    _write_decoy(root)
    return root


@pytest.fixture(scope="module")
def decoy_home(tmp_path_factory) -> Path:
    """HOME for gen_units (so VAULT/ENGINE_ROOT defaults resolve under tmp, not the
    real home); carries the decoy todo_system as a belt-and-suspenders guard."""
    root = tmp_path_factory.mktemp("smoke-home-13c")
    _write_decoy(root)
    return root


@pytest.fixture(scope="module")
def gen_units(decoy_vault, decoy_home):
    with mock.patch.dict(os.environ, {}, clear=False):
        for key in _GEN_UNITS_SCRUB:
            os.environ.pop(key, None)
        os.environ["HOME"] = str(decoy_home)
        os.environ["MACP_VAULT_ROOT"] = str(decoy_vault)
        spec = importlib.util.spec_from_file_location(
            "gen_units_13c", REPO / "scripts" / "systemd" / "gen_units.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    assert mod.VAULT == decoy_vault and mod.HOME == decoy_home
    return mod


@pytest.fixture(scope="module")
def units(gen_units, installed) -> dict[str, str]:
    return gen_units.build(str(installed.python))


@pytest.mark.parametrize("stem", sorted(CONVERTED))
def test_installed_exec_start_dry_run_resolves_into_site_packages(stem, units, installed,
                                                                 decoy_vault):
    name = CONVERTED[stem]
    exec_start, cwd, unit_env = _unit_fields(units[f"{stem}.service"])
    argv = shlex.split(exec_start)
    assert argv[:4] == [str(installed.python), "-P", "-m", "todo_system.engine_script"], argv
    assert argv[4:] == [name], argv
    assert unit_env == {"MACP_VAULT_ROOT": str(decoy_vault)}, unit_env
    assert (cwd / "todo_system" / "engine_script.py").is_file(), cwd  # the decoy is in the way

    argv.insert(4, "--dry-run")
    proc = _run(argv, cwd=cwd, env=_base_env() | unit_env, timeout=60)
    print(f"--- {stem}: {argv[4:]} cwd={cwd.name} ---\n{proc.stdout}")  # -rA shows it
    _ok(proc, f"{stem} ExecStart --dry-run")
    assert "DECOY" not in proc.stdout, proc.stdout

    got = _dry_run(proc)
    script = Path(got["script"])
    assert script.parts[-4:] == ("todo_system", "_engine", "scripts", name), script
    assert "site-packages" in script.parts, script
    assert script.is_relative_to(installed.venv.resolve()), (script, installed.venv)
    exec_argv = json.loads(got["argv"])
    if name.endswith(".py"):
        # The venv's own interpreter runs it, so the script sees the wheel's site-packages.
        assert len(exec_argv) == 2 and exec_argv[1] == str(script), exec_argv
        assert Path(exec_argv[0]).is_relative_to(installed.venv), exec_argv
    else:
        assert exec_argv == [str(script)], exec_argv


def test_without_dash_P_the_vault_checkout_would_shadow_the_wheel(units, installed, decoy_vault):
    exec_start, cwd, unit_env = _unit_fields(units["macp-orchestrator.service"])
    argv = shlex.split(exec_start)
    assert argv[1] == "-P", argv
    del argv[1]
    argv.insert(3, "--dry-run")
    assert cwd == decoy_vault
    proc = _run(argv, cwd=cwd, env=_base_env() | unit_env, timeout=60)
    assert proc.returncode == _DECOY_EXIT, (proc.returncode, proc.stdout, proc.stderr)
    assert "DECOY" in proc.stdout, proc.stdout

# NB: the vault-only-units smoke was removed — the engine-core set has no vault-only
# unit left (the Remote Control stack and usage monitor were author-only, now dropped).
