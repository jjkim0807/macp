from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .calendar_sync import render_calendar_markdown, render_unified_markdown, sync_due_tasks_to_calendar
from . import graph_memory as _graph_memory
from . import not_before as _not_before
from . import ownership as _ownership
from .ownership import HUMAN_OVERRIDE_WORKER_MODES
from . import worker as _worker
from . import task_validate as _task_validate
from . import session_end as _session_end
from . import paths

ROOT = paths.vault_root()
TODO_DIR = ROOT / "Todo"
ITEMS_DIR = TODO_DIR / "items"
DB_PATH = TODO_DIR / "todo.db"
NOW_PATH = TODO_DIR / "NOW.md"
FOCUS_PATH = TODO_DIR / "FOCUS.md"
# The calendar the todo sync writes to when no --calendar-id is given: the configured
# one (MACP_CALENDAR_ID / .macp/config.toml calendar_id, see paths.primary_calendar_id),
# else the account's own "primary" calendar. No personal calendar id lives in the engine.
DEFAULT_CALENDAR_ID = paths.primary_calendar_id() or "primary"
DEFAULT_PRIMARY_CALENDAR_ID = paths.primary_calendar_id(start=ROOT)
DEFAULT_GCAL_CLIENT_SECRET_PATH = ROOT / ".secrets" / "google-calendar-client-secret.json"
DEFAULT_GCAL_TOKEN_PATH = ROOT / ".secrets" / "google-calendar-token.json"
CALENDAR_PATH = TODO_DIR / "Calendar.md"
UNIFIED_PATH = TODO_DIR / "Unified.md"
DASHBOARD_PATH = TODO_DIR / "Dashboard.md"
DEFAULT_FOCUS_DAYS = 7
DEFAULT_RECENT_DAYS = 14

CHECKBOX_RE = re.compile(r"^\s*-\s\[( |x|X)\]\s+(.*)$")
LINK_RE = re.compile(r"\[\[Todo/items/([a-z0-9-]+)(?:\|[^\]]+)?\]\]")

WORK_SECTION_TO_LANE = {
    "## 🔴 긴급": "urgent",
    "## 🟡 이번 주": "this_week",
    "## 🟣 장기 프로젝트": "long_project",
    "## 🟢 다음 주 / 백로그": "backlog",
}

WIP_LIMITS = {
    "urgent": 2,
    "this_week": 3,
    "long_project": 1,
}

LANE_PRIORITY = {
    "urgent": 1,
    "this_week": 2,
    "long_project": 3,
    "backlog": 4,
}


@dataclass
class ParsedTask:
    slug: str
    path: str
    title: str
    status: str
    due: str | None
    not_before: str | None
    updated: str | None
    outcome: str | None
    next_action: str | None
    if_then: str | None
    estimate_p50: str | None
    estimate_p90: str | None
    review_cycle: str | None
    priority: float | None
    total_subtasks: int
    done_subtasks: int
    subtasks: list[tuple[str, int]]
    parent_slug: str | None
    is_parent: int
    tags: list[str] = None  # from frontmatter tags field
    worker_mode: str | None = None  # auto | delegate | blocked | manual | None
    auto_merge: int = 1  # 1=auto-merge PR on CI green (default); 0=open PR + park for human review (`auto_merge: false`)


# Re-exported from .ownership so existing `cli.HUMAN_OVERRIDE_WORKER_MODES` /
# `cli._is_worker_owned` callers keep working.
_is_worker_owned = _ownership.is_worker_owned


# Default priority by lane (used when frontmatter `priority` is missing)
LANE_DEFAULT_PRIORITY = {
    "urgent": 90.0,
    "this_week": 70.0,
    "long_project": 50.0,
    "backlog": 30.0,
}


def db_connect(db_path: Path, *, require_schema: bool = False) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    if require_schema:
        row = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
        ).fetchone()
        if row is None:
            print(
                f"error: database not initialized (no 'tasks' table) at {db_path} — "
                f"run: python -m todo_system.cli init-db --db {db_path}",
                file=sys.stderr,
            )
            sys.exit(2)
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            path TEXT NOT NULL,
            lane TEXT,
            status TEXT NOT NULL DEFAULT 'open',
            due TEXT,
            not_before TEXT,
            updated TEXT,
            outcome TEXT,
            next_action TEXT,
            if_then TEXT,
            estimate_p50 TEXT,
            estimate_p90 TEXT,
            review_cycle TEXT,
            total_subtasks INTEGER NOT NULL DEFAULT 0,
            done_subtasks INTEGER NOT NULL DEFAULT 0,
            imported_at TEXT NOT NULL,
            tags TEXT
        );

        CREATE TABLE IF NOT EXISTS subtasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            ord INTEGER NOT NULL,
            content TEXT NOT NULL,
            is_done INTEGER NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_lane ON tasks(lane);
        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due);
        """
    )

    columns = {row[1] for row in con.execute("PRAGMA table_info(tasks)").fetchall()}
    if "not_before" not in columns:
        con.execute("ALTER TABLE tasks ADD COLUMN not_before TEXT")
    if "priority" not in columns:
        con.execute("ALTER TABLE tasks ADD COLUMN priority REAL")
    if "parent_slug" not in columns:
        con.execute("ALTER TABLE tasks ADD COLUMN parent_slug TEXT")
    if "is_parent" not in columns:
        con.execute("ALTER TABLE tasks ADD COLUMN is_parent INTEGER NOT NULL DEFAULT 0")
    if "worker_mode" not in columns:
        con.execute("ALTER TABLE tasks ADD COLUMN worker_mode TEXT")
    if "auto_merge" not in columns:
        con.execute("ALTER TABLE tasks ADD COLUMN auto_merge INTEGER NOT NULL DEFAULT 1")

    con.execute("CREATE INDEX IF NOT EXISTS idx_tasks_not_before ON tasks(not_before)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_tasks_parent_slug ON tasks(parent_slug)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_tasks_is_parent ON tasks(is_parent)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_tasks_worker_mode ON tasks(worker_mode)")

    con.commit()


def parse_frontmatter(content: str) -> tuple[dict[str, str | list[str]], str]:
    if not content.startswith("---\n"):
        return {}, content

    parts = content.split("\n---\n", 1)
    if len(parts) != 2:
        return {}, content

    fm_raw, body = parts
    fm_lines = fm_raw.splitlines()[1:]
    result: dict[str, str | list[str]] = {}
    idx = 0
    while idx < len(fm_lines):
        line = fm_lines[idx]
        if not line.strip():
            idx += 1
            continue
        if ":" not in line:
            idx += 1
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value == "":
            items: list[str] = []
            idx += 1
            while idx < len(fm_lines):
                sub = fm_lines[idx]
                if sub.strip().startswith("- "):
                    items.append(sub.strip()[2:].strip())
                    idx += 1
                    continue
                if sub.startswith("  - "):
                    items.append(sub.strip()[2:].strip())
                    idx += 1
                    continue
                break
            result[key] = items
            continue
        value = value.strip().strip('"').strip("'")
        result[key] = value
        idx += 1

    return result, body


def update_frontmatter_value(content: str, key: str, value: str) -> str:
    if not content.startswith("---\n"):
        return content

    parts = content.split("\n---\n", 1)
    if len(parts) != 2:
        return content

    fm_raw, body = parts
    fm_lines = fm_raw.splitlines()[1:]
    out_lines: list[str] = []
    idx = 0
    found = False
    while idx < len(fm_lines):
        line = fm_lines[idx]
        stripped = line.strip()
        if stripped.startswith(f"{key}:"):
            out_lines.append(f"{key}: {value}")
            found = True
            idx += 1
            while idx < len(fm_lines):
                nxt = fm_lines[idx]
                if nxt.startswith("  - ") or nxt.strip().startswith("- "):
                    idx += 1
                    continue
                break
            continue
        out_lines.append(line)
        idx += 1

    if not found:
        out_lines.append(f"{key}: {value}")

    return "---\n" + "\n".join(out_lines) + "\n---\n" + body


def remove_frontmatter_key(content: str, key: str) -> str:
    if not content.startswith("---\n"):
        return content

    parts = content.split("\n---\n", 1)
    if len(parts) != 2:
        return content

    fm_raw, body = parts
    fm_lines = fm_raw.splitlines()[1:]
    out_lines: list[str] = []
    idx = 0
    while idx < len(fm_lines):
        line = fm_lines[idx]
        stripped = line.strip()
        if stripped.startswith(f"{key}:"):
            idx += 1
            while idx < len(fm_lines):
                nxt = fm_lines[idx]
                if nxt.startswith("  - ") or nxt.strip().startswith("- "):
                    idx += 1
                    continue
                break
            continue
        out_lines.append(line)
        idx += 1

    return "---\n" + "\n".join(out_lines) + "\n---\n" + body


def find_task_path(con: sqlite3.Connection, slug: str) -> Path | None:
    row = con.execute("SELECT path FROM tasks WHERE slug = ?", (slug,)).fetchone()
    if row and row["path"]:
        path = ROOT / str(row["path"])
        if path.exists():
            return path

    candidate = ITEMS_DIR / f"{slug}.md"
    if candidate.exists():
        return candidate
    return None


def update_markdown_checkboxes(path: Path, mode: str, subtask_ord: int | None = None) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    new_lines: list[str] = []
    seen = 0
    for line in lines:
        match = CHECKBOX_RE.match(line)
        if not match:
            new_lines.append(line)
            continue

        seen += 1
        text = match.group(2)
        indent = line[: len(line) - len(line.lstrip())]
        target_this = subtask_ord is None or seen == subtask_ord

        if target_this:
            if mode == "complete":
                new_lines.append(f"{indent}- [x] {text}")
            elif mode == "reopen":
                new_lines.append(f"{indent}- [ ] {text}")
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def refresh_single_task(con: sqlite3.Connection, task_path: Path) -> None:
    task = parse_task_file(task_path)
    lane_map = parse_lanes()
    lane = lane_map.get(task.slug)
    upsert_task(con, task, lane)


def pick_now_task_sqlite(con: sqlite3.Connection,
                         now: dt.datetime | None = None) -> sqlite3.Row | None:
    """SQLite version of pick_now_task.

    `not_before` is judged in Python by the shared `not_before.is_deferred`, not
    in SQL: a lexical `not_before <= today` kept a `YYYY-MM-DD HH:MM` value out
    all day. `now` pins that clock (default: host-local now)."""
    today = now.date().isoformat() if now else dt.date.today().isoformat()
    # Effective score = priority + urgency bonus from due date.
    # Bonus tiers (days until due):
    #   overdue (<0): +50
    #   ≤3 days:      +30
    #   ≤7 days:      +15
    #   ≤14 days:     +5
    #   >14 / null:   0
    human_modes_placeholder = ", ".join("?" for _ in HUMAN_OVERRIDE_WORKER_MODES)
    rows = con.execute(
            f"""
            SELECT id, slug, title, path, lane, due, not_before, next_action, if_then, parent_slug
            FROM tasks
            WHERE status = 'open'
                AND COALESCE(is_parent, 0) = 0
                AND LOWER(COALESCE(worker_mode, '')) IN ({human_modes_placeholder})
            ORDER BY
                COALESCE(priority, 50) + (
                    CASE
                        WHEN due IS NULL OR due = '' OR due = '-' THEN 0
                        WHEN julianday(due) - julianday(?) < 0 THEN 50
                        WHEN julianday(due) - julianday(?) <= 3 THEN 30
                        WHEN julianday(due) - julianday(?) <= 7 THEN 15
                        WHEN julianday(due) - julianday(?) <= 14 THEN 5
                        ELSE 0
                    END
                ) DESC,
                CASE lane
                    WHEN 'urgent' THEN 1
                    WHEN 'this_week' THEN 2
                    WHEN 'long_project' THEN 3
                    WHEN 'backlog' THEN 4
                    ELSE 9
                END,
                CASE WHEN due IS NULL THEN 1 ELSE 0 END,
                due,
                title
            """,
            (*HUMAN_OVERRIDE_WORKER_MODES, today, today, today, today),
    ).fetchall()
    for row in rows:
        if not _not_before.is_deferred(row[6], now):
            return row
    return None


def pick_now_task(con: sqlite3.Connection | None = None) -> sqlite3.Row | dict | None:
    """Pick the highest-priority actionable task.

    Reads SQLite. Returns a dict-like object (sqlite3.Row).
    """
    if con is None:
        raise ValueError("SQLite connection required")
    return pick_now_task_sqlite(con)


def _row_get(row, key, default=None):
    """Safe key access for both sqlite3.Row (no .get) and dict."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return default


