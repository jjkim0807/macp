"""Installed-wheel smoke for 13a: engine_script, *.sh exec bits, install-safe seams.

Extends the leaf-12 smoke (``test_installed_wheel_smoke.py``) without touching
it. The wheel comes from the session-scoped ``installed_wheel`` in
``conftest.py`` (built once per pytest session, offline with ``UV_OFFLINE=1``),
everything under pytest's tmp dirs — the build goes to ``--out-dir``, the venv
and the vault are tmp, the real vault is never named.

What it pins, from site-packages, with ``python -P`` and ``PYTHONPATH``
scrubbed so nothing can quietly import this checkout:

* AC5 — every ``todo_system/_engine/scripts/*.sh`` in the wheel carries 0755
  (zip ``external_attr``), and the installed copies are executable.
* AC2 — ``python -m todo_system.engine_script --dry-run orchestrator_tick.sh
  --help`` resolves into site-packages. A dry run and not ``--help`` because the
  tick script ignores its arguments: ``--help`` would run a real tick (and its
  ``uv run --project`` against ``_engine``, which ships no pyproject, is leaf
  13b's problem).
* The install-time seams: ``paths.is_installed()`` / ``vault_search_start()``
  answer the wheel way, ``beta_publish``'s scratch sits under the vault's
  ``Worker/_state`` and never under the venv, a module imported
  with no env from inside the vault resolves it by cwd (RED on leaf 12: the
  site-packages anchor ignored the cwd and raised), and from outside any vault
  it still fails loudly.

Deliberately no skip and no marker, the leaf-12 stance: a host without ``uv``
or with a cold uv cache fails here and that is a host preparation problem. No
literal from the identifier gate's pattern may appear in this file: it sits
inside the scanned tree.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
# Env that would point uv or the children back at this checkout / the live vault.
_SCRUB = (
    "PYTHONPATH",
    "PYTHONSAFEPATH",
    "VIRTUAL_ENV",
    "UV_PROJECT_ENVIRONMENT",
    "MAESTRO_VAULT_DIR",
    "KAKAO_VAULT_DIR",
    "MACP_VAULT_ROOT",
)
_ENGINE_SCRIPTS = "todo_system/_engine/scripts/"
_SAMPLE_TASK = """---
tags:
  - todo
  - infra
status: open
due: "-"
updated: 2026-10-07
outcome: "installed-wheel smoke 13a — engine_script + install-safe seams from site-packages"
next_action: "none; fixture only"
if_then: "used only inside the throwaway vault"
estimate_p50: 5m
estimate_p90: 10m
review_cycle: "-"
---

# installed-wheel smoke 13a sample task

- [ ] exists so Todo/items is a real vault tree (5m)

## Progress Log

- 2026-10-07: created by test_installed_wheel_smoke_13a.py
"""


def _base_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _SCRUB}


def _child_env(vault: Path) -> dict[str, str]:
    env = _base_env()
    env["MACP_VAULT_ROOT"] = str(vault)
    env["SMOKE_REPO"] = str(REPO)
    return env


def _run(args, *, cwd: Path, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(a) for a in args], cwd=str(cwd), env=env,
        capture_output=True, text=True, timeout=timeout,
    )


def _ok(proc: subprocess.CompletedProcess, what: str) -> subprocess.CompletedProcess:
    assert proc.returncode == 0, (
        f"{what}: rc={proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
    return proc


def _py(installed, vault: Path, cwd: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return _run([installed.venv / "bin" / "python", "-P", *args],
                cwd=cwd, env=_child_env(vault), timeout=timeout)


def _py_no_env(installed, cwd: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    """Like _py but with NO MACP_VAULT_ROOT: only the cwd can locate a vault."""
    env = _base_env()
    env["SMOKE_REPO"] = str(REPO)
    return _run([installed.venv / "bin" / "python", "-P", *args], cwd=cwd, env=env, timeout=timeout)


@pytest.fixture(scope="module")
def installed(installed_wheel):
    """The session's single wheel install (conftest ``installed_wheel``)."""
    return installed_wheel


