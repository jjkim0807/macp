"""Render test for scripts/claude_auth_watch.py's kakao text composer.

scripts/ has no __init__.py, so the module is loaded by path via importlib.
`_compose_kakao_text` is pure (no subprocess), so this never reaches the real
kakao channel ([[tests-reach-real-alert-sender]]). The auth alert's whole reason
to exist is the remedy ("터미널에서 claude 실행 후 /login") — the old
`f"🔐 {title}\\n{message}"[:179]` could blind-cut it; this pins that it now fits.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

from todo_system import cli
from todo_system import ops_alerts as oa
from todo_system.kakao_format import KAKAO_TEXT_LIMIT

VAULT = Path(__file__).resolve().parents[2]
_SRC = VAULT / "scripts" / "claude_auth_watch.py"


def _load():
    spec = importlib.util.spec_from_file_location("claude_auth_watch", _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


A = _load()


def test_auth_alert_keeps_the_login_remedy_within_budget():
    title = "Claude Code 인증 이상"
    message = ("refreshTokenExpiresAt가 지났고 accessToken이 비었습니다 — "
               "터미널에서 claude 실행 후 /login")
    text = A._compose_kakao_text(title, message)
    assert len(text) <= KAKAO_TEXT_LIMIT
    assert text.splitlines()[0].startswith("🔐 Claude Code 인증 이상")
    assert "/login" in text                     # the remedy survives (short input, whole)


def test_auth_alert_worst_input_stays_bounded():
    text = A._compose_kakao_text("인증 이상", "problem detail " * 40)
    assert len(text) <= KAKAO_TEXT_LIMIT
    assert text.splitlines()[0].startswith("🔐 인증 이상")


# --- Remote Control permanent-error class (2026-10-17 incident) -----------------

def _utc(*ymdhms) -> float:
    return dt.datetime(*ymdhms, tzinfo=dt.timezone.utc).timestamp()


def test_permanent_rc_errors_are_the_relogin_class():
    # CLI 2.1.302 texts seen in claude-remote-control.log on 2026-10-17~19.
    assert A.is_permanent_rc_error(
        "Error: Remote Control isn't enabled for this account. If you recently changed plans, "
        "run `claude auth logout` then `claude auth login` to refresh your entitlements")
    assert A.is_permanent_rc_error("Remote Control environments are not available for your account.")
    assert A.is_permanent_rc_error(
        "Error: Remote Control requires a full-scope login token. Long-lived tokens ...")
    # 2.1.303, 2026-10-19: no credential in the store the CLI was pointed at.
    assert A.is_permanent_rc_error("Error: You must be logged in to use Remote Control.")


def test_transient_rc_errors_keep_the_keepalive_loop():
    assert not A.is_permanent_rc_error(
        "Error: Couldn't verify Remote Control eligibility — the feature-flag service was "
        "unreachable (offline or blocked). Retry, or run with `--debug`")
    assert not A.is_permanent_rc_error("Error: Could not reach the server to look up session")
    assert not A.is_permanent_rc_error(None)
    assert not A.is_permanent_rc_error("")


def test_plan_recreated_after_login_is_flagged():
    login = _utc(2026, 5, 18, 1, 30, 48)      # the old Keychain cdat
    assert A._plan_newer_than_login("2026-10-16T03:46:18.492295Z", login)
    assert not A._plan_newer_than_login("2026-05-01T00:00:00Z", login)
    assert not A._plan_newer_than_login("2026-10-16T03:46:18Z", _utc(2026, 10, 19, 0, 46, 45))
    assert not A._plan_newer_than_login(None, login)
    assert not A._plan_newer_than_login("2026-10-16T03:46:18Z", None)
    assert not A._plan_newer_than_login("not a date", login)


def test_plan_change_problem_carries_the_remedy(monkeypatch, tmp_path):
    cfg = tmp_path / ".claude.json"
    cfg.write_text('{"oauthAccount": {"subscriptionCreatedAt": "2026-10-16T03:46:18Z"}}')

    class Acc:
        @staticmethod
        def claude_json_path(config_dir):
            return cfg

    monkeypatch.setattr(A, "_acc", lambda: Acc)
    monkeypatch.setattr(A, "credential_created_at", lambda config_dir: _utc(2026, 5, 18, 1, 30, 48))
    problems, notes = [], []
    A.check_plan_change(None, tmp_path, problems, notes)
    assert len(problems) == 1 and not notes
    assert "2026-10-16" in problems[0] and "2026-05-18" in problems[0]
    assert "claude auth logout && claude auth login" in problems[0]

    # Relogin after the plan change clears it.
    monkeypatch.setattr(A, "credential_created_at", lambda config_dir: _utc(2026, 10, 19, 0, 46, 45))
    problems, notes = [], []
    A.check_plan_change(None, tmp_path, problems, notes)
    assert not problems and notes == ["[legacy] login newer than plan"]


def test_plan_change_check_skips_without_a_login_stamp(monkeypatch, tmp_path):
    monkeypatch.setattr(A, "credential_created_at", lambda config_dir: None)
    problems, notes = [], []
    A.check_plan_change("hq", tmp_path, problems, notes)
    assert not problems and notes[0].startswith("[hq] plan-vs-login check n/a")


# --- ops human task wiring (ops-claude-auth-<account>) -------------------------

def _creds(exp_ms):
    return {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r",
                              "refreshTokenExpiresAt": exp_ms}}


def _ms_from_now(days):
    return dt.datetime.now(dt.timezone.utc).timestamp() * 1000 + days * 86_400_000


@pytest.fixture
def auth_ops_task(tmp_path, monkeypatch):
    """ops_alerts at a readable tmp vault + frozen day; account machinery stubbed
    so the test is only about the expiry→ops path. Returns the acct-a task path."""
    root = tmp_path / "ops-vault"
    monkeypatch.setattr(oa, "VAULT_ROOT", root)
    monkeypatch.setattr(oa, "_today", lambda: "2049-01-10")
    monkeypatch.setattr(A, "_mark_auth", lambda *a, **k: None)
    monkeypatch.setattr(A, "_check_automation_token", lambda *a, **k: None)
    return root / "Todo" / "items" / "ops-claude-auth-acct-a.md"


def test_expired_refresh_token_opens_the_account_auth_task(auth_ops_task, monkeypatch, tmp_path):
    monkeypatch.setattr(A, "read_credentials", lambda cd: _creds(_ms_from_now(-2)))
    A.check_credential("acct-a", tmp_path, [], [])
    assert auth_ops_task.is_file()
    fm, _ = cli.parse_frontmatter(auth_ops_task.read_text(encoding="utf-8"))
    assert fm["status"] == "open"
    assert fm["worker_mode"] == "manual"
    assert fm["due"] == "2049-01-10"
    assert "ops" in fm["tags"]


def test_expiring_soon_refresh_token_opens_the_account_auth_task(auth_ops_task, monkeypatch, tmp_path):
    monkeypatch.setattr(A, "read_credentials", lambda cd: _creds(_ms_from_now(3)))   # < WARN_DAYS
    A.check_credential("acct-a", tmp_path, [], [])
    assert cli.parse_frontmatter(auth_ops_task.read_text(encoding="utf-8"))[0]["status"] == "open"


def test_healthy_runway_resolves_the_account_auth_task(auth_ops_task, monkeypatch, tmp_path):
    monkeypatch.setattr(A, "read_credentials", lambda cd: _creds(_ms_from_now(-2)))
    A.check_credential("acct-a", tmp_path, [], [])                                   # open it
    assert cli.parse_frontmatter(auth_ops_task.read_text(encoding="utf-8"))[0]["status"] == "open"
    monkeypatch.setattr(A, "read_credentials", lambda cd: _creds(_ms_from_now(30)))  # > WARN_DAYS
    A.check_credential("acct-a", tmp_path, [], [])                                   # resolve it
    assert cli.parse_frontmatter(auth_ops_task.read_text(encoding="utf-8"))[0]["status"] == "done"


def test_healthy_runway_with_no_task_is_a_silent_noop(auth_ops_task, monkeypatch, tmp_path):
    monkeypatch.setattr(A, "read_credentials", lambda cd: _creds(_ms_from_now(30)))
    A.check_credential("acct-a", tmp_path, [], [])
    assert not auth_ops_task.exists()


def test_ops_import_is_stdlib_and_paths_only():
    # The Mac auth-watch imports this under bare /usr/bin/python3 (3.9): ops_alerts
    # must drag in nothing beyond stdlib + `from . import paths`.
    import ast
    src = Path(oa.__file__).read_text(encoding="utf-8")
    abs_mods, rel_mods = set(), set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            if node.level:                       # relative: `from . import paths`
                rel_mods.update(n.name for n in node.names)
            else:
                abs_mods.add((node.module or "").split(".")[0])
        elif isinstance(node, ast.Import):
            for n in node.names:
                abs_mods.add(n.name.split(".")[0])
    assert rel_mods == {"paths"}, rel_mods
    assert abs_mods <= {"__future__", "datetime", "os", "re", "pathlib"}, abs_mods


def test_last_startup_error_sees_the_prefixless_bridge_404(monkeypatch, tmp_path):
    log = tmp_path / "claude-remote-control.log"
    log.write_text(
        "[2026-10-19T00:38:23Z] starting claude remote-control --name x\n"
        "Remote Control environments are not available for your account.\n"
        "Exiting in about 69 seconds.\n")
    monkeypatch.setattr(A, "RC_LOG_PATHS", [log])
    assert A.last_startup_error() == "Remote Control environments are not available for your account."
