"""Per-agent `work | game` mode — the switch that takes a heavy remote out of
the heavy-routing pool while its owner uses the machine for something else.

One small JSON state file, `Worker/agents/_state/agent_mode.json` (gitignored),
holds `{agent_id: {"mode", "since", "reason"}}`. A missing file is the normal
state and means every agent is `work`. A broken file also means `work`
(fail-open) and prints one stderr line per (path, mtime).

Readers: the orchestrator (heavy routing + reroute) and the dashboard
(`/api/status`, `/api/agent-mode`). Writers: the `agent-mode` CLI and the
dashboard POST. Writes take a best-effort `flock` on a sibling lock file around
the read-modify-write, and the file itself is replaced atomically.

stdlib-only on purpose: the dashboard imports this module and must not pull in
the orchestrator (engine-isolation).
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from contextlib import contextmanager
from pathlib import Path

from .atomic_io import atomic_write_text

WORK = "work"
GAME = "game"
MODES = (WORK, GAME)
FILENAME = "agent_mode.json"
REASON_MAX = 200

# (path, mtime_ns) pairs already warned about — one stderr line per broken
# version of the file per process, not one per read.
_warned: set[tuple[str, int]] = set()


def state_path(worker_dir: Path) -> Path:
    """`<worker_dir>/agents/_state/agent_mode.json` (worker_dir = vault `Worker/`)."""
    return Path(worker_dir) / "agents" / "_state" / FILENAME


def _warn_once(path: Path, msg: str) -> None:
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = -1
    key = (str(path), mtime)
    if key in _warned:
        return
    _warned.add(key)
    print(f"[agent_mode] {path}: {msg} — treating as work", file=sys.stderr)


def read_modes(path: Path) -> dict[str, dict]:
    """`{agent_id: {"mode", "since", "reason"}}` for the valid entries only.

    Missing file → `{}` silently. Unreadable / bad JSON / not an object → `{}`
    plus one stderr line. An entry with an unknown mode is dropped (its
    siblings are kept) with one stderr line. Never raises.
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001 - fail-open to work
        _warn_once(path, f"unreadable ({e.__class__.__name__})")
        return {}
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 - fail-open to work
        _warn_once(path, "not valid JSON")
        return {}
    if not isinstance(data, dict):
        _warn_once(path, "top level is not an object")
        return {}
    out: dict[str, dict] = {}
    bad: list[str] = []
    for agent, entry in data.items():
        if not isinstance(entry, dict) or entry.get("mode") not in MODES:
            bad.append(str(agent))
            continue
        since = entry.get("since")
        reason = entry.get("reason")
        out[str(agent)] = {
            "mode": entry["mode"],
            "since": since if isinstance(since, str) and since else None,
            "reason": reason if isinstance(reason, str) else "",
        }
    if bad:
        _warn_once(path, f"invalid entry for {', '.join(sorted(bad))}")
    return out


def get_mode(path: Path, agent_id: str) -> dict:
    """`{"agent", "mode", "since", "reason"}` — `work` / None / "" when unset."""
    e = read_modes(path).get(agent_id) or {}
    return {"agent": agent_id, "mode": e.get("mode", WORK),
            "since": e.get("since"), "reason": e.get("reason", "")}


def is_game(modes: dict | None, agent_id: str) -> bool:
    """Is `agent_id` in game mode according to a `read_modes()` result?"""
    try:
        return bool(modes) and (modes.get(agent_id) or {}).get("mode") == GAME
    except Exception:  # noqa: BLE001 - fail-open to work
        return False


def clean_reason(reason) -> str:
    """Printable characters only, at most REASON_MAX of them."""
    s = reason if isinstance(reason, str) else ""
    return "".join(ch for ch in s if ch.isprintable())[:REASON_MAX]


@contextmanager
def _locked(path: Path):
    """Best-effort exclusive lock on `<path>.lock` (no-op without fcntl)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import fcntl
    except ImportError:  # non-POSIX: atomic replace still holds
        yield
        return
    try:
        fh = open(path.with_name(path.name + ".lock"), "a+")
    except OSError:
        yield
        return
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except OSError:
            pass
        yield
    finally:
        fh.close()       # closing releases the flock


def set_mode(path: Path, agent_id: str, mode: str, reason: str = "", *,
             now_iso: str | None = None) -> dict:
    """Set `agent_id` to `mode`; returns `{agent, mode, since, reason, changed}`.

    Setting the mode it already has keeps `since` (`changed=False`); a given
    non-empty reason still replaces the old one. Raises ValueError on a bad
    mode or an empty agent id, and OSError when the file cannot be written.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {'|'.join(MODES)}, got {mode!r}")
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise ValueError("agent id is empty")
    path = Path(path)
    reason = clean_reason(reason)
    with _locked(path):
        data = read_modes(path)
        cur = data.get(agent_id) or {}
        old_mode = cur.get("mode", WORK)
        changed = old_mode != mode
        if changed or not cur.get("since"):
            since = now_iso or dt.datetime.now().astimezone().isoformat(timespec="seconds")
        else:
            since = cur["since"]
        if not changed and not reason:
            reason = cur.get("reason", "")
        data[agent_id] = {"mode": mode, "since": since, "reason": reason}
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2,
                                           sort_keys=True) + "\n")
    return {"agent": agent_id, "mode": mode, "since": since, "reason": reason,
            "changed": changed}
