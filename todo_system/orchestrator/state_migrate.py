"""git → external state migration, and the assignments snapshot export.

Leaf ⑤ of docs/design/state-store.md. Two one-way copies between the git-mode
files in the vault and the ``external`` backend's SQLite file
(``state_store_sqlite``):

  * ``import_git_state`` — vault → SQLite, once, when switching a host to
    ``MACP_STATE_BACKEND=external``:

      ``Worker/assignments/*.json``              → ``ledger``
      ``Worker/agents/<agent>/inbox/*.json``     → ``mailbox``
      ``Worker/agents/_state/<agent>/cursor.json`` → ``mailbox_cursor``
      ``Worker/planner_state.json``              → ``planner_kv``

    ``Todo/todo.db`` is not migrated — it is a derived md cache (design §②).
    Re-runnable: a second run over the same vault leaves the DB byte-identical,
    and a late re-run after the switch never rewinds a ledger row the SQLite
    side has newer, never resurrects a message it already acked, and merges
    (never replaces) the cursor tombstones.

  * ``export_assignments`` — SQLite ``ledger`` → ``Worker/assignments/<task_id>.json``
    in exactly the bytes ``Ledger.save`` writes, so the dashboard and a cloud
    takeover keep reading the same files. In external mode SQLite is
    authoritative and the exported files are a read-only snapshot.

The vault is only ever *read* by import: sources are parsed as plain files,
never through ``GitMailbox`` (its ``receive`` pulls, its ``ack`` commits).
``--dry-run`` parses the sources and reports counts without opening — let alone
creating — the DB; that is also why this module never constructs
``ExternalStateStore`` (its ``__init__`` bootstraps the file).

Stdlib-only. No production code imports this module; it runs as
``python -m todo_system.orchestrator.state_migrate {import,export}``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Iterator

from .. import paths
from .assignment import Assignment, Ledger
from .mailbox import DEFAULT_ROOT, FileMailbox
from .message import Envelope, now_iso
from .state_store_sqlite import SqliteLedger, SqliteMailbox, _bootstrap, _connect

__all__ = ["export_assignments", "import_git_state", "main"]

_IMPORT_KEYS = (
    "ledger", "ledger_unchanged", "ledger_kept_newer", "ledger_skipped",
    "cursor_agents",
    "mailbox", "mailbox_rewritten", "mailbox_skipped_acked", "mailbox_skipped",
    "planner", "planner_unchanged", "planner_skipped",
)
_EXPORT_KEYS = ("total", "written", "unchanged", "skipped")

# A JSON list → AttributeError (.items()), a missing required field → TypeError,
# a broken file → JSONDecodeError (⊂ ValueError).
_BAD_ROW = (OSError, ValueError, TypeError, AttributeError, KeyError)


# ---------------------------------------------------------------------------
# Source readers (vault, read-only)
# ---------------------------------------------------------------------------


def _parse_ts(s: object) -> dt.datetime | None:
    if not isinstance(s, str):
        return None
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo is not None else None   # naive ↔ aware can't be compared


def _newer(a: str, b: str) -> bool:
    """True only when both timestamps parse and ``a`` is strictly later."""
    ta, tb = _parse_ts(a), _parse_ts(b)
    return ta is not None and tb is not None and ta > tb


def _mtime_iso(f: Path) -> str:
    """The file's mtime as local ISO-8601 — a stand-in for a missing timestamp
    that, unlike ``now_iso()``, is the same on every run."""
    return (dt.datetime.fromtimestamp(f.stat().st_mtime).astimezone()
            .replace(microsecond=0).isoformat())


def _read_assignments(root: Path) -> tuple[list[Assignment], int]:
    """Every parseable row, filename-sorted like ``Ledger.all``; the rest counted.

    A row from an older build without ``created_ts`` / ``updated_ts`` gets the
    file's mtime instead of the dataclass's ``now_iso()`` default, so a rerun
    reads the same row and import stays idempotent.
    """
    if not root.is_dir():
        return [], 0
    rows: list[Assignment] = []
    skipped = 0
    for f in sorted(root.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key in ("created_ts", "updated_ts"):
                    if key not in data:
                        data[key] = _mtime_iso(f)
            rows.append(Assignment.from_json(json.dumps(data)))
        except _BAD_ROW:
            skipped += 1
    return rows, skipped


def _iter_inbox(mail_root: Path) -> Iterator[tuple[str, Path]]:
    """``(agent_id, file)`` for every inbox message, agent- then file-sorted."""
    if not mail_root.is_dir():
        return
    for agent_dir in sorted(mail_root.iterdir()):
        if not agent_dir.is_dir() or agent_dir.name == "_state":
            continue
        inbox = agent_dir / "inbox"
        if not inbox.is_dir():
            continue
        for f in sorted(inbox.glob("*.json")):   # .gitkeep is not matched
            yield agent_dir.name, f


def _load_envelope(agent_id: str, f: Path) -> tuple[Envelope | None, bool]:
    """``(envelope, rewritten)`` with ``msg_id`` pinned to the file stem and
    ``to`` to the inbox it sits in.

    Git-mode delivery is by directory and the runtime filters by stem, so both
    pins only restate what the file already meant. Without the msg_id pin a file
    lacking one would get a fresh random id per run (``Envelope.from_dict``) and
    add a duplicate row every time; a missing ``ts`` takes the file's mtime for
    the same reason.
    """
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, False
    if not isinstance(d, dict):
        return None, False
    rewritten = d.get("msg_id") != f.stem or d.get("to") != agent_id
    d["msg_id"] = f.stem
    d["to"] = agent_id
    if not d.get("ts"):
        try:
            d["ts"] = _mtime_iso(f)
        except OSError:
            return None, False
    try:
        return Envelope.from_dict(d), rewritten
    except (ValueError, KeyError, TypeError):
        return None, False


def _cursor_agents(mail_root: Path) -> list[str]:
    state = mail_root / "_state"
    if not state.is_dir():
        return []
    return sorted(p.name for p in state.iterdir()
                  if p.is_dir() and (p / "cursor.json").is_file())


def _merge_cursor(db: tuple[set[str], dict[str, float]],
                  git: tuple[set[str], dict[str, float]]
                  ) -> tuple[set[str], dict[str, float]]:
    """Union of both cursors: tombstones keep the later ack time, and an id that
    is acked on either side is no longer ``seen`` (``(agent_id, msg_id)`` is the
    ``mailbox_cursor`` primary key, so the two sets must stay disjoint)."""
    acked = dict(db[1])
    for k, v in git[1].items():
        acked[k] = max(acked.get(k, v), v)
    seen = (db[0] | git[0]) - acked.keys()
    return seen, acked


def _load_planner(path: Path) -> tuple[dict, int]:
    """The planner state dict and whether an existing file was unusable."""
    if not path.exists():
        return {}, 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, 1
    return (data, 0) if isinstance(data, dict) else ({}, 1)


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def import_git_state(vault_root: str | Path, sqlite_path: str | Path, *,
                     dry_run: bool = False,
                     planner_state_path: str | Path | None = None) -> dict[str, int]:
    """Copy the vault's git-mode state into the external SQLite file.

    Returns per-area counts (every key always present). With ``dry_run`` only
    the vault sources are parsed and the counts are what a run on a fresh DB
    would write; the DB file and its directory are not touched.
    """
    vault = Path(vault_root)
    db = Path(sqlite_path)
    mail_root = vault / DEFAULT_ROOT
    planner_path = (Path(planner_state_path) if planner_state_path is not None
                    else vault / "Worker" / "planner_state.json")
    counts = dict.fromkeys(_IMPORT_KEYS, 0)

    rows, counts["ledger_skipped"] = _read_assignments(vault / "Worker" / "assignments")
    # FileMailbox runs no git; only its cursor reader is used (same tolerance as
    # the runtime: a broken file reads empty, a non-numeric ack time is dropped).
    git_cursor = FileMailbox(mail_root)._load_cursor
    agents = _cursor_agents(mail_root)
    counts["cursor_agents"] = len(agents)
    planner, counts["planner_skipped"] = _load_planner(planner_path)

    if dry_run:
        counts["ledger"] = len(rows)
        for agent_id, f in _iter_inbox(mail_root):
            env, rewritten = _load_envelope(agent_id, f)
            if env is None:
                counts["mailbox_skipped"] += 1
                continue
            counts["mailbox_rewritten"] += rewritten
            if env.msg_id in git_cursor(agent_id)[1]:
                counts["mailbox_skipped_acked"] += 1
            else:
                counts["mailbox"] += 1
        counts["planner"] = len(planner)
        return counts

    _bootstrap(db)
    ledger = SqliteLedger(db)
    mailbox = SqliteMailbox(db)

    for a in rows:
        existing = ledger.load(a.task_id)
        if existing is not None and existing.to_json() == a.to_json():
            counts["ledger_unchanged"] += 1
        elif existing is not None and _newer(existing.updated_ts, a.updated_ts):
            counts["ledger_kept_newer"] += 1
        else:
            ledger.save(a)
            counts["ledger"] += 1

    # Cursors before messages, so the acked filter below sees merged tombstones.
    # Tombstones are not pruned here — the next runtime ack() prunes them.
    acked_by_agent: dict[str, dict[str, float]] = {}
    for agent_id in agents:
        before = mailbox._load_cursor(agent_id)
        merged = _merge_cursor(before, git_cursor(agent_id))
        if merged != before:
            mailbox._save_cursor(agent_id, *merged)
        acked_by_agent[agent_id] = merged[1]

    for agent_id, f in _iter_inbox(mail_root):
        env, rewritten = _load_envelope(agent_id, f)
        if env is None:
            counts["mailbox_skipped"] += 1
            continue
        counts["mailbox_rewritten"] += rewritten
        acked = acked_by_agent.get(agent_id)
        if acked is None:   # an inbox with no git cursor may still have a DB one
            acked = acked_by_agent[agent_id] = mailbox._load_cursor(agent_id)[1]
        if env.msg_id in acked:
            # The external side already consumed it; re-inserting would
            # resurrect it.
            counts["mailbox_skipped_acked"] += 1
        else:
            mailbox.send(env)   # INSERT OR REPLACE — same row on every run
            counts["mailbox"] += 1

    if planner:
        con = _connect(db)
        try:
            for key in sorted(planner):
                # Not SqlitePlannerKV.put: it stamps now_iso() on every call, so a
                # rerun would change updated_ts. An identical value is left alone;
                # a different one wins (planner state is a throttle cache with no
                # per-key timestamp in the source).
                cur = con.execute(
                    "INSERT INTO planner_kv(key, value_json, updated_ts) VALUES (?, ?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET"
                    "   value_json=excluded.value_json, updated_ts=excluded.updated_ts"
                    " WHERE planner_kv.value_json IS NOT excluded.value_json",
                    (key, json.dumps(planner[key], ensure_ascii=False), now_iso()),
                )
                counts["planner" if cur.rowcount else "planner_unchanged"] += 1
            con.commit()
        finally:
            con.close()

    return counts


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


def export_assignments(sqlite_path: str | Path, vault_root: str | Path, *,
                       dry_run: bool = False) -> dict[str, int]:
    """Dump every SQLite ledger row to ``<vault>/Worker/assignments/<task_id>.json``.

    Same bytes as ``Ledger.save`` (atomic). A file whose text already matches is
    not rewritten, so a no-change export leaves git and mtimes alone; a file with
    no SQLite row is left as is (ledger rows are never deleted in normal flow).
    Raises ``FileNotFoundError`` when the DB does not exist — export never
    bootstraps an empty one.
    """
    db = Path(sqlite_path)
    if not db.is_file():
        raise FileNotFoundError(f"state db not found: {db}")
    con = _connect(db)
    try:
        rows = con.execute("SELECT task_id, data_json FROM ledger ORDER BY task_id").fetchall()
    finally:
        con.close()

    target = Ledger(Path(vault_root) / "Worker" / "assignments")
    counts = dict.fromkeys(_EXPORT_KEYS, 0)
    counts["total"] = len(rows)
    for _task_id, data_json in rows:
        try:
            a = Assignment.from_json(data_json)
        except _BAD_ROW:
            counts["skipped"] += 1
            continue
        p = target._path(a.task_id)
        text = a.to_json()
        try:
            if p.is_file() and p.read_text(encoding="utf-8") == text:
                counts["unchanged"] += 1
                continue
        except (OSError, ValueError):
            pass   # unreadable → rewrite it
        if not dry_run:
            target.save(a)
        counts["written"] += 1
    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_counts(header: str, counts: dict[str, int]) -> None:
    print(header)
    for k, v in counts.items():
        print(f"  {k}: {v}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m todo_system.orchestrator.state_migrate")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("import", "copy the vault's git-mode state into the SQLite db"),
                        ("export", "dump the SQLite ledger to Worker/assignments/")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--vault-root", type=Path, help="default: the resolved vault, else cwd")
        sp.add_argument("--sqlite-path", type=Path,
                        help="default: MACP_STATE_SQLITE_PATH / config / ~/.local/share/macp/state.sqlite")
        sp.add_argument("--dry-run", action="store_true", help="report counts, write nothing")
        if name == "import":
            sp.add_argument("--planner-state", type=Path,
                            help="default: <vault>/Worker/planner_state.json")
    args = p.parse_args(argv)

    vault = args.vault_root if args.vault_root is not None else paths.vault_root_or_cwd()
    explicit_db = args.sqlite_path is not None
    db = args.sqlite_path if explicit_db else paths.state_sqlite_path(start=vault)
    dry = " (dry-run)" if args.dry_run else ""

    if args.cmd == "import":
        counts = import_git_state(vault, db, dry_run=args.dry_run,
                                  planner_state_path=args.planner_state)
        _print_counts(f"import{dry}: {vault} -> {db}", counts)
        return 0

    if not explicit_db:
        try:
            backend = paths.state_backend(start=vault)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        if backend == "git":
            print("state backend is git: export is a no-op (the files are authoritative)")
            return 0
    try:
        counts = export_assignments(db, vault, dry_run=args.dry_run)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    _print_counts(f"export{dry}: {db} -> {vault / 'Worker' / 'assignments'}", counts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
