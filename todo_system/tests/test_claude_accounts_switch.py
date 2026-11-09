"""Regression tests for leaf 4 of ``macp-multi-claude-account`` — per-account
usage and the automatic account switch: ``accounts.classify_capacity`` and the
runtime state (``Worker/_state/claude_accounts.json``: unavailable marks,
``active_override``, ``last_switch``, ``paused_since``), the override step of
``resolve_active`` / ``config.current_account``, the runner's rotate hook on the
capacity deferral, the orchestrator's once-only switch / PAUSED alerts, the usage
guard's per-account snapshots and PAUSED hold, ``macp account use`` clearing a
mark, and the dashboard's ``accounts[] / paused / last_switch`` fields.

Everything runs under ``tmp_path``. The ``roots`` fixture (copied from
``test_claude_accounts_active.py``, not imported) redirects HOME, the two layout
roots, CLAUDE_CONFIG_DIR and the config module's import-time MACP_HOME paths;
``iso`` adds the three seams this leaf introduces or touches — the runtime state
file (``MACP_CLAUDE_ACCOUNTS_STATE``), the usage guard state dir
(``USAGE_GUARD_STATE_DIR``) and the model-choice cache (``model_select._CACHE``)
— and proves the redirection before a test body runs. No ``claude`` binary is
run and no network is touched: every fetch seam is monkeypatched, and the runner
harness fakes the stage subprocess. Account names are ``school`` / ``dev``, dirs
are under tmp, tokens are fakes shaped like ``test_claude_auth.DUMMY_TOKEN``.

No literal from the identifier gate's PATTERN appears in this file: it sits
inside the scanned engine tree.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import time
import urllib.error
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from todo_system.macp import accounts as ACC
from todo_system.macp import config as C

NOW = 1_800_000_000.0  # fixed epoch for deterministic math (test_usage_guard.NOW)
HOLD = ACC.EXHAUSTED_DEFAULT_HOLD_S

TOKEN_SCHOOL = "sk-ant-oat01-" + "s" * 40
TOKEN_DEV = "sk-ant-oat01-" + "d" * 40


@pytest.fixture
def roots(tmp_path, monkeypatch):
    """Redirect every root the code can touch into ``tmp_path`` and prove it."""
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
    # Self-check before any test body runs (a typo'd attribute name would
    # silently leave a real path in place).
    assert Path.home() == home
    assert ACC.resolve_shared_root().is_relative_to(tmp_path)
    assert ACC.resolve_accounts_root().is_relative_to(tmp_path)
    assert ACC.legacy_config_dir().is_relative_to(tmp_path)
    assert ACC.legacy_claude_json().is_relative_to(tmp_path)
    assert C.PROFILES.is_relative_to(tmp_path)
    return SimpleNamespace(tmp=tmp_path, home=home,
                           shared=tmp_path / "shared", accounts=tmp_path / "accounts")


@pytest.fixture
def iso(roots, monkeypatch):
    """``roots`` plus the leaf-4 seams: runtime state file, usage guard state
    dir and model cache, all under tmp — and the proof that they took."""
    from todo_system.orchestrator import model_select as MS

    state = roots.tmp / "claude_accounts.json"
    monkeypatch.setenv("MACP_CLAUDE_ACCOUNTS_STATE", str(state))
    ug_dir = roots.tmp / "ug"
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(ug_dir))
    cache = roots.tmp / "model_pref.json"
    monkeypatch.setattr(MS, "_CACHE", cache)
    assert ACC.runtime_state_path() == state
    assert MS._CACHE.is_relative_to(roots.tmp)
    roots.state = state
    roots.ug = ug_dir / "latest.json"
    roots.cache = cache
    return roots


def _acct(name, agent="claude", priority=100, config_dir=""):
    return SimpleNamespace(name=name, agent=agent, priority=priority, claude_config_dir=config_dir)


def _write_profile(roots, name, accounts, active=""):
    """Write profile ``name`` whose claude accounts each own (and get) the dir
    ``<accounts root>/<account>``; ``accounts`` = ``[(name, priority), ...]``.
    Returns ``{account name: dir}``."""
    dirs: dict[str, Path] = {}
    accs = []
    for aname, prio in accounts:
        d = roots.accounts / aname
        d.mkdir(parents=True, exist_ok=True)
        dirs[aname] = d
        accs.append(C.Account(name=aname, claude_config_dir=str(d), priority=prio))
    C.write_profile(C.Profile(name=name, accounts=accs, active_account=active))
    return dirs


def _project(root: Path, profile="p", account=None) -> Path:
    """A ``.macp/config.toml`` marker at ``root`` (pin ``account`` when given)."""
    (root / ".macp").mkdir(parents=True, exist_ok=True)
    text = f'profile = "{profile}"\n'
    if account:
        text += f'account = "{account}"\n'
    (root / ".macp" / "config.toml").write_text(text, encoding="utf-8")
    return root


def _state(iso) -> dict:
    return json.loads(iso.state.read_text(encoding="utf-8"))


# ---- AC1: classifier + the byte-identity pins -----------------------------------

def test_classify_capacity_markers():
    from todo_system.orchestrator import model_select as MS
    from todo_system.orchestrator import stages

    for mrk in ACC.EXHAUSTED_MARKERS:
        assert ACC.classify_capacity(f"x {mrk} y") == "exhausted", mrk
    for mrk in ACC.AUTH_MARKERS:
        assert ACC.classify_capacity(f"x {mrk} y") == "auth", mrk
    for mrk in ACC.NO_SUBSCRIPTION_MARKERS:
        assert ACC.classify_capacity(f"x {mrk} y") == "no_subscription", mrk
    for mrk in ACC.TRANSIENT_MARKERS:
        assert ACC.classify_capacity(f"x {mrk} y") == "transient", mrk
    # the CLI's own wordings
    assert ACC.classify_capacity("You're out of usage credits · resets 6pm") == "exhausted"
    assert ACC.classify_capacity(
        "You've reached your Fable 5 limit. Run /usage-credits to continue or switch "
        "models with /model.") == "exhausted"
    assert ACC.classify_capacity("Not logged in · Please run /login") == "auth"
    assert ACC.classify_capacity('API Error: 401 {"type":"authentication_error"}') == "auth"
    assert ACC.classify_capacity("Invalid API key · Please run /login") == "auth"
    assert ACC.classify_capacity("No active subscription found. Visit /upgrade") == "no_subscription"
    assert ACC.classify_capacity("API Error: 429 rate limit exceeded") == "transient"
    assert ACC.classify_capacity("overloaded_error") == "transient"
    assert ACC.classify_capacity("all good") is None
    assert ACC.classify_capacity("") is None and ACC.classify_capacity(None) is None
    # precedence when several kinds match: auth > no_subscription > exhausted > transient
    assert ACC.classify_capacity("Not logged in … run /usage-credits") == "auth"
    assert ACC.classify_capacity("upgrade your plan: usage limit reached") == "no_subscription"
    assert ACC.classify_capacity("usage limit hit; rate limit too") == "exhausted"
    assert ACC.classify_capacity("rate limit · overloaded") == "transient"
    # the numeric marker needs digit boundaries — a number that merely contains
    # 401 must not turn an exhausted stage into a sticky auth mark
    assert ACC.classify_capacity("line 1401: You're out of usage credits") == "exhausted"
    assert ACC.classify_capacity("read 4010 bytes · weekly limit") == "exhausted"
    assert ACC.classify_capacity("status=401") == "auth"
    assert ACC.classify_capacity("HTTP 401 Unauthorized") == "auth"
    # case-insensitive
    assert ACC.classify_capacity("WEEKLY LIMIT") == "exhausted"
    assert ACC.classify_capacity("OVERLOADED") == "transient"
    assert ACC.SWITCH_REASONS == ("exhausted", "auth", "no_subscription")

    # byte-identity pins: the deferral gate and the model cache keep their tuples
    assert stages._CAPACITY_MARKERS == (
        "out of usage credits", "usage limit", "weekly limit", "hit your limit",
        "rate limit", "overloaded", "please run /login", "not logged in",
        "reached your", "/usage-credits")
    assert MS._UNAVAILABLE_MARKERS == (
        "out of usage credits", "usage limit", "rate limit", "insufficient credits",
        "reached your", "/usage-credits")
    assert stages.is_capacity_limit("x overloaded y") and not stages.is_capacity_limit("fine")
    assert stages.usage_shows_no_api_work({"unavailable": "no usage in transcript"})
    assert not stages.usage_shows_no_api_work(None)
    assert MS.model_unavailable("rate limit") and not MS.model_unavailable("not logged in")


# ---- AC2: runtime state file ------------------------------------------------------

def test_runtime_state_roundtrip_and_isolation(iso, monkeypatch):
    school = iso.accounts / "school"
    school.mkdir(parents=True)
    a_school = _acct("school", config_dir=str(school))
    assert ACC.load_runtime_state() == {} and not iso.state.exists()
    assert ACC.unavailable_entry(a_school) is None and not iso.state.exists()   # a read creates nothing

    st = ACC.mark_unavailable("school", "exhausted", now=NOW, config_dir=school)
    e = st["unavailable"]["school"]
    assert e["reason"] == "exhausted"
    assert ACC.epoch_from_iso(e["until"]) == NOW + HOLD          # no window known → 5h
    assert ACC.epoch_from_iso(e["since"]) == NOW
    assert e["config_dir"] == str(school)
    assert e["cred_mtime"] is None and e["token_mtime"] is None   # no auth files yet
    assert _state(iso) == st
    # an explicit until (epoch) is kept; a past one falls back to the default hold
    e = ACC.mark_unavailable("school", "exhausted", NOW + 100, now=NOW)["unavailable"]["school"]
    assert ACC.epoch_from_iso(e["until"]) == NOW + 100
    e = ACC.mark_unavailable("school", "exhausted", NOW - 1, now=NOW)["unavailable"]["school"]
    assert ACC.epoch_from_iso(e["until"]) == NOW + HOLD
    # auth / no_subscription are sticky: until null, whatever was passed
    st = ACC.mark_unavailable("dev", "auth", NOW + 100, now=NOW)
    assert st["unavailable"]["dev"]["until"] is None
    assert st["unavailable"]["school"]["reason"] == "exhausted"   # read-merge-write kept it
    assert ACC.mark_unavailable("dev", "no_subscription", now=NOW)["unavailable"]["dev"]["until"] is None
    assert ACC.until_epoch(st["unavailable"]["dev"]) is None
    assert ACC.until_epoch(st["unavailable"]["school"]) == NOW + HOLD
    assert ACC.until_epoch(None) is None

    # a corrupt or non-object file reads as empty and never raises; the next write replaces it
    iso.state.write_text("{not json", encoding="utf-8")
    assert ACC.load_runtime_state() == {}
    assert ACC.unavailable_entry(a_school, now=NOW) is None
    st = ACC.mark_unavailable("school", "exhausted", now=NOW, config_dir=school)
    assert set(st["unavailable"]) == {"school"}
    iso.state.write_text("[1, 2]", encoding="utf-8")
    assert ACC.load_runtime_state() == {}

    # the dir stamp: an entry written for one dir does not apply to a same-named
    # account registered elsewhere; an unstamped entry applies by name
    ACC.mark_unavailable("school", "exhausted", now=NOW, config_dir=school)
    assert ACC.unavailable_entry(a_school, now=NOW)["reason"] == "exhausted"
    assert ACC.unavailable_entry(_acct("school", config_dir=str(iso.tmp / "elsewhere")), now=NOW) is None
    assert ACC.unavailable_reason(a_school, now=NOW) == "exhausted"
    assert ACC.unavailable_reason(a_school, now=NOW + HOLD) is None          # until elapsed
    assert ACC.unavailable_reason(a_school, now=NOW + HOLD - 1) == "exhausted"
    ACC.mark_unavailable("dev", "auth", now=NOW)
    assert ACC.unavailable_entry(_acct("dev", config_dir=str(iso.tmp / "any")), now=NOW)["reason"] == "auth"
    assert ACC.unavailable_entry(None) is None
    # a state passed in is used as given (no file read)
    assert ACC.unavailable_entry(a_school, {}, NOW) is None
    assert ACC.unavailable_entry(a_school, {"unavailable": "junk"}, NOW) is None
    assert ACC.available_accounts([a_school, _acct("cx", agent="codex")], {}, NOW)[1].name == "cx"

    # explicit path= wins over the env
    other = iso.tmp / "other.json"
    ACC.mark_unavailable("x", "auth", now=NOW, path=other)
    assert "x" in ACC.load_runtime_state(other)["unavailable"]
    assert "x" not in ACC.load_runtime_state()["unavailable"]
    # the time helpers
    assert ACC.epoch_from_iso(ACC.iso_from_epoch(NOW)) == NOW
    assert ACC.epoch_from_iso(NOW) == NOW and ACC.epoch_from_iso("junk") is None
    assert ACC.epoch_from_iso("2027-01-15T08:00:00") == ACC.epoch_from_iso("2027-01-15T08:00:00+00:00")

    # path resolution: env → vault root (resolved at call time) → None without a vault
    monkeypatch.delenv("MACP_CLAUDE_ACCOUNTS_STATE")
    monkeypatch.setenv("MACP_VAULT_ROOT", str(iso.tmp / "vault"))
    assert ACC.runtime_state_path() == iso.tmp / "vault" / "Worker" / "_state" / "claude_accounts.json"
    assert ACC.load_runtime_state() == {}
    monkeypatch.setattr(ACC, "vault_root", lambda start=None: (_ for _ in ()).throw(RuntimeError("no vault")))
    assert ACC.runtime_state_path() is None
    assert ACC.load_runtime_state() == {}
    assert ACC.mark_unavailable("school", "auth", now=NOW) == {
        "unavailable": {"school": {"reason": "auth", "since": ACC.iso_from_epoch(NOW),
                                   "until": None, "config_dir": None}}}   # decided, not persisted
    assert ACC.clear_unavailable("school") is False
    assert ACC.claim_paused_alert() is False


# ---- AC2: override + availability in resolve_active / current_account -----------

def test_resolve_active_override_and_availability(iso):
    school_dir, dev_dir = iso.accounts / "school", iso.accounts / "dev"
    school = _acct("school", priority=20, config_dir=str(school_dir))
    dev = _acct("dev", priority=10, config_dir=str(dev_dir))
    cx = _acct("cx", agent="codex", priority=1)
    prof = SimpleNamespace(accounts=[school, dev, cx], active_account="school")

    # state=None: the pure pre-leaf-4 answer, no file read
    assert ACC.resolve_active(prof) == (school, "profile")
    assert ACC.resolve_active(prof, SimpleNamespace(account="dev")) == (dev, "pin")
    assert ACC.active_account(prof) is school
    # an empty state changes nothing
    assert ACC.resolve_active(prof, state={}, now=NOW) == (school, "profile")
    # the override beats the profile's active_account and priority, not the pin
    st = {"active_override": "dev", "active_override_dir": str(dev_dir)}
    assert ACC.resolve_active(prof, state=st, now=NOW) == (dev, "override")
    assert ACC.resolve_active(prof, SimpleNamespace(account="school"), state=st, now=NOW) == (school, "pin")
    assert ACC.active_account(prof, state=st, now=NOW) is dev
    prof.active_account = ""
    assert ACC.resolve_active(SimpleNamespace(accounts=[school, dev]), state={"active_override": "school",
                              "active_override_dir": str(school_dir)}, now=NOW) == (school, "override")
    prof.active_account = "school"
    # an override stamped for another dir, or naming nothing, falls through
    assert ACC.resolve_active(prof, state={"active_override": "dev",
                              "active_override_dir": str(iso.tmp / "x")}, now=NOW) == (school, "profile")
    assert ACC.resolve_active(prof, state={"active_override": "ghost"}, now=NOW) == (school, "profile")
    assert ACC.resolve_active(prof, state={"active_override": "cx"}, now=NOW) == (school, "profile")
    # an unstamped override applies by name
    assert ACC.resolve_active(prof, state={"active_override": "dev"}, now=NOW) == (dev, "override")

    # unavailable accounts are skipped by every rule, including the pin
    st = ACC.mark_unavailable("school", "exhausted", NOW + 100, now=NOW, config_dir=school_dir)
    assert ACC.resolve_active(prof, state=st, now=NOW) == (dev, "priority")
    assert ACC.resolve_active(prof, SimpleNamespace(account="school"), state=st, now=NOW) == (dev, "priority")
    assert ACC.resolve_active(prof, state=st, now=NOW + 100) == (school, "profile")    # until elapsed
    st["active_override"], st["active_override_dir"] = "school", str(school_dir)
    assert ACC.resolve_active(prof, state=st, now=NOW) == (dev, "priority")          # override unavailable
    # every candidate unavailable → the unfiltered answer (the stage env stays
    # stable); all_unavailable is what signals the pause
    st = ACC.mark_unavailable("dev", "auth", now=NOW, config_dir=dev_dir)
    assert ACC.resolve_active(prof, state=st, now=NOW) == (school, "profile")
    assert ACC.resolve_active(prof, SimpleNamespace(account="dev"), state=st, now=NOW) == (dev, "pin")
    assert ACC.available_accounts([school, dev, cx], st, NOW) == [cx]
    assert ACC.all_unavailable([school, dev, cx], state=st, now=NOW) is True
    assert ACC.all_unavailable([school, dev, cx], state=st, now=NOW + 100) is False
    assert ACC.all_unavailable([cx], state=st, now=NOW) is False
    assert ACC.all_unavailable([], state=st) is False and ACC.all_unavailable(None, state=st) is False
    assert ACC.all_unavailable([school, dev]) is True              # state read from the file

    # config.current_account reads the state file at the env path and honours it
    dirs = _write_profile(iso, "p", [("school", 20), ("dev", 10)], active="school")
    proj = _project(iso.tmp / "proj", profile="p")
    iso.state.unlink()
    assert C.current_account(start=proj)[1] == "profile"
    iso.state.write_text(json.dumps({"active_override": "dev", "active_override_dir": str(dirs["dev"])}),
                         encoding="utf-8")
    acct, src = C.current_account(start=proj)
    assert (acct.name, src) == ("dev", "override")
    assert C.active_config_dir(start=proj) == dirs["dev"]
    ACC.mark_unavailable("dev", "auth", config_dir=dirs["dev"])
    assert C.current_account(start=proj)[0].name == "school"       # override unavailable → profile
    # a broken state file is "no state", never an error
    iso.state.write_text("{", encoding="utf-8")
    assert C.current_account(start=proj)[1] == "profile"


# ---- AC2: sticky marks lift on a credential change or by hand ------------------

def test_sticky_release_by_credentials_mtime(iso):
    school_dir = iso.accounts / "school"
    school_dir.mkdir(parents=True)
    a = _acct("school", config_dir=str(school_dir))
    ACC.mark_unavailable("school", "auth", now=NOW, config_dir=school_dir)
    assert ACC.unavailable_entry(a, now=NOW)["reason"] == "auth"
    assert ACC.unavailable_entry(a, now=NOW + 30 * 86400)["reason"] == "auth"   # no expiry
    # the credentials file appearing (a login) releases the mark
    creds = school_dir / ".credentials.json"
    creds.write_text("{}", encoding="utf-8")
    assert ACC.unavailable_entry(a, now=NOW) is None
    # re-marked with the file present: unchanged file → still sticky; a rewrite releases
    ACC.mark_unavailable("school", "auth", now=NOW, config_dir=school_dir)
    assert ACC.unavailable_entry(a, now=NOW)["reason"] == "auth"
    os.utime(creds, (NOW, NOW + 5))
    assert ACC.unavailable_entry(a, now=NOW) is None
    # deleting the file is not a change (a logout must not release an auth mark)
    ACC.mark_unavailable("school", "no_subscription", now=NOW, config_dir=school_dir)
    creds.unlink()
    assert ACC.unavailable_entry(a, now=NOW)["reason"] == "no_subscription"
    # a replaced automation-token releases as well
    (school_dir / "automation-token").write_text(TOKEN_SCHOOL + "\n", encoding="utf-8")
    assert ACC.unavailable_entry(a, now=NOW) is None
    # marked without a dir: no mtimes recorded → only clear_unavailable lifts it
    ACC.mark_unavailable("school", "auth", now=NOW)
    creds.write_text("{}", encoding="utf-8")
    assert ACC.unavailable_entry(a, now=NOW)["reason"] == "auth"

    # clear_unavailable: True once (and the file changed), False after (no write)
    ACC.mark_unavailable("school", "auth", now=NOW, config_dir=school_dir)
    assert ACC.clear_unavailable("school", config_dir=school_dir) is True
    assert _state(iso)["unavailable"] == {}
    before = iso.state.read_bytes()
    assert ACC.clear_unavailable("school", config_dir=school_dir) is False
    assert iso.state.read_bytes() == before
    # a clear for another dir leaves a stamped mark alone
    ACC.mark_unavailable("school", "auth", now=NOW, config_dir=school_dir)
    assert ACC.clear_unavailable("school", config_dir=iso.tmp / "other") is False
    assert ACC.unavailable_entry(a, now=NOW) is not None
    # … and drops the override / pause markers too
    iso.state.write_text(json.dumps({"active_override": "dev", "active_override_dir": "x",
                                     "paused_since": "t", "paused_notified_since": "t"}),
                         encoding="utf-8")
    assert ACC.clear_unavailable("nobody") is True
    assert _state(iso) == {"active_override": None, "active_override_dir": None,
                           "paused_since": None, "paused_notified_since": None}
    # no state file at all: nothing to clear, nothing created
    iso.state.unlink()
    assert ACC.clear_unavailable("school") is False and not iso.state.exists()


# ---- AC2 / AC4: rotate_active, idempotency, the PAUSED claim ----------------------

def test_rotate_active_idempotent_and_paused(iso):
    dirs = _write_profile(iso, "p", [("school", 20), ("dev", 10)], active="school")
    prof = C.load_profile("p")
    school, dev = prof.accounts

    sw = ACC.rotate_active("school", "exhausted", profile=prof, now=NOW)
    assert sw == {"from": "school", "to": "dev", "reason": "exhausted"}
    st = _state(iso)
    assert st["active_override"] == "dev" and st["active_override_dir"] == str(dirs["dev"])
    assert st["unavailable"]["school"]["reason"] == "exhausted"
    assert st["unavailable"]["school"]["config_dir"] == str(dirs["school"])
    assert ACC.epoch_from_iso(st["unavailable"]["school"]["until"]) == NOW + HOLD
    assert st["last_switch"] == {"from": "school", "to": "dev", "reason": "exhausted",
                                 "ts": ACC.iso_from_epoch(NOW)}
    assert st["paused_since"] is None
    assert ACC.resolve_active(prof, state=st, now=NOW) == (dev, "override")
    # idempotent: the account is already marked → no second switch, file untouched
    before = iso.state.read_bytes()
    assert ACC.rotate_active("school", "exhausted", profile=prof, now=NOW + 1) is None
    assert ACC.rotate_active("school", "auth", profile=prof, now=NOW + 1) is None
    assert iso.state.read_bytes() == before
    # the override survives the mark expiring (dev keeps winning until it is unavailable
    # itself or `account use` resets it)
    assert ACC.resolve_active(prof, state=st, now=NOW + HOLD) == (dev, "override")

    # the last account going down: to=None, paused_since stamped, override dropped
    sw = ACC.rotate_active("dev", "auth", profile=prof, now=NOW + 2)
    assert sw == {"from": "dev", "to": None, "reason": "auth"}
    st = _state(iso)
    assert st["active_override"] is None and st["active_override_dir"] is None
    assert st["paused_since"] == ACC.iso_from_epoch(NOW + 2)
    assert st["unavailable"]["dev"]["until"] is None
    assert st["last_switch"]["to"] is None
    assert ACC.all_unavailable(prof.accounts, now=NOW + 2) is True
    # the unfiltered fallback keeps the env stable while paused
    assert ACC.resolve_active(prof, state=st, now=NOW + 2) == (school, "profile")
    # PAUSED alert claim: exactly once per pause
    assert ACC.claim_paused_alert() is True
    assert ACC.claim_paused_alert() is False
    assert _state(iso)["paused_notified_since"] == st["paused_since"]
    # school's hold elapses → reconcile lifts the pause; a later pause is a new one
    assert ACC.all_unavailable(prof.accounts, now=NOW + HOLD + 1, reconcile=True) is False
    assert _state(iso)["paused_since"] is None
    assert ACC.claim_paused_alert() is False
    assert ACC.all_unavailable(prof.accounts, now=NOW + 10, reconcile=True) is True
    assert _state(iso)["paused_since"] == ACC.iso_from_epoch(NOW + 10)
    assert ACC.claim_paused_alert() is True
    assert ACC.claim_paused_alert() is False
    # reconcile writes only on change
    before = iso.state.read_bytes()
    assert ACC.all_unavailable(prof.accounts, now=NOW + 11, reconcile=True) is True
    assert iso.state.read_bytes() == before

    # school's window resets → the tick's reconcile lifts the pause (the stage
    # runs on school via the unfiltered fallback until then); school hitting the
    # wall again is a fresh pause, with the explicit until honoured
    assert ACC.all_unavailable(prof.accounts, now=NOW + HOLD + 1, reconcile=True) is False
    assert _state(iso)["paused_since"] is None
    assert ACC.rotate_active("school", "exhausted", until=NOW + HOLD + 3600, profile=prof,
                             now=NOW + HOLD + 2) == {"from": "school", "to": None, "reason": "exhausted"}
    st = _state(iso)
    assert ACC.epoch_from_iso(st["unavailable"]["school"]["until"]) == NOW + HOLD + 3600
    assert st["paused_since"] == ACC.iso_from_epoch(NOW + HOLD + 2)      # a fresh pause
    assert ACC.claim_paused_alert() is True
    # …and `account use dev` by hand lifts everything (school stays marked until
    # its window; once that passes it is a candidate again and can be re-marked)
    assert ACC.clear_unavailable("dev", config_dir=dirs["dev"]) is True
    assert ACC.rotate_active("school", "exhausted", profile=prof, now=NOW + HOLD + 3500) is None  # still marked
    assert ACC.all_unavailable(prof.accounts, now=NOW + HOLD + 3500, reconcile=True) is False
    assert ACC.rotate_active("school", "exhausted", profile=prof, now=NOW + HOLD + 3700) == {
        "from": "school", "to": "dev", "reason": "exhausted"}

    # the project pin is honoured when it is still available
    iso.state.unlink()
    pin = C.ProjectConfig(profile="p", account="school")
    assert ACC.rotate_active("dev", "exhausted", profile=prof, project=pin, now=NOW) == {
        "from": "dev", "to": "school", "reason": "exhausted"}
    assert ACC.rotate_active("school", "exhausted", profile=prof, project=pin, now=NOW) == {
        "from": "school", "to": None, "reason": "exhausted"}

    # a name outside the profile (the runner mocks' "acct"), an empty name, a
    # profile without claude accounts: None, and no file is created
    iso.state.unlink()
    assert ACC.rotate_active("acct", "exhausted", profile=prof, now=NOW) is None
    assert ACC.rotate_active("", "exhausted", profile=prof, now=NOW) is None
    assert ACC.rotate_active("school", "exhausted", profile=None, now=NOW) is None
    assert ACC.rotate_active("cx", "exhausted", profile=C.Profile(name="q", accounts=[
        C.Account(name="cx", agent="codex", command="x")]), now=NOW) is None
    assert not iso.state.exists()


# ---- AC5 / AC7(e): usage guard — per-account snapshots, 401 → auth, PAUSED hold ----

def _iso_in(seconds: float) -> str:
    return datetime.fromtimestamp(NOW + seconds, tz=timezone.utc).isoformat()


def _usage(session_util=10.0, session_reset_in=3600.0,
           weekly_util=10.0, weekly_reset_in=3 * 86400.0) -> dict:
    """The usage payload shape test_usage_guard.py uses (copied, not imported)."""
    return {
        "five_hour": {"utilization": session_util, "resets_at": _iso_in(session_reset_in)},
        "seven_day": {"utilization": weekly_util, "resets_at": _iso_in(weekly_reset_in)},
    }


def _ug_state(iso) -> dict:
    return json.loads(iso.ug.read_text(encoding="utf-8"))


def _ug_profile(iso, monkeypatch, active="school"):
    """Profile ``p`` with school/dev and the cwd inside a project pinned to it —
    the usage guard's anchor is the cwd. Returns ``{name: dir}``."""
    dirs = _write_profile(iso, "p", [("school", 20), ("dev", 10)], active=active)
    monkeypatch.chdir(_project(iso.tmp / "proj", profile="p"))
    return dirs


