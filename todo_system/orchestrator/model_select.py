"""Resolve the Claude Code model for automated worker/planner runs.

Models are chosen per **tier**, and a task's tier comes from how hard / what
kind of work it is (`classify_tier`):

    heavy    fable → Fable 5.1 → opus → Opus 4.8   architecture-level,
                                             long, or explicitly hard tasks
    standard opus → Opus 5.5 → Opus 4.8     the default for a normal
                                             research→…→report pipeline
    light    sonnet → Sonnet 5 → opus → Opus 4.8   quick/trivial one-liners,
                                             short estimates, delegate housekeeping

Each ladder starts with the CLI's **family alias** (`fable`, `opus`, `sonnet`),
which the installed Claude Code resolves to the newest model of that family —
so when a new model ships and the CLI is upgraded (ops/claude_upgrade.py), the
workers move to it with no code change. Pinned ids follow as fallbacks in case
the newest model is not usable on this account yet. (2026-11-14: the ladders
were hard-coded ids and still ran Fable 5.1 / Sonnet 5 after Fable 5.2 and
Sonnet 5.5 shipped.)

Each ladder is walked top-down and the first model that actually runs wins
(`resolve_model(tier=...)`). Before 2026-09-10 every stage of every task ran on
the heavy ladder at `--effort max`; the tiers exist so Fable credits go to the
tasks that need them and a 15-minute tag fix does not cost a Fable pipeline.

Availability is probed with a minimal 1-turn `claude -p` call (including the
`--effort` flag, so an effort incompatibility also trips the fallback) and cached
for 24h per tier in `Worker/_state/model_pref.json`, so we never probe on every
stage. Set the `WORKER_MODEL` env var to force one model for every tier and
skip probing.

Not replaceable by the CLI's own `--fallback-model`: that covers "overloaded or
not available" only. Verified 2026-07-31 — with Fable 5 credits exhausted,
`claude -p --model claude-fable-5 --fallback-model claude-opus-5` still returned
the credits notice instead of falling back, so quota exhaustion needs this
module's own probe.

Runnable as a module — `python -m todo_system.orchestrator.model_select` prints
the resolved model id (used by scripts/planner_tick.sh).
"""
from __future__ import annotations

import json
import os
import re
import subprocess

from .. import paths
from ..claude_auth import claude_env
import time

# Family aliases: resolved by the installed CLI to the newest model of the family.
PREFERRED = "fable"
# Tried in order: the last pinned Fable (in case the newest one is not on this
# account yet), the newest Opus, the last pinned Opus, then Opus 4.8 as the floor.
FALLBACKS = ("claude-fable-5-1", "opus", "claude-opus-5-5", "claude-opus-4-8")

# Per-tier ladders, each walked top-down by resolve_model(tier=...). The heavy
# ladder IS (PREFERRED, *FALLBACKS) — those two names stay exported because
# scripts/claude_model_autoswitch.py and planner_tick.sh build on them.
# Sonnet 5 was confirmed callable on this host on 2026-09-10 (1-turn probe).
TIERS = ("heavy", "standard", "light")
LADDERS: dict[str, tuple[str, ...]] = {
    "heavy": (PREFERRED, *FALLBACKS),
    "standard": ("opus", "claude-opus-5-5", "claude-opus-4-8"),
    "light": ("sonnet", "claude-sonnet-5", "opus", "claude-opus-4-8"),
}
# `--effort` per tier. Only heavy keeps max. Effort levels are not comparable
# across generations: Anthropic reports Opus 5.5 at `medium` matching or beating
# Opus 5 at `high` on coding, and it thinks more per turn than Opus 5 at the same
# level, so standard runs at high and light at medium. Raise a tier again only
# when verify pass rates show a measured drop.
TIER_EFFORT = {"heavy": "max", "standard": "high", "light": "medium"}
# What a task gets when nothing about it says otherwise. The old behaviour was
# heavy-for-everything; flip this back to "heavy" to restore it wholesale.
DEFAULT_TIER = "standard"

# Tag words that decide a tier outright (after an explicit `model_tier:`).
_HEAVY_TAGS = frozenset({"hard", "complex", "heavy", "architecture"})
_LIGHT_TAGS = frozenset({"trivial", "quick", "light", "chore"})
# estimate_p90 thresholds, in minutes. A p90 at or under LIGHT is a light task,
# at or over HEAVY a heavy one. The template default (p90 90m) lands in the
# middle on purpose — it says nothing about difficulty.
ESTIMATE_LIGHT_MAX_MIN = 45
ESTIMATE_HEAVY_MIN_MIN = 240

