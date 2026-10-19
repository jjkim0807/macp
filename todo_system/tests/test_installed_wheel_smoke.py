"""Installed-wheel smoke: the engine runs from site-packages (public split 12, P4).

The wheel comes from the session-scoped ``installed_wheel`` in ``conftest.py``
(built once per pytest session, shared by every installed-wheel smoke module):
``uv build`` (sdist → wheel, the path a release takes), installed with
``--no-deps`` into an EMPTY venv whose dependencies come from ``uv.lock`` via
``uv sync --frozen --no-install-project`` (cache hits on the locked URLs, so no
index access — ``UV_OFFLINE=1`` throughout, the same offline path
``scripts/check_engine_isolation.sh`` proves on every pytest run). This module
runs that script's read-only CLI legs against a throwaway vault named by
``MACP_VAULT_ROOT``, from a cwd that holds nothing.

The point is the *installed* layout, so every child runs ``python -P`` (no cwd
or script dir on ``sys.path``) with ``PYTHONPATH`` scrubbed, and a positive
control asserts that the imported package lives under the temp venv, that
``paths.engine_root()`` resolved to ``site-packages/todo_system/_engine`` (the
wheel branch of the resolver), and that this checkout is nowhere on
``sys.path``. Without that control a run that quietly imported the checkout
would pass every leg — shape-blind.

Deliberately no skip and no marker: a host without ``uv`` or with a cold uv
cache fails here. The standing gates already classify a cold cache as a host
preparation problem, not a code defect; warm it with one online ``uv run``.
Nothing is written outside pytest's tmp dirs: the build goes to ``--out-dir``,
the venv and the vault are tmp, and the real vault is never named.

No literal from the identifier gate's pattern may appear in this file: it sits
inside the scanned tree.
"""

from __future__ import annotations

import os
import subprocess
import zipfile
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
)
VAULT_PREFIXES = (".macp/", "Todo/", "Memory/", "Worker/", "Notes/", "Decisions/")
REQUIRED_IN_WHEEL = (
    "todo_system/_engine/scripts/orchestrator_tick.sh",
    "todo_system/_engine/.claude/commands/worker-implement.md",
    "todo_system/__init__.py",
    "todo_system/paths.py",
    "todo_system/cli.py",
)
# Same shape as the isolation script's fixture task (the documented work template).
_SAMPLE_TASK = """---
tags:
  - todo
  - infra
status: open
due: "-"
updated: 2026-10-07
outcome: "installed-wheel smoke — the engine reads MACP_VAULT_ROOT from site-packages"
next_action: "cli audit lists this task"
if_then: "used only inside the throwaway vault"
estimate_p50: 5m
estimate_p90: 10m
review_cycle: "-"
---

# installed-wheel smoke sample task

- [ ] appears in cli audit (5m)

## Progress Log

- 2026-10-07: created by test_installed_wheel_smoke.py
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


@pytest.fixture(scope="module")
def installed(installed_wheel):
    """The session's single wheel install (conftest ``installed_wheel``)."""
    return installed_wheel


