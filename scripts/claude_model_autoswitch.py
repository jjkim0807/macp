#!/usr/bin/env python3
"""Keep the legacy single config dir's `settings.json` pointed at a model that can actually run.

When the selected model's credits run out, interactive Claude Code sessions keep
using it and fail on every turn until a human notices and runs `/model`. This
script is the unattended half of that recovery: probe, and if the configured
model is dead, rewrite exactly one key.

Design notes:
- stdlib only, so launchd's `/usr/bin/python3` (no site-packages) can run it —
  same constraint as scripts/macp_monitor.py and todo_system/session_log.
- The probe is NOT reimplemented here. `model_select.probe_model` already
  encodes the rule that matters (the CLI announces an exhausted quota on stdout
  and still exits 0, so rc alone cannot tell "ran" from "refused"). Divergence
  between the two surfaces would be silent, so there is one definition.
- Deliberately not `model_select.resolve_model()`: that always walks from
  PREFERRED and overwrites the *worker's* shared 24h cache. This daemon needs
  the opposite order — confirm the currently-configured model first and stop —
  so a healthy host costs exactly one probe per tick.
- Also unlike `resolve_model()`, the last fallback is never returned unprobed.
  Returning an unverified floor is right for the worker (it must pick something)
  and wrong here: it would write a known-dead id into the user's settings.
- State lives in Worker/_state/ (git-ignored), never next to this tracked
  script, and is kept SEPARATE from the worker's model_pref.json so a daemon
  tick cannot silently redirect worker stages.

Usage:
  python3 scripts/claude_model_autoswitch.py               # a launchd tick
  python3 scripts/claude_model_autoswitch.py --dry-run -v  # decide, write nothing
  python3 scripts/claude_model_autoswitch.py --settings /tmp/s.json   # tests

Quiet unless something happened: a tick that confirms the current model prints
nothing (use -v), so an idle system leaves no daemon-log noise.

Exit codes: 0 = healthy (kept or switched), 1 = nothing written (settings
unreadable/unparseable, or every candidate model is unavailable).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_INSTALL_DIR = Path(__file__).resolve().parents[1]  # …/MaestroACP (scripts/ -> vault)
sys.path.insert(0, str(_INSTALL_DIR))               # bootstrap — raw install location

from todo_system import paths  # noqa: E402  (needs sys.path above)
from todo_system.macp.accounts import legacy_config_dir  # noqa: E402
from todo_system.orchestrator.model_select import (  # noqa: E402
    FALLBACKS,
    PREFERRED,
    invalidate_cache,
    probe_model,
)

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
ROOT = paths.vault_root(start=paths.vault_search_start())
# settings.json is shared across accounts (SHARED_ENTRIES symlink), so the legacy
# single config dir reaches the shared copy — value-identical to the former
# Path.home() / ".claude" / "settings.json".
DEFAULT_SETTINGS = legacy_config_dir() / "settings.json"
DEFAULT_STATE = ROOT / "Worker" / "_state" / "claude_model_autoswitch.json"

# How long to stay on a fallback before spending one probe to check whether
# PREFERRED has recovered. At the installer's 1800s tick that is one upgrade
# probe per 12 ticks — checking every tick would burn quota to learn nothing.
UPGRADE_TTL = 6 * 3600


def load_state(path: Path) -> dict:
    """Last tick's decision. Any unreadable/corrupt state is treated as empty —
    it is a cache of timing, never a source of truth."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def save_state(path: Path, model: str, now: float, last_upgrade_ts: float) -> None:
    """Best-effort: a state write failure must not fail the tick that already
    fixed settings.json."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"model": model, "ts": now, "last_upgrade_ts": last_upgrade_ts},
            indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


def load_settings(path: Path) -> tuple[dict, str]:
    """Return (parsed, raw_text). Raises ValueError with a human-readable reason
    if the file cannot be used — callers must then write NOTHING. Hand-edited
    settings with a syntax error are the case this protects: overwriting them
    would destroy work the user has not saved anywhere else.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ValueError(f"{path} does not exist — refusing to create it") from None
    except OSError as e:
        raise ValueError(f"cannot read {path}: {e}") from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"{path} is not valid JSON ({e}) — leaving it untouched") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path} top level is {type(data).__name__}, expected object")
    return data, raw


