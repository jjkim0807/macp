"""Installed-wheel seams in beta_publish (13a, P3).

``beta_publish`` kept its per-install scratch (lock dir, sweep state/log/
worktrees) under the engine root, which an installed wheel cannot write. The
scratch root is layout-conditional: engine root in a checkout (the existing
pins hold — immune to a redirected vault), the vault's ``Worker/_state`` in a
wheel. ``_spawn_detached`` starts the sweep in the vault-data root, so a child
with no env resolves the vault by the cwd-upward search; in the combined
layout that is the same directory as before.

No real child process is started (``subprocess.Popen`` is swapped for a
recorder). No literal from the identifier gate's pattern may appear
in this file: it sits inside the scanned tree.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from todo_system import paths
from todo_system.orchestrator import beta_publish

ENGINE = Path(__file__).resolve().parents[2]


class _FakeProc:
    pid = 4242

    def wait(self):
        return 0


# --- beta_publish: scratch root + detached sweep cwd ----------------------------


def test_scratch_root_is_engine_root_in_checkout():
    root = paths.engine_root()
    assert not paths.is_installed()
    assert beta_publish._SCRATCH_ROOT == root
    assert beta_publish.LOCK_DIR == root / "Worker" / "_state" / "beta_locks"
    assert beta_publish.SWEEP_STATE_PATH == root / "Worker" / "_state" / "beta_sweep.json"
    assert beta_publish.SWEEP_WORKTREE_DIR == root / "Worker" / "_state" / "beta_worktrees"
    assert beta_publish.SWEEP_LOG_PATH == root / "Worker" / "_state" / "beta_sweep.log"


def test_scratch_root_source_is_layout_conditional():
    src = (ENGINE / "todo_system" / "orchestrator" / "beta_publish.py").read_text(encoding="utf-8")
    assert "_SCRATCH_ROOT = _VAULT_DATA_ROOT if paths.is_installed() else _ENGINE_ROOT" in src
    for name in ("LOCK_DIR", "SWEEP_STATE_PATH", "SWEEP_WORKTREE_DIR", "SWEEP_LOG_PATH"):
        line = re.search(rf"^{name}\s*=\s*(.*)$", src, re.M)
        assert line, name
        assert line.group(1).startswith("_SCRATCH_ROOT / "), (name, line.group(0))
    # The sweep is detached in the vault-data root, never the engine root.
    body = src[src.index("def _spawn_detached("):]
    body = body[:body.index("\ndef ", 1)]
    assert "cwd=str(_VAULT_DATA_ROOT)" in body
    assert "cwd=str(_ENGINE_ROOT)" not in body


def test_spawn_detached_cwd_is_vault_data_root(tmp_path, monkeypatch):
    calls: list[tuple[list[str], dict]] = []

    def fake_popen(args, **kwargs):
        calls.append((list(args), kwargs))
        return _FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.delenv("INVOCATION_ID", raising=False)   # plain spawn, no systemd scope
    log = tmp_path / "state" / "sweep.log"
    pid = beta_publish._spawn_detached(log_path=log)
    assert pid == 4242
    assert log.parent.is_dir()
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == [sys.executable, "-u", "-m", "todo_system.orchestrator.beta_publish", "sweep"]
    assert kwargs["cwd"] == str(beta_publish._VAULT_DATA_ROOT)
    assert kwargs["start_new_session"] is True
    assert kwargs["stderr"] is subprocess.STDOUT
    # Combined layout: the vault-data root IS the engine root, so the cwd is
    # byte-identical to the former cwd=_ENGINE_ROOT. A standalone engine tree
    # (MACP_VAULT_ROOT elsewhere) only has to honour the vault root.
    if beta_publish._VAULT_DATA_ROOT == beta_publish._ENGINE_ROOT:
        assert Path(kwargs["cwd"]) == beta_publish._ENGINE_ROOT == paths.engine_root()


@pytest.mark.parametrize("name", ["LOCK_DIR", "SWEEP_STATE_PATH", "SWEEP_WORKTREE_DIR",
                                  "SWEEP_LOG_PATH"])
def test_scratch_paths_are_not_under_the_vault_data_tree_names(name):
    # Sanity on the derived names themselves (gitignored Worker/_state in both
    # layouts), so a wheel never writes scratch next to canon.
    path = getattr(beta_publish, name)
    assert path.parts[-3:-1] == ("Worker", "_state"), path
