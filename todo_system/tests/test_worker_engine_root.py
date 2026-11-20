"""worker.py engine files resolve against the install, not the vault (c-1).

Notes/macp-public-split-inventory.md §11.8 c-1: worker.py used to anchor three
engine-shipped files — `.claude/commands`, `scripts/kakao_send.sh` and
`scripts/health_report.py` — on the vault root. In a split install
(MACP_VAULT_ROOT pointing at a vault outside the engine tree) that made
restart_stale_daemons() a silent no-op, because health_report was looked up in a
vault that does not ship it.

The check runs in a fresh interpreter: todo_system/conftest.py swaps
worker.KAKAO_SEND_SCRIPT in-process, and the constants are computed at import
time, so only a new process sees what the module really resolves. getattr(...,
None) keeps a pre-fix run failing on the actual path assertion (COMMANDS_DIR /
KAKAO_SEND_SCRIPT under the tmp vault), not merely on a missing attribute.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from todo_system import worker

NAMES = ("_ENGINE_ROOT", "COMMANDS_DIR", "KAKAO_SEND_SCRIPT", "HEALTH_REPORT_SCRIPT")

CHILD = (
    "import json\n"
    "from todo_system import worker as w\n"
    "vals = {}\n"
    f"for n in {NAMES!r}:\n"
    "    v = getattr(w, n, None)\n"
    "    vals[n] = None if v is None else str(v)\n"
    "print(json.dumps(vals))\n"
)


def test_engine_paths_ignore_redirected_vault(tmp_path):
    # Empty vault skeleton: the marker plus Todo/, and no scripts/ or .claude/.
    vault = tmp_path / "vault"
    (vault / ".macp").mkdir(parents=True)
    (vault / ".macp" / "config.toml").write_text("", encoding="utf-8")
    (vault / "Todo").mkdir()

    engine = Path(worker.__file__).resolve().parents[1]

    env = dict(os.environ)
    for key in ("WORKER_HOST", "MAESTRO_VAULT_DIR", "KAKAO_VAULT_DIR"):
        env.pop(key, None)
    env["MACP_VAULT_ROOT"] = str(vault)
    prior = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(engine) + (os.pathsep + prior if prior else "")

    proc = subprocess.run(
        [sys.executable, "-c", CHILD],
        cwd=engine, env=env, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, proc.stdout
    got = json.loads(lines[-1])

    vault_resolved = vault.resolve()
    expected = {
        "_ENGINE_ROOT": engine,
        "COMMANDS_DIR": engine / "todo_system" / "prompts",
        "KAKAO_SEND_SCRIPT": engine / "scripts" / "kakao_send.sh",
        "HEALTH_REPORT_SCRIPT": engine / "scripts" / "health_report.py",
    }
    bad = []
    for name in NAMES:
        raw = got.get(name)
        if raw is None:
            bad.append((name, "missing"))
            continue
        path = Path(raw).resolve()
        if path.is_relative_to(vault_resolved):
            bad.append((name, f"under redirected vault: {raw}"))
        elif not path.is_relative_to(engine):
            bad.append((name, f"outside engine tree: {raw}"))
        elif path != expected[name]:
            bad.append((name, f"expected {expected[name]}, got {raw}"))
    assert not bad, bad
