"""Phone SMS inbox for macp workers and Claude Code agents on this host.

Purpose
-------
Some tasks need a code that arrives by SMS (a site's 2FA, a carrier notice).
The owner's Android phone runs a small forwarder app that POSTs every received
SMS to the dashboard's `POST /api/sms` (see `scripts/macp_dashboard.py`), which
calls `append()` here. Agents on this host then read the inbox with this CLI —
no phone access, no vault access needed.

Design notes
------------
- **Stdlib only**, like `session_log`, so `python3 -m todo_system.sms` works
  without `uv`.
- **Never in git.** Messages carry bank/card OTPs and personal text, so the
  store lives outside the vault: `$MACP_SMS_DIR` or `~/.local/share/macp/sms/`,
  dir 0700, file 0600.
- **Short retention.** Every append drops entries older than
  `RETENTION_DAYS` (7).
- **Idempotent.** The phone retries until it gets a 2xx, so a message carrying
  the same `id` twice is stored once.
- **Ingest-failure alert throttle.** The dashboard alerts when it rejects the
  phone (401/429/400/413), at most once per kind per 6h. The window lives beside
  the inbox (`alert_state.json`, 0600), so a dashboard restart can't reset it.

Usage
-----
    python3 -m todo_system.sms list --since 30m            # recent messages
    python3 -m todo_system.sms list --from 1588 --json
    python3 -m todo_system.sms wait-code --from 15880000 --timeout 120
    # -> prints just the code (e.g. 482913), exit 1 on timeout

`wait-code` only considers messages received after it started (plus
`--since`, default 0s), so call it right *after* triggering the SMS — or pass
`--since 2m` if the trigger may already have fired.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

RETENTION_DAYS = 7
MAX_BODY = 4000


def store_dir() -> Path:
    env = os.environ.get("MACP_SMS_DIR", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".local" / "share" / "macp" / "sms"


def inbox_path() -> Path:
    return store_dir() / "inbox.jsonl"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_ts(s: str) -> dt.datetime | None:
    try:
        t = dt.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


@contextlib.contextmanager
def _locked():
    d = store_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    lock = d / ".lock"
    with open(lock, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read_all() -> list[dict]:
    p = inbox_path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _write_all(records: list[dict]) -> None:
    p = inbox_path()
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


# -- ingest-failure alert throttle (used by the dashboard's POST /api/sms) ----
ALERT_COOLDOWN = dt.timedelta(hours=6)
ALERT_MAX_CHARS = 180  # scripts/kakao_send.sh cuts messages here

# Fallback only for when the state file can't be locked/read/written; the file is
# the source of truth whenever it is usable (so restarts keep the window).
_ALERT_MEMORY: dict[str, dt.datetime] = {}
_ALERT_MEMORY_LOCK = threading.Lock()

INGEST_ALERT_HINTS = {
    "auth": "SMS 토큰 불일치·누락 — 폰이면 /sms-setup 재적용(외부 스캔일 수도 있음)",
    "bad_payload": "잘못된 payload — 릴레이 앱 버전·전송 형식 확인",
    "too_large": "본문 크기 초과 — 릴레이 앱 배치 크기 확인",
}


def alert_state_path() -> Path:
    return store_dir() / "alert_state.json"


def _read_alert_state() -> dict:
    """{kind: iso ts}. Missing or corrupt -> {} (worst case one extra alert).
    OSError (e.g. the path is a directory) propagates to the caller."""
    p = alert_state_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_alert_state(state: dict) -> None:
    p = alert_state_path()
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def _within(last: dt.datetime | None, now: dt.datetime, cooldown: dt.timedelta) -> bool:
    # A `last` in the future (clock stepped back) does not suppress.
    return last is not None and dt.timedelta(0) <= now - last < cooldown


def should_alert(kind: str, now: dt.datetime | None = None,
                 cooldown: dt.timedelta = ALERT_COOLDOWN) -> bool:
    """True at most once per `kind` per `cooldown`, and stamps the slot when it
    says yes. The slot is reserved BEFORE the caller sends, so a failed send is
    not retried inside the window — deliberate: a broken notifier must not turn
    into one alert per rejected request."""
    now = now or _now()
    try:
        with _locked():
            state = _read_alert_state()
            if _within(_parse_ts(state.get(kind, "")), now, cooldown):
                return False
            state[kind] = now.isoformat(timespec="seconds")
            _write_alert_state(state)
            return True
    except OSError:
        with _ALERT_MEMORY_LOCK:
            if _within(_ALERT_MEMORY.get(kind), now, cooldown):
                return False
            _ALERT_MEMORY[kind] = now
            return True


def compose_ingest_alert(kind: str, code: int) -> str:
    """Fixed per-kind template + status code only — never the body, sender,
    token, request path or parse-error text."""
    hint = INGEST_ALERT_HINTS.get(kind, "수신 실패 — 대시보드 저널 확인")
    msg = f"📵 SMS 릴레이 수신 거부 (HTTP {int(code)}): {hint} · 같은 종류 6시간 1회"
    return msg[:ALERT_MAX_CHARS]


def normalize(raw: dict) -> dict:
    """Validate one message from the phone. Raises ValueError on bad input."""
    if not isinstance(raw, dict):
        raise ValueError("message must be an object")
    sender = str(raw.get("from") or "").strip()
    body = raw.get("body")
    if not sender or not isinstance(body, str):
        raise ValueError("message needs 'from' and 'body'")
    body = body[:MAX_BODY]
    sent_at = raw.get("sent_at")
    sent_iso = None
    if isinstance(sent_at, (int, float)) and sent_at > 0:
        # Android timestamps are epoch milliseconds.
        sent_iso = dt.datetime.fromtimestamp(sent_at / 1000, dt.timezone.utc).isoformat(timespec="seconds")
    elif isinstance(sent_at, str) and _parse_ts(sent_at):
        sent_iso = _parse_ts(sent_at).isoformat(timespec="seconds")
    mid = str(raw.get("id") or "").strip()[:128]
    if not mid:
        mid = hashlib.sha256(f"{sender}\0{sent_iso}\0{body}".encode()).hexdigest()[:24]
    return {"id": mid, "from": sender[:64], "body": body, "sent_at": sent_iso,
            "device": str(raw.get("device") or "")[:64] or None}


def append(messages: list[dict], now: dt.datetime | None = None) -> int:
    """Store normalized messages, drop expired ones. Returns how many were new."""
    now = now or _now()
    cutoff = now - dt.timedelta(days=RETENTION_DAYS)
    with _locked():
        records = [r for r in _read_all()
                   if (_parse_ts(r.get("received_at", "")) or now) >= cutoff]
        seen = {r.get("id") for r in records}
        added = 0
        for m in messages:
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            records.append({**m, "received_at": now.isoformat(timespec="seconds")})
            added += 1
        _write_all(records)
    return added


def parse_duration(s: str) -> dt.timedelta:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", s or "")
    if not m:
        raise argparse.ArgumentTypeError(f"bad duration {s!r} (use e.g. 90s, 10m, 2h, 1d)")
    n, unit = float(m.group(1)), m.group(2) or "s"
    return dt.timedelta(seconds=n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit])


def query(since: dt.datetime | None = None, sender: str | None = None,
          pattern: str | None = None) -> list[dict]:
    """Messages in arrival order. `sender` is a substring match on the number
    (digits only, so '+82 10-1234' matches '01012345678'-style variants loosely)."""
    rx = re.compile(pattern) if pattern else None
    want = re.sub(r"\D", "", sender) if sender else None
    out = []
    for r in _read_all():
        t = _parse_ts(r.get("received_at", ""))
        if since and (t is None or t < since):
            continue
        if sender:
            frm = r.get("from", "")
            digits = re.sub(r"\D", "", frm)
            if not ((want and want in digits) or sender.lower() in frm.lower()):
                continue
        if rx and not rx.search(r.get("body", "")):
            continue
        out.append(r)
    return out


# A run of 4-8 digits; prefer one right after a code keyword.
_KEYWORD_CODE = re.compile(r"(?:인증\s*번호|인증\s*코드|승인\s*번호|확인\s*코드|코드|code|OTP)\D{0,12}?(\d{4,8})(?!\d)", re.I)
_ANY_CODE = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")


def extract_code(body: str) -> str | None:
    m = _KEYWORD_CODE.search(body or "")
    if m:
        return m.group(1)
    m = _ANY_CODE.search(body or "")
    return m.group(1) if m else None


def _fmt(r: dict) -> str:
    return f"[{r.get('received_at')}] {r.get('from')}: {r.get('body')}"


def cmd_list(args: argparse.Namespace) -> int:
    since = _now() - args.since if args.since else None
    rows = query(since, args.sender, args.grep)
    if args.limit:
        rows = rows[-args.limit:]
    for r in rows:
        print(json.dumps(r, ensure_ascii=False) if args.json else _fmt(r))
    return 0


def cmd_wait_code(args: argparse.Namespace) -> int:
    # received_at is stored at whole seconds; floor so a message landing in
    # the same second wait-code started isn't missed.
    since = (_now() - args.since).replace(microsecond=0)
    deadline = time.monotonic() + args.timeout
    while True:
        for r in reversed(query(since, args.sender, args.grep)):
            code = extract_code(r.get("body", ""))
            if code:
                if args.verbose:
                    print(_fmt(r), file=sys.stderr)
                print(code)
                return 0
        if time.monotonic() >= deadline:
            print(f"no code SMS within {args.timeout:.0f}s", file=sys.stderr)
            return 1
        time.sleep(args.interval)


def cmd_ingest(args: argparse.Namespace) -> int:
    data = json.load(sys.stdin)
    msgs = data.get("messages", [data]) if isinstance(data, dict) else data
    print(append([normalize(m) for m in msgs]))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m todo_system.sms",
                                description="Read SMS forwarded from the owner's phone.")
    sub = p.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("list", help="print stored messages (oldest first)")
    ls.add_argument("--since", type=parse_duration, help="only the last e.g. 30m / 2h / 1d")
    ls.add_argument("--from", dest="sender", help="sender number or name (substring)")
    ls.add_argument("--grep", help="regex on the body")
    ls.add_argument("--limit", type=int, default=50, help="last N (0 = all; default 50)")
    ls.add_argument("--json", action="store_true", help="one JSON object per line")
    ls.set_defaults(func=cmd_list)

    wc = sub.add_parser("wait-code", help="block until a code SMS arrives; print the code")
    wc.add_argument("--from", dest="sender", help="sender number or name (substring)")
    wc.add_argument("--grep", help="regex the body must match (e.g. 'SNU|서울대')")
    wc.add_argument("--timeout", type=float, default=120, help="seconds (default 120)")
    wc.add_argument("--since", type=parse_duration, default=dt.timedelta(0),
                    help="also accept messages this old (default 0 = only new ones)")
    wc.add_argument("--interval", type=float, default=2.0, help=argparse.SUPPRESS)
    wc.add_argument("-v", "--verbose", action="store_true", help="echo the matched SMS to stderr")
    wc.set_defaults(func=cmd_wait_code)

    ing = sub.add_parser("ingest", help="store JSON from stdin (testing; the phone uses POST /api/sms)")
    ing.set_defaults(func=cmd_ingest)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
