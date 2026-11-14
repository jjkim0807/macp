"""Regression tests for leaf 3 of ``macp-multi-claude-account`` — one source of
truth for the *current* Claude account and every consumer wired to it:
``accounts.resolve_active`` / ``claude_env_for`` / the ``*_for`` path helpers,
``config.current_account`` and the ``active_*`` wrappers, ``claude_auth``
(``token_file()`` + the account layer of ``claude_env()``), the runner's
``_account_env``, ``stages.transcript_path``'s default root, the usage guard's
credential file, the model probe env, ``macp account use`` and the dashboard's
``account`` field.

Everything runs under ``tmp_path``. The ``roots`` fixture (copied from
``test_claude_accounts_layout.py``, not imported) redirects HOME, the two
layout-root env vars, CLAUDE_CONFIG_DIR and the config module's import-time
MACP_HOME paths, and asserts the redirection before a test body runs. That
matters doubly here: ``account use`` re-points the legacy symlinks, and the
consumers resolve the *host* profile when they are not isolated. No ``claude``
binary is run, no network is touched; token values are fakes shaped like
``test_claude_auth.DUMMY_TOKEN``.

No literal from the identifier gate's PATTERN appears in this file: it sits
inside the scanned engine tree.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from todo_system.macp import accounts as ACC
from todo_system.macp import config as C

TOKEN_SCHOOL = "sk-ant-oat01-" + "s" * 40
TOKEN_DEV = "sk-ant-oat01-" + "d" * 40
TOKEN_BASE = "sk-ant-oat01-" + "b" * 40        # what the process env carried before


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


# ---- AC1: resolve_active / active_account ------------------------------------

def test_resolve_active_order():
    school = _acct("school", priority=20)
    dev = _acct("dev", priority=10)
    cx = _acct("cx", agent="codex", priority=1)
    prof = SimpleNamespace(accounts=[school, dev, cx], active_account="")

    # pin > profile active_account > priority (cx is not a claude account)
    assert ACC.resolve_active(prof, SimpleNamespace(account="school")) == (school, "pin")
    assert ACC.resolve_active(prof) == (dev, "priority")
    prof.active_account = "school"
    assert ACC.resolve_active(prof) == (school, "profile")
    assert ACC.resolve_active(prof, SimpleNamespace(account=None)) == (school, "profile")
    assert ACC.resolve_active(prof, SimpleNamespace(account="dev")) == (dev, "pin")
    # a pin / active_account naming no candidate falls through to the next rule
    prof.active_account = ""
    assert ACC.resolve_active(prof, SimpleNamespace(account="nope")) == (dev, "priority")
    assert ACC.resolve_active(prof, SimpleNamespace(account="cx")) == (dev, "priority")
    prof.active_account = "cx"
    assert ACC.resolve_active(prof) == (dev, "priority")
    prof.active_account = "zzz"
    assert ACC.resolve_active(prof) == (dev, "priority")
    assert ACC.resolve_active(prof, SimpleNamespace(account="school")) == (school, "pin")
    # ties: listing order; a missing priority attribute reads as the default
    first, second = _acct("first"), _acct("second")
    assert ACC.resolve_active(SimpleNamespace(accounts=[first, second])) == (first, "priority")
    bare = SimpleNamespace(name="bare", agent="claude")
    assert ACC.resolve_active(SimpleNamespace(accounts=[bare])) == (bare, "priority")
    # nothing to choose from
    assert ACC.resolve_active(SimpleNamespace(accounts=[cx], active_account="cx")) == (None, "none")
    assert ACC.resolve_active(SimpleNamespace(accounts=[], active_account="")) == (None, "none")
    assert ACC.resolve_active(None) == (None, "none")
    assert ACC.resolve_active(None, SimpleNamespace(account="school")) == (None, "none")

    # the real dataclasses behave the same; the wrapper drops the source
    q = C.Profile(name="q", active_account="school",
                  accounts=[C.Account(name="school", priority=20), C.Account(name="dev", priority=10)])
    assert ACC.active_account(q) is q.accounts[0]
    assert ACC.active_account(q, C.ProjectConfig(profile="q", account="dev")) is q.accounts[1]
    assert ACC.active_account(q, C.ProjectConfig(profile="q")) is q.accounts[0]
    q.active_account = ""
    assert ACC.active_account(q) is q.accounts[1]
    assert ACC.active_account(C.Profile(name="e")) is None
    assert ACC.active_account(None) is None


# ---- AC1: path helpers -------------------------------------------------------

def test_path_helpers(roots):
    legacy = roots.home / ".claude"
    assert ACC.config_dir_for(None) == legacy
    assert ACC.config_dir_for(C.Account(name="x")) == legacy                  # default dir
    assert ACC.config_dir_for(SimpleNamespace(claude_config_dir="")) == legacy
    assert ACC.config_dir_for(SimpleNamespace(claude_config_dir=None)) == legacy
    assert ACC.config_dir_for(SimpleNamespace(claude_config_dir="~/cfg")) == roots.home / "cfg"
    dev = roots.accounts / "dev"
    a_dev = C.Account(name="dev", claude_config_dir=str(dev))
    assert ACC.config_dir_for(a_dev) == dev

    assert ACC.is_legacy_config_dir(ACC.LEGACY_CONFIG_DIR)
    assert ACC.is_legacy_config_dir(legacy) and ACC.is_legacy_config_dir(str(legacy))
    assert not ACC.is_legacy_config_dir(dev)
    assert not ACC.is_legacy_config_dir(roots.home / ".claude-shared")
    # spelled, not resolved: a legacy symlink INTO the account dir keeps the two apart
    dev.mkdir(parents=True)
    legacy.symlink_to(dev, target_is_directory=True)
    assert ACC.is_legacy_config_dir(legacy) and not ACC.is_legacy_config_dir(dev)

    assert ACC.token_file_for(None) == legacy / "automation-token"
    assert ACC.credentials_file_for(None) == legacy / ".credentials.json"
    assert ACC.token_file_for(a_dev) == dev / "automation-token"
    assert ACC.credentials_file_for(a_dev) == dev / ".credentials.json"


# ---- AC1: claude_env_for layers ----------------------------------------------

def test_claude_env_for_layers(roots, monkeypatch):
    school = roots.accounts / "school"
    dev = roots.accounts / "dev"
    for d in (school, dev):
        d.mkdir(parents=True)
    (dev / "automation-token").write_text(TOKEN_DEV + "\n", encoding="utf-8")
    a_school = C.Account(name="school", claude_config_dir=str(school))
    a_dev = C.Account(name="dev", claude_config_dir=str(dev))
    base = {"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN_BASE}
    base_before = dict(base)

    # token file present: config dir + THAT account's token (the inherited one is replaced)
    env = ACC.claude_env_for(a_dev, base)
    assert env["CLAUDE_CONFIG_DIR"] == str(dev)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_DEV
    assert env["PATH"] == "/bin"
    # no token file: the inherited token is REMOVED — never another account's token
    env = ACC.claude_env_for(a_school, base)
    assert env["CLAUDE_CONFIG_DIR"] == str(school)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    # a malformed token file counts as none → removed, no exception
    (school / "automation-token").write_text("pbpaste output went wrong\n", encoding="utf-8")
    env = ACC.claude_env_for(a_school, base)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    # a base without the key is simply left without it
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in ACC.claude_env_for(a_school, {"PATH": "/bin"})
    # base is never mutated
    assert base == base_before

    # legacy-dir account: CLAUDE_CONFIG_DIR stays unset (Keychain rule) — the token layer still applies
    legacy = roots.home / ".claude"
    legacy.mkdir()
    env = ACC.claude_env_for(C.Account(name="legacy"), base)
    assert "CLAUDE_CONFIG_DIR" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    (legacy / "automation-token").write_text(TOKEN_SCHOOL, encoding="utf-8")
    env = ACC.claude_env_for(C.Account(name="legacy"), base)
    assert "CLAUDE_CONFIG_DIR" not in env and env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_SCHOOL
    env = ACC.claude_env_for(None, base)
    assert "CLAUDE_CONFIG_DIR" not in env and env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_SCHOOL

    # ANTHROPIC_API_KEY: the config dir layer applies, the token layer is left exactly as it was
    env = ACC.claude_env_for(a_school, {**base, "ANTHROPIC_API_KEY": "k"})
    assert env["CLAUDE_CONFIG_DIR"] == str(school)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_BASE and env["ANTHROPIC_API_KEY"] == "k"
    env = ACC.claude_env_for(a_dev, {"ANTHROPIC_API_KEY": "k"})
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env                        # not injected either

    # base=None copies os.environ (and leaves it alone)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN_BASE)
    monkeypatch.setenv("SOME_MARKER", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = ACC.claude_env_for(a_dev)
    assert env["SOME_MARKER"] == "1" and env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_DEV
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_BASE
    env = ACC.claude_env_for(a_school)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and env["CLAUDE_CONFIG_DIR"] == str(school)
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_BASE


# ---- config.current_account + the active_* wrappers ---------------------------

def test_current_account_and_active_paths(roots, monkeypatch):
    dirs = _write_profile(roots, "p", [("school", 20), ("dev", 10)], active="school")
    school, dev = dirs["school"], dirs["dev"]
    proj = _project(roots.tmp / "proj", profile="p")
    legacy = roots.home / ".claude"

    acct, src = C.current_account(start=proj)
    assert (acct.name, src) == ("school", "profile")
    assert C.active_config_dir(start=proj) == school
    assert C.active_token_file(start=proj) == school / "automation-token"
    assert C.active_credentials_file(start=proj) == school / ".credentials.json"
    # a nested start resolves the same project; a str is accepted
    sub = proj / "a" / "b"
    sub.mkdir(parents=True)
    assert C.current_account(str(sub))[0].name == "school"
    # the project pin wins
    _project(proj, profile="p", account="dev")
    assert C.current_account(start=proj) == (C.load_profile("p").accounts[1], "pin")
    assert C.active_config_dir(start=proj) == dev
    # a pin naming an unknown account falls through to active_account
    _project(proj, profile="p", account="ghost")
    assert C.current_account(start=proj)[1] == "profile"
    # no active_account → priority
    _project(proj, profile="p")
    prof = C.load_profile("p")
    prof.active_account = ""
    C.write_profile(prof)
    acct, src = C.current_account(start=proj)
    assert (acct.name, src) == ("dev", "priority")
    # default start = cwd
    monkeypatch.chdir(proj)
    assert C.current_account()[0].name == "dev"
    assert C.active_config_dir() == dev

    # no project marker → the global default profile (absent here) → none + legacy paths
    outside = roots.tmp / "outside"
    outside.mkdir()
    assert C.current_account(start=outside) == (None, "none")
    assert C.active_config_dir(start=outside) == legacy
    assert C.active_token_file(start=outside) == legacy / "automation-token"
    assert C.active_credentials_file(start=outside) == legacy / ".credentials.json"
    # a project pointing at a missing profile → none
    _project(roots.tmp / "proj2", profile="missing")
    assert C.current_account(start=roots.tmp / "proj2") == (None, "none")
    # the global default profile is honoured when there is no project marker
    C.set_default_profile("p")
    assert C.current_account(start=outside)[0].name == "dev"
    # resolving creates nothing under the (tmp) home
    assert sorted(p.name for p in roots.home.iterdir()) == []

    # no TOML parser (bare python3 < 3.11): profiles read as absent → none, never an error
    monkeypatch.setattr(C, "tomllib", None)
    assert C.load_profile("p") is None
    assert C.default_profile() == "default"
    assert C.current_account(start=proj) == (None, "none")
    assert C.active_config_dir(start=proj) == legacy


# ---- AC2: claude_auth follows the active account ------------------------------

def _boom(start=None):
    raise RuntimeError("boom")


def test_claude_env_follows_active_account(roots, monkeypatch):
    from todo_system import claude_auth

    # the compat constant is still the legacy single-dir file (fixed at import time)
    assert claude_auth.TOKEN_FILE.name == "automation-token"
    assert claude_auth.TOKEN_FILE.parent.name == ".claude"

    dirs = _write_profile(roots, "default", [("school", 20), ("dev", 10)], active="school")
    school, dev = dirs["school"], dirs["dev"]
    (school / "automation-token").write_text(TOKEN_SCHOOL + "\n", encoding="utf-8")
    proj = _project(roots.tmp / "proj", profile="default")
    monkeypatch.chdir(proj)
    base = {"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN_BASE}

    # active account with a token file: its config dir + its token
    env = claude_auth.claude_env(base=base)
    assert env["CLAUDE_CONFIG_DIR"] == str(school)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_SCHOOL
    assert env["PATH"] == "/bin"
    assert claude_auth.token_file() == school / "automation-token"
    assert claude_auth.read_automation_token() == TOKEN_SCHOOL
    # switch to the account without a token file: the inherited token is removed
    prof = C.load_profile("default")
    prof.active_account = "dev"
    C.write_profile(prof)
    env = claude_auth.claude_env(base=base)
    assert env["CLAUDE_CONFIG_DIR"] == str(dev)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert claude_auth.token_file() == dev / "automation-token"
    assert claude_auth.read_automation_token() is None
    assert base == {"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN_BASE}     # never mutated
    # the API-key path: config dir applied, token layer left exactly as given
    env = claude_auth.claude_env(base={**base, "ANTHROPIC_API_KEY": "k"})
    assert env["CLAUDE_CONFIG_DIR"] == str(dev) and env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_BASE
    # an explicit path is the historical seam: no account resolution at all
    p = roots.tmp / "tok"
    p.write_text(TOKEN_DEV, encoding="utf-8")
    env = claude_auth.claude_env(base=base, path=p)
    assert "CLAUDE_CONFIG_DIR" not in env and env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_DEV
    env = claude_auth.claude_env(base=base, path=roots.tmp / "missing")
    assert "CLAUDE_CONFIG_DIR" not in env and env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_BASE
    assert claude_auth.read_automation_token(path=p) == TOKEN_DEV
    # base=None → a copy of os.environ with the account layer applied
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN_BASE)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = claude_auth.claude_env()
    assert env["CLAUDE_CONFIG_DIR"] == str(dev) and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_BASE
    assert claude_auth.auth_source(env) == "keychain-oauth"
    prof.active_account = "school"
    C.write_profile(prof)
    assert claude_auth.auth_source(claude_auth.claude_env()) == "automation-token"

    # no account (profile gone): the pre-account behaviour, byte for byte
    tok = roots.tmp / "legacy-tok"
    monkeypatch.setattr(claude_auth, "TOKEN_FILE", tok)            # the compat/monkeypatch seam
    monkeypatch.setattr(C, "PROFILES", roots.tmp / "no-profiles")
    assert C.current_account() == (None, "none")
    assert claude_auth.token_file() is claude_auth.TOKEN_FILE
    env = claude_auth.claude_env(base=base)
    assert env == base                                              # token kept: no removal, no CONFIG_DIR
    assert claude_auth.read_automation_token() is None
    tok.write_text(TOKEN_DEV, encoding="utf-8")
    assert claude_auth.claude_env(base=base) == {"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN_DEV}
    assert claude_auth.claude_env(base={}) == {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN_DEV}
    assert claude_auth.read_automation_token() == TOKEN_DEV
    tok.write_text("not a token", encoding="utf-8")
    assert claude_auth.claude_env(base=base) == base
    with pytest.raises(claude_auth.MalformedToken):
        claude_auth.read_automation_token()
    # a broken account resolver never breaks auth
    tok.write_text(TOKEN_DEV, encoding="utf-8")
    monkeypatch.setattr(C, "resolve", _boom)
    assert claude_auth.token_file() is claude_auth.TOKEN_FILE
    assert claude_auth.claude_env(base=base) == {"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN_DEV}


# ---- AC2 / AC6: runner stage env --------------------------------------------------

def test_runner_account_env(roots, monkeypatch):
    from todo_system.orchestrator import stages
    from todo_system.orchestrator.runner import StagedExecutor

    dirs = _write_profile(roots, "p", [("school", 20), ("dev", 10)], active="school")
    school, dev = dirs["school"], dirs["dev"]
    (school / "automation-token").write_text(TOKEN_SCHOOL + "\n", encoding="utf-8")
    repo = _project(roots.tmp / "repo", profile="p")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN_BASE)      # what the env file hands every unit
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    msg = SimpleNamespace(task_id="t1", payload={})

    # active account with a token file: its dir + its token (the inherited one replaced)
    env, name = StagedExecutor._account_env(msg, repo=repo)
    assert name == "school"
    assert env["CLAUDE_CONFIG_DIR"] == str(school)
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_SCHOOL
    assert env is not os.environ and os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_BASE
    # active_account changes → the env follows; no token file → the inherited key is removed
    prof = C.load_profile("p")
    prof.active_account = "dev"
    C.write_profile(prof)
    env, name = StagedExecutor._account_env(msg, repo=repo)
    assert name == "dev" and env["CLAUDE_CONFIG_DIR"] == str(dev)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    # the project pin beats active_account
    prof.active_account = "school"
    C.write_profile(prof)
    _project(repo, profile="p", account="dev")
    env, name = StagedExecutor._account_env(SimpleNamespace(task_id="t2", payload={}), repo=repo)
    assert name == "dev" and env["CLAUDE_CONFIG_DIR"] == str(dev)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    # task_id no longer matters: one active account, no per-task round-robin
    for tid in ("a", "b", "c", "zzz", ""):
        assert StagedExecutor._account_env(SimpleNamespace(task_id=tid, payload={}), repo=repo)[1] == "dev"
    # ANTHROPIC_API_KEY: the config dir is still set, the token layer untouched
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    env, name = StagedExecutor._account_env(msg, repo=repo)
    assert env["CLAUDE_CONFIG_DIR"] == str(dev) and env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_BASE
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    # repo=None anchors at the vault data root
    _project(repo, profile="p")
    monkeypatch.setattr(stages, "_VAULT_DATA_ROOT", repo)
    env, name = StagedExecutor._account_env(msg, repo=None)
    assert name == "school" and env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_SCHOOL
    # an account on the legacy single dir: no CLAUDE_CONFIG_DIR (Keychain rule), token layer applied
    C.write_profile(C.Profile(name="p", accounts=[C.Account(name="legacy")]))
    env, name = StagedExecutor._account_env(msg, repo=repo)
    assert name == "legacy" and "CLAUDE_CONFIG_DIR" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env

    # no claude account → (None, None), exactly as before
    C.write_profile(C.Profile(name="p", accounts=[C.Account(name="cx", agent="codex", command="x")]))
    assert StagedExecutor._account_env(msg, repo=repo) == (None, None)
    C.write_profile(C.Profile(name="p"))
    assert StagedExecutor._account_env(msg, repo=repo) == (None, None)
    # a repo without .macp and no global default profile → (None, None)
    bare = roots.tmp / "bare"
    bare.mkdir()
    assert StagedExecutor._account_env(msg, repo=bare) == (None, None)
    # a repo pointing at a missing profile → (None, None)
    assert StagedExecutor._account_env(msg, repo=_project(roots.tmp / "r2", profile="nope")) == (None, None)
    # a broken resolver → (None, None), never an exception
    monkeypatch.setattr(C, "resolve", _boom)
    assert StagedExecutor._account_env(msg, repo=repo) == (None, None)


def test_runner_account_env_honors_assigned_account(roots, monkeypatch):
    """multi-account capacity: the runner-assigned account wins over
    active/priority but never over a project pin; the per-thread carrier feeds
    it; absent assignment is byte-identical."""
    from todo_system.orchestrator import runner as runner_mod
    from todo_system.orchestrator.runner import StagedExecutor

    monkeypatch.setenv("MACP_CLAUDE_ACCOUNTS_STATE", str(roots.tmp / "state.json"))
    dirs = _write_profile(roots, "p", [("school", 20), ("dev", 10)], active="school")
    (dirs["school"] / "automation-token").write_text(TOKEN_SCHOOL + "\n", encoding="utf-8")
    (dirs["dev"] / "automation-token").write_text(TOKEN_DEV + "\n", encoding="utf-8")
    repo = _project(roots.tmp / "repo", profile="p")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    msg = SimpleNamespace(task_id="t1", payload={})

    # assigned beats active/priority (active=school) → runs as dev, its own token
    env, name = StagedExecutor._account_env(msg, repo=repo, assigned="dev")
    assert name == "dev" and env["CLAUDE_CONFIG_DIR"] == str(dirs["dev"])
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_DEV
    # a project pin beats assigned (pin=dev, assigned=school) → dev (pin is a hard route)
    _project(repo, profile="p", account="dev")
    env, name = StagedExecutor._account_env(msg, repo=repo, assigned="school")
    assert name == "dev" and env["CLAUDE_CONFIG_DIR"] == str(dirs["dev"])
    # assigned naming no profile account → falls back to the resolved account
    _project(repo, profile="p")                            # drop the pin (active=school)
    assert StagedExecutor._account_env(msg, repo=repo, assigned="ghost")[1] == "school"
    # assigned == the resolved account → unchanged
    assert StagedExecutor._account_env(msg, repo=repo, assigned="school")[1] == "school"
    # the per-thread carrier feeds `assigned` when the caller passes none
    try:
        runner_mod._ASSIGNED_ACCOUNT.name = "dev"
        assert StagedExecutor._account_env(msg, repo=repo)[1] == "dev"
    finally:
        runner_mod._ASSIGNED_ACCOUNT.name = None
    # carrier unset → byte-identical pre-assignment path (the active account)
    assert StagedExecutor._account_env(msg, repo=repo)[1] == "school"


# ---- AC2: transcript root + model probe follow the active account -----------------

def test_transcript_path_default_root_follows_active_account(roots, monkeypatch):
    from todo_system.orchestrator import stages

    school = ACC.create_account_layout("school")
    dev = ACC.create_account_layout("dev")
    C.write_profile(C.Profile(name="p", active_account="school", accounts=[
        C.Account(name="school", claude_config_dir=str(school), priority=20),
        C.Account(name="dev", claude_config_dir=str(dev), priority=10)]))
    monkeypatch.chdir(_project(roots.tmp / "proj", profile="p"))
    sid = "0123abcd-4567-89ef-0123-456789abcdef"
    slug_dir = roots.shared / "projects" / "-tmp-proj"
    slug_dir.mkdir()
    real = slug_dir / f"{sid}.jsonl"
    real.write_text('{"type":"summary"}\n', encoding="utf-8")

    # an env without CLAUDE_CONFIG_DIR → the active account's dir
    found = stages.transcript_path(sid, env={})
    assert found is not None and found.is_relative_to(school)
    assert found.resolve() == real.resolve()
    prof = C.load_profile("p")
    prof.active_account = "dev"
    C.write_profile(prof)
    found = stages.transcript_path(sid, env={})
    assert found is not None and found.is_relative_to(dev)
    assert stages._default_config_dir() == dev
    # env=None reads os.environ (CLAUDE_CONFIG_DIR is unset under roots) → the same default
    assert stages.transcript_path(sid).is_relative_to(dev)
    # an explicit CLAUDE_CONFIG_DIR still wins
    assert stages.transcript_path(sid, env={"CLAUDE_CONFIG_DIR": str(school)}).is_relative_to(school)
    assert stages.transcript_path(sid, env={"CLAUDE_CONFIG_DIR": str(roots.tmp / "empty")}) is None
    assert stages.transcript_path("no-such-session", env={}) is None

    # no profile → the legacy single dir
    monkeypatch.setattr(C, "PROFILES", roots.tmp / "none")
    legacy = roots.home / ".claude"
    assert stages._default_config_dir() == legacy
    assert stages.transcript_path(sid, env={}) is None
    (legacy / "projects" / "-x").mkdir(parents=True)
    (legacy / "projects" / "-x" / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
    assert stages.transcript_path(sid, env={}) == legacy / "projects" / "-x" / f"{sid}.jsonl"
    # a broken resolver falls back to the legacy dir, never raises
    monkeypatch.setattr(C, "resolve", _boom)
    assert stages._default_config_dir() == legacy
    assert stages.transcript_path(sid, env={}) == legacy / "projects" / "-x" / f"{sid}.jsonl"


def test_model_probe_env_follows_active_account(roots, monkeypatch):
    import subprocess

    from todo_system.orchestrator import model_select

    dirs = _write_profile(roots, "p", [("school", 20), ("dev", 10)], active="school")
    school, dev = dirs["school"], dirs["dev"]
    (school / "automation-token").write_text(TOKEN_SCHOOL, encoding="utf-8")
    monkeypatch.chdir(_project(roots.tmp / "proj", profile="p"))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN_BASE)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    seen: dict = {}

    def fake_run(argv, **kw):
        seen["argv"] = list(argv)
        seen["env"] = kw.get("env")
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    monkeypatch.setattr(model_select.subprocess, "run", fake_run)
    assert model_select._probe("claude-x") is True
    assert seen["argv"][:5] == ["claude", "-p", "Reply with just: ok", "--model", "claude-x"]
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == str(school)
    assert seen["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_SCHOOL
    # switching accounts switches the probe's credentials too (dev has no token → key removed)
    prof = C.load_profile("p")
    prof.active_account = "dev"
    C.write_profile(prof)
    assert model_select._probe("claude-x") is True
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == str(dev)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen["env"]
    assert os.environ["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN_BASE

    # the rc=0-plus-quota-notice rule is untouched
    def refused(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, stdout="You're out of usage credits.", stderr="")

    monkeypatch.setattr(model_select.subprocess, "run", refused)
    assert model_select._probe("claude-x") is False


# ---- AC2: usage guard credential file (Keychain only for the legacy dir) -----------

def test_usage_guard_read_token_follows_active_account(roots, monkeypatch):
    import json
    import subprocess

    from todo_system import usage_guard as ug

    # the compat constant is still the legacy single-dir file (fixed at import time)
    assert ug._CREDENTIALS_FILE.name == ".credentials.json"
    assert ug._CREDENTIALS_FILE.parent.name == ".claude"

    dirs = _write_profile(roots, "p", [("school", 20), ("dev", 10)], active="dev")
    dev = dirs["dev"]
    acc_token = "acc-" + "c" * 40
    (dev / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": acc_token}}), encoding="utf-8")
    monkeypatch.chdir(_project(roots.tmp / "proj", profile="p"))

    def keychain_must_not_run(*a, **k):
        raise AssertionError("Keychain consulted for a non-legacy account dir")

    monkeypatch.setattr(ug.subprocess, "run", keychain_must_not_run)
    assert ug._credentials_file() == (dev / ".credentials.json", False)
    assert ug._read_token() == acc_token
    # the other account has no credentials file → None, and still no Keychain fallback
    prof = C.load_profile("p")
    prof.active_account = "school"
    C.write_profile(prof)
    assert ug._credentials_file() == (dirs["school"] / ".credentials.json", False)
    assert ug._read_token() is None
    # an account on the legacy single dir → the legacy file, Keychain allowed
    C.write_profile(C.Profile(name="p", accounts=[C.Account(name="legacy")]))
    assert ug._credentials_file() == (ug._CREDENTIALS_FILE, True)

    # no profile → the legacy path: Keychain first ...
    monkeypatch.setattr(C, "PROFILES", roots.tmp / "none")
    kc_token = "kc-" + "k" * 40

    def keychain_ok(argv, **kw):
        assert list(argv[:2]) == ["security", "find-generic-password"]
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"claudeAiOauth": {"accessToken": kc_token}}), stderr="")

    monkeypatch.setattr(ug.subprocess, "run", keychain_ok)
    assert ug._credentials_file() == (ug._CREDENTIALS_FILE, True)
    assert ug._read_token() == kc_token
    # ... then the legacy credentials file (the monkeypatch seam)
    legacy_file = roots.tmp / "legacy-creds.json"
    monkeypatch.setattr(ug, "_CREDENTIALS_FILE", legacy_file)

    def keychain_missing(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="not found")

    monkeypatch.setattr(ug.subprocess, "run", keychain_missing)
    assert ug._read_token() is None
    file_token = "file-" + "f" * 40
    legacy_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": file_token}}), encoding="utf-8")
    assert ug._credentials_file() == (legacy_file, True)
    assert ug._read_token() == file_token
    # a broken resolver → the legacy path, never raises
    monkeypatch.setattr(C, "resolve", _boom)
    assert ug._credentials_file() == (legacy_file, True)
    assert ug._read_token() == file_token


# ---- AC3: macp account use ---------------------------------------------------------

def test_account_use_cli(roots, monkeypatch, capsys):
    import json

    from todo_system.macp import __main__ as M
    from todo_system.orchestrator import model_select as MS

    # The fixture's self-check already proved the legacy paths live under tmp —
    # the one precondition for exercising the symlink re-point at all.
    legacy = ACC.legacy_config_dir()
    legacy_json = ACC.legacy_claude_json()
    assert legacy.is_relative_to(roots.tmp) and legacy_json.is_relative_to(roots.tmp)

    school = ACC.create_account_layout("school")
    dev = ACC.create_account_layout("dev")
    (school / "automation-token").write_text(TOKEN_SCHOOL, encoding="utf-8")
    C.write_profile(C.Profile(name="p", active_account="school", accounts=[
        C.Account(name="school", claude_config_dir=str(school), priority=20),
        C.Account(name="dev", claude_config_dir=str(dev), priority=10),
        C.Account(name="cx", agent="codex", command="codex exec {prompt}"),
    ]))
    cache = roots.tmp / "model_pref.json"
    monkeypatch.setattr(MS, "_CACHE", cache)
    cache.write_text("{}", encoding="utf-8")

    def use(*argv):
        rc = M.main(["account", "use", *argv])
        return rc, capsys.readouterr().out

    # pre-migration layout (no symlinks): profile switched, cache dropped, nothing linked
    rc, out = use("dev", "--profile", "p")
    assert rc == 0
    assert C.load_profile("p").active_account == "dev"
    assert not cache.exists()
    assert f"active account of profile 'p' → 'dev' ({dev})" in out
    assert "model cache: invalidated" in out
    assert out.count("not a symlink — left as is") == 2
    assert "hint:" in out and "claude-remote-control" in out
    assert not legacy.exists() and not legacy.is_symlink()
    assert not legacy_json.exists() and not legacy_json.is_symlink()
    assert TOKEN_SCHOOL not in out
    # a second call: nothing cached any more; still rc 0
    rc, out = use("dev", "--profile", "p")
    assert rc == 0 and "model cache: nothing cached" in out

    # post-migration layout: both legacy paths are symlinks → re-pointed at the new account
    legacy.symlink_to(school, target_is_directory=True)
    (school / ".claude.json").write_text(
        json.dumps({"theme": "dark", "oauthAccount": {"accountUuid": "uuid-x"}}), encoding="utf-8")
    legacy_json.symlink_to(school / ".claude.json")
    (dev / ".claude.json").write_text(json.dumps({"theme": "light"}), encoding="utf-8")
    rc, out = use("dev", "--profile", "p")
    assert rc == 0
    assert os.readlink(legacy) == str(dev)
    assert os.readlink(legacy_json) == str(dev / ".claude.json")
    assert f"re-pointed {ACC.LEGACY_CONFIG_DIR} → {dev}" in out
    assert f"re-pointed {ACC.LEGACY_CLAUDE_JSON} → {dev / '.claude.json'}" in out
    assert "uuid-x" not in out
    # only the links moved — the old account's files are untouched, the new one's are reached
    assert json.loads((school / ".claude.json").read_text(encoding="utf-8"))["theme"] == "dark"
    assert (school / "automation-token").read_text(encoding="utf-8") == TOKEN_SCHOOL
    assert json.loads(legacy_json.read_text(encoding="utf-8")) == {"theme": "light"}
    assert (legacy / "automation-token").exists() is False and (legacy / "projects").is_dir()
    # already pointing at the account: reported, not re-linked
    rc, out = use("dev", "--profile", "p")
    assert rc == 0 and out.count("already →") == 2
    # back to school: both links follow, and the resolver follows the profile
    rc, out = use("school", "--profile", "p")
    assert rc == 0
    assert os.readlink(legacy) == str(school) and os.readlink(legacy_json) == str(school / ".claude.json")
    assert C.load_profile("p").active_account == "school"
    monkeypatch.chdir(_project(roots.tmp / "proj", profile="p"))
    assert C.current_account() == (C.load_profile("p").accounts[0], "profile")

    # an account whose dir does not exist: profile switched, links left alone + warning
    assert M.main(["account", "add", "ghost", "--profile", "p",
                   "--config-dir", str(roots.tmp / "no-such-dir")]) == 0
    capsys.readouterr()
    rc, out = use("ghost", "--profile", "p")
    assert rc == 0
    assert C.load_profile("p").active_account == "ghost"
    assert os.readlink(legacy) == str(school) and os.readlink(legacy_json) == str(school / ".claude.json")
    assert out.count("warning:") == 2 and "does not exist" in out
    # an account registered ON the legacy dir: nothing to re-point (a self-link would loop)
    assert M.main(["account", "add", "home", "--profile", "p",
                   "--config-dir", ACC.LEGACY_CONFIG_DIR]) == 0
    capsys.readouterr()
    rc, out = use("home", "--profile", "p")
    assert rc == 0 and "nothing to re-point" in out
    assert os.readlink(legacy) == str(school)
    rc, out = use("school", "--profile", "p")
    assert rc == 0
    # a model_select that cannot be used (e.g. no vault marker) is reported, not fatal
    monkeypatch.setattr(MS, "invalidate_cache", _boom)
    rc, out = use("school", "--profile", "p")
    assert rc == 0 and "model cache: skipped (RuntimeError)" in out

    # error paths: rc 2 and the profile file is byte-identical
    before = (C.PROFILES / "p" / "config.toml").read_text(encoding="utf-8")
    for argv in (("nope", "--profile", "p"),        # unknown name
                 ("cx", "--profile", "p"),          # non-claude account
                 ("dev", "--profile", "missing"),   # no such profile
                 ("--profile", "p")):               # no NAME
        rc, out = use(*argv)
        assert rc == 2, argv
        assert out.startswith(("error:", "usage:")), argv
    assert (C.PROFILES / "p" / "config.toml").read_text(encoding="utf-8") == before
    assert C.load_profile("p").active_account == "school"
    assert os.readlink(legacy) == str(school)

    # `macp use <profile>` is a different command and still works
    assert M.main(["use", "p"]) == 0
    assert "default profile set to 'p'" in capsys.readouterr().out
    assert C.default_profile() == "p"


# ---- AC4: dashboard /api/status.account -------------------------------------------

def test_dashboard_collect_account(roots, monkeypatch):
    import importlib.util

    src = Path(__file__).resolve().parents[2] / "scripts" / "macp_dashboard.py"
    spec = importlib.util.spec_from_file_location("macp_dashboard_acct", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    vault = _project(roots.tmp / "vault", profile="default")
    monkeypatch.setattr(mod, "VAULT", vault)

    # no profile at all → the fail-soft shape
    assert mod.collect_account() == {"active": None, "config_dir": "", "source": "none"}
    dirs = _write_profile(roots, "default", [("school", 20), ("dev", 10)])
    school, dev = dirs["school"], dirs["dev"]
    assert mod.collect_account() == {"active": "dev", "config_dir": str(dev), "source": "priority"}
    prof = C.load_profile("default")
    prof.active_account = "school"
    C.write_profile(prof)
    assert mod.collect_account() == {"active": "school", "config_dir": str(school), "source": "profile"}
    _project(vault, profile="default", account="dev")
    assert mod.collect_account() == {"active": "dev", "config_dir": str(dev), "source": "pin"}
    # the anchor is VAULT, not the cwd
    monkeypatch.chdir(_project(roots.tmp / "elsewhere", profile="other"))
    assert mod.collect_account()["active"] == "dev"
    # an account on the legacy dir reports that (expanded) dir
    C.write_profile(C.Profile(name="default", accounts=[C.Account(name="legacy")]))
    _project(vault, profile="default")
    assert mod.collect_account() == {"active": "legacy", "config_dir": str(roots.home / ".claude"),
                                     "source": "priority"}
    # a broken resolver → fail-soft, never raises
    monkeypatch.setattr(C, "resolve", _boom)
    assert mod.collect_account() == {"active": None, "config_dir": "", "source": "none"}

    # wired into /api/status (the full collector needs live vault data, so only the wiring is pinned)
    assert "collect_account" in mod.collect_status.__code__.co_names
    # the web shell renders the field and tolerates its absence (older server)
    text = src.read_text(encoding="utf-8")
    assert 'id="state-acct"' in text and "d.account" in text