@pytest.fixture(scope="module")
def vault(tmp_path_factory) -> Path:
    """Throwaway vault: the isolation script's phase-2 skeleton + the .macp marker."""
    root = tmp_path_factory.mktemp("smoke-vault")
    for rel in ("Todo/items", "Memory/sessions", "Memory/episodes", "Notes",
                "Decisions", "Worker", ".macp"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    (root / ".macp" / "config.toml").write_text("", encoding="utf-8")
    (root / "Todo" / "items" / "sample-installed-smoke.md").write_text(
        _SAMPLE_TASK, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def empty_cwd(tmp_path_factory) -> Path:
    """Every leg runs from here: no cwd-relative resolution may be involved."""
    return tmp_path_factory.mktemp("smoke-cwd")


@pytest.fixture(scope="module")
def primed_vault(installed, vault, empty_cwd) -> Path:
    # `cli audit` goes straight to the DB, so import-md builds Todo/todo.db first
    # (the isolation script's prime step).
    _ok(_py(installed, vault, empty_cwd, "-m", "todo_system.cli", "import-md"), "cli import-md")
    assert (vault / "Todo" / "todo.db").is_file()
    return vault


# --- AC#5: wheel contents -------------------------------------------------------


def test_wheel_ships_engine_files_and_no_vault_data(installed):
    names = installed.names
    missing = [p for p in REQUIRED_IN_WHEEL if p not in names]
    assert not missing, missing
    leaked = [n for n in names
              if n.startswith(VAULT_PREFIXES) or any(f"/_engine/{v}" in n for v in VAULT_PREFIXES)]
    assert not leaked, leaked


def test_wheel_declares_the_macp_console_script(installed):
    entry_points = [n for n in installed.names if n.endswith(".dist-info/entry_points.txt")]
    assert len(entry_points) == 1, installed.names
    text = zipfile.ZipFile(installed.wheel).read(entry_points[0]).decode("utf-8")
    assert "[console_scripts]" in text, text
    assert "macp = todo_system._entry:main" in text, text


# --- positive control: the children run the INSTALLED package ----------------

_PROBE = """
import os, pathlib, sys
import todo_system
from todo_system import paths, worker
from todo_system.orchestrator import beta_publish, human, stages
venv = pathlib.Path(sys.prefix).resolve()
f = pathlib.Path(todo_system.__file__).resolve()
assert f.is_relative_to(venv), (f, venv)
root = paths.engine_root()
assert root == f.parent / "_engine", (root, f)
assert root.parts[-3:] == ("site-packages", "todo_system", "_engine"), root
assert (root / "scripts" / "orchestrator_tick.sh").is_file(), root
assert (root / ".claude" / "commands" / "worker-implement.md").is_file(), root
assert worker._ENGINE_ROOT == root, worker._ENGINE_ROOT
assert human._ENGINE_ROOT == root, human._ENGINE_ROOT
assert stages._ENGINE_ROOT == root, stages._ENGINE_ROOT
assert beta_publish._ENGINE_ROOT == root, beta_publish._ENGINE_ROOT
assert worker.COMMANDS_DIR.is_dir(), worker.COMMANDS_DIR
assert worker.KAKAO_SEND_SCRIPT.is_file(), worker.KAKAO_SEND_SCRIPT
assert worker.HEALTH_REPORT_SCRIPT.is_file(), worker.HEALTH_REPORT_SCRIPT
assert stages.COMMANDS_DIR.is_dir(), stages.COMMANDS_DIR
assert human.KAKAO_SCRIPT.is_file(), human.KAKAO_SCRIPT
assert worker.ROOT == pathlib.Path(os.environ["MACP_VAULT_ROOT"]), worker.ROOT
repo = pathlib.Path(os.environ["SMOKE_REPO"]).resolve()
assert not any(pathlib.Path(p).resolve() == repo for p in sys.path if p), sys.path
print("OK", root)
"""


def test_child_imports_the_installed_wheel_not_the_checkout(installed, vault, empty_cwd):
    proc = _ok(_py(installed, vault, empty_cwd, "-c", _PROBE), "positive control probe")
    last = proc.stdout.strip().splitlines()[-1]
    assert last.startswith("OK "), proc.stdout
    root = Path(last[3:])
    assert root.is_relative_to(installed.venv.resolve()), (root, installed.venv)


# --- the isolation script's CLI legs, from site-packages -----------------------


def test_cli_audit(installed, primed_vault, empty_cwd):
    proc = _ok(_py(installed, primed_vault, empty_cwd, "-m", "todo_system.cli", "audit"), "cli audit")
    assert "[field audit]" in proc.stdout, proc.stdout
    assert "[wip audit]" in proc.stdout, proc.stdout


def test_session_log_list(installed, primed_vault, empty_cwd):
    _ok(_py(installed, primed_vault, empty_cwd, "-m", "todo_system.session_log", "list"),
        "session_log list")


def test_worker_run_next_dry_run(installed, primed_vault, empty_cwd):
    proc = _ok(_py(installed, primed_vault, empty_cwd,
                   "-m", "todo_system.worker", "run-next", "--dry-run"),
               "worker run-next --dry-run")
    out = proc.stdout + proc.stderr
    # The isolation script's leg C vocabulary: the fixture has no queue, so the
    # worker reports no pending runs and takes the (dry) planner path.
    assert any(s in out for s in ("no pending runs", "entering planner path", "[planner]")), out


def test_console_script_help(installed, vault, empty_cwd):
    proc = _ok(_run([installed.venv / "bin" / "macp", "--help"],
                    cwd=empty_cwd, env=_child_env(vault), timeout=60), "macp --help")
    assert "usage:" in proc.stdout, proc.stdout
