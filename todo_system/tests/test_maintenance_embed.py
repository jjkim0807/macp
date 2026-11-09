"""The maintenance-tick vault embedding (todo_system/maintenance_embed.py).

Every seam is injected: a tmp vault from the ``isolated_vault`` fixture, the
module's own ``VAULT_ROOT`` / ``DB_PATH`` / ``STATE_DIR`` pointed into it, and a
fake clock where the throttle is under test. Nothing here touches the real
``Memory/relations.sqlite``; the suite-wide ``_embed_tick_isolated`` fixture
(todo_system/conftest.py) backs that up, and ``SuiteIsolationTest`` pins that it
is live.

What the acceptance criterion asks for, and where each half is pinned:

* "**증분으로 실행** (내용 해시 미변경 문서는 건너뜀)" — the real-extension leg
  embeds two docs, then returns **None** on a second call with nothing changed,
  and a third call after ONE doc is edited reports exactly one re-embed. That is
  the content-hash diff observed end to end, not a mock of it.
* "**실패는 한 줄 경고만**" — a raising ``embed_vault``, a raising
  ``init_relations_db`` and an absent sqlite-vec each produce one line and no
  exception; ``worker.cmd_maintenance`` keeps going either way.

The mock-free legs guard with ``importorskip`` (the G2 convention): the gate
command must pass whether or not the optional extension is installed, so every
contract that can be pinned without it is pinned without it.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from todo_system import graph_memory as gm
from todo_system import maintenance_embed as me

DOC_A = "Notes/sample-note-a.md"
DOC_B = "Notes/sample-note-b.md"


@pytest.fixture
def tick_vault(isolated_vault, monkeypatch, tmp_path):
    """Lift the suite-wide kill switch and point every seam at the tmp vault.

    Requested (not autouse), so it runs AFTER ``_embed_tick_isolated`` and its
    ``setattr`` wins — the conftest tmp paths stay behind it as a second belt."""
    monkeypatch.setenv(me.KILL_ENV, "1")
    monkeypatch.setattr(me, "VAULT_ROOT", isolated_vault.root)
    monkeypatch.setattr(me, "DB_PATH", isolated_vault.db_path)
    monkeypatch.setattr(me, "STATE_DIR", tmp_path / "embed-state")
    return isolated_vault


class FakeResult:
    """An EmbedResult-shaped answer, so the silence/line rules can be pinned
    without the extension."""

    def __init__(self, *, embedded=0, removed=0, unchanged=0, total=0, reason=None):
        self.embedded, self.removed, self.unchanged = embedded, removed, unchanged
        self.total_corpus, self.dim, self.skipped_reason = total, gm.EMBED_DIM, reason
        self.to_embed = self.to_remove = 0
        self.dry_run = False


def _fake_embed(result, calls):
    def _embed(con, **kw):
        calls.append(kw)
        print("embed_vault's own stdout line")   # must not reach the tick's output
        return result
    return _embed


# -- the kill switch ----------------------------------------------------------

def test_the_kill_switch_stops_the_step_before_it_opens_anything(tick_vault, monkeypatch):
    calls: list = []
    monkeypatch.setattr(gm, "embed_vault", _fake_embed(FakeResult(embedded=3), calls))
    monkeypatch.setattr(gm, "init_relations_db",
                        lambda *_a, **_k: pytest.fail("opened the DB with the switch off"))
    monkeypatch.setenv(me.KILL_ENV, "0")

    assert me.embed_tick() is None
    assert calls == []
    assert me.enabled() is False


@pytest.mark.parametrize("value,expected", [("0", False), ("", True), ("1", True)])
def test_only_an_explicit_zero_disables(monkeypatch, value, expected):
    monkeypatch.setenv(me.KILL_ENV, value)
    assert me.enabled() is expected


# -- silence vs one line ------------------------------------------------------

def test_nothing_changed_is_silent(tick_vault, monkeypatch, capsys):
    calls: list = []
    monkeypatch.setattr(gm, "_load_sqlite_vec", lambda _con: None)
    monkeypatch.setattr(gm, "embed_vault", _fake_embed(FakeResult(unchanged=7, total=7), calls))

    assert me.embed_tick() is None
    assert len(calls) == 1, "silence must come from the result, not from skipping the call"
    assert capsys.readouterr().out == "", "embed_vault's own line must not reach the tick"


def test_work_done_is_exactly_one_line(tick_vault, monkeypatch):
    monkeypatch.setattr(gm, "_load_sqlite_vec", lambda _con: None)
    monkeypatch.setattr(gm, "embed_vault",
                        _fake_embed(FakeResult(embedded=2, removed=1, unchanged=5, total=7), []))

    line = me.embed_tick()

    assert line is not None
    assert line.count("\n") == 0
    assert line.startswith("embed-tick: embedded 2, removed 1, unchanged 5")
    assert f"dim={gm.EMBED_DIM}" in line


def test_the_vault_root_is_handed_to_embed_vault(tick_vault, monkeypatch):
    calls: list = []
    monkeypatch.setattr(gm, "_load_sqlite_vec", lambda _con: None)
    monkeypatch.setattr(gm, "embed_vault", _fake_embed(FakeResult(embedded=1), calls))

    me.embed_tick()

    assert calls == [{"vault_root": tick_vault.root}]


# -- failures: one line, never a raise ----------------------------------------

def test_a_raising_embed_is_one_line_not_an_exception(tick_vault, monkeypatch):
    def _boom(*_a, **_kw):
        raise RuntimeError("embedder exploded")

    monkeypatch.setattr(gm, "_load_sqlite_vec", lambda _con: None)
    monkeypatch.setattr(gm, "embed_vault", _boom)

    line = me.embed_tick()

    assert line is not None
    assert line.startswith("embed-tick: error (RuntimeError: embedder exploded")
    assert line.endswith("— skipped")


def test_an_unopenable_db_is_one_line_not_an_exception(tick_vault, monkeypatch):
    monkeypatch.setattr(gm, "init_relations_db",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk went away")))

    line = me.embed_tick()

    assert line is not None
    assert line.startswith("embed-tick: error (OSError: disk went away")


def test_a_long_error_is_clipped(tick_vault, monkeypatch):
    monkeypatch.setattr(gm, "_load_sqlite_vec", lambda _con: None)
    monkeypatch.setattr(gm, "embed_vault",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x" * 5000)))

    line = me.embed_tick()

    assert len(line) <= me.TEXT_LIMIT + len(" — skipped")


# -- sqlite-vec absent: cheap probe + a throttled line ------------------------

def _no_sqlite_vec(monkeypatch, calls):
    def _raise(_con):
        raise gm.EmbeddingsUnavailable("sqlite-vec is not installed (simulated)")

    monkeypatch.setattr(gm, "_load_sqlite_vec", _raise)
    monkeypatch.setattr(gm, "embed_vault", _fake_embed(FakeResult(embedded=9), calls))


def test_absent_sqlite_vec_skips_without_walking_the_corpus(tick_vault, monkeypatch):
    """The point of the cheap probe: embed_vault hashes ~4,000 docs before it
    discovers the extension is missing, and on such a host that work is thrown
    away every five minutes."""
    calls: list = []
    _no_sqlite_vec(monkeypatch, calls)

    line = me.embed_tick(now=1000.0)

    assert line is not None
    assert "sqlite-vec 없음" in line
    assert calls == [], "the corpus must not be walked once the probe has failed"


def test_the_skip_notice_is_throttled_to_once_a_day(tick_vault, monkeypatch):
    calls: list = []
    _no_sqlite_vec(monkeypatch, calls)

    first = me.embed_tick(now=1000.0)
    inside = me.embed_tick(now=1000.0 + me.SKIP_NOTICE_COOLDOWN_S - 1)
    after = me.embed_tick(now=1000.0 + me.SKIP_NOTICE_COOLDOWN_S + 1)

    assert first is not None
    assert inside is None, "288 identical lines a day is what the throttle prevents"
    assert after is not None


def test_a_clock_that_stepped_back_does_not_suppress(tick_vault, monkeypatch):
    calls: list = []
    _no_sqlite_vec(monkeypatch, calls)

    assert me.embed_tick(now=10_000.0) is not None
    assert me.embed_tick(now=1_000.0) is not None


def test_the_state_file_holds_one_stamp_and_is_private(tick_vault, monkeypatch):
    calls: list = []
    _no_sqlite_vec(monkeypatch, calls)

    me.embed_tick(now=1234.0)

    data = json.loads(me.state_path().read_text(encoding="utf-8"))
    assert data == {"last_skip_notice_ts": 1234.0}
    assert stat.S_IMODE(os.stat(me.state_path()).st_mode) == 0o600


def test_an_unwritable_state_dir_still_yields_one_line(tick_vault, monkeypatch):
    calls: list = []
    _no_sqlite_vec(monkeypatch, calls)
    monkeypatch.setattr(me, "_write_state", lambda _s: "state write failed (OSError)")

    line = me.embed_tick(now=1000.0)

    assert line is not None
    assert "state write failed" in line


def test_a_skipped_reason_from_embed_vault_takes_the_same_throttle(tick_vault, monkeypatch):
    """The probe can pass and embed_vault still skip (extension lost between the
    two). Same shape, same throttle — not a new code path for the reader."""
    monkeypatch.setattr(gm, "_load_sqlite_vec", lambda _con: None)
    monkeypatch.setattr(gm, "embed_vault",
                        _fake_embed(FakeResult(reason="vanished mid-tick"), []))

    assert me.embed_tick(now=1000.0) is not None
    assert me.embed_tick(now=1000.0) is None


# -- the real thing: incremental, end to end ----------------------------------

def test_the_second_tick_is_silent_because_the_hashes_match(tick_vault):
    """AC#3's "내용 해시 미변경 문서는 건너뜀", observed rather than mocked."""
    pytest.importorskip("sqlite_vec")
    tick_vault.write(DOC_A, "---\ntitle: 노트 A\n---\n\n사과에 대한 내용\n")
    tick_vault.write(DOC_B, "---\ntitle: 노트 B\n---\n\n바나나에 대한 내용\n")

    first = me.embed_tick()
    second = me.embed_tick()

    assert first is not None and "embedded 2" in first
    assert second is None, "nothing changed, so the tick must say nothing"


