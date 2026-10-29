"""Proactive Claude Code usage guard — pace automation so it never exhausts
the subscription quota before the next refill.

Why: in late July 2026 the pipeline burned through the entire weekly quota days
before the reset, leaving nothing for interactive use. Existing protections are
all *reactive* — `model_select` probes discover an already-exhausted model, and
the orchestrator's capacity cooldown backs off only after a run has failed on
the limit. This module is *proactive*: it reads the account's live utilization
from the same endpoint Claude Code's `/usage` uses and tells callers to stop
STARTING new automated work when either

  - a window is above its hard cap (leave headroom for the human on the same
    subscription), or
  - utilization is ahead of the pace line (`elapsed% + allowance`) — i.e. on
    track to exhaust before `resets_at`.

Blocking is advisory and self-clearing: callers (worker run-next, orchestrator
dispatch, runner poll, planner tick) simply skip a cycle; nothing is failed or
poisoned, and as the window's clock advances the pace line catches up and the
guard opens again. In-flight work is never interrupted; collect/replies keep
flowing.

Auth: reuses Claude Code's own OAuth token — the `.credentials.json` in the
**active account's** config dir (`macp.config.current_account`: project pin →
profile active_account → priority). With no account configured the legacy
single dir (`macp.accounts.LEGACY_CONFIG_DIR`; module constant
`_CREDENTIALS_FILE`) is read, and only then is the macOS Keychain item
"Claude Code-credentials" consulted first — a Keychain item is not split per
CLAUDE_CONFIG_DIR, so for an account dir it could answer for another account.
No refresh is attempted here; Claude Code refreshes its token on use. A 401
yields a stale snapshot — and, for the active account, marks it unavailable for
auth (`macp.accounts.mark_unavailable`, sticky) unless the stored token had
merely expired (`expiresAt` already past: the CLI refreshes it on its next use,
and a stage run as that account still works), so a stale token is never
mistaken for a lost login.

Accounts (leaf 4 of macp-multi-claude-account): every claude account of the
profile gets its own snapshot under `accounts.<name>` ({fetched_at, usage,
error, fail_count, next_retry_at, decision}) in the same state file, while the
top-level `usage` / `decision` / `windows` stay the ACTIVE account's — the
pre-existing format, byte-compatible. The active account is refreshed by
`get_snapshot()` exactly as before (one `_fetch_usage()` per TTL, `fetch.lock`);
the inactive ones only by `refresh_inactive_accounts()` — a separate entry
point the orchestrator's `usage_hold()` calls — each with its own token, lock
(`fetch.<name>.lock`), failure backoff and a TTL `INACTIVE_TTL_FACTOR` times
the active one. `check()` additionally returns a hold (`allowed: False`,
`paused: True`, reason "all accounts unavailable: …") while
`macp.accounts.all_unavailable()` is true, and lifts it by itself once an
account is usable again (window reset, re-login, `macp account use`).

Fail policy: **fail-open, but with memory.** If usage cannot be fetched (expired
token, offline, 429) the guard allows work — a guard outage must not stall the
whole control plane (cf. the dirty-vault stall class of incidents). The last
good snapshot is honored up to `USAGE_GUARD_STALE_OK_S`; beyond that the guard
reports `no-data`. Crucially it does NOT thereby forget a block it had already
decided: while the previous decision's `blocked_until` is still in the future
the hold stands (`carried: True`). Without that, the endpoint's 429 — which
arrives precisely when utilization is high — reopened the gate exactly when the
hard cap was supposed to hold. Set `USAGE_GUARD_FAIL_CLOSED=1` to block on
no-data outright.

State: `Worker/_state/usage_guard/latest.json` (git-ignored) holds the last
snapshot + the last decision, for the dashboard and macp_monitor to read.

CLI (used by shell ticks):
    python -m todo_system.usage_guard check [--refresh]   # exit 0 allow, 3 block
    python -m todo_system.usage_guard status              # human-readable
"""
from __future__ import annotations

import email.utils
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:  # POSIX-only; the guard keeps the module importable on non-POSIX hosts
    import fcntl
except ImportError:
    fcntl = None

from . import paths
from .macp.accounts import legacy_config_dir as _legacy_config_dir

# Vault-data root. start= is paths.vault_search_start(): the engine root in a
# checkout (byte-identical to the former parents[1] anchor), None in an
# installed wheel so the cwd-upward search runs instead of a site-packages anchor.
ROOT = paths.vault_root(start=paths.vault_search_start())

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_KEYCHAIN_SERVICE = "Claude Code-credentials"
# The legacy single-dir credentials file, fixed at import time as before (the
# monkeypatch seam); `_credentials_file()` is the resolver `_read_token` uses.
_CREDENTIALS_FILE = _legacy_config_dir() / ".credentials.json"

SESSION_WINDOW_S = 5 * 3600
WEEKLY_WINDOW_S = 7 * 86400

# Exit code for "blocked" so shell gates can distinguish it from real errors.
EXIT_BLOCKED = 3


