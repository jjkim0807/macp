"""`recur:` — a task that repeats, from one frontmatter field.

    recur: daily
    recur: weekly
    recur: every 3d
    recur: weekly fri 09:00
    recur: off

A repeating task is a *series* of ordinary task files, one per round:
``<series>.md`` (a first round written without a date) and
``<series>-<YYYYMMDD>.md``. Nothing else marks a series — the slug is the key.
The undated file is a round only while it carries ``recur`` itself, so an
umbrella task that shares the series' name is left out of it.

When the **newest round** of a series reaches its ``not_before`` (a round
without one counts as reached), the orchestrator tick writes the next round as
a copy of it: the dates moved, the checkboxes cleared, the Progress Log reset to
one line. That is the only moment a round is created:

* The newest round's ``status`` is not consulted. Closing or cancelling a round
  early does not end the series — the next round still appears when the closed
  one's time comes — and closing a round never creates one by itself.
* While **another** round of the series is still open, nothing is created, so
  unfinished rounds do not pile up. The series resumes on the first tick after
  that round closes, at the next time still ahead (missed periods are skipped).
* To stop a series, delete ``recur`` from its newest round or set ``recur: off``.

What moves in the copy: the slug date and ``not_before`` become the new time,
and the dates in ``due`` and ``scheduled`` move by the same number of days (a
``HH:MM~HH:MM`` block keeps its clock times, hence its length). What one round
owns alone does not travel: ``completed`` and any external-system link.

The time the next round is counted from is the current round's ``not_before``;
a round without one falls back to its ``scheduled`` date, then the date in its
slug, then today. Times are host-local and naive, as in ``not_before.py``.

Stdlib-only apart from its engine siblings ``not_before``, ``i18n`` and
``orchestrator.statuses`` (all import-light): ``task_validate`` imports this
module, and ``cli`` imports ``task_validate``, so nothing here may import ``cli``.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import i18n
from . import not_before as _nb
from .orchestrator.statuses import is_closed

# The accepted values, as `cli validate` names them in its error.
EXPECTED = "`daily`, `weekly`, `every Nd`, `weekly <day> HH:MM` or `off`"

_OFF = ("", "-", "off")
_EVERY_RE = re.compile(r"every (\d{1,4})d")
_WEEKLY_AT_RE = re.compile(r"weekly (\S+) (\d{1,2}):(\d{2})")
_WEEKDAYS = {
    name: index
    for index, names in enumerate((
        ("mon", "monday", "월", "월요일"),
        ("tue", "tuesday", "화", "화요일"),
        ("wed", "wednesday", "수", "수요일"),
        ("thu", "thursday", "목", "목요일"),
        ("fri", "friday", "금", "금요일"),
        ("sat", "saturday", "토", "토요일"),
        ("sun", "sunday", "일", "일요일"),
    ))
    for name in names
}

_DATED_SLUG_RE = re.compile(r"(?P<series>.+)-(?P<date>\d{8})")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CHECKED_RE = re.compile(r"^([ \t]*-[ \t])\[[xX]\](?=[ \t])", re.M)
_PROGRESS_RE = re.compile(r"^## Progress Log[ \t]*$", re.M)
_H2_RE = re.compile(r"^## ", re.M)

# How many already-taken slots one sweep steps over before it gives a series
# up for this tick. A slot is only ever taken when a round's `not_before` was
# hand-edited away from its slug date, so this is a bound, not a tuning knob.
_MAX_TAKEN_SLOTS = 400

# Frontmatter that belongs to one round and must not ride into its copy: the
# calendar's stamp of when that round was closed, and any link to an external
# system's issue (a copy must never claim the original round's external identity).
_ROUND_ONLY_KEYS = ("completed", "external_ref", "external_updated_at")


@dataclass(frozen=True)
class Recur:
    """A parsed, enabled `recur` value."""

    days: int                       # the period in days: 1, 7 or N
    weekday: int | None = None      # Monday=0 … Sunday=6 — `weekly <day> HH:MM` only
    at: dt.time | None = None       # that form's HH:MM


@dataclass(frozen=True)
class Created:
    """One round `generate_due` wrote."""

    slug: str                       # the new round
    source: str                     # the round it was copied from
    not_before: str                 # the new round's `not_before` (display form)


# ---------------------------------------------------------------------------
# The value
# ---------------------------------------------------------------------------


def _unrecognised(value: object) -> str:
    hint = ""
    if isinstance(value, str) and "#" in value:
        hint = (" This parser does not strip inline comments — move the comment "
                "to its own line above.")
    return f"unrecognised value {value!r}; expected {EXPECTED}.{hint}"


def _parse(value: object) -> tuple[Recur | None, str | None]:
    """`(spec, error)`: `(None, None)` when the field is absent or off,
    `(Recur, None)` for a valid value, `(None, reason)` for anything else."""
    if value is None or value is False:
        # absent — or a strict-YAML reader's reading of a bare `off`
        return None, None
    if isinstance(value, (list, tuple)) and not value:
        return None, None           # a bare `recur:` line
    if not isinstance(value, str):
        return None, _unrecognised(value)
    text = " ".join(value.strip().strip("\"'").lower().split())
    if text in _OFF:
        return None, None
    if text == "daily":
        return Recur(1), None
    if text == "weekly":
        return Recur(7), None
    m = _EVERY_RE.fullmatch(text)
    if m and int(m.group(1)) >= 1:
        return Recur(int(m.group(1))), None
    m = _WEEKLY_AT_RE.fullmatch(text)
    if m:
        weekday = _WEEKDAYS.get(m.group(1))
        hour, minute = int(m.group(2)), int(m.group(3))
        if weekday is not None and hour < 24 and minute < 60:
            return Recur(7, weekday, dt.time(hour, minute)), None
    return None, _unrecognised(value)


def parse_recur(value: object) -> Recur | None:
    """The schedule a `recur` value asks for, or None when the task does not
    repeat — the field is absent, `off`, or not a value this module knows (the
    tick skips those; `recur_error` is what reports them)."""
    return _parse(value)[0]


def recur_error(value: object) -> str | None:
    """Why `value` is not a usable `recur`, or None when it is fine (absent
    and `off` are fine). `cli validate` turns the reason into an ERROR."""
    return _parse(value)[1]


def next_occurrence(spec: Recur, anchor: dt.datetime, now: dt.datetime) -> dt.datetime:
    """The first time after `anchor` on `spec`'s schedule that is still ahead
    of `now` — exactly one time, however many periods were missed.

    Period forms step from the anchor itself, so its clock time carries over.
    `weekly <day> HH:MM` takes the first such day on a **later date** than the
    anchor: a round standing on a Friday is that Friday's round whatever its
    own clock time, and the next one is the Friday after.
    """
    if spec.weekday is None or spec.at is None:
        period = dt.timedelta(days=spec.days)
        when = anchor + period
        if when <= now:
            when = anchor + period * ((now - anchor) // period + 1)
        return when
    day = anchor.date() + dt.timedelta(days=1)
    day += dt.timedelta(days=(spec.weekday - day.weekday()) % 7)
    when = dt.datetime.combine(day, spec.at)
    if when <= now:
        week = dt.timedelta(days=7)
        when += week * ((now - when) // week + 1)
    return when


def split_slug(slug: str) -> tuple[str, dt.date | None]:
    """`(series, round date)`. A trailing `-YYYYMMDD` that is a real date is the
    round's date; any other slug is a series of its own with no date."""
    m = _DATED_SLUG_RE.fullmatch(slug)
    if m:
        try:
            return m.group("series"), dt.datetime.strptime(m.group("date"), "%Y%m%d").date()
        except ValueError:
            pass
    return slug, None


