"""UI-unseen auto-merge hold — "withhold this merge until a device check for the
SAME sha passes".

The sibling of :mod:`resume`. When the runner's confidence guard fires — a verify
stage declared ``confidence: high`` for an app-UI diff that this run never saw on
a device — the task opens its PR but does NOT auto-merge. A hold record pins the
worker branch's HEAD sha at that moment; a later run that captures the app's
screen on the emulator node for the SAME sha releases the merge. This module is
the record + the pure release judgment, exactly the shape that let ``resume`` be
unit-tested without real git.

Records live in ``Worker/_state/pending_merge_holds/<slug>.json``.
``Worker/_state/`` is gitignored (``.gitignore:139``), so — like ``resume`` — these
are **host-local runtime state**: two hosts push the same vault, so a hold pinned
to one checkout's run artifacts must never be consumed by the other.

Design constraints (identical to :mod:`resume`):

- **stdlib only.** Runs on the orchestrator tick path.
- **Judgment is a pure function.** :func:`evaluate` does no I/O — it takes the
  device-evidence facts + ledger state as arguments, so each branch is unit-tested
  without real git / gh / an emulator.
- **Every uncertainty fails safe to "not yet" (`WAIT`).** No device evidence, or a
  device pass whose sha does not match the held one, keeps the hold — releasing on
  an uncertainty would merge a change nobody saw run. The one exception is
  **expiry**: a broken or past expiry goes to discard + alert, never silent
  forever-wait (the "does not silently linger" criterion ``resume`` also holds).
- **All paths are injected.** ``vault_root`` is a required argument of every public
  function, so tests never escape ``tmp_path``.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from . import screen_check
from .assignment import AWAITING_HUMAN, FAILED
# Reuse resume's time helpers + skip tally verbatim: same wire shape, and resume
# does NOT import this module, so there is no import cycle. Keeping one copy means
# a record written by one and read by the other can never drift.
from .resume import _Tally, _iso, _now, _parse_ts, is_expired  # noqa: F401

DEFAULT_EXPIRE_HOURS = 48

# States a hold can release into — the same set resume treats as resumable.
# AWAITING_HUMAN is where the guard parks the task (review_required); FAILED is
# included because a post-park retry can land there while the hold still stands.
RELEASABLE_STATES = frozenset({AWAITING_HUMAN, FAILED})

# evaluate()'s return values
RELEASE = "release"  # device pass for the held sha + a releasable state — merge now
WAIT = "wait"        # no device pass yet, or a different sha — keep the hold
STALE = "stale"      # already resolved / done — discard the hold, leave the task alone
EXPIRED = "expired"  # expired before a device pass arrived — discard + alert once

_SKIP_LINE_CAP = 3
# A run dir is named ``<YYYYMMDDTHHMMSS>-<slug>``; this reads the leading stamp.
_RUN_TS_RE = re.compile(r"^(\d{8}T\d{6})-")


class MergeHoldError(RuntimeError):
    """A hold cannot be scheduled (missing task/assignment) — surfaced by callers."""


def hold_dir(vault_root: str | Path) -> Path:
    return Path(vault_root) / "Worker" / "_state" / "pending_merge_holds"


def record_path(vault_root: str | Path, slug: str) -> Path:
    return hold_dir(vault_root) / f"{slug}.json"


def runs_dir(vault_root: str | Path) -> Path:
    return Path(vault_root) / "Worker" / "runs"


def schedule(vault_root: str | Path, slug: str, *, held_sha: str, branch: str,
             repo: str | None, pr_url: str | None = None, note: str = "",
             expire_hours: float = DEFAULT_EXPIRE_HOURS,
             now: dt.datetime | None = None) -> Path:
    """Write a hold record (0600) pinning this run's branch HEAD (``held_sha``).

    ``held_sha`` is required and non-empty — the whole release contract is "a
    device pass for THIS sha", so a hold with no sha could never release and would
    only expire. ``branch`` / ``repo`` / ``pr_url`` are carried so the release tick
    can merge the PR without re-deriving them. Overwrites an existing record for
    the slug (a re-run of the same still-held task refreshes the sha + expiry)."""
    root = Path(vault_root)
    slug = (slug or "").strip()
    if not slug:
        raise MergeHoldError("slug가 비어 있습니다")
    held_sha = (held_sha or "").strip()
    if not held_sha:
        raise MergeHoldError("held_sha가 비어 있습니다 — 보류는 특정 sha에 묶여야 합니다")
    if expire_hours <= 0:
        raise MergeHoldError(f"expire_hours는 0보다 커야 합니다: {expire_hours}")
    when = now or _now()
    record = {
        "slug": slug,
        "held_sha": held_sha,
        "branch": branch or f"worker/{slug}",
        "repo": repo or "",
        "pr_url": pr_url or "",
        "created_ts": _iso(when),
        "expires_ts": _iso(when + dt.timedelta(hours=expire_hours)),
        "note": note or "",
    }
    path = record_path(root, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def evaluate(record: dict, *, device_pass: bool, device_pass_sha: str | None,
             state: str | None, now: dt.datetime) -> str:
    """Pure judgment: may this held merge be released now? No I/O.

    Order mirrors :func:`resume.evaluate`:

    1. **Expiry first** — an expired hold is discarded (+ alert), never released
       late just because a device pass finally showed up.
    2. **Device evidence for the held sha** — ``device_pass`` must be True AND
       ``device_pass_sha`` must equal ``record["held_sha"]``. No pass, or a pass
       for a different sha (the branch moved), is ``WAIT``: both "can't tell" and
       "not yet" keep the hold rather than merge an unseen change.
    3. **State** — if the task is no longer in a releasable state (someone merged
       it by hand, or it is done), ``STALE``: discard the hold, touch nothing.
    4. Otherwise ``RELEASE``.
    """
    if is_expired(record, now):
        return EXPIRED
    held = str(record.get("held_sha") or "")
    if not device_pass or not device_pass_sha or device_pass_sha != held:
        return WAIT
    if state not in RELEASABLE_STATES:
        return STALE
    return RELEASE


def _run_ts(dir_name: str, *, tz) -> dt.datetime | None:
    """The ``<YYYYMMDDTHHMMSS>`` stamp leading a run-dir name, tz-attached, or None."""
    m = _RUN_TS_RE.match(dir_name)
    if not m:
        return None
    try:
        parsed = dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=tz)


def scan_device_evidence(vault_root: str | Path, slug: str, *,
                         after_ts: str | None = None) -> tuple[bool, str | None]:
    """``(device_pass, device_pass_sha)`` from this slug's most recent app run.

    Walks ``Worker/runs/*-<slug>`` newest-first and returns the first run that
    looks like an app run — one that left a ``head_sha.txt`` (the runner writes it
    for every app run, hold or not). For that run it reports
    :func:`screen_check.has_device_pass` and the pinned sha. A run with no
    ``head_sha.txt`` is skipped (a vault/non-app run carries no device evidence);
    no matching run at all → ``(False, None)``.

    ``after_ts`` (the hold's ``created_ts``) drops runs that started before the
    hold, read off the run-dir timestamp stamp. A stamp that can't be parsed is
    NOT dropped — the sha match in :func:`evaluate` is the real guard, so a
    conservative "keep it" here never releases on its own. I/O lives only here;
    the verdict is :func:`evaluate`'s."""
    runs = runs_dir(vault_root)
    if not runs.is_dir():
        return (False, None)
    try:
        dirs = sorted(
            (d for d in runs.iterdir() if d.is_dir() and d.name.endswith(f"-{slug}")),
            key=lambda d: d.name, reverse=True)
    except OSError:
        return (False, None)
    cutoff = _parse_ts(after_ts, tz=dt.timezone.utc) if after_ts else None
    for d in dirs:
        if cutoff is not None:
            ts = _run_ts(d.name, tz=cutoff.tzinfo)
            if ts is not None and ts < cutoff:
                continue
        sha_file = d / "head_sha.txt"
        if not sha_file.is_file():
            continue
        try:
            sha = sha_file.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            continue
        return (screen_check.has_device_pass(d), sha or None)
    return (False, None)


def load_records(vault_root: str | Path) -> tuple[list[tuple[Path, dict]],
                                                  list[tuple[Path, str]]]:
    """``(records read, records that failed to read)`` — same contract as
    :func:`resume.load_records`: a file that won't parse is surfaced, never deleted
    (its intent can't be recovered), and a non-dict JSON counts as unreadable."""
    good: list[tuple[Path, dict]] = []
    bad: list[tuple[Path, str]] = []
    directory = hold_dir(vault_root)
    if not directory.is_dir():
        return good, bad
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError as e:
        return good, [(directory, f"{type(e).__name__}: {e}")]
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            bad.append((path, f"{type(e).__name__}: {e}"))
            continue
        if not isinstance(data, dict):
            bad.append((path, f"레코드가 객체가 아닙니다: {type(data).__name__}"))
            continue
        good.append((path, data))
    return good, bad
