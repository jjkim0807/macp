#!/usr/bin/env python3
"""Weekly health report for the second-brain automation stack.

"측정 없는 자동화는 조용히 죽는다" ("automation without measurement dies
quietly") — this is the meta-watchdog layer: it checks
whether the safety nets themselves are still alive. Six checks, each mapped
1:1 to a real incident (see INCIDENT_NOTES):

1. 데몬 구코드    — a launchd daemon started before the last todo_system/scripts
                    commit is running merged-but-not-live code (07-24~29: the
                    runner ran 5-day-old code → 168-run redispatch loop).
2. 허위 원장      — Worker/assignments rows stuck in awaiting_human/failed whose
                    PR is actually MERGED → the auto-reconciler itself is dead.
                    gh lookup failures are reported loudly for the same reason.
                    A merged awaiting_human row that still has blocked_on is a
                    park, not a lie (PR #132: a human step is still owed) — it
                    is listed as 사람 대기 and never warns.
3. 데이터 신선도  — trailing-weekday gap scan of the paper-run equity curve:
                    ≥2 consecutive closed weekdays without a market-hours point
                    (07-25~29: 5 silent days while the process stayed alive).
4. watchdog 빈도  — >10 ALERTs on any day of the last 7 (07-30: 135 false
                    alerts in one day drowned the real ones).
5. 로그 에러      — Traceback/ERROR surge in vault logs/*.{err,out}.log since
                    the previous run (byte cursors in Worker/_state/health.json).
6. 디스크 여유    — free space on the filesystem under ~/Worker (the HQ host's root,
                    shared with /tmp) below disk_guard.MIN_FREE_BYTES (09-19:
                    160M left, every Android run died of ENOSPC). The runner's
                    run-start guard already cleans idle clones below that line,
                    so a WARN here means the guard alone is not keeping up.

Design rules:
- judge_* functions are pure (no file/subprocess IO) and unit-tested; the
  collectors (gather_*/read_*/tail_*) do the IO and take injectable paths.
- stdlib only — launchd runs this with bare /usr/bin/python3 (no uv/venv).
- 정상이면 무음 (silent when healthy): the episode is always written, kakao
  fires only on ≥1 WARN.
- Tests load this module by path via importlib → the top level must stay
  side-effect free (constants/defs only; entry point strictly under __main__).

Usage:
    python3 scripts/health_report.py [--dry-run] [--now ISO] [--commit [--push]]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

KST = dt.timezone(dt.timedelta(hours=9))

# Check names — shared by judges, renderers and the run orchestration.
CHECK_DAEMON = "데몬 구코드"
CHECK_LEDGER = "허위 원장"
CHECK_FRESHNESS = "데이터 신선도"
CHECK_WATCHDOG = "watchdog 빈도"
CHECK_LOGS = "로그 에러"
CHECK_DISK = "디스크 여유"

# judge_ledger's name for merged rows parked on purpose (see _is_merged_park).
LEDGER_PARK_LABEL = "사람 대기(park — PR 머지됨·blocked_on 남음)"

INCIDENT_NOTES = [
    (CHECK_DAEMON, "머지된 fix가 런타임 미반영 (07-24~29 runner 구코드 168런 사고)"),
    (CHECK_LEDGER, "자동 리컨사일러(PR #26) 고장 — 머지됐는데 awaiting/failed 잔류 "
                   "(blocked_on park는 사람 대기로 별도 표기)"),
    (CHECK_FRESHNESS, "조용한 루프 침묵 (07-25~29 equity 5일 공백 사고)"),
    (CHECK_WATCHDOG, "임계-주기 불일치 재발 (07-30 일 135회 오탐 사고)"),
    (CHECK_LOGS, "삼켜지는 예외 (calendar-sync NameError 류)"),
    (CHECK_DISK, "루트 디스크 포화 — Android 런 ENOSPC 전멸 (09-19 물꼬 46, 160M 남음)"),
]

# Thresholds.
MARKET_OPEN = dt.time(9, 0)
MARKET_CLOSE = dt.time(15, 30)
FRESHNESS_GAP_DAYS = 2  # consecutive data-less closed weekdays → warn (1 = holiday, silent)
FRESHNESS_SCAN_LIMIT = 10  # closed weekdays to scan back at most
WATCHDOG_WINDOW_DAYS = 7
WATCHDOG_PER_DAY_MAX = 10  # strictly more than this on one day → warn
LOG_ERROR_THRESHOLD = 10  # new-chunk error lines per file ≥ this → warn

LEVEL_ICONS = {"ok": "✅", "warn": "⚠️", "skip": "⏭"}


@dataclass
class CheckResult:
    name: str
    level: str  # "ok" | "warn" | "skip"
    detail: str


# ---------------------------------------------------------------- judges (pure)


def stale_daemons(
    starts: dict[str, dt.datetime], code_time: dt.datetime
) -> list[str]:
    """Labels in `starts` that began before `code_time`, i.e. hold stale code.

    Sole owner of the staleness rule. todo_system/worker.py imports this by path
    to decide what to restart rather than re-deriving the comparison, because two
    copies of this rule drifting apart is precisely the class of accident the
    whole check exists to catch — one copy gets fixed and the other does not.
    """
    return sorted(label for label, started in starts.items() if started < code_time)


def judge_daemon_staleness(
    starts: dict[str, dt.datetime], code_time: dt.datetime | None
) -> CheckResult:
    """(1) A daemon whose process started before the last relevant commit is
    running old code. `starts` holds only agents with a live PID — interval
    agents without one re-exec fresh code each tick and are not at risk."""
    if code_time is None:
        return CheckResult(CHECK_DAEMON, "skip", "코드 기준시각(git log) 취득 실패")
    stale = stale_daemons(starts, code_time)
    if stale:
        return CheckResult(
            CHECK_DAEMON,
            "warn",
            f"코드 최종 커밋({code_time.astimezone(KST):%m-%d %H:%M}) 이전에 기동된 데몬: "
            + ", ".join(stale)
            + " — 재시작 필요",
        )
    if not starts:
        return CheckResult(CHECK_DAEMON, "ok", "실행 중 PID 없음(전부 인터벌 대기) — 비교 대상 없음")
    return CheckResult(CHECK_DAEMON, "ok", f"실행 중 데몬 {len(starts)}개 모두 최신 코드 이후 기동")


def _is_merged_park(row: dict) -> bool:
    """A merged row parked for a human on purpose — the ledger telling the truth.

    PR #132: when Verify named a step still owed outside the PR (`blocked_on`),
    reconcile_merged parks the merged row AWAITING_HUMAN instead of closing it,
    until a human closes the md. Only awaiting_human is exempt: the sweep
    re-parks a FAILED merged+blocked row on every tick, so one still sitting in
    failed means the reconciler is not running — the case this check is for."""
    return (row.get("pr_state") == "MERGED"
            and row.get("state") == "awaiting_human"
            and bool(row.get("blocked_on")))


def judge_ledger(rows: list[dict]) -> CheckResult:
    """(2) rows = [{slug, state, pr_state, error, blocked_on}] for
    awaiting_human/failed assignments. A MERGED pr_state means the ledger is
    lying — except a merged park (_is_merged_park), which is named under
    LEDGER_PARK_LABEL and never warns by itself. A lookup error is reported
    loudly too — the reconciler shares the gh dependency, so '점검 불능' (unable to check) may
    itself mean the safety net is dead. The detail stays one line and names
    slugs only: it is a table cell in the episode, and a blocker is a paragraph."""
    parked = [r for r in rows if _is_merged_park(r)]
    merged = [r for r in rows if r.get("pr_state") == "MERGED" and not _is_merged_park(r)]
    errors = [r for r in rows if r.get("error")]
    park = (f"{LEDGER_PARK_LABEL}: " + ", ".join(r["slug"] for r in parked)) if parked else ""
    if merged or errors:
        parts = []
        if merged:
            parts.append("머지됐는데 원장 미정리: " + ", ".join(r["slug"] for r in merged))
        if errors:
            parts.append(
                "gh 조회 실패(점검 불능): "
                + ", ".join(f"{r['slug']} ({r['error']})" for r in errors)
            )
        if park:
            parts.append(park)
        return CheckResult(CHECK_LEDGER, "warn", " / ".join(parts))
    if not rows:
        return CheckResult(CHECK_LEDGER, "ok", "awaiting_human/failed 항목 없음")
    detail = f"{len(rows)}건 점검 — 허위 원장 없음"
    return CheckResult(CHECK_LEDGER, "ok", f"{detail} · {park}" if park else detail)


def judge_equity_freshness(
    points: list[dt.datetime],
    now: dt.datetime,
    gap_days: int = FRESHNESS_GAP_DAYS,
    scan_limit: int = FRESHNESS_SCAN_LIMIT,
) -> CheckResult:
    """(3) The spec's "stale during market hours" can never fire on the Sunday
    schedule, so instead: walk back over weekdays whose market window
    (09:00–15:30 KST) has fully closed and count consecutive days without an
    in-hours point. A single holiday stays silent; ≥gap_days warns, so the
    5-silent-days incident class is caught retrospectively."""
    if not points:
        return CheckResult(CHECK_FRESHNESS, "warn", "equity_curve 데이터 포인트가 하나도 없음")
    now_kst = now.astimezone(KST)
    days_with_data = set()
    for p in points:
        p_kst = p.astimezone(KST)
        if p_kst.weekday() < 5 and MARKET_OPEN <= p_kst.time() <= MARKET_CLOSE:
            days_with_data.add(p_kst.date())
    gap = 0
    scanned = 0
    day = now_kst.date()
    while scanned < scan_limit:
        window_closed = dt.datetime.combine(day, MARKET_CLOSE, tzinfo=KST) <= now_kst
        if day.weekday() < 5 and window_closed:
            scanned += 1
            if day in days_with_data:
                break
            gap += 1
        day -= dt.timedelta(days=1)
    last_kst = max(p.astimezone(KST) for p in points)
    if gap >= gap_days:
        return CheckResult(
            CHECK_FRESHNESS,
            "warn",
            f"최근 평일 {gap}일 연속 장중(09:00–15:30 KST) 데이터 없음 — "
            f"마지막 포인트 {last_kst.isoformat(timespec='seconds')}",
        )
    return CheckResult(
        CHECK_FRESHNESS,
        "ok",
        f"장중 데이터 공백 {gap}일 (허용 {gap_days - 1}일) — "
        f"마지막 포인트 {last_kst.isoformat(timespec='seconds')}",
    )


def judge_watchdog_alerts(
    alert_times: list[dt.datetime],
    now: dt.datetime,
    per_day_max: int = WATCHDOG_PER_DAY_MAX,
    window_days: int = WATCHDOG_WINDOW_DAYS,
    warn_recent_days: int = 2,
) -> CheckResult:
    """(4) More than per_day_max ALERTs on any single day means the
    threshold/period mismatch class of noise is back (135/day on 07-30).

    Only a noisy day within the last `warn_recent_days` (today/yesterday) pages:
    this check exists to catch RECURRENCE, and an alert storm that was already
    diagnosed and fixed must not keep paging until it ages out of the 7-day
    window — the very first scheduled Sunday run would have re-warned on the
    already-fixed 07-29/30 storm, which is the alert fatigue this report was
    built to avoid. Older noisy days still show in the informational tail."""
    cutoff = now - dt.timedelta(days=window_days)
    per_day: dict[dt.date, int] = {}
    for t in alert_times:
        t_kst = t.astimezone(KST)
        if t_kst >= cutoff:
            per_day[t_kst.date()] = per_day.get(t_kst.date(), 0) + 1
    noisy = {d: c for d, c in per_day.items() if c > per_day_max}
    recent_floor = (now.astimezone(KST) - dt.timedelta(days=warn_recent_days - 1)).date()
    paging = {d: c for d, c in noisy.items() if d >= recent_floor}
    if paging:
        detail = ", ".join(f"{d.isoformat()} {c}회" for d, c in sorted(paging.items()))
        return CheckResult(
            CHECK_WATCHDOG, "warn", f"최근 {warn_recent_days}일 내 일 {per_day_max}회 초과: {detail}"
        )
    if noisy:
        detail = ", ".join(f"{d.isoformat()} {c}회" for d, c in sorted(noisy.items()))
        return CheckResult(
            CHECK_WATCHDOG, "ok",
            f"과거 폭주 이력 있음({detail}) — 최근 {warn_recent_days}일은 정상, 재발 시 다시 경고",
        )
    total = sum(per_day.values())
    peak = max(per_day.values()) if per_day else 0
    return CheckResult(
        CHECK_WATCHDOG,
        "ok",
        f"최근 {window_days}일 ALERT {total}회 (일 최대 {peak}회, 임계 {per_day_max})",
    )


def judge_log_errors(
    new_counts: dict[str, int], threshold: int = LOG_ERROR_THRESHOLD
) -> CheckResult:
    """(5) new_counts = error lines per log file in the chunk appended since
    the previous run (cursor logic lives in tail_new_error_counts)."""
    hot = {name: c for name, c in new_counts.items() if c >= threshold}
    if hot:
        detail = ", ".join(f"{name} {c}건" for name, c in sorted(hot.items()))
        return CheckResult(CHECK_LOGS, "warn", f"신규 구간 에러 급증(임계 {threshold}): {detail}")
    total = sum(new_counts.values())
    return CheckResult(
        CHECK_LOGS,
        "ok",
        f"로그 {len(new_counts)}개 신규 에러 {total}건 (파일당 임계 {threshold} 미만)",
    )


def judge_disk_free(
    usage: tuple[int, int] | None, threshold: int | None = None
) -> CheckResult:
    """(6) usage = (free, total) bytes of the filesystem under ~/Worker. The
    default threshold is the run-start guard's own trigger, read at call time
    (disk_guard.MIN_FREE_BYTES), so the WARN and the cleanup can never disagree
    on what "low" means. Below it the guard is already cleaning idle clones at
    every run start, so the next step a WARN asks for is a human's: more disk
    costs money, which is why no automation decides it."""
    if usage is None:
        return CheckResult(CHECK_DISK, "skip", "디스크 사용량 취득 실패")
    limit = disk_guard.MIN_FREE_BYTES if threshold is None else threshold
    free, total = usage
    gib = disk_guard.GIB
    detail = f"여유 {free / gib:.1f}G / 전체 {total / gib:.1f}G (임계 {limit / gib:.1f}G)"
    if free < limit:
        return CheckResult(CHECK_DISK, "warn",
                           f"{detail} — 런 시작 가드(유휴 체크아웃 gradlew clean)로 "
                           "부족하면 디스크 증설 검토")
    return CheckResult(CHECK_DISK, "ok", detail)


# ---------------------------------------------------------------- parsers (pure)

_LSTART_FMT = "%a %b %d %H:%M:%S %Y"
_WATCHDOG_ALERT_RE = re.compile(r"^\[([^\]]+)\]\s*ALERT")


def _parse_lstart(raw: str) -> dt.datetime | None:
    """`ps -o lstart=` (LC_ALL=C) → aware datetime. The naive parse is local
    wall time; .astimezone() attaches the machine's timezone to it."""
    text = " ".join(raw.split())  # day-of-month may be space-padded
    if not text:
        return None
    try:
        parsed = dt.datetime.strptime(text, _LSTART_FMT)
    except ValueError:
        return None
    return parsed.astimezone()


