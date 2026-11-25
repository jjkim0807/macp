"""`recur:` on the real tick path.

`orchestrator._main(["auto"])` is what the orchestrator unit runs every few
minutes. The path tests below run it for real — real `auto_recur`, real
`recur.generate_due`, real `cli import-md` subprocess, real `tick()` and real
`worker.sync_vault_state()` — against a throwaway copy of `vault-template` that
is a git repository with a bare remote. Only the clock (`not_before._now`) and
the usage guard are pinned.

Generic slugs only: this is a public repo. 2026-11-27 is a Friday.
"""

from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from todo_system import cli, not_before, recur, worker
from todo_system.orchestrator import orchestrator

REPO = Path(__file__).resolve().parents[3]
NOW = dt.datetime(2026, 11, 27, 10, 3)          # just past the round's 10:00


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def _task(*, worker_mode="delegate", recur_line="recur: weekly\n",
          not_before_line='not_before: "2026-11-27 10:00"\n'):
    return (
        "---\ntags:\n  - todo\n  - work\nstatus: open\ndue: 2026-11-29\n"
        f"{not_before_line}updated: 2026-11-20\n"
        'outcome: "the weekly report is committed"\n'
        'next_action: "run the checks"\n'
        'if_then: "when dispatched, run the checks and write the report"\n'
        f"worker_mode: {worker_mode}\n{recur_line}---\n\n# Weekly audit\n\n"
        "- [x] step one\n- [ ] step two\n\n## Progress Log\n\n- 2026-11-20: created\n")


def _items(vault: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted((vault / "Todo" / "items").iterdir())}


def _make_vault(root: Path, tasks: dict[str, str]) -> Path:
    """A copy of `vault-template` (the skeleton a newcomer copies) as a git repo
    on `main` with a bare remote, the given task files committed and imported."""
    vault = root / "vault"
    shutil.copytree(REPO / "vault-template", vault)
    remote = root / "remote.git"
    remote.mkdir()
    _git(remote, "init", "-q", "--bare", "-b", "main")
    _git(vault, "init", "-q", "-b", "main")
    _git(vault, "config", "user.email", "t@t")
    _git(vault, "config", "user.name", "t")
    _git(vault, "remote", "add", "origin", str(remote))
    # what a real vault keeps out of git: the derived cache and runtime state
    (vault / ".gitignore").write_text(
        "Todo/todo.db\nTodo/NOW.md\nTodo/FOCUS.md\nTodo/Dashboard.md\nWorker/_state/\n",
        encoding="utf-8")
    for slug, text in tasks.items():
        (vault / "Todo" / "items" / f"{slug}.md").write_text(text, encoding="utf-8")
    _git(vault, "add", "-A")
    assert _git(vault, "commit", "-qm", "seed").returncode == 0
    assert _git(vault, "push", "-q", "origin", "main").returncode == 0
    return vault


@pytest.fixture
def enter(monkeypatch):
    """`enter(vault)`: point this process — and the `import-md` it spawns — at
    one throwaway vault, pin the clock, and build its todo.db the way the tick
    itself does."""

    def _enter(vault: Path) -> Path:
        monkeypatch.setenv("MACP_VAULT_ROOT", str(vault))
        monkeypatch.setenv("USAGE_GUARD_DISABLE", "1")       # pacing is not under test
        monkeypatch.setattr(not_before, "_now", lambda: NOW)
        # tick()'s reconcile steps and sync_vault_state read these import-time globals
        monkeypatch.setattr(cli, "ROOT", vault)
        monkeypatch.setattr(cli, "TODO_DIR", vault / "Todo")
        monkeypatch.setattr(cli, "ITEMS_DIR", vault / "Todo" / "items")
        monkeypatch.setattr(cli, "DB_PATH", vault / "Todo" / "todo.db")
        monkeypatch.setattr(worker, "ROOT", vault)
        orchestrator._import_md(vault / "Todo" / "items")
        assert (vault / "Todo" / "todo.db").is_file(), "import-md did not build the task database"
        return vault

    return _enter


def _tick(capsys) -> str:
    """One real `auto` tick with the relative defaults the units pass."""
    capsys.readouterr()
    with mock.patch("todo_system.orchestrator.hq_host.check", return_value=(True, "")):
        rc = orchestrator._main(["--uri", "file://Worker/agents", "auto"])
    assert rc == 0
    return capsys.readouterr().out


def _db_row(vault: Path, slug: str):
    con = sqlite3.connect(f"file:{vault / 'Todo' / 'todo.db'}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT status, not_before, due FROM tasks WHERE slug = ?", (slug,)).fetchone()
    finally:
        con.close()


