"""Split install (engine checkout != vault root): one more place mixed the roots.

Notes/macp-public-split-inventory.md §11, leaf 11b.

scripts/health_report.py:gather_code_time() read the commit time of
todo_system/ and scripts/ from the vault's git. A split vault repo holds
neither path, git answers empty, and judge_daemon_staleness sat at "skip"
forever. It now reads the engine checkout (_INSTALL_DIR).
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import subprocess
import sys
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[2]
_HEALTH_SRC = _ENGINE / "scripts" / "health_report.py"


def _vault_skeleton(root: Path) -> Path:
    """Marker + Todo/ + Worker/, and no engine files (no todo_system/, scripts/)."""
    (root / ".macp").mkdir(parents=True)
    (root / ".macp" / "config.toml").write_text("", encoding="utf-8")
    (root / "Todo").mkdir()
    (root / "Worker").mkdir()
    return root


def _load_health_report(monkeypatch, name: str):
    spec = importlib.util.spec_from_file_location(name, _HEALTH_SRC)
    mod = importlib.util.module_from_spec(spec)
    # Registration precedes exec (@dataclass resolves via sys.modules); a
    # unique name keeps test_health_report's module untouched.
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)
    return mod


def test_gather_code_time_reads_engine_checkout_not_vault(tmp_path, monkeypatch):
    vault = _vault_skeleton(tmp_path / "vault")
    monkeypatch.setenv("MACP_VAULT_ROOT", str(vault))
    H = _load_health_report(monkeypatch, "health_report_11b")

    # Split layout sanity: the vault resolved to the tmp skeleton.
    assert Path(H.VAULT).resolve() == vault.resolve()
    assert H._INSTALL_DIR != H.VAULT
    ct = H.gather_code_time()
    assert ct is not None, "code time read from the vault, which ships no engine code"
    assert H.judge_daemon_staleness({}, ct).level != "skip"

    # Combined layout: same value as the git log of the (shared) checkout.
    monkeypatch.setattr(H, "VAULT", H._INSTALL_DIR)
    out = subprocess.run(
        ["git", "-C", str(H._INSTALL_DIR), "log", "-1", "--format=%cI", "--",
         *H.CODE_TIME_PATHS],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0 and out.stdout.strip()
    expected = dt.datetime.fromisoformat(out.stdout.strip())
    assert H.gather_code_time() == expected

    # A real git failure still yields skip.
    assert H.judge_daemon_staleness({}, None).level == "skip"
