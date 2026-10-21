"""usage_guard policy tests — pure evaluate() math plus check() fail-open."""
from __future__ import annotations

import email.utils
import importlib
import json
import os
import subprocess
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

from todo_system import usage_guard as ug

NOW = 1_800_000_000.0  # fixed epoch for deterministic math


@pytest.fixture(autouse=True)
def _clean_usage_guard_env(monkeypatch):
    # The host (runner env) may export USAGE_GUARD_* overrides (e.g. a temporary
    # usage-burn window) that change evaluate()'s thresholds and desync these
    # hand-computed expectations. Scan the prefix rather than naming keys so a
    # future knob is covered automatically.
    for name in list(os.environ):
        if name.startswith("USAGE_GUARD_"):
            monkeypatch.delenv(name, raising=False)


def _iso_in(seconds: float) -> str:
    return datetime.fromtimestamp(NOW + seconds, tz=timezone.utc).isoformat()


def _usage(session_util=10.0, session_reset_in=3600.0,
           weekly_util=10.0, weekly_reset_in=3 * 86400.0) -> dict:
    return {
        "five_hour": {"utilization": session_util,
                      "resets_at": _iso_in(session_reset_in)},
        "seven_day": {"utilization": weekly_util,
                      "resets_at": _iso_in(weekly_reset_in)},
    }


def test_within_pace_allows():
    # weekly: 3d left of 7d -> ~57% elapsed; util 30% is well under pace
    d = ug.evaluate(_usage(session_util=20.0, weekly_util=30.0), NOW)
    assert d["allowed"]
    assert d["reason"] is None


def test_weekly_hard_cap_blocks_until_reset():
    # weekly_util must clear the hard cap to take the hard-cap branch; reference
    # the guard's own cap so this stays correct if the cap moves again (was 85.0,
    # silently below the 80->90 bump in 64d6d1a8 and thus taking the pace branch).
    d = ug.evaluate(_usage(weekly_util=ug._cfg()["weekly_hard_cap"] + 5.0), NOW)
    assert not d["allowed"]
    assert "hard cap" in d["reason"]
    weekly = next(w for w in d["windows"] if w["name"] == "weekly")
    assert weekly["blocked_until"] == pytest.approx(NOW + 3 * 86400.0, abs=2)


def test_weekly_pace_ahead_blocks_with_catchup_before_reset():
    # week just started (reset 7d away -> 0% elapsed) but 40% already burned:
    # exactly the burn-it-all-in-one-day failure mode this guard exists for.
    d = ug.evaluate(_usage(weekly_util=40.0, weekly_reset_in=7 * 86400.0), NOW)
    assert not d["allowed"]
    assert "ahead of pace" in d["reason"]
    weekly = next(w for w in d["windows"] if w["name"] == "weekly")
    assert NOW < weekly["blocked_until"] < NOW + 7 * 86400.0
    # catch-up: elapsed% reaches util - allowance (40 - 12 = 28%) of the window
    assert weekly["blocked_until"] == pytest.approx(NOW + 0.28 * 7 * 86400.0, rel=0.01)


def test_pace_block_self_clears_as_window_advances():
    usage = _usage(weekly_util=40.0, weekly_reset_in=3 * 86400.0)
    # 4d elapsed of 7d -> pace line ~57% + allowance; util 40% is fine now
    d = ug.evaluate(usage, NOW)
    assert d["allowed"]


def test_session_hard_cap_blocks():
    d = ug.evaluate(_usage(session_util=95.0), NOW)
    assert not d["allowed"]
    assert "session" in d["reason"]


def test_missing_fields_allow():
    assert ug.evaluate({}, NOW)["allowed"]
    assert ug.evaluate({"five_hour": {"utilization": None}}, NOW)["allowed"]


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("USAGE_GUARD_WEEKLY_HARD_CAP", "50")
    d = ug.evaluate(_usage(weekly_util=55.0), NOW)
    assert not d["allowed"]