def _http_error(code: int, headers=None):
    def _raise(*_a, **_k):
        raise urllib.error.HTTPError("https://example.invalid/usage", code, "err", headers or {}, None)
    return _raise


def test_check_pauses_when_all_accounts_unavailable(iso, monkeypatch):
    from todo_system import usage_guard as ug

    dirs = _ug_profile(iso, monkeypatch)
    calls: list[int] = []
    monkeypatch.setattr(ug, "_fetch_usage", lambda: calls.append(1) or _usage())

    # nothing marked: the ordinary decision, plus the active account's mirror
    d = ug.check(now=NOW)
    assert d["allowed"] is True and "paused" not in d
    st = _ug_state(iso)
    assert st["account"] == "school"
    assert st["accounts"]["school"]["usage"] == st["usage"]
    assert st["accounts"]["school"]["decision"]["allowed"] is True
    assert st["accounts"]["school"]["fetched_at"] == NOW
    assert set(st) >= {"fetched_at", "usage", "decision", "fail_count", "next_retry_at", "error"}
    assert "dev" not in st["accounts"]                 # check() never fetches an inactive account
    assert calls == [1]

    # every account unavailable → the PAUSED hold, through the same check()
    ACC.mark_unavailable("school", "exhausted", NOW + 5 * 3600, now=NOW, config_dir=dirs["school"])
    ACC.mark_unavailable("dev", "auth", now=NOW, config_dir=dirs["dev"])
    d = ug.check(now=NOW + 1)
    assert d["allowed"] is False and d["paused"] is True
    assert d["reason"].startswith("all accounts unavailable:")
    assert "school(exhausted)" in d["reason"] and "dev(auth)" in d["reason"]
    assert d["blocked_until"] == NOW + 5 * 3600            # the earliest timed mark
    assert d["accounts_unavailable"] == {"school": "exhausted", "dev": "auth"}
    assert d["underlying"]["allowed"] is True
    assert d["windows"] and d["checked_at"] == NOW + 1 and d["stale"] is False
    assert _state(iso)["paused_since"] == ACC.iso_from_epoch(NOW + 1)
    assert _ug_state(iso)["decision"]["paused"] is True
    assert _ug_state(iso)["account"] == "school"          # the unfiltered fallback while paused
    assert calls == [1]                                    # cached snapshot, no fetch
    # a no-data tick carries the usage block underneath, never the pause itself
    monkeypatch.setattr(ug, "_fetch_usage", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    d = ug.check(now=NOW + 4 * 3600)                       # snapshot older than stale_ok
    assert d["paused"] is True
    assert "fail-open" in d["underlying"]["reason"] and not d["underlying"].get("carried")
    # school's window passes → the hold lifts by itself, paused_since cleared, and
    # the decision underneath is the plain fail-open (nothing carried)
    d = ug.check(now=NOW + 5 * 3600 + 1)
    assert d["allowed"] is True and "paused" not in d and not d.get("carried")
    assert _state(iso)["paused_since"] is None
    # sticky marks only: paused with no deadline; `account use` (clear) lifts it at once
    ACC.mark_unavailable("school", "no_subscription", now=NOW, config_dir=dirs["school"])
    d = ug.check(now=NOW + 5 * 3600 + 2)
    assert d["paused"] is True and d["blocked_until"] is None
    assert ACC.clear_unavailable("school", config_dir=dirs["school"]) is True
    d = ug.check(now=NOW + 5 * 3600 + 3)
    assert d["allowed"] is True and "paused" not in d
    # a blown-up account layer never breaks the decision (fail-open to the usage verdict)
    with monkeypatch.context() as mp:
        mp.setattr(ug, "_account_context", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert ug.check(now=NOW + 5 * 3600 + 4)["allowed"] is True

    # no profile at all → the pre-existing shape: no `account(s)` keys, never paused
    monkeypatch.setattr(C, "PROFILES", iso.tmp / "none")
    monkeypatch.setattr(ug, "_fetch_usage", lambda: _usage())
    iso.ug.unlink()
    d = ug.check(now=NOW + 200)
    assert d["allowed"] is True and "paused" not in d
    st = _ug_state(iso)
    assert "accounts" not in st and "account" not in st


def test_active_401_marks_auth(iso, monkeypatch):
    from todo_system import usage_guard as ug

    dirs = _ug_profile(iso, monkeypatch)
    monkeypatch.setattr(ug, "_fetch_usage", _http_error(401))
    d = ug.check(now=NOW)
    assert d["allowed"] is True and "paused" not in d      # fail-open, as before; dev is still there
    e = _state(iso)["unavailable"]["school"]
    assert e["reason"] == "auth" and e["until"] is None and e["config_dir"] == str(dirs["school"])
    assert "401" in _ug_state(iso)["error"]
    # the guard now reads dev (school is skipped by resolve_active)
    assert ug._credentials_file() == (dirs["dev"] / ".credentials.json", False)
    # a 401 as dev too → both marked → PAUSED
    d = ug.check(force=True, now=NOW + 1)
    assert _state(iso)["unavailable"]["dev"]["reason"] == "auth"
    assert d["paused"] is True and d["blocked_until"] is None
    # already marked: the original stamp is kept (no rewrite)
    before = iso.state.read_bytes()
    ug.check(force=True, now=NOW + 2)
    assert iso.state.read_bytes() == before

    # a 429 / an offline endpoint marks nothing
    iso.state.unlink()
    monkeypatch.setattr(ug, "_fetch_usage", _http_error(429, {"Retry-After": "10"}))
    ug.check(force=True, now=NOW + 3)
    monkeypatch.setattr(ug, "_fetch_usage", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    ug.check(force=True, now=NOW + 4)
    assert not iso.state.exists()

    # an access token that had merely expired (expiresAt past) is a stale
    # snapshot, not a lost login: no mark
    creds = dirs["school"] / ".credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": TOKEN_SCHOOL, "expiresAt": int((time.time() - 3600) * 1000)}}), encoding="utf-8")
    monkeypatch.setattr(ug, "_fetch_usage", _http_error(401))
    ug.check(force=True, now=NOW + 5)
    assert not iso.state.exists()
    # … a token still valid on paper that the endpoint rejects IS a lost login
    creds.write_text(json.dumps({"claudeAiOauth": {
        "accessToken": TOKEN_SCHOOL, "expiresAt": int((time.time() + 3600) * 1000)}}), encoding="utf-8")
    ug.check(force=True, now=NOW + 6)
    assert _state(iso)["unavailable"]["school"]["reason"] == "auth"
    assert _state(iso)["unavailable"]["school"]["cred_mtime"] == creds.stat().st_mtime

    # no profile → a 401 marks nothing and creates nothing
    iso.state.unlink()
    monkeypatch.setattr(C, "PROFILES", iso.tmp / "none")
    ug.check(force=True, now=NOW + 7)
    assert not iso.state.exists()


