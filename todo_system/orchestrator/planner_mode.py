"""Planner aggressiveness — plan harder while tokens are unused and the queue is idle.

Why: the planner tick is tuned for scarcity. It backs off 24h on an unchanged
open-task state, creates at most 3 tasks per call, and schedules a discovery run
per repo only every 14 days. That is right when the subscription is tight, but a
week with most of its budget unused and nothing queued or running just idles:
the unused quota expires at the reset and buys nothing.

This module decides, AFTER every existing gate in scripts/planner_tick.sh has
passed (HQ host, PAUSED, usage guard block, daily run budget, queue depth), one
of two modes:

  aggressive  usage decision allowed, fresh (not stale/carried), BOTH session
              and weekly windows have slack >= a threshold (default: one
              `*_parallel_step` of usage_guard), and the runner has a slot
              that nothing will fill: queued + in-flight runs < the parallel
              slots the guard grants right now (`capacity`). With capacity 1
              that is the original rule — queue at or below
              MAESTRO_PLANNER_AGGRESSIVE_MAX_QUEUE (0) and no run in flight.
  normal      everything else — including the usage guard's no-data fail-open,
              a stale snapshot and a carried hold. Unknown means normal. A
              queue at or over the depth limit (reachable only through the
              keep-alive bypass, keep_alive.py) is normal before any check.

Only aggressive changes anything: a shorter backoff, a higher new-task cap
(inside the queue depth limit), a shorter discovery interval, and a "탐색 모드"
block injected into the planner prompt. The policy's blocked patterns are still
enforced by that block; nothing here widens what the planner may create.

Knobs (env, read per call):
    MAESTRO_PLANNER_AGGRESSIVE=0                            disable entirely
    MAESTRO_PLANNER_AGGRESSIVE_BACKOFF_HOURS              (2)  vs normal 24
    MAESTRO_PLANNER_AGGRESSIVE_MAX_NEW_TASKS              (6)  clamped to depth room
    MAESTRO_PLANNER_AGGRESSIVE_DISCOVERY_INTERVAL_DAYS    (3)  never above normal
    MAESTRO_PLANNER_AGGRESSIVE_MAX_QUEUE                  (0)
    MAESTRO_PLANNER_AGGRESSIVE_MIN_SLACK_SESSION  (session_parallel_step)
    MAESTRO_PLANNER_AGGRESSIVE_MIN_SLACK_WEEKLY   (weekly_parallel_step)

CLI (planner_tick.sh; always exits 0, any error prints MODE=normal):
    python -m todo_system.orchestrator.planner_mode decide --queue-depth N --depth-limit D
    python -m todo_system.orchestrator.planner_mode emit-context --max-new-tasks N --depth-limit D
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .. import paths, usage_guard
from . import assignment, discovery

# Vault-data root. start= is paths.vault_search_start(): the engine root in a
# checkout (byte-identical to the former parents[2] anchor), None in an
# installed wheel so the cwd-upward search runs instead of a site-packages anchor.
_ROOT = paths.vault_root(start=paths.vault_search_start())
ASSIGNMENTS_DIR = _ROOT / "Worker" / "assignments"

AGGRESSIVE = "aggressive"
NORMAL = "normal"

# Normal-mode values, mirrored from scripts/planner_tick.sh (EXHAUST_HOURS) and
# Worker/planner_policy.yaml (max_new_tasks_per_invocation). Informational only:
# the shell never applies knob values in normal mode.
NORMAL_BACKOFF_HOURS = 24
NORMAL_MAX_NEW_TASKS = 3

DEFAULT_BACKOFF_HOURS = 2
DEFAULT_MAX_NEW_TASKS = 6
DEFAULT_DISCOVERY_INTERVAL_DAYS = 3.0
DEFAULT_MAX_QUEUE = 0

# A run in these states is consuming (or about to consume) the budget; an
# awaiting_human / needs_input row is parked and counts as idle.
RUNNING_STATES = frozenset({assignment.DISPATCHED, assignment.ACCEPTED,
                            assignment.IN_PROGRESS})


def _env(env: dict | None) -> dict:
    return os.environ if env is None else env


def enabled(env: dict | None = None) -> bool:
    v = str(_env(env).get("MAESTRO_PLANNER_AGGRESSIVE", "1")).strip().lower()
    return v not in ("0", "false", "no", "off")


def _env_float(env: dict | None, name: str, default: float, *, minimum: float) -> float:
    try:
        v = float(_env(env).get(name, default))
    except (TypeError, ValueError):
        return default
    return v if v >= minimum else default  # NaN compares False -> default


def _env_int(env: dict | None, name: str, default: int, *, minimum: int) -> int:
    try:
        v = int(str(_env(env).get(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return v if v >= minimum else default


def min_slack(name: str, env: dict | None = None, cfg: dict | None = None) -> float:
    """Slack (pp) a window needs for aggressive mode: one parallel step by default."""
    cfg = cfg or usage_guard._cfg()
    return _env_float(env, f"MAESTRO_PLANNER_AGGRESSIVE_MIN_SLACK_{name.upper()}",
                      cfg[f"{name}_parallel_step"], minimum=0.0)


def planner_aggressiveness(decision: dict, queue_depth: int, running: int, *,
                           env: dict | None = None,
                           capacity: int = 1) -> tuple[str, str]:
    """(mode, reason) for one planner tick. Pure — no I/O.

    Checks run in order and the first failure answers `normal`, so the reason
    always names the one thing standing between this tick and aggressive mode.

    `capacity` is how many runs the runner may hold at once right now. Requiring
    zero runs in flight starved the parallel runner: with 2-3 slots busy the
    planner never refilled, so the queue drained to the last repo-serial task
    and the free slots (and the week's quota) idled (2026-10-12).
    """
    if not enabled(env):
        return NORMAL, "disabled (MAESTRO_PLANNER_AGGRESSIVE=0)"
    if decision.get("allowed") is not True:
        return NORMAL, "usage guard blocked"
    # A carried hold replays an old block; that is not evidence of headroom.
    # Plain `stale` is fine: check() only evaluates a snapshot younger than
    # stale_ok_s (3h), and the usage endpoint 429s often enough that demanding a
    # <4min fetch at the planner's 30-min tick kept this mode off entirely
    # (2026-09-26/27: every tick answered "stale usage data").
    if decision.get("carried"):
        return NORMAL, "stale usage data"
    cfg = usage_guard._cfg()
    slack: dict[str, float] = {}
    for w in decision.get("windows") or []:
        s = usage_guard.window_slack(w, cfg)
        if s is not None:
            slack[w["name"]] = s
    missing = [n for n in ("session", "weekly") if n not in slack]
    if missing:
        return NORMAL, f"no usage data ({', '.join(missing)})"
    for name in ("session", "weekly"):
        need = min_slack(name, env, cfg)
        if slack[name] < need:
            return NORMAL, f"{name} slack {slack[name]:.1f}pp < {need:g}pp"
    max_queue = _env_int(env, "MAESTRO_PLANNER_AGGRESSIVE_MAX_QUEUE",
                         DEFAULT_MAX_QUEUE, minimum=0)
    free = max(1, capacity) - running - queue_depth
    if queue_depth > max_queue and free <= 0:
        return NORMAL, f"queue {queue_depth} > {max_queue}"
    if running > 0 and free <= 0:
        return NORMAL, f"{running} run(s) in flight"
    stale = " (stale)" if decision.get("stale") else ""
    load = "idle" if running == 0 else f"{running} running, {free} free slot(s)"
    return AGGRESSIVE, (f"session slack {slack['session']:.1f}pp{stale}, "
                        f"weekly slack {slack['weekly']:.1f}pp, "
                        f"queue {queue_depth}, {load}")


def running_count(assignments_dir: Path | None = None) -> int:
    """Ledger rows whose state is dispatched / accepted / in_progress.
    Unreadable files are skipped (they say nothing about what is running)."""
    d = Path(assignments_dir) if assignments_dir else ASSIGNMENTS_DIR
    n = 0
    try:
        files = sorted(d.glob("*.json"))
    except OSError:
        return 0
    for f in files:
        try:
            row = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(row, dict) and row.get("state") in RUNNING_STATES:
            n += 1
    return n


def knobs(env: dict | None = None, *, queue_depth: int = 0,
          depth_limit: int = 5) -> dict:
    """Aggressive-mode values. max_new_tasks never exceeds the room left under
    the queue depth limit; the discovery interval never exceeds the normal one."""
    backoff = _env_int(env, "MAESTRO_PLANNER_AGGRESSIVE_BACKOFF_HOURS",
                       DEFAULT_BACKOFF_HOURS, minimum=1)
    max_new = _env_int(env, "MAESTRO_PLANNER_AGGRESSIVE_MAX_NEW_TASKS",
                       DEFAULT_MAX_NEW_TASKS, minimum=1)
    max_new = max(1, min(max_new, depth_limit - queue_depth))
    disc = _env_float(env, "MAESTRO_PLANNER_AGGRESSIVE_DISCOVERY_INTERVAL_DAYS",
                      DEFAULT_DISCOVERY_INTERVAL_DAYS, minimum=0.0)
    if disc <= 0:
        disc = DEFAULT_DISCOVERY_INTERVAL_DAYS
    disc = min(disc, discovery.interval_days(env))
    return {"backoff_hours": backoff, "max_new_tasks": max_new,
            "discovery_interval_days": disc}


def normal_knobs(env: dict | None = None) -> dict:
    return {"backoff_hours": NORMAL_BACKOFF_HOURS,
            "max_new_tasks": NORMAL_MAX_NEW_TASKS,
            "discovery_interval_days": discovery.interval_days(env)}


def render_explore_block(max_new_tasks: int, depth_limit: int) -> str:
    """The "탐색 모드" context appended to the planner prompt (aggressive only)."""
    return "\n".join([
        "## 탐색 모드 (planner_tick 주입 — 토큰 여유 + 유휴 큐)",
        "",
        "이번 호출은 **탐색 모드**다. 세션·주간 사용량 모두 여유가 있고, 디스패치 가능한 큐와 "
        "실행 중인 run을 합쳐도 실행 슬롯에 빈자리가 있다. 남는 토큰은 리셋과 함께 사라지므로 이번에는 "
        "적극적으로 후보를 찾는다.",
        "",
        "- `no trigger events since last exhausted plan` 자체 skip을 하지 않는다. trigger "
        "이벤트가 없어도 아래 소스를 직접 훑고 판단한다.",
        "- 기존 leaf 연장에 그치지 말고 다음을 적극 후보로 삼는다:",
        "  - 활성(비취소) 프로젝트 리포의 TODO/FIXME/HACK 중 구체적이고 자기완결적인 것",
        "  - 테스트 공백 (테스트 없는 모듈·함수, 풀 수 있는 skip/xfail)",
        "  - 아직 반영되지 않은 discovery 리포트 후보 (`Memory/episodes/*-discovery-*.md`)",
        "  - 완료 태스크의 report에 남은 followup (`Worker/_state/followups.jsonl`)",
        f"- 이번 호출의 새 태스크 상한은 **{max_new_tasks}개**다 (policy의 "
        f"`max_new_tasks_per_invocation` 대신 적용). 큐 깊이 한도 "
        f"`auto_queue_depth_limit` {depth_limit}은 그대로 지킨다.",
        "- `Worker/planner_policy.yaml`의 `blocked_patterns`는 전부 그대로 강제한다: "
        "외부 행동, 파괴적·비가역 작업, 사용자 결정에 묶인 것, 유료 작업, "
        "사용자가 미뤄둔 것(deferred·`not_before`), 직접 구현, planner 자기 가드레일 수정.",
        "- 러너는 리포당 한 번에 하나만 실행한다. 큐·실행 중 태스크와 **다른 리포**의 후보를 "
        "우선해 빈 슬롯이 실제로 채워지게 한다.",
        "- busywork는 여전히 금지다. 구체적이고 안전한 후보가 없으면 `exhausted`가 정답이다.",
        "",
    ])


def _one_line(s: str) -> str:
    return " ".join(str(s).split())


def _fmt_days(v: float) -> str:
    return f"{v:g}"


def _emit(mode: str, reason: str, k: dict) -> None:
    print(f"MODE={mode}")
    print(f"REASON={_one_line(reason)}")
    print(f"BACKOFF_HOURS={int(k['backoff_hours'])}")
    print(f"MAX_NEW_TASKS={int(k['max_new_tasks'])}")
    print(f"DISCOVERY_INTERVAL_DAYS={_fmt_days(float(k['discovery_interval_days']))}")


def max_parallel(env: dict | None = None) -> int:
    """The runner's concurrency ceiling, as runner._max_parallel reads it:
    RUNNER_MAX_PARALLEL, else the largest registry `concurrency`. Any trouble
    reading it answers 1 — the original idle-only rule."""
    raw = str(_env(env).get("RUNNER_MAX_PARALLEL", "")).strip()
    try:
        if raw:
            return max(1, int(raw))
        from .registry import load_registry
        reg = load_registry(base=_ROOT)
        return max([1] + [int(getattr(a, "concurrency", 1) or 1) for a in reg.agents])
    except Exception:
        return 1


def decide(queue_depth: int, depth_limit: int, *,
           assignments_dir: Path | None = None) -> tuple[str, str, dict]:
    # Only the keep-alive bypass of the depth gate reaches here at the limit.
    if queue_depth >= depth_limit:
        return NORMAL, f"queue {queue_depth} >= depth limit {depth_limit}", normal_knobs()
    decision = usage_guard.check()
    capacity = usage_guard.parallel_slots(decision, max_parallel())
    mode, reason = planner_aggressiveness(decision, queue_depth,
                                          running_count(assignments_dir),
                                          capacity=capacity)
    if mode == AGGRESSIVE:
        return mode, reason, knobs(queue_depth=queue_depth, depth_limit=depth_limit)
    return mode, reason, normal_knobs()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m todo_system.orchestrator.planner_mode")
    sub = p.add_subparsers(dest="cmd", required=True)
    pd = sub.add_parser("decide", help="print MODE/REASON/knob KEY=VALUE lines")
    pd.add_argument("--queue-depth", type=int, default=0)
    pd.add_argument("--depth-limit", type=int, default=5)
    pd.add_argument("--assignments-dir", type=Path, default=None)
    pe = sub.add_parser("emit-context", help="print the 탐색 모드 prompt block")
    pe.add_argument("--max-new-tasks", type=int, required=True)
    pe.add_argument("--depth-limit", type=int, default=5)
    args = p.parse_args(argv)

    if args.cmd == "emit-context":
        sys.stdout.write(render_explore_block(args.max_new_tasks, args.depth_limit))
        return 0

    # decide: fail closed — any error is a normal tick, never an aggressive one.
    try:
        mode, reason, k = decide(args.queue_depth, args.depth_limit,
                                 assignments_dir=args.assignments_dir)
    except Exception as exc:
        mode, reason = NORMAL, f"mode check error: {type(exc).__name__}"
        k = {"backoff_hours": NORMAL_BACKOFF_HOURS,
             "max_new_tasks": NORMAL_MAX_NEW_TASKS,
             "discovery_interval_days": discovery.DEFAULT_INTERVAL_DAYS}
    _emit(mode, reason, k)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
