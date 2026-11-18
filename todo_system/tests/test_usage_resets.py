"""Limit-reset grants: pacing with them and claiming them (2026-11-17)."""
from __future__ import annotations

import datetime as dt
import json
import shutil
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from todo_system import usage_guard as U
from todo_system import usage_resets as R

NOW = dt.datetime(2026, 11, 17, 13, 0, tzinfo=dt.timezone.utc).timestamp()


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


def _usage(util, hours_to_reset, fable_pct=None):
    u = {"five_hour": {"utilization": 10.0, "resets_at": _iso(NOW + 3600)},
         "seven_day": {"utilization": util, "resets_at": _iso(NOW + hours_to_reset * 3600)},
         "limits": []}
    if fable_pct is not None:
        u["limits"].append({"kind": "weekly_scoped", "percent": fable_pct,
                            "scope": {"model": {"id": None, "display_name": "Fable"}}})
    return u


FULL = {"id": "full-reset-2611", "label": "One full limit reset", "resets_total": 1,
        "resets_left": 1, "ends_at": _iso(NOW + 20 * 86400), "clears": ["five_hour", "seven_day",
        "seven_day_overage_included"], "paused": False, "usable_now": True, "type": "full"}
FABLE = {"id": "launch-2610-fable", "label": "Fable reset", "resets_total": 1, "resets_left": 1,
         "ends_at": _iso(NOW + 100 * 86400), "clears": ["five_hour", "seven_day_overage_included"],
         "paused": False, "usable_now": True, "type": "full"}


def _st(*grants, **kw):
    return {"eligible": True, "grants": list(grants), "cooldown_until": None, **kw}


@pytest.fixture(autouse=True)
def _default_pacing(monkeypatch):
    # Pin the guard's defaults: a shell that still carries USAGE_GUARD_* overrides
    # (e.g. a burn-mode env) must not change these pacing expectations.
    import os
    for k in [k for k in os.environ if k.startswith("USAGE_GUARD_")]:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def state_dir(monkeypatch):
    d = Path(tempfile.mkdtemp())
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(d))
    monkeypatch.delenv("USAGE_RESETS_DISABLE", raising=False)
    monkeypatch.delenv("USAGE_RESETS_AUTOCLAIM", raising=False)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_grant_filters():
    st = _st(FULL, FABLE, {**FULL, "id": "used", "resets_left": 0},
             {**FULL, "id": "paused", "paused": True}, {**FULL, "id": "BAD ID"})
    assert [g["id"] for g in R.full_grants(st, NOW)] == ["full-reset-2611"]
    assert [g["id"] for g in R.model_grants(st, NOW)] == ["launch-2610-fable"]
    assert R.full_grants({**st, "eligible": False}, NOW) == []
    expired = {**FULL, "ends_at": _iso(NOW - 60)}
    assert R.full_grants(_st(expired), NOW) == []


def test_weekly_line_spreads_extra_windows():
    assert R.weekly_line(40.0, 12.0, 0, 0) == 52.0
    assert R.weekly_line(40.0, 12.0, 1, 0) == 92.0          # two windows' budget
    assert R.weekly_line(60.0, 12.0, 0, 1) == 32.0          # one already spent


def test_evaluate_widens_pace_with_resets(monkeypatch):
    usage = _usage(80.0, hours_to_reset=7 * 24 * 0.6)       # 40% elapsed
    plain = U.evaluate(usage, NOW)
    assert not plain["allowed"]                              # 80 > 40 + 12
    widened = U.evaluate(usage, NOW, resets={"available": 1, "claimed": 0})
    weekly = [w for w in widened["windows"] if w["name"] == "weekly"][0]
    assert widened["allowed"] and weekly["pace_line"] == pytest.approx(92.0, abs=0.2)
    # the slack the planner/runner read follows the widened line
    assert U.window_slack(weekly) == pytest.approx(10.0, abs=0.3)   # min(92, cap 90) - 80