def _parse_watchdog_line(line: str) -> dt.datetime | None:
    """`[ISO ts] ALERT: …` → ts; heartbeat and other non-ALERT lines → None."""
    m = _WATCHDOG_ALERT_RE.match(line)
    if m is None:
        return None
    try:
        return dt.datetime.fromisoformat(m.group(1))
    except ValueError:
        return None


def _parse_equity_rows(text: str) -> list[dt.datetime]:
    """`timestamp,equity` CSV text → timestamps (header/malformed rows skipped)."""
    points = []
    for line in text.splitlines():
        ts = line.split(",", 1)[0].strip()
        if not ts:
            continue
        try:
            points.append(dt.datetime.fromisoformat(ts))
        except ValueError:
            continue
    return points


def _count_stream_errors(text: str) -> int:
    """Error-ish lines: Traceback headers + lines containing ERROR (uppercase —
    'NameError:' etc. are already covered by their traceback header)."""
    count = 0
    for line in text.splitlines():
        if line.startswith("Traceback (most recent call last") or "ERROR" in line:
            count += 1
    return count


# ---------------------------------------------------------------- renderers (pure)


def render_episode(results: list[CheckResult], now: dt.datetime) -> str:
    """Markdown episode for Memory/episodes/YYYY-MM-DD-health.md."""
    warns = [r for r in results if r.level == "warn"]
    skips = [r for r in results if r.level == "skip"]
    status = "warn" if warns else "ok"
    if warns:
        summary = f"⚠️ 이상 {len(warns)}건: " + ", ".join(r.name for r in warns)
    else:
        summary = f"✅ 전체 정상 ({len(results) - len(skips)}/{len(results)} 점검)"
    if skips:
        summary += f" · 점검 불능 {len(skips)}건"
    day = f"{now.astimezone(KST):%Y-%m-%d}"
    lines = [
        "---",
        "tags:",
        "  - memory",
        "  - episode",
        "  - health",
        f"event_date: {day}",
        f"recorded_date: {day}",
        f"status: {status}",
        "---",
        "",
        f"# {day} — 주간 헬스리포트",
        "",
        f"- 기준 시각: {now.astimezone(KST).isoformat(timespec='seconds')}",
        f"- 종합: {summary}",
        "",
        "| 점검 | 상태 | 상세 |",
        "|---|---|---|",
    ]
    for r in results:
        icon = LEVEL_ICONS.get(r.level, "❓")
        lines.append(f"| {r.name} | {icon} {r.level} | {r.detail.replace('|', '/')} |")
    lines += [
        "",
        "## 감시 대상 사고 (1:1 대응)",
        "",
        "| 점검 | 감지하는 재발 |",
        "|---|---|",
    ]
    for name, note in INCIDENT_NOTES:
        lines.append(f"| {name} | {note} |")
    lines += [
        "",
        "> 원칙: **정상이면 무음** — 카카오는 WARN이 있을 때만 발송된다.",
        "",
    ]
    return "\n".join(lines)