def test_refresh_inactive_accounts_ttl_lock_backoff(iso, monkeypatch):
    fcntl = pytest.importorskip("fcntl")
    from todo_system import usage_guard as ug

    dirs = _ug_profile(iso, monkeypatch)
    prof = C.load_profile("p")
    school, dev = prof.accounts
    (dirs["dev"] / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": TOKEN_DEV}}), encoding="utf-8")

    # the per-account token reader: that account's file, no Keychain for an account dir
    def keychain_must_not_run(*a, **k):
        raise AssertionError("Keychain consulted for an account dir")

    monkeypatch.setattr(ug.subprocess, "run", keychain_must_not_run)
    assert ug._read_token_for(dev) == TOKEN_DEV
    assert ug._read_token_for(school) is None
    assert ug._read_token() is None                         # the active one (school) has no file
    with pytest.raises(RuntimeError):
        ug._request_usage(None)

    active_calls: list[int] = []
    monkeypatch.setattr(ug, "_fetch_usage", lambda: active_calls.append(1) or _usage())
    seen: list[str] = []
    monkeypatch.setattr(ug, "_fetch_usage_for",
                        lambda acct: seen.append(acct.name) or _usage(weekly_util=42.0))
    ttl = ug._cfg()["fetch_ttl_s"]

    out = ug.refresh_inactive_accounts(now=NOW)
    assert seen == ["dev"] and active_calls == []           # the active account is never its target
    assert out["dev"]["usage"]["seven_day"]["utilization"] == 42.0
    assert out["dev"]["decision"]["allowed"] is True and out["dev"]["fetched_at"] == NOW
    assert out["dev"]["fail_count"] == 0 and out["dev"]["error"] is None
    st = _ug_state(iso)
    assert st["accounts"]["dev"]["usage"]["seven_day"]["utilization"] == 42.0
    assert "usage" not in st                                 # the top level stays the active account's
    assert (iso.ug.parent / "fetch.dev.lock").exists()
    # TTL × INACTIVE_TTL_FACTOR: fresh → no fetch; past → one fetch
    assert ug.INACTIVE_TTL_FACTOR == 4
    ug.refresh_inactive_accounts(now=NOW + ttl * 4 - 1)
    assert seen == ["dev"]
    ug.refresh_inactive_accounts(now=NOW + ttl * 4 + 1)
    assert seen == ["dev", "dev"]
    # check() mirrors the active account next to it and leaves dev's entry alone
    ug.check(now=NOW + ttl * 4 + 2)
    st = _ug_state(iso)
    assert st["account"] == "school" and st["accounts"]["school"]["usage"] == st["usage"]
    assert st["accounts"]["dev"]["fetched_at"] == NOW + ttl * 4 + 1
    assert active_calls == [1]

    # a failure backs off per account: Retry-After honoured, fail_count counted,
    # the last good snapshot kept
    def _429(acct):
        seen.append(acct.name)
        raise urllib.error.HTTPError("https://example.invalid/usage", 429, "Too Many Requests",
                                     {"Retry-After": "150"}, None)

    monkeypatch.setattr(ug, "_fetch_usage_for", _429)
    t = NOW + ttl * 8 + 10
    out = ug.refresh_inactive_accounts(now=t)
    assert out["dev"]["fail_count"] == 1 and out["dev"]["next_retry_at"] == pytest.approx(t + 150)
    assert out["dev"]["usage"]["seven_day"]["utilization"] == 42.0
    assert "429" in out["dev"]["error"]
    n = len(seen)
    ug.refresh_inactive_accounts(now=t + 100)               # inside the backoff window
    assert len(seen) == n
    ug.refresh_inactive_accounts(now=t + 151)
    assert len(seen) == n + 1
    st = _ug_state(iso)
    assert st["accounts"]["dev"]["fail_count"] == 2
    assert st["fail_count"] == 0 and st["accounts"]["school"]["fail_count"] == 0   # untouched

    # the per-account lock: a busy fetch.dev.lock serves the cache, no fetch
    holder = os.open(str(iso.ug.parent / "fetch.dev.lock"), os.O_CREAT | os.O_RDWR)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        n = len(seen)
        ug.refresh_inactive_accounts(now=t + 1000)
        assert len(seen) == n
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    # a 401 for an inactive account marks nothing
    monkeypatch.setattr(ug, "_fetch_usage_for", _http_error(401))
    ug.refresh_inactive_accounts(now=t + 2000)
    assert "unavailable" not in ACC.load_runtime_state()
    # the inactive set follows resolve_active with the runtime state: an
    # unavailable dev is still the inactive target while school is active, and
    # once school is marked instead, dev is the active one and school the target
    ACC.mark_unavailable("dev", "auth", now=NOW, config_dir=dirs["dev"])
    monkeypatch.setattr(ug, "_fetch_usage_for", lambda acct: seen.append(acct.name) or _usage())
    ug.refresh_inactive_accounts(now=t + 3000)
    assert seen[-1] == "dev"
    ACC.clear_unavailable("dev", config_dir=dirs["dev"])
    ACC.mark_unavailable("school", "auth", now=NOW, config_dir=dirs["school"])
    ug.refresh_inactive_accounts(now=t + 4000)
    assert seen[-1] == "school"
    # no profile → {} and no fetch; a broken resolver → {}
    n = len(seen)
    monkeypatch.setattr(C, "PROFILES", iso.tmp / "none")
    assert ug.refresh_inactive_accounts(now=t) == {} and len(seen) == n


def test_exhausted_until_from_snapshot(iso):
    from todo_system import usage_guard as ug

    cfg = ug._cfg()
    iso.ug.parent.mkdir(parents=True, exist_ok=True)

    def write(**kw):
        iso.ug.write_text(json.dumps(kw), encoding="utf-8")

    # per-account entry: the session window at its cap → its reset
    write(accounts={"school": {"usage": _usage(session_util=cfg["session_hard_cap"])}})
    assert ug.exhausted_until("school", NOW) == pytest.approx(NOW + 3600, abs=2)
    # both over → the later reset (the weekly)
    write(accounts={"school": {"usage": _usage(session_util=cfg["session_hard_cap"] + 5,
                                               weekly_util=cfg["weekly_hard_cap"] + 5)}})
    assert ug.exhausted_until("school", NOW) == pytest.approx(NOW + 3 * 86400, abs=2)
    # under both caps → None (the caller holds the default 5h); unknown name → None
    write(accounts={"school": {"usage": _usage(session_util=cfg["session_hard_cap"] - 1,
                                               weekly_util=cfg["weekly_hard_cap"] - 1)}})
    assert ug.exhausted_until("school", NOW) is None
    assert ug.exhausted_until("dev", NOW) is None
    # the top-level usage counts only for the recorded active account
    write(account="school", usage=_usage(weekly_util=cfg["weekly_hard_cap"] + 1))
    assert ug.exhausted_until("school", NOW) == pytest.approx(NOW + 3 * 86400, abs=2)
    assert ug.exhausted_until("dev", NOW) is None
    # a reset already behind `now` is ignored; garbage never raises
    assert ug.exhausted_until("school", NOW + 4 * 86400) is None
    write(accounts={"school": {"usage": {"five_hour": {"utilization": "x", "resets_at": 5}}}})
    assert ug.exhausted_until("school", NOW) is None
    write(accounts="junk", account="school", usage="junk")
    assert ug.exhausted_until("school", NOW) is None
    iso.ug.write_text("{", encoding="utf-8")
    assert ug.exhausted_until("school", NOW) is None
    iso.ug.unlink()
    assert ug.exhausted_until("school", NOW) is None


# ---- AC1: check(account=…) — per-account usage, independent backoff -------------------

def test_check_account_fetches_named_account_and_persists(iso, monkeypatch):
    from todo_system import usage_guard as ug

    _ug_profile(iso, monkeypatch)                       # school active, dev inactive
    monkeypatch.setattr(ug, "_fetch_usage",
                        lambda: (_ for _ in ()).throw(AssertionError("active fetch on account=dev")))
    seen: list[str] = []
    monkeypatch.setattr(ug, "_fetch_usage_for",
                        lambda acct: seen.append(acct.name) or _usage(weekly_util=42.0))

    d = ug.check(account="dev", now=NOW)
    assert seen == ["dev"] and d["allowed"] is True       # dev's own token, active untouched
    st = _ug_state(iso)
    assert st["accounts"]["dev"]["usage"]["seven_day"]["utilization"] == 42.0
    assert st["accounts"]["dev"]["decision"]["allowed"] is True
    assert st["accounts"]["dev"]["fetched_at"] == NOW
    assert "usage" not in st and "dev" != st.get("account")   # top-level stays the active account's


def test_check_account_backoff_is_independent_per_account(iso, monkeypatch):
    from todo_system import usage_guard as ug

    _ug_profile(iso, monkeypatch)
    monkeypatch.setattr(ug, "_fetch_usage", lambda: _usage(weekly_util=11.0))
    monkeypatch.setattr(ug, "_fetch_usage_for", lambda acct: _usage(weekly_util=22.0))
    ug.check(now=NOW)                                     # school mirror
    ug.check(account="dev", now=NOW)                      # dev snapshot, good
    assert _ug_state(iso)["accounts"]["dev"]["fail_count"] == 0

    # a 429 for dev lands only on dev's entry — school / top-level are untouched
    monkeypatch.setattr(ug, "_fetch_usage_for",
                        _http_error(429, {"Retry-After": "150"}))
    d = ug.check(account="dev", force=True, now=NOW + 1)
    assert d["allowed"] is True                           # last good snapshot still evaluated
    st = _ug_state(iso)
    assert st["accounts"]["dev"]["fail_count"] == 1
    assert st["accounts"]["dev"]["next_retry_at"] == pytest.approx(NOW + 1 + 150)
    assert "429" in st["accounts"]["dev"]["error"]
    assert st["accounts"]["dev"]["usage"]["seven_day"]["utilization"] == 22.0   # kept
    assert st["accounts"]["school"]["fail_count"] == 0 and st["fail_count"] == 0


def test_check_account_active_delegates_byte_identical(iso, monkeypatch):
    from todo_system import usage_guard as ug

    _ug_profile(iso, monkeypatch)
    monkeypatch.setattr(ug, "_fetch_usage", lambda: _usage(weekly_util=13.0))
    assert ug.check(account="school", now=NOW + 5) == ug.check(now=NOW + 5)


def test_check_account_unknown_or_no_profile_is_fail_open(iso, monkeypatch):
    from todo_system import usage_guard as ug

    _ug_profile(iso, monkeypatch)
    monkeypatch.setattr(ug, "_fetch_usage_for",
                        lambda acct: (_ for _ in ()).throw(AssertionError("fetch for unknown name")))
    d = ug.check(account="ghost", now=NOW)                # not a profile account
    assert d["allowed"] is True and d.get("stale") is True
    # no profile at all → still fail-open, never raises
    monkeypatch.setattr(C, "PROFILES", iso.tmp / "none")
    assert ug.check(account="dev", now=NOW)["allowed"] is True


# ---- AC2: account_slots — slots summed across usable accounts ------------------------

def _seed_dev_decision(iso, monkeypatch, ug, **usage_kw):
    """Give dev a stored ``accounts.dev.decision`` (what account_slots reads for
    an inactive account) by refreshing it once with a known usage payload."""
    monkeypatch.setattr(ug, "_fetch_usage_for", lambda acct: _usage(**usage_kw))
    ug.refresh_inactive_accounts(now=NOW)


def test_account_slots_sums_two_wide_open_accounts(iso, monkeypatch):
    from todo_system import usage_guard as ug

    _ug_profile(iso, monkeypatch)                         # school active, dev inactive
    monkeypatch.setattr(ug, "_fetch_usage", lambda: _usage())     # school wide open
    _seed_dev_decision(iso, monkeypatch, ug)                      # dev wide open
    cap = ug.account_slots(3, now=NOW)
    assert cap["per_account"] == {"school": 3, "dev": 3}
    assert cap["total"] == 6 and cap["active"] == "school"


def test_account_slots_excludes_a_blocked_account(iso, monkeypatch):
    from todo_system import usage_guard as ug

    _ug_profile(iso, monkeypatch)
    monkeypatch.setattr(ug, "_fetch_usage", lambda: _usage())     # school wide open → 3
    _seed_dev_decision(iso, monkeypatch, ug,
                       weekly_util=ug._cfg()["weekly_hard_cap"] + 1.0)   # dev blocked → 0
    cap = ug.account_slots(3, now=NOW)
    assert cap["per_account"]["dev"] == 0 and cap["per_account"]["school"] == 3
    assert cap["total"] == 3 and cap["order"] == ["school"]           # only slots>0 ranked


def test_account_slots_single_account_matches_parallel_slots(iso, monkeypatch):
    from todo_system import usage_guard as ug

    _write_profile(iso, "p", [("school", 20)], active="school")   # one claude account
    monkeypatch.chdir(_project(iso.tmp / "solo", profile="p"))
    monkeypatch.setattr(ug, "_fetch_usage", lambda: _usage())
    # byte-identical to the former single-account sizing, cap None → no global cut
    assert ug.account_slots(3, None, now=NOW)["total"] == ug.parallel_slots(ug.check(now=NOW), 3)


def test_account_slots_global_cap_cuts_the_sum(iso, monkeypatch):
    from todo_system import usage_guard as ug

    _ug_profile(iso, monkeypatch)
    monkeypatch.setattr(ug, "_fetch_usage", lambda: _usage())
    _seed_dev_decision(iso, monkeypatch, ug)
    assert ug.account_slots(3, now=NOW)["total"] == 6                 # uncapped
    assert ug.account_slots(3, global_cap=4, now=NOW)["total"] == 4   # cut


def test_account_slots_order_is_widest_slack_then_priority(iso, monkeypatch):
    from todo_system import usage_guard as ug

    _ug_profile(iso, monkeypatch)
    monkeypatch.setattr(ug, "_fetch_usage", lambda: _usage(weekly_util=62.0))  # school slack ~7pp
    _seed_dev_decision(iso, monkeypatch, ug)                                   # dev slack ~59pp
    assert ug.account_slots(3, now=NOW)["order"] == ["dev", "school"]          # wider slack first


def test_account_slots_drops_an_unavailable_account(iso, monkeypatch):
    from todo_system import usage_guard as ug

    dirs = _ug_profile(iso, monkeypatch)
    monkeypatch.setattr(ug, "_fetch_usage", lambda: _usage())
    _seed_dev_decision(iso, monkeypatch, ug)
    ACC.mark_unavailable("dev", "auth", now=NOW, config_dir=dirs["dev"])       # sticky mark
    cap = ug.account_slots(3, now=NOW)
    assert "dev" not in cap["per_account"] and cap["total"] == 3


def test_account_slots_no_profile_is_empty(iso, monkeypatch):
    from todo_system import usage_guard as ug

    monkeypatch.setattr(C, "PROFILES", iso.tmp / "none")
    cap = ug.account_slots(3, now=NOW)
    assert cap == {"total": 0, "per_account": {}, "slack": {}, "order": [], "active": None}


# ---- AC3 / AC7(a)(b)(c)(d)(g): runner — rotate on the capacity deferral ---------------

DEFERRAL_KEYS = {"branch", "run_dir", "stages", "capacity_limited", "checkpoint_sha", "repo"}
DEFERRED = "stage implement: usage/rate limit — deferred (no penalty)"


def _run_stage(iso, *, stdout: str, rc: int, stderr: str = "", account_env=None,
               usage_rec=None):
    """One external-repo pass through ``StagedExecutor.execute`` with every
    machine read stubbed — the patch list of
    ``test_runner.ImplementCheckpointTest._run`` (copied) — EXCEPT
    ``_account_env``, which stays real unless ``account_env`` is given: the
    profile under tmp decides the account, as live. The fake stage subprocess
    returns ``(rc, stdout, stderr)``; ``usage_rec`` stubs
    ``stages.record_stage_usage`` (None = the real reader, which records
    nothing for a stub argv). Its own RUNS_DIR per call (run ids have
    one-second resolution). Returns ``(ExecOutcome, progress lines)``."""
    from todo_system.orchestrator import message as m
    from todo_system.orchestrator import repos, stages
    from todo_system.orchestrator import runner as runner_mod
    from todo_system.orchestrator.registry import Agent
    from todo_system.orchestrator.runner import StagedExecutor

    runs = Path(tempfile.mkdtemp(dir=iso.tmp))
    progress_lines: list[str] = []
    vp = subprocess.CompletedProcess([], 0, "", "")
    vp.problem = None
    vp.orphaned_commits = None
    msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="t",
                     payload={"repo": "git@github.com:x/y.git", "auto_merge": False,
                              "model_tier": "standard"})

    def fake_run(argv, **kw):
        return subprocess.CompletedProcess(argv, rc, stdout, stderr)

    checkpoint = mock.Mock(return_value={"pushed": False, "tip": None, "note": ""})
    patches = [
        mock.patch.object(repos, "ensure_worktree", return_value=iso.clone),
        mock.patch.object(stages, "restore_main"),
        mock.patch.object(stages, "install_push_guard"),
        mock.patch.object(stages, "default_branch", return_value="main"),
        mock.patch.object(stages, "prepare_external_branch",
                          return_value=subprocess.CompletedProcess([], 0, "", "")),
        mock.patch.object(stages, "prepare_vault_branch", return_value=vp),
        mock.patch.object(stages, "remote_branch_exists", return_value=True),
        mock.patch.object(stages, "resumable_run", return_value=None),
        mock.patch.object(stages, "inherit_artifacts", return_value=[]),
        mock.patch.object(stages, "record_related", return_value=None),
        mock.patch.object(stages, "RUNS_DIR", runs),
        mock.patch.object(stages, "build_stage_argv", return_value=["true"]),
        mock.patch.object(stages, "checkpoint_push", checkpoint),
        mock.patch.object(stages, "cleanup_build_daemons"),
        mock.patch.object(stages, "remove_worktree"),
        mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
        mock.patch.object(StagedExecutor, "_acquire_checkout_lease", return_value=None),
        mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
        mock.patch.object(StagedExecutor, "_pipeline_for", return_value=("implement",)),
        mock.patch("todo_system.worker.runner_heartbeat_start"),
        mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run),
        mock.patch("subprocess.run", side_effect=fake_run),
    ]
    if account_env is not None:
        patches.append(mock.patch.object(StagedExecutor, "_account_env", return_value=account_env))
    if usage_rec is not None:
        patches.append(mock.patch.object(stages, "record_stage_usage", return_value=usage_rec))
    agent = Agent(id="w", kind="ai-local", capabilities=["git"], repo="~/x")
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        out = StagedExecutor().execute(msg, agent, progress=lambda s: progress_lines.append(s))
    return out, progress_lines