def test_only_the_edited_doc_is_re_embedded(tick_vault):
    pytest.importorskip("sqlite_vec")
    tick_vault.write(DOC_A, "---\ntitle: 노트 A\n---\n\n사과\n")
    tick_vault.write(DOC_B, "---\ntitle: 노트 B\n---\n\n바나나\n")
    me.embed_tick()

    tick_vault.write(DOC_A, "---\ntitle: 노트 A\n---\n\n사과가 아니라 포도\n")
    line = me.embed_tick()

    assert line is not None
    assert "embedded 1" in line
    assert "unchanged 1" in line


def test_a_deleted_doc_is_removed_on_the_next_tick(tick_vault):
    pytest.importorskip("sqlite_vec")
    tick_vault.write(DOC_A, "---\ntitle: 노트 A\n---\n\n사과\n")
    tick_vault.write(DOC_B, "---\ntitle: 노트 B\n---\n\n바나나\n")
    me.embed_tick()

    (tick_vault.root / DOC_B).unlink()
    line = me.embed_tick()

    assert line is not None
    assert "removed 1" in line


def test_an_empty_vault_is_silent(tick_vault):
    pytest.importorskip("sqlite_vec")
    assert me.embed_tick() is None


# -- the tick call site -------------------------------------------------------

def _quiet_tick(monkeypatch):
    """Every other maintenance step stubbed to its "nothing happened" answer, so
    only the embed step's own output is left. Returns the worker module and a
    recorder the LAST step before the embed one appends to."""
    from todo_system import worker

    monkeypatch.setattr(worker, "IS_REMOTE_HOST", False)
    for name in ("autoclose_stale_reviews_local", "sweep_remote_completed_runs",
                 "sync_vault_state"):
        monkeypatch.setattr(worker, name, lambda *_a, **_k: 0)
    for name in ("rotate_logs", "check_unapplied_migrations", "pull_engine_checkout",
                 "bump_vault_engine_pin", "sync_wrappers", "pull_browser_checkout",
                 "restart_stale_daemons"):
        monkeypatch.setattr(worker, name, lambda *_a, **_k: [])
    monkeypatch.setattr(worker, "check_dispatch_silence", lambda *_a, **_k: None)
    monkeypatch.setattr(worker.browser_watch, "check_login_watch", lambda *_a, **_k: None)
    earlier: list = []
    monkeypatch.setattr(worker, "check_work_starvation",
                        lambda *_a, **_k: earlier.append("starvation") or None)
    monkeypatch.setattr(worker.maintenance_disk, "check_and_prune",
                        lambda *_a, **_k: earlier.append("disk") or None)
    return worker, earlier