def test_check_fail_open_without_data(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(ug, "_fetch_usage",
                        lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    d = ug.check(now=NOW)
    assert d["allowed"]
    assert "fail-open" in d["reason"]
    # decision is persisted for the dashboard/monitor
    state = json.loads((tmp_path / "latest.json").read_text())
    assert state["decision"]["allowed"] is True


def test_check_fail_closed_env(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("USAGE_GUARD_FAIL_CLOSED", "1")
    monkeypatch.setattr(ug, "_fetch_usage",
                        lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    assert not ug.check(now=NOW)["allowed"]


def test_check_uses_cached_snapshot_and_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path))
    (tmp_path / "latest.json").write_text(json.dumps({
        "fetched_at": NOW - 10,
        "usage": _usage(weekly_util=90.0),
    }))
    calls = []
    monkeypatch.setattr(ug, "_fetch_usage", lambda: calls.append(1))
    d = ug.check(now=NOW)
    assert not d["allowed"]
    assert calls == []  # fresh cache -> no network


def test_check_stale_beyond_limit_fails_open(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path))
    (tmp_path / "latest.json").write_text(json.dumps({
        "fetched_at": NOW - 4 * 3600,  # older than stale_ok (3h)
        "usage": _usage(weekly_util=90.0),
    }))
    monkeypatch.setattr(ug, "_fetch_usage",
                        lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    d = ug.check(now=NOW)
    # no prior decision to carry -> the stale snapshot alone cannot block
    assert d["allowed"]


# --- 429 storm: a failed fetch must throttle re-probing, not fire every tick ---
# The endpoint answers 429 exactly while utilization is high; the old get_snapshot
# only gated on fetched_at (last SUCCESS), so after a failure every check() re-hit
# the network — amplifying the very 429 that was the problem.


def test_429_storm_is_throttled_by_backoff(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path))
    calls = []

    def _429():
        calls.append(1)
        raise urllib.error.HTTPError(ug.USAGE_URL, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(ug, "_fetch_usage", _429)
    ug.check(now=NOW)
    ug.check(now=NOW + 1)
    ug.check(now=NOW + 2)
    # after the first failure the guard backs off; it must not re-probe each tick
    assert len(calls) == 1


def _http_429(headers=None):
    def _raise():
        raise urllib.error.HTTPError(
            ug.USAGE_URL, 429, "Too Many Requests", headers or {}, None)
    return _raise


def test_failure_backoff_is_exponential_without_retry_after(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("USAGE_GUARD_BACKOFF_BASE_S", "60")
    monkeypatch.setenv("USAGE_GUARD_BACKOFF_MAX_S", "1800")
    calls = []
    monkeypatch.setattr(ug, "_fetch_usage",
                        lambda: calls.append(1) or _http_429()())

    def _state():
        return json.loads((tmp_path / "latest.json").read_text())

    ug.check(now=NOW)                       # 1st failure -> base delay (60s)
    assert _state()["fail_count"] == 1
    assert _state()["next_retry_at"] == pytest.approx(NOW + 60)
    ug.check(now=NOW + 30)                  # still inside the window: no re-probe
    assert len(calls) == 1
    ug.check(now=NOW + 60)                  # window elapsed -> 2nd failure, doubles
    assert len(calls) == 2
    assert _state()["fail_count"] == 2
    assert _state()["next_retry_at"] == pytest.approx(NOW + 60 + 120)
    ug.check(now=NOW + 60 + 120)           # 3rd failure -> 240s
    assert _state()["fail_count"] == 3
    assert _state()["next_retry_at"] == pytest.approx(NOW + 60 + 120 + 240)


def test_failure_backoff_caps_at_max(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("USAGE_GUARD_BACKOFF_BASE_S", "60")
    monkeypatch.setenv("USAGE_GUARD_BACKOFF_MAX_S", "300")
    # a large prior fail_count would blow past the cap without the min()
    (tmp_path / "latest.json").write_text(json.dumps({"fail_count": 20}))
    monkeypatch.setattr(ug, "_fetch_usage", _http_429())
    ug.check(now=NOW)
    state = json.loads((tmp_path / "latest.json").read_text())
    assert state["fail_count"] == 21
    assert state["next_retry_at"] == pytest.approx(NOW + 300)  # capped, not 60*2**20


def test_retry_after_seconds_is_honored(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(ug, "_fetch_usage", _http_429({"Retry-After": "150"}))
    ug.check(now=NOW)
    state = json.loads((tmp_path / "latest.json").read_text())
    assert state["next_retry_at"] == pytest.approx(NOW + 150)  # header beats default


def test_retry_after_http_date_is_honored(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path))
    when = email.utils.formatdate(NOW + 900, usegmt=True)
    monkeypatch.setattr(ug, "_fetch_usage", _http_429({"Retry-After": when}))
    ug.check(now=NOW)
    state = json.loads((tmp_path / "latest.json").read_text())
    assert state["next_retry_at"] == pytest.approx(NOW + 900, abs=1)


def test_success_resets_backoff(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path))
    # a prior failure left a counter + an already-elapsed backoff window
    (tmp_path / "latest.json").write_text(json.dumps({
        "fail_count": 3, "next_retry_at": NOW - 1, "error": "old"}))
    monkeypatch.setattr(ug, "_fetch_usage", lambda: _usage(weekly_util=10.0))
    ug.check(now=NOW)
    state = json.loads((tmp_path / "latest.json").read_text())
    assert state["fail_count"] == 0
    assert state["next_retry_at"] == 0
    assert state["error"] is None
    assert state["usage"]  # fresh snapshot stored


def test_refresh_bypasses_backoff(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path))
    # a wide-open window that would normally suppress fetching
    (tmp_path / "latest.json").write_text(json.dumps({
        "fail_count": 5, "next_retry_at": NOW + 100000}))
    calls = []
    monkeypatch.setattr(ug, "_fetch_usage",
                        lambda: calls.append(1) or _usage(weekly_util=10.0))
    ug.check(force=True, now=NOW)
    assert calls == [1]  # --refresh bypassed the backoff window


# --- process-wide single fetch: a busy lock must serve cache, not re-probe ---


def test_busy_lock_uses_cache_and_does_not_fetch(tmp_path, monkeypatch):
    fcntl = pytest.importorskip("fcntl")
    import os
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path))
    # stale cache: get_snapshot would normally re-fetch (older than the TTL)
    (tmp_path / "latest.json").write_text(json.dumps({
        "fetched_at": NOW - 4 * 3600,
        "usage": _usage(weekly_util=90.0),
    }))
    calls = []
    monkeypatch.setattr(ug, "_fetch_usage", lambda: calls.append(1))
    # another "process" holds the lock: a SEPARATE open fd is a distinct open
    # file description, so flock conflicts even within one process (hermetic).
    holder = os.open(str(tmp_path / "fetch.lock"), os.O_CREAT | os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        state = ug.get_snapshot(now=NOW)
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    assert calls == []            # lock busy -> no network fetch
    assert state["usage"]         # cached snapshot served instead


def test_lock_unavailable_still_fetches_fail_open(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path))
    calls = []
    monkeypatch.setattr(ug, "_fetch_usage",
                        lambda: calls.append(1) or _usage(weekly_util=10.0))
    monkeypatch.setattr(ug, "_acquire_fetch_lock", lambda: ("unavailable", None))
    ug.get_snapshot(now=NOW)
    assert calls == [1]           # no fcntl / lock error must not suppress fetch


