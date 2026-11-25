"""AC6 regression set for macp-multi-account-parallel-capacity — the six
capacity scenarios gathered in ONE place so the contract reads at a glance:

  1. one account             → byte-identical to the former single-account sizing
  2. two accounts            → the per-account slots SUM
  3. one account blocked     → only the rest count
  4. assignment order        → widest slack first, ties by priority
  5. a project pin           → forces that account, ignoring slack/balance
  6. global cap              → RUNNER_MAX_PARALLEL cuts the sum (when set)

The deepest per-seam tests live with their phase (account_slots +
check(account=) in test_claude_accounts_switch.py; _account_env / _assign_account
/ poll_parallel in test_runner.py + test_claude_accounts_active.py); this file
pins the end-to-end behaviour as a single readable set.

Everything runs under tmp: HOME, the account/shared roots, the runtime-state and
usage-guard state files, and MACP_HOME are all redirected and the USAGE_GUARD_*
threshold overrides a host may export are stripped (else the hand-computed slot
math desyncs). No network — every usage fetch seam is monkeypatched. Account
names are the neutral school / dev; paths are tmp.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from todo_system import usage_guard as ug
from todo_system.macp import accounts as ACC
from todo_system.macp import config as C

NOW = 1_800_000_000.0


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Redirect every root the capacity path reads, and strip host USAGE_GUARD_*
    overrides so the slot arithmetic is deterministic."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MACP_CLAUDE_SHARED_ROOT", str(tmp_path / "shared"))
    monkeypatch.setenv("MACP_CLAUDE_ACCOUNTS_ROOT", str(tmp_path / "accounts"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    macp_home = tmp_path / "macp"
    monkeypatch.setattr(C, "HOME", macp_home)
    monkeypatch.setattr(C, "PROFILES", macp_home / "profiles")
    monkeypatch.setattr(C, "GLOBAL_CONFIG", macp_home / "config.toml")
    monkeypatch.setenv("MACP_CLAUDE_ACCOUNTS_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path / "ug"))
    for name in list(os.environ):
        if name.startswith("USAGE_GUARD_") and name != "USAGE_GUARD_STATE_DIR":
            monkeypatch.delenv(name, raising=False)
    for name in ("RUNNER_MAX_PARALLEL", "RUNNER_SLOTS_STATE"):
        monkeypatch.delenv(name, raising=False)
    return SimpleNamespace(tmp=tmp_path, accounts=tmp_path / "accounts")


def _iso_in(seconds: float) -> str:
    return datetime.fromtimestamp(NOW + seconds, tz=timezone.utc).isoformat()


def _usage(session_util=10.0, session_reset_in=3600.0,
           weekly_util=10.0, weekly_reset_in=3 * 86400.0) -> dict:
    return {"five_hour": {"utilization": session_util, "resets_at": _iso_in(session_reset_in)},
            "seven_day": {"utilization": weekly_util, "resets_at": _iso_in(weekly_reset_in)}}


def _profile(env, monkeypatch, accounts, active="school", *, cwd_pin=None) -> dict:
    """Write profile ``p`` (claude accounts owning dirs under the accounts root)
    and chdir into a project pinned to it (``cwd_pin`` pins an account). Returns
    ``{name: dir}``. `monkeypatch.chdir` so the cwd is restored after the test."""
    dirs: dict[str, Path] = {}
    accs = []
    for name, prio in accounts:
        d = env.accounts / name
        d.mkdir(parents=True, exist_ok=True)
        dirs[name] = d
        accs.append(C.Account(name=name, claude_config_dir=str(d), priority=prio))
    C.write_profile(C.Profile(name="p", accounts=accs, active_account=active))
    proj = env.tmp / "proj"
    (proj / ".macp").mkdir(parents=True, exist_ok=True)
    text = 'profile = "p"\n' + (f'account = "{cwd_pin}"\n' if cwd_pin else "")
    (proj / ".macp" / "config.toml").write_text(text, encoding="utf-8")
    monkeypatch.chdir(proj)
    return dirs


def _seed_inactive(monkeypatch, **usage_kw):
    """Store the inactive account's decision snapshot (what account_slots reads
    for a non-active account)."""
    monkeypatch.setattr(ug, "_fetch_usage_for", lambda acct: _usage(**usage_kw))
    ug.refresh_inactive_accounts(now=NOW)


# 1. one account → byte-identical to the former single-account sizing ----------

def test_one_account_is_byte_identical(env, monkeypatch):
    _profile(env, monkeypatch, [("school", 20)])
    monkeypatch.setattr(ug, "_fetch_usage", lambda: _usage())
    # RUNNER_MAX_PARALLEL unset → global_cap None; total == parallel_slots(check(), N)
    assert ug.account_slots(4, None, now=NOW)["total"] == ug.parallel_slots(ug.check(now=NOW), 4)


# 2. two accounts → the per-account slots sum ----------------------------------

def test_two_accounts_sum(env, monkeypatch):
    _profile(env, monkeypatch, [("school", 20), ("dev", 10)])
    monkeypatch.setattr(ug, "_fetch_usage", lambda: _usage())     # school wide open → 3
    _seed_inactive(monkeypatch)                                   # dev wide open → 3
    assert ug.account_slots(3, now=NOW)["total"] == 6


# 3. one account blocked → only the rest count ---------------------------------

def test_blocked_account_drops_out(env, monkeypatch):
    _profile(env, monkeypatch, [("school", 20), ("dev", 10)])
    monkeypatch.setattr(ug, "_fetch_usage", lambda: _usage())                       # school → 3
    _seed_inactive(monkeypatch, weekly_util=ug._cfg()["weekly_hard_cap"] + 1.0)      # dev blocked → 0
    cap = ug.account_slots(3, now=NOW)
    assert cap["per_account"]["dev"] == 0 and cap["total"] == 3

    # a sticky-unavailable account is dropped entirely (not even a 0 entry)
    _seed_inactive(monkeypatch)                                                      # dev back wide open
    ACC.mark_unavailable("dev", "auth", now=NOW, config_dir=env.accounts / "dev")
    cap = ug.account_slots(3, now=NOW)
    assert "dev" not in cap["per_account"] and cap["total"] == 3


# 4. assignment order → widest slack first, ties by priority -------------------

def test_order_is_widest_slack_then_priority(env, monkeypatch):
    _profile(env, monkeypatch, [("school", 20), ("dev", 10)])
    monkeypatch.setattr(ug, "_fetch_usage", lambda: _usage(weekly_util=62.0))   # school slack ~7pp
    _seed_inactive(monkeypatch)                                                 # dev slack ~59pp
    assert ug.account_slots(3, now=NOW)["order"] == ["dev", "school"]

    # a tie on slack breaks by priority (dev prio 10 < school prio 20 → dev first)
    monkeypatch.setattr(ug, "_fetch_usage", lambda: _usage())
    _seed_inactive(monkeypatch)                                                 # both wide open, equal slack
    assert ug.account_slots(3, now=NOW)["order"] == ["dev", "school"]


# 5. a project pin forces that account, ignoring slack / balance ---------------

def test_project_pin_forces_the_account(env, monkeypatch):
    from todo_system.orchestrator import stages
    from todo_system.orchestrator.mailbox import FileMailbox
    from todo_system.orchestrator.registry import Agent
    from todo_system.orchestrator.runner import Runner, ShadowExecutor

    _profile(env, monkeypatch, [("school", 20), ("dev", 10)], cwd_pin="school")
    monkeypatch.setattr(ug, "_fetch_usage", lambda: _usage())
    _seed_inactive(monkeypatch)
    cap = ug.account_slots(3, now=NOW)
    assert cap["order"][0] == "dev"               # dev is the natural first (equal slack, prio 10)
    # the vault anchor is the pinned project dir
    monkeypatch.setattr(stages, "_VAULT_DATA_ROOT", env.tmp / "proj")
    runner = Runner(FileMailbox(env.tmp / "agents"), Agent(id="a", kind="ai-local"),
                    ShadowExecutor(), log=lambda *_: None)
    # a vault task (no repo) → the pin (school) wins over the dev-first slack order,
    # and ignores school's in-flight count
    assert runner._assign_account({}, "t", cap, {"school": 99}) == "school"


# 6. global cap cuts the sum (only when RUNNER_MAX_PARALLEL is set) -------------

def test_global_cap_cuts_the_sum(env, monkeypatch):
    _profile(env, monkeypatch, [("school", 20), ("dev", 10)])
    monkeypatch.setattr(ug, "_fetch_usage", lambda: _usage())
    _seed_inactive(monkeypatch)
    assert ug.account_slots(3, None, now=NOW)["total"] == 6     # uncapped
    assert ug.account_slots(3, 4, now=NOW)["total"] == 4        # cut to the cap
