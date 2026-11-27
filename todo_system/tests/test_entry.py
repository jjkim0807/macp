"""``todo_system._entry.main``: the ``macp`` console script.

Without a vault, ``import todo_system.cli`` raises ``VaultRootError``. The entry
must turn that into exit 2 with the error and a quick start, not a traceback; with
a vault it must hand off to ``cli.main`` unchanged.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from unittest import mock

import pytest

from todo_system import _entry, paths

VAULT_TEMPLATE = Path(__file__).resolve().parents[2] / "vault-template"


def _main_fresh_cli(argv: list[str] | None = None, *, vault: bool = False) -> int:
    """``_entry.main`` with a real re-import of todo_system.cli.

    ``vault=False`` makes the vault unresolvable. Drop both the module cache
    entry and the package attribute (``from todo_system import cli`` reads the
    latter first), then put the originals back so later tests see the same objects.
    """
    import todo_system
    saved_mod = sys.modules.pop("todo_system.cli", None)
    had_attr = hasattr(todo_system, "cli")
    saved_attr = todo_system.__dict__.pop("cli", None)
    try:
        if vault:
            return _entry.main(argv)
        with mock.patch.object(paths, "vault_root",
                               side_effect=paths.VaultRootError("cannot resolve the vault root")):
            return _entry.main(argv)
    finally:
        sys.modules.pop("todo_system.cli", None)
        todo_system.__dict__.pop("cli", None)
        if saved_mod is not None:
            sys.modules["todo_system.cli"] = saved_mod
        if had_attr:
            todo_system.cli = saved_attr


def test_missing_vault_prints_fix_and_exits_2(capsys):
    rc = _main_fresh_cli()
    err = capsys.readouterr().err
    assert rc == 2
    assert "cannot resolve the vault root" in err
    assert "MACP_VAULT_ROOT" in err and "vault-template" in err
    assert "macp init" in err          # lite mode's one-command start
    assert "Traceback" not in err


def test_with_vault_hands_off_to_cli_main():
    from todo_system import cli
    with mock.patch.object(cli, "main", return_value=7) as m:
        assert _entry.main() == 7
    m.assert_called_once_with()


def _assert_lists_readme_verbs(out: str) -> None:
    lines = out.splitlines()
    for verb in ("init", "add", "up", "status"):
        hits = [ln for ln in lines if ln.split()[:1] == [verb]]
        assert hits, f"{verb!r} missing from macp --help:\n{out}"
        assert len(hits[0].split()) > 1, f"{verb!r} has no one-line description"


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_without_vault_lists_readme_verbs_and_exits_0(flag, capsys, monkeypatch, tmp_path):
    # A new user right after `uv tool install`: fresh HOME, no vault anywhere.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    monkeypatch.delenv("MACP_HOME", raising=False)
    rc = _main_fresh_cli([flag])
    captured = capsys.readouterr()
    assert rc == 0
    _assert_lists_readme_verbs(captured.out)
    assert "macp init" in captured.out
    assert "Traceback" not in captured.out + captured.err


def test_help_with_vault_lists_readme_verbs_and_task_cli(capsys, monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    shutil.copytree(VAULT_TEMPLATE, vault)
    monkeypatch.setenv("MACP_VAULT_ROOT", str(vault))
    rc = _main_fresh_cli(["--help"], vault=True)
    out = capsys.readouterr().out
    assert rc == 0
    _assert_lists_readme_verbs(out)
    assert "usage: macp" in out          # the todo CLI's own help follows
    assert "complete" in out and "import-md" in out