def get_parent_row_sqlite(con: sqlite3.Connection, parent_slug: str | None) -> sqlite3.Row | None:
    """SQLite version of get_parent_row."""
    if not parent_slug:
        return None
    return con.execute(
        """
        SELECT slug, title, path, due, outcome
        FROM tasks
        WHERE slug = ?
        LIMIT 1
        """,
        (parent_slug,),
    ).fetchone()


def get_parent_row(
    con: sqlite3.Connection | None,
    parent_slug: str | None = None,
    parent_id: str | None = None,
) -> sqlite3.Row | dict | None:
    """Get parent task row from SQLite, looked up by parent_slug."""
    if con is None:
        raise ValueError("SQLite connection required")
    return get_parent_row_sqlite(con, parent_slug)


def get_first_open_subtask_sqlite(con: sqlite3.Connection, task_id: int) -> str | None:
    """SQLite version of get_first_open_subtask."""
    row = con.execute(
        """
        SELECT content
        FROM subtasks
        WHERE task_id = ?
          AND is_done = 0
        ORDER BY ord
        LIMIT 1
        """,
        (task_id,),
    ).fetchone()
    return str(row["content"]) if row else None


def get_first_open_subtask(con: sqlite3.Connection | None, task_id: int | str) -> str | None:
    """Get the first open subtask content from SQLite.

    task_id is an integer.
    """
    if con is None:
        raise ValueError("SQLite connection required")
    return get_first_open_subtask_sqlite(con, int(task_id))


def render_now_markdown(con: sqlite3.Connection | None = None) -> str:
    """Render NOW.md content from SQLite."""
    now = dt.datetime.now().isoformat(timespec="seconds")
    task = pick_now_task(con)
    if task is None:
        return (
            "---\n"
            "tags:\n"
            "  - todo\n"
            "  - now\n"
            f"updated: {dt.date.today().isoformat()}\n"
            "---\n\n"
            "# ⚡ NOW (지금 실행할 1개)\n\n"
            f"> last_synced: {now}\n\n"
            "- 실행할 open task가 없습니다.\n"
        )

    task_link = f"[[{task['path'].replace('.md', '')}|{task['title']}]]"
    next_action = task["next_action"] or "(next_action 미설정)"
    first_subtask = get_first_open_subtask(con, task["id"]) or "(남은 subtask 없음)"
    if_then = task["if_then"] or "(if_then 미설정)"

    parent_slug = _row_get(task, "parent_slug")
    parent_id = _row_get(task, "parent_id")
    parent_row = get_parent_row(con, parent_slug=parent_slug, parent_id=parent_id)
    if parent_row is not None:
        parent_link = f"[[{parent_row['path'].replace('.md', '')}|{parent_row['title']}]]"
        parent_block = (
            f"- **Parent**: {parent_link}\n"
            f"- **Parent Outcome**: {parent_row['outcome'] or '-'}\n"
            f"- **Parent Due**: `{parent_row['due'] or '-'}`\n\n"
        )
    else:
        parent_block = ""

    return (
        "---\n"
        "tags:\n"
        "  - todo\n"
        "  - now\n"
        f"updated: {dt.date.today().isoformat()}\n"
        "---\n\n"
        "# ⚡ NOW (지금 실행할 1개)\n\n"
        f"> last_synced: {now}\n\n"
        f"{parent_block}"
        f"- **Task**: {task_link}\n"
        f"- **Lane**: `{task['lane'] or '-'}`\n"
        f"- **Due**: `{task['due'] or '-'}`\n"
        f"- **Not Before**: `{task['not_before'] or '-'}`\n"
        f"- **Next Action**: {next_action}\n"
        f"- **First Open Subtask**: {first_subtask}\n"
        f"- **If-Then**: {if_then}\n"
    )


def sync_now_note(con: sqlite3.Connection) -> None:
    NOW_PATH.write_text(render_now_markdown(con), encoding="utf-8")


