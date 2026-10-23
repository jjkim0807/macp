"""state_migrate — git→external one-off import + assignments snapshot export.

Leaf ⑤ of docs/design/state-store.md. The git-mode invariant (the default
backend stays git and nothing in production imports the migrator) is pinned
first; the import/export behaviour follows.

Every DB here is an explicit tmp ``sqlite_path`` — never the default
``paths.state_sqlite_path()``, which would write a real host file outside the
vault.
"""

from __future__ import annotations

from pathlib import Path

import todo_system
from todo_system.orchestrator.state_store import GitStateStore, open_state_store

# ---------------------------------------------------------------------------
# Git-mode invariant — pinned before the migrator exists
# ---------------------------------------------------------------------------


def test_default_backend_without_config_is_git(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_STATE_BACKEND", raising=False)
    # Keep the .macp/config.toml walk inside tmp, away from any host config.
    monkeypatch.chdir(tmp_path)
    assert isinstance(open_state_store(vault_root=tmp_path), GitStateStore)


def test_no_production_module_imports_state_migrate():
    pkg = Path(todo_system.__file__).parent
    offenders = [
        str(p.relative_to(pkg))
        for p in pkg.rglob("*.py")
        if "tests" not in p.relative_to(pkg).parts
        and p.name != "state_migrate.py"
        and "state_migrate" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