# ---------------------------------------------------------------------------
# Frontmatter lines. Top-level keys only, and every other byte kept as written:
# the copy must differ from its source in the moved fields and nothing else.
# ---------------------------------------------------------------------------


def _frontmatter(text: str) -> tuple[list[str], str] | None:
    """`(frontmatter lines, body)`, or None for a file without a frontmatter
    block. Same split as the engine's line parsers (`cli.parse_frontmatter`)."""
    if not text.startswith("---\n"):
        return None
    head, fence, body = text.partition("\n---\n")
    if not fence:
        return None
    return head.split("\n")[1:], body


def _value(lines: list[str], key: str) -> str | None:
    """A top-level scalar as the line parsers read it — trimmed, surrounding
    quotes stripped, and the last one when the key is written twice. None when
    the key is absent."""
    prefix = f"{key}:"
    found = None
    for line in lines:
        if line.startswith(prefix):
            found = line[len(prefix):].strip().strip('"').strip("'")
    return found


def _is_list_item(line: str) -> bool:
    return line.strip().startswith("- ")


def _replace(lines: list[str], key: str, rendered: str | None) -> list[str]:
    """`lines` with the top-level `key` set to `rendered`: written where the key
    first stood (appended when it is absent), and every other line of that key —
    a block list under it, a second definition further down — dropped, so the
    readers that take the last definition see this one. `rendered` None removes
    the key."""
    prefix = f"{key}:"
    out: list[str] = []
    done = False
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line.startswith(prefix):
            out.append(line)
            continue
        if not done and rendered is not None:
            out.append(f"{key}: {rendered}")
        done = True
        while i < len(lines) and _is_list_item(lines[i]):
            i += 1
    if not done and rendered is not None:
        out.append(f"{key}: {rendered}")
    return out