def _runner_profile(iso, accounts=(("school", 20), ("dev", 10)), active="school"):
    dirs = _write_profile(iso, "p", list(accounts), active=active)
    iso.clone = _project(iso.tmp / "clone", profile="p")
    iso.cache.write_text("{}", encoding="utf-8")
    return dirs


def _resolved_account(iso) -> str | None:
    from todo_system.orchestrator.runner import StagedExecutor
    return StagedExecutor._account_env(SimpleNamespace(task_id="t", payload={}), repo=iso.clone)[1]


def test_exhausted_rc1_switches_school_to_dev(iso):
    dirs = _runner_profile(iso)
    # a usage snapshot that says school's session window is over its cap →
    # the mark's `until` is that window's reset (via usage_guard.exhausted_until)
    from todo_system import usage_guard as ug
    reset = time.time() + 1800
    iso.ug.parent.mkdir(parents=True, exist_ok=True)
    iso.ug.write_text(json.dumps({"accounts": {"school": {"usage": {
        "five_hour": {"utilization": ug._cfg()["session_hard_cap"] + 1,
                      "resets_at": datetime.fromtimestamp(reset, tz=timezone.utc).isoformat()},
        "seven_day": {"utilization": 10.0, "resets_at": _iso_in(86400)}}}}}), encoding="utf-8")

    out, progress = _run_stage(iso, stdout="You're out of usage credits · resets 6pm\n", rc=1)
    assert out.ok is False and out.summary == DEFERRED
    assert out.payload["capacity_limited"] is True
    assert out.payload["account_switch"] == {"from": "school", "to": "dev", "reason": "exhausted"}
    assert set(out.payload) == DEFERRAL_KEYS | {"account_switch"}
    st = _state(iso)
    assert st["unavailable"]["school"]["reason"] == "exhausted"
    assert st["unavailable"]["school"]["config_dir"] == str(dirs["school"])
    assert ACC.epoch_from_iso(st["unavailable"]["school"]["until"]) == pytest.approx(reset, abs=2)
    assert st["active_override"] == "dev" and st["active_override_dir"] == str(dirs["dev"])
    assert st["last_switch"]["from"] == "school" and st["last_switch"]["to"] == "dev"
    assert st["paused_since"] is None
    assert not iso.cache.exists()                                   # model cache dropped
    assert "account: school" in progress
    assert "account: school → dev (exhausted)" in progress
    # the next run resolves dev (the override) …
    assert _resolved_account(iso) == "dev"
    # … and dev hitting the wall too — no snapshot for it → the 5h default — pauses
    iso.cache.write_text("{}", encoding="utf-8")
    before = time.time()
    out, progress = _run_stage(
        iso, rc=1,
        stdout="You've reached your Fable 5 limit. Run /usage-credits to continue or switch models with /model.\n")
    assert out.payload["account_switch"] == {"from": "dev", "to": None, "reason": "exhausted"}
    st = _state(iso)
    assert st["active_override"] is None and st["paused_since"]
    assert ACC.epoch_from_iso(st["unavailable"]["dev"]["until"]) == pytest.approx(before + HOLD, abs=120)
    assert "account: dev → none (exhausted)" in progress
    assert not iso.cache.exists()
    # a third run (both marked — the unfiltered fallback runs school): no switch, nothing written
    before_bytes = iso.state.read_bytes()
    out, progress = _run_stage(iso, stdout="You're out of usage credits\n", rc=1)
    assert out.payload["capacity_limited"] is True and "account_switch" not in out.payload
    assert set(out.payload) == DEFERRAL_KEYS
    assert iso.state.read_bytes() == before_bytes
    assert not any(" → " in line for line in progress)


