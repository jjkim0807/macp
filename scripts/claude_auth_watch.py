#!/usr/bin/env python3
"""Watch the Claude Code CLI credential in the login Keychain and shout when it dies.

Repo home: MaestroACP/scripts — installed by install_remote_control_launchagent.sh.
(2026-08-19에 예전 단일 설정 디렉터리(legacy config dir)의 스크립트에서 이 레포로 이관.)

Multi-account (leaf ⑤): the credential / automation-token / chrome checks run
once per claude account of the resolved profile (macp.config.resolve), each
against its own CLAUDE_CONFIG_DIR; a named account whose login is dead is marked
unavailable('auth') so the runner's resolve_active skips it. Transcripts, the
remote-control daemon and its logs are shared, so those are checked once. With no
profile / no claude account it falls back to the single legacy config dir exactly
as before (and never marks — there is nothing to switch to).

Background: on 2026-08-15 the stored OAuth refresh token hit its absolute expiry
(refreshTokenExpiresAt 2026-08-15T05:45Z, ~90 days after the 2026-05-18 login).
The CLI blanked accessToken/refreshToken to empty strings and every headless
process — the remote-control LaunchAgent and the MaestroACP cron jobs — began
answering "Not logged in - Please run /login" with error=authentication_failed.
Nothing was logged anywhere, so it went unnoticed for a day and a half.

This script runs from <label_prefix>claude-auth-watch and fails loudly instead:

  1. credential shape  - accessToken / refreshToken non-empty
  2. expiry runway     - refreshTokenExpiresAt still WARN_DAYS away
  3. observed failures - any session transcript written in the last day
                         carrying "error": "authentication_failed"
  4. chrome integration- claudeInChromeDefaultEnabled stays true so sessions
                         spawned by remote-control keep their Chrome tools;
                         extension pairing state is reported as a note
  5. housekeeping      - truncate the remote-control log if it runs away
  6. plan vs login     - oauthAccount.subscriptionCreatedAt newer than the
                         login (Keychain item creation) → the token's Remote
                         Control entitlement is stale; re-login before the
                         daemon's next restart exposes it (2026-10-17)

It never prints or stores token material: only lengths, expiry dates and counts.
Remedy when it fires: run `claude` in a terminal, `/login`, then
`launchctl kickstart -k gui/$UID/<label_prefix>claude-remote-control`
(``<label_prefix>`` = ``paths.label_prefix()``, the vault's ``.macp/config.toml`` key).
For a Remote Control entitlement failure (RC_RELOGIN_HINT) it is the full
`claude auth logout && claude auth login` — a refresh does not renew entitlements.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import os
import re
import subprocess
import sys
from pathlib import Path

KEYCHAIN_SERVICE = "Claude Code-credentials"
IS_DARWIN = sys.platform == "darwin"
_INSTALL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_INSTALL_DIR))          # bootstrap — raw install location
from todo_system import daemonctl  # noqa: E402
from todo_system import paths  # noqa: E402
# Single source of the length budget + self-limiting composer. Leaf module,
# stdlib-only, so it stays safe under launchd's bare /usr/bin/python3 (no venv).
from todo_system.kakao_format import compose_alert  # noqa: E402
# Engine root through the resolver: dev checkout -> repo root (same value as the
# bootstrap above); wheel install -> todo_system/_engine, where scripts/ and
# .claude/commands ship in repo layout. The bootstrap only puts the engine on
# sys.path; this is the definition the rest of the module uses.
_INSTALL_DIR = paths.engine_root()
# Data root through the resolver: honours MACP_VAULT_ROOT / .macp/config.toml.
# The start= anchor is paths.vault_search_start(): the engine root in a checkout
# (byte-identical to the former Path(__file__).resolve().parents[1] when unset),
# None in an installed wheel so the cwd-upward search runs instead of anchoring
# on site-packages, which never holds a vault marker.
VAULT = paths.vault_root(start=paths.vault_search_start())
# Daemon label through the resolver too (env MACP_LABEL_PREFIX → the vault's
# .macp/config.toml → neutral default), anchored at the resolved vault — the
# same file as the install's in the combined layout, never site-packages.
RC_LABEL = f"{paths.label_prefix(start=VAULT)}claude-remote-control"
# Per-account paths (credential / projects / .claude.json) are derived from each
# account's CLAUDE_CONFIG_DIR at call time — see _credentials_file,
# _shared_projects_dir and check_chrome — not from module constants, so the watch
# follows whatever accounts the profile holds. Linux (cloud HQ): the CLI keeps its
# OAuth credential in a 0600 <config dir>/.credentials.json (same
# {"claudeAiOauth": {...}} shape); the Mac uses the shared login Keychain.
# 7일: 재로그인은 로그인 시점부터 ~27일 창을 새로 준다(맥 9/07 로그인 → 10/04,
# VM도 10/10으로 같은 폭). 잔여기간과 무관하므로 일찍 알릴 이유가 없다 — 21일로 두면
# 로그인 6일 뒤부터 매 창의 대부분 동안 경고가 울려 소음이 된다(2026-09-14).
WARN_DAYS = 7
TRANSCRIPT_WINDOW_HOURS = 26  # a bit over a day, so a daily run never has a blind spot
LOG_PATH = (Path.home() / "Library" / "Logs" / "claude-auth-watch.log" if IS_DARWIN
            else Path.home() / ".local" / "state" / "macp" / "claude-auth-watch.log")
RC_LOG_PATHS = [
    Path.home() / "Library" / "Logs" / "claude-remote-control.log",
    Path.home() / "Library" / "Logs" / "claude-remote-control.out.log",
    Path.home() / "Library" / "Logs" / "claude-remote-control.debug.log",   # --debug-file (wrapper, 2026-09-12)
] if IS_DARWIN else [
    VAULT / "logs" / "claude-remote-control.err.log",   # systemd StandardError=append:
    VAULT / "logs" / "claude-remote-control.out.log",
    Path.home() / ".local" / "state" / "macp" / "claude-remote-control.debug.log",
]
# The remote-control status block redraws (~3.5 MB/day of repeated "Connected"
# lines), so cap it well under a day's worth of the 6-hourly check interval.
RC_LOG_MAX_BYTES = 5 * 1024 * 1024


def log(line: str) -> None:
    stamp = dt.datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a") as fh:
        fh.write(f"[{stamp}] {line}\n")
    print(line)


KAKAO_SCRIPT = pathlib.Path(__file__).resolve().parent / "kakao_send.sh"


def _compose_kakao_text(title: str, message: str) -> str:
    """Self-limiting memo text for an auth alert: headline 🔐<title> + the remedy
    as a word-clipped tail. Pure (no send), so it is unit-testable without reaching
    the real kakao channel ([[tests-reach-real-alert-sender]]); replaces the blind
    `[:179]` cut that could bisect the /login instruction it exists to deliver."""
    return compose_alert(f"🔐 {title}", tail=message)


def notify(title: str, message: str, link: str | None = None,
           level: str | None = None) -> None:
    """카카오 + 데스크톱 배너. 둘 다 best effort.

    데스크톱 배너만 쓰던 것을 2026-09-07에 카카오 우선으로 바꿨다: 이 경고는
    launchd 헤드리스 컨텍스트에서 뜨고(osascript 자체가 거부될 수 있다), 배너는
    화면을 안 보고 있으면 그냥 사라진다. 인증 만료는 놓치면 자동화가 통째로
    죽는 종류라 실제로 읽는 채널로 보내야 한다. 카카오는 kakao_send.sh가 이미
    다른 알림에 쓰는 경로다(길이는 compose_alert가 보장 — 눈먼 커트 없음). `link`은
    kakao_send.sh 2번째 인자(열기 버튼)로 넘어간다.

    `level`은 노이즈 등급 옵트인(scripts/KAKAO_ALERTS.md): stdlib 제약 때문에
    라우터를 import하지 않고 kakao_send.sh의 KAKAO_LEVEL env 경로를 태운다.
    None(기본)이면 env를 건드리지 않아 기존 즉시 발송 그대로다."""
    if KAKAO_SCRIPT.exists():
        try:
            args = [str(KAKAO_SCRIPT), _compose_kakao_text(title, message)]
            if link:
                args.append(link)
            env = dict(os.environ)
            if level:
                env["KAKAO_LEVEL"] = level
            subprocess.run(args, capture_output=True, timeout=30, env=env)
        except Exception:
            pass
    if not IS_DARWIN:
        return
    script = (
        f'display notification {json.dumps(message)} '
        f'with title {json.dumps(title)} sound name "Basso"'
    )
    subprocess.run(["osascript", "-e", script], capture_output=True)


def _acc():
    """The accounts layout module, imported lazily so merely *loading* this
    script (test_paths / the wheel smoke / the sibling watchdog+upgrade imports)
    pulls in no account resolution — only a run does."""
    from todo_system.macp import accounts as ACC
    return ACC


def _acct_label(name) -> str:
    """Prefix for every per-account message so an alert names the account."""
    return f"[{name}] " if name else "[legacy] "


def _credentials_file(config_dir) -> Path:
    """An account's OAuth credential file (Linux): ``<config dir>/.credentials.json``."""
    return Path(config_dir) / ".credentials.json"


