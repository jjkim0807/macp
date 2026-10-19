"""``todo_system._entry.main``: the ``macp`` console script.

Without a vault, ``import todo_system.cli`` raises ``VaultRootError``. The entry
must turn that into exit 2 with the error and a quick start, not a traceback; with
a vault it must hand off to ``cli.main`` unchanged.
"""

from __future__ import annotations

import sys
from unittest import mock

from todo_system import _entry, paths


def test_missing_vault_prints_fix_and_exits_2(capsys):
    # Force a real re-import of todo_system.cli: drop both the module cache entry
    # and the package attribute (``from todo_system import cli`` reads the latter
    # first), then put the originals back so later tests see the same objects.
    import todo_system
    saved_mod = sys.modules.pop("todo_system.cli", None)
    had_attr = hasattr(todo_system, "cli")
    saved_attr = todo_system.__dict__.pop("cli", None)
    try:
        with mock.patch.object(paths, "vault_root",
                               side_effect=paths.VaultRootError("cannot resolve the vault root")):
            rc = _entry.main()
    finally:
        sys.modules.pop("todo_system.cli", None)
        todo_system.__dict__.pop("cli", None)
        if saved_mod is not None:
            sys.modules["todo_system.cli"] = saved_mod
        if had_attr:
            todo_system.cli = saved_attr
    err = capsys.readouterr().err
    assert rc == 2
    assert "cannot resolve the vault root" in err
    assert "MACP_VAULT_ROOT" in err and "vault-template" in err
    assert "Traceback" not in err


def test_with_vault_hands_off_to_cli_main():
    from todo_system import cli
    with mock.patch.object(cli, "main", return_value=7) as m:
        assert _entry.main() == 7
    m.assert_called_once_with()