def _parse_iso_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def render_focus_markdown_sqlite(
    con: sqlite3.Connection,
    days: int = DEFAULT_FOCUS_DAYS,
    recent_days: int = DEFAULT_RECENT_DAYS,
) -> str:
    """SQLite version of render_focus_markdown."""
    today = dt.date.today()
    horizon = today + dt.timedelta(days=days)
    recent_cutoff = today - dt.timedelta(days=recent_days)

    rows = con.execute(
        """
        SELECT slug, title, path, lane, status, due, updated, next_action, worker_mode
        FROM tasks
        WHERE status IN ('open', 'waiting', 'blocked')
        ORDER BY
          COALESCE(priority, 50) DESC,
          CASE lane
            WHEN 'urgent' THEN 1
            WHEN 'this_week' THEN 2
            WHEN 'long_project' THEN 3
            WHEN 'backlog' THEN 4
            ELSE 9
          END,
          CASE WHEN due IS NULL THEN 1 ELSE 0 END,
          due,
          title
        """
    ).fetchall()

    overdue: list[sqlite3.Row] = []
    today_items: list[sqlite3.Row] = []
    upcoming: list[sqlite3.Row] = []
    recently_updated_undated: list[sqlite3.Row] = []
    worker_owned: list[sqlite3.Row] = []

    for row in rows:
        # Worker-owned tasks (worker_mode: auto/delegate) are the worker
        # pipeline's responsibility (Worker/queue.json + Dashboard "🤖 Worker
        # Agent 현황"), not the human's — keep them out of the human buckets
        # entirely so FOCUS/NOW only ever shows what a person needs to act on.
        if _is_worker_owned(row["worker_mode"]):
            worker_owned.append(row)
            continue

        due_date = _parse_iso_date(row["due"])
        updated_date = _parse_iso_date(row["updated"])

        if due_date:
            if due_date < today:
                overdue.append(row)
            elif due_date == today:
                today_items.append(row)
            elif due_date <= horizon:
                upcoming.append(row)
            continue

        if updated_date and updated_date >= recent_cutoff:
            recently_updated_undated.append(row)

    def fmt_row(row: sqlite3.Row) -> str:
        link = f"[[{str(row['path']).replace('.md', '')}|{row['title']}]]"
        return (
            f"- {link} | lane=`{row['lane'] or '-'}` | status=`{row['status']}` | "
            f"due=`{row['due'] or '-'}`\n"
            f"  next_action: {row['next_action'] or '-'}"
        )

    def fmt_worker_row(row: sqlite3.Row) -> str:
        link = f"[[{str(row['path']).replace('.md', '')}|{row['title']}]]"
        return (
            f"- {link} | worker_mode=`{row['worker_mode']}` | status=`{row['status']}` | "
            f"due=`{row['due'] or '-'}`\n"
            f"  next_action: {row['next_action'] or '-'}"
        )

    def section(title: str, items: list[sqlite3.Row]) -> str:
        if not items:
            return f"## {title}\n\n- 없음\n\n"
        return "## " + title + "\n\n" + "\n".join(fmt_row(i) for i in items) + "\n\n"

    def worker_section(title: str, items: list[sqlite3.Row]) -> str:
        if not items:
            return ""
        return "## " + title + "\n\n" + "\n".join(fmt_worker_row(i) for i in items) + "\n\n"

    now = dt.datetime.now().isoformat(timespec="seconds")
    return (
        "---\n"
        "tags:\n"
        "  - todo\n"
        "  - focus\n"
        f"updated: {today.isoformat()}\n"
        "---\n\n"
        "# 📅 Date-Filtered Tasks\n\n"
        f"> last_synced: {now}\n"
        f"> filter: due <= {horizon.isoformat()} or updated within {recent_days} days (undated)\n"
        f"> worker_mode: auto/delegate 태스크는 사람 섹션에서 제외 — 아래 별도 섹션 또는 "
        "Dashboard의 '🤖 Worker Agent 현황' 참고\n\n"
        + section("🚨 Overdue", overdue)
        + section("🟠 Today", today_items)
        + section(f"🟡 Next {days} Days", upcoming)
        + section(f"🔵 Recently Updated (No Due, {recent_days}d)", recently_updated_undated)
        + worker_section("🤖 Worker 대기열 (사람 액션 불필요, 큐 미등록 포함)", worker_owned)
    )


def render_focus_markdown(
    con: sqlite3.Connection | None = None,
    days: int = DEFAULT_FOCUS_DAYS,
    recent_days: int = DEFAULT_RECENT_DAYS,
) -> str:
    """Render FOCUS.md content from SQLite."""
    if con is None:
        raise ValueError("SQLite connection required")
    return render_focus_markdown_sqlite(con, days=days, recent_days=recent_days)


def sync_focus_note(
    con: sqlite3.Connection,
    days: int = DEFAULT_FOCUS_DAYS,
    recent_days: int = DEFAULT_RECENT_DAYS,
) -> None:
    FOCUS_PATH.write_text(render_focus_markdown(con, days=days, recent_days=recent_days), encoding="utf-8")


def _strip_markdown_header(md: str) -> str:
    body = md
    if body.startswith("---\n"):
        parts = body.split("\n---\n", 1)
        if len(parts) == 2:
            body = parts[1]
    lines = body.splitlines()
    out: list[str] = []
    skipping_h1 = True
    for line in lines:
        if skipping_h1 and line.startswith("# "):
            skipping_h1 = False
            continue
        if skipping_h1 and line.strip() == "":
            continue
        skipping_h1 = False
        out.append(line)
    return "\n".join(out).lstrip("\n")


def render_worker_activity_markdown() -> str:
    """Read Worker/queue.json and render in_progress / pending / recent runs.

    Returns a markdown block (no header line — caller adds the heading).
    Gracefully degrades if queue.json is missing or malformed.
    """
    queue_path = ROOT / "Worker" / "queue.json"
    if not queue_path.exists():
        return "> Worker 큐 파일이 아직 없습니다.\n"
    try:
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"> Worker 큐 읽기 실패: {exc}\n"

    runs = queue.get("runs", [])
    if not runs:
        return "> 진행 중이거나 대기 중인 worker run이 없습니다.\n"

    def _fmt(entry: dict) -> str:
        slug = entry.get("slug", "?")
        run_id = entry.get("run_id", "")
        mode = entry.get("mode") or "auto"
        branch = entry.get("branch") or "-"
        started = entry.get("started_at") or "-"
        finished = entry.get("finished_at") or "-"
        return f"- `{slug}` (mode={mode}, branch={branch}, run_id={run_id}, started={started}, finished={finished})\n"

    in_progress = [r for r in runs if r.get("status") == "in_progress"]
    pending = [r for r in runs if r.get("status") == "pending"]
    completed = [r for r in runs if r.get("status") == "completed"]
    failed = [r for r in runs if r.get("status") == "failed"]

    # Recent (last 24h) completed/failed by finished_at.
    cutoff = (dt.datetime.now() - dt.timedelta(hours=24)).isoformat(timespec="seconds")
    recent_terminal = sorted(
        [r for r in completed + failed if (r.get("finished_at") or "") >= cutoff],
        key=lambda r: r.get("finished_at") or "",
        reverse=True,
    )[:5]

    out: list[str] = []
    out.append(
        f"> queue summary: in_progress={len(in_progress)} pending={len(pending)} "
        f"completed={len(completed)} failed={len(failed)} total={len(runs)}\n\n"
    )

    if in_progress:
        out.append("### 🟢 현재 실행 중\n\n")
        out.extend(_fmt(e) for e in in_progress)
        out.append("\n")

    if pending:
        out.append("### 🟡 대기 중 (다음 run-next에서 처리)\n\n")
        out.extend(_fmt(e) for e in pending)
        out.append("\n")

    if recent_terminal:
        out.append("### 📜 최근 완료/실패 (24시간 이내)\n\n")
        for e in recent_terminal:
            status = e.get("status", "?")
            icon = "✅" if status == "completed" else "❌"
            out.append(f"- {icon} `{e.get('slug', '?')}` — {status}, finished={e.get('finished_at', '-')}\n")
        out.append("\n")

    if not (in_progress or pending or recent_terminal):
        out.append("> 진행 중/대기 중/최근 완료된 run이 없습니다.\n")

    return "".join(out)


