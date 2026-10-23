"""Self-limiting alert composition — the ONE source of the length budget.

Since the Slack-only cutover the default channel is Slack, whose practical text
limit is generous (~4000 chars), so :data:`KAKAO_TEXT_LIMIT` now tracks that
budget. Kakao stays as a rollback-only backend: its "나에게 보내기" memo text caps
around 200 chars (Korean included), and ``scripts/kakao_send.sh`` still cuts
anything longer at 179 + "…" as a last-resort safety net on the kakao curl path
only — a separate net at a different layer, left in place for the rollback route.
That cut is *blind*: it will happily bisect a sha, a URL, or the very sentence
that tells a human what to do — and a truncated sha still looks like a usable one,
so the person copies it and fails (see the entity
``macp-kakao-message-silent-truncation``). The fix is to make every alert compose
itself to fit BEFORE it reaches the script:

* the **headline** (line 1: what happened + what to do) always goes out whole —
  it carries the reason the alert exists, so it must survive any length;
* **whole optional lines** are appended only when they fit in their entirety, so
  a value (sha / command / path) is never cut in half;
* a single free-text **tail** (a worker's English note) is clipped on a WORD
  boundary to whatever budget is left, never mid-token.

stdlib-only ON PURPOSE: ``scripts/health_report.py`` and
``scripts/claude_auth_watch.py`` run under launchd's ``/usr/bin/python3`` (no uv,
no venv, no PyYAML), so this module must never import a third-party package. It is
a near-leaf: the only things it imports from ``todo_system`` are the stdlib-only
``paths`` resolver (which itself pulls in no third-party or project package) and
``i18n`` (the message table, which pulls only ``paths``), so it still loads under
bare ``/usr/bin/python3``. ``human`` re-exports
:data:`KAKAO_TEXT_LIMIT` so orchestrator-side callers keep the single name
``human.KAKAO_TEXT_LIMIT`` to reference, while the two launchd scripts import this
leaf directly.
"""

from __future__ import annotations

import datetime as _dt
import json as _json
import os as _os
import time as _time
from collections.abc import Iterable
from pathlib import Path

from . import i18n, paths

# Slack's practical text budget (~4000 chars) now that Slack is the default
# backend; the name is kept for compatibility (human.KAKAO_TEXT_LIMIT re-exports
# it and the launchd scripts import it, so a change here moves every caller at
# once). This is the compose-stage budget in Python. It is NOT the same net as
# kakao_send.sh's last-resort 179 + "…" cut: that one lives at the kakao curl
# layer and stays as the rollback-only kakao safety net, independent of this.
# This is the ONLY definition of the compose budget in the codebase.
KAKAO_TEXT_LIMIT = 4000

_ELLIPSIS = "…"


def clip_words(text: str, budget: int) -> str:
    """Trim *text* to at most *budget* characters on a WORD boundary.

    Whitespace runs (including newlines) collapse to single spaces so the result
    is always one line. When trimming is needed the cut is marked with a trailing
    "…" and falls on a word boundary, so a truncated token can never masquerade as
    a whole one. A single first word longer than the budget is hard-cut (there is
    no boundary to fall back to) but is still marked, so the loss is never silent.
    Returns "" for a non-positive budget and text unchanged when it already fits.
    """
    text = " ".join(text.split())
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    if budget == 1:
        return _ELLIPSIS
    limit = budget - 1  # reserve one char for the ellipsis
    out = ""
    for word in text.split(" "):
        candidate = word if not out else f"{out} {word}"
        if len(candidate) <= limit:
            out = candidate
        else:
            break
    if not out:  # first word alone overflows — hard-cut it, still marked
        out = text[:limit]
    return out + _ELLIPSIS


def compose_alert(headline: str, lines: Iterable[str] = (), *, tail: str = "") -> str:
    """Compose a memo that fits :data:`KAKAO_TEXT_LIMIT` by construction.

    *headline* always goes out whole (it names what happened and what to do). Each
    of *lines* is appended only if the WHOLE line still fits the budget — a line
    that would overflow is dropped entirely rather than cut, so a value it carries
    (a sha, a path, a command) can never be bisected; order them most-actionable
    first. An optional *tail* of free-form text is clipped on a word boundary to
    whatever budget remains and appended last, so a worker's English note can ride
    along without ever pushing a mandatory line out or being cut mid-token.

    The headline itself is NOT clipped here: callers keep headlines short by
    design, and silently trimming the one line that must arrive intact would
    defeat the purpose (a pathological headline is still caught by kakao_send.sh's
    last-resort net, which now logs when it fires).
    """
    text = headline
    for line in lines:
        if line and len(text) + 1 + len(line) <= KAKAO_TEXT_LIMIT:
            text = f"{text}\n{line}"
    if tail:
        remaining = KAKAO_TEXT_LIMIT - len(text) - 1  # room after a newline
        if remaining >= 3:  # at least a char + the ellipsis is worth showing
            clipped = clip_words(tail, remaining)
            if clipped:
                text = f"{text}\n{clipped}"
    return text