def _watched_accounts() -> list[tuple[str | None, Path]]:
    """``[(name, config_dir)]`` for every claude account of the resolved profile,
    or ``[(None, <legacy config dir>)]`` when there is no profile or no claude
    account (the pre-account behaviour). Resolved at call time, never at import —
    the sibling scripts load this module without wanting profile resolution."""
    try:
        from todo_system.macp import config as _C
        prof = _C.resolve().get("profile")
        ACC = _acc()
        accts = [a for a in (getattr(prof, "accounts", None) or [])
                 if getattr(a, "agent", "claude") == "claude"]
        if accts:
            return [(a.name, ACC.config_dir_for(a)) for a in accts]
    except Exception:
        pass
    return [(None, _acc().legacy_config_dir())]


def _shared_projects_dir(watched) -> Path:
    """The single shared transcript root. Every account's ``projects/`` is a
    symlink into one shared dir, so any watched account's ``projects/`` is that
    dir; with no account it is the legacy ``<config dir>/projects``
    (pre-migration). Checked once per run (AC3)."""
    base = watched[0][1] if watched else _acc().legacy_config_dir()
    return Path(base) / "projects"


def _mark_auth(name, config_dir) -> None:
    """Mark a NAMED account unavailable('auth', sticky) so ``resolve_active``
    skips it until its credentials change or ``macp account use`` clears it. A
    legacy single account (``name`` None) is never marked — there is no other
    account to switch to, and the shared alert already fires. Best-effort: the
    state write never blocks the alert."""
    if name is None:
        return
    try:
        ACC = _acc()
        ACC.mark_unavailable(name, ACC.CAPACITY_AUTH, config_dir=config_dir)
    except Exception:
        pass


