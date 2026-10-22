"""StateStore: one entry point for the orchestrator's persistent state.

Leaf ② of docs/design/state-store.md. The four state areas — assignment ledger,
mailbox, planner kv, todo cache — are exposed as sub-surfaces of a single
`StateStore` (the §② composition decision: `mailbox()` hands back a `Mailbox`,
it does not flatten send/receive/ack into the store).

`GitStateStore` is the git mode and the default backend. It wraps the current
objects unchanged — `Ledger`, `open_mailbox("git://Worker/agents")`, the
planner_state.json that worker.load/save_planner_state use, and the same
todo.db counts as metrics._db_counts — so behaviour is identical. No call site
goes through it yet; routing them is leaf ⑥.

todo.db is a derived cache of the md files (not a source of truth), so the
store only delegates reads to it; it never writes or owns the file.

Stdlib-only. `todo_system.worker` is imported lazily (planner default path
only): importing it at module top would pin its vault ROOT as a side effect.
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .. import paths
from .assignment import Ledger
from .atomic_io import atomic_write_text
from .mailbox import DEFAULT_ROOT, Mailbox, open_mailbox

__all__ = [
    "GitPlannerKV",
    "GitStateStore",
    "GitTodoCacheReader",
    "PlannerKV",
    "StateStore",
    "TodoCacheReader",
    "open_state_store",
]


# ---------------------------------------------------------------------------
# Sub-surface ABCs
# ---------------------------------------------------------------------------


class PlannerKV(ABC):
    """Planner state as a key/value surface.

    Mirrors worker.load_planner_state / save_planner_state: `all()` is the
    whole state dict, `put()` replaces one key and keeps the others.
    """

    @abstractmethod
    def get(self, key: str) -> Any | None: ...

    @abstractmethod
    def put(self, key: str, value: Any) -> None: ...

    @abstractmethod
    def all(self) -> dict: ...


class TodoCacheReader(ABC):
    """Read-only view of the derived todo cache (todo.db).

    Mirrors metrics._db_counts.
    """

    @abstractmethod
    def counts(self) -> dict: ...


class StateStore(ABC):
    """Single entry point for the orchestrator's persistent state.

    Each area is a sub-surface object; backends differ only in what they
    hand back here.
    """

    @abstractmethod
    def ledger(self) -> Ledger: ...

    @abstractmethod
    def mailbox(self) -> Mailbox: ...

    @abstractmethod
    def planner_kv(self) -> PlannerKV: ...

    @abstractmethod
    def todo_cache(self) -> TodoCacheReader: ...


# ---------------------------------------------------------------------------
# Git mode — wraps the current implementations by composition
# ---------------------------------------------------------------------------


class GitPlannerKV(PlannerKV):
    """planner_state.json, read and written in worker.save_planner_state's format.

    With no explicit `path`, the file is worker.PLANNER_STATE_PATH looked up on
    every access — so it follows the remote-host state dir and any rebinding,
    and is the same file worker.load/save_planner_state use by construction.
    """

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path is not None else None

    @property
    def path(self) -> Path:
        if self._path is not None:
            return self._path
        from .. import worker

        return worker.PLANNER_STATE_PATH

    def all(self) -> dict:
        p = self.path
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        # One divergence from load_planner_state, which hands back a non-dict
        # as-is: put() needs a dict. No writer ever produces a non-dict.
        return data if isinstance(data, dict) else {}

    def get(self, key: str) -> Any | None:
        return self.all().get(key)

    def put(self, key: str, value: Any) -> None:
        state = self.all()
        state[key] = value
        # Same bytes as save_planner_state (indent=2, raw UTF-8, trailing
        # newline), written atomically so a crash never tears the file.
        atomic_write_text(self.path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


_ZERO_COUNTS = {"done": 0, "auto_done": 0, "cancelled": 0}


class GitTodoCacheReader(TodoCacheReader):
    """Counts from todo.db — the same queries and fallback as metrics._db_counts.

    Keep the SQL in sync with metrics._db_counts. Unlike it, the db is opened
    read-only and closed, so a missing db yields the zero counts without
    leaving an empty file behind.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def counts(self) -> dict:
        con = None
        try:
            # Plain as_posix() in the URI: a `?` or `#` in the path would need
            # quoting, and no vault path carries one.
            con = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True)
            done = con.execute("SELECT COUNT(*) FROM tasks WHERE status='done'").fetchone()[0]
            auto_done = con.execute(
                "SELECT COUNT(*) FROM tasks WHERE status='done' AND worker_mode IN ('auto','delegate')"
            ).fetchone()[0]
            cancelled = con.execute("SELECT COUNT(*) FROM tasks WHERE status='cancelled'").fetchone()[0]
            return {"done": done, "auto_done": auto_done, "cancelled": cancelled}
        except Exception:
            return dict(_ZERO_COUNTS)
        finally:
            if con is not None:
                con.close()


class GitStateStore(StateStore):
    """The git-backed store: today's files, wrapped as-is.

    Sub-surfaces are built on first use and cached per instance (a GitMailbox
    runs `git rev-parse` when built; its cursor lives on disk, so sharing one
    is safe).
    """

    def __init__(
        self,
        vault_root: str | Path | None = None,
        *,
        mailbox_uri: str = f"git://{DEFAULT_ROOT}",
        planner_state_path: str | Path | None = None,
        todo_db_path: str | Path | None = None,
    ):
        # Same resolution as worker.ROOT / metrics._ROOT.
        self.vault_root = (
            Path(vault_root) if vault_root is not None
            else paths.vault_root(start=paths.vault_search_start())
        )
        self.mailbox_uri = mailbox_uri
        self.planner_state_path = planner_state_path
        self.todo_db_path = todo_db_path
        self._ledger: Ledger | None = None
        self._mailbox: Mailbox | None = None
        self._planner_kv: PlannerKV | None = None
        self._todo_cache: TodoCacheReader | None = None

    def ledger(self) -> Ledger:
        if self._ledger is None:
            self._ledger = Ledger(self.vault_root / "Worker" / "assignments")
        return self._ledger

    def mailbox(self) -> Mailbox:
        if self._mailbox is None:
            self._mailbox = open_mailbox(self.mailbox_uri, base=self.vault_root)
        return self._mailbox

    def planner_kv(self) -> PlannerKV:
        if self._planner_kv is None:
            self._planner_kv = GitPlannerKV(self.planner_state_path)
        return self._planner_kv

    def todo_cache(self) -> TodoCacheReader:
        if self._todo_cache is None:
            db = self.todo_db_path or self.vault_root / "Todo" / "todo.db"
            self._todo_cache = GitTodoCacheReader(db)
        return self._todo_cache


# ---------------------------------------------------------------------------
# Composition entry point — routes on paths.state_backend() (leaf ③/§⑥)
# ---------------------------------------------------------------------------


def open_state_store(vault_root: str | Path | None = None) -> StateStore:
    """Build the StateStore for the configured backend (design §③/§⑥).

    ``git`` (the default) returns :class:`GitStateStore`; ``external`` is leaf
    ④'s not-yet-built SQLite backend and raises ``NotImplementedError`` naming
    it, rather than silently falling back to git.
    """
    backend = paths.state_backend()
    if backend == "git":
        return GitStateStore(vault_root)
    raise NotImplementedError(
        "state_backend='external' has no backend yet — see "
        "docs/design/state-store.md leaf ④ (ExternalStateStore / state_store_sqlite.py)"
    )