def _cfg() -> dict:
    """Thresholds, env-overridable (read per call so a resident daemon picks up
    changes without a restart — see the stages.py import-time-constant lesson).

    - hard_cap: block outright above this utilization %, until the window
      resets. Weekly defaults lower than session because a blown weekly locks
      the account for days and the human shares it.
    - allowance: how far (percentage points) utilization may run AHEAD of the
      elapsed fraction of the window before pacing kicks in. Session is loose
      (bursts recover within 5h); weekly is tight (that is the limit that was
      actually exhausted).
    """
    def f(name: str, default: float) -> float:
        try:
            return float(os.environ.get(name, default))
        except (TypeError, ValueError):
            return default

    return {
        "session_hard_cap": f("USAGE_GUARD_SESSION_HARD_CAP", 90.0),
        # 2026-08-10: 80 -> 90. At 80 the queue stalled ~35h with three ready
        # tasks while a fifth of the week's budget was still unused; one of them
        # was the fix a live paper-trading run was waiting on. The cap exists to
        # leave the human room on the same account, and 10% covers that.
        #
        # A real trade, not a free win: the closer to 100, the likelier automation
        # eats the budget before the week ends and leaves nothing for interactive
        # work. If a week ever runs dry mid-week, put this back first.
        "weekly_hard_cap": f("USAGE_GUARD_WEEKLY_HARD_CAP", 90.0),
        "session_allowance": f("USAGE_GUARD_SESSION_ALLOWANCE", 30.0),
        "weekly_allowance": f("USAGE_GUARD_WEEKLY_ALLOWANCE", 12.0),
        "fetch_ttl_s": f("USAGE_GUARD_TTL_S", 240.0),
        # 2026-09-27: 1h -> 3h. The endpoint's rate limit is shared with every
        # Claude CLI on the account (the CLI polls /api/oauth/usage itself), so
        # 429s with Retry-After ~1h are routine; at 1h the guard spent those
        # stretches in no-data and the planner's aggressive mode never fired.
        "stale_ok_s": f("USAGE_GUARD_STALE_OK_S", 10800.0),
        # Failure backoff: after a failed fetch, wait before re-probing. Grows
        # exponentially per consecutive failure from base, capped at max, unless
        # the response carried a Retry-After (then that wins). The endpoint 429s
        # exactly while utilization is high, so re-probing every tick amplifies
        # the very outage it is reacting to.
        "backoff_base_s": f("USAGE_GUARD_BACKOFF_BASE_S", 60.0),
        "backoff_max_s": f("USAGE_GUARD_BACKOFF_MAX_S", 1800.0),
        "fail_closed": os.environ.get("USAGE_GUARD_FAIL_CLOSED") == "1",
        # Parallel pacing (see parallel_slots): each full `step` of slack below a
        # window's gate line buys one more concurrent run. Weekly is the scarce
        # window, so its step is the finest. Session was 15pp, which held the
        # runner at 2 slots early in every 5h window even with the week ~97%
        # unused (2026-10-12); its own pace/hard-cap gate already stops runaway
        # use, so the step only needs to taper fan-out near that line.
        "session_parallel_step": f("USAGE_GUARD_SESSION_PARALLEL_STEP", 8.0),
        "weekly_parallel_step": f("USAGE_GUARD_WEEKLY_PARALLEL_STEP", 5.0),
        # Opt-in minimum-progress mode (orchestrator `_min_progress_dispatch`):
        # during a *pace* hold with nothing live, still dispatch the single top
        # priority task. Hard-cap / paused / no-data holds stay full holds. Off
        # by default so the plain hold behaviour is unchanged.
        "min_progress": os.environ.get("USAGE_GUARD_MIN_PROGRESS", "0").strip().lower()
                        in ("1", "true", "yes", "on"),
    }


def min_progress_enabled() -> bool:
    """Whether `USAGE_GUARD_MIN_PROGRESS` is on (read per call). Never raises."""
    try:
        return bool(_cfg()["min_progress"])
    except Exception:
        return False


def _state_path() -> Path:
    override = os.environ.get("USAGE_GUARD_STATE_DIR")
    base = Path(override) if override else ROOT / "Worker" / "_state" / "usage_guard"
    return base / "latest.json"


# ---------------------------------------------------------------------------
# token + fetch
# ---------------------------------------------------------------------------


def _credentials_file() -> tuple[Path, bool]:
    """``(path, keychain_allowed)`` for the OAuth credential: the active
    account's ``.credentials.json`` (``macp.config.current_account``) with the
    Keychain lookup disabled — a Keychain item is not per CLAUDE_CONFIG_DIR, so
    it could answer for another account — or, with no account, an account on the
    legacy single dir, or any resolver error, the legacy ``_CREDENTIALS_FILE``
    (the monkeypatch seam) with the Keychain allowed first, as before. Never
    raises; function-local imports keep the import-time footprint unchanged."""
    try:
        from .macp import accounts as _acc
        from .macp.config import current_account
        acct, _source = current_account()
        if acct is not None:
            d = _acc.config_dir_for(acct)
            if not _acc.is_legacy_config_dir(d):
                return _acc.credentials_file_for(acct), False
    except Exception:
        pass
    return _CREDENTIALS_FILE, True


def _read_token() -> str | None:
    """Claude Code's OAuth access token for the active account: the macOS
    Keychain (legacy single dir only), else the account's credentials file
    (`_credentials_file`). Returns None when neither yields one."""
    return _read_token_from(*_credentials_file())


def _read_token_for(acct) -> str | None:
    """The OAuth access token of one specific account (`_read_token` is the
    active one): its `.credentials.json`, the Keychain allowed only when the
    account lives on the legacy single dir — the same rule as above."""
    from .macp import accounts as _acc
    return _read_token_from(_acc.credentials_file_for(acct),
                            _acc.is_legacy_config_dir(_acc.config_dir_for(acct)))


def _read_token_from(path: Path, keychain: bool) -> str | None:
    """`(path, keychain_allowed)` → access token or None (the body `_read_token`
    always had; split out so the per-account reader shares it byte for byte)."""
    if keychain:
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout)
                token = (data.get("claudeAiOauth") or {}).get("accessToken")
                if token:
                    return token
        except Exception:
            pass
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (data.get("claudeAiOauth") or {}).get("accessToken")
    except Exception:
        return None


def _fetch_usage() -> dict:
    """One GET against the oauth usage endpoint as the active account. Raises
    on any failure. The no-argument seam every caller and test patches."""
    return _request_usage(_read_token())


def _fetch_usage_for(acct) -> dict:
    """`_fetch_usage` for one specific (inactive) account — a separate seam so
    `get_snapshot()` / `check()` never reach it. Raises on any failure."""
    return _request_usage(_read_token_for(acct))


def _request_usage(token: str | None) -> dict:
    if not token:
        raise RuntimeError("no Claude Code OAuth token found")
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# snapshot cache
# ---------------------------------------------------------------------------


def _load_state() -> dict:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass  # state persistence is best-effort; the decision was already made