def build_kakao_message(results: list[CheckResult]) -> str:
    """Short alert naming the WARN checks only (healthy checks are not mentioned).

    Self-limiting via compose_alert (single length source = kakao_format): the
    headline (count + where to look) always fits, and the WARN check names ride as
    a word-clipped tail, so a run with many WARNs is never blind-cut mid-name."""
    warns = [r for r in results if r.level == "warn"]
    names = ", ".join(r.name for r in warns)
    return compose_alert(
        f"⚠️ 주간 헬스리포트: 이상 {len(warns)}건 — 상세는 Memory/episodes/*-health.md",
        tail=names)


# ---------------------------------------------------------------- IO: paths

_INSTALL_DIR = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(_INSTALL_DIR))          # bootstrap — raw install location
from todo_system import daemonctl  # noqa: E402
from todo_system import paths  # noqa: E402
# Single source of the length budget + self-limiting composer. Leaf module,
# stdlib-only, so it is safe under launchd's bare /usr/bin/python3 (no venv).
from todo_system.kakao_format import compose_alert  # noqa: E402
# The run-start disk guard owns the "disk is low" line (MIN_FREE_BYTES) and the
# measurement. Stdlib plus three stdlib-only siblings — safe under bare python3.
from todo_system.orchestrator import disk_guard  # noqa: E402
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
ASSIGNMENTS_DIR = VAULT / "Worker" / "assignments"
STATE_PATH = VAULT / "Worker" / "_state" / "health.json"  # gitignored runtime state
EPISODES_DIR = VAULT / "Memory" / "episodes"
VAULT_LOGS_DIR = VAULT / "logs"
# Engine file: resolve from the install dir, not the vault (split install).
KAKAO_SEND_SH = _INSTALL_DIR / "scripts" / "kakao_send.sh"
# Logs of the live paper run. This must look at the live-only directory, not the worker
# worktree (~/Worker/ai-stock-trading) — when the live run was split out to ~/Trading on
# 2026-08-07, the worker-side equity_curve.csv froze at that point. Pointing this at the
# wrong place makes the check raise false alarms while missing the very thing it
# watches, a 'silent live loop'.
TRADING_LOGS_DIR = Path.home() / "Trading" / "ai-stock-trading" / "logs"
EQUITY_CSV = TRADING_LOGS_DIR / "equity_curve.csv"
WATCHDOG_LOG = TRADING_LOGS_DIR / "watchdog.log"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LAUNCH_AGENT_GLOB = f"{daemonctl.SECONDBRAIN_PREFIX}*.plist"
# Config-key lookup anchored at the vault (its .macp/config.toml pins the slug);
# same file as the engine's in the combined layout, the right one in a split
# install, and never site-packages in a wheel.
FALLBACK_REPO = paths.repo_slug(start=VAULT)
LEDGER_STATES = {"awaiting_human", "failed"}
# Paths whose change means a resident daemon is running stale logic, and the sole
# definition of that set — todo_system/worker.py restarts against this very tuple.
# Docs, tasks and stage prompts are excluded on purpose: prompts are read fresh at
# each stage spawn, so editing them never needs a restart.
CODE_TIME_PATHS = ("todo_system", "scripts")


