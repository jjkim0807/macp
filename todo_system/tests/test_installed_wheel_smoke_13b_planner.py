"""Installed-wheel smoke for the 13b residue: planner_tick.sh from site-packages.

Extends the 13b smoke (``test_installed_wheel_smoke_13b.py``) without touching
it. The wheel comes from the session-scoped ``installed_wheel`` in
``conftest.py`` (built once per pytest session, offline with ``UV_OFFLINE=1``),
everything under pytest's tmp dirs — the real vault is never named.

``python -m todo_system.engine_script planner_tick.sh`` **really runs** from
site-packages, up to the last gate before ``claude``: every engine module the
tick imports before that point (hq_host, usage_guard, task_deps, planner_mode,
model_select, followups, discovery, lease) runs under the venv's interpreter
with ``-P``. The tick stops at the checkout lease, held by another host in a
pre-written ``Worker/checkout.lock``, and exits 0 ("skip this tick") without
ever calling ``claude``.

That rc 0 alone would prove nothing: the script also exits 0 straight away
when ``claude`` or ``sqlite3`` is missing from PATH. So fakes for both sit
ahead of PATH (``claude`` records and fails), and the test asserts the lines
only the real modules print — the hq_host fail-open notice, the mode verdict,
``model: <WORKER_MODEL>`` (model_select imported; a failure would print the
fallback id) and the "lease held" line (lease imported and answered "held";
a broken import fails open and calls claude). ``uv`` is a tripwire (rc 97) at
the front of PATH: before the residue the hard-coded ``uv run python`` lines
hit it, the lease call failed open, and the fake claude ran.

Hermetic: ``HOME`` is tmp (usage_guard finds no credentials → fail-open, no
network), ``WORKER_MODEL`` short-circuits model_select's probe. No skip: the
script is bash. No literal from the identifier gate's pattern may appear in
this file: it sits inside the scanned tree.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

# Env that would point uv or the children back at this checkout / the live
# vault, pre-decide the scripts' interpreter, or change the tick's gates.
_SCRUB = (
    "PYTHONPATH",
    "PYTHONSAFEPATH",
    "VIRTUAL_ENV",
    "UV_PROJECT_ENVIRONMENT",
    "MAESTRO_VAULT_DIR",
    "KAKAO_VAULT_DIR",
    "MACP_VAULT_ROOT",
    "MACP_PYTHON",
    "KAKAO_LEVEL",
    "KAKAO_EVENT_KEY",
    "KAKAO_PROFILE",
    "MACP_REPO_SLUG",
    "WORKER_MODEL",
    "MACP_HOST_ID",
    "MAESTRO_DAILY_RUN_BUDGET",
    "MAESTRO_PLANNER_AGGRESSIVE",
    "MAESTRO_DISCOVERY",
    "MAESTRO_DISCOVERY_INTERVAL_DAYS",
)
_REPO_SLUG = "acme/smoke-13b-planner"
_BRANCH = '[[ ! -f "$ENGINE_DIR/pyproject.toml" && -n "${MACP_PYTHON:-}" ]]'
_MODEL = "smoke-model-13b"
_OTHER_HOLDER = "elsewhere-host:1"
_SAMPLE_TASK = """---
tags:
  - todo
  - infra
status: open
due: "-"
updated: 2026-10-08
outcome: "installed-wheel smoke 13b planner — planner_tick.sh from site-packages"
next_action: "none; fixture only"
if_then: "used only inside the throwaway vault"
estimate_p50: 5m
estimate_p90: 10m
review_cycle: "-"
---

# installed-wheel smoke 13b planner sample task

- [ ] exists so Todo/items is a real vault tree (5m)

## Progress Log

- 2026-10-08: created by test_installed_wheel_smoke_13b_planner.py
"""
# Tripwire at the front of PATH: any uv line lands here.
_UV_TRIPWIRE = """#!/bin/sh
printf 'uv %s\\n' "$*" >> "$SMOKE_UV_LOG"
exit 97
"""
# claude must never be reached: record and fail.
_FAKE_CLAUDE = """#!/bin/sh
printf 'claude %s\\n' "$*" >> "$SMOKE_CLAUDE_LOG"
exit 1
"""
# sqlite3 only feeds the backoff hash; present so the PATH guard passes.
_FAKE_SQLITE = "#!/bin/sh\nexit 0\n"


def _base_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _SCRUB}


def _child_env(vault: Path) -> dict[str, str]:
    env = _base_env()
    env["MACP_VAULT_ROOT"] = str(vault)
    return env


def _run(args, *, cwd: Path, env: dict[str, str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(a) for a in args], cwd=str(cwd), env=env,
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )


def _ok(proc: subprocess.CompletedProcess, what: str) -> subprocess.CompletedProcess:
    assert proc.returncode == 0, (
        f"{what}: rc={proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")
    return proc


def _py(installed, vault: Path, cwd: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return _run([installed.venv / "bin" / "python", "-P", *args],
                cwd=cwd, env=_child_env(vault), timeout=timeout)


def _engine_script(installed, cwd: Path, env: dict[str, str], *args: str,
                   timeout: int = 180) -> subprocess.CompletedProcess:
    return _run([installed.venv / "bin" / "python", "-P", "-m", "todo_system.engine_script", *args],
                cwd=cwd, env=env, timeout=timeout)


def _write_exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture(scope="module")
def installed(installed_wheel):
    """The session's single wheel install (conftest ``installed_wheel``)."""
    return installed_wheel