def _parse_retry_after(value: str | None, now: float) -> float | None:
    """A Retry-After header value -> absolute epoch to retry at, or None when
    absent / unparseable. Accepts delta-seconds ("120") or an HTTP-date
    (RFC 7231, e.g. "Wed, 21 Oct 2026 07:28:00 GMT"). Never raises."""
    if not value:
        return None
    value = value.strip()
    try:
        return now + max(float(value), 0.0)
    except (TypeError, ValueError):
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:  # a naive HTTP-date is UTC by spec
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _acquire_fetch_lock(name: str | None = None):
    """Best-effort, non-blocking inter-process lock so several daemons
    (orchestrator/runner/planner/worker) sharing one state file do not all fetch
    at once. Returns a (status, fd) pair:

      ("acquired", fd)      -> this process may fetch; release fd when done
      ("busy", None)        -> another process is fetching; serve cached state
      ("unavailable", None) -> no fcntl or a lock error -> fetch anyway (fail-open)

    The lock is an open-file-description flock on `<state_dir>/fetch.lock` —
    the active account's, taken with no argument as always — or on
    `<state_dir>/fetch.<name>.lock` for an inactive account's refresh.
    """
    if fcntl is None:
        return ("unavailable", None)
    try:
        lock_path = _state_path().parent / (f"fetch.{name}.lock" if name else "fetch.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    except Exception:
        return ("unavailable", None)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return ("acquired", fd)
    except BlockingIOError:
        os.close(fd)
        return ("busy", None)
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        return ("unavailable", None)


def _release_fetch_lock(fd) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except Exception:
        pass


def _apply_fetch_failure(target: dict, exc: BaseException, cfg: dict, now: float) -> None:
    """Record a failed fetch on a snapshot dict (the top-level state, or one
    account's entry): the error, the consecutive-failure count and the backoff
    window — Retry-After when the response carried one, else the exponential
    delay capped at `backoff_max_s`. The arithmetic `get_snapshot` always had,
    shared with the inactive-account refresh."""
    fail_count = int(target.get("fail_count") or 0) + 1
    retry_after = None
    if isinstance(exc, urllib.error.HTTPError) and exc.headers:
        retry_after = _parse_retry_after(exc.headers.get("Retry-After"), now)
    if retry_after is not None:
        next_retry_at = retry_after
    else:
        delay = min(cfg["backoff_base_s"] * (2 ** (fail_count - 1)),
                    cfg["backoff_max_s"])
        next_retry_at = now + delay
    target.update({"attempted_at": now,
                   "error": f"{type(exc).__name__}: {exc}",
                   "fail_count": fail_count, "next_retry_at": next_retry_at})


def _is_http_401(exc: BaseException) -> bool:
    return isinstance(exc, urllib.error.HTTPError) and getattr(exc, "code", None) == 401


def _credentials_expired(acct) -> bool | None:
    """Whether the account's stored access token had already expired on its own
    (`claudeAiOauth.expiresAt`, epoch milliseconds or seconds) — None when the
    file or the field cannot be read."""
    try:
        from .macp import accounts as _acc
        data = json.loads(_acc.credentials_file_for(acct).read_text(encoding="utf-8"))
        exp = (data.get("claudeAiOauth") or {}).get("expiresAt")
        if exp is None:
            return None
        exp = float(exp)
        if exp > 1e12:            # milliseconds
            exp /= 1000.0
        return exp <= time.time()
    except Exception:
        return None


def _mark_active_auth_failed(exc: BaseException) -> None:
    """A 401 from the usage endpoint for the ACTIVE account marks it unavailable
    for auth (`macp.accounts.mark_unavailable`, sticky: `resolve_active` skips
    it from the next call on and `check()` pauses when nothing is left) — unless
    the stored access token had simply expired: the CLI refreshes it on its next
    use, and a stage run as that account still works (with an automation token
    it does not even read this file), so an expired token is a stale snapshot,
    not a lost login. Inactive accounts are never marked from here for the same
    reason (their token is refreshed by nobody). Never raises."""
    try:
        if not _is_http_401(exc):
            return
        from .macp import accounts as _acc
        _cands, active = _account_context()
        if active is None or _credentials_expired(active):
            return
        if _acc.unavailable_entry(active) is not None:
            return                       # already marked — keep the original stamp
        _acc.mark_unavailable(active.name, "auth", config_dir=_acc.config_dir_for(active))
    except Exception:
        pass


def get_snapshot(*, force: bool = False, now: float | None = None) -> dict:
    """Return the cached state ({'fetched_at', 'usage', 'error', 'fail_count',
    'next_retry_at', ...}), refreshing usage when older than the fetch TTL.

    On fetch failure the previous usage is kept, 'error' records what happened,
    and a backoff window (`next_retry_at`) throttles re-probing so a failing
    endpoint is not hit on every tick — Retry-After is honored when present,
    otherwise the delay grows exponentially per consecutive failure up to the
    cap. A fresh success clears the counter and window. `force` (CLI --refresh)
    bypasses both the TTL and the backoff."""
    now = time.time() if now is None else now
    cfg = _cfg()
    state = _load_state()
    fetched_at = float(state.get("fetched_at") or 0)
    if not force and state.get("usage") and now - fetched_at < cfg["fetch_ttl_s"]:
        return state
    # Failure backoff: still inside the window from the last failed fetch -> serve
    # the cached state without re-probing. `force` overrides; success clears it.
    try:
        next_retry_at = float(state.get("next_retry_at") or 0)
    except (TypeError, ValueError):
        next_retry_at = 0.0
    if not force and now < next_retry_at:
        return state
    # Single-fetch lock: only one process probes the endpoint at a time; the rest
    # serve their cached state. A missing fcntl / any lock error is fail-open.
    lock_status, lock_fd = _acquire_fetch_lock()
    if lock_status == "busy":
        return state
    try:
        try:
            usage = _fetch_usage()
            state.update({"fetched_at": now, "attempted_at": now, "usage": usage,
                          "error": None, "fail_count": 0, "next_retry_at": 0})
        except Exception as exc:
            _apply_fetch_failure(state, exc, cfg, now)
            _mark_active_auth_failed(exc)      # 401 → sticky auth mark (see the docstring)
        _save_state(state)
    finally:
        _release_fetch_lock(lock_fd)
    return state


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


def _parse_iso(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts).timestamp()
    except ValueError:
        return None


def _iso_utc(ts: float | None) -> str | None:
    """Epoch -> ISO 8601 UTC string (seconds precision), None-safe."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def pace_release_at(util: float, resets_at: float, window_s: float,
                    allowance: float) -> float:
    """When a pace hold lifts: the moment elapsed% + allowance catches up to
    `util` (linear in the window clock), or the window reset if that comes
    first — a window that cannot catch up before it resets is released by the
    reset. Pure."""
    catch_up = resets_at - window_s * (1.0 - (util - allowance) / 100.0)
    return min(catch_up, resets_at)


def _eval_window(name: str, info: dict | None, window_s: float,
                 hard_cap: float, allowance: float, now: float) -> dict:
    """Decision for one rolling window (session/weekly).

    elapsed% is derived from resets_at: start = resets_at - window. Pacing
    blocks while util > elapsed% + allowance; the block lifts by itself as the
    window clock advances (blocked_until = the catch-up time), so no state is
    needed to recover.
    """
    out = {"name": name, "utilization": None, "resets_at": None,
           "elapsed_pct": None, "blocked": False, "reason": None,
           "blocked_until": None}
    # An absent weekly block stays "missing": the scarce window must never read
    # as empty on a shape change. The 5h session idles for hours every night.
    if (info is None and name == "session") or \
            (isinstance(info, dict) and info.get("resets_at") is None):
        # No reset time = no window running: nothing has been used since the
        # last one closed, so it is maximal headroom, not missing data. Report
        # it as a window about to start (0% elapsed, 0% used unless the endpoint
        # said otherwise). Read as "no data", the planner's aggressive mode
        # stayed off in exactly the idle hours it exists for (2026-09-27).
        util = (info or {}).get("utilization")
        out.update({"utilization": float(util or 0.0), "elapsed_pct": 0.0})
        return out
    if not isinstance(info, dict):
        return out
    util = info.get("utilization")
    resets_at = _parse_iso(info.get("resets_at"))
    if util is None or resets_at is None:
        return out
    util = float(util)
    remaining = min(max(resets_at - now, 0.0), window_s)
    elapsed_pct = 100.0 * (1.0 - remaining / window_s)
    out.update({"utilization": util, "resets_at": resets_at,
                "elapsed_pct": round(elapsed_pct, 1)})
    if util >= hard_cap:
        out.update({
            "blocked": True, "blocked_until": resets_at,
            "reason": f"{name} {util:.0f}% >= hard cap {hard_cap:.0f}% "
                      f"(until reset)",
        })
        return out
    if util > elapsed_pct + allowance:
        # catch-up time: when elapsed% + allowance == util
        out.update({
            "blocked": True,
            "blocked_until": pace_release_at(util, resets_at, window_s, allowance),
            "reason": f"{name} {util:.0f}% ahead of pace "
                      f"({elapsed_pct:.0f}% elapsed + {allowance:.0f}pp allowance)",
        })
    return out


def evaluate(usage: dict, now: float | None = None) -> dict:
    """Pure policy: usage payload -> decision. No I/O (unit-testable)."""
    now = time.time() if now is None else now
    cfg = _cfg()
    windows = [
        _eval_window("session", usage.get("five_hour"), SESSION_WINDOW_S,
                     cfg["session_hard_cap"], cfg["session_allowance"], now),
        _eval_window("weekly", usage.get("seven_day"), WEEKLY_WINDOW_S,
                     cfg["weekly_hard_cap"], cfg["weekly_allowance"], now),
    ]
    blocked = [w for w in windows if w["blocked"]]
    until = max((w["blocked_until"] for w in blocked), default=None)
    return {
        "allowed": not blocked,
        "reason": "; ".join(w["reason"] for w in blocked) or None,
        "blocked_until": until,
        "release_eta": _iso_utc(until),
        "windows": windows,
        "checked_at": now,
    }


def hold_kind(decision: dict | None) -> str | None:
    """Classify a decision's hold. Pure; never raises.

    - None: not a hold (allowed, or the guard is disabled).
    - "paused": every account unavailable (`_paused_decision`).
    - "no_data": a carried no-data hold, or a fail-closed one with no windows.
    - "hard_cap": some blocked window is at/over its hard cap.
    - "pace": blocked windows exist and none is at its hard cap.
    - "unknown": anything else (e.g. a hand-built `{"allowed": False}`).

    Derived from the existing window fields plus `_cfg()`, so the decision
    shape stays as it was.
    """
    try:
        if not decision or decision.get("disabled") or decision.get("allowed", True):
            return None
        if decision.get("paused"):
            return "paused"
        if decision.get("carried"):
            return "no_data"
        windows = decision.get("windows")
        if isinstance(windows, list) and not windows:
            return "no_data"
        blocked = [w for w in windows or [] if isinstance(w, dict) and w.get("blocked")]
        if not blocked:
            return "unknown"
        cfg = _cfg()
        kinds = set()
        for w in blocked:
            name = w.get("name")
            util = w.get("utilization")
            if "hard cap" in str(w.get("reason") or ""):
                kinds.add("hard_cap")
            elif name not in ("session", "weekly") or util is None:
                kinds.add("unknown")
            elif float(util) >= cfg[f"{name}_hard_cap"]:
                kinds.add("hard_cap")
            else:
                kinds.add("pace")
        for k in ("hard_cap", "unknown"):
            if k in kinds:
                return k
        return "pace"
    except Exception:
        return "unknown"


def window_slack(w: dict, cfg: dict | None = None) -> float | None:
    """Percentage points between a window's utilization and the line the guard
    would block at (`min(elapsed% + allowance, hard_cap)`), floored at 0. Pure.

    None when the window is not session/weekly or its numbers are missing, so
    callers can tell "no evidence" apart from "no headroom". Shared by
    `parallel_slots` and the planner's aggressive-mode verdict
    (todo_system/orchestrator/planner_mode.py) so both read headroom the same way.
    """
    name = w.get("name")
    if name not in ("session", "weekly"):
        return None
    if w.get("utilization") is None or w.get("elapsed_pct") is None:
        return None
    cfg = cfg or _cfg()
    line = min(float(w["elapsed_pct"]) + cfg[f"{name}_allowance"],
               cfg[f"{name}_hard_cap"])
    return max(line - float(w["utilization"]), 0.0)


def parallel_slots(decision: dict, max_slots: int) -> int:
    """How many runs may execute at once under `decision`. Pure (unit-testable).

    The gate above is binary — start work or don't — so a week with most of its
    budget unused still ran one task at a time while dispatched work queued for
    hours behind it. This widens the gate when there is room: per window, the
    slack is the distance from utilization to the line the guard would block at
    (`min(elapsed% + allowance, hard_cap)`), and every full `*_parallel_step` of
    it earns one more slot. The tighter window decides; `max_slots` caps it.

    0 when blocked (the existing hold). 1 whenever the numbers can't be trusted —
    no windows (fail-open no-data) or a stale snapshot — so an outage never
    fans out: parallelism is only ever granted on fresh evidence of headroom.
    """
    if max_slots <= 0 or not decision.get("allowed", True):
        return 0
    if max_slots == 1:
        return 1
    windows = [w for w in decision.get("windows") or []
               if w.get("utilization") is not None and w.get("elapsed_pct") is not None]
    if not windows or decision.get("stale"):
        return 1
    cfg = _cfg()
    slots = max_slots
    for w in windows:
        slack = window_slack(w, cfg)
        if slack is None:
            continue
        step = cfg[f"{w['name']}_parallel_step"]
        earned = 1 + int(slack // step) if step > 0 else 1
        slots = min(slots, earned)
    return max(1, slots)


def _no_data_decision(state: dict, cfg: dict, now: float) -> dict:
    """Decision for "usage cannot be read" — fail-open, but never *forget*.

    Plain fail-open reopened the gate exactly when it mattered: the usage
    endpoint answers 429 precisely while utilization is high, so on 2026-08-24
    the weekly hard cap was announced at 90% and then dropped twice on 429s,
    letting the week ride to 100%. A guard outage still must not stall the
    control plane (cf. the 35h queue stall behind the old 80% cap), so the
    compromise is: keep honoring the block we already knew about, and only fall
    open once its own `blocked_until` has passed. That timestamp is absolute —
    the hold expires on schedule whether or not the endpoint ever recovers.
    """
    err = state.get("error") or "never fetched"
    prior = state.get("decision") or {}
    if prior.get("paused"):
        # A PAUSED hold (`_paused_decision`) is recomputed from the runtime state
        # on every tick; only the usage block underneath it is worth carrying
        # across no-data ticks — otherwise `macp account use` could not lift the
        # pause until the exhausted window's own deadline.
        prior = prior.get("underlying") or {}
    # Visibility fields, kept OUT of `reason` (test_repeated_no_data_ticks_… pins
    # `reason` invariant across ticks): last good snapshot age, last error, and
    # the next scheduled retry so `status`/dashboard can tell idle from HOLD from
    # blind instead of showing a blank window.
    last_error = state.get("error")
    try:
        fetched_at = float(state.get("fetched_at") or 0)
    except (TypeError, ValueError):
        fetched_at = 0.0
    age_s = (now - fetched_at) if fetched_at else None
    try:
        next_retry_at = float(state.get("next_retry_at") or 0) or None
    except (TypeError, ValueError):
        next_retry_at = None
    try:
        until = prior.get("blocked_until")
        until = float(until) if until is not None else None
    except (TypeError, ValueError):
        until = None
    if until is not None and now < until:
        # `held_reason` carries the ORIGINAL cause across repeated no-data
        # cycles; reading `reason` here would nest it one deeper each tick.
        held = prior.get("held_reason") or prior.get("reason") or "unknown"
        return {
            "allowed": False,
            "reason": f"no usage data ({err}) — holding last known block: {held}",
            "held_reason": held,
            "blocked_until": until,
            "release_eta": _iso_utc(until),
            "windows": prior.get("windows") or [],
            "checked_at": now, "stale": True, "carried": True,
            "age_s": age_s, "last_error": last_error, "next_retry_at": next_retry_at,
        }
    return {
        "allowed": not cfg["fail_closed"],
        "reason": f"no usage data ({err}) — "
                  + ("fail-closed" if cfg["fail_closed"] else "fail-open"),
        "blocked_until": None, "release_eta": None, "windows": [], "checked_at": now,
        "stale": True,
        "age_s": age_s, "last_error": last_error, "next_retry_at": next_retry_at,
    }


# ---------------------------------------------------------------------------
# per-account snapshots + the PAUSED hold (leaf 4)
# ---------------------------------------------------------------------------

# An inactive account's snapshot may be this many active TTLs old before it is
# refreshed — nobody dispatches on it, so a slow picture is enough.
INACTIVE_TTL_FACTOR = 4
_ACCOUNT_SNAPSHOT_KEYS = ("fetched_at", "attempted_at", "usage", "error",
                          "fail_count", "next_retry_at")


def _account_context() -> tuple[list, object | None]:
    """``(the profile's claude accounts, the active one or None)`` for the cwd —
    the anchor `_credentials_file()` resolves too — with the runtime state
    applied (`macp.accounts.resolve_active`: an unavailable active account is
    skipped exactly as the runner skips it). ``([], None)`` on any resolver
    error or without a profile / account: every per-account path is then inert,
    which is the pre-existing behaviour. Function-local imports keep the
    import-time footprint unchanged."""
    try:
        from .macp import accounts as _acc
        from .macp.config import resolve
        ctx = resolve()
        prof = ctx.get("profile")
        cands = [a for a in (getattr(prof, "accounts", None) or [])
                 if getattr(a, "agent", "claude") == "claude"]
        active, _source = _acc.resolve_active(prof, ctx.get("project"),
                                              state=_acc.load_runtime_state())
        return cands, active
    except Exception:
        return [], None


def _accounts_map(state: dict) -> dict:
    accounts = state.get("accounts")
    return accounts if isinstance(accounts, dict) else {}


def exhausted_until(name: str, now: float | None = None) -> float | None:
    """When account ``name``'s quota comes back, read from the last snapshot in
    the state file (``accounts.<name>.usage``, else the top-level ``usage`` when
    ``name`` is the recorded active account): the latest ``resets_at`` among the
    session / weekly windows at or above their hard cap, as epoch seconds. None
    when no window is over the cap or nothing is known — the runner then holds
    the account for the default 5h (a model-scoped limit such as "reached your
    Fable 5 limit" shows up in neither window, so it gets the default too). A
    reset already in the past is ignored. Pure read, never raises."""
    now = time.time() if now is None else now
    try:
        state = _load_state()
        entry = _accounts_map(state).get(name)
        usage = entry.get("usage") if isinstance(entry, dict) else None
        if not isinstance(usage, dict) and state.get("account") == name:
            usage = state.get("usage")
        if not isinstance(usage, dict):
            return None
        cfg = _cfg()
        best = None
        for key, cap in (("five_hour", cfg["session_hard_cap"]),
                         ("seven_day", cfg["weekly_hard_cap"])):
            info = usage.get(key)
            if not isinstance(info, dict) or info.get("utilization") is None:
                continue
            resets = _parse_iso(info.get("resets_at"))
            if resets is None or resets <= now:
                continue
            if float(info["utilization"]) >= cap and (best is None or resets > best):
                best = resets
        return best
    except Exception:
        return None


def _refresh_account_entry(acct, cfg: dict, now: float, *, force: bool = False) -> dict:
    """Refresh one claude account's ``accounts.<name>`` snapshot entry and return
    it: fetch with that account's own token (`_read_token_for`), its own lock
    (`fetch.<name>.lock`), its own failure backoff, and a TTL of
    `INACTIVE_TTL_FACTOR` × the active TTL, then read-merge-write only its own
    entry. `force` bypasses both the TTL and the backoff (the `check(account=)`
    entry point uses it). This is the exact per-account body
    `refresh_inactive_accounts` always ran, extracted verbatim so the leaf-4
    refresh stays byte-identical. Never raises outside the caller's try."""
    name = acct.name
    prior = _accounts_map(_load_state()).get(name)
    ent = dict(prior) if isinstance(prior, dict) else {}
    try:
        fetched_at = float(ent.get("fetched_at") or 0)
    except (TypeError, ValueError):
        fetched_at = 0.0
    if not force and ent.get("usage") and now - fetched_at < cfg["fetch_ttl_s"] * INACTIVE_TTL_FACTOR:
        return ent
    try:
        next_retry_at = float(ent.get("next_retry_at") or 0)
    except (TypeError, ValueError):
        next_retry_at = 0.0
    if not force and now < next_retry_at:
        return ent
    lock_status, lock_fd = _acquire_fetch_lock(name)
    if lock_status == "busy":
        return ent
    try:
        try:
            usage = _fetch_usage_for(acct)
            ent.update({"fetched_at": now, "attempted_at": now, "usage": usage,
                        "error": None, "fail_count": 0, "next_retry_at": 0})
            ent["decision"] = evaluate(usage, now)
        except Exception as exc:
            _apply_fetch_failure(ent, exc, cfg, now)
        # Re-read before writing: check() may have saved the active
        # snapshot meanwhile, and this must only replace its own entry.
        fresh = _load_state()
        accounts = _accounts_map(fresh)
        accounts[name] = ent
        fresh["accounts"] = accounts
        _save_state(fresh)
    finally:
        _release_fetch_lock(lock_fd)
    return ent


def refresh_inactive_accounts(now: float | None = None) -> dict:
    """Refresh the snapshot of every claude account that is NOT the active one
    — its own token (`_read_token_for`), its own lock (`fetch.<name>.lock`), its
    own failure backoff, and a TTL of `INACTIVE_TTL_FACTOR` × the active TTL —
    and store it under ``accounts.<name>`` ({fetched_at, attempted_at, usage,
    error, fail_count, next_retry_at, decision}). A separate entry point on
    purpose: `get_snapshot()` / `check()` keep fetching the active account alone
    through `_fetch_usage()`, exactly as before, and `check()` mirrors the
    active account's entry itself. The orchestrator's `usage_hold()` calls this
    after `check()`. Returns ``{name: entry}`` for the inactive accounts (`{}`
    without a profile). Never raises: a failed fetch is recorded in the entry
    (fail-open), and a 401 here marks nothing (see `_mark_active_auth_failed`)."""
    now = time.time() if now is None else now
    out: dict = {}
    try:
        cfg = _cfg()
        cands, active = _account_context()
        inactive = [a for a in cands if active is None or a.name != active.name]
        for acct in inactive:
            out[acct.name] = _refresh_account_entry(acct, cfg, now)
    except Exception:
        pass
    return out


def _paused_decision(cands, decision: dict, now: float) -> dict:
    """The hold `check()` returns while every account is unavailable. Keeps the
    decision's keys (`allowed` False, `reason`, `blocked_until` = the earliest
    timed mark to lift, or None when only sticky marks remain, `windows`,
    `checked_at`, `stale`) and adds `paused: True`, `accounts_unavailable`
    ({name: reason}) and `underlying` — the usage decision it replaced, which
    `_no_data_decision` carries instead of the pause itself."""
    from .macp import accounts as _acc
    runtime = _acc.load_runtime_state()
    reasons: dict[str, str] = {}
    untils: list[float] = []
    for acct in cands:
        entry = _acc.unavailable_entry(acct, runtime, now)
        if entry is None:
            continue
        reasons[acct.name] = str(entry.get("reason") or "unavailable")
        until = _acc.until_epoch(entry)
        if until is not None:
            untils.append(until)
    until = min(untils) if untils else None
    return {
        "allowed": False,
        "reason": "all accounts unavailable: "
                  + ", ".join(f"{n}({r})" for n, r in reasons.items()),
        "paused": True,
        "blocked_until": until,
        "release_eta": _iso_utc(until),
        "windows": decision.get("windows") or [],
        "checked_at": now,
        "stale": bool(decision.get("stale")),
        "accounts_unavailable": reasons,
        "underlying": decision,
    }


def _apply_account_state(state: dict, decision: dict, now: float) -> dict:
    """Mirror the active account's snapshot under ``accounts.<active>`` (plus
    the `account` name) and return the PAUSED hold instead of `decision` when
    `macp.accounts.all_unavailable()` says no account is usable — reconciling
    the runtime state's `paused_since` on the way, so the hold lifts by itself
    the tick an account comes back. With no profile / account nothing is
    touched. Called inside `check()`'s own try/except: any error leaves the
    usage decision as it was (fail-open)."""
    from .macp import accounts as _acc
    cands, active = _account_context()
    if active is not None:
        state["account"] = active.name
        accounts = _accounts_map(state)
        mirror = {k: state.get(k) for k in _ACCOUNT_SNAPSHOT_KEYS}
        mirror["decision"] = decision
        accounts[active.name] = mirror
        state["accounts"] = accounts
    if cands and _acc.all_unavailable(cands, now=now, reconcile=True):
        return _paused_decision(cands, decision, now)
    return decision


def _persist_account_decision(name: str, decision: dict) -> None:
    """Store `decision` under ``accounts.<name>.decision``, read-merge-write so a
    concurrent active `check()` / inactive refresh only has its own entry
    touched. Best-effort, like `_save_state`."""
    try:
        fresh = _load_state()
        accounts = _accounts_map(fresh)
        prior = accounts.get(name)
        ent = dict(prior) if isinstance(prior, dict) else {}
        ent["decision"] = decision
        accounts[name] = ent
        fresh["accounts"] = accounts
        _save_state(fresh)
    except Exception:
        pass


def _check_account(name: str, *, force: bool, now: float) -> dict:
    """`check()` for an arbitrary account by name (the `account=` arm). The
    active account — or an unknown/absent profile — delegates to the no-argument
    `check()` so the active path stays byte-identical; any other claude account
    of the profile is refreshed with its own token/lock/backoff
    (`_refresh_account_entry`) and evaluated, the decision persisted under
    ``accounts.<name>.decision``. Never raises: an unknown account or any error
    yields the fail-open no-data decision (≈1 slot)."""
    try:
        cands, active = _account_context()
        if active is not None and active.name == name:
            return check(force=force, now=now)
        acct = next((a for a in cands if a.name == name), None)
        if acct is None:
            return _no_data_decision({"error": f"unknown account {name!r}"}, _cfg(), now)
        cfg = _cfg()
        ent = _refresh_account_entry(acct, cfg, now, force=force)
        usage = ent.get("usage")
        try:
            fetched_at = float(ent.get("fetched_at") or 0)
        except (TypeError, ValueError):
            fetched_at = 0.0
        if usage and now - fetched_at <= cfg["stale_ok_s"]:
            decision = evaluate(usage, now)
            decision["stale"] = now - fetched_at > cfg["fetch_ttl_s"] * INACTIVE_TTL_FACTOR
        else:
            decision = _no_data_decision(ent, cfg, now)
        _persist_account_decision(name, decision)
        return decision
    except Exception as exc:        # belt&braces — check() never raises
        return _no_data_decision({"error": f"{type(exc).__name__}: {exc}"}, _cfg(), now)


def check(*, account: str | None = None, force: bool = False,
          now: float | None = None) -> dict:
    """Fetch (cached) + evaluate + persist the decision. Never raises.

    Returns the decision dict; `allowed` is the only key callers must honor.
    `paused: True` (leaf 4) marks the hold for "every account unavailable" —
    see `_apply_account_state`.

    `account` (leaf: multi-account capacity) evaluates a NAMED claude account
    instead of the active one — its own token, 429 backoff, stale and fail-open
    rules, all independent per `accounts.<name>` (see `_check_account`). The
    active account (or `account=None`, the default) runs the byte-identical path
    below.
    """
    if os.environ.get("USAGE_GUARD_DISABLE") == "1":
        # Explicit off switch (README quick start, API-billed setups): no fetch,
        # no state write, never a hold. Read per call like _cfg(), so a resident
        # daemon follows the variable without a restart.
        return {"allowed": True, "disabled": True,
                "reason": "usage guard disabled (USAGE_GUARD_DISABLE=1)"}
    now = time.time() if now is None else now
    if account is not None:
        return _check_account(account, force=force, now=now)
    cfg = _cfg()
    try:
        state = get_snapshot(force=force, now=now)
    except Exception as exc:  # get_snapshot shouldn't raise, but belt&braces
        state = {"error": f"{type(exc).__name__}: {exc}"}
    usage = state.get("usage")
    fetched_at = float(state.get("fetched_at") or 0)
    if usage and now - fetched_at <= cfg["stale_ok_s"]:
        decision = evaluate(usage, now)
        decision["stale"] = now - fetched_at > cfg["fetch_ttl_s"]
    else:
        decision = _no_data_decision(state, cfg, now)
    try:
        decision = _apply_account_state(state, decision, now)
    except Exception:
        pass
    state["decision"] = decision
    _save_state(state)
    return decision


# ---------------------------------------------------------------------------
# multi-account capacity: slots summed across usable accounts
# ---------------------------------------------------------------------------


def _decision_slack(decision: dict) -> float | None:
    """The tightest window slack of a decision (`min window_slack`), or None when
    no window has usable numbers — the headroom used to rank accounts."""
    slacks = [s for s in (window_slack(w) for w in (decision.get("windows") or []))
              if s is not None]
    return min(slacks) if slacks else None


def account_slots(per_account_max: int, global_cap: int | None = None,
                  now: float | None = None) -> dict:
    """Parallel run slots available right now across every usable claude account
    of the profile (the runner's fan-out sizing). Read-only except the ACTIVE
    account's own `check()` — one fetch per TTL, exactly as the single-account
    guard always did; inactive accounts are read from their stored
    ``accounts.<name>.decision`` snapshot (kept warm by
    `refresh_inactive_accounts`), never fetched here, so the 15s poll does not
    hammer the endpoint.

    A *usable* account = authed + no live unavailable mark + guard-allowed. Each
    contributes `parallel_slots(decision, per_account_max)`; the sum is cut by
    `global_cap` when it is not None. One claude account with `global_cap` None
    is byte-identical to the former single-account sizing (`parallel_slots(check(),
    per_account_max)`), so the single-account path never changes.

    Returns ``{"total": int, "per_account": {name: slots}, "slack": {name:
    slack|None}, "order": [names with slots>0, widest slack first — ties by
    priority, then listing order], "active": name|None}``. `order` is the
    assignment preference. Never raises: any error returns an empty cap
    (total 0, no accounts), which the runner reads as "no account model" and
    falls back to its pre-existing, account-agnostic dispatch."""
    now = time.time() if now is None else now
    out: dict = {"total": 0, "per_account": {}, "slack": {}, "order": [],
                 "active": None}
    try:
        from .macp import accounts as _acc
        cands, active = _account_context()
        if not cands:
            return out
        out["active"] = active.name if active is not None else None
        state = _acc.load_runtime_state()
        usable = {a.name for a in _acc.available_accounts(cands, state, now)}
        stored = _accounts_map(_load_state())
        ranked: list[tuple] = []
        for idx, acct in enumerate(cands):
            name = acct.name
            if name not in usable:            # live unavailable mark → no slots
                continue
            if active is not None and name == active.name:
                decision = check(now=now)     # the active account: fetch (TTL-cached)
            else:
                ent = stored.get(name)
                decision = ent.get("decision") if isinstance(ent, dict) else None
                if not isinstance(decision, dict):
                    # no warm snapshot yet → fail-open 1 slot (never fan out on
                    # no evidence), the same floor parallel_slots gives a stale one.
                    decision = {"allowed": True, "windows": [], "stale": True}
            slots_a = parallel_slots(decision, per_account_max)
            slack_a = _decision_slack(decision)
            out["per_account"][name] = slots_a
            out["slack"][name] = slack_a
            if slots_a > 0:
                prio = int(getattr(acct, "priority", 100) or 100)
                sort_slack = slack_a if slack_a is not None else float("-inf")
                ranked.append((-sort_slack, prio, idx, name))
        total = sum(out["per_account"].values())
        if global_cap is not None:
            total = min(total, global_cap)
        out["total"] = total
        out["order"] = [name for _s, _p, _i, name in sorted(ranked)]
    except Exception:
        return {"total": 0, "per_account": {}, "slack": {}, "order": [],
                "active": None}
    return out


# ---------------------------------------------------------------------------
# runner live-slots snapshot (dashboard / `macp account list` read it)
# ---------------------------------------------------------------------------


def runner_slots_path() -> Path:
    """Path of the runner's live-slots snapshot: ``RUNNER_SLOTS_STATE`` when set,
    else ``<vault>/Worker/_state/runner_slots.json`` (git-ignored, derived)."""
    override = os.environ.get("RUNNER_SLOTS_STATE")
    return Path(override).expanduser() if override else \
        ROOT / "Worker" / "_state" / "runner_slots.json"


def write_runner_slots(snapshot: dict) -> None:
    """Best-effort persist of the runner's live-slots snapshot — never raises, so
    a write hiccup can never sink the poll loop."""
    try:
        p = runner_slots_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def read_runner_slots() -> dict:
    """The runner's last live-slots snapshot, or ``{}`` (fail-soft, never raises)."""
    try:
        d = json.loads(runner_slots_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _fmt_ts(epoch: float | None) -> str:
    if not epoch:
        return "-"
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone().strftime(
        "%m-%d %H:%M")


def _print_account_lines() -> None:
    """`status`: one line per account snapshot (`accounts.<name>`), with its
    live unavailable mark. Nothing when there are no per-account entries."""
    try:
        accounts = _accounts_map(_load_state())
        if not accounts:
            return
        from .macp import accounts as _acc
        cands, active = _account_context()
        by_name = {a.name: a for a in cands}
        runtime = _acc.load_runtime_state()
        for name, ent in sorted(accounts.items()):
            usage = (ent or {}).get("usage") or {}
            sess = (usage.get("five_hour") or {}).get("utilization")
            week = (usage.get("seven_day") or {}).get("utilization")
            mark = ""
            entry = _acc.unavailable_entry(by_name[name], runtime) if name in by_name else None
            if entry is not None:
                until = _acc.until_epoch(entry)
                mark = (f" [unavailable: {entry.get('reason')} "
                        f"{'until ' + _fmt_ts(until) if until else 'sticky'}]")
            tag = " (active)" if active is not None and active.name == name else ""
            print(f"account {name}{tag}: session={sess if sess is not None else '?'}% "
                  f"weekly={week if week is not None else '?'}%{mark}")
    except Exception:
        pass


def _main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m todo_system.usage_guard")
    sub = p.add_subparsers(dest="cmd", required=True)
    pc = sub.add_parser("check", help="exit 0 = ok to spend, 3 = blocked")
    pc.add_argument("--refresh", action="store_true", help="bypass fetch TTL")
    sub.add_parser("status", help="print windows + decision")
    args = p.parse_args(argv)

    d = check(force=getattr(args, "refresh", False))

    if args.cmd == "check":
        if d["allowed"]:
            print(f"usage-guard: OK ({d.get('reason') or 'within pace'})")
            return 0
        print(f"usage-guard: BLOCKED — {d['reason']} "
              f"(retry ~{_fmt_ts(d.get('blocked_until'))})")
        return EXIT_BLOCKED

    # status
    windows = d.get("windows", [])
    for w in windows:
        util = w["utilization"]
        print(f"{w['name']:8s} util={util if util is not None else '?':>5}% "
              f"elapsed={w['elapsed_pct']}% "
              f"resets={_fmt_ts(w['resets_at'])} "
              f"{'BLOCKED: ' + w['reason'] if w['blocked'] else 'ok'}")
    if not windows:
        # 429 / no-data: show the last good snapshot instead of nothing, so idle
        # vs paced-HOLD vs blind is distinguishable at a glance.
        usage = (_load_state().get("usage") or {})
        sess = (usage.get("five_hour") or {}).get("utilization")
        week = (usage.get("seven_day") or {}).get("utilization")
        age_s = d.get("age_s")
        age = f"{age_s / 3600:.1f}h ago" if age_s is not None else "never fetched"
        print(f"last good: session={sess if sess is not None else '?'}% "
              f"weekly={week if week is not None else '?'}% ({age})")
        if d.get("last_error"):
            print(f"last error: {d['last_error']}")
        if d.get("next_retry_at"):
            print(f"next retry: {_fmt_ts(d['next_retry_at'])}")
    _print_account_lines()
    verdict = "ALLOWED" if d["allowed"] else f"BLOCKED — {d['reason']}"
    stale = " (stale data)" if d.get("stale") else ""
    print(f"decision: {verdict}{stale}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
