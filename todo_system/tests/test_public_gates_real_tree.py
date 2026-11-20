"""Public-split gates run against *this repo's real tree*, not a fixture.

``test_check_public_identifiers.py`` pins the gate *logic* on throwaway repos
via the ``AUDIT_*`` env seams. That never looks at the code a PR actually
ships, and the gates themselves only ran when a public-split leaf named them in
its acceptance criteria — vault-task Verify runs just ruff + pytest. PR #201
merged a hard-coded GitHub owner URL into ``orchestrator/beta_publish.py``
through exactly that gap. These tests put the gates on the standard pytest
path, so any worker PR that plants a personal identifier in the engine tree
fails ``pytest -q`` with the offending file named in the assertion output:

* ratchet (``scripts/check_public_identifiers.sh``) — no file may gain hits
  over ``scripts/public_identifiers_baseline.txt``.
* export (``... --export``) — the public export set must stay at zero.
* engine isolation (``scripts/check_engine_isolation.sh``) — the engine tree
  archived alone still runs against a copy of ``vault-template``, with
  ``UV_OFFLINE=1`` so it can never reach the network. It also runs the README
  "Try it in five minutes" loop (leg T) in a fresh HOME and fails if any step
  exits non-zero or an expected output line goes missing. Measured 2026-10-08
  on the HQ host, warm uv cache: PASS in 6.31s and 3.00s (limit for inclusion
  was 60s offline); 2026-11-20 with leg T: PASS in 7.6s. The script archives
  committed HEAD and builds a fresh ``.venv`` in a ``mktemp`` dir from the uv
  cache for its ``uv run`` calls — a host with a cold cache fails here offline
  rather than hanging.

No literal from the identifier gate's ``PATTERN`` may appear in this file: it
sits inside the scanned tree and is *not* on the script's self-exclude list,
so the gate checks this file too.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
_TIMEOUT = 60
# Env that would silently point a gate at another tree/baseline: the identifier
# script's test-only seams, plus git's own repo-location overrides (the scripts
# resolve the repo with ``git rev-parse --show-toplevel`` from cwd).
_LEAKY_ENV = (
    "AUDIT_ROOT",
    "AUDIT_BASELINE",
    "AUDIT_EXPORT_EXCLUDE",
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
)


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _LEAKY_ENV}
    env.update(extra)
    return env


def _run(
    script: str, *args: str, env: dict[str, str] | None = None, timeout: int = _TIMEOUT
) -> tuple[int, str]:
    proc = subprocess.run(
        ["bash", str(SCRIPTS / script), *args],
        cwd=str(REPO),
        env=env if env is not None else _env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout + proc.stderr


def _reported_repo(out: str) -> Path:
    line = next(ln for ln in out.splitlines() if ln.startswith("repo:"))
    return Path(line.split(":", 1)[1].strip()).resolve()


def test_repo_root_is_a_git_checkout():
    # Assert, never skip: a Verify worktree always has .git (file or dir), and
    # a silent skip here would turn every gate below into a no-op.
    assert (REPO / ".git").exists(), REPO


@pytest.mark.parametrize("args", [(), ("--export",)], ids=["ratchet", "export"])
def test_real_tree_identifier_gate_passes(args):
    if not (SCRIPTS / "check_public_identifiers.sh").is_file():
        pytest.skip(
            "vault-only: the identifier gate script is self-excluded from the engine "
            "export set (see the script's own SCOPE), so it exists only in the vault — "
            "there is nothing to run in an engine-only tree (public-split-10 AC2)."
        )
    rc, out = _run("check_public_identifiers.sh", *args)
    # ``out`` carries the hit list (``+ path:N`` / ``  path:N``) on failure.
    assert rc == 0, out
    assert "RESULT: PASS" in out, out
    # The gate must have scanned this checkout, not some leaked other root.
    assert _reported_repo(out) == REPO, out


def test_real_tree_engine_isolation_passes():
    rc, out = _run(
        "check_engine_isolation.sh", env=_env(UV_OFFLINE="1"), timeout=_TIMEOUT * 2
    )
    assert rc == 0, out
    assert "RESULT: PASS" in out, out
    assert _reported_repo(out) == REPO, out