def read_credentials(config_dir) -> dict | None:
    """The CLI's OAuth credential for an account: its ``<config dir>/.credentials.json``
    on Linux, the shared login Keychain on the Mac (one item, not per config dir)."""
    if not IS_DARWIN:
        try:
            return json.loads(_credentials_file(config_dir).read_text())
        except (OSError, json.JSONDecodeError):
            return None
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def check_credential(name, config_dir, problems: list[str], notes: list[str]) -> None:
    """Check one account's OAuth credential. A *hard logout* — the credential is
    missing/unreadable, has no claudeAiOauth block, an empty access/refresh token,
    or a refresh token already past its absolute expiry (no re-issue possible) —
    marks a named account unavailable('auth'). A merely *soon-to-expire* refresh
    token (WARN_DAYS) is a warning but not a mark: it is still valid, matching the
    usage guard's rule (a refreshable access token is never an auth failure)."""
    label = _acct_label(name)
    creds = read_credentials(config_dir)
    if creds is None:
        problems.append(
            label
            + (f"Keychain item '{KEYCHAIN_SERVICE}' is missing or unreadable " if IS_DARWIN
               else f"{_credentials_file(config_dir)} is missing or unreadable ")
            + "(headless processes cannot authenticate)"
        )
        _mark_auth(name, config_dir)
        return

    oauth = creds.get("claudeAiOauth") or {}
    if not oauth:
        problems.append(label + "credential has no claudeAiOauth block")
        _mark_auth(name, config_dir)
        return

    logged_out = False
    for field in ("accessToken", "refreshToken"):
        if not (oauth.get(field) or ""):
            problems.append(label + f"claudeAiOauth.{field} is empty — the CLI is logged out")
            logged_out = True

    now_ms = dt.datetime.now(dt.timezone.utc).timestamp() * 1000
    refresh_exp = oauth.get("refreshTokenExpiresAt")
    if isinstance(refresh_exp, (int, float)) and refresh_exp > 0:
        when = dt.datetime.fromtimestamp(refresh_exp / 1000, dt.timezone.utc)
        days = (refresh_exp - now_ms) / 86_400_000
        if days <= 0:
            problems.append(
                label + f"refresh token expired {abs(days):.1f}d ago ({when:%Y-%m-%d %H:%MZ})"
            )
            logged_out = True
        elif days <= WARN_DAYS:
            problems.append(
                label + f"refresh token expires in {days:.1f}d ({when:%Y-%m-%d %H:%MZ}) — "
                "re-login before it does"
            )
        else:
            notes.append(label + f"refresh token good for {days:.0f}d (until {when:%Y-%m-%d})")
    else:
        notes.append(label + "refresh token carries no expiry field")

    if logged_out:
        _mark_auth(name, config_dir)

    _check_automation_token(name, config_dir, problems, notes)


