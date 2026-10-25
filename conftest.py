"""Test-collection-only default for ``MACP_VAULT_ROOT``.

A fresh clone has no ``MACP_VAULT_ROOT`` and no ``.macp/config.toml`` marker,
so importing ``todo_system.cli`` (which resolves the vault root at import
time via ``calendar_sync.py``) raises ``todo_system.paths.VaultRootError``
during collection — before any test runs. ``setdefault`` only fills the gap
when the variable is absent or empty; a real ``MACP_VAULT_ROOT`` (set by a
caller, or by production code) is left untouched, and ``todo_system.paths``
keeps raising ``VaultRootError`` on its own when nothing is set.
"""

import os

if not os.environ.get("MACP_VAULT_ROOT"):
    os.environ["MACP_VAULT_ROOT"] = "vault-template"