# --- no-data must not ERASE a block already decided (2026-08-24 regression) ---
# The usage endpoint answers 429 exactly while utilization is high, so plain
# fail-open reopened the gate the moment the hard cap started mattering and the
# weekly window rode 90% -> 100%.


def _blocked_state(*, blocked_until: float, fetched_at: float = NOW - 4 * 3600) -> dict:
    """State whose snapshot is too stale to re-evaluate, but which already
    recorded a hard-cap block."""
    return {
        "fetched_at": fetched_at,
        "usage": _usage(weekly_util=92.0),
        "decision": {
            "allowed": False,
            "reason": "weekly 92% >= hard cap 90% (until reset)",
            "blocked_until": blocked_until,
            "windows": [{"name": "weekly", "blocked": True}],
        },
    }


def test_429_does_not_lift_an_active_hard_cap_block(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path))
    (tmp_path / "latest.json").write_text(
        json.dumps(_blocked_state(blocked_until=NOW + 3600)))

    def _429():
        raise urllib.error.HTTPError(ug.USAGE_URL, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(ug, "_fetch_usage", _429)
    d = ug.check(now=NOW)
    assert not d["allowed"]
    assert d["carried"] is True
    assert d["blocked_until"] == NOW + 3600
    assert "hard cap" in d["reason"]  # the original cause survives


def test_carried_block_expires_on_its_own_schedule(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path))
    (tmp_path / "latest.json").write_text(
        json.dumps(_blocked_state(blocked_until=NOW - 1)))
    monkeypatch.setattr(ug, "_fetch_usage",
                        lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    d = ug.check(now=NOW)
    # a permanently broken endpoint must not mean a permanent stall
    assert d["allowed"]
    assert not d.get("carried")


def test_repeated_no_data_ticks_neither_nest_nor_extend(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path))
    (tmp_path / "latest.json").write_text(
        json.dumps(_blocked_state(blocked_until=NOW + 3600)))
    monkeypatch.setattr(ug, "_fetch_usage",
                        lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    first = ug.check(now=NOW)
    second = ug.check(now=NOW + 60)
    third = ug.check(now=NOW + 120)
    # the deadline is absolute, not pushed forward by each failed tick
    assert third["blocked_until"] == NOW + 3600
    # and the cause is stated once, not re-wrapped every cycle
    assert third["held_reason"] == first["held_reason"]
    assert third["reason"].count("holding last known block") == 1
    assert second["reason"] == first["reason"]


def test_fail_closed_still_wins_when_nothing_to_carry(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("USAGE_GUARD_FAIL_CLOSED", "1")
    (tmp_path / "latest.json").write_text(
        json.dumps(_blocked_state(blocked_until=NOW - 1)))
    monkeypatch.setattr(ug, "_fetch_usage",
                        lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    d = ug.check(now=NOW)
    assert not d["allowed"]
    assert "fail-closed" in d["reason"]


# --- no-data visibility: `status` must surface the last good snapshot ---


def test_status_shows_last_good_on_no_data(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path))
    now = ug.time.time()
    (tmp_path / "latest.json").write_text(json.dumps({
        "fetched_at": now - 4 * 3600,         # older than stale_ok -> no-data branch
        "usage": _usage(session_util=42.0, weekly_util=63.0),
        "error": "HTTPError: HTTP Error 429: Too Many Requests",
        "next_retry_at": now + 300,
    }))
    monkeypatch.setattr(ug, "_fetch_usage", _http_429())  # backoff holds; belt&braces
    rc = ug._main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "last good" in out
    assert "42" in out and "63" in out     # last good session/weekly utilization
    assert "429" in out                    # last fetch error surfaced
    assert "next retry" in out             # next scheduled retry shown


# --- ROOT resolves via paths.vault_root (public-engine / vault split follow-up) ---
# ROOT is an import-time constant, so the env/config/cwd branches only take effect on
# reimport: each test sets the environment, reloads the module, asserts, then a trailing
# clean reload (outside the monkeypatch context, env already restored) puts ROOT back to
# the real repo root for the tests that follow.


# byte-identity / install-pin hold only in the combined vault layout (a
# .macp/config.toml marker at the repo root); an engine-only export tree has no
# marker and reload() raises VaultRootError by design, so these two skip there
# (public-split-10 AC2 vault-only / AC4). The live vault has the marker and runs them.
_vault_only_byte_identity = pytest.mark.skipif(
    not (Path(ug.__file__).resolve().parents[1] / ".macp" / "config.toml").is_file(),
    reason="vault-only: byte-identity holds only in the combined vault layout "
           "(.macp/config.toml at the repo root); the engine-only export tree has no marker.",
)


@_vault_only_byte_identity
def test_root_no_env_is_byte_identical_repo_root(monkeypatch):
    # No MACP_VAULT_ROOT and no cwd override -> the resolver returns the install
    # marker directory, exactly the pre-resolver Path(__file__).resolve().parents[1].
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        mod = importlib.reload(ug)
        assert mod.ROOT == Path(mod.__file__).resolve().parents[1]
    importlib.reload(ug)


def test_root_env_redirects_module_and_state_path(monkeypatch, tmp_path):
    # MACP_VAULT_ROOT wins, and the redirect must reach the consumer: _state_path()
    # reads ROOT at call time and builds the guard's state file under the new root.
    with monkeypatch.context() as m:
        m.setenv("MACP_VAULT_ROOT", str(tmp_path))
        m.delenv("USAGE_GUARD_STATE_DIR", raising=False)
        mod = importlib.reload(ug)
        assert mod.ROOT == tmp_path
        assert mod._state_path() == tmp_path / "Worker" / "_state" / "usage_guard" / "latest.json"
    importlib.reload(ug)


@_vault_only_byte_identity
def test_root_start_pins_install_dir_not_cwd(monkeypatch, tmp_path):
    # A bogus .macp/config.toml in the cwd must NOT capture ROOT: the module pins
    # start= to its install dir, so resolution ignores the cwd-upward search. If it
    # resolved from cwd instead, ROOT would become '/bogus'.
    macp = tmp_path / ".macp"
    macp.mkdir()
    (macp / "config.toml").write_text("vault_root = '/bogus'\n", encoding="utf-8")
    with monkeypatch.context() as m:
        m.delenv("MACP_VAULT_ROOT", raising=False)
        m.chdir(tmp_path)
        mod = importlib.reload(ug)
        assert mod.ROOT == Path(mod.__file__).resolve().parents[1]
        assert mod.ROOT != Path("/bogus")
    importlib.reload(ug)


# ---------------------------------------------------------------------------
# parallel_slots — usage headroom -> concurrent runs
# ---------------------------------------------------------------------------


def test_parallel_slots_wide_open_hits_the_ceiling():
    # weekly: 3d left -> ~57% elapsed, util 10 -> slack ~59pp; session ~80% elapsed
    d = ug.evaluate(_usage(session_util=10.0, weekly_util=10.0), NOW)
    assert ug.parallel_slots(d, 3) == 3


def test_parallel_slots_tight_weekly_stays_serial():
    # weekly line = 57.1 + 12 = 69.1; util 66 -> slack ~3pp < one 5pp step
    d = ug.evaluate(_usage(weekly_util=66.0), NOW)
    assert d["allowed"]
    assert ug.parallel_slots(d, 3) == 1


def test_parallel_slots_one_step_of_weekly_slack_earns_two():
    # slack ~7pp -> 1 + 1 step
    d = ug.evaluate(_usage(weekly_util=62.0), NOW)
    assert ug.parallel_slots(d, 3) == 2


def test_parallel_slots_hard_cap_bounds_the_line():
    # Late in the week elapsed + allowance exceeds the hard cap; the cap is the
    # line, so utilization just under it earns no extra slot.
    cap = ug._cfg()["weekly_hard_cap"]
    d = ug.evaluate(_usage(weekly_util=cap - 2.0, weekly_reset_in=3600.0), NOW)
    assert d["allowed"]
    assert ug.parallel_slots(d, 3) == 1


def test_parallel_slots_blocked_is_zero():
    d = ug.evaluate(_usage(weekly_util=ug._cfg()["weekly_hard_cap"] + 1.0), NOW)
    assert ug.parallel_slots(d, 3) == 0


def test_parallel_slots_untrusted_numbers_never_fan_out():
    fail_open = {"allowed": True, "windows": [], "stale": True}
    assert ug.parallel_slots(fail_open, 3) == 1
    stale = ug.evaluate(_usage(), NOW)
    stale["stale"] = True
    assert ug.parallel_slots(stale, 3) == 1


def test_parallel_slots_ceiling_of_one_is_serial():
    assert ug.parallel_slots(ug.evaluate(_usage(), NOW), 1) == 1


def test_parallel_slots_step_env_override(monkeypatch):
    monkeypatch.setenv("USAGE_GUARD_WEEKLY_PARALLEL_STEP", "100")
    d = ug.evaluate(_usage(), NOW)
    assert ug.parallel_slots(d, 3) == 1


# ---------------------------------------------------------------------------
# window_slack — shared headroom formula (parallel_slots + planner_mode)
# ---------------------------------------------------------------------------


def test_window_slack_matches_hand_computed_line():
    d = ug.evaluate(_usage(session_util=10.0, weekly_util=30.0), NOW)
    by_name = {w["name"]: w for w in d["windows"]}
    # session: 1h left of 5h -> 80% elapsed; min(80 + 30, 90) = 90; 90 - 10 = 80
    assert ug.window_slack(by_name["session"]) == pytest.approx(80.0)
    # weekly: 3d left of 7d -> 57.1% elapsed; 57.1 + 12 = 69.1; 69.1 - 30 = 39.1
    assert ug.window_slack(by_name["weekly"]) == pytest.approx(39.1)
    # past the line -> floored at 0, never negative
    assert ug.window_slack({"name": "weekly", "utilization": 85.0,
                            "elapsed_pct": 10.0}) == 0.0


def test_window_slack_none_without_evidence():
    assert ug.window_slack({"name": "opus", "utilization": 1.0, "elapsed_pct": 50.0}) is None
    assert ug.window_slack({"name": "weekly", "utilization": None, "elapsed_pct": 50.0}) is None
    assert ug.window_slack({"name": "session", "utilization": 5.0, "elapsed_pct": None}) is None


# ---------------------------------------------------------------------------
# hermetic-env regression: a host-exported temporary usage-burn override
# (e.g. a weekly-pacing adjustment) must not desync these modules' math
# ---------------------------------------------------------------------------


def test_hermetic_against_host_usage_guard_env():
    env = dict(os.environ)
    env["USAGE_GUARD_WEEKLY_ALLOWANCE"] = "100"
    env["USAGE_GUARD_WEEKLY_HARD_CAP"] = "90"
    env["USAGE_GUARD_SESSION_HARD_CAP"] = "90"
    result = subprocess.run(
        [sys.executable, "-m", "pytest",
         "todo_system/tests/test_usage_guard.py",
         "todo_system/tests/test_planner_mode.py",
         "-q", "-p", "no:cacheprovider",
         "--deselect",
         "todo_system/tests/test_usage_guard.py::"
         "test_hermetic_against_host_usage_guard_env"],
        env=env, cwd=str(Path(__file__).resolve().parents[2]),
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]


def test_disable_switch_allows_without_fetching(monkeypatch):
    # README quick start sets USAGE_GUARD_DISABLE=1 on a host whose real account
    # may be ahead of pace; check() must allow and must not fetch or persist.
    import todo_system.usage_guard as ug
    monkeypatch.setenv("USAGE_GUARD_DISABLE", "1")
    def boom(*a, **k):
        raise AssertionError("must not fetch when disabled")
    monkeypatch.setattr(ug, "get_snapshot", boom)
    monkeypatch.setattr(ug, "_save_state", boom)
    d = ug.check()
    assert d["allowed"] is True and d.get("disabled") is True
    assert ug.check(account="other")["allowed"] is True


def test_disable_switch_only_on_exact_one(monkeypatch):
    import todo_system.usage_guard as ug
    monkeypatch.setenv("USAGE_GUARD_DISABLE", "0")
    monkeypatch.setattr(ug, "get_snapshot", lambda **k: {"error": "x"})
    monkeypatch.setattr(ug, "_save_state", lambda s: None)
    assert ug.check().get("disabled") is None