def render_dashboard_markdown(
    con: sqlite3.Connection,
    *,
    primary_calendar_id: str | None = None,
    client_secret_path: Path | None = None,
    token_path: Path | None = None,
    days: int = 7,
    recent_days: int = DEFAULT_RECENT_DAYS,
) -> str:
    today = dt.date.today().isoformat()
    now = dt.datetime.now().isoformat(timespec="seconds")

    header = (
        "---\n"
        "tags:\n"
        "  - todo\n"
        "  - dashboard\n"
        f"updated: {today}\n"
        "---\n\n"
        "# 📊 Dashboard\n\n"
        "> 자동 생성 — 직접 수정 금지\n"
        f"> last_synced: {now}\n\n"
    )

    sections: list[str] = [header]

    sections.append("## 🎯 NOW (지금 실행할 1개)\n\n")
    sections.append(_strip_markdown_header(render_now_markdown(con)).rstrip() + "\n\n")

    sections.append("## 🤖 Worker Agent 현황\n\n")
    sections.append(render_worker_activity_markdown().rstrip() + "\n\n")

    calendar_ready = bool(primary_calendar_id and client_secret_path and token_path)
    if calendar_ready:
        try:
            cal_md = render_calendar_markdown(
                calendar_id=primary_calendar_id,
                client_secret_path=client_secret_path,
                token_path=token_path,
                days=days,
            )
            sections.append(f"## ⏰ 오늘/다가오는 {days}일 일정\n\n")
            sections.append(_strip_markdown_header(cal_md).rstrip() + "\n\n")
        except Exception as exc:
            sections.append(f"## ⏰ 일정\n\n> Calendar 연동 오류: {exc}\n\n")
    else:
        sections.append("## ⏰ 일정\n\n> Calendar 연동 미설정\n\n")

    sections.append(f"## 🟡 FOCUS (Due ≤ {days}일, 최근 {recent_days}일 갱신)\n\n")
    sections.append(
        _strip_markdown_header(render_focus_markdown(con, days=days, recent_days=recent_days)).rstrip() + "\n\n"
    )

    if calendar_ready:
        try:
            uni_md = render_unified_markdown(
                calendar_id=primary_calendar_id,
                client_secret_path=client_secret_path,
                token_path=token_path,
                con=con,
                days=days,
            )
            sections.append("## 📋 우선순위 전체 뷰\n\n")
            sections.append(_strip_markdown_header(uni_md).rstrip() + "\n")
        except Exception as exc:
            sections.append(f"## 📋 우선순위 전체 뷰\n\n> 렌더링 오류: {exc}\n")
    else:
        sections.append("## 📋 우선순위 전체 뷰\n\n> Calendar 연동 미설정\n")

    return "".join(sections)


def sync_dashboard_note(
    con: sqlite3.Connection,
    *,
    primary_calendar_id: str | None = None,
    client_secret_path: Path | None = None,
    token_path: Path | None = None,
    days: int = 7,
    recent_days: int = DEFAULT_RECENT_DAYS,
) -> None:
    DASHBOARD_PATH.write_text(
        render_dashboard_markdown(
            con,
            primary_calendar_id=primary_calendar_id,
            client_secret_path=client_secret_path,
            token_path=token_path,
            days=days,
            recent_days=recent_days,
        ),
        encoding="utf-8",
    )


def cmd_complete(args: argparse.Namespace) -> int:
    con = db_connect(Path(args.db))
    init_db(con)

    task_path = find_task_path(con, args.slug)
    if task_path is None:
        print(f"task not found: {args.slug}")
        return 1

    update_markdown_checkboxes(task_path, mode="complete", subtask_ord=args.subtask)

    now = dt.date.today().isoformat()
    content = task_path.read_text(encoding="utf-8")
    content = update_frontmatter_value(content, "updated", now)
    if args.subtask is None:
        content = update_frontmatter_value(content, "status", "done")
    task_path.write_text(content, encoding="utf-8")

    refresh_single_task(con, task_path)
    sync_now_note(con)
    sync_focus_note(con)
    sync_dashboard_note(con)
    con.commit()
    print(f"completed: {args.slug}" + (f" (subtask #{args.subtask})" if args.subtask else ""))
    return 0


def cmd_reopen(args: argparse.Namespace) -> int:
    con = db_connect(Path(args.db))
    init_db(con)

    task_path = find_task_path(con, args.slug)
    if task_path is None:
        print(f"task not found: {args.slug}")
        return 1

    update_markdown_checkboxes(task_path, mode="reopen", subtask_ord=args.subtask)

    now = dt.date.today().isoformat()
    content = task_path.read_text(encoding="utf-8")
    content = update_frontmatter_value(content, "updated", now)
    content = update_frontmatter_value(content, "status", "open")
    task_path.write_text(content, encoding="utf-8")

    refresh_single_task(con, task_path)
    sync_now_note(con)
    sync_focus_note(con)
    sync_dashboard_note(con)
    con.commit()
    print(f"reopened: {args.slug}" + (f" (subtask #{args.subtask})" if args.subtask else ""))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    con = db_connect(Path(args.db))
    init_db(con)

    task_path = find_task_path(con, args.slug)
    if task_path is None:
        print(f"task not found: {args.slug}")
        return 1

    content = task_path.read_text(encoding="utf-8")

    updates: list[tuple[str, str]] = []
    removals: list[str] = []

    if args.status:
        updates.append(("status", args.status))
    if args.due is not None:
        updates.append(("due", args.due))
    if args.not_before is not None:
        not_before = args.not_before
        # Quote a time value (`2026-10-12 17:00`) like if_then so a YAML reader
        # keeps it a string. Date-only values stay bare (unchanged bytes).
        if ":" in not_before and not not_before.startswith(('"', "'")):
            not_before = f'"{not_before}"'
        updates.append(("not_before", not_before))
    if args.next_action is not None:
        updates.append(("next_action", args.next_action))
    if args.outcome is not None:
        updates.append(("outcome", args.outcome))
    if args.if_then is not None:
        updates.append(("if_then", f'"{args.if_then}"'))
    if args.estimate_p50 is not None:
        updates.append(("estimate_p50", args.estimate_p50))
    if args.estimate_p90 is not None:
        updates.append(("estimate_p90", args.estimate_p90))
    if args.review_cycle is not None:
        updates.append(("review_cycle", args.review_cycle))

    if args.clear_due:
        removals.append("due")
    if args.clear_not_before:
        removals.append("not_before")
    if args.clear_next_action:
        removals.append("next_action")
    if args.clear_outcome:
        removals.append("outcome")
    if args.clear_if_then:
        removals.append("if_then")
    if args.clear_estimate_p50:
        removals.append("estimate_p50")
    if args.clear_estimate_p90:
        removals.append("estimate_p90")
    if args.clear_review_cycle:
        removals.append("review_cycle")

    if not updates and not removals:
        print("no updates requested")
        return 1

    for key in removals:
        content = remove_frontmatter_key(content, key)
    for key, value in updates:
        content = update_frontmatter_value(content, key, value)

    content = update_frontmatter_value(content, "updated", dt.date.today().isoformat())
    task_path.write_text(content, encoding="utf-8")

    refresh_single_task(con, task_path)
    sync_now_note(con)
    sync_focus_note(con)
    sync_dashboard_note(con)
    con.commit()

    changed_keys = [k for k, _ in updates] + [f"-{k}" for k in removals]
    print(f"updated: {args.slug} ({', '.join(changed_keys)})")
    return 0


def parse_task_file(path: Path) -> ParsedTask:
    content = path.read_text(encoding="utf-8")
    frontmatter, body = parse_frontmatter(content)

    title = path.stem
    for line in body.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break

    subtasks: list[tuple[str, int]] = []
    for line in body.splitlines():
        match = CHECKBOX_RE.match(line)
        if not match:
            continue
        is_done = 1 if match.group(1).lower() == "x" else 0
        text = match.group(2).strip()
        subtasks.append((text, is_done))

    done_subtasks = sum(done for _, done in subtasks)
    total_subtasks = len(subtasks)

    def fm_value(key: str) -> str | None:
        value = frontmatter.get(key)
        if isinstance(value, list):
            return ", ".join(value)
        return value

    # Extract tags from frontmatter (list)
    tags_raw = frontmatter.get("tags")
    if isinstance(tags_raw, list):
        tags = [str(t).strip() for t in tags_raw if t]
    elif isinstance(tags_raw, str) and tags_raw.strip():
        tags = [tags_raw.strip()]
    else:
        tags = []

    return ParsedTask(
        slug=path.stem,
        path=str(path.relative_to(ROOT)),
        title=title,
        status=fm_value("status") or "open",
        due=fm_value("due"),
        not_before=fm_value("not_before"),
        updated=fm_value("updated"),
        outcome=fm_value("outcome"),
        next_action=fm_value("next_action"),
        if_then=fm_value("if_then"),
        estimate_p50=fm_value("estimate_p50"),
        estimate_p90=fm_value("estimate_p90"),
        review_cycle=fm_value("review_cycle"),
        priority=_parse_priority(fm_value("priority")),
        total_subtasks=total_subtasks,
        done_subtasks=done_subtasks,
        subtasks=subtasks,
        parent_slug=_parse_parent_slug(fm_value("parent")),
        is_parent=1 if str(fm_value("is_parent") or "").strip().lower() == "true" else 0,
        tags=tags,
        worker_mode=(fm_value("worker_mode") or "").strip().lower() or None,
        auto_merge=0 if str(fm_value("auto_merge")).strip().lower() in ("false", "0", "no") else 1,
    )