@pytest.fixture(scope="module")
def vault(tmp_path_factory) -> Path:
    """Throwaway vault: the isolation script's skeleton + the .macp marker."""
    root = tmp_path_factory.mktemp("smoke-vault-13a")
    for rel in ("Todo/items", "Memory/sessions", "Memory/episodes", "Notes",
                "Decisions", "Worker", ".macp"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    (root / ".macp" / "config.toml").write_text("", encoding="utf-8")
    (root / "Todo" / "items" / "sample-installed-smoke-13a.md").write_text(
        _SAMPLE_TASK, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def empty_cwd(tmp_path_factory) -> Path:
    """Legs that must not depend on the cwd run from here: nothing above it is a vault."""
    return tmp_path_factory.mktemp("smoke-cwd-13a")


@pytest.fixture(scope="module")
def site_engine(installed, vault, empty_cwd) -> Path:
    """site-packages/todo_system/_engine of the installed wheel, from the child."""
    proc = _ok(_py(installed, vault, empty_cwd, "-c",
                   "from todo_system import paths; print(paths.engine_root())"),
               "engine_root probe")
    root = Path(proc.stdout.strip().splitlines()[-1])
    assert root.parts[-3:] == ("site-packages", "todo_system", "_engine"), root
    assert root.is_relative_to(installed.venv.resolve()), (root, installed.venv)
    return root


# --- AC5: *.sh exec bits in the wheel and after install ------------------------


def test_wheel_shell_scripts_have_exec_bit(installed):
    scripts = [zi for zi in installed.infos
               if zi.filename.startswith(_ENGINE_SCRIPTS) and zi.filename.endswith(".sh")]
    names = [zi.filename for zi in scripts]
    assert f"{_ENGINE_SCRIPTS}orchestrator_tick.sh" in names, names
    assert f"{_ENGINE_SCRIPTS}kakao_send.sh" in names, names
    # The two gate scripts were 100644 in git until this leaf. The identifier gate
    # excludes itself from the public export, so an exported tree lacks it.
    assert f"{_ENGINE_SCRIPTS}check_engine_isolation.sh" in names, names
    if (REPO / "scripts" / "check_public_identifiers.sh").is_file():
        assert f"{_ENGINE_SCRIPTS}check_public_identifiers.sh" in names, names
    wrong = [(zi.filename, oct((zi.external_attr >> 16) & 0o777)) for zi in scripts
             if (zi.external_attr >> 16) & 0o777 != 0o755]
    assert not wrong, f"not 0755 in the wheel: {wrong}"


def test_wheel_bash_wrapper_is_executable_too(installed):
    wrapper = [zi for zi in installed.infos if zi.filename == f"{_ENGINE_SCRIPTS}macp"]
    assert len(wrapper) == 1, [zi.filename for zi in installed.infos if zi.filename.endswith("/macp")]
    assert (wrapper[0].external_attr >> 16) & 0o777 == 0o755


def test_installed_shell_scripts_are_executable(site_engine):
    scripts = sorted((site_engine / "scripts").glob("*.sh"))
    assert scripts, site_engine
    not_exec = [p.name for p in scripts if not os.access(p, os.X_OK)]
    assert not not_exec, f"installed without the exec bit: {not_exec}"


# --- AC2: engine_script resolves into site-packages ---------------------------


def _dry_run(proc: subprocess.CompletedProcess) -> dict[str, str]:
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == 3, proc.stdout
    return dict(ln.split("=", 1) for ln in lines)


def test_engine_script_dry_run_resolves_into_site_packages(installed, vault, empty_cwd):
    proc = _ok(_py(installed, vault, empty_cwd, "-m", "todo_system.engine_script",
                   "--dry-run", "orchestrator_tick.sh", "--help"),
               "engine_script --dry-run orchestrator_tick.sh --help")
    got = _dry_run(proc)
    path = Path(got["script"])
    assert path.parts[-4:] == ("todo_system", "_engine", "scripts", "orchestrator_tick.sh"), path
    assert path.is_relative_to(installed.venv.resolve()), (path, installed.venv)
    assert not path.is_relative_to(REPO), path
    assert path.is_file()
    assert json.loads(got["argv"]) == [str(path), "--help"]
    assert got["executable"] == "true", got
    assert os.access(path, os.X_OK)


def test_engine_script_dry_run_python_script_uses_the_venv_interpreter(installed, vault, empty_cwd):
    proc = _ok(_py(installed, vault, empty_cwd, "-m", "todo_system.engine_script",
                   "--dry-run", "health_report.py", "--help"),
               "engine_script --dry-run health_report.py")
    got = _dry_run(proc)
    argv = json.loads(got["argv"])
    assert Path(argv[0]).resolve() == (installed.venv / "bin" / "python").resolve(), argv
    assert Path(argv[1]).parts[-4:] == ("todo_system", "_engine", "scripts", "health_report.py")


def test_engine_script_list_and_unknown_name(installed, vault, empty_cwd):
    proc = _ok(_py(installed, vault, empty_cwd, "-m", "todo_system.engine_script", "--list"),
               "engine_script --list")
    assert "orchestrator_tick.sh" in proc.stdout and "kakao_send.sh" in proc.stdout, proc.stdout
    assert "site-packages" in proc.stdout, proc.stdout

    proc = _py(installed, vault, empty_cwd, "-m", "todo_system.engine_script", "no-such-script.sh")
    assert proc.returncode == 2, (proc.returncode, proc.stdout, proc.stderr)
    assert "no such engine script: no-such-script.sh" in proc.stderr, proc.stderr
    assert "orchestrator_tick.sh" in proc.stderr, proc.stderr


# --- install-time seams ----------------------------------------------------------

_LAYOUT_PROBE = """
import json, os, pathlib, sys
from todo_system import paths, worker
from todo_system.orchestrator import beta_publish, human, stages
venv = pathlib.Path(sys.prefix).resolve()
scratch = {
    "LOCK_DIR": beta_publish.LOCK_DIR,
    "SWEEP_STATE_PATH": beta_publish.SWEEP_STATE_PATH,
    "SWEEP_WORKTREE_DIR": beta_publish.SWEEP_WORKTREE_DIR,
    "SWEEP_LOG_PATH": beta_publish.SWEEP_LOG_PATH,
}
print(json.dumps({
    "installed": paths.is_installed(),
    "search_start": paths.vault_search_start(),
    "engine_root": str(paths.engine_root()),
    "engine_root_in_venv": paths.engine_root().is_relative_to(venv),
    "worker_root": str(worker.ROOT),
    "human_root": str(human._VAULT_DATA_ROOT),
    "stages_root": str(stages._VAULT_DATA_ROOT),
    "beta_root": str(beta_publish._VAULT_DATA_ROOT),
    "scratch_root": str(beta_publish._SCRATCH_ROOT),
    "scratch": {k: str(v) for k, v in scratch.items()},
    "scratch_in_venv": [k for k, v in scratch.items() if v.resolve().is_relative_to(venv)],
}, default=str))
"""


def test_installed_layout_probe(installed, vault, empty_cwd):
    proc = _ok(_py(installed, vault, empty_cwd, "-c", _LAYOUT_PROBE), "layout probe")
    got = json.loads(proc.stdout.strip().splitlines()[-1])
    assert got["installed"] is True
    assert got["search_start"] is None
    assert got["engine_root_in_venv"] is True
    assert Path(got["engine_root"]).parts[-3:] == ("site-packages", "todo_system", "_engine")
    for key in ("worker_root", "human_root", "stages_root", "beta_root", "scratch_root"):
        assert got[key] == str(vault), (key, got[key])
    state = vault / "Worker" / "_state"
    assert got["scratch"] == {
        "LOCK_DIR": str(state / "beta_locks"),
        "SWEEP_STATE_PATH": str(state / "beta_sweep.json"),
        "SWEEP_WORKTREE_DIR": str(state / "beta_worktrees"),
        "SWEEP_LOG_PATH": str(state / "beta_sweep.log"),
    }
    assert got["scratch_in_venv"] == []


def test_installed_module_resolves_vault_from_cwd_without_env(installed, vault):
    # RED on leaf 12: worker.ROOT was vault_root(start=site-packages), which
    # ignores the cwd and raises VaultRootError even from inside a vault.
    proc = _ok(_py_no_env(installed, vault, "-c",
                          "from todo_system import worker, usage_guard, session_end\n"
                          "print(worker.ROOT)\nprint(usage_guard.ROOT)\nprint(session_end.ROOT)"),
               "import with no env from inside the vault")
    lines = proc.stdout.strip().splitlines()[-3:]
    assert [Path(p) for p in lines] == [vault.resolve()] * 3, proc.stdout


def test_installed_module_resolves_vault_from_a_subdirectory_without_env(installed, vault):
    proc = _ok(_py_no_env(installed, vault / "Todo" / "items", "-c",
                          "from todo_system.orchestrator import beta_publish\n"
                          "print(beta_publish._VAULT_DATA_ROOT)\nprint(beta_publish.LOCK_DIR)"),
               "import with no env from a vault subdirectory")
    root, lock = proc.stdout.strip().splitlines()[-2:]
    assert Path(root) == vault.resolve()
    assert Path(lock) == vault.resolve() / "Worker" / "_state" / "beta_locks"


def test_installed_module_fails_loudly_outside_vault(installed, empty_cwd):
    proc = _py_no_env(installed, empty_cwd, "-c", "from todo_system import worker")
    assert proc.returncode != 0, proc.stdout
    assert ("VaultRootError" in proc.stderr
            or "cannot resolve the MaestroACP vault root" in proc.stderr), proc.stderr
    assert "site-packages" in proc.stderr, proc.stderr   # names the install location it refused


def test_installed_engine_script_works_without_env(installed, empty_cwd):
    # Engine files ship with the code: resolution needs no vault at all.
    proc = _ok(_py_no_env(installed, empty_cwd, "-m", "todo_system.engine_script",
                          "--dry-run", "kakao_send.sh"), "engine_script with no env")
    got = _dry_run(proc)
    assert Path(got["script"]).parts[-4:] == ("todo_system", "_engine", "scripts", "kakao_send.sh")