def _shift_date(lines: list[str], key: str, days: int) -> list[str]:
    """Move the first date in the top-level `key:` value by `days`. The rest of
    the line — quotes, a `HH:MM~HH:MM` block — stays as written, and a value
    without a date (`-`, a bare time block) is left alone."""
    prefix = f"{key}:"
    out: list[str] = []
    for line in lines:
        m = _DATE_RE.search(line, len(prefix)) if line.startswith(prefix) else None
        if m:
            try:
                moved = dt.date.fromisoformat(m.group(0)) + dt.timedelta(days=days)
                line = line[:m.start()] + moved.isoformat() + line[m.end():]
            except (ValueError, OverflowError):
                pass                # not a real date: leave the line as written
        out.append(line)
    return out


def _leading_date(value: str | None) -> dt.date | None:
    m = _DATE_RE.match(value or "")
    if not m:
        return None
    try:
        return dt.date.fromisoformat(m.group(0))
    except ValueError:
        return None


def _midnight(day: dt.date | None) -> dt.datetime | None:
    return dt.datetime.combine(day, dt.time.min) if day is not None else None


def _render_not_before(when: dt.datetime) -> str:
    """The frontmatter value: a bare date at 00:00 (the two mean the same), else
    a quoted date-time so a YAML reader keeps it a string (`cli set` does the same)."""
    if when.time() == dt.time.min:
        return when.date().isoformat()
    if when.second:
        return f'"{when:%Y-%m-%d %H:%M:%S}"'
    return f'"{when:%Y-%m-%d %H:%M}"'


def _fresh_body(body: str, log_line: str) -> str:
    """The body for a new round: every checkbox open again and the Progress Log
    holding `log_line` alone (the section is added when the body has none)."""
    body = _CHECKED_RE.sub(r"\1[ ]", body)
    head = _PROGRESS_RE.search(body)
    if head is None:
        return body.rstrip("\n") + f"\n\n## Progress Log\n\n{log_line}\n"
    following = _H2_RE.search(body, head.end())
    rest = "\n" + body[following.start():] if following else ""
    return body[:head.start()] + f"## Progress Log\n\n{log_line}\n" + rest


def build_next_round(text: str, *, prev_slug: str, new_slug: str,
                     anchor: dt.datetime, when: dt.datetime, today: dt.date) -> str:
    """The markdown of the round after `text` (the round `prev_slug`).

    `anchor` is the time the current round stands on and `when` the new round's
    `not_before`; `due` and `scheduled` move by the whole days between the two
    dates. Raises ValueError for a file with no frontmatter block.
    """
    parsed = _frontmatter(text)
    if parsed is None:
        raise ValueError("no frontmatter block")
    lines, body = parsed
    shift = (when.date() - anchor.date()).days
    lines = _replace(lines, "status", "open")
    lines = _replace(lines, "not_before", _render_not_before(when))
    lines = _shift_date(lines, "due", shift)
    lines = _shift_date(lines, "scheduled", shift)
    lines = _replace(lines, "updated", today.isoformat())
    for key in _ROUND_ONLY_KEYS:
        lines = _replace(lines, key, None)
    if _value(lines, "worker_branch") == f"worker/{prev_slug}":
        lines = _replace(lines, "worker_branch", f'"worker/{new_slug}"')
    log_line = i18n.t("recur.md.created", today=today.isoformat(), prev=prev_slug)
    return "---\n" + "\n".join(lines) + "\n---\n" + _fresh_body(body, log_line)


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