@pytest.fixture(scope="module")
def vault(tmp_path_factory) -> Path:
    """Throwaway vault: the isolation script's skeleton and the .macp marker.
    The script creates Worker/_state itself."""
    root = tmp_path_factory.mktemp("smoke-vault-13b-planner")
    for rel in ("Todo/items", "Memory/sessions", "Memory/episodes", "Notes",
                "Decisions", "Worker", ".macp"):
        (root / rel).mkdir(parents=True, exist_ok=True)
    (root / ".macp" / "config.toml").write_text(f'repo_slug = "{_REPO_SLUG}"\n', encoding="utf-8")
    (root / "Todo" / "items" / "sample-installed-smoke-13b-planner.md").write_text(
        _SAMPLE_TASK, encoding="utf-8")
    return root


@pytest.fixture(scope="module")
def empty_cwd(tmp_path_factory) -> Path:
    """Legs that must not depend on the cwd run from here: nothing above it is a vault."""
    return tmp_path_factory.mktemp("smoke-cwd-13b-planner")


@pytest.fixture(scope="module")
def home(tmp_path_factory) -> Path:
    """Tmp HOME: no credentials for the usage guard, nothing of the host's."""
    return tmp_path_factory.mktemp("smoke-home-13b-planner")


@pytest.fixture(scope="module")
def fake_bin(tmp_path_factory) -> Path:
    """Put ahead of PATH: the uv tripwire, a failing claude, an empty sqlite3."""
    root = tmp_path_factory.mktemp("smoke-bin-13b-planner")
    _write_exec(root / "uv", _UV_TRIPWIRE)
    _write_exec(root / "claude", _FAKE_CLAUDE)
    _write_exec(root / "sqlite3", _FAKE_SQLITE)
    return root


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


@pytest.fixture(scope="module")
def primed_vault(installed, vault, empty_cwd) -> Path:
    # The tick's queue gate reads Todo/todo.db: import-md builds it first.
    _ok(_py(installed, vault, empty_cwd, "-m", "todo_system.cli", "import-md"), "cli import-md")
    assert (vault / "Todo" / "todo.db").is_file()
    return vault


# --- the wheel's script body -------------------------------------------------------


def test_wheel_planner_tick_carries_the_installed_branch(site_engine):
    # `uv build` reads the working tree, so this pins what shipped, not the checkout.
    assert not (site_engine / "pyproject.toml").exists(), site_engine
    text = (site_engine / "scripts" / "planner_tick.sh").read_text(encoding="utf-8")
    assert text.count(_BRANCH) == 1


# --- engine_script planner_tick.sh really runs, up to the claude call ----------------


def test_engine_script_runs_planner_tick_up_to_the_claude_call(installed, primed_vault, empty_cwd,
                                                               home, fake_bin, site_engine):
    lease = primed_vault / "Worker" / "checkout.lock"
    lease.write_text(json.dumps({"holder": _OTHER_HOLDER, "expires_at": time.time() + 3600}),
                     encoding="utf-8")
    env = _child_env(primed_vault)
    env["HOME"] = str(home)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["SMOKE_UV_LOG"] = str(home / "uv-tripwire.log")
    env["SMOKE_CLAUDE_LOG"] = str(home / "claude.log")
    env["WORKER_MODEL"] = _MODEL
    env["PYTHONUTF8"] = "1"

    proc = _engine_script(installed, empty_cwd, env, "planner_tick.sh")
    out = proc.stdout + proc.stderr
    print(f"--- engine_script planner_tick.sh rc={proc.returncode} ---\n{out}")  # -rA shows it
    assert proc.returncode == 0, f"rc={proc.returncode}\n{out}"

    # Each line below is printed only after the real module ran from site-packages.
    assert "no Worker/hq_host marker" in out, out                 # hq_host --check
    assert "[planner-tick] mode=" in out, out                     # past every gate
    assert f"[planner-tick] model: {_MODEL}" in out, out          # model_select imported
    assert "[planner-tick] checkout lease held" in out, out       # lease answered "held"
    # Stopped before claude, and the installed branch never named uv.
    assert not Path(env["SMOKE_CLAUDE_LOG"]).exists(), Path(env["SMOKE_CLAUDE_LOG"]).read_text()
    assert not Path(env["SMOKE_UV_LOG"]).exists(), Path(env["SMOKE_UV_LOG"]).read_text()
    assert "uv run" not in out, out
    # The other host's lease is untouched; state lives in the vault only.
    assert json.loads(lease.read_text(encoding="utf-8"))["holder"] == _OTHER_HOLDER
    assert (primed_vault / "Worker" / "_state").is_dir()
    assert not (site_engine / "Worker").exists()
    assert not (primed_vault / ".venv").exists()