def test_not_logged_in_switches_sticky(iso):
    dirs = _runner_profile(iso)
    out, progress = _run_stage(iso, stdout="Not logged in · Please run /login\n", rc=1)
    assert out.summary == DEFERRED
    assert out.payload["account_switch"] == {"from": "school", "to": "dev", "reason": "auth"}
    e = _state(iso)["unavailable"]["school"]
    assert e["reason"] == "auth" and e["until"] is None
    assert e["config_dir"] == str(dirs["school"]) and e["cred_mtime"] is None
    # the pre-existing model-cache hook ignores auth text; the rotate hook dropped the cache
    assert not iso.cache.exists()
    assert "account: school → dev (auth)" in progress
    school = C.load_profile("p").accounts[0]
    assert ACC.unavailable_entry(school) is not None
    assert _resolved_account(iso) == "dev"
    # a login (the credentials file appearing) releases the mark — the override stays on dev
    (dirs["school"] / ".credentials.json").write_text("{}", encoding="utf-8")
    assert ACC.unavailable_entry(school) is None
    assert _resolved_account(iso) == "dev"
    # stderr is part of the classification: the gate reads stdout, the kind comes
    # from both (an auth error on stderr beats the exhausted wording on stdout).
    # With the state file gone the profile's active_account (school) runs again.
    iso.state.unlink()
    out, _ = _run_stage(iso, stdout="usage limit\n", stderr='API Error: 401 {"type":"authentication_error"}',
                        rc=1)
    assert out.payload["account_switch"] == {"from": "school", "to": "dev", "reason": "auth"}
    assert _state(iso)["unavailable"]["school"]["until"] is None


