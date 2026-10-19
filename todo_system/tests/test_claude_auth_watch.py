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


def test_last_startup_error_sees_the_prefixless_bridge_404(monkeypatch, tmp_path):
    log = tmp_path / "claude-remote-control.log"
    log.write_text(
        "[2026-10-19T00:38:23Z] starting claude remote-control --name x\n"
        "Remote Control environments are not available for your account.\n"
        "Exiting in about 69 seconds.\n")
    monkeypatch.setattr(A, "RC_LOG_PATHS", [log])
    assert A.last_startup_error() == "Remote Control environments are not available for your account."
