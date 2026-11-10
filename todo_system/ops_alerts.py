"""Open and resolve a vault *human task* for an operational alert.

Three maintenance watchers page a person through kakao/Slack when something only
a human can fix breaks — the HQ browser profile logs out, the root disk runs low,
a Claude refresh token nears or passes its absolute expiry. A page scrolls away;
the thing that needs doing then has no home on the dashboard. This module gives
each such alert a durable `Todo/items/ops-<key>.md` task so it also shows up in
the web/app "직접 할 일" (manual) list until the situation clears.

Two APIs, both idempotent and **fail-open** (they never raise into the watcher's
real job — a vault write that fails prints one line and returns):

* ``open_alert(key, title, steps, level=None)`` — create the task when no open
  one for ``key`` exists (frontmatter ``status: open`` · ``worker_mode: manual``
  · ``due: <today>`` · tags ``todo``/``ops``/the key; body = the human steps as
  checkboxes + a Progress Log). Already open → append one ``재발생`` line, at most
  once per day. A ``done`` task for the same key is revived in place (one file
  per key, never a second).
* ``resolve_alert(key, note)`` — flip the open ``ops-<key>`` task to
  ``status: done`` and log the recovery. No task / already done → nothing (one
  ``stat`` — the steady healthy case writes nothing and prints nothing, so a
  watcher's "정상이면 무음" contract survives).

The vault root is resolved through :func:`vault_root`, with a call-time
``VAULT_ROOT`` seam mirroring :mod:`todo_system.maintenance_disk` so the suite can
point every write at a tmp dir (the ``conftest`` TRIPWIRE does not guard
``Todo/items/``). ``level`` is advisory: it is stored as a free-form ``severity``
frontmatter string and shown in the Progress Log, never as the numeric
``priority`` and never as a tag, so dashboard classification and ordering are
untouched — urgency comes from ``due: <today>`` alone.

Stdlib + :mod:`todo_system.paths` only, and 3.9-safe
(``from __future__ import annotations``): ``scripts/claude_auth_watch.py`` imports
this under macOS launchd's bare ``/usr/bin/python3``, so pulling in a third-party
or engine-upper dependency would take that watcher down. It owns the small task
format it reads back, so no YAML/frontmatter engine is imported either.
"""

from __future__ import annotations

import datetime
import os
import re
from pathlib import Path

from . import paths

# Call-time seam, like maintenance_disk.VAULT_ROOT: None → paths.vault_root();
# the suite-wide _ops_alerts_isolated fixture points it at a tmp dir so no test
# leaks an ops task into the real vault / vault-template.
VAULT_ROOT: Path | None = None

_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_DASHES_RE = re.compile(r"-{2,}")


def vault_root() -> Path:
    return Path(VAULT_ROOT) if VAULT_ROOT is not None else paths.vault_root()


def _today() -> str:
    """Local date as ``YYYY-MM-DD`` (a module function so tests can fake a day)."""
    return datetime.date.today().isoformat()


def _slug(key: str) -> str:
    """Filename- and tag-safe token: lowercase, non ``[a-z0-9-]`` → ``-``,
    collapse/strip repeats; empty → ``legacy``."""
    s = _DASHES_RE.sub("-", _SLUG_RE.sub("-", str(key or "").strip().lower())).strip("-")
    return s or "legacy"


def account_key(name: object) -> str:
    """The ops key for a Claude account's auth alert: ``claude-auth-<slug>``
    (``None`` / empty → ``claude-auth-legacy``). Keeps the per-account key building
    in one place so callers never hand-slug a real account name."""
    return f"claude-auth-{_slug(name if name else 'legacy')}"


def _items_dir() -> Path:
    return vault_root() / "Todo" / "items"


def _path(key: str) -> Path:
    return _items_dir() / f"ops-{_slug(key)}.md"


# -- the small frontmatter format this module owns ---------------------------

def _frontmatter(text: str) -> str:
    """The frontmatter block (everything before the closing ``\\n---\\n``), or ""."""
    if not text.startswith("---\n"):
        return ""
    head, sep, _ = text.partition("\n---\n")
    return head if sep else ""


