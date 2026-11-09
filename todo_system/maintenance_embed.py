"""Maintenance-tick vault embedding — keep the vectors from going stale.

``graph_memory.embed_vault`` has been incremental since memory v2 · G2 (a
content-hash diff in ``plan_embedding``), but nothing ever *called* it on a
schedule: the only runs it ever had were a human typing ``graph_memory embed``
(once, 2026-11-09). So the semantic half of ``query_related`` was answering from
whatever the corpus looked like at that one moment, and every note written since
was invisible to it — silently, because the query degrades to BFS-only instead
of failing.

``embed_tick`` is a step of the 5-minute maintenance tick
(``worker.cmd_maintenance``):

* nothing to do (every doc's content hash unchanged): **no output at all** —
  정상이면 무음, like the disk prune, so the tick's log stays readable;
* docs changed, were added or went away: exactly one ``embed-tick:`` line
  naming how many were embedded, removed and left unchanged;
* sqlite-vec not installed (the shipped wheel's state): one line, **throttled to
  once per SKIP_NOTICE_COOLDOWN_S**, and the corpus is not walked at all. The
  throttle is the whole reason this module has state: without it a host with no
  extension would print 288 identical lines a day, and the cheap probe is what
  keeps it from hashing ~4,000 docs every five minutes to reach the same answer.

No new incremental logic lives here. ``plan_embedding``'s content-hash diff
already is the "skip unchanged docs" behaviour; this module's whole job is to
call it on a schedule and to turn every failure into one line.

Fail-open by contract: the step never raises into the tick. A broken DB, an
unreadable vault or an embedder error becomes an ``embed-tick: error (…)`` line
and the rest of the tick runs. ``worker.cmd_maintenance`` wraps the call in its
own try/except as a second belt (the ``maintenance_disk`` shape).

Unlike the related-records reader (``orchestrator/related_context.py``), this
step opens the DB **read-write** — embedding IS a write, and it is the one place
allowed to create/migrate ``Memory/relations.sqlite``. It writes only inside
that file, which the vault gitignores, so there is no vault state to roll back.
``--dry-run`` stays genuinely read-only: it opens ``mode=ro`` and reports
instead when the DB does not exist yet.

Environment (read at call time):

  MACP_EMBED_TICK            "0" switches the step off entirely.
  MACP_EMBED_STATE_DIR       state dir; default ~/.local/share/macp/embed.

Stdlib plus stdlib-only siblings — no top-level ``import sqlite_vec`` (it stays
behind ``graph_memory._load_sqlite_vec``'s lazy seam). For a read-only look at
what the next tick would embed:

    python -m todo_system.maintenance_embed --dry-run
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from . import graph_memory as gm

KILL_ENV = "MACP_EMBED_TICK"
STATE_DIR_ENV = "MACP_EMBED_STATE_DIR"

SKIP_NOTICE_COOLDOWN_S = 24 * 3600   # check_work_starvation's cadence
TEXT_LIMIT = 300                     # chars of an error in the one line

# Seams, all read at CALL time — never frozen into a default argument — so the
# suite-wide fixture in todo_system/conftest.py can point every one at a tmp dir.
VAULT_ROOT: Path | None = None   # None → graph_memory.ROOT (attribute, not a copy)
DB_PATH: Path | None = None      # None → graph_memory.DB_PATH
STATE_DIR: Path | None = None    # None → $MACP_EMBED_STATE_DIR, else the default
_now = time.time


# -- config -------------------------------------------------------------------

def enabled() -> bool:
    """Only an explicit "0" disables the step (``maintenance_disk.enabled``'s rule)."""
    return os.environ.get(KILL_ENV, "").strip() != "0"


def vault_root() -> Path:
    """``gm.ROOT`` read as an ATTRIBUTE, so ``monkeypatch.setattr(gm, "ROOT", …)``
    (the ``isolated_vault`` fixture's seam) keeps its meaning here."""
    return Path(VAULT_ROOT) if VAULT_ROOT is not None else Path(gm.ROOT)


def db_path() -> Path:
    if DB_PATH is not None:
        return Path(DB_PATH)
    if VAULT_ROOT is not None:
        return Path(VAULT_ROOT) / "Memory" / Path(gm.DB_PATH).name
    return Path(gm.DB_PATH)


def state_dir() -> Path:
    if STATE_DIR is not None:
        return Path(STATE_DIR)
    env = os.environ.get(STATE_DIR_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "share" / "macp" / "embed"


def state_path() -> Path:
    return state_dir() / "embed_state.json"


# -- state (one stamp: the throttled skip notice) ------------------------------

def _read_state() -> dict:
    state = {"last_skip_notice_ts": 0.0}
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if isinstance(data, dict):
        for key in state:
            try:
                state[key] = float(data.get(key, 0.0))
            except (TypeError, ValueError):
                pass
    return state


def _write_state(state: dict) -> str | None:
    """None on success, else a short reason (the line carries it).
    ``maintenance_disk._write_state``'s atomic-replace + 0600 shape."""
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        p = state_path()
        tmp = p.with_name(p.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"last_skip_notice_ts": state["last_skip_notice_ts"]}, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except OSError as e:
        return f"state write failed ({type(e).__name__})"
    return None


def _cooling(now: float, last: float, window: float) -> bool:
    """Whether the previous notice is recent enough to suppress this one.

    ``last <= 0`` means "never notified" and never suppresses — spelt out rather
    than left to ``now - 0.0`` exceeding the window, which only holds because
    real clocks return ~1.8e9 (``maintenance_disk._cooling`` leans on that, and
    under an injected clock its first notice is swallowed). A stamp in the future
    (the clock stepped back) does not suppress either."""
    if last <= 0:
        return False
    return 0 <= now - last < window


def _skip_notice(reason: str, now: float) -> str | None:
    """The throttled "no sqlite-vec" line, or None while still cooling."""
    state = _read_state()
    if _cooling(now, state["last_skip_notice_ts"], SKIP_NOTICE_COOLDOWN_S):
        return None
    state["last_skip_notice_ts"] = now
    # Stamped BEFORE returning, so a caller that dies mid-print cannot turn the
    # notice into one line per tick (maintenance_disk's rule).
    err = _write_state(state)
    line = f"embed-tick: sqlite-vec 없음 — 건너뜀 ({reason})"[:TEXT_LIMIT]
    return f"{line}; {err}" if err else line


# -- the step -----------------------------------------------------------------

def embed_tick(*, now: float | None = None) -> str | None:
    """The maintenance step. Returns the line to print, or None (disabled,
    nothing changed, or a cooling skip notice). Never raises."""
    con = None
    try:
        if not enabled():
            return None
        t = _now() if now is None else now
        con = gm.init_relations_db(db_path())
        # Cheap guard FIRST. embed_vault walks and hashes the whole corpus
        # (~4,000 docs) before it discovers the extension is missing, and on a
        # host without sqlite-vec that work is thrown away every five minutes.
        try:
            gm._load_sqlite_vec(con)
        except gm.EmbeddingsUnavailable as exc:
            return _skip_notice(str(exc), t)
        # Incremental by construction: plan_embedding's content-hash diff IS the
        # "skip unchanged docs" behaviour, so nothing is reimplemented here.
        # embed_vault prints its own success line; swallow it so the tick gets
        # exactly one line (ours) rather than two.
        with contextlib.redirect_stdout(io.StringIO()):
            res = gm.embed_vault(con, vault_root=vault_root())
        if res.skipped_reason:
            return _skip_notice(res.skipped_reason, t)
        if res.embedded == 0 and res.removed == 0:
            return None                  # unchanged: silent, like the disk prune
        return (f"embed-tick: embedded {res.embedded}, removed {res.removed}, "
                f"unchanged {res.unchanged} of {res.total_corpus} corpus docs "
                f"(dim={res.dim})")
    except Exception as e:               # noqa: BLE001 - must never break maintenance
        return f"embed-tick: error ({type(e).__name__}: {e})"[:TEXT_LIMIT] + " — skipped"
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:            # noqa: BLE001 - closing is best-effort
                pass


# -- entry point ---------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """``--dry-run``: print the plan the next tick would apply. Read-only — a
    ro connection, and no DB is created where none exists."""
    ap = argparse.ArgumentParser(
        prog="python -m todo_system.maintenance_embed",
        description="Show what the maintenance vault embedding would do. Read-only.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the incremental embed plan; write nothing")
    args = ap.parse_args(argv)
    if not args.dry_run:
        print("only --dry-run is supported: the embed itself runs inside the maintenance "
              "tick (worker.cmd_maintenance)", file=sys.stderr)
        return 2
    root, db = vault_root(), db_path()
    print(f"vault root: {root}")
    print(f"edge db:    {db}")
    print(f"kill switch {KILL_ENV}: "
          + ("off (embed enabled)" if enabled() else "ON — the embed is disabled"))
    if not db.is_file():
        print("edge db does not exist yet — the first tick creates it and embeds "
              "the whole corpus")
        return 0
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        try:
            gm._load_sqlite_vec(con)
            print("sqlite-vec: loaded — a tick would embed the plan below")
        except gm.EmbeddingsUnavailable as exc:
            print(f"sqlite-vec: UNAVAILABLE ({exc}) — a tick would print one "
                  "throttled skip line and embed nothing")
        to_embed, unchanged, to_remove = gm.plan_embedding(
            con, gm.iter_embed_corpus(root))
        gm._print_embed_plan(to_embed, unchanged, to_remove, dim=gm.EMBED_DIM)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