def _check_automation_token(name, config_dir, problems: list[str], notes: list[str]) -> None:
    """장수 automation token 유무(계정의 config dir 기준). 추론 전용 헤드리스 잡의
    Keychain 만료 대비책.

    토큰 자체는 읽지 않고 존재·권한만 본다 (내용은 절대 로깅하지 않는다).
    없다고 problem 은 아니다 — 아직 발급 안 한 상태가 정상 경로일 수 있다.

    적용 범위 주의: remote-control은 이 토큰으로 살릴 수 없다. CLI 2.1.236은 장수
    토큰을 거부한다("Remote Control requires a full-scope login token"). 2026-09-07에
    아래 '미발급' 노트를 보고 토큰을 발급했다가, 당시 그것을 주입하던 래퍼 때문에
    remote-control이 30초 크래시 루프에 빠졌다. 래퍼는 이제 변수를 unset 하고,
    노트 문구도 적용 범위를 명시한다.
    """
    label = _acct_label(name)
    tok = Path(config_dir) / "automation-token"
    if not tok.exists():
        notes.append(
            label + "automation-token 미발급 — 추론 전용 헤드리스 잡이 Keychain 만료에 그대로 "
            f"노출됨 (`claude setup-token` 후 {tok} 에 저장, chmod 600). "
            "remote-control은 해당 없음 — Keychain 로그인 전용"
        )
        return
    mode = tok.stat().st_mode & 0o777
    if mode != 0o600:
        problems.append(label + f"automation-token 권한이 {oct(mode)} — chmod 600 필요")

    # 내용이 토큰 모양인지까지 본다. 2026-09-07에 `pbpaste`가 setup-token 출력이
    # 아니라 명령줄 자체를 파일에 넣은 일이 있었다 — 파일 존재만 확인했다면
    # "발급 완료"로 읽히고 정작 인증은 Keychain 만료와 함께 죽었을 것이다.
    try:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
        from todo_system.claude_auth import _looks_like_token
    except Exception:
        notes.append(label + "automation-token 있음 (형태 검증 모듈 미로드)")
        return
    try:
        body = tok.read_text(encoding="utf-8").strip()
    except OSError as exc:
        problems.append(label + f"automation-token 읽기 실패: {exc}")
        return
    if not _looks_like_token(body):
        problems.append(
            label + f"automation-token 내용이 토큰 형태가 아님 (길이 {len(body)}) — "
            "`claude setup-token` 출력만 넣을 것. 지우면 Keychain으로 폴백"
        )
    else:
        notes.append(
            label + "automation-token 있음 (600, 형태 OK) — 추론 전용 잡은 Keychain 만료와 "
            "독립 (remote-control은 여전히 Keychain 의존)"
        )


