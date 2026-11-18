"""Claude Code limit-reset grants ("cedar_ember"): read them, pace with them, claim them.

A Claude plan can carry one-off grants that clear the usage limits ("One full limit
reset (November 2026)", "New model launch: one Fable reset", ...). The interactive
CLI offers them at the limit wall ("Use an available limit reset and keep
working"); a headless worker never sees that menu, so the grants used to expire
unused while the usage guard held the queue until the weekly reset.

This module:

* reads the grants (`status`) — the same read the CLI does,
  ``GET /api/oauth/usage?cedar_ember=1&skip_spend=1``. Eligibility is judged per
  client ("surface"), so the request carries the CLI's ``x-app: cli`` and
  ``User-Agent: claude-cli/<version> (external, cli)``; without them the answer is
  ``eligible: false, ineligible_reason: "surface"``.
* turns them into pacing inputs for ``usage_guard.evaluate`` (`pacing_inputs`):
  ``available`` full resets still usable this window and ``claimed`` resets already
  spent in it. The weekly pace line then spreads ``(1 + available + claimed)``
  windows' worth of usage over the week instead of one.
* claims one (`maybe_claim`) when the weekly limit is at the hard cap, the pace
  line allows more work, a full grant is usable now and the natural weekly reset
  is still far away. A per-model grant (e.g. the Fable launch reset) is claimed
  when that model's own weekly limit is exhausted and the general weekly window is
  still on pace. ``POST /api/organizations/{org}/reset_rate_limits`` with
  ``{program: "cedar_ember", grant_id, request_id}``, as the CLI does.

The endpoints are internal and undocumented (read from the CLI, 2026-11-17); every
call is fail-open and never raises. Off switches: ``USAGE_RESETS_DISABLE=1`` (no
read, no pacing change, no claim) and ``USAGE_RESETS_AUTOCLAIM=0`` (read and pace,
never claim).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAIM_URL = "https://api.anthropic.com/api/organizations/{org}/reset_rate_limits"
PROGRAM = "cedar_ember"
STATUS_TTL_S = 900.0                 # grants change slowly; one read per 15 min
CLAIM_MIN_HOURS_TO_RESET = 6.0       # never spend a grant this close to the natural reset
MODEL_CLAIM_MIN_HOURS_TO_RESET = 24.0
CLAIM_COOLDOWN_S = 1800.0            # after any claim attempt, wait before the next
_GRANT_ID_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
_DEFAULT_CLI_VERSION = "2.1.325"


# ---------------------------------------------------------------------------
# switches + state
# ---------------------------------------------------------------------------

def disabled() -> bool:
    return os.environ.get("USAGE_RESETS_DISABLE") == "1"


def autoclaim_enabled() -> bool:
    return not disabled() and os.environ.get("USAGE_RESETS_AUTOCLAIM", "1") not in (
        "0", "false", "no", "off")


def _state_file() -> Path:
    from . import usage_guard
    return usage_guard._state_path().with_name("resets.json")


def _load() -> dict:
    try:
        data = json.loads(_state_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(state: dict) -> None:
    try:
        p = _state_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def _parse_iso(ts) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_CLI_VERSION: str | None = None


def _cli_version() -> str:
    global _CLI_VERSION
    if _CLI_VERSION is None:
        ver = _DEFAULT_CLI_VERSION
        try:
            out = subprocess.run(["claude", "--version"], capture_output=True, text=True,
                                 timeout=20).stdout
            m = re.search(r"(\d+\.\d+\.\d+)", out or "")
            if m:
                ver = m.group(1)
        except Exception:
            pass
        _CLI_VERSION = ver
    return _CLI_VERSION


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "x-app": "cli",
        "User-Agent": f"claude-cli/{_cli_version()} (external, cli)",
    }


def _http_status(token: str) -> dict | None:
    req = urllib.request.Request(USAGE_URL + "?cedar_ember=1&skip_spend=1",
                                 headers=_headers(token))
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    ce = body.get("cedar_ember") if isinstance(body, dict) else None
    return ce if isinstance(ce, dict) else None


def _http_claim(token: str, org: str, grant_id: str) -> dict:
    payload = json.dumps({"program": PROGRAM, "grant_id": grant_id,
                          "request_id": uuid.uuid4().hex}).encode("utf-8")
    req = urllib.request.Request(CLAIM_URL.format(org=org), data=payload,
                                 headers=_headers(token), method="POST")
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _token() -> str | None:
    from . import usage_guard
    return usage_guard._read_token()


def _org_uuid() -> str | None:
    """organizationUuid of the active account (its config dir's .claude.json)."""
    candidates: list[Path] = []
    try:
        from .macp import accounts as _acc
        from .macp.config import current_account
        acct, _src = current_account()
        if acct is not None:
            candidates.append(Path(_acc.config_dir_for(acct)) / ".claude.json")
    except Exception:
        pass
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_dir:
        candidates.append(Path(env_dir) / ".claude.json")
    try:
        from .macp.accounts import legacy_config_dir
        candidates.append(Path(legacy_config_dir()) / ".claude.json")
    except Exception:
        pass
    for p in candidates:
        try:
            org = (json.loads(p.read_text(encoding="utf-8")).get("oauthAccount") or {}).get(
                "organizationUuid")
            if org:
                return str(org)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# status (cached) + pure helpers
# ---------------------------------------------------------------------------

def status(*, force: bool = False, now: float | None = None,
           fetch=None) -> dict | None:
    """The cedar_ember block, cached `STATUS_TTL_S`. None when disabled/unknown."""
    if disabled():
        return None
    now = time.time() if now is None else now
    state = _load()
    cached = state.get("status")
    if not force and cached is not None and now - float(state.get("status_at") or 0) < STATUS_TTL_S:
        return cached
    try:
        if fetch is not None:
            st = fetch()
        else:
            tok = _token()
            st = _http_status(tok) if tok else None
        if st is not None:
            state.update({"status": st, "status_at": now, "status_error": None})
            _save(state)
            return st
    except Exception as exc:
        state.update({"status_error": f"{type(exc).__name__}: {exc}"[:200], "status_at": now})
        _save(state)
    return cached


def _usable(g: dict, now: float) -> bool:
    if not isinstance(g, dict) or g.get("paused") or not g.get("usable_now"):
        return False
    if int(g.get("resets_left") or 0) <= 0:
        return False
    if not _GRANT_ID_RE.match(str(g.get("id") or "")):
        return False
    ends = _parse_iso(g.get("ends_at"))
    return ends is None or ends > now


def full_grants(st: dict | None, now: float) -> list[dict]:
    """Usable grants that clear the general weekly window ("seven_day")."""
    if not isinstance(st, dict) or st.get("eligible") is False:
        return []
    return [g for g in st.get("grants") or []
            if _usable(g, now) and "seven_day" in (g.get("clears") or [])]


def model_grants(st: dict | None, now: float) -> list[dict]:
    """Usable grants that clear a per-model weekly limit but not the general one
    (e.g. a model-launch reset that clears seven_day_overage_included)."""
    if not isinstance(st, dict) or st.get("eligible") is False:
        return []
    return [g for g in st.get("grants") or []
            if _usable(g, now) and "seven_day" not in (g.get("clears") or [])
            and "seven_day_overage_included" in (g.get("clears") or [])]


def _window_key(weekly_resets_at) -> str:
    ts = _parse_iso(weekly_resets_at) if not isinstance(weekly_resets_at, (int, float)) \
        else float(weekly_resets_at)
    return str(int(ts // 3600)) if ts else ""


def claimed_in_window(weekly_resets_at, state: dict | None = None) -> int:
    state = _load() if state is None else state
    return int((state.get("claimed") or {}).get(_window_key(weekly_resets_at), 0))


def pacing_inputs(usage: dict | None, now: float | None = None) -> dict | None:
    """{available, claimed} full resets for the weekly pace line, or None (no
    change). Reads only the cached status — the network read happens in
    `maybe_claim` (orchestrator tick), never on a runner's hot path."""
    if disabled() or os.environ.get("USAGE_RESETS_PACING", "1") in ("0", "false", "no", "off"):
        return None
    now = time.time() if now is None else now
    state = _load()
    st = state.get("status")
    weekly = (usage or {}).get("seven_day") or {}
    resets_at = weekly.get("resets_at")
    available = sum(int(g.get("resets_left") or 0) for g in full_grants(st, now))
    claimed = claimed_in_window(resets_at, state) if resets_at else 0
    if available <= 0 and claimed <= 0:
        return None
    return {"available": available, "claimed": claimed, "autoclaim": autoclaim_enabled()}


def weekly_line(elapsed_pct: float, allowance: float, available: int, claimed: int) -> float:
    """The weekly pace line in this window's utilization %, spreading
    (1 + available + claimed) windows of budget over the week. Pure."""
    mult = 1 + max(0, available) + max(0, claimed)
    return elapsed_pct * mult - 100.0 * max(0, claimed) + allowance


def hard_cap_release_at(util: float, resets_at: float, window_s: float, allowance: float,
                        available: int, claimed: int) -> float | None:
    """When a weekly hard-cap hold is expected to lift through an auto-claimed
    full reset, or None (no grant, or the claim would land too close to the
    weekly reset to be made — `plan_claim` refuses inside CLAIM_MIN_HOURS_TO_RESET).

    The claim happens once the widened pace line reaches `util`; the reset
    brings the window back to ~0%, and dispatch resumes when the line with one
    more grant claimed is back above 0 — that later point is the release. Pure."""
    if available <= 0:
        return None
    mult = 1 + available + claimed
    claim_pct = (util + 100.0 * claimed - allowance) / mult
    claim_at = resets_at - window_s * (1.0 - claim_pct / 100.0)
    if resets_at - claim_at < CLAIM_MIN_HOURS_TO_RESET * 3600.0:
        return None
    release_pct = (100.0 * (claimed + 1) - allowance) / mult
    return max(claim_at, resets_at - window_s * (1.0 - release_pct / 100.0))


# ---------------------------------------------------------------------------
# claim policy
# ---------------------------------------------------------------------------

def _model_exhausted(usage: dict) -> bool:
    for lim in (usage or {}).get("limits") or []:
        if not isinstance(lim, dict):
            continue
        scope = lim.get("scope") or {}
        if lim.get("kind") == "weekly_scoped" and (scope.get("model") or {}) and \
                float(lim.get("percent") or 0) >= 100:
            return True
    return False


def plan_claim(usage: dict, st: dict | None, now: float, *, hard_cap: float,
               allowance: float, claimed: int, last_attempt: float = 0.0) -> tuple[str, str] | None:
    """(grant_id, why) to claim now, or None. Pure."""
    if not isinstance(usage, dict) or not isinstance(st, dict):
        return None
    if now - last_attempt < CLAIM_COOLDOWN_S:
        return None
    cooldown = _parse_iso(st.get("cooldown_until"))
    if cooldown and cooldown > now:
        return None
    weekly = usage.get("seven_day") or {}
    util = weekly.get("utilization")
    resets_at = _parse_iso(weekly.get("resets_at"))
    if util is None or resets_at is None:
        return None
    util = float(util)
    hours_left = (resets_at - now) / 3600.0
    elapsed_pct = 100.0 * (1.0 - min(max(resets_at - now, 0.0), 7 * 86400.0) / (7 * 86400.0))
    fulls = full_grants(st, now)
    available = sum(int(g.get("resets_left") or 0) for g in fulls)
    line = weekly_line(elapsed_pct, allowance, available, claimed)
    on_pace = util <= line
    # 1) general weekly at the wall, on pace for the extra budget, reset far away
    if fulls and util >= hard_cap and on_pace and hours_left >= CLAIM_MIN_HOURS_TO_RESET:
        g = min(fulls, key=lambda x: _parse_iso(x.get("ends_at")) or float("inf"))
        return str(g["id"]), (f"weekly {util:.0f}% >= cap {hard_cap:.0f}%, on pace "
                              f"(line {line:.0f}%), reset in {hours_left:.0f}h")
    # 2) a model's own weekly limit exhausted while the general window has room
    models = model_grants(st, now)
    if models and _model_exhausted(usage) and util < hard_cap and on_pace \
            and hours_left >= MODEL_CLAIM_MIN_HOURS_TO_RESET:
        g = min(models, key=lambda x: _parse_iso(x.get("ends_at")) or float("inf"))
        return str(g["id"]), f"per-model weekly exhausted, general {util:.0f}% on pace"
    return None


def maybe_claim(usage: dict | None, *, now: float | None = None, fetch=None,
                claim=None, notify=None) -> dict | None:
    """Orchestrator-tick hook: claim one grant when `plan_claim` says so. Returns
    {"grant_id", "result", ...} when a claim was attempted, else None. Never raises."""
    if not autoclaim_enabled() or not usage:
        return None
    try:
        from . import usage_guard
        now = time.time() if now is None else now
        cfg = usage_guard._cfg()
        st = status(now=now, fetch=fetch)
        state = _load()
        resets_at = (usage.get("seven_day") or {}).get("resets_at")
        claimed = claimed_in_window(resets_at, state) if resets_at else 0
        plan = plan_claim(usage, st, now, hard_cap=cfg["weekly_hard_cap"],
                          allowance=cfg["weekly_allowance"], claimed=claimed,
                          last_attempt=float(state.get("last_attempt") or 0))
        if plan is None:
            return None
        grant_id, why = plan
        state["last_attempt"] = now
        _save(state)
        if claim is not None:
            resp = claim(grant_id)
        else:
            tok, org = _token(), _org_uuid()
            if not tok or not org:
                return {"grant_id": grant_id, "result": "error", "reason": "no token/org"}
            resp = _http_claim(tok, org, grant_id)
        result = str((resp or {}).get("result") or "error")
        out = {"grant_id": grant_id, "result": result, "why": why,
               "resets_left": (resp or {}).get("resets_left"),
               "cleared": (resp or {}).get("cleared") or [],
               "weekly_resets_at": (resp or {}).get("weekly_resets_at")}
        state = _load()
        hist = state.setdefault("history", [])
        hist.append({"at": now, **out})
        state["history"] = hist[-50:]
        if result == "reset":
            key = _window_key(resets_at)
            if key and "seven_day" in out["cleared"]:
                claimed_map = state.setdefault("claimed", {})
                claimed_map[key] = int(claimed_map.get(key, 0)) + 1
            state["status_at"] = 0          # re-read grants on the next tick
        _save(state)
        if result == "reset":
            try:
                usage_guard.get_snapshot(force=True, now=now)
            except Exception:
                pass
        if notify is not None:
            try:
                notify(f"🔄 Claude 한도 리셋 사용: {grant_id} → {result} ({why})")
            except Exception:
                pass
        return out
    except Exception as exc:
        return {"result": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}


def _main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="python -m todo_system.usage_resets")
    p.add_argument("cmd", choices=["status"], nargs="?", default="status")
    p.add_argument("--refresh", action="store_true")
    args = p.parse_args(argv)
    now = time.time()
    st = status(force=args.refresh, now=now)
    if st is None:
        print("limit-reset grants: unknown (disabled or not readable)")
        return 1
    print(f"eligible={st.get('eligible')} reason={st.get('ineligible_reason')} "
          f"at_limit={st.get('at_limit')} cooldown_until={st.get('cooldown_until')}")
    for g in st.get("grants") or []:
        print(f"- {g.get('id')}: {g.get('label')} — left {g.get('resets_left')}/"
              f"{g.get('resets_total')}, clears {','.join(g.get('clears') or [])}, "
              f"usable_now={g.get('usable_now')}, ends {g.get('ends_at')}")
    state = _load()
    for h in (state.get("history") or [])[-5:]:
        print(f"  claim {h.get('grant_id')} -> {h.get('result')} at "
              f"{datetime.fromtimestamp(h.get('at', 0)).isoformat(timespec='minutes')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
