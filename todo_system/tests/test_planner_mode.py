"""planner_mode — aggressive-when-idle-headroom verdict, knobs, CLI, and the
planner_tick.sh wiring that must leave the normal path untouched."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from todo_system import usage_guard as ug
from todo_system.orchestrator import planner_mode as pm

NOW = 1_800_000_000.0

_KNOB_ENVS = (
    "MAESTRO_PLANNER_AGGRESSIVE",
    "MAESTRO_PLANNER_AGGRESSIVE_BACKOFF_HOURS",
    "MAESTRO_PLANNER_AGGRESSIVE_MAX_NEW_TASKS",
    "MAESTRO_PLANNER_AGGRESSIVE_DISCOVERY_INTERVAL_DAYS",
    "MAESTRO_PLANNER_AGGRESSIVE_MAX_QUEUE",
    "MAESTRO_PLANNER_AGGRESSIVE_MIN_SLACK_SESSION",
    "MAESTRO_PLANNER_AGGRESSIVE_MIN_SLACK_WEEKLY",
    "MAESTRO_DISCOVERY_INTERVAL_DAYS",
    "RUNNER_MAX_PARALLEL",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _KNOB_ENVS:
        monkeypatch.delenv(name, raising=False)
    # USAGE_GUARD_* is scanned by prefix (not hardcoded) so a host override of
    # any current or future knob (hard caps, allowances, parallel steps, ...)
    # can't desync planner_aggressiveness()'s math — see test_usage_guard.py's
    # matching fixture for why this must be a prefix scan, not a fixed list.
    for name in list(os.environ):
        if name.startswith("USAGE_GUARD_"):
            monkeypatch.delenv(name, raising=False)


def _iso_in(seconds: float) -> str:
    return datetime.fromtimestamp(NOW + seconds, tz=timezone.utc).isoformat()


def _usage(session_util=10.0, session_reset_in=3600.0,
           weekly_util=10.0, weekly_reset_in=3 * 86400.0) -> dict:
    # session: 1h left of 5h -> 80% elapsed -> line min(80+30, 90) = 90
    # weekly: 3d left of 7d -> 57.1% elapsed -> line 57.1 + 12 = 69.1
    return {
        "five_hour": {"utilization": session_util,
                      "resets_at": _iso_in(session_reset_in)},
        "seven_day": {"utilization": weekly_util,
                      "resets_at": _iso_in(weekly_reset_in)},
    }


def _headroom(**kw) -> dict:
    d = ug.evaluate(_usage(**kw), NOW)
    d["stale"] = False
    return d


# ---------------------------------------------------------------------------
# planner_aggressiveness — the pure verdict
# ---------------------------------------------------------------------------


def test_headroom_empty_queue_idle_is_aggressive():
    mode, reason = pm.planner_aggressiveness(_headroom(), 0, 0, env={})
    assert mode == "aggressive"
    assert "session slack" in reason and "weekly slack" in reason and "idle" in reason


def test_blocked_is_normal():
    d = ug.evaluate(_usage(weekly_util=ug._cfg()["weekly_hard_cap"] + 1.0), NOW)
    assert not d["allowed"]
    assert pm.planner_aggressiveness(d, 0, 0, env={}) == ("normal", "usage guard blocked")


def test_stale_within_ok_window_still_counts():
    # check() only evaluates a snapshot younger than stale_ok_s; the endpoint
    # 429s too often to demand a <TTL fetch at the planner's 30-min tick.
    d = _headroom()
    d["stale"] = True
    mode, reason = pm.planner_aggressiveness(d, 0, 0, env={})
    assert mode == "aggressive" and "stale" in reason


def test_idle_session_window_is_headroom_not_missing_data():
    # overnight: no 5h window running -> the endpoint reports no reset time
    for five_hour in ({"utilization": 0.0, "resets_at": None}, None):
        usage = _usage()
        usage["five_hour"] = five_hour
        d = ug.evaluate(usage, NOW)
        d["stale"] = False
        mode, reason = pm.planner_aggressiveness(d, 0, 0, env={})
        assert mode == "aggressive", (five_hour, reason)


def test_absent_weekly_window_is_still_missing_data():
    usage = _usage()
    usage["seven_day"] = None
    d = ug.evaluate(usage, NOW)
    d["stale"] = False
    mode, reason = pm.planner_aggressiveness(d, 0, 0, env={})
    assert mode == "normal" and "weekly" in reason


def test_no_data_fail_open_is_normal():
    fail_open = {"allowed": True, "windows": [], "stale": True,
                 "reason": "no usage data (x) — fail-open"}
    assert pm.planner_aggressiveness(fail_open, 0, 0, env={})[0] == "normal"
    # even if some caller dropped `stale`, empty windows alone are not evidence
    no_windows = {"allowed": True, "windows": []}
    mode, reason = pm.planner_aggressiveness(no_windows, 0, 0, env={})
    assert mode == "normal" and "no usage data" in reason


def test_carried_hold_is_normal():
    carried = {"allowed": True, "carried": True, "windows": _headroom()["windows"]}
    mode, reason = pm.planner_aggressiveness(carried, 0, 0, env={})
    assert mode == "normal" and "stale" in reason


def test_only_one_window_is_normal():
    d = _headroom()
    d["windows"] = [w for w in d["windows"] if w["name"] == "session"]
    mode, reason = pm.planner_aggressiveness(d, 0, 0, env={})
    assert mode == "normal" and "weekly" in reason


def test_session_slack_threshold_is_one_parallel_step():
    # line 90: util 83 -> slack 7 < 8; util 82 -> slack 8 == 8 passes
    mode, reason = pm.planner_aggressiveness(_headroom(session_util=83.0), 0, 0, env={})
    assert mode == "normal" and reason.startswith("session slack")
    assert pm.planner_aggressiveness(_headroom(session_util=82.0), 0, 0, env={})[0] == "aggressive"


def test_weekly_slack_just_under_step_is_normal():
    # line 69.1: util 65 -> slack 4.1 < 5
    d = _headroom(weekly_util=65.0)
    assert d["allowed"]
    mode, reason = pm.planner_aggressiveness(d, 0, 0, env={})
    assert mode == "normal" and reason.startswith("weekly slack")


def test_queue_depth_threshold():
    assert pm.planner_aggressiveness(_headroom(), 1, 0, env={}) == ("normal", "queue 1 > 0")
    env = {"MAESTRO_PLANNER_AGGRESSIVE_MAX_QUEUE": "2"}
    assert pm.planner_aggressiveness(_headroom(), 1, 0, env=env)[0] == "aggressive"


def test_running_run_is_normal():
    assert pm.planner_aggressiveness(_headroom(), 0, 1, env={}) == (
        "normal", "1 run(s) in flight")


def test_free_parallel_slot_is_aggressive_despite_running_and_queue():
    # capacity 3: 1 running + 1 queued leaves a slot nothing will fill
    mode, reason = pm.planner_aggressiveness(_headroom(), 1, 1, env={}, capacity=3)
    assert mode == "aggressive"
    assert reason.endswith("queue 1, 1 running, 1 free slot(s)")


def test_full_parallel_slots_are_normal():
    assert pm.planner_aggressiveness(_headroom(), 0, 3, env={}, capacity=3) == (
        "normal", "3 run(s) in flight")
    assert pm.planner_aggressiveness(_headroom(), 2, 1, env={}, capacity=3) == (
        "normal", "queue 2 > 0")


def test_capacity_one_keeps_the_idle_only_rule():
    assert pm.planner_aggressiveness(_headroom(), 0, 0, env={}, capacity=1)[0] == "aggressive"
    assert pm.planner_aggressiveness(_headroom(), 0, 1, env={}, capacity=1)[0] == "normal"
    assert pm.planner_aggressiveness(_headroom(), 1, 0, env={}, capacity=0)[0] == "normal"


def test_max_parallel_reads_env_then_registry(monkeypatch):
    assert pm.max_parallel({"RUNNER_MAX_PARALLEL": "2"}) == 2
    assert pm.max_parallel({"RUNNER_MAX_PARALLEL": "junk"}) == 1
    assert pm.max_parallel({}) >= 1


@pytest.mark.parametrize("off", ["0", "false", "no", "OFF"])
def test_kill_switch_disables(off):
    mode, reason = pm.planner_aggressiveness(
        _headroom(), 0, 0, env={"MAESTRO_PLANNER_AGGRESSIVE": off})
    assert mode == "normal" and "disabled" in reason


def test_min_slack_override_flips_verdict():
    # weekly slack ~59pp by default: a 70pp requirement refuses, 4pp accepts 65%
    assert pm.planner_aggressiveness(
        _headroom(), 0, 0,
        env={"MAESTRO_PLANNER_AGGRESSIVE_MIN_SLACK_WEEKLY": "70"})[0] == "normal"
    assert pm.planner_aggressiveness(
        _headroom(weekly_util=65.0), 0, 0,
        env={"MAESTRO_PLANNER_AGGRESSIVE_MIN_SLACK_WEEKLY": "4"})[0] == "aggressive"


def test_invalid_env_values_fall_back_to_defaults():
    env = {"MAESTRO_PLANNER_AGGRESSIVE_MIN_SLACK_WEEKLY": "lots",
           "MAESTRO_PLANNER_AGGRESSIVE_MIN_SLACK_SESSION": "-3",
           "MAESTRO_PLANNER_AGGRESSIVE_MAX_QUEUE": "many"}
    # defaults: weekly step 5 refuses slack 4.1; queue limit 0 refuses 1
    assert pm.planner_aggressiveness(_headroom(weekly_util=65.0), 0, 0, env=env)[0] == "normal"
    assert pm.planner_aggressiveness(_headroom(), 1, 0, env=env)[0] == "normal"
    assert pm.planner_aggressiveness(_headroom(), 0, 0, env=env)[0] == "aggressive"


# ---------------------------------------------------------------------------
# knobs
# ---------------------------------------------------------------------------


def test_knobs_defaults_clamp_to_depth_room():
    k = pm.knobs({}, queue_depth=0, depth_limit=5)
    assert k == {"backoff_hours": 2, "max_new_tasks": 5, "discovery_interval_days": 3.0}
    assert pm.knobs({}, queue_depth=0, depth_limit=10)["max_new_tasks"] == 6
    assert pm.knobs({}, queue_depth=3, depth_limit=5)["max_new_tasks"] == 2
    assert pm.knobs({}, queue_depth=5, depth_limit=5)["max_new_tasks"] == 1


def test_knobs_env_overrides():
    env = {"MAESTRO_PLANNER_AGGRESSIVE_BACKOFF_HOURS": "4",
           "MAESTRO_PLANNER_AGGRESSIVE_MAX_NEW_TASKS": "2",
           "MAESTRO_PLANNER_AGGRESSIVE_DISCOVERY_INTERVAL_DAYS": "1.5"}
    assert pm.knobs(env, queue_depth=0, depth_limit=5) == {
        "backoff_hours": 4, "max_new_tasks": 2, "discovery_interval_days": 1.5}


def test_knobs_discovery_never_longer_than_normal_interval():
    env = {"MAESTRO_DISCOVERY_INTERVAL_DAYS": "2"}
    assert pm.knobs(env)["discovery_interval_days"] == 2.0
    env = {"MAESTRO_PLANNER_AGGRESSIVE_DISCOVERY_INTERVAL_DAYS": "30"}
    assert pm.knobs(env)["discovery_interval_days"] == 14.0


def test_knobs_bad_values_fall_back():
    env = {"MAESTRO_PLANNER_AGGRESSIVE_BACKOFF_HOURS": "1.5",
           "MAESTRO_PLANNER_AGGRESSIVE_MAX_NEW_TASKS": "0",
           "MAESTRO_PLANNER_AGGRESSIVE_DISCOVERY_INTERVAL_DAYS": "0"}
    assert pm.knobs(env, queue_depth=0, depth_limit=10) == {
        "backoff_hours": 2, "max_new_tasks": 6, "discovery_interval_days": 3.0}


def test_normal_knobs_are_the_existing_values():
    assert pm.normal_knobs({}) == {"backoff_hours": 24, "max_new_tasks": 3,
                                   "discovery_interval_days": 14.0}


# ---------------------------------------------------------------------------
# running_count
# ---------------------------------------------------------------------------


def test_running_count_counts_only_in_flight_states(tmp_path):
    states = ["dispatched", "accepted", "in_progress", "awaiting_human",
              "needs_input", "failed", "done"]
    for i, s in enumerate(states):
        (tmp_path / f"t{i}.json").write_text(json.dumps({"task_id": f"t{i}", "state": s}))
    (tmp_path / "corrupt.json").write_text("{not json")
    (tmp_path / "list.json").write_text("[]")
    assert pm.running_count(tmp_path) == 3


def test_running_count_missing_dir_is_zero(tmp_path):
    assert pm.running_count(tmp_path / "nope") == 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _kv(out: str) -> dict:
    return dict(line.split("=", 1) for line in out.strip().splitlines())


def test_cli_decide_aggressive(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(pm.usage_guard, "check", lambda **_: _headroom())
    rc = pm.main(["decide", "--queue-depth", "0", "--depth-limit", "5",
                  "--assignments-dir", str(tmp_path)])
    assert rc == 0
    kv = _kv(capsys.readouterr().out)
    assert list(kv) == ["MODE", "REASON", "BACKOFF_HOURS", "MAX_NEW_TASKS",
                        "DISCOVERY_INTERVAL_DAYS"]
    assert kv["MODE"] == "aggressive"
    assert kv["BACKOFF_HOURS"] == "2"
    assert kv["MAX_NEW_TASKS"] == "5"
    assert kv["DISCOVERY_INTERVAL_DAYS"] == "3"


def test_cli_decide_running_is_normal_with_normal_knobs(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("RUNNER_MAX_PARALLEL", "1")
    (tmp_path / "a.json").write_text(json.dumps({"state": "in_progress"}))
    monkeypatch.setattr(pm.usage_guard, "check", lambda **_: _headroom())
    pm.main(["decide", "--queue-depth", "0", "--depth-limit", "5",
             "--assignments-dir", str(tmp_path)])
    kv = _kv(capsys.readouterr().out)
    assert kv["MODE"] == "normal"
    assert kv["REASON"] == "1 run(s) in flight"
    assert (kv["BACKOFF_HOURS"], kv["MAX_NEW_TASKS"], kv["DISCOVERY_INTERVAL_DAYS"]) == (
        "24", "3", "14")


def test_cli_decide_fails_closed_on_error(monkeypatch, tmp_path, capsys):
    def boom(**_):
        raise RuntimeError("guard\nexploded")
    monkeypatch.setattr(pm.usage_guard, "check", boom)
    rc = pm.main(["decide", "--queue-depth", "0", "--depth-limit", "5",
                  "--assignments-dir", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    kv = _kv(out)
    assert kv["MODE"] == "normal"
    assert kv["REASON"] == "mode check error: RuntimeError"
    assert len(out.strip().splitlines()) == 5


def test_cli_emit_context(capsys):
    assert pm.main(["emit-context", "--max-new-tasks", "4", "--depth-limit", "5"]) == 0
    out = capsys.readouterr().out
    for needle in ("탐색 모드", "**4개**", "blocked_patterns", "TODO/FIXME",
                   "followup", "discovery", "not_before", "exhausted",
                   "no trigger events since last exhausted plan"):
        assert needle in out, needle


# ---------------------------------------------------------------------------
# scripts/planner_tick.sh wiring — normal path pinned, aggressive scoped
# ---------------------------------------------------------------------------

TICK = Path(__file__).resolve().parents[2] / "scripts" / "planner_tick.sh"


@pytest.fixture(scope="module")
def tick_text() -> str:
    return TICK.read_text(encoding="utf-8")


def test_tick_normal_path_lines_unchanged(tick_text):
    for line in (
        "DEPTH_LIMIT=5",
        "EXHAUST_HOURS=24",
        'PROMPT="/worker-planner"',
        'echo "[planner-tick] open-task state unchanged and last run < '
        '${EXHAUST_HOURS}h ago; skip (exhausted)"',
        "  DISC=$($PY -m todo_system.orchestrator.discovery maybe-create 2>/dev/null)",
        'PROMPT="$PROMPT"$\'\\n\\n\'"$CTX"',
        'PROMPT="$PROMPT"$\'\\n\\n\'"$DCTX"',
    ):
        assert line in tick_text, line
    # both planner invocations keep their flags
    assert tick_text.count('claude -p "$PROMPT" \\\n'
                           '    --model "$MODEL" --effort high --max-turns 30 \\\n') == 2


def _open_ifs_at_each_line(lines: list[str]):
    """Yield (line, stack of enclosing `if` headers) for shell lines. One-line
    `if …; then …; fi` forms neither push nor pop."""
    stack: list[str] = []
    for raw in lines:
        s = raw.strip()
        if s.startswith("#"):
            continue
        yield s, list(stack)
        if s.startswith("if ") and not re.search(r"\bthen\b.*\bfi\b", s):
            stack.append(s)
        elif s == "fi" or s.startswith("fi "):
            stack.pop()


def test_tick_aggressive_overrides_are_scoped(tick_text):
    # Start at the mode block: the gates above have their own `if`s.
    body = tick_text[tick_text.index("\nMODE=normal\n"):]
    checked = 0
    for line, ifs in _open_ifs_at_each_line(body.splitlines()):
        if not ("AGG_" in line or "planner_mode emit-context" in line
                or "MAESTRO_DISCOVERY_INTERVAL_DAYS=" in line):
            continue
        checked += 1
        assert any("MODE" in h and '"aggressive"' in h for h in ifs), line
    # parse (3 + 3 case) + EXHAUST override + emit-context + echo + discovery
    assert checked >= 9


def test_tick_mode_decided_after_every_gate(tick_text):
    decide = tick_text.index("planner_mode decide")
    assert tick_text.index("MODE=normal") < decide
    for gate in ("Worker/_state/PAUSED", "usage_guard check",
                 "MAESTRO_DAILY_RUN_BUDGET", ">= ${DEPTH_LIMIT}; skip"):
        assert tick_text.index(gate) < decide, gate
    log = tick_text.index('echo "[planner-tick] mode=${MODE} (${MODE_REASON})"')
    assert decide < log < tick_text.index("LAST_HASH=")
    assert not re.search(r"\bexit\b", tick_text[decide:log])


def test_tick_script_parses():
    assert subprocess.run(["bash", "-n", str(TICK)]).returncode == 0


def _run_mode_block(tmp_path: Path, tick_text: str, cli_out: str) -> dict:
    """Execute the real mode block from planner_tick.sh with $PY faked to
    print `cli_out`, and report MODE / EXHAUST_HOURS / the log line."""
    start = tick_text.index("\nMODE=normal\n") + 1
    end_marker = '  EXHAUST_HOURS="$AGG_BACKOFF"\nfi\n'
    end = tick_text.index(end_marker) + len(end_marker)
    (tmp_path / "out.txt").write_text(cli_out, encoding="utf-8")
    (tmp_path / "fake.sh").write_text(f'cat "{tmp_path / "out.txt"}"\n', encoding="utf-8")
    script = "\n".join([
        "set -uo pipefail",
        f'PY="bash {tmp_path / "fake.sh"}"',
        "N=0", "DEPTH_LIMIT=5", "EXHAUST_HOURS=24",
        tick_text[start:end],
        'echo "RESULT MODE=$MODE EXHAUST=$EXHAUST_HOURS"',
    ])
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    res = dict(kv.split("=", 1) for kv in lines[-1].split()[1:])
    res["log"] = next(line for line in lines if line.startswith("[planner-tick] mode="))
    return res


_AGG_OUT = ("MODE=aggressive\nREASON=session slack 80.0pp, weekly slack 59.1pp, queue 0, idle\n"
            "BACKOFF_HOURS=2\nMAX_NEW_TASKS=5\nDISCOVERY_INTERVAL_DAYS=3\n")


def test_tick_mode_block_aggressive(tmp_path, tick_text):
    res = _run_mode_block(tmp_path, tick_text, _AGG_OUT)
    assert res["MODE"] == "aggressive" and res["EXHAUST"] == "2"
    assert res["log"] == ("[planner-tick] mode=aggressive (session slack 80.0pp, "
                          "weekly slack 59.1pp, queue 0, idle)")


def test_tick_mode_block_normal_keeps_backoff(tmp_path, tick_text):
    out = ("MODE=normal\nREASON=queue 1 > 0\nBACKOFF_HOURS=24\n"
           "MAX_NEW_TASKS=3\nDISCOVERY_INTERVAL_DAYS=14\n")
    res = _run_mode_block(tmp_path, tick_text, out)
    assert res["MODE"] == "normal" and res["EXHAUST"] == "24"
    assert res["log"] == "[planner-tick] mode=normal (queue 1 > 0)"


@pytest.mark.parametrize("cli_out", [
    "",                                                    # CLI missing / crashed
    "garbage\n",
    _AGG_OUT.replace("BACKOFF_HOURS=2", "BACKOFF_HOURS=2.5"),
    _AGG_OUT.replace("BACKOFF_HOURS=2", "BACKOFF_HOURS=0"),
    _AGG_OUT.replace("MAX_NEW_TASKS=5", "MAX_NEW_TASKS="),
    _AGG_OUT.replace("DISCOVERY_INTERVAL_DAYS=3", "DISCOVERY_INTERVAL_DAYS=x"),
])
def test_tick_mode_block_fails_closed(tmp_path, tick_text, cli_out):
    res = _run_mode_block(tmp_path, tick_text, cli_out)
    assert res["MODE"] == "normal" and res["EXHAUST"] == "24"
    assert res["log"].startswith("[planner-tick] mode=normal (")


def _queue_depth(tmp_path: Path, rows) -> str:
    # Gate 1 runs `keep_alive queue-depth`; run that CLI against a scratch todo.db.
    (tmp_path / "Todo").mkdir()
    (tmp_path / "Worker" / "assignments").mkdir(parents=True)
    import sqlite3
    con = sqlite3.connect(tmp_path / "Todo" / "todo.db")
    con.execute("CREATE TABLE tasks (slug TEXT, status TEXT, is_parent INT, "
                "worker_mode TEXT, not_before TEXT)")
    con.executemany("INSERT INTO tasks VALUES (?, 'open', 0, 'auto', ?)", rows)
    con.commit()
    con.close()
    return subprocess.run(
        [sys.executable, "-m", "todo_system.orchestrator.keep_alive", "queue-depth",
         "--db", str(tmp_path / "Todo" / "todo.db"),
         "--assignments-dir", str(tmp_path / "Worker" / "assignments")],
        cwd=tmp_path, capture_output=True, text=True, check=True,
        env={**os.environ, "PYTHONPATH": str(TICK.parents[1])}).stdout


def test_tick_queue_depth_skips_not_before_deferred(tmp_path):
    out = _queue_depth(tmp_path, [
        ("ready-none", None), ("ready-empty", ""), ("ready-past", "2000-01-01"),
        ("held-future", "2999-01-01")])
    assert out.strip() == "3"


def test_tick_queue_depth_honors_not_before_time(tmp_path):
    # A time-of-day value is deferred only until that time, not the whole day.
    out = _queue_depth(tmp_path, [
        ("held-time", "2999-01-01 10:00"), ("held-timeT", "2999-01-01T10:00"),
        ("ready-time", "2000-01-01 10:00"), ("ready-timeT", "2000-01-01T23:59:59"),
        ("ready-none", None)])
    assert out.strip() == "3"


def test_tick_gate1_uses_shared_rule(tick_text):
    # One rule, in keep_alive: no duplicate SQL or heredoc left in the tick.
    assert "N=$($PY -m todo_system.orchestrator.keep_alive queue-depth 2>/dev/null)" in tick_text
    assert "worker_mode IN" not in tick_text
    assert "<<'PY'" not in tick_text


def test_tick_keep_alive_after_hard_gates(tick_text):
    ka = tick_text.index("keep_alive emit-context")
    for gate in ("hq_host --check", "Worker/_state/PAUSED", "usage_guard check",
                 "MAESTRO_DAILY_RUN_BUDGET"):
        assert tick_text.index(gate) < ka, gate
    assert ka < tick_text.index(">= ${DEPTH_LIMIT}; skip")


def test_decide_at_depth_limit_is_normal_without_usage_check(monkeypatch, tmp_path, capsys):
    def boom(**_):
        raise AssertionError("usage guard must not be consulted at the depth limit")
    monkeypatch.setattr(pm.usage_guard, "check", boom)
    mode, reason, k = pm.decide(5, 5, assignments_dir=tmp_path)
    assert mode == pm.NORMAL and "depth limit" in reason
    assert k == pm.normal_knobs()
    assert pm.main(["decide", "--queue-depth", "7", "--depth-limit", "5",
                    "--assignments-dir", str(tmp_path)]) == 0
    kv = _kv(capsys.readouterr().out)
    assert kv["MODE"] == "normal" and kv["REASON"] == "queue 7 >= depth limit 5"


def test_tick_queue_depth_time_boundary_matches_helper():
    # The Gate-1 SQL comparison and not_before.is_deferred agree at the minute.
    import sqlite3
    from todo_system import not_before as nb
    from todo_system.orchestrator import keep_alive
    assert ("replace(trim(not_before),'T',' ') > "
            "strftime('%Y-%m-%d %H:%M:%S','now','localtime')") in keep_alive.ACTIONABLE_SQL
    con = sqlite3.connect(":memory:")
    sql = "SELECT replace(trim(?),'T',' ') > ?"
    for value in ("2026-10-12 17:00", "2026-10-12T17:00", "2026-10-12"):
        for now in (datetime(2026, 10, 12, 16, 59, 30), datetime(2026, 10, 12, 17, 0),
                    datetime(2026, 10, 12, 0, 0), datetime(2026, 10, 11, 23, 59)):
            got = con.execute(sql, (value, now.strftime("%Y-%m-%d %H:%M:%S"))).fetchone()[0]
            assert bool(got) == nb.is_deferred(value, now), (value, now)
    con.close()


def test_cli_decide_free_slot_beside_a_running_run_is_aggressive(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("RUNNER_MAX_PARALLEL", "3")
    (tmp_path / "a.json").write_text(json.dumps({"state": "in_progress"}))
    monkeypatch.setattr(pm.usage_guard, "check", lambda **_: _headroom())
    pm.main(["decide", "--queue-depth", "1", "--depth-limit", "5",
             "--assignments-dir", str(tmp_path)])
    kv = _kv(capsys.readouterr().out)
    assert kv["MODE"] == "aggressive"
    assert kv["MAX_NEW_TASKS"] == "4"