# --- the real path: tick -> md -> todo.db -> commit ------------------------------


def test_a_tick_writes_the_next_round_absorbs_it_and_the_sync_commits_it(tmp_path, enter, capsys):
    vault = enter(_make_vault(tmp_path, {"weekly-audit-20261127": _task()}))
    new = vault / "Todo" / "items" / "weekly-audit-20261204.md"
    assert not new.exists()

    out = _tick(capsys)

    # 1. the tick wrote the next round, before dispatching the current one
    assert ("[orchestrator] recur: created weekly-audit-20261204 from "
            "weekly-audit-20261127 (not_before 2026-12-04T10:00)") in out
    assert "candidates=1 dispatched=1 " in out
    assert "  dispatched weekly-audit-20261127 -> worker-default" in out
    assert out.index("recur: created") < out.index("candidates=")
    text = new.read_text(encoding="utf-8")
    assert '\nnot_before: "2026-12-04 10:00"\n' in text
    assert "\ndue: 2026-12-06\n" in text
    assert "\nstatus: open\n" in text
    assert "\nrecur: weekly\n" in text
    assert "- [ ] step one\n- [ ] step two\n" in text
    assert text.endswith("## Progress Log\n\n- 2026-11-27: 생성 — weekly-audit-20261127에서 recur\n")
    # the current round is untouched by the step
    assert (vault / "Todo" / "items" / "weekly-audit-20261127.md").read_text(
        encoding="utf-8") == _task()

    # 2. absorbed: the tick's own import-md put it in todo.db, deferred
    assert _db_row(vault, "weekly-audit-20261204") == ("open", "2026-12-04 10:00", "2026-12-06")

    # 3. a second tick creates nothing and does not dispatch the new round
    before = _items(vault)
    again = _tick(capsys)
    assert "recur" not in again
    assert "candidates=1 dispatched=0 " in again
    assert _items(vault) == before

    # 4. committed: the todo-sync commit carries the new file to the remote
    assert _git(vault, "status", "--short", "Todo/items").stdout.strip() == (
        "?? Todo/items/weekly-audit-20261204.md")
    assert worker.sync_vault_state() >= 1
    assert _git(vault, "log", "-1", "--format=%s").stdout.startswith("chore: sync vault state (")
    assert _git(vault, "status", "--short", "Todo/items").stdout == ""
    remote = tmp_path / "remote.git"
    assert _git(remote, "show", "main:Todo/items/weekly-audit-20261204.md").stdout == text


def test_a_persons_own_task_repeats_without_any_worker(tmp_path, enter, capsys):
    """`worker_mode: manual` is never dispatched; its next round is written all the same."""
    task = _task(worker_mode="manual", recur_line="recur: weekly fri 09:00\n",
                 not_before_line='not_before: "2026-11-27 09:00"\n')
    vault = enter(_make_vault(tmp_path, {"weekly-review": task}))

    out = _tick(capsys)

    assert ("[orchestrator] recur: created weekly-review-20261204 from weekly-review "
            "(not_before 2026-12-04T09:00)") in out
    assert "candidates=0 dispatched=0 " in out
    assert _db_row(vault, "weekly-review-20261204") == ("open", "2026-12-04 09:00", "2026-12-06")


def test_a_tick_over_tasks_without_recur_is_byte_identical(tmp_path, enter, capsys, monkeypatch):
    """Two identical vaults with no `recur` anywhere: one ticks with the step, one
    with the step stubbed out. Same output, same task files, no import."""
    tasks = {
        "task-a": _task(recur_line=""),
        "task-b-20261127": _task(recur_line="", worker_mode="manual"),
        "task-c": _task(recur_line="", worker_mode="manual", not_before_line=""),
    }
    imports = []
    real_import = orchestrator._import_md

    def counting_import(items_dir):
        imports.append(Path(items_dir))
        return real_import(items_dir)

    swept = []
    real_generate = recur.generate_due

    def spying_generate(items_dir, **kwargs):
        swept.append(Path(items_dir))
        return real_generate(items_dir, **kwargs)

    with_step = enter(_make_vault(tmp_path / "with-step", tasks))
    before = _items(with_step)
    monkeypatch.setattr(orchestrator, "_import_md", counting_import)
    monkeypatch.setattr(recur, "generate_due", spying_generate)
    out_with = _tick(capsys)
    assert swept == [with_step / "Todo" / "items"]          # the step really ran
    assert imports == []                                    # …and imported nothing
    assert _items(with_step) == before                      # …and wrote nothing
    assert "recur" not in out_with
    monkeypatch.setattr(orchestrator, "_import_md", real_import)

    without_step = enter(_make_vault(tmp_path / "without-step", tasks))
    monkeypatch.setattr(orchestrator, "auto_recur", lambda items_dir, **_kw: [])
    out_without = _tick(capsys)
    assert len(swept) == 1                                  # the stub kept the step out
    assert _items(without_step) == before

    assert out_with == out_without
    assert "candidates=1 dispatched=1 " in out_with