def test_evaluate_pace_hold_releases_on_widened_schedule():
    usage = _usage(85.0, hours_to_reset=7 * 24 * 0.67)      # 33% elapsed, under the cap
    d = U.evaluate(usage, NOW, resets={"available": 1, "claimed": 0})
    assert not d["allowed"] and "reset(s) left" in d["reason"]
    # catch-up when elapsed%*2 + 12 == 85 → elapsed 36.5%
    resets_at = NOW + 7 * 24 * 0.67 * 3600
    expect = resets_at - 7 * 86400 * (1 - 0.365)
    assert d["blocked_until"] == pytest.approx(expect, abs=600)


def test_plan_claim_full_at_cap_on_pace():
    usage = _usage(95.0, hours_to_reset=7 * 24 * 0.5)       # 50% elapsed, line 112
    plan = R.plan_claim(usage, _st(FULL, FABLE), NOW, hard_cap=90, allowance=12, claimed=0)
    assert plan and plan[0] == "full-reset-2611"


def test_plan_claim_holds_when_ahead_of_pace_or_reset_near_or_cooldown():
    st = _st(FULL)
    ahead = _usage(94.0, hours_to_reset=7 * 24 * 0.67)      # line 78 < 94
    assert R.plan_claim(ahead, st, NOW, hard_cap=90, allowance=12, claimed=0) is None
    near = _usage(95.0, hours_to_reset=3)
    assert R.plan_claim(near, st, NOW, hard_cap=90, allowance=12, claimed=0) is None
    ok = _usage(95.0, hours_to_reset=7 * 24 * 0.5)
    assert R.plan_claim(ok, st, NOW, hard_cap=90, allowance=12, claimed=0,
                        last_attempt=NOW - 60) is None
    assert R.plan_claim(ok, _st(FULL, cooldown_until=_iso(NOW + 600)), NOW,
                        hard_cap=90, allowance=12, claimed=0) is None
    below_cap = _usage(70.0, hours_to_reset=7 * 24 * 0.5)
    assert R.plan_claim(below_cap, st, NOW, hard_cap=90, allowance=12, claimed=0) is None


def test_plan_claim_model_grant_when_model_exhausted():
    usage = _usage(30.0, hours_to_reset=7 * 24 * 0.5, fable_pct=100)
    plan = R.plan_claim(usage, _st(FABLE), NOW, hard_cap=90, allowance=12, claimed=0)
    assert plan and plan[0] == "launch-2610-fable"
    not_exhausted = _usage(30.0, hours_to_reset=7 * 24 * 0.5, fable_pct=60)
    assert R.plan_claim(not_exhausted, _st(FABLE), NOW, hard_cap=90, allowance=12,
                        claimed=0) is None


def test_maybe_claim_records_and_counts_claimed(state_dir, monkeypatch):
    monkeypatch.setattr(U, "get_snapshot", lambda **kw: {})
    calls = []
    usage = _usage(95.0, hours_to_reset=7 * 24 * 0.5)
    out = R.maybe_claim(usage, now=NOW, fetch=lambda: _st(FULL),
                        claim=lambda gid: calls.append(gid) or {
                            "result": "reset", "resets_left": 0,
                            "cleared": ["five_hour", "seven_day"]})
    assert calls == ["full-reset-2611"] and out["result"] == "reset"
    assert R.claimed_in_window(usage["seven_day"]["resets_at"]) == 1
    # the cooldown stops a second claim on the next tick
    again = R.maybe_claim(usage, now=NOW + 60, fetch=lambda: _st(FULL),
                          claim=lambda gid: calls.append(gid) or {"result": "reset"})
    assert again is None and calls == ["full-reset-2611"]
    # pacing now counts the spent reset
    inputs = R.pacing_inputs(usage, NOW + 60)
    assert inputs and inputs["claimed"] == 1


def test_switches(state_dir, monkeypatch):
    usage = _usage(95.0, hours_to_reset=7 * 24 * 0.5)
    monkeypatch.setenv("USAGE_RESETS_AUTOCLAIM", "0")
    assert R.maybe_claim(usage, now=NOW, fetch=lambda: _st(FULL),
                         claim=lambda g: pytest.fail("must not claim")) is None
    monkeypatch.setenv("USAGE_RESETS_DISABLE", "1")
    assert R.status(now=NOW, fetch=lambda: pytest.fail("must not read")) is None
    assert R.pacing_inputs(usage, NOW) is None