def _read_field(text: str, key: str) -> str | None:
    """A scalar ``key: value`` from the frontmatter, quotes stripped, or None."""
    fm = _frontmatter(text)
    m = re.search(rf"(?m)^{re.escape(key)}:[ \t]*(.*?)[ \t]*$", fm)
    if not m:
        return None
    return m.group(1).strip().strip('"').strip("'") or None


def _set_field(text: str, key: str, value: str) -> str:
    """Replace the scalar ``key:`` line in the frontmatter (or append it), leaving
    list fields like ``tags:`` and the body untouched."""
    if not text.startswith("---\n"):
        return text
    head, sep, body = text.partition("\n---\n")
    if not sep:
        return text
    lines = head.splitlines()            # lines[0] == "---"
    out = [lines[0]]
    found = False
    i = 1
    while i < len(lines):
        line = lines[i]
        if line.split(":", 1)[0].strip() == key and ":" in line:
            out.append(f"{key}: {value}")
            found = True
            i += 1
            # drop any list-continuation lines that belonged to the old value
            while i < len(lines) and (lines[i].startswith("  - ")
                                      or lines[i].lstrip().startswith("- ")):
                i += 1
            continue
        out.append(line)
        i += 1
    if not found:
        out.append(f"{key}: {value}")
    return "\n".join(out) + "\n---\n" + body


def _render(key: str, title: str, steps, level, today: str) -> str:
    tag = _slug(key)
    steps = [str(s) for s in (steps or []) if str(s).strip()]
    if not steps:
        steps = ["상황을 확인하고 조치한다"]
    head = ["---", "tags:", "  - todo", "  - ops"]
    if tag not in ("todo", "ops"):
        head.append(f"  - {tag}")
    head += [
        "status: open",
        "worker_mode: manual",
        f"due: {today}",
        f"updated: {today}",
    ]
    if level:
        head.append(f"severity: {level}")
    head += [
        f'outcome: "{title} 상황이 해소되고 감시기가 정상을 관측하면 이 태스크가 자동으로 닫힌다"',
        f'next_action: "{steps[0]}"',
        "---",
        "",
        f"# {title}",
        "",
    ]
    head += [f"- [ ] {s}" for s in steps]
    head += [
        "",
        "## Progress Log",
        "",
        f"- {today}: 경보 발생" + (f" ({level})" if level else ""),
        "",
    ]
    return "\n".join(head) + "\n"


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file + ``os.replace`` (atomic),
    creating parents. Not wrapped — callers own the fail-open ``try``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


# -- the two public APIs -----------------------------------------------------

def open_alert(key: str, title: str, steps, level: str | None = None) -> None:
    """Open (or revive) the ``ops-<key>`` human task, idempotently. Fail-open."""
    try:
        path = _path(key)
        today = _today()
        text = None
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                text = None
        if text is not None and _read_field(text, "status") == "open":
            # already open: a 재발생 note at most once per day, no file churn otherwise
            if _read_field(text, "updated") != today:
                text = _set_field(text, "updated", today)
                text = text.rstrip("\n") + f"\n- {today}: 재발생\n"
                _atomic_write(path, text)
            return
        _atomic_write(path, _render(key, title, steps, level, today))
    except Exception as exc:  # noqa: BLE001 - a vault write must never break the watcher
        print(f"ops-alert: open({key!r}) failed — {exc}")


def resolve_alert(key: str, note: str = "") -> None:
    """Close the open ``ops-<key>`` task. No task / already done → silent no-op
    (one ``stat``, no write, no output). Fail-open."""
    try:
        path = _path(key)
        if not path.exists():
            return                       # the steady healthy case: 무음
        text = path.read_text(encoding="utf-8")
        if _read_field(text, "status") != "open":
            return                       # already resolved — no write
        today = _today()
        text = _set_field(text, "status", "done")
        text = _set_field(text, "updated", today)
        tail = f"\n- {today}: 해소" + (f" — {note}" if note else "") + "\n"
        _atomic_write(path, text.rstrip("\n") + tail)
    except Exception as exc:  # noqa: BLE001 - a vault write must never break the watcher
        print(f"ops-alert: resolve({key!r}) failed — {exc}")