# ---------------------------------------------------------------- collectors (IO)


def _live_daemon_pids(agents_dir: Path) -> dict[str, int]:
    """label -> live PID. Mac: every <prefix>secondbrain.* LaunchAgent plist
    that launchd reports a pid for. Cloud HQ (Linux): the systemd --user units
    from todo_system.daemonctl. Idle interval jobs (no pid) are omitted."""
    live: dict[str, int] = {}
    if not daemonctl.IS_DARWIN:
        for label, pid in (daemonctl.loaded_pids() or {}).items():
            if pid:
                live[label] = pid
        return live
    uid = os.getuid()
    for plist in sorted(agents_dir.glob(LAUNCH_AGENT_GLOB)):
        label = plist.stem
        try:
            info = subprocess.run(
                ["launchctl", "print", f"gui/{uid}/{label}"],
                capture_output=True, text=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if info.returncode != 0:  # not loaded
            continue
        m = re.search(r"^\s*pid = (\d+)\b", info.stdout, re.MULTILINE)
        if m is None:  # loaded but idle (interval agent between ticks)
            continue
        live[label] = int(m.group(1))
    return live


def gather_daemon_starts(agents_dir: Path = LAUNCH_AGENTS_DIR) -> dict[str, dt.datetime]:
    """Label → process start time for <prefix>secondbrain.* agents that have
    a live PID right now. Interval agents idle between ticks re-exec fresh code
    each run, so agents without a PID are not at risk and are skipped."""
    starts: dict[str, dt.datetime] = {}
    for label, pid in _live_daemon_pids(agents_dir).items():
        try:
            ps = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                capture_output=True, text=True, timeout=15,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError):
            continue
        started = _parse_lstart(ps.stdout)
        if started is not None:
            starts[label] = started
    return starts