def _is_preferred(model: str) -> bool:
    """`model` names PREFERRED, with or without a `[…]` suffix such as `[1m]`."""
    return model == PREFERRED or model.startswith(PREFERRED + "[")


def choose_model(current: str | None, state: dict, now: float) -> dict:
    """Pick the model settings.json should name.

    Returns {"model": str|None, "probed": {id: bool}, "upgrade_attempted": bool};
    `model` is None only when every candidate is unavailable, which means "write
    nothing" rather than "pick the least-bad dead one".
    """
    probed: dict[str, bool] = {}

    def alive(model: str) -> bool:
        # memoized so one tick never probes the same id twice — at most 3 calls.
        # probe_model is looked up as a module global on each call so tests can
        # substitute it without ever reaching the real CLI.
        if model not in probed:
            probed[model] = bool(probe_model(model))
        return probed[model]

    upgrade_attempted = False
    # (1) periodically try to climb back up: a fallback is a demotion, not a home.
    # PREFERRED is a family alias, so "fable[1m]" (alias + context suffix) is
    # already the preferred family and must not be rewritten to bare "fable".
    if current and not _is_preferred(current) and now - float(state.get("last_upgrade_ts") or 0) >= UPGRADE_TTL:
        upgrade_attempted = True
        if alive(PREFERRED):
            return {"model": PREFERRED, "probed": probed, "upgrade_attempted": True}
    # (2) the configured model still works — the common case, and it stops here
    if current and alive(current):
        return {"model": current, "probed": probed, "upgrade_attempted": upgrade_attempted}
    # (3) walk the ladder; `alive` memoization skips anything probed above
    for model in (PREFERRED, *FALLBACKS):
        if alive(model):
            return {"model": model, "probed": probed, "upgrade_attempted": upgrade_attempted}
    return {"model": None, "probed": probed, "upgrade_attempted": upgrade_attempted}


def write_settings(path: Path, data: dict, raw: str, model: str) -> None:
    """Replace only the `model` key: back up, mutate the parsed dict (so every
    unknown key survives verbatim), then swap atomically via a same-directory
    temp file — a crash can leave the old file or the new one, never a truncated
    one. Callers must skip this entirely when the value is unchanged.
    """
    path.with_name(path.name + ".bak").write_text(raw, encoding="utf-8")
    data["model"] = model
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()    # no-op after a successful replace; cleans up a failed one
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Point the legacy single-dir settings.json at a model that is actually usable.")
    ap.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS,
                    help="settings.json to maintain (default: %(default)s)")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE,
                    help="tick state file (default: %(default)s)")
    ap.add_argument("--dry-run", action="store_true",
                    help="probe and report the decision, write nothing")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="also report ticks that change nothing")
    args = ap.parse_args(argv)

    try:
        data, raw = load_settings(args.settings)
    except ValueError as e:
        print(f"claude-model-autoswitch: {e}", file=sys.stderr)
        return 1

    current = data.get("model")
    current = current if isinstance(current, str) and current else None
    now = time.time()
    state = load_state(args.state)
    decision = choose_model(current, state, now)
    chosen = decision["model"]

    if args.verbose:
        for model, ok in decision["probed"].items():
            print(f"probe {model}: {'ok' if ok else 'unavailable'}")

    if chosen is None:
        print("claude-model-autoswitch: every candidate model is unavailable — "
              f"leaving {current or 'settings.json'} untouched", file=sys.stderr)
        return 1

    last_upgrade_ts = now if decision["upgrade_attempted"] else float(state.get("last_upgrade_ts") or 0)

    if chosen == current:
        # No write at all: rewriting identical bytes would churn the mtime and
        # make `ls -l ~/.claude` imply a change nobody made.
        if args.verbose:
            print(f"claude-model-autoswitch: {current} still available — no change")
        if not args.dry_run:
            save_state(args.state, chosen, now, last_upgrade_ts)
        return 0

    if args.dry_run:
        print(f"DRY_RUN: would switch {current or '(unset)'} -> {chosen} in {args.settings}")
        return 0

    write_settings(args.settings, data, raw, chosen)
    # Tell the worker too: it caches its own choice for 24h, and it is the same
    # account hitting the same wall.
    invalidate_cache()
    save_state(args.state, chosen, now, last_upgrade_ts)
    print(f"claude-model-autoswitch: switched {current or '(unset)'} -> {chosen}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
