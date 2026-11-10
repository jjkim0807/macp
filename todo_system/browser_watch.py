"""Watch the HQ's logged-in browser profile and page when its login drops.

The macp-browser profile keeps sessions to consoles that have no API. A session
expires without telling anyone; the first sign used to be a task failing on a
login wall. The maintenance tick calls :func:`check_login_watch`, which runs the
profile's own probe at most once per interval and pages on state *transitions*
only.

Probe contract (``login-check.mjs``, separate repo): one line of JSON on stdout,
``{"state": ..., "account"?: ..., "checked_at": ...}``, with exit code
0 logged_in / 1 logged_out / 2 no_chrome / 3 unknown. The JSON ``state`` wins;
the exit code is the fallback when stdout does not parse. ``account`` is never
read, stored or sent, and stdout/stderr never reach a message.

Environment (read at call time):

  MACP_BROWSER_DIR               probe directory; default ~/.config/macp-browser.
                                 No ``login-check.mjs`` there → silent skip (the
                                 normal case on the Mac and remote hosts).
  MACP_BROWSER_NODE              node binary; default ``node``.
  MACP_BROWSER_WATCH_DIR         state directory (outside git); default
                                 ~/.local/share/macp/browser.
  MACP_BROWSER_WATCH_INTERVAL_S  seconds between probes; default 3600.
  MACP_HQ_SSH_HINT               ``gcloud compute ssh`` target in the re-login
                                 steps; default is a neutral placeholder.

Messages carry the re-login steps (start the browser, tunnel noVNC from the
Mac, log in by hand) so the page is actionable on its own. They carry nothing
the probe printed and no credential of any kind.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import ops_alerts
from .kakao_format import compose_alert

STATES = ("logged_in", "logged_out", "no_chrome", "unknown")
BAD = frozenset({"logged_out", "no_chrome", "unknown"})
UNKNOWN_ALERT_STREAK = 3
RUN_TIMEOUT_S = 90  # the probe's own watchdog is 60s
DEFAULT_INTERVAL_S = 3600.0
DEFAULT_SSH_HINT = "<hq-instance> --zone <zone> --project <project>"

_EXIT_STATES = {0: "logged_in", 1: "logged_out", 2: "no_chrome"}


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def browser_dir() -> Path:
    env = _env("MACP_BROWSER_DIR")
    return Path(env).expanduser() if env else Path.home() / ".config" / "macp-browser"


def script_path() -> Path:
    return browser_dir() / "login-check.mjs"


def node_bin() -> str:
    return _env("MACP_BROWSER_NODE") or "node"


def state_dir() -> Path:
    env = _env("MACP_BROWSER_WATCH_DIR")
    return Path(env).expanduser() if env else Path.home() / ".local" / "share" / "macp" / "browser"


def state_path() -> Path:
    return state_dir() / "login_watch.json"


def interval_s() -> float:
    try:
        value = float(_env("MACP_BROWSER_WATCH_INTERVAL_S") or DEFAULT_INTERVAL_S)
    except ValueError:
        return DEFAULT_INTERVAL_S
    return value if value > 0 else DEFAULT_INTERVAL_S


def ssh_hint() -> str:
    return _env("MACP_HQ_SSH_HINT") or DEFAULT_SSH_HINT


# -- probe -------------------------------------------------------------------

def _parse_stdout(stdout: str) -> str | None:
    """`state` from the last non-empty stdout line; None when it doesn't parse."""
    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    if not lines:
        return None
    try:
        data = json.loads(lines[-1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or "state" not in data:
        return None
    state = data.get("state")
    return state if state in STATES else "unknown"


def run_login_check(script: Path, *, run: Callable = subprocess.run) -> str | None:
    """One of STATES, or None when the node binary itself is missing.

    A missing node is a configuration problem, not a lost login, so it gets its
    own signal instead of counting toward the unknown streak.
    """
    try:
        proc = run([node_bin(), str(script)], capture_output=True, text=True,
                   timeout=RUN_TIMEOUT_S, check=False)
    except FileNotFoundError:
        return None
    except (subprocess.SubprocessError, OSError):
        return "unknown"
    state = _parse_stdout(proc.stdout)
    if state is not None:
        return state
    return _EXIT_STATES.get(proc.returncode, "unknown")


# -- state file --------------------------------------------------------------

def _default_state() -> dict:
    # No file yet: assume the last thing we told anyone was "logged in", so a
    # profile that is already broken at deploy time pages once.
    return {"last_checked_at": 0.0, "notified": "logged_in", "unknown_streak": 0}


def _read_state() -> dict:
    state = _default_state()
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return state
    if not isinstance(data, dict):
        return state
    try:
        state["last_checked_at"] = float(data.get("last_checked_at", 0.0))
    except (TypeError, ValueError):
        pass
    if data.get("notified") in STATES:
        state["notified"] = data["notified"]
    streak = data.get("unknown_streak")
    if isinstance(streak, int) and streak >= 0:
        state["unknown_streak"] = streak
    return state


def _write_state(state: dict) -> None:
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        p = state_path()
        tmp = p.with_name(p.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({k: state[k] for k in ("last_checked_at", "notified", "unknown_streak")}, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except OSError as exc:
        print(f"browser-watch: state write failed — {exc}")


# -- transitions -------------------------------------------------------------

def decide(prev: dict, observed: str) -> tuple[dict, str | None]:
    """Pure transition table: (new state, message kind or None).

    The kind is ``"recover"`` or ``"alert:<state>"``. The baseline is the last
    state we *paged* about, not the last one observed, so a bad state that
    persists stays quiet and a move between bad states (their fixes differ)
    pages once more.
    """
    new = dict(prev)
    notified = prev.get("notified", "logged_in")
    if observed == "logged_in":
        new["unknown_streak"] = 0
        if notified != "logged_in":
            new["notified"] = "logged_in"
            return new, "recover"
        return new, None
    if observed in ("logged_out", "no_chrome"):
        new["unknown_streak"] = 0
        if observed != notified:
            new["notified"] = observed
            return new, f"alert:{observed}"
        return new, None
    # unknown: one or two in a row is a network blip, the third is a page.
    streak = int(prev.get("unknown_streak", 0)) + 1
    new["unknown_streak"] = streak
    if streak >= UNKNOWN_ALERT_STREAK and notified != "unknown":
        new["notified"] = "unknown"
        return new, "alert:unknown"
    return new, None


# -- messages ----------------------------------------------------------------

_HEADLINES = {
    "logged_out": "🌐⚠️ 본부 브라우저 로그인 풀림 — 재로그인이 필요합니다",
    "no_chrome": "🌐⚠️ 본부 브라우저 Chrome 미기동 — 기동 후 로그인 확인이 필요합니다",
    "unknown": "🌐⚠️ 본부 브라우저 로그인 판정 불가 3회 연속 — 확인이 필요합니다",
}


def compose_login_alert(state: str) -> str:
    headline = _HEADLINES.get(state, _HEADLINES["unknown"])
    return compose_alert(headline, lines=[
        "1) 본부에서: browser.sh start",
        f"2) 맥에서: gcloud compute ssh {ssh_hint()} -- -L 6080:localhost:6080",
        "3) 맥 브라우저로 http://localhost:6080/vnc.html 열고 직접 로그인",
    ])


def compose_login_recovered() -> str:
    return compose_alert("✅ 본부 브라우저 로그인 복구 — 조치 불필요")


def _compose(kind: str) -> str:
    if kind == "recover":
        return compose_login_recovered()
    return compose_login_alert(kind.split(":", 1)[1])


def _sync_ops_task(kind: str) -> None:
    """Mirror the page into the durable `ops-hq-browser` vault human task.

    `recover` closes it; any `alert:<state>` (logged_out / no_chrome / the unknown
    3-streak) opens the single task with the same re-login steps the page carries.
    Delegated to ops_alerts, which is idempotent and fail-open, so this never
    perturbs the existing kakao paging/throttle regardless of call order."""
    if kind == "recover":
        ops_alerts.resolve_alert("hq-browser", "로그인 복구")
    elif kind.startswith("alert:"):
        state = kind.split(":", 1)[1]
        ops_alerts.open_alert(
            "hq-browser",
            _HEADLINES.get(state, _HEADLINES["unknown"]),
            [
                "본부에서: browser.sh start",
                f"맥에서: gcloud compute ssh {ssh_hint()} -- -L 6080:localhost:6080",
                "맥 브라우저로 http://localhost:6080/vnc.html 열고 직접 로그인",
            ],
            level="alert",
        )


# -- entry point -------------------------------------------------------------

def check_login_watch(now: float | None = None, *,
                      notify: Callable[[str], object] | None = None,
                      run: Callable = subprocess.run) -> str | None:
    """Probe at most once per interval; return the message sent, if any.

    Never raises. The state is written BEFORE notifying, so a broken notifier
    cannot turn into one page per tick.
    """
    try:
        script = script_path()
        if not script.is_file():
            return None
        now = time.time() if now is None else now
        prev = _read_state()
        elapsed = now - prev["last_checked_at"]
        # A last check in the future (clock stepped back) does not suppress.
        if 0 <= elapsed < interval_s():
            return None
        observed = run_login_check(script, run=run)
        if observed is None:
            print(f"browser-watch: node binary not found ({node_bin()}) — skipped")
            _write_state({**prev, "last_checked_at": now})
            return None
        new, kind = decide(prev, observed)
        new["last_checked_at"] = now
        _write_state(new)
        if kind is None:
            return None
        _sync_ops_task(kind)
        msg = _compose(kind)
        if notify is not None:
            try:
                notify(msg)
            except Exception as exc:  # noqa: BLE001 - paging is best-effort
                print(f"browser-watch: notify failed — {exc}")
        return msg
    except Exception as exc:  # noqa: BLE001 - must never break maintenance
        print(f"browser-watch: error — {exc}")
        return None