def test_exit0_quota_switches(iso):
    dirs = _runner_profile(iso)
    quota = ("You've reached your Fable 5 limit. Run /usage-credits to continue or switch "
             "models with /model.\n")
    # rc 0 + a usage record proving zero API work + the marker → the deferral, and the switch
    out, progress = _run_stage(iso, stdout=quota, rc=0,
                               usage_rec={"unavailable": "no usage in transcript"})
    assert out.summary == DEFERRED and out.payload["capacity_limited"] is True
    assert out.payload["account_switch"] == {"from": "school", "to": "dev", "reason": "exhausted"}
    assert _state(iso)["unavailable"]["school"]["config_dir"] == str(dirs["school"])
    assert "account: school → dev (exhausted)" in progress
    # rc 0 with real API work is a green stage, never a deferral, never a switch
    iso.state.unlink()
    out, progress = _run_stage(iso, stdout=quota, rc=0, usage_rec={"input_tokens": 5})
    assert "capacity_limited" not in out.payload and "account_switch" not in out.payload
    assert not iso.state.exists() and not any(" → " in line for line in progress)
    # rc 0 and no usage record at all (kill switch / stub argv): same
    out, _ = _run_stage(iso, stdout=quota, rc=0)
    assert "capacity_limited" not in out.payload and not iso.state.exists()