def _parse_priority(raw: str | None) -> float | None:
    if raw is None or raw == "" or raw == "-":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


_PARENT_LINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]")


def _parse_parent_slug(raw: str | None) -> str | None:
    if not raw or raw == "-":
        return None
    match = _PARENT_LINK_RE.search(raw)
    if not match:
        return None
    target = match.group(1).strip()
    # Accept either bare slug or full path like "Todo/items/private/withdrawal-process"
    return target.rsplit("/", 1)[-1]


def parse_lanes() -> dict[str, str]:
    """Parse Todo/Todo.md for lane assignments."""
    lane_by_slug: dict[str, str] = {}
    todo_path = TODO_DIR / "Todo.md"
    if not todo_path.exists():
        return lane_by_slug

    current_lane = ""
    for line in todo_path.read_text(encoding="utf-8").splitlines():
        if line in WORK_SECTION_TO_LANE:
            current_lane = WORK_SECTION_TO_LANE[line]
            continue
        if line.startswith("## ") and line not in WORK_SECTION_TO_LANE:
            current_lane = ""
            continue
        if current_lane == "":
            continue
        match = LINK_RE.search(line)
        if match:
            lane_by_slug[match.group(1)] = current_lane

    return lane_by_slug


def iter_task_files() -> Iterable[Path]:
    """Iterate over all task files in Todo/items/."""
    if not ITEMS_DIR.exists():
        return
    for path in sorted(ITEMS_DIR.glob("*.md")):
        yield path


def collect_watch_files() -> list[Path]:
    """Collect files to watch for changes (index + all task files)."""
    files: list[Path] = []
    todo_index = TODO_DIR / "Todo.md"
    if todo_index.exists():
        files.append(todo_index)

    for path in iter_task_files():
        files.append(path)
    return files


def snapshot_mtime() -> dict[str, float]:
    snap: dict[str, float] = {}
    for path in collect_watch_files():
        if path.exists():
            snap[str(path)] = path.stat().st_mtime
    return snap


def upsert_task(con: sqlite3.Connection, task: ParsedTask, lane: str | None) -> None:
    now = dt.datetime.now().isoformat(timespec="seconds")
    # Resolve priority: frontmatter overrides; fallback to lane default; else 50
    priority = task.priority
    if priority is None:
        priority = LANE_DEFAULT_PRIORITY.get(lane or "", 50.0)
    # Serialize tags as comma-separated string for SQLite
    tags_str = ",".join(task.tags) if task.tags else ""
    con.execute(
        """
        INSERT INTO tasks (
            slug, title, path, lane, status, due, not_before, updated,
            outcome, next_action, if_then, estimate_p50, estimate_p90, review_cycle,
            priority, total_subtasks, done_subtasks, imported_at,
            parent_slug, is_parent, tags, worker_mode, auto_merge
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            title=excluded.title,
            path=excluded.path,
            lane=excluded.lane,
            status=excluded.status,
            due=excluded.due,
            not_before=excluded.not_before,
            updated=excluded.updated,
            outcome=excluded.outcome,
            next_action=excluded.next_action,
            if_then=excluded.if_then,
            estimate_p50=excluded.estimate_p50,
            estimate_p90=excluded.estimate_p90,
            review_cycle=excluded.review_cycle,
            priority=excluded.priority,
            total_subtasks=excluded.total_subtasks,
            done_subtasks=excluded.done_subtasks,
            imported_at=excluded.imported_at,
            parent_slug=excluded.parent_slug,
            is_parent=excluded.is_parent,
            tags=excluded.tags,
            worker_mode=excluded.worker_mode,
            auto_merge=excluded.auto_merge
        """,
        (
            task.slug,
            task.title,
            task.path,
            lane,
            task.status,
            task.due,
            task.not_before,
            task.updated,
            task.outcome,
            task.next_action,
            task.if_then,
            task.estimate_p50,
            task.estimate_p90,
            task.review_cycle,
            priority,
            task.total_subtasks,
            task.done_subtasks,
            now,
            task.parent_slug,
            task.is_parent,
            tags_str,
            task.worker_mode,
            task.auto_merge,
        ),
    )

    task_row = con.execute("SELECT id FROM tasks WHERE slug = ?", (task.slug,)).fetchone()
    if task_row is None:
        return

    task_id = int(task_row["id"])
    con.execute("DELETE FROM subtasks WHERE task_id = ?", (task_id,))
    con.executemany(
        "INSERT INTO subtasks(task_id, ord, content, is_done) VALUES(?, ?, ?, ?)",
        [(task_id, idx, text, done) for idx, (text, done) in enumerate(task.subtasks, start=1)],
    )


def import_markdown_to_db(con: sqlite3.Connection) -> int:
    lane_map = parse_lanes()
    imported_slugs: set[str] = set()
    count = 0
    for path in iter_task_files():
        try:
            task = parse_task_file(path)
        except FileNotFoundError:
            print(f"import-md: skipped vanished {path}")
            continue
        lane = lane_map.get(task.slug)
        upsert_task(con, task, lane)
        imported_slugs.add(task.slug)
        count += 1

    # Remove DB entries whose markdown files no longer exist
    all_db_slugs = [
        row[0] for row in con.execute("SELECT slug FROM tasks").fetchall()
    ]
    for slug in all_db_slugs:
        if slug not in imported_slugs:
            con.execute("DELETE FROM tasks WHERE slug = ?", (slug,))

    sync_now_note(con)
    sync_focus_note(con)
    sync_dashboard_note(con)
    con.commit()
    return count