# -- Noise-tier routing --------------------------------------------------------
# One judgement decides the tier: does the person receiving this have to act
# within hours? immediate = yes (or money/security/data loss is on the line);
# digest = worth knowing, nothing to do — queued and delivered ONCE a day by
# scripts/kakao_digest_send.py; silent = expected behaviour, log only. Callers
# OPT IN: a call that names no level stays an immediate send, byte for byte, so
# an alert can never quietly disappear because its sender predates the tiers.
# When in doubt: immediate — and so on ANY routing failure below (fail-open).
# Tier assignments per sender live in scripts/KAKAO_ALERTS.md.

LEVEL_IMMEDIATE = "immediate"
LEVEL_DIGEST = "digest"
LEVEL_SILENT = "silent"

# Same event key (kind + subject) fired again inside this window is merged into
# one send: the next delivery carries "외 N건". A sender whose re-alerts should
# be rarer than 10 minutes passes cooldown_s (the window becomes max of the two).
DEDUP_WINDOW_S = 600.0
# Dedup entries idle longer than this are pruned — the state file must not grow
# with every event key that ever existed.
_DEDUP_KEEP_S = 7 * 24 * 3600.0


def _vault_root() -> Path:
    """The vault this alert state lives under. KAKAO_VAULT_DIR is the same
    test-only seam kakao_send.sh honours and it stays TOP priority: when set it
    wins outright and the shared resolver is never consulted. Unset (production)
    the fallback goes through paths.vault_root() anchored at
    paths.vault_search_start(): in a checkout — with MACP_VAULT_ROOT also unset —
    byte-identical to the old Path(__file__).resolve().parents[1] this module sat
    on; in an installed wheel the anchor is None and the cwd-upward search runs
    instead of a site-packages anchor."""
    env = _os.environ.get("KAKAO_VAULT_DIR")
    if env:
        return Path(env)
    return paths.vault_root(start=paths.vault_search_start())