def check_chrome(name, config_dir, problems: list[str], notes: list[str]) -> None:
    """Keep the Chrome-integration preconditions for spawned sessions visible,
    per account (its ``.claude.json`` and ``chrome/`` live in its config dir).

    Linux (cloud HQ) has no Chrome — skipped there with a note.

    claudeInChromeDefaultEnabled=true is what makes every session spawned by the
    remote-control service default to Chrome tools; the installer sets it, and
    anything flipping it back off should fail loudly. Extension pairing is a
    one-time interactive approval we can only observe, not fix — report it as a
    note either way. Verified 2026-08-19: completing the pairing writes
    hasCompletedClaudeInChromeOnboarding=true to the account's .claude.json and
    drops ``<config dir>/chrome/chrome-native-host`` (it does NOT write a
    chromeExtension block, contrary to the first guess).
    """
    label = _acct_label(name)
    if not IS_DARWIN:
        notes.append(label + "chrome integration n/a on linux")
        return
    claude_json = _acc().claude_json_path(config_dir)
    try:
        cfg = json.loads(claude_json.read_text())
    except (OSError, json.JSONDecodeError):
        problems.append(label + f"{claude_json} unreadable — cannot verify Chrome integration")
        return

    if cfg.get("claudeInChromeDefaultEnabled") is not True:
        problems.append(
            label + "claudeInChromeDefaultEnabled is not true — spawned sessions lost their "
            "Chrome tools (re-run scripts/install_remote_control_launchagent.sh)"
        )

    paired = (
        cfg.get("hasCompletedClaudeInChromeOnboarding") is True
        or (Path(config_dir) / "chrome" / "chrome-native-host").exists()
    )
    if paired:
        notes.append(label + "chrome extension paired")
    else:
        notes.append(
            label + "chrome extension not paired yet — one-time approval needed "
            "(scripts/REMOTE_CONTROL_SETUP.md)"
        )


def parse_timestamp(value: object) -> float | None:
    """Epoch seconds from a transcript record's ISO-8601 timestamp."""
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def credential_written_at(config_dir) -> float | None:
    """Epoch seconds of this account's credential last write — the last
    login/refresh: its ``.credentials.json`` mtime on Linux, the Keychain item's
    ``mdat`` on the Mac (shared, config_dir not consulted).

    Used as a floor on the transcript scan so that failures from *before* a
    recovery login stop being reported the moment the credential is replaced.
    """
    if not IS_DARWIN:
        try:
            return _credentials_file(config_dir).stat().st_mtime
        except OSError:
            return None
    return _keychain_stamp("mdat")


def _keychain_stamp(attr: str) -> float | None:
    """Epoch seconds of the Keychain item's ``mdat`` (last write) or ``cdat``
    (creation) attribute, from ``security find-generic-password`` metadata."""
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE],
        capture_output=True,
        text=True,
    )
    for line in proc.stdout.splitlines():
        if f'"{attr}"' not in line:
            continue
        # Line looks like: "mdat"<timedate>=0x3230...5A00  "20260816100005Z\000"
        match = re.search(r'"(\d{14}Z)', line)
        if not match:
            return None
        parsed = dt.datetime.strptime(match.group(1), "%Y%m%d%H%M%SZ")
        return parsed.replace(tzinfo=dt.timezone.utc).timestamp()
    return None


def credential_created_at(config_dir) -> float | None:
    """Epoch seconds of the LOGIN, as opposed to the last refresh.

    Mac only: ``claude auth login`` deletes and re-creates the Keychain item, so
    its ``cdat`` is the login time (verified 2026-10-19: cdat 2026-05-18 →
    2026-10-19T00:46Z across a logout/login, while mdat moves on every refresh).
    Linux rewrites ``.credentials.json`` on refresh too, so there is no login
    stamp to read there — None, and the plan-vs-login check is skipped.
    """
    if not IS_DARWIN:
        return None
    return _keychain_stamp("cdat")


def _plan_newer_than_login(subscription_created_at: str | None,
                           credential_created_at_s: float | None) -> bool:
    """True when the account's plan was (re)created AFTER the current login.

    Pure, so it is unit-testable. ``subscription_created_at`` is the ISO stamp
    the CLI caches in ``.claude.json`` → ``oauthAccount.subscriptionCreatedAt``
    (profile refetches keep it current even while the token stays old).
    """
    if not subscription_created_at or credential_created_at_s is None:
        return False
    created = parse_timestamp(subscription_created_at)
    if created is None:
        return False
    return created > credential_created_at_s