def test_transient_does_not_switch(iso):
    _runner_profile(iso)
    out, progress = _run_stage(iso, stdout="API Error: 429 rate limit exceeded\n", rc=1)
    assert out.summary == DEFERRED and out.payload["capacity_limited"] is True
    assert "account_switch" not in out.payload and set(out.payload) == DEFERRAL_KEYS
    assert not iso.state.exists()
    assert not any(" → " in line for line in progress)
    assert not iso.cache.exists()          # the pre-existing hook: "rate limit" is a model marker
    assert _resolved_account(iso) == "school"
    iso.cache.write_text("{}", encoding="utf-8")
    out, progress = _run_stage(iso, stdout="overloaded_error: Overloaded\n", rc=1)
    assert out.summary == DEFERRED and set(out.payload) == DEFERRAL_KEYS
    assert not iso.state.exists() and iso.cache.exists()     # overloaded: neither hook fires
    assert not any(" → " in line for line in progress)


def test_no_account_profile_keeps_old_behaviour(iso):
    stdout = "You're out of usage credits · resets 6pm\n"
    iso.clone = _project(iso.tmp / "clone", profile="p")
    iso.cache.write_text("{}", encoding="utf-8")

    def deferred_unchanged(account_env=None):
        out, progress = _run_stage(iso, stdout=stdout, rc=1, account_env=account_env)
        assert out.ok is False and out.summary == DEFERRED
        assert set(out.payload) == DEFERRAL_KEYS and out.payload["capacity_limited"] is True
        assert not iso.state.exists()
        assert not any(" → " in line for line in progress)
        return progress

    # a profile with no account at all
    C.write_profile(C.Profile(name="p"))
    assert _resolved_account(iso) is None
    assert "account: school" not in deferred_unchanged()
    # a codex-only profile
    C.write_profile(C.Profile(name="p", accounts=[C.Account(name="cx", agent="codex", command="x")]))
    deferred_unchanged()
    # a single claude account: its quota is the usage guard's business, as before
    _write_profile(iso, "p", [("school", 20)], active="school")
    assert "account: school" in deferred_unchanged()
    assert _resolved_account(iso) == "school"
    # no profile marker in the repo and no global default profile
    bare = iso.tmp / "bare"
    bare.mkdir()
    iso.clone = bare
    deferred_unchanged()
    # the runner tests' mocks — no account, and a name that is in no profile —
    # even against a two-account profile
    iso.clone = _project(iso.tmp / "clone", profile="p")
    _write_profile(iso, "p", [("school", 20), ("dev", 10)], active="school")
    deferred_unchanged(account_env=(None, ""))
    deferred_unchanged(account_env=({"CLAUDE_CONFIG_DIR": str(iso.tmp / "cfg")}, "acct"))
    # the model cache was dropped by the pre-existing hook in every case (same as before)
    assert not iso.cache.exists()


# ---- AC4 / AC7(f): orchestrator — one alert per switch, one per pause ------------------

def test_collect_notifies_switch_once_and_paused_once(iso):
    from todo_system.orchestrator import assignment as A
    from todo_system.orchestrator import message as m
    from todo_system.orchestrator.assignment import Assignment, Ledger
    from todo_system.orchestrator.mailbox import FileMailbox
    from todo_system.orchestrator.orchestrator import REVIEW_RENOTIFY_COOLDOWN_S, Orchestrator, Task
    from todo_system.orchestrator.registry import Agent, Registry

    mb = FileMailbox(iso.tmp / "mb")
    ledger = Ledger(iso.tmp / "assignments")
    reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
    sent: list[str] = []
    routed: list[tuple] = []

    def rec(text, *, level=None, event_key=None, cooldown_s=0.0):
        routed.append((event_key, cooldown_s))
        return ("send", text)

    # max_attempts=1: no re-dispatch inside collect(), so nothing reads the live Todo
    orch = Orchestrator(mb, reg, ledger, notify=lambda t, link=None: sent.append(t),
                        alert_route=rec, max_attempts=1)

    def fail(a, payload):
        mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.FAIL, frm="w", task_id=a.task_id,
                           corr_id=a.corr_id, payload=payload))
        return orch.collect()

    def fresh(corr):
        """A new dispatched attempt of the same task (a fresh corr_id)."""
        a2 = Assignment(task_id="t", agent_id="w", corr_id=corr, kind="ai-local", state=A.DISPATCHED)
        ledger.save(a2)
        return a2

    deferred = {"reason": "stage implement: usage/rate limit — deferred (no penalty)",
                "capacity_limited": True, "branch": "worker/t", "stages": []}
    switch = {"from": "school", "to": "dev", "reason": "exhausted"}
    paused = {"from": "dev", "to": None, "reason": "auth"}

    a = orch.dispatch(Task("t", {"summary": "x"}))
    assert a is not None
    fail(a, {**deferred, "account_switch": switch})
    assert routed == [("account-switch:school:dev", REVIEW_RENOTIFY_COOLDOWN_S)]
    assert len(sent) == 1 and "school → dev" in sent[0] and "exhausted" in sent[0]
    assert "📋 t" in sent[0]
    row = ledger.load("t")
    assert row.state == A.FAILED and row.capacity_limited is True       # the deferral path, unchanged
    assert not iso.state.exists()                                        # a switch alert touches no state
    # the same switch reported by another attempt: the same key (the real router
    # merges it inside the cooldown window; rotate_active's idempotency means the
    # runner reports one switch per wall in the first place)
    fail(fresh("c2"), {**deferred, "account_switch": switch})
    assert routed[-1] == ("account-switch:school:dev", REVIEW_RENOTIFY_COOLDOWN_S)
    assert len(routed) == 2

    # PAUSED (to=None): once per pause — the claim on the runtime state's paused_since
    iso.state.write_text(json.dumps({"paused_since": "2026-10-16T00:00:00+00:00"}), encoding="utf-8")
    fail(fresh("c3"), {**deferred, "account_switch": paused})
    assert routed[-1] == ("account-paused", REVIEW_RENOTIFY_COOLDOWN_S)
    assert "PAUSED" in sent[-1] and "dev: auth" in sent[-1]
    assert _state(iso)["paused_notified_since"] == "2026-10-16T00:00:00+00:00"
    n_routed, n_sent = len(routed), len(sent)
    fail(fresh("c4"), {**deferred, "account_switch": paused})
    assert (len(routed), len(sent)) == (n_routed, n_sent)                # the same pause: silent
    assert ledger.load("t").capacity_limited is True
    # the pause lifted meanwhile (usage guard reconciled paused_since away): no alert
    iso.state.write_text(json.dumps({"paused_since": None,
                                     "paused_notified_since": "2026-10-16T00:00:00+00:00"}),
                         encoding="utf-8")
    fail(fresh("c5"), {**deferred, "account_switch": paused})
    assert len(routed) == n_routed
    # a NEW pause (a later paused_since): announced again, once
    iso.state.write_text(json.dumps({"paused_since": "2026-10-16T06:00:00+00:00",
                                     "paused_notified_since": "2026-10-16T00:00:00+00:00"}),
                         encoding="utf-8")
    fail(fresh("c6"), {**deferred, "account_switch": paused})
    assert routed[-1][0] == "account-paused" and len(routed) == n_routed + 1
    fail(fresh("c7"), {**deferred, "account_switch": paused})
    assert len(routed) == n_routed + 1

    # the pre-existing shapes stay silent: a capacity FAIL without account_switch,
    # a non-capacity FAIL (even one carrying the key), a RESULT
    n_routed = len(routed)
    fail(fresh("c8"), deferred)
    assert len(routed) == n_routed and ledger.load("t").capacity_limited is True
    fail(fresh("c9"), {"reason": "boom", "account_switch": switch})
    assert len(routed) == n_routed and ledger.load("t").capacity_limited is False
    fresh("c10")
    mb.send(m.Envelope(to=m.ORCHESTRATOR, type=m.RESULT, frm="w", task_id="t", corr_id="c10",
                       payload={"account_switch": switch, "review_required": True}))
    with mock.patch.object(orch, "_notify_review"):
        orch.collect()
    assert len(routed) == n_routed and ledger.load("t").state == A.AWAITING_HUMAN
    # a broken claim guard never loses the PAUSED alert (fail-open to sending)
    iso.state.write_text(json.dumps({"paused_since": "2026-10-16T07:00:00+00:00"}), encoding="utf-8")
    with mock.patch.object(ACC, "claim_paused_alert", side_effect=RuntimeError("boom")):
        fail(fresh("c11"), {**deferred, "account_switch": paused})
    assert routed[-1][0] == "account-paused"
    # a notify failure is swallowed, as everywhere else; a malformed switch is ignored
    orch.notify = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kakao down"))
    fail(fresh("c12"), {**deferred, "account_switch": switch})
    assert ledger.load("t").state == A.FAILED and routed[-1][0] == "account-switch:school:dev"
    n_routed = len(routed)
    fail(fresh("c13"), {**deferred, "account_switch": "junk"})
    assert len(routed) == n_routed and ledger.load("t").capacity_limited is True

    # usage_hold(): the inactive refresh runs after check() and can never hold or break the tick
    from todo_system import usage_guard
    calls: list[str] = []
    with mock.patch.object(usage_guard, "check", return_value={"allowed": True}), \
            mock.patch.object(usage_guard, "refresh_inactive_accounts",
                              side_effect=lambda: calls.append("refresh") or {}):
        assert orch.usage_hold() is None and calls == ["refresh"]
    with mock.patch.object(usage_guard, "check", return_value={"allowed": False, "reason": "all accounts unavailable: x", "paused": True}), \
            mock.patch.object(usage_guard, "refresh_inactive_accounts", side_effect=RuntimeError("boom")):
        assert orch.usage_hold() == "all accounts unavailable: x"


# ---- AC6: dashboard accounts[] / paused / last_switch · AC2: `account use` clears ---------

EMPTY_RUNTIME = {"accounts": [], "paused": False, "last_switch": None}


def _row(name, *, active, available=True, reason=None, session=None, weekly=None, resets=None):
    return {"name": name, "active": active, "available": available, "reason": reason,
            "session_pct": session, "weekly_pct": weekly, "resets_at": resets}