def test_cmd_maintenance_calls_the_step(monkeypatch, capsys):
    worker, _earlier = _quiet_tick(monkeypatch)
    monkeypatch.setattr(worker.maintenance_embed, "embed_tick",
                        lambda *_a, **_k: "embed-tick: embedded 2, removed 0, unchanged 5")

    rc = worker.cmd_maintenance()

    assert rc == 0
    assert "embed-tick: embedded 2" in capsys.readouterr().out


def test_a_silent_step_adds_no_line_to_the_tick(monkeypatch, capsys):
    worker, _earlier = _quiet_tick(monkeypatch)
    monkeypatch.setattr(worker.maintenance_embed, "embed_tick", lambda *_a, **_k: None)

    assert worker.cmd_maintenance() == 0
    assert "embed-tick" not in capsys.readouterr().out


def test_a_step_that_raises_anyway_does_not_stop_the_tick(monkeypatch, capsys):
    """Second belt: embed_tick promises never to raise, but if it ever does the
    tick still runs its other steps and only flags rc."""
    worker, earlier = _quiet_tick(monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("contract violated")

    monkeypatch.setattr(worker.maintenance_embed, "embed_tick", _boom)

    rc = worker.cmd_maintenance()

    assert rc == 1
    assert "embed-tick: error — contract violated" in capsys.readouterr().out
    assert earlier == ["starvation", "disk"], "the steps before it must still have run"


# -- the CLI ------------------------------------------------------------------

def test_the_cli_refuses_anything_but_dry_run(capsys):
    assert me.main([]) == 2
    assert "only --dry-run is supported" in capsys.readouterr().err


def test_dry_run_reports_a_missing_db_without_creating_it(tick_vault, capsys):
    assert not tick_vault.db_path.exists()

    assert me.main(["--dry-run"]) == 0

    assert "does not exist yet" in capsys.readouterr().out
    assert not tick_vault.db_path.exists(), "--dry-run must stay read-only"


def test_dry_run_prints_the_plan_and_writes_nothing(tick_vault, capsys):
    tick_vault.write(DOC_A, "---\ntitle: 노트 A\n---\n\n사과\n")
    tick_vault.connect().close()          # the DB exists, nothing embedded yet

    assert me.main(["--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "embed plan (dry-run)" in out
    assert DOC_A in out
    con = tick_vault.connect()
    try:
        assert not gm._table_exists(con, "vec_sources"), "--dry-run wrote state"
    finally:
        con.close()


def test_the_module_is_importable_without_sqlite_vec():
    """The stdlib-only shape graph_memory is on: no top-level `import sqlite_vec`
    here either — it stays behind graph_memory's lazy seam."""
    import ast
    import sys
    from pathlib import Path

    source = Path(me.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            imported.add(node.module.split(".")[0])

    assert "sqlite_vec" not in imported
    assert imported - set(sys.stdlib_module_names) - {"todo_system"} == set()


def test_the_suite_wide_isolation_fixture_is_live():
    """Negative control for conftest's `_embed_tick_isolated`: without the
    `tick_vault` fixture the switch is OFF and every seam is in a tmp dir, so an
    unisolated cmd_maintenance test cannot embed the real vault."""
    assert os.environ.get(me.KILL_ENV) == "0"
    assert me.embed_tick() is None
    assert "embed-tick" in str(me.state_dir()) or "embed-tick" in str(me.state_path())
    assert not me.vault_root().exists()