def test_check_passes_cached_grants_into_the_verdict(state_dir, monkeypatch):
    usage = _usage(80.0, hours_to_reset=7 * 24 * 0.6)
    (state_dir / "latest.json").write_text(json.dumps({"fetched_at": NOW, "usage": usage}))
    (state_dir / "resets.json").write_text(json.dumps({"status": _st(FULL), "status_at": NOW}))
    d = U.check(now=NOW)
    assert d["allowed"]                                       # held without the grant
    monkeypatch.setenv("USAGE_RESETS_DISABLE", "1")
    assert not U.check(now=NOW)["allowed"]


def test_orchestrator_usage_hold_calls_maybe_claim(monkeypatch, tmp_path):
    """Wiring: the tick's usage_hold() runs the claim hook before the verdict."""
    from todo_system.orchestrator.assignment import Ledger
    from todo_system.orchestrator.mailbox import FileMailbox
    from todo_system.orchestrator.orchestrator import Orchestrator
    from todo_system.orchestrator.registry import Registry

    monkeypatch.delenv("USAGE_RESETS_DISABLE", raising=False)
    orch = Orchestrator(FileMailbox(tmp_path / "agents"), Registry([]),
                        Ledger(tmp_path / "assignments"), notify=lambda t, level=None: None)
    usage = _usage(95.0, 80)
    seen = []
    with mock.patch.object(U, "get_snapshot", return_value={"usage": usage}), \
            mock.patch.object(R, "maybe_claim", side_effect=lambda u, **kw: seen.append(u)), \
            mock.patch.object(U, "check", return_value={"allowed": True}):
        assert orch.usage_hold() is None
    assert seen == [usage]


# -- hard-cap hold ETA with an auto-claimed reset (2026-11-18) ---------------

def test_hard_cap_release_is_the_post_claim_pace_point():
    from todo_system import usage_resets as R
    W = 7 * 86400.0
    resets_at = 1_000_000_000.0
    # 1 full reset, none claimed, allowance 12: claim at elapsed 41.5%,
    # post-reset (util ~0) pace line 2e-88 >= 0 at elapsed 44% -> release.
    eta = R.hard_cap_release_at(95.0, resets_at, W, 12.0, 1, 0)
    assert abs(eta - (resets_at - W * 0.56)) < 1.0
    assert R.hard_cap_release_at(95.0, resets_at, W, 12.0, 0, 0) is None


def test_hard_cap_release_none_when_claim_lands_too_close_to_reset():
    from todo_system import usage_resets as R
    W = 7 * 86400.0
    # util 99 with allowance 0 -> claim at 99.5% elapsed = 0.84h before reset < 6h
    assert R.hard_cap_release_at(199.0, 1e9, W, 0.0, 1, 0) is None


def test_evaluate_hard_cap_eta_uses_autoclaimed_reset():
    from todo_system import usage_guard as G
    now = 1_000_000_000.0
    W = 7 * 86400.0
    resets_at = now + W * 0.66            # elapsed 34%
    usage = {"seven_day": {"utilization": 95.0, "resets_at": _iso(resets_at)},
             "five_hour": {"utilization": 2.0, "resets_at": _iso(now + 3600)}}
    plain = G.evaluate(usage, now=now, resets={"available": 1, "claimed": 0})
    assert plain["blocked_until"] == pytest.approx(resets_at, abs=1)   # no autoclaim -> reset
    auto = G.evaluate(usage, now=now, resets={"available": 1, "claimed": 0, "autoclaim": True})
    assert auto["blocked_until"] == pytest.approx(resets_at - W * 0.56, abs=1)
    assert "auto-claimed limit reset" in auto["reason"]
    assert "hard cap" in auto["reason"]   # hold kind classification keys on this
