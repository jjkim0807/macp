"""Happy-path tests for the read-only CLI commands ``list``, ``today``,
``audit`` and ``weekly-review``.

Each command runs against a ``tmp_path`` DB that is initialized with
``init_db`` and seeded with plain ``INSERT``s — never ``Todo/todo.db``. The
commands are driven in-process (``build_parser().parse_args`` →
``args.func``) so coverage of ``todo_system/cli.py`` actually counts them;
one subprocess smoke test keeps the real ``python -m todo_system.cli`` entry
point honest. The uninitialized-DB error path lives in
``test_cli_uninitialized_db.py``.

Due dates are pinned far in the past (2000-01-01) or future (2999-12-31) so
``today`` does not depend on the wall clock.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from todo_system import cli

REPO = Path(__file__).resolve().parents[2]

PAST = "2000-01-01"
FUTURE = "2999-12-31"


def _init(tmp_path: Path) -> Path:
    db = tmp_path / "todo.db"
    con = cli.db_connect(db)
    cli.init_db(con)
    con.close()
    return db


def _seed(db: Path, *rows: dict) -> None:
    con = sqlite3.connect(str(db))
    for row in rows:
        values = {
            "path": f"Todo/items/{row['slug']}.md",
            "imported_at": "2026-01-01T00:00:00",
            "status": "open",
            **row,
        }
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        con.execute(f"INSERT INTO tasks ({cols}) VALUES ({marks})", list(values.values()))
    con.commit()
    con.close()


def _run(db: Path, capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    args = cli.build_parser().parse_args(["--db", str(db), *argv])
    rc = args.func(args)
    return rc, capsys.readouterr().out


def _line_index(out: str, needle: str) -> int:
    for i, line in enumerate(out.splitlines()):
        if needle in line:
            return i
    raise AssertionError(f"{needle!r} not in output:\n{out}")


# ---------------------------------------------------------------- list

ALPHA = {
    "slug": "alpha",
    "title": "Alpha task",
    "lane": "urgent",
    "due": PAST,
    "total_subtasks": 3,
    "done_subtasks": 1,
    "tags": "todo,work",
    "worker_mode": "auto",
    "next_action": "write the test",
}
BETA = {"slug": "beta", "title": "Beta task", "status": "done"}


def test_list_empty_initialized_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _init(tmp_path)

    rc, out = _run(db, capsys, "list")

    assert rc == 0
    assert "no tasks found" in out


def test_list_rows_format_and_default_status_all(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _init(tmp_path)
    _seed(db, ALPHA, BETA)

    rc, out = _run(db, capsys, "list")

    assert rc == 0
    assert (
        "- Alpha task | slug=alpha | lane=urgent | status=open | due=2000-01-01 | "
        "progress=1/3 | tags=todo,work | owner=worker:auto"
    ) in out
    # The default --status is "all", so done rows are listed too.
    assert (
        "- Beta task | slug=beta | lane=- | status=done | due=- | "
        "progress=0/0 | tags= | owner=human"
    ) in out
    # NULL due sorts last.
    assert _line_index(out, "slug=alpha") < _line_index(out, "slug=beta")
    # next_action is only printed with --verbose.
    assert "next_action:" not in out
    assert "no tasks found" not in out


def test_list_status_open_filters_done(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _init(tmp_path)
    _seed(db, ALPHA, BETA)

    rc, out = _run(db, capsys, "list", "--status", "open")

    assert rc == 0
    assert "slug=alpha" in out
    assert "slug=beta" not in out

    rc, out = _run(db, capsys, "list", "--status", "done")

    assert rc == 0
    assert "slug=beta" in out
    assert "slug=alpha" not in out


def test_list_verbose_and_filters(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _init(tmp_path)
    gamma = {
        "slug": "gamma",
        "title": "Gamma task",
        "lane": "backlog",
        "tags": "todo,private",
        "worker_mode": "manual",
    }
    _seed(db, ALPHA, BETA, gamma)

    rc, out = _run(db, capsys, "list", "--verbose")
    assert rc == 0
    assert "  next_action: write the test" in out
    assert _line_index(out, "slug=alpha") + 1 == _line_index(out, "next_action: write the test")
    assert "slug=gamma | lane=backlog" in out and "owner=human" in out

    rc, out = _run(db, capsys, "list", "--lane", "urgent")
    assert rc == 0
    assert "slug=alpha" in out
    assert "slug=beta" not in out and "slug=gamma" not in out

    rc, out = _run(db, capsys, "list", "--tag", "work")
    assert rc == 0
    assert "slug=alpha" in out
    assert "slug=gamma" not in out and "slug=beta" not in out

    rc, out = _run(db, capsys, "list", "--who", "worker")
    assert rc == 0
    assert "slug=alpha" in out
    assert "slug=beta" not in out and "slug=gamma" not in out

    rc, out = _run(db, capsys, "list", "--who", "human")
    assert rc == 0
    assert "slug=beta" in out and "slug=gamma" in out
    assert "slug=alpha" not in out

    rc, out = _run(db, capsys, "list", "--lane", "this_week")
    assert rc == 0
    assert "no tasks found" in out


def test_list_entrypoint_smoke(tmp_path: Path) -> None:
    db = _init(tmp_path)
    _seed(db, ALPHA)

    result = subprocess.run(
        [sys.executable, "-m", "todo_system.cli", "--db", str(db), "list"],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )

    assert result.returncode == 0, result.stderr
    assert "slug=alpha" in result.stdout


# ---------------------------------------------------------------- today


def test_today_empty_initialized_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _init(tmp_path)

    rc, out = _run(db, capsys, "today")

    assert rc == 0
    assert "no due tasks up to today" in out


def test_today_shows_only_open_past_due(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _init(tmp_path)
    _seed(
        db,
        {"slug": "past", "title": "Past task", "due": PAST, "lane": "urgent",
         "next_action": "ship it"},
        {"slug": "past-nolane", "title": "Past no lane", "due": PAST},
        {"slug": "future", "title": "Future task", "due": FUTURE},
        {"slug": "nodue", "title": "No due task"},
        {"slug": "dash", "title": "Dash due task", "due": "-"},
        {"slug": "closed", "title": "Closed task", "due": PAST, "status": "done"},
        {"slug": "gone", "title": "Gone task", "due": PAST, "status": "cancelled"},
    )

    rc, out = _run(db, capsys, "today")

    assert rc == 0
    assert out.startswith("due tasks up to ")
    assert "- 2000-01-01 | Past task (lane=urgent, slug=past)" in out
    assert "  next_action: ship it" in out
    assert "- 2000-01-01 | Past no lane (lane=-, slug=past-nolane)" in out
    for slug in ("future", "nodue", "dash", "closed", "gone"):
        assert f"slug={slug})" not in out
    assert "no due tasks up to today" not in out


def test_today_future_only_is_empty(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _init(tmp_path)
    _seed(db, {"slug": "future", "title": "Future task", "due": FUTURE})

    rc, out = _run(db, capsys, "today")

    assert rc == 0
    assert "no due tasks up to today" in out
    assert "slug=future" not in out


# ---------------------------------------------------------------- audit

COMPLETE_FIELDS = {
    "outcome": "done when tested",
    "next_action": "write the test",
    "if_then": "if seated then write",
    "estimate_p50": "30m",
    "estimate_p90": "60m",
}


def _wip_line(lane: str, count: int, state: str) -> str:
    return f"- {lane}: {count}/{cli.WIP_LIMITS[lane]} -> {state}"


def test_audit_empty_initialized_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _init(tmp_path)

    rc, out = _run(db, capsys, "audit")

    assert rc == 0
    assert "[field audit]" in out
    assert "- all open tasks have required evidence-based fields" in out
    assert "[wip audit]" in out
    for lane in cli.WIP_LIMITS:
        assert _wip_line(lane, 0, "OK") in out
    assert "VIOLATION" not in out
    assert _line_index(out, "[field audit]") < _line_index(out, "[wip audit]")


def test_audit_missing_fields_and_wip_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _init(tmp_path)
    gap_fields = {k: v for k, v in COMPLETE_FIELDS.items() if k not in ("outcome", "if_then")}
    _seed(
        db,
        {"slug": "full", "title": "Full task", "lane": "urgent", **COMPLETE_FIELDS},
        {"slug": "gap", "title": "Gap task", "lane": "this_week", **gap_fields},
        # Non-open rows are not field-audited.
        {"slug": "closed-gap", "title": "Closed gap", "status": "done"},
        {"slug": "gone-gap", "title": "Gone gap", "status": "cancelled"},
    )

    rc, out = _run(db, capsys, "audit")

    assert rc == 0
    assert "- Gap task (gap) missing: outcome, if_then" in out
    assert "(full)" not in out
    assert "(closed-gap)" not in out and "(gone-gap)" not in out
    assert "- all open tasks have required evidence-based fields" not in out
    assert _wip_line("urgent", 1, "OK") in out
    assert _wip_line("this_week", 1, "OK") in out
    assert _wip_line("long_project", 0, "OK") in out


def test_audit_wip_violation_still_rc0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _init(tmp_path)
    rows = [
        {"slug": f"urgent-{i}", "title": f"Urgent {i}", "lane": "urgent", **COMPLETE_FIELDS}
        for i in range(3)
    ]
    rows += [
        {"slug": f"urgent-done-{i}", "title": f"Urgent done {i}", "lane": "urgent",
         "status": "done"}
        for i in range(2)
    ]
    rows += [
        {"slug": f"backlog-{i}", "title": f"Backlog {i}", "lane": "backlog", **COMPLETE_FIELDS}
        for i in range(5)
    ]
    _seed(db, *rows)

    rc, out = _run(db, capsys, "audit")

    # A WIP violation is reported, not signalled through the exit code.
    assert rc == 0
    assert "- all open tasks have required evidence-based fields" in out
    assert _wip_line("urgent", 3, "VIOLATION") in out
    assert _wip_line("this_week", 0, "OK") in out
    wip_section = out.split("[wip audit]", 1)[1]
    assert "backlog" not in wip_section


# ---------------------------------------------------------------- weekly-review


def test_weekly_review_empty_initialized_db(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _init(tmp_path)

    rc, out = _run(db, capsys, "weekly-review")

    assert rc == 0
    assert "no open tasks" in out


def test_weekly_review_rows(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _init(tmp_path)
    _seed(
        db,
        {"slug": "wa", "title": "Weekly A", "lane": "this_week", "tags": "todo",
         "total_subtasks": 4, "done_subtasks": 2, "estimate_p50": "30m",
         "estimate_p90": "60m", "next_action": "review the plan"},
        {"slug": "wb", "title": "Weekly B"},
        {"slug": "wc", "title": "Weekly closed", "status": "done", "lane": "this_week"},
        {"slug": "wd", "title": "Weekly gone", "status": "cancelled", "lane": "this_week"},
    )

    rc, out = _run(db, capsys, "weekly-review")

    assert rc == 0
    assert out.startswith("weekly review snapshot")
    assert "- Weekly A | lane=this_week | tags=todo | progress=2/4 | p50=30m | p90=60m" in out
    assert "  next_action: review the plan" in out
    assert "- Weekly B | lane=- | tags= | progress=- | p50=- | p90=-" in out
    assert "Weekly closed" not in out
    assert "Weekly gone" not in out
    assert "no open tasks" not in out


def test_weekly_review_done_only_is_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = _init(tmp_path)
    _seed(db, {"slug": "wc", "title": "Weekly closed", "status": "done"})

    rc, out = _run(db, capsys, "weekly-review")

    assert rc == 0
    assert "no open tasks" in out
    assert "Weekly closed" not in out


# ---------------------------------------------------------------- queue

QUEUE_ROW_KEYS = ["slug", "title", "project", "group", "worker_mode", "priority",
                  "checkout", "wait", "wait_text", "holder", "since"]


def _queue_vault(tmp_path: Path) -> Path:
    """`<tmp>/Todo/todo.db` + a catch-all registry + an empty ledger: the layout
    `queue` derives the vault from (db -> parent.parent)."""
    for d in ("Todo/items", "Worker/assignments", "Worker/agents", "Worker/runs"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "Worker/agents/registry.yaml").write_text(
        "agents:\n  - id: w\n    kind: ai-local\n    projects: ['*']\n", encoding="utf-8")
    db = tmp_path / "Todo" / "todo.db"
    con = cli.db_connect(db)
    cli.init_db(con)
    con.close()
    _seed(db,
          {"slug": "qx-auto", "title": "Queued auto", "worker_mode": "auto",
           "path": str(tmp_path / "Todo/items/qx-auto.md")},
          {"slug": "qx-later", "title": "Scheduled", "worker_mode": "auto",
           "not_before": "2999-01-01", "path": str(tmp_path / "Todo/items/qx-later.md")})
    return db


def test_queue_prints_the_three_blocks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = _queue_vault(tmp_path)

    rc, out = _run(db, capsys, "queue")

    assert rc == 0
    assert "큐 (1)" in out
    assert "⏳ 예약됨 (1)" in out
    assert "🔗 선행 대기 (0)" in out
    assert "qx-auto" in out and "next_tick" in out
    assert "qx-later" in out and "2999-01-01" in out
    assert "(없음)" in out


def test_queue_json_is_the_dashboard_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    db = _queue_vault(tmp_path)

    rc, out = _run(db, capsys, "queue", "--json")

    assert rc == 0
    got = json.loads(out)
    assert sorted(got) == ["queue", "queue_blocked", "queue_upcoming"]
    assert [list(r) for r in got["queue"]] == [QUEUE_ROW_KEYS]
    assert got["queue"][0]["slug"] == "qx-auto"
    assert got["queue_upcoming"][0]["slug"] == "qx-later"
    assert got["queue_blocked"] == []
    # one row builder: the CLI prints exactly what the dashboard's collector returns
    dash = cli._load_dashboard()
    assert got == dash.collect_queue(
        db=db, assign=tmp_path / "Worker/assignments",
        registry=tmp_path / "Worker/agents/registry.yaml")
