"""Regression tests for ``scripts/claude_accounts_migrate.py`` (leaf ② of
``macp-multi-claude-account``): the one-shot move of the legacy single config
dir into the shared layout — dry-run, precheck, daemon gate, manifest-first
apply and manifest-driven rollback.

Everything runs under ``tmp_path``. The ``roots`` fixture (a copy of the one in
``test_claude_accounts_layout.py``) redirects ``HOME``, the two layout-root env
vars and the config module's MACP_HOME paths, and asserts the redirection before
a test body runs, so no test touches the real home, the real config dir or the
real vault. ``systemctl`` is never executed — the script's ``subprocess.run``
is replaced by a fake — and the ``claude`` binary is never run. The script is
loaded by path (``scripts/`` has no ``__init__.py``), as
``test_claude_auth_watch.py`` does.

The fake credentials below are sentinels: every test that produces output
asserts they never appear in stdout or in the manifest (AC5).

No literal from the identifier gate's PATTERN appears in this file: it sits
inside the scanned engine tree.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from todo_system import claude_auth
from todo_system.macp import accounts as ACC
from todo_system.macp import config as C
from todo_system.orchestrator import stages

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "claude_accounts_migrate.py"


def _load():
    spec = importlib.util.spec_from_file_location("claude_accounts_migrate", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


MIG = _load()

# Shaped like a real setup-token (>= 40 chars, no whitespace / slash / tilde /
# dollar) so the auth module's own check accepts it — and still a sentinel.
FAKE_TOKEN = "sk-ant-oat01-" + "A1b2C3d4" * 6
FAKE_ACCESS = "fake-access-token-sentinel-" + "z" * 32
FAKE_USER_ID = "FAKE-USER-ID-SENTINEL"
SENTINELS = (FAKE_TOKEN, FAKE_ACCESS, FAKE_USER_ID)
SID = "0123abcd-4567-89ef-0123-456789abcdef"


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
    assert Path.home() == home
    assert ACC.resolve_shared_root().is_relative_to(tmp_path)
    assert ACC.resolve_accounts_root().is_relative_to(tmp_path)
    assert ACC.legacy_config_dir().is_relative_to(tmp_path)
    assert C.PROFILES.is_relative_to(tmp_path)
    return SimpleNamespace(tmp=tmp_path, home=home,
                           shared=tmp_path / "shared", accounts=tmp_path / "accounts")


@pytest.fixture
def legacy_tree(roots):
    """A fake pre-layout config dir: transcripts + a RELATIVE memory link (plus
    an absolute one and a dangling relative one), settings, commands, hooks,
    credentials, automation token, one CLI cache dir, the settings file beside
    the dir, and the HQ-shaped profile (``daily_run_budget = 0`` only).
    ``skills/``, ``plugins/``, ``agents/``, ``settings.local.json`` and
    ``keybindings.json`` are deliberately absent."""
    legacy = ACC.legacy_config_dir()
    legacy_json = ACC.legacy_claude_json()
    vault_mem = roots.tmp / "vault" / "Memory" / "claude" / "vault"
    vault_mem.mkdir(parents=True)
    (vault_mem / "note.md").write_text("# note\n", encoding="utf-8")

    proj = legacy / "projects" / "-tmp-proj"
    proj.mkdir(parents=True)
    (proj / f"{SID}.jsonl").write_text('{"type":"summary"}\n', encoding="utf-8")
    (proj / "memory").symlink_to(os.path.relpath(vault_mem, proj), target_is_directory=True)
    assert not os.path.isabs(os.readlink(proj / "memory"))
    assert (proj / "memory" / "note.md").is_file()        # resolves in place ...
    abs_proj = legacy / "projects" / "-tmp-abs"
    abs_proj.mkdir()
    (abs_proj / "memory").symlink_to(vault_mem, target_is_directory=True)
    gone = legacy / "projects" / "-tmp-gone"
    gone.mkdir()
    (gone / "memory").symlink_to("../../../nowhere/memory")
    assert not (gone / "memory").exists()

    (legacy / "settings.json").write_text('{"hooks": {}}\n', encoding="utf-8")
    (legacy / "commands").mkdir()
    (legacy / "commands" / "x.md").write_text("# x\n", encoding="utf-8")
    (legacy / "hooks").mkdir()
    (legacy / "hooks" / "stop.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (legacy / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": FAKE_ACCESS}}), encoding="utf-8")
    os.chmod(legacy / ".credentials.json", 0o600)
    (legacy / "automation-token").write_text(FAKE_TOKEN + "\n", encoding="utf-8")
    os.chmod(legacy / "automation-token", 0o600)
    (legacy / "statsig").mkdir()
    (legacy / "statsig" / "cache.bin").write_bytes(b"\x00" * 16)
    os.chmod(legacy, 0o700)
    legacy_json.write_text(json.dumps({
        "oauthAccount": {"emailAddress": "redacted"}, "userID": FAKE_USER_ID, "theme": "dark",
    }), encoding="utf-8")

    profile_text = "# unlimited (2026-10-13)\ndaily_run_budget = 0\n"
    profile_path = C.PROFILES / "default" / "config.toml"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(profile_text, encoding="utf-8")

    acct = roots.accounts / "school"
    return SimpleNamespace(legacy=legacy, legacy_json=legacy_json, vault_mem=vault_mem,
                           profile_path=profile_path, profile_text=profile_text,
                           acct=acct, acct_json=acct / legacy_json.name)


def snapshot(root: Path) -> dict[str, tuple]:
    """``{relative path: ("symlink", target) | ("dir", mode) | ("file", bytes, mode)}``
    without following links; a root that is itself a link is ``{"": (...)}``."""
    root = Path(root)
    if root.is_symlink():
        return {"": ("symlink", os.readlink(root))}
    if not root.exists():
        return {}
    out: dict[str, tuple] = {"": ("dir", stat.S_IMODE(os.lstat(root).st_mode))}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            st = os.lstat(p)
            rel = str(p.relative_to(root))
            if stat.S_ISLNK(st.st_mode):
                out[rel] = ("symlink", os.readlink(p))
            elif stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir", stat.S_IMODE(st.st_mode))
            else:
                out[rel] = ("file", st.st_size, stat.S_IMODE(st.st_mode))
    return out


def fake_systemctl(monkeypatch, active=frozenset(), *, missing=False) -> list[list[str]]:
    """Replace the script's ``subprocess.run``: records calls, answers
    ``is-active`` with rc 0 for units in ``active`` and rc 3 otherwise."""
    calls: list[list[str]] = []

    def fake(cmd, *args, **kwargs):
        calls.append(list(cmd))
        assert list(cmd[:3]) == ["systemctl", "--user", "is-active"], cmd
        if missing:
            raise FileNotFoundError("systemctl")
        on = cmd[3] in active
        return subprocess.CompletedProcess(cmd, 0 if on else 3,
                                           stdout="active\n" if on else "inactive\n", stderr="")

    monkeypatch.setattr(MIG.subprocess, "run", fake)
    return calls


def _run(capsys, argv: list[str]) -> tuple[int, str]:
    rc = MIG.main(argv)
    return rc, capsys.readouterr().out


def _assert_no_secret(text: str) -> None:
    for s in SENTINELS:
        assert s not in text


# ---- AC1: dry-run changes nothing ----------------------------------------------

def test_fake_token_shape_matches_real_check():
    assert claude_auth._looks_like_token(FAKE_TOKEN) is True


def test_dry_run_prints_plan_and_changes_nothing(roots, legacy_tree, capsys):
    before = snapshot(roots.tmp)
    rc, out = _run(capsys, [])
    assert rc == 0
    assert snapshot(roots.tmp) == before
    assert not roots.shared.exists() and not roots.accounts.exists()
    legacy, acct = legacy_tree.legacy, legacy_tree.acct
    assert "dry-run" in out
    assert f"{legacy / 'projects'} -> {roots.shared / 'projects'}" in out
    assert f"{legacy / 'settings.json'} -> {roots.shared / 'settings.json'}" in out
    assert f"-> {acct / '.credentials.json'}" in out
    assert f"-> {acct / 'automation-token'}" in out
    assert f"{len(FAKE_TOKEN) + 1} bytes" in out            # size only ...
    assert f"{legacy / 'statsig'} -> {acct / 'statsig'}" in out
    assert f"{legacy_tree.legacy_json} -> {legacy_tree.acct_json}" in out
    assert f"symlink  {legacy} -> {acct}" in out
    assert f"symlink  {legacy_tree.legacy_json} -> {legacy_tree.acct_json}" in out
    assert "layout" in out and "new shared dirs: skills, plugins, agents" in out
    assert f"repoint  {roots.shared / 'projects' / '-tmp-proj' / 'memory'}:" in out
    assert "(skipped: dangling)" in out
    repoints = [ln for ln in out.splitlines() if "repoint" in ln]
    assert len(repoints) == 2                                # relative + dangling ...
    assert not any("-tmp-abs" in ln for ln in repoints)      # ... never the absolute one
    assert str(roots.accounts / "_migration-") in out
    assert f"profile:               {legacy_tree.profile_path}: add account 'school'" in out
    assert "priority 10" in out and 'active_account = "school"' in out
    assert "daily_run_budget = 0 preserved" in out
    _assert_no_secret(out)                                   # ... never the content


def test_precheck_nothing_to_migrate(roots, capsys):
    before = snapshot(roots.tmp)
    rc, out = _run(capsys, [])
    assert rc == 2
    assert f"nothing to migrate: {ACC.legacy_config_dir()}" in out
    rc, out = _run(capsys, ["--apply"])
    assert rc == 2 and "nothing to migrate" in out
    assert snapshot(roots.tmp) == before
    assert not roots.shared.exists() and not roots.accounts.exists()


def test_precheck_conflicts_change_nothing(roots, legacy_tree, capsys):
    # (b) a shared entry already occupied
    (roots.shared / "projects").mkdir(parents=True)
    before = snapshot(roots.tmp)
    rc, out = _run(capsys, [])
    assert rc == 2
    assert f"already exists: {roots.shared / 'projects'}" in out
    assert "conflicts" in out
    rc, out = _run(capsys, ["--apply"])
    assert rc == 2 and "already exists" in out
    assert snapshot(roots.tmp) == before
    (roots.shared / "projects").rmdir()
    roots.shared.rmdir()
    # (c) a non-empty account dir
    legacy_tree.acct.mkdir(parents=True)
    (legacy_tree.acct / "stray").write_text("x\n", encoding="utf-8")
    before = snapshot(roots.tmp)
    rc, out = _run(capsys, ["--apply"])
    assert rc == 2
    assert f"account dir exists and is not empty: {legacy_tree.acct}" in out
    assert snapshot(roots.tmp) == before
    (legacy_tree.acct / "stray").unlink()
    legacy_tree.acct.rmdir()
    roots.accounts.rmdir()
    # (d) an invalid account name
    before = snapshot(roots.tmp)
    rc, out = _run(capsys, ["--account", "../x", "--apply"])
    assert rc == 2 and "invalid account name" in out
    assert snapshot(roots.tmp) == before
    assert not roots.shared.exists() and not roots.accounts.exists()
    # (e) the legacy settings file already a symlink (partial earlier apply)
    legacy_tree.legacy_json.unlink()
    legacy_tree.legacy_json.symlink_to(roots.tmp / "elsewhere.json")
    before = snapshot(roots.tmp)
    rc, out = _run(capsys, ["--apply"])
    assert rc == 2 and "already a symlink" in out and "--rollback" in out
    assert snapshot(roots.tmp) == before
    _assert_no_secret(out)


def test_runs_as_bare_process(roots, legacy_tree):
    """The sys.path bootstrap carries a bare interpreter (no venv, isolated
    mode, minimal env) — the way a human runs it from a vault checkout."""
    before = snapshot(roots.tmp)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(roots.home),
        "MACP_HOME": str(roots.tmp / "macp"),
        "MACP_CLAUDE_SHARED_ROOT": str(roots.shared),
        "MACP_CLAUDE_ACCOUNTS_ROOT": str(roots.accounts),
    }
    proc = subprocess.run([sys.executable, "-I", str(SCRIPT)], cwd=str(roots.tmp), env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "dry-run" in proc.stdout
    assert str(legacy_tree.acct) in proc.stdout
    assert f"profile:               {legacy_tree.profile_path}" in proc.stdout
    assert snapshot(roots.tmp) == before
    _assert_no_secret(proc.stdout + proc.stderr)


# ---- AC2: daemon gate -----------------------------------------------------------

def _manifests(roots) -> list[Path]:
    if not roots.accounts.exists():
        return []
    return sorted(p / "manifest.json" for p in roots.accounts.iterdir()
                  if p.name.startswith("_migration-"))


def test_apply_refuses_while_daemons_active(roots, legacy_tree, capsys, monkeypatch):
    before = snapshot(roots.tmp)
    calls = fake_systemctl(monkeypatch, active={"macp-runner-worker-default.service"})
    rc, out = _run(capsys, ["--apply"])
    assert rc == 3
    assert "refusing to apply: active units: macp-runner-worker-default.service" in out
    assert "systemctl --user stop" in out and "--i-stopped-daemons" in out
    for unit in MIG.HARD_UNITS + MIG.ADVISORY_UNITS:
        assert unit in out, unit
    assert [c[3] for c in calls] == list(MIG.HARD_UNITS)     # every hard unit was asked
    assert snapshot(roots.tmp) == before
    assert _manifests(roots) == []                           # no manifest before the gate
    # a live timer alone blocks too: the oneshot .service is inactive between ticks
    fake_systemctl(monkeypatch, active={"macp-orchestrator.timer"})
    rc, out = _run(capsys, ["--apply"])
    assert rc == 3 and "active units: macp-orchestrator.timer" in out
    assert snapshot(roots.tmp) == before
    # no systemctl at all -> fail closed
    fake_systemctl(monkeypatch, missing=True)
    rc, out = _run(capsys, ["--apply"])
    assert rc == 3 and "cannot verify daemons" in out and "--i-stopped-daemons" in out
    assert snapshot(roots.tmp) == before
    # the dry-run never asks systemctl
    calls = fake_systemctl(monkeypatch, active=set(MIG.HARD_UNITS))
    rc, out = _run(capsys, [])
    assert rc == 0 and calls == []
    assert snapshot(roots.tmp) == before
    # --i-stopped-daemons bypasses the gate without asking
    calls = fake_systemctl(monkeypatch, active=set(MIG.HARD_UNITS))
    rc, out = _run(capsys, ["--apply", "--i-stopped-daemons"])
    assert rc == 0 and calls == [] and "daemon check skipped" in out
    assert legacy_tree.legacy.is_symlink()
    _assert_no_secret(out)


def test_hard_units_cover_the_timer_driven_daemons():
    assert len(MIG.HARD_UNITS) == 6
    assert set(MIG.HARD_UNITS) == {
        "macp-runner-worker-default.service", "macp-orchestrator.service",
        "macp-orchestrator.timer", "claude-remote-control.service",
        "claude-auth-watch.service", "claude-auth-watch.timer"}
    assert set(MIG.ADVISORY_UNITS) == {
        "macp-maintenance.timer", "macp-planner.timer", "claude-upgrade.timer",
        "claude-remote-control-watchdog.timer"}
    # the resume line starts the runner last
    assert MIG._resume_hint().split()[-1] == "macp-runner-worker-default.service"


# ---- AC3 / AC5: apply --------------------------------------------------------------

def test_apply_migrates_everything(roots, legacy_tree, capsys, monkeypatch):
    fake_systemctl(monkeypatch)
    before = snapshot(roots.tmp)
    legacy_files = sum(1 for k, v in before.items() if v[0] == "file" and k.startswith("home/"))
    rc, out = _run(capsys, ["--apply"])
    assert rc == 0, out
    legacy, legacy_json = legacy_tree.legacy, legacy_tree.legacy_json
    acct, acct_json, shared = legacy_tree.acct, legacy_tree.acct_json, roots.shared
    assert "migration complete" in out and "daemons: none of the hard units is active" in out

    # shared entries are real on the shared side
    for entry in ("projects", "settings.json", "commands", "hooks"):
        assert (shared / entry).exists() and not (shared / entry).is_symlink(), entry
    assert (shared / "projects" / "-tmp-proj" / f"{SID}.jsonl").is_file()
    assert (shared / "commands" / "x.md").is_file() and (shared / "hooks" / "stop.sh").is_file()
    # the account dir carries the nine absolute links; the layout created the
    # shared dirs the legacy dir never had
    assert ACC.layout_drift(acct) == []
    for entry in ACC.SHARED_ENTRIES:
        assert os.readlink(acct / entry) == str(shared / entry), entry
    for d in ("skills", "plugins", "agents"):
        assert (shared / d).is_dir() and not (shared / d).is_symlink(), d
    assert (acct / "settings.local.json").is_symlink() and not (shared / "settings.local.json").exists()
    # per-account files live in the account dir only, modes kept
    for name in (".credentials.json", "automation-token"):
        p = acct / name
        assert p.is_file() and not p.is_symlink(), name
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, name
        assert not (shared / name).exists(), name
    assert acct_json.is_file() and not acct_json.is_symlink()
    assert (acct / "statsig" / "cache.bin").is_file()
    assert stat.S_IMODE(os.lstat(acct).st_mode) == 0o700
    # the legacy locations are symlinks to the account side ...
    assert legacy.is_symlink() and os.readlink(legacy) == str(acct)
    assert legacy_json.is_symlink() and os.readlink(legacy_json) == str(acct_json)
    # ... so a consumer that still spells the legacy path keeps resolving (design (b))
    assert (legacy / ".credentials.json").is_file()
    assert (legacy / "automation-token").read_text(encoding="utf-8") == FAKE_TOKEN + "\n"
    assert (legacy / "projects" / "-tmp-proj" / f"{SID}.jsonl").is_file()
    assert legacy_json.read_text(encoding="utf-8") == acct_json.read_text(encoding="utf-8")
    # --resume continuity through the account dir
    t = stages.transcript_path(SID, env={"CLAUDE_CONFIG_DIR": str(acct)})
    assert t is not None and t.is_relative_to(acct)
    assert t.resolve() == (shared / "projects" / "-tmp-proj" / f"{SID}.jsonl").resolve()
    # memory links: relative -> absolute, absolute untouched, dangling untouched
    mem = shared / "projects" / "-tmp-proj" / "memory"
    assert os.path.isabs(os.readlink(mem))
    assert mem.resolve() == legacy_tree.vault_mem.resolve()
    assert (mem / "note.md").read_text(encoding="utf-8") == "# note\n"
    assert (acct / "projects" / "-tmp-proj" / "memory" / "note.md").is_file()
    assert os.readlink(shared / "projects" / "-tmp-abs" / "memory") == str(legacy_tree.vault_mem)
    assert os.readlink(shared / "projects" / "-tmp-gone" / "memory") == "../../../nowhere/memory"
    assert "dangling" in out
    assert not list(shared.rglob("*.macp-tmp"))
    # profile: extended, never replaced
    text = legacy_tree.profile_path.read_text(encoding="utf-8")
    assert 'active_account = "school"' in text and "priority = 10" in text
    assert f'claude_config_dir = "{acct}"' in text and "daily_run_budget = 0" in text
    prof = C.load_profile("default")
    assert prof.daily_run_budget == 0 and prof.active_account == "school"
    assert [(a.name, a.priority, a.claude_config_dir) for a in prof.accounts] == \
        [("school", 10, str(acct))]
    # manifest: exactly one, every step done, order as specified, no secret
    manifests = _manifests(roots)
    assert len(manifests) == 1
    mtext = manifests[0].read_text(encoding="utf-8")
    m = json.loads(mtext)
    assert m["version"] == 1 and m["account"] == "school" and m["status"] == "ready"
    assert m["legacy_dir"] == str(legacy) and m["account_dir"] == str(acct)
    assert m["profile_before"] == legacy_tree.profile_text
    assert m["legacy_dir_mode"] == 0o700 and m["legacy_claude_json_present"] is True
    assert all(e["done"] is True for e in m["entries"])
    assert [e["op"] for e in m["entries"]] == [
        "mkdir", "move", "move", "move", "move", "layout", "move", "move", "move", "move",
        "rmdir-legacy", "symlink", "symlink", "repoint", "repoint", "profile"]
    moves = [(e["src"], e["dst"]) for e in m["entries"] if e["op"] == "move"]
    assert moves[:4] == [(str(legacy / x), str(shared / x))
                         for x in ("projects", "settings.json", "commands", "hooks")]
    assert moves[4:] == [(str(legacy / x), str(acct / x))
                         for x in (".credentials.json", "automation-token", "statsig")] + \
        [(str(legacy_json), str(acct_json))]
    tok = next(e for e in m["entries"] if e.get("src") == str(legacy / "automation-token"))
    assert tok["kind"] == "file" and tok["bytes"] == len(FAKE_TOKEN) + 1
    rep = [e for e in m["entries"] if e["op"] == "repoint"]
    assert rep[0]["skipped"] == "dangling" and "skipped" not in rep[1]
    assert rep[1]["old_target"] == os.path.relpath(legacy_tree.vault_mem, legacy / "projects" / "-tmp-proj")
    assert rep[1]["new_target"] == str(legacy_tree.vault_mem.resolve())
    assert not (manifests[0].parent / "manifest.json.tmp").exists()
    _assert_no_secret(mtext)
    _assert_no_secret(out)
    # nothing was deleted: every legacy file is now under shared/ or the account dir
    after = snapshot(roots.tmp)
    moved = sum(1 for k, v in after.items()
                if v[0] == "file" and (k.startswith("shared/") or k.startswith("accounts/school/")))
    assert moved == legacy_files == 8
    assert (roots.tmp / "vault" / "Memory" / "claude" / "vault" / "note.md").is_file()


def test_already_migrated_dry_run_and_apply_rc0(roots, legacy_tree, capsys, monkeypatch):
    fake_systemctl(monkeypatch)
    assert _run(capsys, ["--apply"])[0] == 0
    after = (snapshot(roots.accounts), snapshot(roots.shared), snapshot(roots.home))
    rc, out = _run(capsys, [])
    assert rc == 0
    assert f"already migrated: {legacy_tree.legacy} -> {legacy_tree.acct}" in out
    assert f"already migrated: {legacy_tree.legacy_json} -> {legacy_tree.acct_json}" in out
    assert "plan (" not in out
    rc, out = _run(capsys, ["--apply"])
    assert rc == 0 and "already migrated" in out
    assert (snapshot(roots.accounts), snapshot(roots.shared), snapshot(roots.home)) == after
    assert len(_manifests(roots)) == 1


def test_non_linux_platform_warns_keychain(roots, legacy_tree, capsys, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    rc, out = _run(capsys, [])
    assert rc == 0 and "Keychain" in out and "platform 'darwin' is not linux" in out
    fake_systemctl(monkeypatch)
    rc, out = _run(capsys, ["--apply"])
    assert rc == 0 and "Keychain" in out
    assert json.loads(_manifests(roots)[0].read_text(encoding="utf-8"))["platform"] == "darwin"


def test_profile_update_path_keeps_other_accounts(roots, legacy_tree, capsys, monkeypatch):
    legacy_tree.profile_path.write_text(
        'daily_run_budget = 0\nactive_account = "dev"\n\n'
        f'[[account]]\nname = "school"\nagent = "claude"\nclaude_config_dir = "{roots.tmp / "old"}"\n\n'
        f'[[account]]\nname = "dev"\nagent = "claude"\nclaude_config_dir = "{roots.tmp / "dev"}"\n'
        'priority = 20\n', encoding="utf-8")
    rc, out = _run(capsys, [])
    assert rc == 0 and "update account 'school'" in out and "daily_run_budget = 0 preserved" in out
    fake_systemctl(monkeypatch)
    rc, out = _run(capsys, ["--apply"])
    assert rc == 0
    prof = C.load_profile("default")
    assert [(a.name, a.priority, a.claude_config_dir) for a in prof.accounts] == \
        [("school", 10, str(legacy_tree.acct)), ("dev", 20, str(roots.tmp / "dev"))]
    assert prof.active_account == "school" and prof.daily_run_budget == 0


def test_profile_created_when_absent(roots, legacy_tree, capsys, monkeypatch):
    legacy_tree.profile_path.unlink()
    legacy_tree.profile_path.parent.rmdir()
    rc, out = _run(capsys, [])
    assert rc == 0 and "create account 'school'" in out
    assert "does not exist" in out and "daily_run_budget = 40" in out
    fake_systemctl(monkeypatch)
    rc, out = _run(capsys, ["--apply"])
    assert rc == 0 and legacy_tree.profile_path.is_file()
    prof = C.load_profile("default")
    assert prof.daily_run_budget == 40 and prof.active_account == "school"
    assert [a.name for a in prof.accounts] == ["school"]
    assert json.loads(_manifests(roots)[0].read_text(encoding="utf-8"))["profile_before"] is None


# ---- AC4: rollback ----------------------------------------------------------------

def test_rollback_restores_original_tree(roots, legacy_tree, capsys, monkeypatch):
    fake_systemctl(monkeypatch)
    before_home = snapshot(roots.home)
    before_vault = snapshot(roots.tmp / "vault")
    assert _run(capsys, ["--apply"])[0] == 0
    manifest = _manifests(roots)[0]
    rc, out = _run(capsys, ["--rollback", str(manifest), "--i-stopped-daemons"])
    assert rc == 0, out
    assert "rollback complete" in out
    # byte-for-byte the tree we started from (kinds, link targets, sizes, modes)
    assert snapshot(roots.home) == before_home
    assert snapshot(roots.tmp / "vault") == before_vault
    legacy = legacy_tree.legacy
    assert legacy.is_dir() and not legacy.is_symlink()
    assert stat.S_IMODE(os.lstat(legacy).st_mode) == 0o700
    assert legacy_tree.legacy_json.is_file() and not legacy_tree.legacy_json.is_symlink()
    assert not os.path.isabs(os.readlink(legacy / "projects" / "-tmp-proj" / "memory"))
    assert (legacy / "projects" / "-tmp-proj" / "memory" / "note.md").is_file()
    assert (legacy / "automation-token").read_text(encoding="utf-8") == FAKE_TOKEN + "\n"
    assert not legacy_tree.acct.exists() and not legacy_tree.acct.is_symlink()
    assert not roots.shared.exists()
    assert [p.name for p in roots.accounts.iterdir()] == [manifest.parent.name]
    m = json.loads(manifest.read_text(encoding="utf-8"))
    assert all(e["done"] is False for e in m["entries"])
    # the profile is left as the migration wrote it — restoring it is the human's step
    text = legacy_tree.profile_path.read_text(encoding="utf-8")
    assert 'active_account = "school"' in text
    assert m["profile_before"] == legacy_tree.profile_text
    assert f"profile: {legacy_tree.profile_path} is left as written" in out
    assert "profile_before" in out
    _assert_no_secret(out)
    # a second rollback has nothing left to do
    rc, out = _run(capsys, ["--rollback", str(manifest)])
    assert rc == 0 and "nothing to roll back" in out
    assert snapshot(roots.home) == before_home
    # and the dry-run plans the migration afresh (the profile already names school)
    rc, out = _run(capsys, [])
    assert rc == 0 and "dry-run" in out and "update account 'school'" in out
    assert snapshot(roots.home) == before_home


def test_rollback_refuses_when_daemons_active(roots, legacy_tree, capsys, monkeypatch):
    fake_systemctl(monkeypatch)
    assert _run(capsys, ["--apply"])[0] == 0
    manifest = _manifests(roots)[0]
    after = (snapshot(roots.home), snapshot(roots.shared), snapshot(roots.accounts))
    fake_systemctl(monkeypatch, active={"claude-auth-watch.timer"})
    rc, out = _run(capsys, ["--rollback", str(manifest)])
    assert rc == 3
    assert "refusing to roll back: active units: claude-auth-watch.timer" in out
    assert "--i-stopped-daemons" in out
    assert (snapshot(roots.home), snapshot(roots.shared), snapshot(roots.accounts)) == after
    assert all(e["done"] for e in json.loads(manifest.read_text(encoding="utf-8"))["entries"])
    # a bogus manifest is rejected before anything else
    bogus = roots.tmp / "bogus.json"
    bogus.write_text('{"version": 99}\n', encoding="utf-8")
    rc, out = _run(capsys, ["--rollback", str(bogus), "--i-stopped-daemons"])
    assert rc == 2 and "not a version-1 manifest" in out


def test_rollback_stops_when_link_replaced_by_file(roots, legacy_tree, capsys, monkeypatch):
    fake_systemctl(monkeypatch)
    assert _run(capsys, ["--apply"])[0] == 0
    manifest = _manifests(roots)[0]
    legacy_json = legacy_tree.legacy_json
    # the CLI's atomic rename-write turned the settings-file link into a plain file (§3 drift)
    legacy_json.unlink()
    legacy_json.write_text("{}\n", encoding="utf-8")
    rc, out = _run(capsys, ["--rollback", str(manifest)])
    assert rc == 2
    assert "no longer a symlink" in out and "by hand" in out and "manifest kept" in out
    m = json.loads(manifest.read_text(encoding="utf-8"))
    by_op = {}
    for e in m["entries"]:
        by_op.setdefault(e["op"], []).append(e)
    # the steps after the settings-file link are undone and marked so ...
    assert by_op["profile"][0]["done"] is False
    assert [e["done"] for e in by_op["repoint"]] == [False, False]
    assert not os.path.isabs(os.readlink(roots.shared / "projects" / "-tmp-proj" / "memory"))
    # ... the link step itself and everything before it are still done
    links = {e["path"]: e["done"] for e in by_op["symlink"]}
    assert links == {str(legacy_tree.legacy): True, str(legacy_json): True}
    assert legacy_tree.legacy.is_symlink()
    assert legacy_json.read_text(encoding="utf-8") == "{}\n"       # untouched
    assert legacy_tree.acct_json.is_file()
    # a human merges the plain file into the account copy and removes it; the
    # rollback then completes
    legacy_json.unlink()
    rc, out = _run(capsys, ["--rollback", str(manifest)])
    assert rc == 0 and "rollback complete" in out and "already gone" in out
    assert legacy_tree.legacy.is_dir() and not legacy_tree.legacy.is_symlink()
    assert legacy_json.is_file() and not legacy_json.is_symlink()
    assert not roots.shared.exists()
    _assert_no_secret(out)


def test_partial_apply_keeps_manifest_and_rollback_restores(roots, legacy_tree, capsys,
                                                             monkeypatch):
    fake_systemctl(monkeypatch)
    before = snapshot(roots.home)
    real_layout = ACC.create_account_layout

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(ACC, "create_account_layout", boom)
    rc, out = _run(capsys, ["--apply"])
    assert rc == 1
    assert f"error at step 6/16 (layout {legacy_tree.acct})" in out
    manifest = _manifests(roots)[0]
    assert f"manifest kept: {manifest}" in out and f"--rollback {manifest}" in out
    m = json.loads(manifest.read_text(encoding="utf-8"))
    assert [e["done"] for e in m["entries"]] == [True] * 5 + [False] * 11
    # the four shared entries moved, nothing else — and nothing was deleted
    assert (roots.shared / "projects" / "-tmp-proj" / f"{SID}.jsonl").is_file()
    assert not (legacy_tree.legacy / "projects").exists()
    assert (legacy_tree.legacy / "automation-token").is_file()
    assert legacy_tree.legacy.is_dir() and not legacy_tree.legacy.is_symlink()
    assert not legacy_tree.acct.exists()
    _assert_no_secret(out)

    monkeypatch.setattr(ACC, "create_account_layout", real_layout)
    rc, out = _run(capsys, ["--rollback", str(manifest)])
    assert rc == 0, out
    assert snapshot(roots.home) == before
    assert not roots.shared.exists()
    assert all(e["done"] is False
               for e in json.loads(manifest.read_text(encoding="utf-8"))["entries"])
    # the profile step never ran, so the profile is untouched
    assert legacy_tree.profile_path.read_text(encoding="utf-8") == legacy_tree.profile_text


# ---- structure: stdlib-only script without legacy-path literals; docs §7 --------

def test_script_is_stdlib_and_free_of_legacy_literals():
    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    absolute: set[str] = set()
    from_mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            absolute.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert not node.level, "a script has no relative imports"
            absolute.add((node.module or "").split(".")[0])
            from_mods.add(node.module or "")
    assert absolute, "no imports parsed"
    assert absolute <= set(sys.stdlib_module_names) | {"todo_system"}, absolute
    assert {m for m in from_mods if m.startswith("todo_system")} == \
        {"todo_system", "todo_system.macp"}
    # the legacy paths are spelled only through the accounts module — leaf ③'s
    # ratchet scans scripts/**/*.py docstrings included
    for literal in ("~/.claude", "Path.home()", "$HOME", 'expanduser("~', ".claude.json"):
        assert literal not in source, literal
    # moves only: no shutil, no rmtree, no os.rename; unlink is confined to the
    # symlinks the script made (repoint swap + rollback)
    assert "shutil" not in source and "rmtree" not in source and "os.rename(" not in source
    assert source.count("os.replace(") >= 4
    assert "sys.exit(main())" in source
    # the daemon units come from daemonctl's constants, never from a literal prefix
    assert "unit_name(" in source and "SECONDBRAIN_PREFIX" in source and "LABEL_PREFIX" in source


# docs/claude-accounts.md is vault-owned (not in the engine export); its §7 coverage is not pinned here.