def test_dashboard_account_runtime_fields(iso, monkeypatch):
    import importlib.util

    src = Path(__file__).resolve().parents[2] / "scripts" / "macp_dashboard.py"
    spec = importlib.util.spec_from_file_location("macp_dashboard_switch", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    vault = _project(iso.tmp / "vault", profile="default")
    monkeypatch.setattr(mod, "VAULT", vault)
    base_keys = {"active", "config_dir", "source"}

    # no profile → the base shape plus the empty additions
    assert mod.collect_account_runtime(mod.collect_account()) == {
        "active": None, "config_dir": "", "source": "none", **EMPTY_RUNTIME}
    dirs = _write_profile(iso, "default", [("school", 20), ("dev", 10)], active="school")
    base = mod.collect_account()
    assert set(base) == base_keys and base["active"] == "school"          # the ③ object is untouched
    out = mod.collect_account_runtime(base)
    assert set(out) == base_keys | set(EMPTY_RUNTIME)
    assert {k: out[k] for k in base_keys} == base
    assert out["accounts"] == [_row("school", active=True), _row("dev", active=False)]
    assert out["paused"] is False and out["last_switch"] is None

    # the env-pointed state files (the iso fixture's): a mark + snapshots show up
    prof = C.load_profile("default")
    assert ACC.rotate_active("school", "exhausted", until=NOW + 100, profile=prof, now=NOW) == {
        "from": "school", "to": "dev", "reason": "exhausted"}
    iso.ug.parent.mkdir(parents=True, exist_ok=True)
    iso.ug.write_text(json.dumps({
        "account": "school", "usage": _usage(session_util=42.0, weekly_util=63.0),
        "accounts": {"dev": {"usage": _usage(session_util=5.0, weekly_util=7.5)}}}), encoding="utf-8")
    base = mod.collect_account()
    assert base == {"active": "dev", "config_dir": str(dirs["dev"]), "source": "override"}
    out = mod.collect_account_runtime(base)
    assert out["accounts"] == [
        _row("school", active=False, available=False, reason="exhausted",
             session=42.0, weekly=63.0, resets=NOW + 100),
        _row("dev", active=True, session=5.0, weekly=7.5)]
    assert out["paused"] is False
    assert out["last_switch"] == {"from": "school", "to": "dev", "reason": "exhausted",
                                  "ts": ACC.iso_from_epoch(NOW)}
    # every account unavailable → paused (the base drops the availability filter and
    # the override, still recorded, keeps winning — a stable env while paused)
    ACC.mark_unavailable("dev", "auth", now=NOW, config_dir=dirs["dev"])
    base = mod.collect_account()
    assert base["active"] == "dev" and base["source"] == "override"
    out = mod.collect_account_runtime(base)
    assert out["paused"] is True
    assert [(r["name"], r["available"], r["reason"], r["resets_at"]) for r in out["accounts"]] == [
        ("school", False, "exhausted", NOW + 100), ("dev", False, "auth", None)]
    # a per-account snapshot for the active name beats the top-level one; garbage reads as none
    iso.ug.write_text(json.dumps({"account": "school", "usage": _usage(session_util=42.0),
                                  "accounts": {"school": {"usage": {"five_hour": {"utilization": "x"}}}}}),
                      encoding="utf-8")
    out = mod.collect_account_runtime(mod.collect_account())
    assert out["accounts"][0]["session_pct"] is None and out["accounts"][0]["weekly_pct"] is None
    iso.ug.write_text("{", encoding="utf-8")
    assert mod.collect_account_runtime(mod.collect_account())["accounts"][0]["session_pct"] is None

    # without the env overrides the files are read under VAULT (at call time)
    monkeypatch.delenv("MACP_CLAUDE_ACCOUNTS_STATE")
    monkeypatch.delenv("USAGE_GUARD_STATE_DIR")
    monkeypatch.setenv("MACP_VAULT_ROOT", str(vault))        # current_account's state path too
    out = mod.collect_account_runtime(mod.collect_account())
    assert out["accounts"] == [_row("school", active=True), _row("dev", active=False)]
    assert out["paused"] is False and out["last_switch"] is None
    state_file = vault / "Worker" / "_state" / "claude_accounts.json"
    ACC.mark_unavailable("school", "exhausted", NOW + 100, now=NOW, config_dir=dirs["school"],
                         path=state_file)
    (vault / "Worker" / "_state" / "usage_guard").mkdir(parents=True)
    (vault / "Worker" / "_state" / "usage_guard" / "latest.json").write_text(
        json.dumps({"accounts": {"school": {"usage": _usage(session_util=42.0, weekly_util=63.0)}}}),
        encoding="utf-8")
    out = mod.collect_account_runtime(mod.collect_account())
    assert out["active"] == "dev" and out["source"] == "priority"
    assert out["accounts"][0] == _row("school", active=False, available=False, reason="exhausted",
                                      session=42.0, weekly=63.0, resets=NOW + 100)
    # the anchor is VAULT, not the cwd
    monkeypatch.chdir(_project(iso.tmp / "elsewhere", profile="other"))
    assert mod.collect_account_runtime(mod.collect_account())["accounts"][0]["name"] == "school"
    # fail-soft: a broken resolver → base fail-soft shape + empty additions, never raises
    monkeypatch.setattr(C, "resolve", _boom)
    assert mod.collect_account_runtime(mod.collect_account()) == {
        "active": None, "config_dir": "", "source": "none", **EMPTY_RUNTIME}
    assert mod.collect_account_runtime({"active": "x", "config_dir": "", "source": "pin"}) == {
        "active": "x", "config_dir": "", "source": "pin", **EMPTY_RUNTIME}

    # wired into /api/status through BOTH names; the web shell renders PAUSED
    assert "collect_account" in mod.collect_status.__code__.co_names
    assert "collect_account_runtime" in mod.collect_status.__code__.co_names
    text = src.read_text(encoding="utf-8")
    assert 'id="state-acct"' in text and "a.paused" in text and "PAUSED" in text


def test_dashboard_account_slots_field(iso, monkeypatch):
    import importlib.util

    src = Path(__file__).resolve().parents[2] / "scripts" / "macp_dashboard.py"
    spec = importlib.util.spec_from_file_location("macp_dashboard_slots", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    vault = iso.tmp / "vault"
    monkeypatch.setattr(mod, "VAULT", vault)
    monkeypatch.delenv("RUNNER_SLOTS_STATE", raising=False)

    # no runner snapshot → the empty shape, never raises
    assert mod.collect_account_slots() == {"total_used": 0, "total_max": None, "per_account": []}

    # a runner snapshot under VAULT → parsed used/max per account (server computes, web renders)
    sp = vault / "Worker" / "_state" / "runner_slots.json"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"updated": NOW, "total_used": 1, "total_max": 5,
                              "per_account": {"school": {"used": 1, "max": 3},
                                              "dev": {"used": 0, "max": 2}}}), encoding="utf-8")
    out = mod.collect_account_slots()
    assert out["total_used"] == 1 and out["total_max"] == 5
    assert out["per_account"] == [{"name": "school", "used": 1, "max": 3},
                                  {"name": "dev", "used": 0, "max": 2}]
    # it is attached to the account object (not folded into collect_account_runtime)
    assert "collect_account_slots" in mod.collect_status.__code__.co_names
    assert mod.collect_account_runtime(mod.collect_account()).get("slots") is None
    # garbage snapshot never raises → the empty shape
    sp.write_text("{", encoding="utf-8")
    assert mod.collect_account_slots()["per_account"] == []
    # web shell renders the server-provided slot fields
    text = src.read_text(encoding="utf-8")
    assert "a.slots" in text and "슬롯" in text


def _boom(start=None):
    raise RuntimeError("boom")


def test_account_use_clears_runtime_mark(iso, capsys):
    from todo_system.macp import __main__ as M

    school = ACC.create_account_layout("school")
    dev = ACC.create_account_layout("dev")
    C.write_profile(C.Profile(name="p", active_account="dev", accounts=[
        C.Account(name="school", claude_config_dir=str(school), priority=20),
        C.Account(name="dev", claude_config_dir=str(dev), priority=10)]))
    iso.cache.write_text("{}", encoding="utf-8")
    proj = _project(iso.tmp / "proj", profile="p")

    def use(name):
        rc = M.main(["account", "use", name, "--profile", "p"])
        return rc, capsys.readouterr().out

    # the auto-switch left school marked (auth, sticky), the override on dev and a pause claimed
    ACC.mark_unavailable("school", "auth", now=NOW, config_dir=school)
    iso.state.write_text(json.dumps({**_state(iso), "active_override": "dev", "active_override_dir": str(dev),
                                     "paused_since": "t0", "paused_notified_since": "t0"}),
                         encoding="utf-8")
    assert C.current_account(start=proj)[0].name == "dev"
    rc, out = use("school")
    assert rc == 0
    assert "runtime state: cleared unavailable mark / override for 'school'" in out
    assert "model cache: invalidated" in out and not iso.cache.exists()
    assert C.load_profile("p").active_account == "school"
    st = _state(iso)
    assert st["unavailable"] == {} and st["active_override"] is None and st["active_override_dir"] is None
    assert st["paused_since"] is None and st["paused_notified_since"] is None
    assert C.current_account(start=proj) == (C.load_profile("p").accounts[0], "profile")
    # nothing left to clear: no line, no rewrite
    before = iso.state.read_bytes()
    rc, out = use("school")
    assert rc == 0 and "runtime state:" not in out and iso.state.read_bytes() == before
    # a mark stamped for a different dir of the same name is left alone (no line)
    ACC.mark_unavailable("dev", "auth", now=NOW, config_dir=iso.tmp / "elsewhere")
    rc, out = use("dev")
    assert rc == 0 and "runtime state:" not in out and "dev" in _state(iso)["unavailable"]
    # a broken state layer is reported, never fatal
    with mock.patch.object(ACC, "clear_unavailable", side_effect=RuntimeError("boom")):
        rc, out = use("dev")
    assert rc == 0 and "runtime state: skipped (RuntimeError)" in out
    # the error paths (rc 2) touch neither the profile nor the state
    ACC.mark_unavailable("school", "auth", now=NOW, config_dir=school)
    before = iso.state.read_bytes()
    assert M.main(["account", "use", "nope", "--profile", "p"]) == 2
    assert M.main(["account", "use", "school", "--profile", "missing"]) == 2
    capsys.readouterr()
    assert iso.state.read_bytes() == before and C.load_profile("p").active_account == "dev"
    # no state file at all: the plain ③ output, nothing created
    iso.state.unlink()
    rc, out = use("school")
    assert rc == 0 and "runtime state:" not in out and not iso.state.exists()
