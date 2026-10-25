"""The space-free PATH wrappers must run the ENGINE checkout, not the vault venv.

Since the public split the vault holds no todo_system/; its .venv carries a pinned,
older engine. 2026-10-25: `macp-emulator` from a vault cwd failed with "No module
named todo_system.orchestrator.emulator_cli" because the wrapper still used
PYTHONPATH=<vault> + <vault>/.venv/bin/python.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WRAPPERS = ["macp", "macp-sms", "macp-remote-gate", "macp-emulator", "macp-anthropic-usage"]


def _fake_python(path: Path, tag: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/usr/bin/env bash\necho "{tag} PYTHONPATH=$PYTHONPATH VAULT=$MACP_VAULT_ROOT ARGS=$*"\n')
    path.chmod(0o755)


@pytest.mark.parametrize("name", WRAPPERS)
def test_engine_checkout_is_preferred(tmp_path, name):
    engine = tmp_path / "engine"
    (engine / "todo_system").mkdir(parents=True)
    _fake_python(engine / ".venv" / "bin" / "python", "ENGINE")
    vault = tmp_path / "vault"
    _fake_python(vault / ".venv" / "bin" / "python", "VAULT")
    env = {k: v for k, v in os.environ.items() if k not in ("MACP_VAULT_ROOT", "MACP_REPO")}
    env.update(MACP_ENGINE_ROOT=str(engine), MACP_REPO=str(vault))
    out = subprocess.run(["bash", str(ROOT / "scripts" / name), "x"], env=env,
                         capture_output=True, text=True, cwd=tmp_path).stdout
    assert out.startswith("ENGINE "), out
    assert f"PYTHONPATH={engine}" in out
    assert f"VAULT={vault}" in out          # vault stays resolvable for the module


@pytest.mark.parametrize("name", WRAPPERS)
def test_falls_back_to_vault_venv_without_engine_checkout(tmp_path, name):
    vault = tmp_path / "vault"
    _fake_python(vault / ".venv" / "bin" / "python", "VAULT")
    env = {k: v for k, v in os.environ.items() if k not in ("MACP_VAULT_ROOT", "MACP_REPO")}
    env.update(MACP_ENGINE_ROOT=str(tmp_path / "no-engine"), MACP_REPO=str(vault))
    out = subprocess.run(["bash", str(ROOT / "scripts" / name), "x"], env=env,
                         capture_output=True, text=True, cwd=tmp_path).stdout
    assert out.startswith("VAULT "), out
    assert f"PYTHONPATH={vault}" in out