def gather_code_time() -> dt.datetime | None:
    """Committer time of the last commit touching the runtime code paths.

    CODE_TIME_PATHS ship with the engine, so they are read from the engine
    checkout; a split vault repo has no todo_system/ or scripts/ and git answers
    empty, which pinned the daemon check at skip. Byte-identical in the combined
    layout where VAULT == _INSTALL_DIR.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(_INSTALL_DIR), "log", "-1", "--format=%cI", "--", *CODE_TIME_PATHS],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    text = out.stdout.strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def _repo_slug_from_url(url: str | None) -> str:
    """git URL (https/ssh) → 'owner/repo' for gh --repo; fallback = vault repo."""
    if not url:
        return FALLBACK_REPO
    parts = [p for p in re.split(r"[:/]", url.strip().removesuffix(".git")) if p]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return FALLBACK_REPO


def gather_ledger_rows(assignments_dir: Path = ASSIGNMENTS_DIR) -> list[dict]:
    """awaiting_human/failed assignments → rows for judge_ledger. One gh call
    per row (the set is small). 'no pull requests found' is a legitimate
    non-anomaly (failed-before-push); every other lookup failure becomes an
    error row — fail-loud, the reconciler shares this gh dependency.
    `blocked_on` rides along because judge_ledger needs it to tell a merged
    park from a lying ledger — without it every park would still warn."""
    rows: list[dict] = []
    for path in sorted(assignments_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("state") not in LEDGER_STATES:
            continue
        slug = data.get("task_id") or path.stem
        payload = data.get("payload") or {}
        branch = payload.get("branch") or f"worker/{slug}"
        row: dict = {"slug": slug, "state": data["state"], "pr_state": None, "error": None,
                     "blocked_on": str(data.get("blocked_on") or "")}
        try:
            out = subprocess.run(
                ["gh", "pr", "view", branch,
                 "--repo", _repo_slug_from_url(payload.get("repo")),
                 "--json", "state"],
                capture_output=True, text=True, timeout=30,
            )
            if out.returncode == 0:
                row["pr_state"] = json.loads(out.stdout).get("state")
            elif "no pull requests found" not in (out.stderr or "").lower():
                stderr_lines = (out.stderr or "").strip().splitlines() or ["gh failed"]
                row["error"] = stderr_lines[0][:120]
        except FileNotFoundError:
            row["error"] = "gh 미설치"
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            row["error"] = str(e)[:120]
        rows.append(row)
    return rows


def read_equity_points(path: Path = EQUITY_CSV) -> list[dt.datetime]:
    if not path.exists():
        return []
    return _parse_equity_rows(path.read_text(encoding="utf-8", errors="replace"))


def read_watchdog_alerts(path: Path = WATCHDOG_LOG) -> list[dt.datetime]:
    if not path.exists():
        return []
    alerts = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        ts = _parse_watchdog_line(line)
        if ts is not None:
            alerts.append(ts)
    return alerts


def tail_new_error_counts(
    log_dir: Path, cursors: dict[str, int]
) -> tuple[dict[str, int], dict[str, int]]:
    """Per-file error count in the bytes appended since the stored cursor.
    First observation of a file is baseline only (count 0 — pre-existing noise
    must not fire the check); cursor > size means the file was truncated or
    rotated → rescan from 0. Returns (counts, new_cursors)."""
    counts: dict[str, int] = {}
    new_cursors: dict[str, int] = {}
    paths = sorted(set(log_dir.glob("*.err.log")) | set(log_dir.glob("*.out.log")))
    for path in paths:
        key = path.name
        try:
            size = path.stat().st_size
        except OSError:
            continue
        prev = cursors.get(key)
        if prev is None:
            counts[key] = 0
            new_cursors[key] = size
            continue
        start = 0 if prev > size else prev
        if start >= size:
            counts[key] = 0
            new_cursors[key] = size
            continue
        try:
            with open(path, "rb") as f:
                f.seek(start)
                chunk = f.read(size - start)
        except OSError:
            continue
        counts[key] = _count_stream_errors(chunk.decode("utf-8", errors="replace"))
        new_cursors[key] = size
    return counts, new_cursors


def gather_disk_usage(disk_usage=None) -> tuple[int, int] | None:
    """(free, total) of the filesystem under ~/Worker, measured exactly as the
    guard measures it — on the HQ host that is the root filesystem /tmp shares.
    None when it cannot be read."""
    return disk_guard.measure(disk_usage=disk_usage)


# ---------------------------------------------------------------- state / outputs


def load_state(path: Path = STATE_PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_episode(text: str, now: dt.datetime) -> Path:
    """Same-day rerun overwrites — the episode is a snapshot, not a journal."""
    EPISODES_DIR.mkdir(parents=True, exist_ok=True)
    path = EPISODES_DIR / f"{now.astimezone(KST):%Y-%m-%d}-health.md"
    path.write_text(text, encoding="utf-8")
    return path


def send_kakao(message: str) -> None:
    """kakao_send.sh is itself non-fatal (exits 0 on API errors); this wrapper
    only guards against the script missing or hanging."""
    try:
        subprocess.run(
            ["/bin/zsh", str(KAKAO_SEND_SH), message],
            capture_output=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"WARN kakao send failed (non-fatal): {e}", file=sys.stderr)


def _commit_episode(episode_path: Path, now: dt.datetime, push: bool = False) -> None:
    """Optional and non-fatal (session_log precedent). Not part of the launchd
    default invocation — avoids index.lock races with the orchestrator tick."""
    try:
        subprocess.run(["git", "-C", str(VAULT), "add", str(episode_path)],
                       check=True, capture_output=True, timeout=30)
        subprocess.run(
            ["git", "-C", str(VAULT), "commit", "-m",
             f"chore(health): weekly report {now.astimezone(KST):%Y-%m-%d}"],
            check=True, capture_output=True, timeout=30,
        )
        if push:
            subprocess.run(["git", "-C", str(VAULT), "push"],
                           check=True, capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"WARN episode commit/push failed (non-fatal): {e}", file=sys.stderr)


# ---------------------------------------------------------------- orchestration


def run_checks(
    now: dt.datetime, log_cursors: dict[str, int]
) -> tuple[list[CheckResult], dict[str, int]]:
    """Collect + judge all six checks. A collector crash isolates to a 'skip'
    result for that check (visible in the episode) instead of killing the run."""
    results: list[CheckResult] = []
    try:
        results.append(judge_daemon_staleness(gather_daemon_starts(), gather_code_time()))
    except Exception as e:
        results.append(CheckResult(CHECK_DAEMON, "skip", f"수집 실패: {e}"))
    try:
        results.append(judge_ledger(gather_ledger_rows()))
    except Exception as e:
        results.append(CheckResult(CHECK_LEDGER, "skip", f"수집 실패: {e}"))
    try:
        results.append(judge_equity_freshness(read_equity_points(), now))
    except Exception as e:
        results.append(CheckResult(CHECK_FRESHNESS, "skip", f"수집 실패: {e}"))
    try:
        results.append(judge_watchdog_alerts(read_watchdog_alerts(), now))
    except Exception as e:
        results.append(CheckResult(CHECK_WATCHDOG, "skip", f"수집 실패: {e}"))
    new_cursors = dict(log_cursors)
    try:
        counts, new_cursors = tail_new_error_counts(VAULT_LOGS_DIR, log_cursors)
        results.append(judge_log_errors(counts))
    except Exception as e:
        results.append(CheckResult(CHECK_LOGS, "skip", f"수집 실패: {e}"))
    try:
        results.append(judge_disk_free(gather_disk_usage()))
    except Exception as e:
        results.append(CheckResult(CHECK_DISK, "skip", f"수집 실패: {e}"))
    return results, new_cursors


def _parse_now(raw: str | None) -> dt.datetime:
    if not raw:
        return dt.datetime.now(KST)
    parsed = dt.datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="주간 시스템 헬스리포트 — 안전장치 6종 점검, 정상이면 무음"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="리포트를 stdout으로만 출력 — episode/state 쓰기·카카오 발송 없음")
    parser.add_argument("--now", help="판정 기준 시각 (ISO 8601; naive면 KST로 해석, 재현·테스트용)")
    parser.add_argument("--commit", action="store_true",
                        help="episode 파일을 git commit (launchd 기본 호출엔 미사용)")
    parser.add_argument("--push", action="store_true", help="--commit 후 push까지 (non-fatal)")
    args = parser.parse_args(argv)

    now = _parse_now(args.now)
    state = load_state()
    results, new_cursors = run_checks(now, state.get("log_cursors") or {})
    episode_text = render_episode(results, now)
    warns = [r for r in results if r.level == "warn"]

    if args.dry_run:
        print(episode_text)
        if warns:
            print(f"[dry-run] 카카오 발송 예정 메시지: {build_kakao_message(results)}")
        else:
            print("[dry-run] 전체 정상 — 카카오 무음")
        return 0

    episode_path = write_episode(episode_text, now)
    save_state({
        "last_run": now.isoformat(),
        "status": "warn" if warns else "ok",
        "results": [asdict(r) for r in results],
        "log_cursors": new_cursors,
        "episode": str(episode_path.relative_to(VAULT)),
    })
    if warns:
        send_kakao(build_kakao_message(results))
    print(f"health report: {episode_path} status={'warn' if warns else 'ok'}")
    if args.commit:
        _commit_episode(episode_path, now, push=args.push)
    return 0


if __name__ == "__main__":
    sys.exit(main())