def check_plan_change(name, config_dir, problems: list[str], notes: list[str]) -> None:
    """Catch a login whose Remote Control entitlement the server no longer honours.

    2026-10-16 03:46Z the plan was re-created (oauthAccount.subscriptionCreatedAt);
    the 2026-05-18 login kept refreshing fine, every check here stayed OK, and the
    running daemon — registered on 10/15 — kept serving. The 10/17 16:40 upgrade
    restart then failed with "Remote Control isn't enabled for this account" /
    bridge 404 for two days. Token refreshes do NOT pick up entitlements; only a
    new login does, so warn as soon as the plan stamp passes the login stamp,
    while the daemon is still up and the fix is a calm one.
    """
    label = _acct_label(name)
    login_at = credential_created_at(config_dir)
    if login_at is None:
        notes.append(label + "plan-vs-login check n/a (no login timestamp on this platform)")
        return
    try:
        cfg = json.loads(_acc().claude_json_path(config_dir).read_text())
    except (OSError, json.JSONDecodeError):
        notes.append(label + "plan-vs-login check skipped (.claude.json unreadable)")
        return
    plan_at = (cfg.get("oauthAccount") or {}).get("subscriptionCreatedAt")
    if _plan_newer_than_login(plan_at, login_at):
        login_day = dt.datetime.fromtimestamp(login_at, dt.timezone.utc).strftime("%Y-%m-%d")
        problems.append(
            label + f"plan (re)created {plan_at[:10]} after the current login ({login_day}) — "
            f"remote-control will be refused as 'not enabled for this account' at its next "
            f"restart; {RC_RELOGIN_HINT}"
        )
        return
    notes.append(label + "login newer than plan")