def cmd_init(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = db_connect(db_path)
    init_db(con)
    print(f"initialized db: {db_path}")
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    con = db_connect(db_path)
    init_db(con)

    count = import_markdown_to_db(con)
    print(f"imported tasks: {count}")

    # Worker-contract problems are silent at runtime, so surface them at the
    # one moment every task file passes through. Non-fatal by default: a bad
    # whitelist should not block syncing unrelated tasks.
    findings = _task_validate.validate_dir(ITEMS_DIR)
    if findings:
        errors = sum(
            1 for issues in findings.values() for i in issues if i.level == _task_validate.ERROR
        )
        print(f"\n[worker contract] {len(findings)} open task(s) with issues, {errors} error(s):")
        for slug, issues in findings.items():
            for issue in issues:
                print(f"- {issue.format(slug)}")
        if getattr(args, "strict", False) and errors:
            return 1

    if args.calendar_sync:
        try:
            created_or_updated, skipped = sync_due_tasks_to_calendar(
                con=con,
                calendar_id=args.calendar_id,
                client_secret_path=Path(args.gcal_client_secret),
                token_path=Path(args.gcal_token),
            )
            print(
                f"calendar sync complete: {created_or_updated} upserted, {skipped} skipped "
                f"(calendar={args.calendar_id})"
            )
        except FileNotFoundError as exc:
            print(f"calendar sync pending: {exc}")

    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    if args.slug:
        path = ITEMS_DIR / f"{args.slug}.md"
        if not path.exists():
            print(f"no such task: {path}")
            return 1
        findings = {args.slug: _task_validate.validate_file(path)}
        findings = {k: v for k, v in findings.items() if v}
    else:
        findings = _task_validate.validate_dir(ITEMS_DIR, only_open=not args.all)

    if not findings:
        scope = "all" if args.all else "open"
        print(f"worker contract OK ({scope} tasks)")
        return 0

    errors = 0
    for slug, issues in findings.items():
        for issue in issues:
            print(issue.format(slug))
            errors += issue.level == _task_validate.ERROR
    print(f"\n{len(findings)} task(s), {errors} error(s)")
    return 1 if errors else 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Reserve a conditional deferred resume for a parked/failed task.

    The manual alternative this replaces is hand-editing
    `Worker/assignments/<slug>.json` (or babysitting a `/tmp` polling script) to
    say `refail_count=0, state=failed, blocked_on=''` once the vault has caught
    up — surgery on the orchestrator's own durable state, in the exact window
    where a tick may be writing it. Reserve the intent instead and let the tick
    decide when the conditions hold.

    Thin delegation to `orchestrator.resume`, like `session-end` does
    (`_session_end.cmd_session_end`): this file stays a parser + dispatcher.
    `ROOT` is read at call time so tests can point it at a tmp vault.
    """
    from .orchestrator import resume as _resume

    try:
        path = _resume.schedule(ROOT, args.slug, after_commit=args.after_commit,
                                expire_hours=args.expire_hours, note=args.note or "")
    except _resume.ResumeError as e:
        print(f"resume 예약 거부: {e}")
        return 2
    sha = (args.after_commit or "").strip()
    ancestry = f"{sha}가 HEAD의 조상" if sha else "커밋 조건 없음"
    print(f"resume 예약: {path}")
    print(f"  조건: 라이브 체크아웃 HEAD가 main + {ancestry} "
          f"+ state가 awaiting_human/failed")
    print(f"  만료: {args.expire_hours}h 뒤 (만료되면 예약만 지워지고 경보 1회)")
    print("  다음 오케스트레이터 틱이 조건을 판정합니다 — assignment json은 건드리지 마세요.")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    """Scaffold a task file from a template so the worker fields start valid."""
    template = TODO_DIR / "templates" / f"{args.template}-task-template.md"
    if not template.exists():
        available = sorted(p.stem.replace("-task-template", "") for p in (TODO_DIR / "templates").glob("*-task-template.md"))
        print(f"no template {args.template!r}; available: {', '.join(available)}")
        return 1

    dest = ITEMS_DIR / f"{args.slug}.md"
    if dest.exists():
        print(f"already exists: {dest}")
        return 1

    content = template.read_text(encoding="utf-8")
    today = dt.date.today().isoformat()
    content = content.replace("updated: 2026-05-19", f"updated: {today}")
    content = content.replace("due: YYYY-MM-DD", 'due: "-"')
    content = content.replace("worker_branch: \"worker/<slug>\"", f'worker_branch: "worker/{args.slug}"')
    content = content.replace("# 작업 제목", f"# {args.title or args.slug}")

    dest.write_text(content, encoding="utf-8")
    print(f"created {dest}")

    issues = _task_validate.validate_file(dest)
    for issue in issues:
        print(issue.format(args.slug))
    print("\nFill in outcome / next_action / if_then, then run: import-md")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = db_connect(db_path)
    init_db(con)

    # Heartbeat: marks this process's writes as in-flight so the restart gate
    # (worker._todo_sync_idle) can tell "mid-sync" from "sleeping between
    # ticks" — the process tree cannot (see that gate's docstring). Started
    # before the first write, cleared after the last; a crash in between
    # leaves a file the gate's stale cutoff disarms in 10 minutes.
    _worker.todo_sync_heartbeat_start()
    count = import_markdown_to_db(con)
    print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] initial sync: {count} tasks")
    calendar_sync_enabled = bool(args.calendar_sync)

    if calendar_sync_enabled:
        try:
            created_or_updated, skipped = sync_due_tasks_to_calendar(
                con=con,
                calendar_id=args.calendar_id,
                client_secret_path=Path(args.gcal_client_secret),
                token_path=Path(args.gcal_token),
            )
            print(
                f"[{dt.datetime.now().isoformat(timespec='seconds')}] calendar sync: "
                f"{created_or_updated} upserted, {skipped} skipped"
            )
        except Exception as exc:
            print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] calendar sync warning: {exc}")
            if isinstance(exc, FileNotFoundError):
                print(
                    f"[{dt.datetime.now().isoformat(timespec='seconds')}] calendar sync disabled until credentials are provided"
                )
                calendar_sync_enabled = False

    prev_snapshot = snapshot_mtime()
    prev_date = dt.date.today()
    _worker.todo_sync_heartbeat_clear()
    print(
        f"[{dt.datetime.now().isoformat(timespec='seconds')}] watching files (interval={args.interval}s)"
    )

    try:
        while True:
            time.sleep(args.interval)
            current = snapshot_mtime()
            today = dt.date.today()
            date_rolled = today != prev_date
            if date_rolled:
                print(
                    f"[{dt.datetime.now().isoformat(timespec='seconds')}] date rolled over {prev_date} -> {today}; forcing regen"
                )
                prev_date = today
            if current != prev_snapshot or date_rolled:
                _worker.todo_sync_heartbeat_start()
                count = import_markdown_to_db(con)
                print(
                    f"[{dt.datetime.now().isoformat(timespec='seconds')}] change detected -> synced {count} tasks"
                )
                if calendar_sync_enabled:
                    try:
                        created_or_updated, skipped = sync_due_tasks_to_calendar(
                            con=con,
                            calendar_id=args.calendar_id,
                            client_secret_path=Path(args.gcal_client_secret),
                            token_path=Path(args.gcal_token),
                        )
                        print(
                            f"[{dt.datetime.now().isoformat(timespec='seconds')}] calendar sync: "
                            f"{created_or_updated} upserted, {skipped} skipped"
                        )
                    except Exception as exc:
                        print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] calendar sync warning: {exc}")
                        if isinstance(exc, FileNotFoundError):
                            print(
                                f"[{dt.datetime.now().isoformat(timespec='seconds')}] calendar sync disabled until credentials are provided"
                            )
                            calendar_sync_enabled = False

                    try:
                        cal_md = render_calendar_markdown(
                            calendar_id=args.primary_calendar_id,
                            client_secret_path=Path(args.gcal_client_secret),
                            token_path=Path(args.gcal_token),
                            days=7,
                        )
                        CALENDAR_PATH.write_text(cal_md, encoding="utf-8")
                        uni_md = render_unified_markdown(
                            calendar_id=args.primary_calendar_id,
                            client_secret_path=Path(args.gcal_client_secret),
                            token_path=Path(args.gcal_token),
                            con=con,
                            days=7,
                        )
                        UNIFIED_PATH.write_text(uni_md, encoding="utf-8")
                        sync_dashboard_note(
                            con,
                            primary_calendar_id=args.primary_calendar_id,
                            client_secret_path=Path(args.gcal_client_secret),
                            token_path=Path(args.gcal_token),
                            days=7,
                        )
                        print(
                            f"[{dt.datetime.now().isoformat(timespec='seconds')}] dashboard+unified+calendar regenerated"
                        )
                    except Exception as exc:
                        print(f"[{dt.datetime.now().isoformat(timespec='seconds')}] dashboard regen warning: {exc}")

                # M3-a integration: keep Memory/relations.sqlite + Memory/index.md fresh.
                try:
                    gm_con = _graph_memory.init_relations_db()
                    try:
                        _graph_memory.scan_vault_and_index(con=gm_con)
                        index_md = _graph_memory.render_index_markdown(gm_con)
                    finally:
                        gm_con.close()
                    _graph_memory.INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
                    _graph_memory.INDEX_PATH.write_text(index_md, encoding="utf-8")
                except Exception as exc:
                    print(
                        f"[{dt.datetime.now().isoformat(timespec='seconds')}] graph_memory warning: {exc}"
                    )

                # M4-a integration: pick up worker_mode:auto tasks added since last cycle.
                try:
                    _worker.scan_queue()
                except Exception as exc:
                    print(
                        f"[{dt.datetime.now().isoformat(timespec='seconds')}] worker scan warning: {exc}"
                    )

                prev_snapshot = current
                _worker.todo_sync_heartbeat_clear()
    except KeyboardInterrupt:
        print("daemon stopped")

    return 0


def cmd_list(args: argparse.Namespace) -> int:
    con = db_connect(Path(args.db), require_schema=True)
    where = []
    params: list[str] = []

    if args.tag:
        # tags is stored as comma-separated, use LIKE for simple matching
        where.append("tags LIKE ?")
        params.append(f"%{args.tag}%")
    if args.status and args.status != "all":
        where.append("status = ?")
        params.append(args.status)
    if args.lane:
        where.append("lane = ?")
        params.append(args.lane)
    human_modes_placeholder = ", ".join("?" for _ in HUMAN_OVERRIDE_WORKER_MODES)
    if args.who == "worker":
        where.append(f"LOWER(COALESCE(worker_mode, '')) NOT IN ({human_modes_placeholder})")
        params.extend(HUMAN_OVERRIDE_WORKER_MODES)
    elif args.who == "human":
        where.append(f"LOWER(COALESCE(worker_mode, '')) IN ({human_modes_placeholder})")
        params.extend(HUMAN_OVERRIDE_WORKER_MODES)

    sql = "SELECT slug, lane, status, due, title, next_action, total_subtasks, done_subtasks, tags, worker_mode FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY CASE WHEN due IS NULL THEN 1 ELSE 0 END, due, title"

    rows = con.execute(sql, params).fetchall()
    if not rows:
        print("no tasks found")
        return 0

    for row in rows:
        progress = f"{row['done_subtasks']}/{row['total_subtasks']}"
        tags_str = row['tags'] or ''
        owner = f"worker:{row['worker_mode']}" if _is_worker_owned(row["worker_mode"]) else "human"
        print(
            f"- {row['title']} | slug={row['slug']} | lane={row['lane'] or '-'} | "
            f"status={row['status']} | due={row['due'] or '-'} | progress={progress} | "
            f"tags={tags_str} | owner={owner}"
        )
        if args.verbose and row["next_action"]:
            print(f"  next_action: {row['next_action']}")
    return 0


def cmd_today(args: argparse.Namespace) -> int:
    con = db_connect(Path(args.db), require_schema=True)
    today = dt.date.today().isoformat()
    rows = con.execute(
        """
        SELECT title, slug, due, lane, next_action
        FROM tasks
        WHERE status = 'open'
          AND due IS NOT NULL
          AND due != '-'
          AND due <= ?
        ORDER BY due, lane, title
        """,
        (today,),
    ).fetchall()

    if not rows:
        print("no due tasks up to today")
        return 0

    print(f"due tasks up to {today}:")
    for row in rows:
        print(f"- {row['due']} | {row['title']} (lane={row['lane'] or '-'}, slug={row['slug']})")
        if row["next_action"]:
            print(f"  next_action: {row['next_action']}")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    con = db_connect(Path(args.db), require_schema=True)
    rows = con.execute(
        """
        SELECT slug, title, lane, status, outcome, next_action, if_then, estimate_p50, estimate_p90
        FROM tasks
        WHERE status = 'open'
        ORDER BY title
        """
    ).fetchall()

    required_missing = []
    for row in rows:
        missing = [
            key
            for key in ["outcome", "next_action", "if_then", "estimate_p50", "estimate_p90"]
            if not row[key]
        ]
        if missing:
            required_missing.append((row["slug"], row["title"], missing))

    print("[field audit]")
    if not required_missing:
        print("- all open tasks have required evidence-based fields")
    else:
        for slug, title, missing in required_missing:
            print(f"- {title} ({slug}) missing: {', '.join(missing)}")

    print("\n[wip audit]")
    lane_counts = dict(
        con.execute(
            "SELECT lane, COUNT(*) AS cnt FROM tasks WHERE status='open' GROUP BY lane"
        ).fetchall()
    )
    for lane, limit in WIP_LIMITS.items():
        cnt = int(lane_counts.get(lane, 0))
        state = "OK" if cnt <= limit else "VIOLATION"
        print(f"- {lane}: {cnt}/{limit} -> {state}")

    return 0


def cmd_weekly_review(args: argparse.Namespace) -> int:
    con = db_connect(Path(args.db), require_schema=True)
    rows = con.execute(
        """
        SELECT lane, title, done_subtasks, total_subtasks, estimate_p50, estimate_p90, next_action, tags
        FROM tasks
        WHERE status = 'open'
        ORDER BY lane, title
        """
    ).fetchall()

    if not rows:
        print("no open tasks")
        return 0

    print("weekly review snapshot")
    for row in rows:
        total = int(row["total_subtasks"])
        done = int(row["done_subtasks"])
        ratio = f"{done}/{total}" if total > 0 else "-"
        tags_str = row['tags'] or ''
        print(
            f"- {row['title']} | lane={row['lane'] or '-'} | tags={tags_str} | "
            f"progress={ratio} | p50={row['estimate_p50'] or '-'} | p90={row['estimate_p90'] or '-'}"
        )
        if row["next_action"]:
            print(f"  next_action: {row['next_action']}")

    return 0


def cmd_now(args: argparse.Namespace) -> int:
    con = db_connect(Path(args.db))
    init_db(con)
    sync_now_note(con)
    sync_focus_note(con)
    sync_dashboard_note(con)
    print(f"updated now file: {NOW_PATH}")
    print(render_now_markdown(con))
    return 0


def cmd_focus(args: argparse.Namespace) -> int:
    con = db_connect(Path(args.db))
    init_db(con)
    import_markdown_to_db(con)
    sync_focus_note(con, days=args.days, recent_days=args.recent_days)
    sync_dashboard_note(con, days=args.days, recent_days=args.recent_days)
    print(f"updated focus file: {FOCUS_PATH}")
    print(render_focus_markdown(con, days=args.days, recent_days=args.recent_days))
    return 0


def cmd_calendar_sync(args: argparse.Namespace) -> int:
    con = db_connect(Path(args.db))
    init_db(con)
    import_markdown_to_db(con)
    try:
        created_or_updated, skipped = sync_due_tasks_to_calendar(
            con=con,
            calendar_id=args.calendar_id,
            client_secret_path=Path(args.gcal_client_secret),
            token_path=Path(args.gcal_token),
        )
        print(
            f"calendar sync complete: {created_or_updated} upserted, {skipped} skipped "
            f"(calendar={args.calendar_id})"
        )
        return 0
    except FileNotFoundError as exc:
        print(f"calendar sync pending: {exc}")
        return 0


def cmd_calendar_fetch(args: argparse.Namespace) -> int:
    try:
        md = render_calendar_markdown(
            calendar_id=args.calendar_id,
            client_secret_path=Path(args.gcal_client_secret),
            token_path=Path(args.gcal_token),
            days=args.days,
        )
        CALENDAR_PATH.write_text(md, encoding="utf-8")
        print(f"updated calendar file: {CALENDAR_PATH}")
        print(md)
        return 0
    except FileNotFoundError as exc:
        print(f"calendar fetch pending: {exc}")
        return 0


def cmd_unified(args: argparse.Namespace) -> int:
    con = db_connect(Path(args.db))
    init_db(con)
    import_markdown_to_db(con)

    client_secret = Path(args.gcal_client_secret)
    token = Path(args.gcal_token)

    try:
        # 1) Sync todos → TODO calendar
        created_or_updated, skipped = sync_due_tasks_to_calendar(
            con=con,
            calendar_id=DEFAULT_CALENDAR_ID,
            client_secret_path=client_secret,
            token_path=token,
        )
        print(f"calendar sync: {created_or_updated} upserted, {skipped} skipped")

        # 2) Fetch primary calendar → Calendar.md
        cal_md = render_calendar_markdown(
            calendar_id=args.calendar_id,
            client_secret_path=client_secret,
            token_path=token,
            days=args.days,
        )
        CALENDAR_PATH.write_text(cal_md, encoding="utf-8")
        print(f"updated calendar file: {CALENDAR_PATH}")

        # 3) Render unified view
        md = render_unified_markdown(
            calendar_id=args.calendar_id,
            client_secret_path=client_secret,
            token_path=token,
            con=con,
            days=args.days,
        )
        UNIFIED_PATH.write_text(md, encoding="utf-8")
        print(f"updated unified file: {UNIFIED_PATH}")

        sync_dashboard_note(
            con,
            primary_calendar_id=args.calendar_id,
            client_secret_path=client_secret,
            token_path=token,
            days=args.days,
        )
        print(f"updated dashboard file: {DASHBOARD_PATH}")
        print(md)
        return 0
    except FileNotFoundError as exc:
        print(f"unified view pending: {exc}")
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="second-brain todo system (SQLite + Markdown sync)")
    parser.add_argument("--db", default=str(DB_PATH), help="Path to SQLite DB file")

    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-db", help="Initialize SQLite database")
    p_init.set_defaults(func=cmd_init)

    p_import = sub.add_parser("import-md", help="Import markdown todo item files into DB")
    p_import.add_argument("--calendar-sync", action="store_true")
    p_import.add_argument("--calendar-id", default=DEFAULT_CALENDAR_ID)
    p_import.add_argument("--gcal-client-secret", default=str(DEFAULT_GCAL_CLIENT_SECRET_PATH))
    p_import.add_argument("--gcal-token", default=str(DEFAULT_GCAL_TOKEN_PATH))
    p_import.add_argument(
        "--strict", action="store_true", help="Exit non-zero if any worker-contract error is found"
    )
    p_import.set_defaults(func=cmd_import)

    p_validate = sub.add_parser(
        "validate", help="Check task files against the worker contract (allowed_tools, etc.)"
    )
    p_validate.add_argument("slug", nargs="?", help="Validate one task (default: all open tasks)")
    p_validate.add_argument("--all", action="store_true", help="Include done tasks too")
    p_validate.set_defaults(func=cmd_validate)

    p_new = sub.add_parser("new", help="Scaffold a task file from a template")
    p_new.add_argument("slug", help="kebab-case slug (becomes Todo/items/<slug>.md)")
    p_new.add_argument("--title", help="Task title for the body heading")
    p_new.add_argument(
        "--template", default="auto-worker", help="Template name (default: auto-worker)"
    )
    p_new.set_defaults(func=cmd_new)

    p_list = sub.add_parser("list", help="List tasks from DB")
    p_list.add_argument("--tag", help="Filter by tag (partial match)")
    p_list.add_argument("--status", choices=["all", "open", "done", "waiting", "blocked"], default="all")
    p_list.add_argument("--lane", choices=["urgent", "this_week", "long_project", "backlog"])
    p_list.add_argument(
        "--who",
        choices=["all", "human", "worker"],
        default="all",
        help="Filter by owner: human (worker_mode not in auto/delegate) or worker (auto/delegate)",
    )
    p_list.add_argument("--verbose", action="store_true")
    p_list.set_defaults(func=cmd_list)

    p_today = sub.add_parser("today", help="Show due tasks up to today")
    p_today.set_defaults(func=cmd_today)

    p_audit = sub.add_parser("audit", help="Audit required fields + WIP limits")
    p_audit.set_defaults(func=cmd_audit)

    p_weekly = sub.add_parser("weekly-review", help="Print weekly review snapshot")
    p_weekly.set_defaults(func=cmd_weekly_review)

    p_complete = sub.add_parser("complete", help="Complete a task (or one subtask) by slug")
    p_complete.add_argument("slug", help="Task slug (filename without .md)")
    p_complete.add_argument("--subtask", type=int, help="1-based subtask order to complete")
    p_complete.set_defaults(func=cmd_complete)

    p_reopen = sub.add_parser("reopen", help="Reopen a task (or one subtask) by slug")
    p_reopen.add_argument("slug", help="Task slug (filename without .md)")
    p_reopen.add_argument("--subtask", type=int, help="1-based subtask order to reopen")
    p_reopen.set_defaults(func=cmd_reopen)

    p_set = sub.add_parser("set", help="Set task frontmatter fields by slug")
    p_set.add_argument("slug", help="Task slug (filename without .md)")
    p_set.add_argument("--status", choices=["open", "done", "waiting", "blocked"])
    p_set.add_argument("--due", help="YYYY-MM-DD")
    p_set.add_argument("--not-before",
                       help="YYYY-MM-DD | 'YYYY-MM-DD HH:MM' | YYYY-MM-DDTHH:MM[:SS] "
                            "(host-local; task is not actionable before this moment)")
    p_set.add_argument("--next-action")
    p_set.add_argument("--outcome")
    p_set.add_argument("--if-then")
    p_set.add_argument("--estimate-p50")
    p_set.add_argument("--estimate-p90")
    p_set.add_argument("--review-cycle")

    p_set.add_argument("--clear-due", action="store_true")
    p_set.add_argument("--clear-not-before", action="store_true")
    p_set.add_argument("--clear-next-action", action="store_true")
    p_set.add_argument("--clear-outcome", action="store_true")
    p_set.add_argument("--clear-if-then", action="store_true")
    p_set.add_argument("--clear-estimate-p50", action="store_true")
    p_set.add_argument("--clear-estimate-p90", action="store_true")
    p_set.add_argument("--clear-review-cycle", action="store_true")
    p_set.set_defaults(func=cmd_set)

    p_daemon = sub.add_parser("daemon", help="Watch markdown files and auto-sync DB")
    p_daemon.add_argument("--interval", type=int, default=3, help="Polling interval in seconds")
    p_daemon.add_argument("--calendar-sync", action="store_true")
    p_daemon.add_argument("--calendar-id", default=DEFAULT_CALENDAR_ID)
    p_daemon.add_argument("--primary-calendar-id", default=DEFAULT_PRIMARY_CALENDAR_ID, help="Primary calendar for unified view")
    p_daemon.add_argument("--gcal-client-secret", default=str(DEFAULT_GCAL_CLIENT_SECRET_PATH))
    p_daemon.add_argument("--gcal-token", default=str(DEFAULT_GCAL_TOKEN_PATH))
    p_daemon.set_defaults(func=cmd_daemon)

    p_now = sub.add_parser("now", help="Generate/update Todo/NOW.md with one actionable task")
    p_now.set_defaults(func=cmd_now)

    p_focus = sub.add_parser("focus", help="Generate date-filtered task view (Todo/FOCUS.md)")
    p_focus.add_argument("--days", type=int, default=DEFAULT_FOCUS_DAYS, help="Due-date horizon in days")
    p_focus.add_argument(
        "--recent-days",
        type=int,
        default=DEFAULT_RECENT_DAYS,
        help="Include undated tasks updated within N days",
    )
    p_focus.set_defaults(func=cmd_focus)

    p_gcal = sub.add_parser("calendar-sync", help="Sync due work tasks to Google Calendar")
    p_gcal.add_argument("--calendar-id", default=DEFAULT_CALENDAR_ID)
    p_gcal.add_argument("--gcal-client-secret", default=str(DEFAULT_GCAL_CLIENT_SECRET_PATH))
    p_gcal.add_argument("--gcal-token", default=str(DEFAULT_GCAL_TOKEN_PATH))
    p_gcal.set_defaults(func=cmd_calendar_sync)

    p_gcal_fetch = sub.add_parser("calendar-fetch", help="Fetch upcoming events from Google Calendar into Todo/Calendar.md")
    p_gcal_fetch.add_argument("--calendar-id", default=DEFAULT_PRIMARY_CALENDAR_ID)
    p_gcal_fetch.add_argument("--days", type=int, default=7, help="Number of days ahead to fetch")
    p_gcal_fetch.add_argument("--gcal-client-secret", default=str(DEFAULT_GCAL_CLIENT_SECRET_PATH))
    p_gcal_fetch.add_argument("--gcal-token", default=str(DEFAULT_GCAL_TOKEN_PATH))
    p_gcal_fetch.set_defaults(func=cmd_calendar_fetch)

    p_unified = sub.add_parser("unified", help="Generate unified view (calendar + todos) into Todo/Unified.md")
    p_unified.add_argument("--calendar-id", default=DEFAULT_PRIMARY_CALENDAR_ID)
    p_unified.add_argument("--days", type=int, default=7, help="Number of days ahead to show")
    p_unified.add_argument("--gcal-client-secret", default=str(DEFAULT_GCAL_CLIENT_SECRET_PATH))
    p_unified.add_argument("--gcal-token", default=str(DEFAULT_GCAL_TOKEN_PATH))
    p_unified.set_defaults(func=cmd_unified)

    p_session_end = sub.add_parser(
        "session-end",
        help="Run the §10 session-end checklist (classify → journal → sync report)",
    )
    p_session_end.add_argument("--project", required=True, help="Project slug for the session journal")
    p_session_end.add_argument("--note", help="Freeform note recorded on the journal checkpoint")
    p_session_end.add_argument("--task", action="append", help="Related Todo slug to link (repeatable)")
    p_session_end.add_argument(
        "--dry-run", action="store_true", help="Classify and report only; record/commit nothing"
    )
    p_session_end.set_defaults(func=_session_end.cmd_session_end)

    p_resume = sub.add_parser(
        "resume",
        help="Reserve a conditional deferred resume: re-dispatch a parked/failed "
             "task once the live vault checkout is on main and carries a commit",
    )
    p_resume.add_argument("slug", help="Todo slug to resume (needs an existing assignment)")
    p_resume.add_argument(
        "--after-commit",
        help="Only resume once this sha is an ancestor of the live checkout's HEAD",
    )
    p_resume.add_argument(
        "--expire-hours", type=float, default=24,
        help="Drop the reservation (and alert once) after this many hours (default: 24)",
    )
    p_resume.add_argument("--note", help="Why this resume was reserved")
    p_resume.set_defaults(func=cmd_resume)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