# --- auto_recur itself -----------------------------------------------------------


def _due_items(tmp_path, monkeypatch) -> Path:
    items = tmp_path / "Todo" / "items"
    items.mkdir(parents=True)
    (items / "weekly-audit-20261127.md").write_text(_task(), encoding="utf-8")
    monkeypatch.setattr(not_before, "_now", lambda: NOW)
    return items


def test_auto_recur_creates_then_syncs_the_db_once(tmp_path, monkeypatch, capsys):
    items = _due_items(tmp_path, monkeypatch)
    calls = []
    created = orchestrator.auto_recur(items, _import=lambda: calls.append(1))
    assert [c.slug for c in created] == ["weekly-audit-20261204"]
    assert (items / "weekly-audit-20261204.md").is_file()
    assert calls == [1]
    assert capsys.readouterr().out == (
        "[orchestrator] recur: created weekly-audit-20261204 from weekly-audit-20261127 "
        "(not_before 2026-12-04T10:00)\n")
    # idempotent: the next tick finds nothing and does not import again
    assert orchestrator.auto_recur(items, _import=lambda: calls.append(1)) == []
    assert calls == [1]
    assert capsys.readouterr().out == ""


def test_auto_recur_with_nothing_due_is_silent_and_skips_the_import(tmp_path, monkeypatch, capsys):
    items = _due_items(tmp_path, monkeypatch)
    monkeypatch.setattr(not_before, "_now", lambda: dt.datetime(2026, 11, 27, 9, 59))
    calls = []
    assert orchestrator.auto_recur(items, _import=lambda: calls.append(1)) == []
    assert calls == []
    assert capsys.readouterr().out == ""


def test_auto_recur_is_fail_open_on_a_sweep_error(tmp_path, capsys):
    def boom(*_a, **_k):
        raise RuntimeError("disk gone")

    assert orchestrator.auto_recur(tmp_path, _generate=boom) == []
    assert capsys.readouterr().out == "[orchestrator] recur sweep skipped (fail-open): disk gone\n"


def test_auto_recur_keeps_what_it_created_when_the_import_fails(tmp_path, monkeypatch, capsys):
    items = _due_items(tmp_path, monkeypatch)

    def boom():
        raise RuntimeError("no interpreter")

    created = orchestrator.auto_recur(items, _import=boom)
    assert [c.slug for c in created] == ["weekly-audit-20261204"]
    assert "[orchestrator] import-md after recur failed (fail-open): no interpreter\n" in (
        capsys.readouterr().out)


def test_auto_recur_names_a_series_it_had_to_skip(tmp_path, monkeypatch, capsys):
    items = _due_items(tmp_path, monkeypatch)
    (items / "broken-series.md").mkdir()                    # reading it raises
    created = orchestrator.auto_recur(items, _import=lambda: None)
    assert [c.slug for c in created] == ["weekly-audit-20261204"]
    assert "[orchestrator] recur skipped broken-series: " in capsys.readouterr().out


# --- the suite guard (todo_system/conftest.py) -------------------------------------


def test_the_step_does_nothing_under_a_real_root(real_auto_side_effects):
    """`main auto` tests that keep the default --items-dir point the step at the
    vault this process resolved. It must not even be read there."""
    assert orchestrator.auto_recur is not real_auto_side_effects["auto_recur"]
    calls = []

    def recorder(*args, **kwargs):
        calls.append(args)
        return []

    for items in (worker.ROOT / "Todo" / "items", worker.ROOT, REPO / "vault-template" / "Todo" / "items"):
        assert orchestrator.auto_recur(items, _generate=recorder, _import=recorder) == []
    assert calls == []


def test_the_step_is_the_real_one_for_a_tmp_vault(tmp_path):
    calls = []

    def recorder(items_dir, **kwargs):
        calls.append(items_dir)
        return []

    assert orchestrator.auto_recur(tmp_path, _generate=recorder) == []
    assert calls == [tmp_path]