# Vault-data root. start= is paths.vault_search_start(): the engine root in a
# checkout (byte-identical to the former parents[2] anchor), None in an
# installed wheel so the cwd-upward search runs instead of a site-packages anchor.
_ROOT = paths.vault_root(start=paths.vault_search_start())
_CACHE = _ROOT / "Worker" / "_state" / "model_pref.json"
_TTL = 24 * 3600
# A cached choice equal to the tier's first (most-preferred) rung keeps the full
# 24h TTL; any *other* choice — a fallback or the floor — expires after at most
# 1h so the next stage re-probes from the top of the ladder. Before this, one
# transient probe failure could pin a tier to its floor for a full day until a
# human cleared the cache (the `model-choice-does-not-self-heal` incident,
# 2026-11-10: standard·heavy stuck on the floor claude-opus-4-8).
_FLOOR_TTL = 3600


# The CLI reports an exhausted quota on stdout and still exits 0, so the exit
# code alone cannot tell "ran fine" from "refused to run". Observed 2026-07-31:
# `claude -p ok --model claude-fable-5` printed "You're out of usage credits."
# with rc=0, so _probe kept reporting Fable 5 as available, the fallback was
# unreachable, and every pipeline stage died on research for hours.
_UNAVAILABLE_MARKERS = (
    "out of usage credits",
    "usage limit",
    "rate limit",
    "insufficient credits",
    # 2026-09-01: CLI rebrand message ("You've reached your Fable 5 limit. Run
    # /usage-credits to continue…") matches none of the above — see the sibling
    # list in stages._CAPACITY_MARKERS for the incident this caused.
    "reached your",
    "/usage-credits",
)


def model_unavailable(text: str) -> bool:
    """True if `text` says the *selected model* cannot be used right now.

    Deliberately narrower than `stages.is_capacity_limit()`, which also fires on
    account-wide problems (`not logged in`, `please run /login`) and transient
    load (`overloaded`). Those are not "pick a different model" signals — a
    re-probe would just burn three more calls against the same wall.
    """
    t = (text or "").lower()
    return any(mark in t for mark in _UNAVAILABLE_MARKERS)


# The CLI's rc=1 "the model answered but wanted another turn" — proof the model
# ran, not that it is unavailable.
_MAX_TURNS_MARKER = "reached max turns"


