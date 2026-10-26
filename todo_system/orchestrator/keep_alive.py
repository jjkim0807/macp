"""Keep-alive projects — a full queue must not starve a research chain.

Why: scripts/planner_tick.sh skips the planner when the actionable auto-queue is
at the depth limit, and backs off for EXHAUST_HOURS after an unchanged
open-task state. Both gates are about the queue as a whole. A project the vault
policy says must never park (a standing research chain) can therefore go dry:
other projects fill the queue, its last task closes, and nothing ever creates
the next one.

The vault policy names such projects by task-slug prefix:

    keep_alive_projects:
      - prefix: "proj-a-"
        decision: "Decisions/<date>-proj-a-....md"   # optional

An entry is **starved** when no actionable task's slug starts with its prefix.
Actionable is the same rule Gate 1 counts as queue depth (ACTIONABLE_SQL minus
failed ledger rows), so the rule lives here once and the tick calls it for both.
When anything is starved the tick bypasses only the depth skip and the
exhausted backoff (never the HQ host, PAUSED, usage guard or daily budget
gates) and injects render_context()'s block into the planner prompt.

Fail-safe: a missing, empty or unparseable key, or any error reading the
policy, the DB or the ledger, means "nothing starved". emit-context then prints
nothing, the tick behaves exactly as before and the prompt stays byte-identical.

CLI (planner_tick.sh; always exits 0):
    python -m todo_system.orchestrator.keep_alive queue-depth    # count; 0 on error
    python -m todo_system.orchestrator.keep_alive emit-context   # block; empty on error
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .. import paths

# Vault-data root, as planner_mode / followups resolve it.
_ROOT = paths.vault_root(start=paths.vault_search_start())
POLICY_PATH = _ROOT / "Worker" / "planner_policy.yaml"
DB_PATH = _ROOT / "Todo" / "todo.db"
ASSIGNMENTS_DIR = _ROOT / "Worker" / "assignments"

ACTIONABLE_MODES = ("auto", "delegate", "discover")

ACTIONABLE_SQL = (
    "SELECT slug FROM tasks WHERE status='open' AND is_parent=0 "
    "AND worker_mode IN ('auto','delegate','discover') "
    # A task deferred by not_before cannot run yet, so it is not queue depth:
    # two tasks held to a future date blocked aggressive mode (queue 2 > 0)
    # and kept the planner from refilling a dry queue (2026-09-27).
    # Date-only = 00:00 that day, a time host-local (mirrors
    # todo_system/not_before.py): `YYYY-MM-DD[ |T]HH:MM[:SS]` vs local now.
    "AND NOT (COALESCE(not_before,'') GLOB '[0-9][0-9][0-9][0-9]-*' "
    "AND replace(trim(not_before),'T',' ') > "
    "strftime('%Y-%m-%d %H:%M:%S','now','localtime'))")

HEADING = "## keep-alive starved 프로젝트 (planner_tick 주입)"


def failed_task_ids(assignments_dir: Path | None = None) -> set[str]:
    """task_ids of ledger rows in state `failed`. A stalled (failed+cooldown)
    task is not actionable work, and must NOT block the planner: otherwise a
    task that failed on a red check would keep the planner from creating the
    fix that unblocks it. Unreadable files are skipped."""
    d = Path(assignments_dir) if assignments_dir else ASSIGNMENTS_DIR
    failed: set[str] = set()
    for f in sorted(d.glob("*.json")):
        try:
            row = json.loads(f.read_text(encoding="utf-8"))
            if row.get("state") == "failed":
                failed.add(row.get("task_id"))
        except Exception:
            pass
    return failed


def actionable_slugs(db_path: Path | None = None,
                     assignments_dir: Path | None = None) -> set[str]:
    """Open actionable task slugs minus failed ones. The DB is opened read-only
    so a missing one is never created; errors propagate to the caller."""
    db = Path(db_path) if db_path else DB_PATH
    con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        open_auto = {r[0] for r in con.execute(ACTIONABLE_SQL)}
    finally:
        con.close()
    return open_auto - failed_task_ids(assignments_dir)


def load_policy(path: Path | None = None) -> list[dict]:
    """`keep_alive_projects` entries as [{prefix, decision}]; [] when the file,
    the key or its value is missing, empty, malformed or unreadable."""
    try:
        import yaml

        p = Path(path) if path else POLICY_PATH
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            return []
        raw = doc.get("keep_alive_projects")
        if not isinstance(raw, list):
            return []
        out = []
        for e in raw:
            if not isinstance(e, dict):
                continue
            prefix = e.get("prefix")
            if not isinstance(prefix, str) or not prefix.strip():
                continue
            decision = e.get("decision")
            out.append({"prefix": prefix.strip(),
                        "decision": decision.strip() if isinstance(decision, str)
                        and decision.strip() else None})
        return out
    except Exception:
        return []


def starved(policy: list[dict], actionable: set[str]) -> list[dict]:
    """Policy entries (in order) with no actionable slug under their prefix. Pure."""
    return [e for e in policy
            if not any(s.startswith(e["prefix"]) for s in actionable)]


def render_context(starved_list: list[dict]) -> str:
    """The keep-alive block appended to the planner prompt; "" when none."""
    if not starved_list:
        return ""
    lines = [
        HEADING,
        "",
        "아래 프로젝트는 볼트 정책 `keep_alive_projects`에 있지만 실행 가능한 열린 태스크가 0개다. "
        "그래서 이번 호출은 큐 깊이 skip과 exhausted 백오프를 건너뛰고 열렸다.",
        "",
    ]
    for e in starved_list:
        lines.append(f"- [{e['prefix']}] decision: {e['decision'] or '(없음)'}")
    lines += [
        "",
        "- 접두사마다 slug가 그 접두사로 시작하는 태스크를 **최소 1건** 만든다. decision 문서가 "
        "있으면 먼저 읽고 그 방향을 잇는다.",
        "- 이 프로젝트를 `exhausted`·수렴으로 판정하지 않는다. `auto queue depth limit reached`와 "
        "`no trigger events since last exhausted plan` skip도 이 프로젝트에는 적용하지 않는다.",
        "- `worker_mode: auto` 또는 `delegate`로 만들고 `not_before`는 비우거나 과거로 둔다 "
        "(quick·미래 not_before 태스크는 실행 가능으로 세지 않아 다음 틱에도 굶은 상태로 남는다).",
        "- 가드레일은 그대로다: `max_new_tasks_per_invocation`이 전체 상한이고 굶은 프로젝트가 "
        "먼저 채운다(넘치는 것은 다음 틱), `blocked_patterns`와 `required_fields`도 그대로 지킨다.",
        "- 생성이 정말 불가능하면 `verdict: error tasks=0 reason=\"keep-alive <접두사>: <사유>\"`로 "
        "답하고 `notifications.kakao_on_error` 경로로 알린다.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m todo_system.orchestrator.keep_alive")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("queue-depth", "print the actionable auto-queue depth"),
                        ("emit-context", "print the keep-alive starved block")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--db", type=Path, default=None)
        sp.add_argument("--assignments-dir", type=Path, default=None)
        if name == "emit-context":
            sp.add_argument("--policy", type=Path, default=None)
    args = p.parse_args(argv)

    if args.cmd == "queue-depth":
        try:
            n = len(actionable_slugs(args.db, args.assignments_dir))
        except Exception:
            n = 0
        sys.stdout.write(f"{n}\n")
        return 0

    # emit-context: fail safe — any error means nothing starved, nothing printed.
    try:
        policy = load_policy(args.policy)
        if policy:
            sys.stdout.write(render_context(
                starved(policy, actionable_slugs(args.db, args.assignments_dir))))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