def check_transcripts(watched, problems: list[str], notes: list[str]) -> None:
    """Catch the symptom directly, in case a credential looks fine but calls 401.

    Transcripts are SHARED across accounts (``projects/`` is one symlinked dir),
    so this runs once (AC3). The floor is the most recent credential write among
    the watched accounts — any account's fresh login clears failures from before
    it, so a recovery on one account does not keep resurfacing the other's."""
    projects = _shared_projects_dir(watched)
    if not projects.is_dir():
        return
    cutoff = dt.datetime.now().timestamp() - TRANSCRIPT_WINDOW_HOURS * 3600
    floors = [w for w in (credential_written_at(cd) for _, cd in watched) if w is not None]
    written = max(floors) if floors else None
    if written is not None and written > cutoff:
        # A fresh login clears the slate: only failures since it are still news.
        cutoff = written
    hits: list[str] = []
    for path in projects.glob("*/*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
            with path.open(errors="replace") as fh:
                for line in fh:
                    # Substring first (cheap); then confirm it is a real transcript
                    # record rather than the string quoted inside some tool output.
                    if '"authentication_failed"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("error") != "authentication_failed":
                        continue
                    # Judge by the record's own timestamp, not the file's mtime:
                    # a transcript gets touched (last-prompt trailer) long after
                    # its last real entry, which would otherwise resurrect an
                    # already-fixed failure on every run.
                    when = parse_timestamp(record.get("timestamp"))
                    if when is not None and when < cutoff:
                        continue
                    hits.append(path.name)
                    break
        except OSError:
            continue
    if hits:
        problems.append(
            f"{len(hits)} session(s) in the last {TRANSCRIPT_WINDOW_HOURS}h failed with "
            f"authentication_failed (e.g. {hits[0]})"
        )
    else:
        notes.append("no authentication_failed in recent transcripts")


def trim_remote_control_log() -> None:
    for path in RC_LOG_PATHS:
        try:
            if path.is_file() and path.stat().st_size > RC_LOG_MAX_BYTES:
                keep = path.read_bytes()[-RC_LOG_MAX_BYTES // 2 :]
                path.write_bytes(b"[truncated by claude-auth-watch]\n" + keep)
                log(f"truncated oversized log {path}")
        except OSError:
            continue


def running_remote_control_pid() -> int | None:
    """PID of the live `claude remote-control` daemon, if any.

    ps, not pgrep. On 2026-09-07 `pgrep -f "claude remote-control"` returned
    nothing while `ps -eo pid=,args=` showed pid 86153 running exactly that
    command: pgrep silently omitted 4 of this user's 252 processes and the
    daemon was one of them (`pgrep -x claude` missed it too, so it is the
    process that is invisible, not the pattern that is wrong).

    Answering "not running" for a daemon that IS running is the most expensive
    mistake this module can make — claude_upgrade reads it as "nothing to
    strand" and upgrades the binary out from under the live daemon.
    """
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        fields = line.split()
        # `<pid> /path/to/claude remote-control --name ...` — match the
        # subcommand positionally so a session whose arguments merely mention
        # remote-control cannot pose as the daemon.
        if len(fields) < 3 or fields[2] != "remote-control":
            continue
        if Path(fields[1]).name != "claude":
            continue
        try:
            return int(fields[0])
        except ValueError:
            continue
    return None


def pinned_exec_paths(pid: int) -> set[str] | None:
    """Absolute paths of the `claude` executable this pid mapped at startup.

    lsof still reports a text-mapped file after it is unlinked, which is exactly
    the case we want to catch — the path is gone from the filesystem but the
    process keeps spawning from it. None means lsof could not tell us.

    Linux: /proc/<pid>/exe, which keeps the path and appends " (deleted)"
    once the file is unlinked — the same signal, no lsof needed.
    """
    if not IS_DARWIN:
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            return None
        return {exe[: -len(" (deleted)")] if exe.endswith(" (deleted)") else exe}
    try:
        proc = subprocess.run(
            ["lsof", "-p", str(pid), "-Fftn"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    paths: set[str] = set()
    fd = ""
    for line in proc.stdout.splitlines():
        if line.startswith("f"):
            fd = line[1:]
        elif line.startswith("n/") and fd == "txt" and Path(line[1:]).name == "claude":
            paths.add(line[1:])
    return paths


def is_descendant_of(pid: int) -> bool:
    """True if we run underneath `pid` — restarting it would kill this process."""
    cur = os.getpid()
    for _ in range(20):
        if cur == pid:
            return True
        try:
            parent = subprocess.run(
                ["ps", "-p", str(cur), "-o", "ppid="],
                capture_output=True,
                text=True,
                timeout=30,
            ).stdout.strip()
            cur = int(parent)
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
        if cur <= 1:
            return False
    return False


RC_RELOGIN_HINT = "해결: claude auth logout && claude auth login (브라우저 승인 필요)"

# Exit text after which no restart helps — the account/entitlement/credential
# class. Mirrors PERMANENT_PATTERNS in claude_remote_control.sh (keep in step);
# every entry is a CLI 2.1.302 eligibility / registration message. Deliberately
# absent: "Couldn't verify Remote Control eligibility — the feature-flag service
# was unreachable" (network, transient — the 30 s KeepAlive loop is right there).
PERMANENT_RC_ERROR_MARKERS = (
    "You must be logged in to use Remote Control",
    "Remote Control isn't enabled for this account",
    "environments are not available for your account",
    "requires a full-scope login token",
    "requires a claude.ai subscription",
    "requires claude.ai subscription auth",
    "Unable to determine your organization",
    "disabled by your organization's policy",
    "only available when using Claude via api.anthropic.com",
)


def is_permanent_rc_error(line: str | None) -> bool:
    """Does this remote-control stderr line mean 'relogin/entitlement, not retry'?"""
    if not line:
        return False
    return any(marker in line for marker in PERMANENT_RC_ERROR_MARKERS)


def last_startup_error(max_bytes: int = 8192, max_len: int = 240) -> str | None:
    """remote-control stderr 로그 끝에서 가장 최근 에러 한 줄.

    KeepAlive 크래시 루프는 launchctl 상태만 봐서는 원인이 안 보인다. 로그 끝에
    이유가 그대로 찍혀 있으므로(예: "Remote Control requires a full-scope login
    token") 알림 문구에 실어 보낸다. 이 로그에 토큰 문자열이 남는 경로는 없다.
    """
    try:
        with RC_LOG_PATHS[0].open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - max_bytes))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        line = line.strip()
        # The bridge-registration 404 ("Remote Control environments are not
        # available for your account.") carries no "Error" prefix (2026-10-17).
        if line.startswith("Error") or "Error:" in line or is_permanent_rc_error(line):
            return line[:max_len]
    return None


def check_remote_control_binary(problems: list[str], notes: list[str]) -> None:
    """Catch the daemon holding an exec path that an upgrade deleted.

    `claude remote-control` resolves its own executable ONCE at startup and
    spawns every remote session from that absolute path, e.g.
    /opt/homebrew/Caskroom/claude-code/2.1.204/claude. `brew upgrade` installs
    the new version beside it and deletes the old directory — but the
    long-running daemon keeps the dead path. The service then still reports
    Ready/Connected while EVERY new session dies with
    `spawn error: ENOENT ... posix_spawn`. Observed 2026-09-04 (2.1.204 →
    2.1.236): two days of silent failure. KeepAlive cannot save us here because
    the daemon never exits — only the sessions do.

    Restarting in this state costs nothing: a daemon whose exec path is gone can
    spawn no sessions, so there are none to protect.
    """
    pid = running_remote_control_pid()
    if pid is None:
        # 왜 죽었는지까지 한 줄에 실어 보낸다. 2026-09-07 automation-token 크래시
        # 루프는 자격증명·만료·transcript 검사를 모두 통과하고 여기서만 걸렸는데,
        # "is not running"만으로는 원인을 찾으러 로그를 다시 뒤져야 했다.
        why = last_startup_error()
        detail = f" — 마지막 stderr: {why}" if why else ""
        if is_permanent_rc_error(why):
            # 2026-10-17: "isn't enabled for this account" sat in this PROBLEM
            # line for two days; the reader needs the remedy, not just the cause.
            # The wrapper is backing off (5 min → 1 h) rather than looping.
            detail += f" — 재시작으로는 안 살아남, {RC_RELOGIN_HINT}"
        problems.append(
            f"{RC_LABEL} is not running — remote control unreachable{detail} "
            f"({daemonctl.status_hint(RC_LABEL)})"
        )
        return

    pinned = pinned_exec_paths(pid)
    if pinned is None:
        notes.append(f"remote-control alive (pid {pid}); lsof failed, exec path unverified")
        return
    if not pinned:
        notes.append(f"remote-control alive (pid {pid}); no exec path reported, unverified")
        return

    stale = sorted(p for p in pinned if not Path(p).exists())
    if not stale:
        notes.append(f"remote-control exec path live (pid {pid})")
        return

    detail = ", ".join(stale)
    if is_descendant_of(pid):
        problems.append(
            f"{RC_LABEL} (pid {pid}) spawns from a deleted binary ({detail}); every new "
            "remote session fails — NOT auto-restarting, this check is running underneath it"
        )
        return

    log(f"remote-control pid {pid} spawns from deleted binary ({detail}) — restarting {RC_LABEL}")
    try:
        restart = daemonctl.restart(RC_LABEL, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        problems.append(
            f"{RC_LABEL} spawns from a deleted binary ({detail}) and the restart failed "
            f"({exc}) — run `{daemonctl.restart_hint(RC_LABEL)}`"
        )
        return

    if restart.returncode != 0:
        problems.append(
            f"{RC_LABEL} spawns from a deleted binary ({detail}) and the restart "
            f"failed ({restart.stderr.strip() or restart.returncode}) — run "
            f"`{daemonctl.restart_hint(RC_LABEL)}`"
        )
        return

    notes.append(f"remote-control was pinned to deleted {detail} — auto-restarted")
    # 요약 등급: 이미 자가 회복이 끝난 FYI다. 인증 이상(main의 notify)은 즉시 유지.
    notify(
        "Claude Remote Control 자동 복구",
        f"업그레이드로 사라진 바이너리({detail})를 붙들고 있어 재시작했습니다.",
        level="digest",
    )


def check_accounts(watched, problems: list[str], notes: list[str]) -> None:
    """Per-account credential + automation-token + chrome checks; a named account
    with a dead login is marked unavailable('auth') by check_credential. Shared
    resources — transcripts, the remote-control daemon and its logs — are NOT
    touched here; main() does those once per run."""
    for name, config_dir in watched:
        check_credential(name, config_dir, problems, notes)
        check_plan_change(name, config_dir, problems, notes)
        check_chrome(name, config_dir, problems, notes)


def main() -> int:
    problems: list[str] = []
    notes: list[str] = []

    watched = _watched_accounts()
    check_accounts(watched, problems, notes)
    check_transcripts(watched, problems, notes)
    check_remote_control_binary(problems, notes)
    trim_remote_control_log()

    if problems:
        summary = "; ".join(problems)
        log(f"PROBLEM {summary}")
        notify(
            "Claude Code 인증 이상",
            problems[0] + " — 터미널에서 claude 실행 후 /login",
        )
        return 1

    log("OK " + "; ".join(notes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
