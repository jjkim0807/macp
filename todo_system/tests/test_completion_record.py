"""`completed:` is stamped once, on a human timed block's open→done (2026-11-19).

Runs the real ``cli.import_markdown_to_db`` / ``cli.refresh_single_task`` paths
(both reach ``cli.upsert_task`` → ``calendar_sync.record_completion_transition``)
with the ``vault`` fixture pattern of ``test_import_md_vanished.py``.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from todo_system import calendar_sync as CS
from todo_system import cli

STAMP = "2026-11-19 16:53"


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "vault"
    items = root / "Todo" / "items"
    items.mkdir(parents=True)
    monkeypatch.setattr(cli, "ROOT", root)
    monkeypatch.setattr(cli, "ITEMS_DIR", items)
    monkeypatch.setattr(CS, "_now_local", lambda: dt.datetime(2026, 11, 19, 16, 53))
    con = cli.db_connect(root / "todo.db")
    cli.init_db(con)
    return SimpleNamespace(root=root, items=items, con=con)


def _md(items: Path, slug: str, *, status: str = "open",
        scheduled: str | None = "2026-11-19 10:00~13:00", extra: str = "") -> Path:
    fm = ["---", f"status: {status}"]
    if scheduled is not None:
        fm.append(f'scheduled: "{scheduled}"')
    if extra:
        fm.append(extra)
    fm.append("---")
    path = items / f"{slug}.md"
    path.write_text("\n".join(fm) + f"\n\n# {slug}\n\n- [ ] step\n", encoding="utf-8")
    return path


def _set_done(path: Path) -> None:
    path.write_text(path.read_text(encoding="utf-8").replace("status: open", "status: done"),
                    encoding="utf-8")


def test_open_to_done_stamps_once(vault):
    p = _md(vault.items, "t")
    cli.import_markdown_to_db(vault.con)
    assert "completed:" not in p.read_text(encoding="utf-8")

    _set_done(p)
    cli.import_markdown_to_db(vault.con)
    text = p.read_text(encoding="utf-8")
    assert f'completed: "{STAMP}"' in text

    # Already done in the DB now → a later import does not touch the md.
    before = p.read_bytes()
    cli.import_markdown_to_db(vault.con)
    assert p.read_bytes() == before


def test_task_first_seen_done_is_not_stamped(vault):
    p = _md(vault.items, "old", status="done")
    cli.import_markdown_to_db(vault.con)
    assert "completed:" not in p.read_text(encoding="utf-8")


def test_existing_completed_is_kept(vault):
    p = _md(vault.items, "t", extra='completed: "2026-11-18 09:00"')
    cli.import_markdown_to_db(vault.con)
    _set_done(p)
    cli.import_markdown_to_db(vault.con)
    text = p.read_text(encoding="utf-8")
    assert 'completed: "2026-11-18 09:00"' in text
    assert STAMP not in text


@pytest.mark.parametrize("mode, stamped", [("auto", False), ("delegate", False),
                                            ("manual", True), ("off", True)])
def test_only_human_owned_tasks_are_stamped(vault, mode, stamped):
    p = _md(vault.items, "t", extra=f"worker_mode: {mode}")
    cli.import_markdown_to_db(vault.con)
    _set_done(p)
    cli.import_markdown_to_db(vault.con)
    assert ("completed:" in p.read_text(encoding="utf-8")) is stamped


@pytest.mark.parametrize("scheduled", ["2026-11-19", None, "-"])
def test_untimed_tasks_are_not_stamped(vault, scheduled):
    p = _md(vault.items, "t", scheduled=scheduled)
    cli.import_markdown_to_db(vault.con)
    _set_done(p)
    cli.import_markdown_to_db(vault.con)
    assert "completed:" not in p.read_text(encoding="utf-8")


def test_refresh_single_task_path_also_stamps(vault):
    """`cli complete` / `cli set --status done` write the md then call
    refresh_single_task — the same upsert hook covers them."""
    p = _md(vault.items, "t")
    cli.import_markdown_to_db(vault.con)
    _set_done(p)
    cli.refresh_single_task(vault.con, p)
    assert f'completed: "{STAMP}"' in p.read_text(encoding="utf-8")
    row = vault.con.execute("SELECT status FROM tasks WHERE slug = 't'").fetchone()
    assert row[0] == "done"


def test_errors_never_break_the_import(vault, monkeypatch, capsys):
    p = _md(vault.items, "t")
    cli.import_markdown_to_db(vault.con)
    _set_done(p)
    before = p.read_bytes()

    def boom(_):
        raise RuntimeError("boom")

    monkeypatch.setattr(CS, "_parse_scheduled_value", boom)
    assert cli.import_markdown_to_db(vault.con) == 1
    assert p.read_bytes() == before
    assert "completed record skipped" in capsys.readouterr().out
    row = vault.con.execute("SELECT status FROM tasks WHERE slug = 't'").fetchone()
    assert row[0] == "done"