def _locked_append(path: Path, text: str) -> None:
    """Append under flock so the digest sender's drain cannot interleave with a
    writer mid-line. flock is advisory and unix-only, which covers both hosts;
    if it is unavailable the plain append still lands (worst case: a torn line,
    which the digest reader tolerates)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        try:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
        except Exception:
            pass
        fh.write(text)


def _route_log(log_path: Path, now: float, line: str) -> None:
    stamp = _dt.datetime.fromtimestamp(now).strftime("%Y-%m-%dT%H:%M:%S")
    _locked_append(log_path, f"[{stamp}] route: {line}\n")


def route(
    message: str,
    *,
    level: str | None = None,
    event_key: str | None = None,
    now: float | None = None,
    digest_path: Path | str | None = None,
    dedup_path: Path | str | None = None,
    log_path: Path | str | None = None,
    window_s: float = DEDUP_WINDOW_S,
    cooldown_s: float = 0.0,
) -> tuple[str, str | None]:
    """Route one alert into its noise tier. Returns ``(verdict, final_message)``:

    * ``("send", text)`` — deliver now; ``text`` may carry an "외 N건" suffix
      when suppressed duplicates were merged into this delivery;
    * ``("digest", None)`` — appended to the digest queue, nothing to send;
    * ``("silent", None)`` — logged only;
    * ``("suppressed", None)`` — an immediate duplicate inside the event-key
      window; counted, to surface on the window's next send.

    Never raises, and every failure path degrades to ``("send", message)``:
    losing an alert costs more than a duplicate ping. An unknown level string is
    treated as immediate for the same reason (a typo must not mute a sender).
    """
    try:
        now = _time.time() if now is None else float(now)
        root = _vault_root()
        digest_p = Path(digest_path) if digest_path else root / "logs" / "kakao_digest.jsonl"
        dedup_p = Path(dedup_path) if dedup_path else root / "logs" / "kakao_dedup.json"
        log_p = Path(log_path) if log_path else root / "logs" / "kakao_route.log"
        lvl = (level or "").strip().lower() or LEVEL_IMMEDIATE

        if lvl == LEVEL_DIGEST:
            entry = {"ts": _dt.datetime.fromtimestamp(now).isoformat(timespec="seconds"),
                     "key": event_key or "", "msg": message}
            _locked_append(digest_p, _json.dumps(entry, ensure_ascii=False) + "\n")
            _route_log(log_p, now, f"DIGEST key={event_key or '-'} msg: {message[:80]!r}")
            return ("digest", None)

        if lvl == LEVEL_SILENT:
            _route_log(log_p, now, f"SILENT key={event_key or '-'} msg: {message[:80]!r}")
            return ("silent", None)

        # immediate (default, and the fallback for any unknown level string)
        if not event_key:
            return ("send", message)

        try:
            state = _json.loads(dedup_p.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                state = {}
        except Exception:
            state = {}
        window = max(float(window_s), float(cooldown_s))
        ent = state.get(event_key)
        if isinstance(ent, dict) and now - float(ent.get("last_sent", 0.0)) < window:
            ent["suppressed"] = int(ent.get("suppressed", 0)) + 1
            ent["last_seen"] = now
            state[event_key] = ent
            _save_dedup(dedup_p, state, now)
            _route_log(log_p, now,
                       f"SUPPRESSED key={event_key} n={ent['suppressed']} msg: {message[:80]!r}")
            return ("suppressed", None)

        merged = int(ent.get("suppressed", 0)) if isinstance(ent, dict) else 0
        final = message
        if merged > 0:
            suffix = "\n" + i18n.t("digest.merged", n=merged)
            if len(message) + len(suffix) <= KAKAO_TEXT_LIMIT:
                final = message + suffix   # never at the cost of the headline
        state[event_key] = {"last_sent": now, "last_seen": now, "suppressed": 0}
        _save_dedup(dedup_p, state, now)
        return ("send", final)
    except Exception:
        return ("send", message)


def _save_dedup(path: Path, state: dict, now: float) -> None:
    state = {k: v for k, v in state.items()
             if isinstance(v, dict)
             and now - float(v.get("last_seen", v.get("last_sent", 0.0)) or 0.0) < _DEDUP_KEEP_S}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def read_digest_entries(text: str) -> list[dict]:
    """Parse digest-queue jsonl leniently: a torn/corrupt line becomes an entry
    whose msg is the raw line, so nothing queued can vanish from the summary."""
    entries: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = _json.loads(line)
            if isinstance(obj, dict) and obj.get("msg"):
                entries.append(obj)
            else:
                entries.append({"msg": line})
        except ValueError:
            entries.append({"msg": line})
    return entries


# The ko value of todo_system.orchestrator.followups.HUMAN_DIGEST_HEAD. Both read
# the i18n table rather than importing each other — this module is a leaf per the
# module docstring (launchd runs it under bare /usr/bin/python3 with no
# orchestrator package on the path), and i18n is the stdlib-only table both share.
# Used only to detect entries that need a human decision, so the digest headline
# never claims "no action needed" while one is waiting.
_HUMAN_DECISION_HEAD = i18n.MESSAGES["ko"]["followup.digest_head"]


def _human_decision_heads() -> tuple[str, ...]:
    """The follow-up digest head in every language. A notice queued under one
    language can be drained under the other, and it must still count."""
    heads = {tbl["followup.digest_head"] for tbl in i18n.MESSAGES.values()
             if tbl.get("followup.digest_head")}
    return tuple(sorted(heads | {_HUMAN_DECISION_HEAD}))


def compose_digest(entries: list[dict]) -> str:
    """One self-limiting memo summarising the queued FYI alerts. Repeats of the
    same headline kind collapse to one "×N" line; singletons keep their first
    line (word-clipped, so a slug is never bisected). Lines that still overflow
    are dropped whole by compose_alert — the FULL entries survive in
    logs/kakao_digest.sent.jsonl, and the headline always carries the count.

    If any entry is a parked human-decision follow-up (see
    ``_human_decision_heads``, in either language), the headline swaps "no action
    needed" for the pending decision count so that signal never gets buried in a
    digest that otherwise reads as "nothing to do"."""
    heads = _human_decision_heads()
    human_pending = sum(
        1 for e in entries
        if str(e.get("msg") or "").strip().startswith(heads)
    )
    if human_pending:
        headline = i18n.t("digest.headline_pending", n=len(entries), k=human_pending)
    else:
        headline = i18n.t("digest.headline_fyi", n=len(entries))
    empty = i18n.t("digest.empty_entry")
    groups: dict[str, list[str]] = {}
    for e in entries:
        msg = str(e.get("msg") or "").strip() or empty
        first = msg.splitlines()[0]
        head = first.split(":", 1)[0].strip() or first
        groups.setdefault(head, []).append(first)
    lines = []
    for head, firsts in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(firsts) == 1:
            lines.append(clip_words(f"· {firsts[0]}", 60))
        else:
            lines.append(clip_words(f"· {head} ×{len(firsts)}", 60))
    return compose_alert(headline, lines)