def _holds_back(path: Path, *, undated: bool) -> bool:
    """Whether an older round keeps the next one from being written: it is
    still open.

    The undated `<series>.md` counts only when it carries `recur` itself. A
    first round does; an umbrella task that merely shares the series' name
    (`parent:` of the rounds, say) does not, and must not stall the series for
    as long as it stays open. A dated file is always a round.

    An unreadable file counts as open: the sweep then creates nothing, which is
    the side that cannot duplicate a round."""
    try:
        parsed = _frontmatter(path.read_text(encoding="utf-8"))
    except OSError:
        return True
    lines = parsed[0] if parsed else []
    if undated and _value(lines, "recur") is None:
        return False
    return not is_closed(_value(lines, "status"))


def _next_round(items: Path, series: str, rounds: list[tuple[dt.date, str]],
                now: dt.datetime) -> Created | None:
    """Write the round after `series`'s newest one if it is due; None otherwise."""
    latest_date, latest = rounds[-1]
    text = (items / f"{latest}.md").read_text(encoding="utf-8")
    if "\nrecur:" not in text:
        return None                 # the common case: read once, parse nothing
    parsed = _frontmatter(text)
    if parsed is None:
        return None
    lines, _body = parsed
    spec = parse_recur(_value(lines, "recur"))
    if spec is None:
        return None
    not_before = _value(lines, "not_before")
    if _nb.is_deferred(not_before, now):
        return None
    # Newest first: the round still open is nearly always the one just before.
    for date, slug in reversed(rounds[:-1]):
        if _holds_back(items / f"{slug}.md", undated=date == dt.date.min):
            return None
    anchor = (_nb.parse_not_before(not_before)
              or _midnight(_leading_date(_value(lines, "scheduled")))
              or _midnight(latest_date if latest_date != dt.date.min else None)
              or _midnight(now.date()))
    when = next_occurrence(spec, anchor, now)
    for _ in range(_MAX_TAKEN_SLOTS):
        dest = items / f"{series}-{when:%Y%m%d}.md"
        if not dest.exists():
            break
        when = next_occurrence(spec, when, now)
    else:
        return None
    markdown = build_next_round(text, prev_slug=latest, new_slug=dest.stem,
                                anchor=anchor, when=when, today=now.date())
    try:
        # "x": a file that appeared since the check above is never overwritten.
        fh = open(dest, "x", encoding="utf-8")
    except FileExistsError:
        return None
    try:
        with fh:
            fh.write(markdown)
    except OSError:
        # No half-written round (a full disk): it would be imported as a task and
        # would read as a taken slot. The next tick tries this slot again.
        dest.unlink(missing_ok=True)
        raise
    return Created(dest.stem, latest, _nb.display_not_before(when))


def generate_due(items_dir: str | Path, *, now: dt.datetime | None = None,
                 warn: Callable[[str], None] | None = None) -> list[Created]:
    """Write the next round of every series in `items_dir` that is due one, and
    return what was written (empty on most ticks).

    Reads the newest round of each series once; a directory in which no task
    carries `recur` is only read, never written. Running it again right away
    creates nothing: the round just written is now the newest one, and it is
    not due until its own `not_before`. A series that fails (an unreadable
    file, a full disk) is reported through `warn` and does not stop the others.
    `now` pins the clock (default: `not_before`'s host-local clock).
    """
    items = Path(items_dir)
    if not items.is_dir():
        return []
    if now is None:
        now = _nb._now()
    series: dict[str, list[tuple[dt.date, str]]] = {}
    for path in items.glob("*.md"):
        key, date = split_slug(path.stem)
        # A first round written without a date sorts before every dated one.
        series.setdefault(key, []).append((date or dt.date.min, path.stem))
    created: list[Created] = []
    for key in sorted(series):
        rounds = sorted(series[key])
        try:
            made = _next_round(items, key, rounds, now)
        except Exception as e:  # noqa: BLE001 — one bad series must not stop the rest
            if warn is not None:
                warn(f"{rounds[-1][1]}: {type(e).__name__}: {e}")
            continue
        if made is not None:
            created.append(made)
    return created
