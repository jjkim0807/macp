"""Regression tests for the multi-account Claude layout (leaf 1 of
``macp-multi-claude-account``): ``todo_system/macp/accounts.py``, the additive
profile schema (``active_account`` / ``[[account]].priority``) and the
``account add`` / ``account env`` CLI paths.

Everything runs under ``tmp_path``. The ``roots`` fixture redirects ``HOME``,
the two layout-root env vars and the config module's import-time MACP_HOME
paths, and asserts the redirection before a test body runs, so no test touches
the real home, the real config dir or the real vault. The ``claude`` binary is
never run: ``--resume`` continuity and memory sharing are pinned on the file
model (``stages.transcript_path`` + the ``projects/<key>/memory`` link).

No literal from the identifier gate's PATTERN appears in this file: it sits
inside the scanned engine tree.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from todo_system.macp import __main__ as M
from todo_system.macp import accounts as ACC
from todo_system.macp import config as C
from todo_system.orchestrator import stages

ACCOUNTS_PY = Path(ACC.__file__)


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
    assert C.PROFILES.is_relative_to(tmp_path)
    return SimpleNamespace(tmp=tmp_path, home=home,
                           shared=tmp_path / "shared", accounts=tmp_path / "accounts")


def _links(acct: Path) -> dict[str, tuple[bool, str | None]]:
    return {p.name: (p.is_symlink(), os.readlink(p) if p.is_symlink() else None)
            for p in acct.iterdir()}


# ---- AC1: root resolution + legacy constants ---------------------------------

def test_root_resolution_order(roots, monkeypatch):
    tmp = roots.tmp
    # 1. env
    assert ACC.resolve_shared_root() == tmp / "shared"
    assert ACC.resolve_accounts_root() == tmp / "accounts"
    assert ACC.account_dir("school") == tmp / "accounts" / "school"

    # 2. nearest .macp/config.toml walking up from `start`
    monkeypatch.delenv("MACP_CLAUDE_SHARED_ROOT")
    monkeypatch.delenv("MACP_CLAUDE_ACCOUNTS_ROOT")
    proj = tmp / "proj"
    (proj / ".macp").mkdir(parents=True)
    cfg = proj / ".macp" / "config.toml"
    cfg.write_text(
        'profile = "default"\n'
        f'claude_shared_root = "{tmp / "cfg-shared"}"\n'
        f'claude_accounts_root = "{tmp / "cfg-accounts"}"\n',
        encoding="utf-8")
    sub = proj / "a" / "b"
    sub.mkdir(parents=True)
    assert ACC.resolve_shared_root(start=sub) == tmp / "cfg-shared"
    assert ACC.resolve_accounts_root(start=sub) == tmp / "cfg-accounts"
    assert ACC.account_dir("dev", start=sub) == tmp / "cfg-accounts" / "dev"
    # a relative value is taken relative to the directory holding the config;
    # a tilde value expands against (the patched) HOME
    cfg.write_text('claude_shared_root = "rel-shared"\nclaude_accounts_root = "~/acc"\n',
                   encoding="utf-8")
    assert ACC.resolve_shared_root(start=sub) == proj.resolve() / "rel-shared"
    assert ACC.resolve_accounts_root(start=sub) == roots.home / "acc"

    # 3. config present but without the keys → default under HOME (search stops
    #    at the first config file, like paths.label_prefix)
    bare = tmp / "bare"
    (bare / ".macp").mkdir(parents=True)
    (bare / ".macp" / "config.toml").write_text('profile = "default"\n', encoding="utf-8")
    assert ACC.resolve_shared_root(start=bare) == roots.home / ".claude-shared"
    assert ACC.resolve_accounts_root(start=bare) == roots.home / ".claude-accounts"
    # resolving creates nothing
    assert sorted(p.name for p in roots.home.iterdir()) == []

    # env beats config
    monkeypatch.setenv("MACP_CLAUDE_SHARED_ROOT", str(tmp / "env-shared"))
    assert ACC.resolve_shared_root(start=sub) == tmp / "env-shared"

    # legacy constants: tilde strings, expanded at call time against HOME
    assert ACC.LEGACY_CONFIG_DIR == "~/.claude"
    assert ACC.LEGACY_CLAUDE_JSON == "~/.claude.json"
    assert ACC.legacy_config_dir() == roots.home / ".claude"
    assert ACC.legacy_claude_json() == roots.home / ".claude.json"
    # .claude.json sits beside the legacy dir, inside any other dir
    assert ACC.claude_json_path(ACC.LEGACY_CONFIG_DIR) == roots.home / ".claude.json"
    assert ACC.claude_json_path(roots.home / ".claude") == roots.home / ".claude.json"
    assert ACC.claude_json_path(tmp / "accounts" / "dev") == tmp / "accounts" / "dev" / ".claude.json"


# ---- AC2: layout, idempotence, drift -----------------------------------------

def test_create_layout_symlinks_and_idempotent(roots):
    shared = roots.shared
    acct = ACC.create_account_layout("school")
    assert acct == roots.accounts / "school"
    assert acct.is_dir()

    assert len(ACC.SHARED_ENTRIES) == 9
    for entry in ACC.SHARED_ENTRIES:
        link = acct / entry
        assert link.is_symlink(), entry
        target = os.readlink(link)
        assert os.path.isabs(target), (entry, target)
        assert target == str(shared / entry), (entry, target)
    # directory entries: the shared side exists, so the link resolves
    assert ACC.SHARED_DIR_ENTRIES == {"projects", "commands", "skills", "hooks", "plugins", "agents"}
    for entry in ACC.SHARED_DIR_ENTRIES:
        assert (shared / entry).is_dir(), entry
        assert (acct / entry).is_dir(), entry
    # file entries: linked even though the shared side does not exist yet (dangling)
    file_entries = [e for e in ACC.SHARED_ENTRIES if e not in ACC.SHARED_DIR_ENTRIES]
    assert sorted(file_entries) == ["keybindings.json", "settings.json", "settings.local.json"]
    for entry in file_entries:
        assert not (shared / entry).exists(), entry
        assert (acct / entry).is_symlink() and not (acct / entry).exists(), entry
    # ... so the first write through the link lands on the shared side
    (acct / "settings.json").write_text("{}\n", encoding="utf-8")
    assert (shared / "settings.json").is_file()
    assert (acct / "settings.json").read_text(encoding="utf-8") == "{}\n"
    # nothing per-account is created by the layout
    for entry in ACC.ACCOUNT_ONLY_ENTRIES + ACC.ACCOUNT_CACHE_ENTRIES:
        assert not (acct / entry).exists() and not (acct / entry).is_symlink(), entry

    # idempotent: a second call changes nothing (dangling links included)
    before = _links(acct)
    assert ACC.create_account_layout("school") == acct
    assert _links(acct) == before
    assert ACC.layout_drift(acct) == []

    # a second account points at the very same targets
    dev = ACC.create_account_layout("dev")
    assert dev == roots.accounts / "dev" and dev != acct
    assert {e: os.readlink(dev / e) for e in ACC.SHARED_ENTRIES} == \
        {e: os.readlink(acct / e) for e in ACC.SHARED_ENTRIES}
    assert ACC.layout_drift(dev) == []


def test_create_layout_explicit_roots_and_name_validation(roots):
    tmp = roots.tmp
    acct = ACC.create_account_layout("k", accounts_root=tmp / "alt-acc", shared_root=tmp / "alt-shared")
    assert acct == tmp / "alt-acc" / "k"
    assert os.readlink(acct / "projects") == str(tmp / "alt-shared" / "projects")
    assert not roots.shared.exists() and not roots.accounts.exists()
    # drift is judged against the root it was built with
    assert ACC.layout_drift(acct, shared_root=tmp / "alt-shared") == []
    assert len(ACC.layout_drift(acct)) == 9           # env root ≠ the targets → every entry
    # a tilde root expands against HOME
    acct2 = ACC.create_account_layout("t", accounts_root="~/acc", shared_root="~/sh")
    assert acct2 == roots.home / "acc" / "t"
    assert os.readlink(acct2 / "projects") == str(roots.home / "sh" / "projects")
    for bad in ("", ".", "..", "a/b", "../x"):
        with pytest.raises(ValueError):
            ACC.create_account_layout(bad)
        with pytest.raises(ValueError):
            ACC.account_dir(bad)


def test_layout_drift_reports_replaced_and_missing(roots):
    acct = ACC.create_account_layout("school")
    elsewhere = roots.tmp / "elsewhere"
    elsewhere.mkdir()
    # a file link replaced by a regular file (what an atomic rename-write does)
    (acct / "settings.json").unlink()
    (acct / "settings.json").write_text("{}\n", encoding="utf-8")
    # a link removed
    (acct / "commands").unlink()
    # a dir link replaced by a real directory
    (acct / "projects").unlink()
    (acct / "projects").mkdir()
    # a link pointing somewhere else
    (acct / "agents").unlink()
    (acct / "agents").symlink_to(elsewhere, target_is_directory=True)

    drift = ACC.layout_drift(acct)
    assert len(drift) == 4, drift
    by_entry = {line.split(":", 1)[0]: line for line in drift}
    assert set(by_entry) == {"settings.json", "commands", "projects", "agents"}
    assert "regular file" in by_entry["settings.json"]
    assert "missing" in by_entry["commands"]
    assert "regular directory" in by_entry["projects"]
    assert "symlink to" in by_entry["agents"] and str(elsewhere) in by_entry["agents"]
    assert all(str(roots.shared) in line for line in drift)

    # create_account_layout only fills in what is missing — it never replaces
    # a regular file/dir or re-points a link (data preservation first)
    ACC.create_account_layout("school")
    assert os.readlink(acct / "commands") == str(roots.shared / "commands")
    assert (acct / "settings.json").is_file() and not (acct / "settings.json").is_symlink()
    assert (acct / "projects").is_dir() and not (acct / "projects").is_symlink()
    assert os.readlink(acct / "agents") == str(elsewhere)
    assert {line.split(":", 1)[0] for line in ACC.layout_drift(acct)} == \
        {"settings.json", "projects", "agents"}


# ---- AC8: transcript + memory continuity across accounts --------------------

def test_transcript_path_shared_between_accounts(roots):
    school = ACC.create_account_layout("school")
    dev = ACC.create_account_layout("dev")
    sid = "0123abcd-4567-89ef-0123-456789abcdef"
    slug_dir = roots.shared / "projects" / "-tmp-proj"
    slug_dir.mkdir()
    real = slug_dir / f"{sid}.jsonl"
    real.write_text('{"type":"summary"}\n', encoding="utf-8")

    a = stages.transcript_path(sid, env={"CLAUDE_CONFIG_DIR": str(school)})
    b = stages.transcript_path(sid, env={"CLAUDE_CONFIG_DIR": str(dev)})
    assert a is not None and b is not None
    # each account sees it under its own (unresolved) config dir ...
    assert a.is_relative_to(school) and b.is_relative_to(dev)
    assert a != b
    # ... but it is one and the same file
    assert a.resolve() == b.resolve() == real.resolve()
    assert a.read_text(encoding="utf-8") == real.read_text(encoding="utf-8")
    # controls: no layout → nothing; unknown id → nothing
    bare = roots.tmp / "bare-config"
    bare.mkdir()
    assert stages.transcript_path(sid, env={"CLAUDE_CONFIG_DIR": str(bare)}) is None
    assert stages.transcript_path("no-such-session", env={"CLAUDE_CONFIG_DIR": str(school)}) is None


def test_memory_symlink_resolves_to_same_vault_path(roots):
    vault_mem = roots.tmp / "vault" / "Memory" / "claude" / "vault"
    vault_mem.mkdir(parents=True)
    (vault_mem / "note.md").write_text("# note\n", encoding="utf-8")
    school = ACC.create_account_layout("school")
    dev = ACC.create_account_layout("dev")
    key = "-tmp-proj"
    proj_dir = roots.shared / "projects" / key
    proj_dir.mkdir()
    # the real layout: an ABSOLUTE link projects/<key>/memory → <vault>/Memory/claude/vault
    (proj_dir / "memory").symlink_to(vault_mem, target_is_directory=True)
    assert os.path.isabs(os.readlink(proj_dir / "memory"))

    for acct in (school, dev):
        link = acct / "projects" / key / "memory"
        assert link.resolve() == vault_mem.resolve(), acct
        assert (link / "note.md").read_text(encoding="utf-8") == "# note\n"
    assert (school / "projects" / key / "memory").resolve() == \
        (dev / "projects" / key / "memory").resolve()


# ---- AC3: .claude.json seeding -----------------------------------------------

def test_seed_claude_json_whitelist_only(roots):
    tmp = roots.tmp
    src = tmp / "src" / ".claude.json"
    src.parent.mkdir()
    full = {k: f"v-{k}" for k in ACC.CLAUDE_JSON_SEED_KEYS}
    full["projects"] = {"/p": {"allowedTools": ["Bash(git:*)"], "mcpServers": {}, "hasTrustDialogAccepted": True}}
    full["hasCompletedOnboarding"] = True
    full.update({
        "oauthAccount": {"accountUuid": "uuid-x", "emailAddress": "redacted", "organizationUuid": "org-x"},
        "userID": "user-x",
        "subscriptionType": "max",
        "somethingNew": 1,
    })
    src.write_text(json.dumps(full), encoding="utf-8")
    dst = tmp / "dst" / ".claude.json"

    assert ACC.seed_claude_json(src, dst) is True
    out = json.loads(dst.read_text(encoding="utf-8"))
    assert set(out) == set(ACC.CLAUDE_JSON_SEED_KEYS)
    for k in ("oauthAccount", "userID", "subscriptionType", "somethingNew"):
        assert k not in out, k
    assert out["projects"] == full["projects"]
    assert out["hasCompletedOnboarding"] is True
    assert stat.S_IMODE(dst.stat().st_mode) == 0o600
    assert "uuid-x" not in dst.read_text(encoding="utf-8")

    # one-shot: an existing destination is never overwritten
    text = dst.read_text(encoding="utf-8")
    src.write_text(json.dumps({"theme": "light"}), encoding="utf-8")
    assert ACC.seed_claude_json(src, dst) is False
    assert dst.read_text(encoding="utf-8") == text

    # missing / broken / non-object / auth-only sources → False, nothing written
    d2 = tmp / "d2" / ".claude.json"
    assert ACC.seed_claude_json(tmp / "nope.json", d2) is False
    broken = tmp / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert ACC.seed_claude_json(broken, d2) is False
    arr = tmp / "arr.json"
    arr.write_text("[1, 2]", encoding="utf-8")
    assert ACC.seed_claude_json(arr, d2) is False
    auth_only = tmp / "auth.json"
    auth_only.write_text(json.dumps({"oauthAccount": {"accountUuid": "u"}, "userID": "u"}), encoding="utf-8")
    assert ACC.seed_claude_json(auth_only, d2) is False
    assert not d2.exists() and not d2.parent.exists()
    # a partial source seeds only what it has
    partial = tmp / "partial.json"
    partial.write_text(json.dumps({"theme": "dark", "userID": "u"}), encoding="utf-8")
    assert ACC.seed_claude_json(partial, d2) is True
    assert json.loads(d2.read_text(encoding="utf-8")) == {"theme": "dark"}


# ---- AC4: account add ---------------------------------------------------------

def test_account_add_builds_layout_seeds_and_prints_login_hint(roots, capsys):
    # no profile → rc 2, and no layout is created before the check
    assert M.main(["account", "add", "z", "--profile", "nope"]) == 2
    assert "error: profile 'nope' not found" in capsys.readouterr().out
    assert not roots.accounts.exists() and not roots.shared.exists()

    C.write_profile(C.Profile(name="p"))
    rc = M.main(["account", "add", "school", "--profile", "p"])
    out = capsys.readouterr().out
    assert rc == 0
    school = roots.accounts / "school"
    assert school.is_dir() and ACC.layout_drift(school) == []
    prof = C.load_profile("p")
    assert [a.name for a in prof.accounts] == ["school"]
    assert prof.accounts[0].agent == "claude"
    assert prof.accounts[0].claude_config_dir == str(school)
    assert prof.accounts[0].priority == 100 and prof.active_account == ""
    assert f"added account 'school' [claude] ({school}) to profile 'p'" in out
    assert "parallel capacity 1x" in out
    assert f"CLAUDE_CONFIG_DIR={school} claude /login" in out
    # first account of the profile: nothing to seed from
    assert "seeded" not in out
    assert not (school / ".claude.json").exists()

    # the CLI's own .claude.json after login (auth + settings in one file)
    (school / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"accountUuid": "uuid-x", "emailAddress": "redacted"},
        "userID": "user-x",
        "theme": "dark",
        "hasCompletedOnboarding": True,
    }), encoding="utf-8")
    rc = M.main(["account", "add", "dev", "--profile", "p"])
    out = capsys.readouterr().out
    assert rc == 0
    dev = roots.accounts / "dev"
    assert ACC.layout_drift(dev) == []
    seeded = json.loads((dev / ".claude.json").read_text(encoding="utf-8"))
    assert seeded == {"theme": "dark", "hasCompletedOnboarding": True}
    assert "oauthAccount" not in seeded and "userID" not in seeded
    assert "seeded .claude.json (non-auth keys only) from account 'school'" in out
    assert f"CLAUDE_CONFIG_DIR={dev} claude /login" in out
    # only paths and names are printed — never a value from the source file
    assert "uuid-x" not in out and "user-x" not in out and "redacted" not in out
    assert [a.name for a in C.load_profile("p").accounts] == ["school", "dev"]
    # both accounts share the same targets
    assert os.readlink(dev / "projects") == os.readlink(school / "projects") == \
        str(roots.shared / "projects")

    # duplicate: rc 0, nothing changes
    rc = M.main(["account", "add", "dev", "--profile", "p"])
    out = capsys.readouterr().out
    assert rc == 0 and "already exists" in out and "claude /login" not in out
    assert len(C.load_profile("p").accounts) == 2

    # active_account picks the seed source (dev here, not the first account)
    (dev / ".claude.json").write_text(json.dumps({"theme": "light", "userID": "user-y"}), encoding="utf-8")
    prof = C.load_profile("p")
    prof.active_account = "dev"
    C.write_profile(prof)
    rc = M.main(["account", "add", "third", "--profile", "p"])
    out = capsys.readouterr().out
    assert rc == 0
    assert json.loads((roots.accounts / "third" / ".claude.json").read_text(encoding="utf-8")) == {"theme": "light"}
    assert "from account 'dev'" in out
    assert C.load_profile("p").active_account == "dev"     # untouched by add

    # pre-existing content in the account dir is kept and reported as drift
    old = roots.accounts / "old"
    old.mkdir()
    (old / "settings.json").write_text('{"keep": true}\n', encoding="utf-8")
    rc = M.main(["account", "add", "old", "--profile", "p"])
    out = capsys.readouterr().out
    assert rc == 0
    assert (old / "settings.json").is_file() and not (old / "settings.json").is_symlink()
    assert (old / "settings.json").read_text(encoding="utf-8") == '{"keep": true}\n'
    assert "warning: layout drift — settings.json: regular file" in out
    assert f"CLAUDE_CONFIG_DIR={old} claude /login" in out

    # an invalid name is rejected before anything is written
    rc = M.main(["account", "add", "../x", "--profile", "p"])
    out = capsys.readouterr().out
    assert rc == 2 and out.startswith("error:")
    assert [a.name for a in C.load_profile("p").accounts] == ["school", "dev", "third", "old"]


def test_account_add_explicit_config_dir_and_non_claude_unchanged(roots, capsys):
    C.write_profile(C.Profile(name="p"))
    x = roots.tmp / "x"
    rc = M.main(["account", "add", "a1", "--profile", "p", "--config-dir", str(x)])
    out = capsys.readouterr().out
    assert rc == 0
    # exactly the two historical lines, no layout, no hint
    assert out.splitlines() == [
        f"added account 'a1' [claude] ({x}) to profile 'p'",
        "  profile 'p' now has 1 account(s) = parallel capacity 1x",
    ]
    assert not x.exists() and not roots.accounts.exists() and not roots.shared.exists()
    assert C.load_profile("p").accounts[0].claude_config_dir == str(x)
    # a tilde value is stored verbatim (consumers expand it)
    rc = M.main(["account", "add", "a2", "--profile", "p", "--config-dir", "~/cfg2"])
    out = capsys.readouterr().out
    assert rc == 0 and "claude /login" not in out and len(out.splitlines()) == 2
    assert C.load_profile("p").accounts[1].claude_config_dir == "~/cfg2"

    rc = M.main(["account", "add", "cx", "--profile", "p",
                 "--agent", "codex", "--command", "codex exec {prompt}"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.splitlines() == [
        "added account 'cx' [codex] (codex exec {prompt}) to profile 'p'",
        "  profile 'p' now has 3 account(s) = parallel capacity 3x",
    ]
    assert not roots.accounts.exists() and not roots.shared.exists()
    cx = C.load_profile("p").accounts[2]
    assert (cx.agent, cx.command) == ("codex", "codex exec {prompt}")
    assert cx.claude_config_dir == ACC.LEGACY_CONFIG_DIR       # in-memory default only ...
    blocks = C.PROFILES.joinpath("p", "config.toml").read_text(encoding="utf-8").split("[[account]]")
    assert 'name = "cx"' in blocks[3] and "claude_config_dir" not in blocks[3]   # ... never written

    # ⑤ expanded each claude line (optional active mark, config dir — shown as
    # configured, so the tilde survives — then priority / creds / token / status /
    # usage); the non-claude line keeps the short form. One line per account, in
    # order, with the config dir / command intact.
    rc = M.main(["account", "list", "--profile", "p"])
    lines = capsys.readouterr().out.splitlines()
    assert rc == 0
    assert len(lines) == 3
    a1_line = next(ln for ln in lines if "a1 [claude]" in ln)
    a2_line = next(ln for ln in lines if "a2 [claude]" in ln)
    cx_line = next(ln for ln in lines if "cx [codex]" in ln)
    assert f"a1 [claude]: {x}" in a1_line
    assert "a2 [claude]: ~/cfg2" in a2_line                 # tilde shown as configured
    assert cx_line.strip() == "cx [codex]: codex exec {prompt}"   # non-claude unchanged
    assert "creds=" in a1_line and "token=" in a1_line and "status=" in a1_line


# ---- AC5: account env ---------------------------------------------------------

def test_account_env_output_and_selection(roots, capsys):
    a_dir = roots.tmp / "cfg-school"
    b_dir = roots.tmp / "cfg-dev"
    prof = C.Profile(name="p", accounts=[
        C.Account(name="school", claude_config_dir=str(a_dir), priority=20),
        C.Account(name="dev", claude_config_dir=str(b_dir), priority=10),
        C.Account(name="cx", agent="codex", command="codex exec {prompt}", priority=1),
    ])
    C.write_profile(prof)

    def env(*argv):
        rc = M.main(["account", "env", *argv])
        cap = capsys.readouterr()
        return rc, cap.out, cap.err

    # no NAME, no active_account → lowest priority among claude accounts (cx is ignored)
    assert env("--profile", "p") == (0, f"export CLAUDE_CONFIG_DIR='{b_dir}'\n", "")
    # active_account wins
    prof.active_account = "school"
    C.write_profile(prof)
    assert env("--profile", "p") == (0, f"export CLAUDE_CONFIG_DIR='{a_dir}'\n", "")
    # an active_account that is not a claude account falls through to priority
    prof.active_account = "cx"
    C.write_profile(prof)
    assert env("--profile", "p") == (0, f"export CLAUDE_CONFIG_DIR='{b_dir}'\n", "")
    # explicit NAME
    assert env("school", "--profile", "p") == (0, f"export CLAUDE_CONFIG_DIR='{a_dir}'\n", "")

    # error paths: rc 2, stdout EMPTY (eval-safe), message on stderr
    C.write_profile(C.Profile(name="q", accounts=[C.Account(name="cx", agent="codex", command="x")]))
    C.write_profile(C.Profile(name="r"))
    for argv in (("nope", "--profile", "p"),          # unknown name
                 ("cx", "--profile", "p"),            # non-claude by name
                 ("--profile", "missing"),            # no profile
                 ("--profile", "q"),                  # only non-claude accounts
                 ("--profile", "r")):                 # no accounts at all
        rc, out, err = env(*argv)
        assert rc == 2, argv
        assert out == "", argv
        assert err.startswith("error:") and err.endswith("\n"), argv

    # a tilde value is expanded for the shell; a quote inside is escaped
    C.write_profile(C.Profile(name="t", accounts=[C.Account(name="s", claude_config_dir="~/x'y")]))
    assert env("--profile", "t") == (
        0, "export CLAUDE_CONFIG_DIR='" + str(roots.home / "x") + "'\\''y'\n", "")
    # the legacy default dir prints NOTHING (rc 0) since #10: an explicit
    # CLAUDE_CONFIG_DIR, even one spelling the default path, makes the CLI skip
    # its Keychain login (2026-10-19 Remote Control outage). Same rule as
    # accounts.claude_env_for; test_macp_cli pins it from the CLI side too.
    C.write_profile(C.Profile(name="u", accounts=[C.Account(name="s")]))
    assert env("--profile", "u") == (0, "", "")


def test_pick_account_rules():
    def acc(name, agent="claude", priority=100):
        return SimpleNamespace(name=name, agent=agent, priority=priority)

    a, b, c = acc("a", priority=20), acc("b", priority=10), acc("c", "codex", 1)
    assert ACC.pick_account([a, b, c]) is b                       # lowest priority, claude only
    assert ACC.pick_account([a, b, c], "a") is a                  # active name wins
    assert ACC.pick_account([a, b, c], "c") is b                  # active non-claude → fall through
    assert ACC.pick_account([a, b, c], "zzz") is b                # unknown active → fall through
    assert ACC.pick_account([a, b, c], agent=None) is c           # agent=None: everyone
    assert ACC.pick_account([c]) is None
    assert ACC.pick_account([]) is None
    first, second = acc("first"), acc("second")
    assert ACC.pick_account([first, second]) is first             # ties: listing order
    assert ACC.pick_account([acc("x", priority="bad"), acc("y", priority=50)]).name == "y"
    assert ACC.pick_account([SimpleNamespace(name="n", agent="claude")]).name == "n"   # no priority attr


# ---- AC6: profile schema ------------------------------------------------------

def test_profile_schema_roundtrip_and_default_output_unchanged(roots):
    # a profile that never uses the new fields writes exactly the historical text
    path = C.write_profile(C.Profile(name="p", accounts=[C.Account(name="x")]))
    expected = "\n".join([
        'name = "p"', 'vault = ""', 'daily_run_budget = 40', 'max_parallel_per_repo = 2',
        'model = "auto"', '', '[[account]]', 'name = "x"', 'agent = "claude"',
        f'claude_config_dir = "{ACC.LEGACY_CONFIG_DIR}"', '',
    ])
    assert path.read_text(encoding="utf-8") == expected
    loaded = C.load_profile("p")
    assert loaded.active_account == "" and loaded.accounts[0].priority == 100

    # new fields are appended with defaults — existing field order is untouched
    assert [f.name for f in dataclasses.fields(C.Account)] == \
        ["name", "agent", "claude_config_dir", "command", "env", "priority",
         "key_file", "models"]
    assert [f.name for f in dataclasses.fields(C.Profile)] == \
        ["name", "vault", "daily_run_budget", "max_parallel_per_repo", "model", "accounts",
         "active_account", "max_parallel_gradle_builds"]
    assert C.Account("pos").claude_config_dir == ACC.LEGACY_CONFIG_DIR

    # non-default values round-trip through the documented keys
    path = C.write_profile(C.Profile(name="q", active_account="dev", accounts=[
        C.Account(name="dev", priority=10),
        C.Account(name="school", priority=20),
        C.Account(name="cx", agent="codex", command="codex exec {prompt}", env={"K": "v"}, priority=5),
    ]))
    text = path.read_text(encoding="utf-8")
    assert 'active_account = "dev"' in text
    assert text.count("priority = ") == 3 and "priority = 10" in text and "priority = 5" in text
    assert text.index('active_account = "dev"') < text.index("[[account]]")
    q = C.load_profile("q")
    assert q is not None                                           # tomllib accepted it
    assert q.active_account == "dev"
    assert [(a.name, a.priority) for a in q.accounts] == [("dev", 10), ("school", 20), ("cx", 5)]
    assert q.accounts[2].env == {"K": "v"} and q.accounts[2].command == "codex exec {prompt}"
    # an older profile without the keys loads with the defaults
    (C.PROFILES / "old").mkdir(parents=True)
    (C.PROFILES / "old" / "config.toml").write_text(
        'name = "old"\n\n[[account]]\nname = "a"\nagent = "claude"\n', encoding="utf-8")
    old = C.load_profile("old")
    assert old.active_account == "" and old.accounts[0].priority == 100
    assert old.accounts[0].claude_config_dir == ACC.LEGACY_CONFIG_DIR

    # account_for: pin → round-robin, as before (priority/active_account have no consumer here)
    assert C.account_for(q, C.ProjectConfig(profile="q", account="school")).name == "school"
    rr = C.account_for(q, C.ProjectConfig(profile="q"), "task-1")
    assert rr in q.accounts
    assert C.account_for(q, C.ProjectConfig(profile="q"), "task-1") is rr    # stable per task_id
    assert C.account_for(q, None) is q.accounts[0]                            # no task_id → first
    assert C.account_for(C.Profile(name="e"), None) is None

    # build_invocation: the legacy default dir → no override; any other dir → set
    _, env = C.build_invocation(C.Account(name="x"), "hi")
    assert "CLAUDE_CONFIG_DIR" not in env
    _, env = C.build_invocation(C.Account(name="y", claude_config_dir=str(roots.home / ".claude")), "hi")
    assert "CLAUDE_CONFIG_DIR" not in env
    other = roots.accounts / "dev"
    argv, env = C.build_invocation(C.Account(name="dev", claude_config_dir=str(other), priority=10),
                                   "hi", ["--model", "m"])
    assert argv == ["claude", "-p", "hi", "--model", "m"]
    assert env["CLAUDE_CONFIG_DIR"] == str(other)


# ---- AC1: leaf module, stdlib only, literal confined ------------------------

def test_accounts_module_is_leaf_and_stdlib():
    source = ACCOUNTS_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    absolute: set[str] = set()
    relative: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            absolute.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative.add(node.module or "")
            else:
                absolute.add((node.module or "").split(".")[0])
    assert absolute, "no imports parsed"
    assert absolute <= set(sys.stdlib_module_names), absolute - set(sys.stdlib_module_names)
    assert relative == {"paths"}                      # never `config` (config imports accounts)
    # the legacy literal lives only in the two LEGACY_* constants
    assert source.count("~/.claude") == 2
    lines = [ln for ln in source.splitlines() if "~/.claude" in ln]
    assert lines == ['LEGACY_CONFIG_DIR = "~/.claude"', 'LEGACY_CLAUDE_JSON = "~/.claude.json"']
    # and nowhere else in the macp package
    pkg = ACCOUNTS_PY.parent
    for name in ("config.py", "__init__.py", "__main__.py"):
        assert "~/.claude" not in (pkg / name).read_text(encoding="utf-8"), name
