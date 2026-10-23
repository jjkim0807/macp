"""state_migrate — git→external one-off import + assignments snapshot export.

Leaf ⑤ of docs/design/state-store.md. The git-mode invariant (the default
backend stays git and nothing in production imports the migrator) is pinned
first; the import/export behaviour follows.

Every DB here is an explicit tmp ``sqlite_path`` — never the default
``paths.state_sqlite_path()``, which would write a real host file outside the
vault.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import todo_system
from todo_system.orchestrator import message as m
from todo_system.orchestrator import state_migrate
from todo_system.orchestrator.assignment import DONE, IN_PROGRESS, Assignment, Ledger
from todo_system.orchestrator.message import Envelope
from todo_system.orchestrator.state_migrate import export_assignments, import_git_state
from todo_system.orchestrator.state_store import GitStateStore, open_state_store
from todo_system.orchestrator.state_store_sqlite import (
    SqliteLedger,
    SqliteMailbox,
    SqlitePlannerKV,
)

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

AGENT = m.ORCHESTRATOR
MSG_IDS = ["01M5AAAAAAAAAAAAAAAAAAAAA1", "01M5AAAAAAAAAAAAAAAAAAAAA2",
           "01M5AAAAAAAAAAAAAAAAAAAAA3"]
PLANNER = {"todo_state_hash": "abc123", "last_run": "2026-10-23T09:00:00+09:00",
           "verdict": {"created": 2, "note": "큐 비어 있음"}}


def _assignments() -> list[Assignment]:
    return [
        Assignment(task_id="t-a", agent_id="ai-coder", corr_id="c-a", state=IN_PROGRESS,
                   kind="ai-claude", attempts=2, refail_count=1,
                   created_ts="2026-10-20T10:00:00+09:00",
                   updated_ts="2026-10-21T10:00:00+09:00",
                   last_note="검증 대기 중",
                   history=[{"ts": "2026-10-21T10:00:00+09:00", "type": "PROGRESS",
                             "from": "ai-coder"}],
                   payload={"repo": "https://example.invalid/r.git", "n": 3}),
        Assignment(task_id="t-b", agent_id="ai-coder", corr_id="c-b", state=DONE,
                   created_ts="2026-10-19T10:00:00+09:00",
                   updated_ts="2026-10-19T12:00:00+09:00",
                   job_running=False, checkout_busy=True, close_fingerprint="f00d"),
        Assignment(task_id="t-c", agent_id="human", corr_id="c-c", state="awaiting_human",
                   created_ts="2026-10-22T08:00:00+09:00",
                   updated_ts="2026-10-22T08:30:00+09:00",
                   blocked_on="upstream PR"),
    ]


@pytest.fixture
def vault(tmp_path) -> Path:
    v = tmp_path / "vault"
    led = Ledger(v / "Worker" / "assignments")
    for a in _assignments():
        led.save(a)
    inbox = v / "Worker" / "agents" / AGENT / "inbox"
    inbox.mkdir(parents=True)
    (inbox / ".gitkeep").touch()
    for i, mid in enumerate(MSG_IDS):
        env = Envelope(to=AGENT, type=m.RESULT, frm="ai-coder", task_id=f"t-{'abc'[i]}",
                       msg_id=mid, ts=f"2026-10-23T0{i}:00:00+09:00",
                       payload={"summary": f"끝 {i}"})
        (inbox / f"{mid}.json").write_text(env.to_json(), encoding="utf-8")
    cursor = v / "Worker" / "agents" / "_state" / AGENT / "cursor.json"
    cursor.parent.mkdir(parents=True)
    cursor.write_text(json.dumps({"seen": [MSG_IDS[0]], "acked": {}}), encoding="utf-8")
    (v / "Worker" / "planner_state.json").write_text(
        json.dumps(PLANNER, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return v


@pytest.fixture
def db(tmp_path) -> Path:
    return tmp_path / "state" / "state.sqlite"


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


def _dump(db: Path) -> dict[str, list[tuple]]:
    con = sqlite3.connect(str(db))
    try:
        out = {}
        for table in ("meta", "ledger", "mailbox", "mailbox_cursor", "planner_kv"):
            cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
            out[table] = con.execute(
                f"SELECT * FROM {table} ORDER BY {', '.join(cols)}").fetchall()
        return out
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_import_then_export_roundtrip_assignments(vault, db, tmp_path):
    import_git_state(vault, db)
    fresh = tmp_path / "fresh"
    counts = export_assignments(db, fresh)
    assert counts == {"total": 3, "written": 3, "unchanged": 0, "skipped": 0}

    src = vault / "Worker" / "assignments"
    out = fresh / "Worker" / "assignments"
    assert sorted(p.name for p in out.glob("*.json")) == sorted(p.name for p in src.glob("*.json"))
    for f in src.glob("*.json"):
        exported = (out / f.name).read_text(encoding="utf-8")
        assert json.loads(exported) == json.loads(f.read_text(encoding="utf-8"))
        assert exported.encode("utf-8") == f.read_bytes()   # same bytes as Ledger.save

    # Exporting back over the original vault changes nothing — not even mtimes.
    before = _snapshot(vault)
    counts = export_assignments(db, vault)
    assert counts == {"total": 3, "written": 0, "unchanged": 3, "skipped": 0}
    assert _snapshot(vault) == before


def test_roundtrip_legacy_row_keeps_known_keys(tmp_path, db):
    v = tmp_path / "vault"
    src = v / "Worker" / "assignments"
    src.mkdir(parents=True)
    legacy = {"task_id": "legacy", "agent_id": "ai-coder", "corr_id": "c-l",
              "state": DONE,
              "history": [{"ts": "2026-01-01T00:00:00+09:00", "type": "RESULT"}],
              "future_field": {"x": 1}}
    (src / "legacy.json").write_text(json.dumps(legacy), encoding="utf-8")

    first = import_git_state(v, db)
    assert first["ledger"] == 1
    out = tmp_path / "out"
    export_assignments(db, out)
    got = json.loads((out / "Worker" / "assignments" / "legacy.json").read_text(encoding="utf-8"))
    for k, val in legacy.items():
        if k != "future_field":
            assert got[k] == val
    assert "future_field" not in got   # documented Assignment.from_json drop
    assert got["refail_count"] == 0     # missing optional field -> default

    # Missing timestamps are pinned to the file's mtime, so a rerun is a no-op.
    dump = _dump(db)
    second = import_git_state(v, db)
    assert second["ledger"] == 0 and second["ledger_unchanged"] == 1
    assert _dump(db) == dump


def test_broken_assignment_files_are_skipped(vault, db):
    src = vault / "Worker" / "assignments"
    (src / "broken.json").write_text("{not json", encoding="utf-8")
    (src / "list.json").write_text("[1, 2]", encoding="utf-8")
    (src / "partial.json").write_text(json.dumps({"task_id": "p"}), encoding="utf-8")
    counts = import_git_state(vault, db)
    assert counts["ledger"] == 3
    assert counts["ledger_skipped"] == 3
    assert [a.task_id for a in SqliteLedger(db).all()] == ["t-a", "t-b", "t-c"]


# ---------------------------------------------------------------------------
# Idempotence + source read-only
# ---------------------------------------------------------------------------


def test_import_twice_is_idempotent(vault, db):
    first = import_git_state(vault, db)
    assert first["ledger"] == 3
    assert first["mailbox"] == 3
    assert first["cursor_agents"] == 1
    assert first["planner"] == 3
    dump = _dump(db)

    second = import_git_state(vault, db)
    assert _dump(db) == dump   # incl. planner_kv.updated_ts
    assert second["ledger"] == 0
    assert second["ledger_unchanged"] == 3
    assert second["planner"] == 0
    assert second["planner_unchanged"] == 3
    assert second["mailbox"] == 3   # INSERT OR REPLACE of the same row


def test_import_does_not_modify_source_vault(vault, db):
    before = _snapshot(vault)
    import_git_state(vault, db)
    import_git_state(vault, db)
    assert _snapshot(vault) == before
    assert (vault / "Worker" / "agents" / AGENT / "inbox" / ".gitkeep").exists()


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(vault, db, tmp_path):
    before = _snapshot(vault)
    dry = import_git_state(vault, db, dry_run=True)
    assert not db.exists()
    assert not db.parent.exists()
    assert _snapshot(vault) == before

    # The counts are what a real run on a fresh DB writes.
    real = import_git_state(vault, tmp_path / "real.sqlite")
    assert dry == real

    # Against an existing DB: nothing in its directory changes either.
    import_git_state(vault, db)
    db_before = _snapshot(db.parent)
    import_git_state(vault, db, dry_run=True)
    assert _snapshot(db.parent) == db_before

    # Export dry-run reports what it would write and writes nothing.
    fresh = tmp_path / "fresh"
    counts = export_assignments(db, fresh, dry_run=True)
    assert counts["written"] == 3
    assert not fresh.exists()
    assert _snapshot(vault) == before


# ---------------------------------------------------------------------------
# Mailbox specifics
# ---------------------------------------------------------------------------


def test_mailbox_msg_id_pinned_to_stem_and_cursor_respected(vault, db):
    inbox = vault / "Worker" / "agents" / AGENT / "inbox"
    no_id = {"to": AGENT, "type": m.PROGRESS, "from": "ai-coder", "task_id": "t-a",
             "payload": {}}
    (inbox / "01M5AAAAAAAAAAAAAAAAAAAAB1.json").write_text(json.dumps(no_id), encoding="utf-8")
    wrong = Envelope(to=AGENT, type=m.FAIL, frm="ai-coder", msg_id="SOMETHING-ELSE",
                     ts="2026-10-23T05:00:00+09:00")
    (inbox / "01M5AAAAAAAAAAAAAAAAAAAAB2.json").write_text(wrong.to_json(), encoding="utf-8")
    (inbox / "01M5AAAAAAAAAAAAAAAAAAAAB3.json").write_text("{half", encoding="utf-8")

    counts = import_git_state(vault, db)
    assert counts["mailbox"] == 5
    assert counts["mailbox_rewritten"] == 2
    assert counts["mailbox_skipped"] == 1

    con = sqlite3.connect(str(db))
    try:
        ids = [r[0] for r in con.execute("SELECT msg_id FROM mailbox ORDER BY msg_id")]
    finally:
        con.close()
    assert ids == sorted(MSG_IDS + ["01M5AAAAAAAAAAAAAAAAAAAAB1", "01M5AAAAAAAAAAAAAAAAAAAAB2"])

    dump = _dump(db)
    import_git_state(vault, db)
    assert _dump(db) == dump   # no duplicate rows, no fresh ids or ts

    got = [e.msg_id for e in SqliteMailbox(db).receive(AGENT)]
    assert MSG_IDS[0] not in got   # the git cursor's `seen` came along
    assert got == sorted(MSG_IDS[1:] + ["01M5AAAAAAAAAAAAAAAAAAAAB1",
                                        "01M5AAAAAAAAAAAAAAAAAAAAB2"])


def test_reimport_after_switch_does_not_rewind_or_resurrect(vault, db):
    import_git_state(vault, db)

    # Life on the external backend: a message is consumed, a row moves on.
    mb = SqliteMailbox(db)
    consumed = mb.receive(AGENT)[0]
    mb.ack(AGENT, consumed)
    ledger = SqliteLedger(db)
    a = ledger.load("t-a")
    a.state = DONE
    a.updated_ts = "2026-10-21T11:00:00+09:00"   # +1h over the git row
    ledger.save(a)

    counts = import_git_state(vault, db)
    assert counts["ledger_kept_newer"] == 1
    assert counts["mailbox_skipped_acked"] >= 1
    assert ledger.load("t-a").state == DONE
    assert consumed.msg_id not in [e.msg_id for e in mb.receive(AGENT)]
    _, acked = mb._load_cursor(AGENT)
    assert consumed.msg_id in acked   # tombstone survived the merge


def test_cursor_merge_keeps_both_sides(vault, db):
    import_git_state(vault, db)
    mb = SqliteMailbox(db)
    mb._save_cursor(AGENT, {"DB-SEEN"}, {"DB-ACKED": 100.0, MSG_IDS[2]: 50.0})
    cursor = vault / "Worker" / "agents" / "_state" / AGENT / "cursor.json"
    cursor.write_text(json.dumps({"seen": [MSG_IDS[0], MSG_IDS[2]],
                                  "acked": {MSG_IDS[2]: 70.0, "GIT-ACKED": 10.0}}),
                      encoding="utf-8")
    import_git_state(vault, db)
    seen, acked = mb._load_cursor(AGENT)
    assert seen == {"DB-SEEN", MSG_IDS[0]}
    assert acked == {"DB-ACKED": 100.0, MSG_IDS[2]: 70.0, "GIT-ACKED": 10.0}


# ---------------------------------------------------------------------------
# Planner state
# ---------------------------------------------------------------------------


def test_planner_state_flattened_and_override(vault, db, tmp_path):
    import_git_state(vault, db)
    assert SqlitePlannerKV(db).all() == PLANNER

    alt = tmp_path / "remote" / "planner_state.json"
    alt.parent.mkdir()
    alt.write_text(json.dumps({"todo_state_hash": "zzz"}), encoding="utf-8")
    other = tmp_path / "other.sqlite"
    counts = import_git_state(vault, other, planner_state_path=alt)
    assert counts["planner"] == 1
    assert SqlitePlannerKV(other).all() == {"todo_state_hash": "zzz"}

    # A changed value in the source wins; the untouched keys stay put.
    counts = import_git_state(vault, db, planner_state_path=alt)
    assert counts["planner"] == 1 and counts["planner_unchanged"] == 0
    assert SqlitePlannerKV(db).get("todo_state_hash") == "zzz"
    assert SqlitePlannerKV(db).get("verdict") == PLANNER["verdict"]


def test_planner_state_missing_or_broken(vault, db, tmp_path):
    counts = import_git_state(vault, db, planner_state_path=tmp_path / "absent.json")
    assert counts["planner"] == 0 and counts["planner_skipped"] == 0
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    counts = import_git_state(vault, db, planner_state_path=bad)
    assert counts["planner"] == 0 and counts["planner_skipped"] == 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_import_and_export(vault, db, capsys):
    rc = state_migrate.main(["import", "--vault-root", str(vault), "--sqlite-path", str(db)])
    assert rc == 0
    assert "  ledger: 3" in capsys.readouterr().out
    rc = state_migrate.main(["export", "--vault-root", str(vault), "--sqlite-path", str(db)])
    assert rc == 0
    assert "  unchanged: 3" in capsys.readouterr().out


def test_cli_import_dry_run(vault, db, capsys):
    rc = state_migrate.main(["import", "--dry-run", "--vault-root", str(vault),
                             "--sqlite-path", str(db)])
    assert rc == 0
    assert "import (dry-run)" in capsys.readouterr().out
    assert not db.parent.exists()


def test_cli_export_git_mode_is_noop(vault, db, monkeypatch, capsys, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MACP_STATE_BACKEND", "git")
    # So a fall-through could never reach the host's default db.
    monkeypatch.setenv("MACP_STATE_SQLITE_PATH", str(db))
    before = _snapshot(vault)
    rc = state_migrate.main(["export", "--vault-root", str(vault)])
    assert rc == 0
    assert "no-op" in capsys.readouterr().out
    assert not db.exists()
    assert _snapshot(vault) == before


def test_cli_export_missing_db(vault, tmp_path, capsys):
    missing = tmp_path / "nope" / "state.sqlite"
    rc = state_migrate.main(["export", "--vault-root", str(vault),
                             "--sqlite-path", str(missing)])
    assert rc == 2
    assert "not found" in capsys.readouterr().err
    assert not missing.exists()
    assert not missing.parent.exists()
