"""Browser login watch: transitions, unknown streak, interval gate, fail-open.

No node is needed: every test injects the subprocess runner. The conftest
fixture already points MACP_BROWSER_DIR / MACP_BROWSER_WATCH_DIR at tmp_path;
tests that want the probe to "exist" create the script file there.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from todo_system import browser_watch as bw
from todo_system import worker

T0 = 1_000_000.0
HOUR = 3600.0


@pytest.fixture
def probe(tmp_path, monkeypatch):
    d = tmp_path / "browser"
    d.mkdir()
    (d / "login-check.mjs").write_text("// stub\n")
    monkeypatch.setenv("MACP_BROWSER_DIR", str(d))
    monkeypatch.delenv("MACP_BROWSER_WATCH_INTERVAL_S", raising=False)
    monkeypatch.delenv("MACP_HQ_SSH_HINT", raising=False)
    return d / "login-check.mjs"


def fake_run(stdout="", returncode=0, calls=None):
    def _run(cmd, **kwargs):
        if calls is not None:
            calls.append(cmd)
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)
    return _run


def state_run(state, calls=None):
    return fake_run(json.dumps({"state": state, "checked_at": "x"}), 0, calls)


def raising_run(exc):
    def _run(cmd, **kwargs):
        raise exc
    return _run


def check(state_or_run, now, sent):
    run = state_or_run if callable(state_or_run) else state_run(state_or_run)
    return bw.check_login_watch(now, notify=sent.append, run=run)


# -- decide (pure) -----------------------------------------------------------

def _seq(observations, start=None):
    st = start or bw._default_state()
    kinds = []
    for obs in observations:
        st, kind = bw.decide(st, obs)
        kinds.append(kind)
    return st, kinds


def test_logged_in_to_logged_out_alerts_once_then_recovers_once():
    _, kinds = _seq(["logged_in", "logged_out", "logged_out", "logged_out",
                     "logged_in", "logged_in"])
    assert kinds == [None, "alert:logged_out", None, None, "recover", None]


def test_no_chrome_alerts_and_move_between_bad_states_alerts_once():
    _, kinds = _seq(["no_chrome", "no_chrome", "logged_out", "logged_out"])
    assert kinds == ["alert:no_chrome", None, "alert:logged_out", None]


def test_unknown_alerts_only_on_third_in_a_row():
    st, kinds = _seq(["unknown"] * 5 + ["logged_in"])
    assert kinds == [None, None, "alert:unknown", None, None, "recover"]
    assert st["unknown_streak"] == 0


def test_unknown_twice_then_logged_in_is_silent_and_resets_streak():
    st, kinds = _seq(["unknown", "unknown", "logged_in", "unknown", "unknown"])
    assert kinds == [None] * 5
    assert st["unknown_streak"] == 2


def test_unknown_twice_then_logged_out_alerts_as_logged_out():
    st, kinds = _seq(["unknown", "unknown", "logged_out"])
    assert kinds == [None, None, "alert:logged_out"]
    assert st["unknown_streak"] == 0


# -- check_login_watch: first run, interval gate, skip -----------------------

def test_first_run_bad_state_alerts_once(probe):
    sent = []
    assert check("logged_out", T0, sent)
    assert len(sent) == 1
    assert check("logged_out", T0 + HOUR + 1, sent) is None
    assert len(sent) == 1


def test_first_run_logged_in_is_silent(probe):
    sent = []
    assert check("logged_in", T0, sent) is None
    assert sent == []
    assert bw.state_path().is_file()


def test_interval_gate_skips_the_probe(probe):
    calls, sent = [], []
    check(state_run("logged_in", calls), T0, sent)
    check(state_run("logged_in", calls), T0 + 10, sent)
    assert len(calls) == 1
    check(state_run("logged_in", calls), T0 + HOUR + 1, sent)
    assert len(calls) == 2


def test_interval_is_configurable(probe, monkeypatch):
    monkeypatch.setenv("MACP_BROWSER_WATCH_INTERVAL_S", "60")
    calls, sent = [], []
    check(state_run("logged_in", calls), T0, sent)
    check(state_run("logged_in", calls), T0 + 61, sent)
    assert len(calls) == 2


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5"])
def test_bad_interval_falls_back_to_default(monkeypatch, raw):
    monkeypatch.setenv("MACP_BROWSER_WATCH_INTERVAL_S", raw)
    assert bw.interval_s() == bw.DEFAULT_INTERVAL_S


def test_clock_stepped_back_does_not_suppress(probe):
    calls, sent = [], []
    check(state_run("logged_in", calls), T0, sent)
    check(state_run("logged_in", calls), T0 - 100, sent)
    assert len(calls) == 2


def test_missing_script_skips_silently(tmp_path, monkeypatch):
    monkeypatch.setenv("MACP_BROWSER_DIR", str(tmp_path / "absent"))
    calls, sent = [], []
    assert check(state_run("logged_out", calls), T0, sent) is None
    assert calls == [] and sent == []
    assert not bw.state_path().exists()


def test_unknown_streak_survives_across_ticks(probe):
    sent = []
    for i in range(2):
        assert check("unknown", T0 + i * (HOUR + 1), sent) is None
    assert check("unknown", T0 + 2 * (HOUR + 1), sent)
    assert check("unknown", T0 + 3 * (HOUR + 1), sent) is None
    assert len(sent) == 1
    assert check("logged_in", T0 + 4 * (HOUR + 1), sent)
    assert "복구" in sent[-1]


# -- parsing -----------------------------------------------------------------

def test_parse_uses_last_stdout_line(probe):
    out = 'noise\n{"state":"logged_in"}\n{"state":"no_chrome","checked_at":"x"}\n\n'
    assert bw.run_login_check(probe, run=fake_run(out, 0)) == "no_chrome"


@pytest.mark.parametrize("rc,expected", [(0, "logged_in"), (1, "logged_out"),
                                         (2, "no_chrome"), (3, "unknown"), (7, "unknown")])
def test_garbage_stdout_falls_back_to_exit_code(probe, rc, expected):
    assert bw.run_login_check(probe, run=fake_run("not json", rc)) == expected


def test_json_state_wins_over_exit_code(probe):
    assert bw.run_login_check(probe, run=fake_run('{"state":"logged_in"}', 1)) == "logged_in"


def test_out_of_contract_state_is_unknown(probe):
    assert bw.run_login_check(probe, run=fake_run('{"state":"weird"}', 0)) == "unknown"


def test_timeout_counts_as_unknown(probe):
    run = raising_run(subprocess.TimeoutExpired(["node"], bw.RUN_TIMEOUT_S))
    assert bw.run_login_check(probe, run=run) == "unknown"
    check(run, T0, [])
    assert json.loads(bw.state_path().read_text())["unknown_streak"] == 1


def test_probe_invoked_with_node_and_timeout(probe, monkeypatch):
    monkeypatch.setenv("MACP_BROWSER_NODE", "/opt/node/bin/node")
    seen = {}

    def _run(cmd, **kwargs):
        seen["cmd"], seen["kw"] = cmd, kwargs
        return SimpleNamespace(stdout='{"state":"logged_in"}', stderr="", returncode=0)

    bw.run_login_check(probe, run=_run)
    assert seen["cmd"] == ["/opt/node/bin/node", str(probe)]
    assert seen["kw"]["timeout"] > 60


# -- fail-open (injection) ---------------------------------------------------

def test_run_raising_never_escapes(probe):
    assert bw.check_login_watch(T0, notify=[].append,
                                run=raising_run(RuntimeError("boom"))) is None


def test_notify_raising_never_escapes_and_state_is_saved(probe):
    def bad_notify(msg):
        raise RuntimeError("kakao down")

    msg = bw.check_login_watch(T0, notify=bad_notify, run=state_run("logged_out"))
    assert msg
    assert json.loads(bw.state_path().read_text())["notified"] == "logged_out"
    sent = []
    assert check("logged_out", T0 + HOUR + 1, sent) is None
    assert sent == []


def test_node_missing_logs_only(probe, capsys):
    sent = []
    check(raising_run(FileNotFoundError("node")), T0, sent)
    assert sent == []
    st = json.loads(bw.state_path().read_text())
    assert st["unknown_streak"] == 0 and st["notified"] == "logged_in"
    assert st["last_checked_at"] == T0
    assert "node binary not found" in capsys.readouterr().out


def test_cmd_maintenance_continues_when_watch_raises(monkeypatch):
    called = []

    def rec(name, ret):
        def _f(*a, **k):
            called.append(name)
            return ret
        return _f

    monkeypatch.setattr(worker, "IS_REMOTE_HOST", False)
    monkeypatch.setattr(worker, "autoclose_stale_reviews_local", rec("autoclose", 0))
    monkeypatch.setattr(worker, "sweep_remote_completed_runs", rec("sweep", 0))
    monkeypatch.setattr(worker, "sync_vault_state", rec("sync", 0))
    monkeypatch.setattr(worker, "rotate_logs", rec("rotate", []))
    monkeypatch.setattr(worker, "check_unapplied_migrations", rec("migrations", []))
    monkeypatch.setattr(worker, "bump_vault_engine_pin", rec("vault-pin", []))
    monkeypatch.setattr(worker, "sync_wrappers", rec("wrappers", []))
    monkeypatch.setattr(worker, "restart_stale_daemons", rec("restart", []))
    monkeypatch.setattr(worker, "check_dispatch_silence", rec("silence", None))
    monkeypatch.setattr(worker, "check_work_starvation", rec("starvation", None))

    def boom(*a, **k):
        raise RuntimeError("watch exploded")

    monkeypatch.setattr(worker.browser_watch, "check_login_watch", boom)
    assert worker.cmd_maintenance() == 1
    assert called == ["autoclose", "sweep", "sync", "rotate", "migrations",
                      "vault-pin", "wrappers", "restart", "silence", "starvation"]


def test_cmd_maintenance_pages_through_kakao_notify(probe, monkeypatch):
    for name in ("autoclose_stale_reviews_local", "sweep_remote_completed_runs",
                 "sync_vault_state"):
        monkeypatch.setattr(worker, name, lambda: 0)
    for name in ("rotate_logs", "check_unapplied_migrations", "restart_stale_daemons",
                 "sync_wrappers", "bump_vault_engine_pin"):
        monkeypatch.setattr(worker, name, lambda: [])
    for name in ("check_dispatch_silence", "check_work_starvation"):
        monkeypatch.setattr(worker, name, lambda: None)
    monkeypatch.setattr(worker, "IS_REMOTE_HOST", False)
    sent = []
    monkeypatch.setattr(worker, "kakao_notify", lambda msg, *a, **k: sent.append(msg))
    # check_login_watch's default `run` is bound at import; patch the probe call.
    real = bw.run_login_check
    monkeypatch.setattr(bw, "run_login_check",
                        lambda script, run=None: real(script, run=state_run("logged_out")))
    assert worker.cmd_maintenance() == 0
    assert len(sent) == 1 and "로그인 풀림" in sent[0]


# -- message content ---------------------------------------------------------

@pytest.mark.parametrize("state", ["logged_out", "no_chrome", "unknown"])
def test_alert_carries_relogin_steps(state):
    msg = bw.compose_login_alert(state)
    for needle in ("browser.sh start", "gcloud compute ssh",
                   "-L 6080:localhost:6080", "localhost:6080/vnc.html"):
        assert needle in msg


def test_ssh_hint_is_configurable(monkeypatch):
    monkeypatch.setenv("MACP_HQ_SSH_HINT", "hq-box --zone z1 --project p1")
    assert "gcloud compute ssh hq-box --zone z1 --project p1 -- -L" in bw.compose_login_alert("logged_out")


def test_alert_never_carries_probe_output_or_secrets(probe):
    secret_out = json.dumps({"state": "logged_out", "account": "someone@example.com",
                             "cookie": "SID=abc123secret", "token": "ya29.zzz"})
    sent = []
    check(fake_run(secret_out, 1), T0, sent)
    assert len(sent) == 1
    msg = sent[0]
    for leaked in ("someone@example.com", "SID=abc123secret", "ya29.zzz", "account"):
        assert leaked not in msg
    for word in ("cookie", "token", "password", "비번"):
        assert word not in msg.lower()
    assert "account" not in bw.state_path().read_text()


def test_recovery_message_says_no_action():
    assert "조치 불필요" in bw.compose_login_recovered()


# -- state file location / permissions / defaults ----------------------------

def test_state_file_is_private_and_under_configured_dir(probe, tmp_path, monkeypatch):
    d = tmp_path / "custom-state"
    monkeypatch.setenv("MACP_BROWSER_WATCH_DIR", str(d))
    check("logged_in", T0, [])
    p = d / "login_watch.json"
    assert p.is_file()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


def test_corrupt_state_file_falls_back_to_defaults(probe):
    bw.state_dir().mkdir(parents=True, exist_ok=True)
    bw.state_path().write_text("{not json")
    sent = []
    check("logged_out", T0, sent)
    assert len(sent) == 1


def test_defaults_are_home_relative(monkeypatch):
    for name in ("MACP_BROWSER_DIR", "MACP_BROWSER_WATCH_DIR", "MACP_BROWSER_NODE"):
        monkeypatch.delenv(name, raising=False)
    home = Path.home()
    assert bw.script_path() == home / ".config" / "macp-browser" / "login-check.mjs"
    assert bw.state_dir() == home / ".local" / "share" / "macp" / "browser"
    assert bw.node_bin() == "node"
    assert worker.ROOT not in bw.state_path().parents
