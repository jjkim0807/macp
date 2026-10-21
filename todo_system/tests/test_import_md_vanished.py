"""``import_markdown_to_db`` must survive a file that vanishes between the
``iter_task_files()`` scan and ``parse_task_file()``'s read (the real daemon
crash-loop this guards: a vault-sync rebase deletes a task md mid-scan).

Uses the same ``vault``-fixture pattern as ``test_cli_write_commands.py``:
redirect ``cli.ROOT``/``cli.ITEMS_DIR`` into a tmp dir (``DB_PATH`` etc. are
already redirected by the package-wide ``_RUNTIME_PATHS`` autouse fixture in
``todo_system/conftest.py``).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from todo_system import cli


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "vault"
    items = root / "Todo" / "items"
    items.mkdir(parents=True)

    monkeypatch.setattr(cli, "ROOT", root)
    monkeypatch.setattr(cli, "ITEMS_DIR", items)

    db = root / "todo.db"
    con = cli.db_connect(db)
    cli.init_db(con)

    return SimpleNamespace(root=root, items=items, con=con)


def _write_md(items: Path, slug: str, *, title: str) -> Path:
    lines = ["---", "status: open", "---", "", f"# {title}", "", "- [ ] step one", ""]
    path = items / f"{slug}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_vanished_file_is_skipped_and_logged(vault, capsys: pytest.CaptureFixture[str]) -> None:
    _write_md(vault.items, "keep-a", title="Keep A")
    vanished_path = _write_md(vault.items, "vanishes", title="Vanishes")
    _write_md(vault.items, "keep-b", title="Keep B")

    real_parse_task_file = cli.parse_task_file

    def wrapper(path: Path):
        if path.name == "vanishes.md":
            raise FileNotFoundError(path)
        return real_parse_task_file(path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "parse_task_file", wrapper)
        count = cli.import_markdown_to_db(vault.con)

    out = capsys.readouterr().out
    assert count == 2
    assert f"import-md: skipped vanished {vanished_path}" in out

    slugs = {row[0] for row in vault.con.execute("SELECT slug FROM tasks")}
    assert slugs == {"keep-a", "keep-b"}


def test_other_exceptions_still_propagate(vault) -> None:
    _write_md(vault.items, "keep-a", title="Keep A")
    _write_md(vault.items, "breaks", title="Breaks")

    real_parse_task_file = cli.parse_task_file

    def wrapper(path: Path):
        if path.name == "breaks.md":
            raise ValueError("not a FileNotFoundError")
        return real_parse_task_file(path)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "parse_task_file", wrapper)
        with pytest.raises(ValueError):
            cli.import_markdown_to_db(vault.con)
