"""Related past records, computed by the engine and handed over as data.

Why this module exists: the three consumers that most need "which past records
relate to this task" are the three that **cannot run a command to find out**.

* The research stage's whitelist is ``STAGE_TOOLS["research"] = "Read Write"``
  (``orchestrator/stages.py``) — no Bash at all. ``TASK_TOOLS_STAGES =
  ("implement",)`` means a task's own ``allowed_tools`` cannot widen it either,
  so "run this query yourself" is a dead instruction there however it is worded.
* ``scripts/brief.sh`` passes ``Read Write Edit Bash(git log:*) Bash(git diff:*)
  Bash(git status:*)`` — same conclusion.
* ``scripts/planner_tick.sh`` does pass a bare ``Bash``, so the planner *could*
  run a query. It is injected anyway: a tick-computed section is deterministic
  and its cost is bounded by the tick, not by what the agent decides to do.

So the engine runs the query and hands over the answer: the runner writes
``<run_dir>/related.md`` before the research stage starts
(:func:`stages.record_related` → :func:`write_related`), and the two shell ticks
append :func:`emit_context`'s block to their prompt. **No agent's tool
permissions change** — that is a hard requirement, pinned by
``todo_system/tests/test_related_context_wiring.py``.

One module for all three consumers so the privacy boundary is enforced in one
place. It is the fifth member of the ``emit-context`` family the planner tick
already calls (``followups`` / ``discovery`` / ``planner_mode`` / ``keep_alive``),
and shaped like them: always exit 0, print nothing on any failure.

R5 privacy boundary — **metadata only**. A rendered line carries the slug, the
title, the source path, the score split and the edge type. It never carries a
body excerpt, nor ``outcome`` / ``next_action`` text: those are read as *query
input* (``graph_memory._task_query_text``) and must not come back out. A reader
who needs more opens the ``source`` path itself.

Fail-open by contract. Every public function swallows its own errors and
degrades, in this order:

1. **hybrid** — vector KNN + graph BFS, when the vault has been embedded and
   sqlite-vec loads;
2. **BFS-only** — when it has not (``graph_memory._knn_seeds`` returns ``{}`` by
   itself, no exception); the rendered ``mode:`` says which of the two ran;
3. **nothing at all** — no file, no prompt section — when the DB is missing,
   unreadable, or has no ``edges`` table. A pipeline must never fail because a
   *hint* could not be computed.

The DB is opened **read-only** (``mode=ro``): ``graph_memory.init_relations_db``
is a write path (mkdir + CREATE TABLE), and neither a run on an external repo nor
a planner tick may create or migrate the vault's ``Memory/relations.sqlite``. A
missing ``edges`` table therefore *raises* on a ro connection and becomes case 3
above, which is the wanted behaviour — creating the table instead would be a
write AND would hide the fact that nothing is indexed yet. Edge freshness is the
todo-sync daemon's job (it calls ``scan_vault_and_index`` every cycle), so
nothing here scans the vault. The journal mode is left alone on purpose: WAL
would create ``relations.sqlite-wal`` / ``-shm``, which are NOT gitignored in the
vault, and untracked files under ``Memory/`` stall the 5-minute queue
(CLAUDE.md §10.1).

Environment (read at call time):

  MACP_RELATED_CONTEXT   "0" switches every path off — ``related.md`` and both
                         prompt sections. The runtime rollback for this wiring.

CLI (``planner_tick.sh`` / ``brief.sh``; always exits 0):

    python -m todo_system.orchestrator.related_context emit-context \
        [--limit N] [--per-task N] [--vault-root DIR]

Stdlib only, like ``graph_memory`` itself — no top-level ``import sqlite_vec``
anywhere (that stays behind ``graph_memory._load_sqlite_vec``'s lazy seam).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .. import graph_memory as gm

KILL_ENV = "MACP_RELATED_CONTEXT"

MAX_ITEMS = 8          # AC#1: "최대 8개" per task, in related.md and per prompt row
DEFAULT_TASKS = 5      # planner passes --limit 5; brief passes --limit 3
MIN_KNN = 10           # never ask query_related for fewer seeds than its own default

RELATED_FILENAME = "related.md"
SNAPSHOT_REL = ("Todo", "Snapshots", "latest.json")
ITEMS_REL = ("Todo", "items")

PROMPT_HEADING = "## 관련 과거 기록 (memory v2, 엔진이 미리 질의)"

# Seams, both read at CALL time — never frozen into a default argument — so a
# test can point them at a tmp vault, and so `--vault-root` really redirects.
VAULT_ROOT: Path | None = None   # None → graph_memory.ROOT (attribute, not a copy)
DB_PATH: Path | None = None      # None → derived from VAULT_ROOT, else graph_memory.DB_PATH


# -- config -------------------------------------------------------------------

def enabled() -> bool:
    """Only an explicit "0" disables the wiring (``maintenance_disk.enabled``'s rule)."""
    return os.environ.get(KILL_ENV, "").strip() != "0"


def vault_root() -> Path:
    """``gm.ROOT`` is read as an ATTRIBUTE here, not imported as a value: tests
    (and ``eval_memory``) redirect the vault with ``monkeypatch.setattr(gm,
    "ROOT", …)``, which a ``from ..graph_memory import ROOT`` would defeat."""
    return Path(VAULT_ROOT) if VAULT_ROOT is not None else Path(gm.ROOT)


def db_path(root: Path | None = None) -> Path:
    """The edges DB. A vault root always redirects the DB with it — an explicit
    ``root`` (``--vault-root``) included, so a CLI run against another vault
    cannot read this host's DB while rendering that vault's tasks. The filename
    is taken from ``gm.DB_PATH`` so it is spelt in one place."""
    if root is not None:
        return Path(root) / "Memory" / Path(gm.DB_PATH).name
    if DB_PATH is not None:
        return Path(DB_PATH)
    if VAULT_ROOT is not None:
        return Path(VAULT_ROOT) / "Memory" / Path(gm.DB_PATH).name
    return Path(gm.DB_PATH)


# -- the read-only connection -------------------------------------------------

def _connect_ro(path: Path | None = None) -> sqlite3.Connection | None:
    """A read-only connection to the edges DB, or ``None`` — never a raise, and
    never a created file. ``mode=ro`` is the whole point (see module docstring)."""
    db = Path(path) if path is not None else db_path()
    try:
        if not db.is_file():
            return None
        con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con
    except (sqlite3.Error, OSError, ValueError):
        return None


# -- titles (metadata, so R5-clean) -------------------------------------------

def _own_doc(root: Path, slug: str, source_file: str | None) -> Path | None:
    """The vault file that IS ``slug``'s own document, or ``None``.

    ``source_file`` counts only when its stem matches the slug. For a *graph* hit
    it is the file that DECLARED the connecting edge — one endpoint's file, which
    is usually the OTHER node — so reading a title out of it would label this
    item with its neighbour's title. For a pure-semantic hit it is the doc
    itself, so the stem check accepts it (and nested paths like
    ``Notes/<dir>/<file>.md`` come for free). Otherwise the slug is looked up
    directly under each scan root: five ``is_file()`` calls, no vault walk.
    """
    if source_file:
        candidate = Path(source_file)
        if candidate.stem == slug:
            own = root / candidate
            if own.is_file():
                return own
    for rel in gm.SCAN_ROOTS:
        own = root / rel / f"{slug}.md"
        if own.is_file():
            return own
    return None


def title_for(root: Path, slug: str, source_file: str | None = None) -> str:
    """The doc's frontmatter ``title``, else the slug. Metadata only, so no R5
    concern; every failure falls back to the slug **silently** (a title is a
    nicety, and a missing one must not cost the whole item)."""
    own = _own_doc(Path(root), slug, source_file)
    if own is None:
        return slug
    try:
        fm, _body = gm.parse_frontmatter(own.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return slug
    raw = fm.get("title")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return slug


# -- the query ----------------------------------------------------------------

def related_items(task: Mapping[str, Any], *, limit: int = MAX_ITEMS,
                  con: sqlite3.Connection | None = None) -> list:
    """Up to ``limit`` :class:`graph_memory.RelatedItem` for one task identity.

    The single choke point for the kill switch and for the fail-open contract:
    **no exception leaves this function** and a disabled switch yields ``[]``, so
    every consumer degrades to "no hint" by doing nothing special. Pass ``con``
    to reuse one read-only connection across several tasks (the CLI does);
    otherwise one is opened and closed here.
    """
    if not enabled() or limit <= 0:
        return []
    own_con = con is None
    try:
        if own_con:
            con = _connect_ro()
        if con is None:
            return []
        items = gm.query_related(con, task, k=max(int(limit), MIN_KNN))
        return list(items)[:int(limit)]
    except Exception:       # noqa: BLE001 - a hint must never break the caller
        return []
    finally:
        if own_con and con is not None:
            try:
                con.close()
            except Exception:   # noqa: BLE001 - closing is best-effort too
                pass


def mode_of(items: Sequence) -> str:
    """``"hybrid"`` when the vector layer actually contributed, else
    ``"bfs-only"``. An honest signal for the reader (and a testable one): it says
    whether the vault's embeddings were in play or the result is pure graph."""
    return "hybrid" if any(getattr(it.score, "vector", 0.0) > 0 for it in items) else "bfs-only"


# -- rendering (metadata only — this is where R5 is enforced) -----------------

def item_line(item, title: str) -> str:
    """One rendered record: slug, title, source, split score, discovering edge.

    Deliberately NOT included: any body text, ``outcome`` or ``next_action``.
    Those are query *input* only (``graph_memory._task_query_text``); letting
    them out here would turn a metadata pointer into a content leak (R5).
    """
    bits = [f"- `{item.slug}` — {title}"]
    if item.source_file:
        bits.append(f"source: `{item.source_file}`")
    bits.append(
        f"score {item.score.combined:.3f} "
        f"(vector {item.score.vector:.2f} / graph {item.score.graph:.2f})"
    )
    if item.edge_type:
        hops = item.hops
        hop_text = f", {hops} hop" if isinstance(hops, int) else ""
        bits.append(f"edge: {item.edge_type}{hop_text}")
    if item.run_id:
        bits.append(f"run: `{item.run_id}`")
    return " · ".join(bits)


def render_lines(items: Sequence, *, root: Path) -> list[str]:
    """:func:`item_line` for each item, titles resolved against ``root``."""
    return [item_line(it, title_for(root, it.slug, it.source_file)) for it in items]


def render_related_md(task: Mapping[str, Any], items: Sequence, *,
                      root: Path, run_id: str | None = None,
                      now: str | None = None) -> str:
    """The body of ``<run_dir>/related.md``. Metadata only (see :func:`item_line`)."""
    slug = str(task.get("slug") or "")
    generated = now or dt.datetime.now().isoformat(timespec="seconds")
    lines = [
        "---",
        "kind: related",
        f"run_id: {run_id or '-'}",
        f"slug: {slug or '-'}",
        f"generated: {generated}",
        "source: graph_memory.query_related",
        f"mode: {mode_of(items)}",
        f"count: {len(items)}",
        "---",
        "",
        f"# 관련 과거 기록 — {slug or '(slug 없음)'}",
        "",
        "엔진이 research 스테이지가 시작되기 **전에** 질의해 둔 목록이다 (이 스테이지의 도구는",
        "`Read Write` 뿐이라 직접 질의할 수 없다). **메타데이터만** 적혀 있다 — 본문 발췌는 없다.",
        "더 필요하면 아래 `source` 경로를 직접 Read 하라. 이 파일이 아예 없으면 엔진이 만들지",
        "못한 것이고 실패가 아니다 — 그냥 진행하면 된다.",
        "",
    ]
    lines += render_lines(items, root=root)
    lines.append("")
    return "\n".join(lines)


def render_prompt_section(rows: Iterable[tuple[Mapping[str, Any], Sequence]], *,
                          root: Path, heading: str = PROMPT_HEADING) -> str:
    """The block ``planner_tick.sh`` / ``brief.sh`` append to their prompt.

    Returns ``""`` when no row has any item — which is what lets both callers
    keep their prompt **byte-identical** on the common path (the ``followups`` /
    ``keep_alive`` contract: empty output ⇒ nothing appended).
    """
    body: list[str] = []
    for task, items in rows:
        if not items:
            continue
        slug = str(task.get("slug") or "?")
        title = task.get("title")
        label = f"`{slug}`" + (f" — {title}" if isinstance(title, str) and title.strip() else "")
        body.append(f"### {label}")
        body.append("")
        body += render_lines(items, root=root)
        body.append("")
    if not body:
        return ""
    return "\n".join([
        heading,
        "",
        "아래는 엔진이 `graph_memory.query_related`로 미리 뽑아 둔 **메타데이터만**의 목록이다 "
        "(본문 발췌 없음). 중복 생성을 피하고 과거 실패·결정을 잇는 데 쓰고, 더 필요하면 "
        "`source` 경로를 직접 읽어라. 목록이 비어 있으면 이 섹션 자체가 없다.",
        "",
        *body,
    ])


# -- which tasks to query (AC#2's "후보/Top-3") --------------------------------

def _snapshot_tasks(root: Path, limit: int) -> list[dict[str, Any]]:
    """``Todo/Snapshots/latest.json``'s ``top_priority`` head.

    That array is ``tasks WHERE status='open'`` sorted by priority desc
    (``scripts/export_db_snapshot.py``), and ``brief.sh`` regenerates the file
    immediately before it assembles the prompt — so brief's "Top-3" here is the
    head of the very list the brief agent then reads.
    """
    path = root
    for part in SNAPSHOT_REL:
        path = path / part
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = doc.get("top_priority") if isinstance(doc, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if len(out) >= limit:
            break
        if not isinstance(row, dict):
            continue
        slug = row.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        out.append(_task_identity(slug.strip(), row))
    return out


def _task_identity(slug: str, src: Mapping[str, Any]) -> dict[str, Any]:
    """A task mapping shaped for ``graph_memory.query_related``.

    ``outcome`` / ``next_action`` / ``touches`` are carried because
    ``_task_query_text`` queries with them — they are INPUT only and never
    rendered (see :func:`item_line`)."""
    task: dict[str, Any] = {"slug": slug}
    for key in ("title", "touches", "outcome", "next_action"):
        value = src.get(key)
        if isinstance(value, (list, tuple)) or (isinstance(value, str) and value.strip()):
            task[key] = value
    return task


def _priority_of(fm: Mapping[str, Any]) -> float:
    try:
        return float(str(fm.get("priority", "")).strip() or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _items_tasks(root: Path, limit: int) -> list[dict[str, Any]]:
    """Fallback for a vault with no snapshot (``vault-template`` included): open
    tasks from ``Todo/items/*.md`` frontmatter, priority desc then slug asc so
    the answer is deterministic. Shaped like ``eval_memory._scan_vault_tasks``."""
    items_dir = root
    for part in ITEMS_REL:
        items_dir = items_dir / part
    if not items_dir.is_dir():
        return []
    rows: list[tuple[float, str, dict[str, Any]]] = []
    for path in sorted(items_dir.glob("*.md")):
        try:
            fm, _body = gm.parse_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(fm.get("status", "")).strip() not in ("", "open"):
            continue
        rows.append((-_priority_of(fm), path.stem, _task_identity(path.stem, fm)))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [task for _p, _s, task in rows[:limit]]


def top_open_tasks(root: Path | None = None, *, limit: int = DEFAULT_TASKS) -> list[dict[str, Any]]:
    """The ``limit`` task identities the prompt sections are built for. Snapshot
    first, frontmatter scan as the fallback; ``[]`` on any failure."""
    base = Path(root) if root is not None else vault_root()
    if limit <= 0:
        return []
    try:
        rows = _snapshot_tasks(base, limit)
        return rows if rows else _items_tasks(base, limit)
    except Exception:       # noqa: BLE001 - fail safe: no tasks ⇒ no section
        return []


# -- the three entry points ---------------------------------------------------

def write_related(run_dir: Path, task: Mapping[str, Any], *, run_id: str | None = None,
                  limit: int = MAX_ITEMS) -> str | None:
    """Write ``<run_dir>/related.md`` and return the one progress line, or ``None``.

    ``None`` means "no file" and is the normal answer for: the kill switch set,
    no DB, no ``edges`` table, a query failure, or simply zero related records
    (the ``record_orphaned_commits`` rule — nothing to say, so nothing written).
    The caller must treat ``None`` as success; AC#1 forbids blocking the
    pipeline on it. Returns the line rather than the path because the count and
    the degradation mode are only knowable here.
    """
    try:
        items = related_items(task, limit=limit)
        if not items:
            return None
        root = vault_root()
        text = render_related_md(task, items, root=root, run_id=run_id)
        path = Path(run_dir) / RELATED_FILENAME
        path.write_text(text, encoding="utf-8")
        return f"related: {len(items)} record(s) → {RELATED_FILENAME} ({mode_of(items)})"
    except Exception:       # noqa: BLE001 - second belt; related_items has the first
        return None


def emit_context(*, limit: int = DEFAULT_TASKS, per_task: int = MAX_ITEMS,
                 root: Path | None = None, heading: str = PROMPT_HEADING) -> str:
    """The prompt block for the top ``limit`` open tasks; ``""`` when there is
    nothing to say (so both shell callers keep a byte-identical prompt)."""
    if not enabled():
        return ""
    base = Path(root) if root is not None else vault_root()
    tasks = top_open_tasks(base, limit=limit)
    if not tasks:
        return ""
    # db_path(root), not db_path(): an explicit --vault-root has to move the DB
    # too, or the block would pair one vault's tasks with another vault's edges.
    con = _connect_ro(db_path(root))
    if con is None:
        return ""
    try:
        rows = [(task, related_items(task, limit=per_task, con=con)) for task in tasks]
    finally:
        try:
            con.close()
        except Exception:   # noqa: BLE001 - closing is best-effort
            pass
    return render_prompt_section(rows, root=base, heading=heading)


# -- CLI ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m todo_system.orchestrator.related_context",
        description="Related past records for the top open tasks, as a prompt block.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    emit = sub.add_parser("emit-context", help="print the related-records block (empty on error)")
    emit.add_argument("--limit", type=int, default=DEFAULT_TASKS,
                      help=f"how many top open tasks to query (default {DEFAULT_TASKS})")
    emit.add_argument("--per-task", type=int, default=MAX_ITEMS,
                      help=f"max records per task (default {MAX_ITEMS})")
    emit.add_argument("--vault-root", type=Path, default=None,
                      help="query this vault instead of the resolved one")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Always 0 — the callers inject the stdout verbatim and must never see a
    failure turn into a dead tick. Any error is one stderr line they drop."""
    args = build_parser().parse_args(argv)
    try:
        sys.stdout.write(emit_context(limit=args.limit, per_task=args.per_task,
                                      root=args.vault_root))
    except Exception as exc:    # noqa: BLE001 - fail safe: print nothing, exit 0
        print(f"related-context: error ({type(exc).__name__}: {exc})"[:300], file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