def _probe(model: str, effort: str = "max") -> bool:
    """True if the claude CLI can actually run this model.

    Requires output that isn't a quota refusal (see `_UNAVAILABLE_MARKERS`) and
    exit 0 — or exit 1 with "Reached max turns", which means the model ran.

    Runs in an empty temporary directory. Probing from the vault (the caller's
    cwd) loaded its CLAUDE.md and hooks, so a bare "ok" often made the model
    start working, hit `--max-turns 1` and exit 1; working models were judged
    dead and tiers fell to their floor (2026-11-10, 2026-11-13: standard on
    Opus 4.8 while Opus 5.5 was fine).
    """
    import tempfile

    try:
        with tempfile.TemporaryDirectory(prefix="macp-model-probe-") as cwd:
            r = subprocess.run(
                # probe with the effort the tier will actually run at (max unless
                # a tier says otherwise) so an effort-level rejection also trips
                # the fallback.
                ["claude", "-p", "Reply with just: ok", "--model", model,
                 "--effort", effort, "--max-turns", "1"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=90, cwd=cwd,
                # 스테이지와 같은 자격증명으로 프로브해야 판정이 일치한다 —
                # 다른 토큰으로 재면 "모델 됨"이라 해놓고 실제 스테이지가 죽는다.
                # claude_env()가 활성 계정(macp.config.current_account)의 config dir과
                # automation-token을 해석하므로 프로브도 스테이지와 같은 계정으로 돈다.
                env=claude_env(),
            )
        out = f"{r.stdout or ''}\n{r.stderr or ''}"
        if model_unavailable(out):
            return False
        return r.returncode == 0 or (
            r.returncode == 1 and _MAX_TURNS_MARKER in out.lower())
    except Exception:
        return False


def probe_model(model: str) -> bool:
    """Public alias for `_probe` — one live 1-turn call, no cache, no fallback
    walk. Exists so out-of-package callers (scripts/claude_model_autoswitch.py)
    reuse this probe instead of reimplementing the rc=0-plus-quota-notice rule.
    """
    return _probe(model)


def invalidate_cache() -> bool:
    """Drop the cached model choice so the next `resolve_model()` re-probes
    instead of waiting out the 24h TTL. True if a cache file was removed.

    Best-effort by contract: never raises. It is called from the runner's stage
    loop, where an invalidation failure must not take down a pipeline that is
    otherwise fine — worst case the stale choice survives until the TTL. The
    runner's account-rotation hook (`StagedExecutor._maybe_rotate_account`)
    and `macp account use` call it too: the cache is account-agnostic, so a
    model exhausted on one account must not stay cached as dead for the next.
    """
    try:
        _CACHE.unlink()
    except Exception:
        return False    # already gone, or unremovable — either way nothing dropped
    return True


def invalidate_cache_if_unavailable(text: str) -> bool:
    """Invalidate the cache only when `text` carries a model-unavailable signal.

    The one-line entry point for callers holding raw stage output. Returns True
    only when a cache file was actually removed.
    """
    return invalidate_cache() if model_unavailable(text) else False


def _default_notify(message: str) -> None:
    """Best-effort kakao alert, imported lazily so this module stays stdlib-only
    AT IMPORT (``check_engine_isolation.sh`` archives and imports the tree, and
    ``worker`` pulls in non-stdlib deps). The 24h-per-tier throttle lives in the
    cache (``alerted_ts``), so this is a plain immediate send."""
    try:
        from .. import worker
        worker.kakao_notify(message)
    except Exception:       # noqa: BLE001 — notification is best-effort
        pass


def _read_cache() -> dict:
    try:
        c = json.loads(_CACHE.read_text(encoding="utf-8"))
        return c if isinstance(c, dict) else {}
    except Exception:
        return {}


def _cached_model(tier: str, now: float) -> str | None:
    """The tier's cached choice if it is still inside the TTL, else None.

    The file has two layouts that must both keep working: the pre-tier
    `{"model", "ts"}` (which the unblock playbook in memory still writes by
    hand) is read as the heavy tier's entry; every tier also lives under
    `tiers.<name>`, which is what new writes use."""
    c = _read_cache()
    entry = (c.get("tiers") or {}).get(tier)
    if not isinstance(entry, dict) and tier == "heavy":
        entry = c
    if not isinstance(entry, dict):
        return None
    try:
        model = entry.get("model")
        if not model:
            return None
        # A first-rung choice keys off the tier actually being read, so the
        # legacy flat entry (only read as heavy) compares against
        # LADDERS["heavy"][0] = PREFERRED and a hand-written floor there also
        # expires in 1h. Any other choice gets the short floor TTL.
        ttl = _TTL if str(model) == LADDERS[tier][0] else _FLOOR_TTL
        if now - float(entry.get("ts", 0)) < ttl:
            return str(model)
    except (TypeError, ValueError):
        pass
    return None


def _alerted_ts(tier: str) -> float:
    """The tier's last downgrade-alert timestamp (0.0 if never alerted). Stored
    in the tier's own cache entry so the 24h throttle is atomic with the choice
    write and survives the hourly floor re-probe."""
    entry = (_read_cache().get("tiers") or {}).get(tier)
    if isinstance(entry, dict):
        try:
            return float(entry.get("alerted_ts", 0))
        except (TypeError, ValueError):
            pass
    return 0.0


def _store_model(tier: str, model: str, now: float,
                 *, alerted_ts: float | None = None) -> None:
    """Write the tier's choice without clobbering the other tiers' entries.
    The heavy choice is mirrored to the legacy top-level keys so anything that
    still reads `model`/`ts` (tests, the memory playbook) sees the same value.

    `alerted_ts`, when given, records when a downgrade alert last fired for this
    tier (the 24h throttle). When None the prior value is preserved rather than
    dropped, so a routine re-probe write does not reset the throttle window."""
    try:
        c = _read_cache()
        tiers = c.get("tiers") if isinstance(c.get("tiers"), dict) else {}
        prev = tiers.get(tier) if isinstance(tiers.get(tier), dict) else {}
        entry = {"model": model, "ts": now}
        carried = alerted_ts if alerted_ts is not None else prev.get("alerted_ts")
        if carried is not None:
            entry["alerted_ts"] = carried
        tiers[tier] = entry
        c["tiers"] = tiers
        if tier == "heavy":
            c["model"], c["ts"] = model, now
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(json.dumps(c), encoding="utf-8")
    except Exception:
        pass


def resolve_model(*, use_cache: bool = True, tier: str = "heavy",
                  now: float | None = None, notify=_default_notify) -> str:
    """Return the first usable model id on `tier`'s ladder (default: the heavy
    ladder — Fable 5, else down FALLBACKS — which is what the planner and every
    pre-tier caller mean). WORKER_MODEL overrides every tier. The result is
    cached per tier (24h for the first rung, 1h for a fallback/floor so a
    transient failure self-heals) so this is cheap to call at import time. The
    last rung is returned unprobed — if even that is unusable there is nothing
    better to pick, and probing it only burns a call.

    When a *fresh probe* resolves a tier to anything other than its first rung,
    one downgrade alert is sent through `notify` (default: the lazy kakao path),
    throttled to once per tier per 24h via the cached `alerted_ts`. `now` and
    `notify` are injectable for tests (an injected clock + a fake notifier).
    """
    override = os.environ.get("WORKER_MODEL")
    if override:
        return override
    if tier not in LADDERS:
        tier = "heavy"
    if now is None:
        now = time.time()
    if use_cache:
        cached = _cached_model(tier, now)
        if cached:
            return cached
    candidates = LADDERS[tier]
    effort = TIER_EFFORT.get(tier, "max")
    model = next((m for m in candidates[:-1] if _probe(m, effort)), candidates[-1])
    # Fresh probe resolution only (never a cache hit, which returns above).
    is_floor = model != candidates[0]
    prev_alerted = _alerted_ts(tier)
    should_alert = is_floor and (now - prev_alerted >= _TTL)
    _store_model(tier, model, now,
                 alerted_ts=now if should_alert else None)
    if should_alert:
        # After the write, so a notify failure never loses the cache entry.
        notify(f"⚠️ model tier '{tier}' fell to {model} "
               f"(prefer {candidates[0]}); re-probing hourly.")
    return model


# --- task → tier classification --------------------------------------------

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([dhms])", re.I)


def estimate_minutes(text) -> float | None:
    """`estimate_p90`-style duration ("30m", "1h30m", "2h", "1d") in minutes.
    None for anything unparsable, the '-' placeholder, or an empty value — a
    bare number is *not* guessed at, it could be hours or minutes."""
    if text is None:
        return None
    s = str(text).strip().lower()
    if not s or s == "-":
        return None
    parts = _DURATION_RE.findall(s)
    if not parts or "".join(f"{n}{u}" for n, u in parts) != re.sub(r"\s+", "", s):
        return None
    scale = {"d": 24 * 60, "h": 60, "m": 1, "s": 1 / 60}
    return sum(float(n) * scale[u] for n, u in parts)


def classify_tier(payload: dict | None) -> tuple[str, str]:
    """Decide a task's model tier from its dispatch payload.

    Returns `(tier, reason)` — the reason is a short human string the runner
    logs, so a surprising choice can be traced to the field that made it.
    Precedence, first hit wins:
      1. `model_tier:` in frontmatter (heavy | standard | light) — the author's
         explicit call. An unrecognised value is ignored, not honoured.
      2. tags: hard/complex/heavy/architecture → heavy; trivial/quick/light/chore
         → light.
      3. worker_mode: quick → light (it already runs the one-call pipeline);
         delegate → standard (research / update-the-task-file, no code).
      4. estimate_p90: ≤ ESTIMATE_LIGHT_MAX_MIN → light, ≥ ESTIMATE_HEAVY_MIN_MIN
         → heavy.
      5. DEFAULT_TIER.
    """
    p = payload or {}
    explicit = str(p.get("model_tier") or "").strip().lower()
    if explicit in LADDERS:
        return explicit, f"model_tier: {explicit}"
    tags = {str(t).strip().lower() for t in (p.get("tags") or [])}
    hit = sorted(tags & _HEAVY_TAGS)
    if hit:
        return "heavy", f"tag: {hit[0]}"
    hit = sorted(tags & _LIGHT_TAGS)
    if hit:
        return "light", f"tag: {hit[0]}"
    wm = str(p.get("worker_mode") or "").strip().lower()
    if wm == "quick":
        return "light", "worker_mode: quick"
    if wm == "delegate":
        return "standard", "worker_mode: delegate"
    mins = estimate_minutes(p.get("estimate_p90"))
    if mins is not None:
        if mins <= ESTIMATE_LIGHT_MAX_MIN:
            return "light", f"estimate_p90: {p.get('estimate_p90')}"
        if mins >= ESTIMATE_HEAVY_MIN_MIN:
            return "heavy", f"estimate_p90: {p.get('estimate_p90')}"
        return DEFAULT_TIER, f"estimate_p90: {p.get('estimate_p90')} (mid-band)"
    return DEFAULT_TIER, "default"


def shift_tier(tier: str, steps: int) -> str:
    """`tier` moved `steps` rungs toward light (positive) or heavy (negative),
    clamped at the ends. Used for the stage-type dimension: a mechanical stage
    (report) runs one notch below the task's tier."""
    if tier not in TIERS:
        tier = DEFAULT_TIER
    i = min(max(TIERS.index(tier) + steps, 0), len(TIERS) - 1)
    return TIERS[i]


if __name__ == "__main__":
    import sys
    print(resolve_model(tier=sys.argv[1] if len(sys.argv) > 1 else "heavy"))
