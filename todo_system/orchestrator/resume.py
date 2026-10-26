"""Conditional deferred resume (resume-when) — "redispatch this task once the vault has commit X".

2026-09-25 `ai-stock-trading-lowturnover-ic-to-sharpe` stalled on
awaiting_human. The task md had `long_job` attached and was pushed to the
vault's main, but `tasks.py` reads payload's `long_job` from the **live
vault checkout's md**, and that checkout was sitting on another vault
task's worker branch. Reverting the assignment in that state would have
re-run the whole pipeline without `long_job`. So a human had to hand-run
`/tmp/lt-reset.sh` (poll every minute → hand-edit
`Worker/assignments/<slug>.json` once the condition was met), and that
throwaway script even had to clear the permission classifier for approval.
This pattern (edit task md → wait for vault sync → resume) keeps repeating.
This module absorbs that watch script as a macp feature.

Reservation records live in `Worker/_state/pending_resumes/<slug>.json`.
`Worker/_state/` is gitignored (`.gitignore:139`), so these records are
**host-local runtime state** — since two hosts push the same vault,
committing a reservation pinned to "how far has this checkout's HEAD gotten"
would let another host consume someone else's condition.

Design constraints:

- **stdlib only.** This code runs on the orchestrator's tick path, and the
  tick must also start under a bare `python3` (the same constraint as
  `assignment.py`·`session_log`).
- **Judgment is a pure function.** `evaluate()` does no I/O — it takes git
  results and ledger state as arguments. So each branch is unit-tested
  without real git.
- **Every uncertainty fails safe to "not yet" (`WAIT`).** When it's
  impossible to tell whether git failed, or whether a sha is really not an
  ancestor, resuming anyway would unconditionally re-run the pipeline. The
  one exception is **expiry**: a broken or past expiry time goes to
  discard, not infinite wait. "Does not silently linger" is the acceptance
  criterion.
- **All paths are injected as arguments.** `vault_root` is a required
  argument of every public function (the tick passes
  `ledger.root.parent.parent`, the CLI passes `paths.vault_root()`). The
  module never computes the root itself, so tests can never escape
  `tmp_path`.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

from .assignment import AWAITING_HUMAN, FAILED, RESUMED_SCHEDULED

DEFAULT_EXPIRE_HOURS = 24

# States that can accept a resume. AWAITING_HUMAN is not in `TERMINAL` (so
# dispatch doesn't casually re-run a parked row on its own) and FAILED is the
# normal retry path.
RESUMABLE_STATES = frozenset({AWAITING_HUMAN, FAILED})

# evaluate()'s return values
RESUME = "resume"    # all three conditions met — resume now
WAIT = "wait"        # not yet (or can't judge) — keep the record, re-check next tick
STALE = "stale"      # someone already resumed it or it's done — discard the reservation, leave assignment alone
EXPIRED = "expired"  # expired — assignment unchanged, discard the reservation + alert once

# How many skip lines with the same cause to print in one sweep (same value as orchestrator._SKIP_LINE_CAP).
_SKIP_LINE_CAP = 3


class ResumeError(RuntimeError):
    """The reservation cannot be accepted — the CLI turns this into a non-zero exit."""


def resume_dir(vault_root: str | Path) -> Path:
    return Path(vault_root) / "Worker" / "_state" / "pending_resumes"


def record_path(vault_root: str | Path, slug: str) -> Path:
    return resume_dir(vault_root) / f"{slug}.json"


def _now() -> dt.datetime:
    """tz-aware current time. Uses the same shape as `message.now_iso()` —
    ledger's `updated_ts` is parsed with `dt.datetime.fromisoformat`, so an
    offset must be attached."""
    return dt.datetime.now().astimezone().replace(microsecond=0)


def _iso(when: dt.datetime) -> str:
    return when.replace(microsecond=0).isoformat()


def _parse_ts(value, *, tz) -> dt.datetime | None:
    """Parse an ISO string into a tz-aware datetime. None if it can't be parsed.

    A naive value gets `tz` attached — so one hand-written record doesn't
    throw a TypeError during comparison and rattle the whole sweep.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def is_expired(record: dict, now: dt.datetime) -> bool:
    """Whether this has expired. If `expires_ts` is missing or broken, it **counts as expired**.

    Reading a broken expiry time as "wait forever" means a reservation
    nobody clears sits silently waiting on its condition forever — a state
    the acceptance criteria forbid. It's safer to discard a record that
    can't be recovered and alert a human.
    """
    expires = _parse_ts(record.get("expires_ts"), tz=now.tzinfo)
    if expires is None:
        return True
    return now > expires


def evaluate(record: dict, *, branch: str | None, head_contains: bool | None,
             state: str | None, now: dt.datetime) -> str:
    """A **pure function** that judges "is it okay to resume now". No I/O.

    The judgment order is the essence of this function:

    1. **Check expiry first.** If an expired reservation silently runs
       because its condition was finally met, a reservation a human set 24
       hours ago ends up re-running the pipeline right now.
    2. `branch != "main"` → `WAIT`. `branch is None` (= git failed) is also
       `WAIT`. If the live checkout is on someone else's worker branch, the
       task md belongs to that branch too — which is exactly why this
       feature exists (the original incident).
    3. If `after_commit` is set but `head_contains is not True` → `WAIT`.
       Rather than lumping `None` (can't judge) together with `False` (not
       an ancestor), both go to "not yet" — because neither case should
       resume.
    4. If state isn't `awaiting_human`/`failed` → `STALE`. Someone already
       resumed it, or it's done. Touching the assignment would only cause
       harm.
    5. Otherwise → `RESUME`.
    """
    if is_expired(record, now):
        return EXPIRED
    if branch != "main":
        return WAIT
    if record.get("after_commit") and head_contains is not True:
        return WAIT
    if state not in RESUMABLE_STATES:
        return STALE
    return RESUME


def schedule(vault_root: str | Path, slug: str, *, after_commit: str | None = None,
             expire_hours: float = DEFAULT_EXPIRE_HOURS, note: str = "",
             now: dt.datetime | None = None) -> Path:
    """Write a reservation record. A reservation that can't be accepted is
    rejected with `ResumeError`.

    **Both** `Todo/items/<slug>.md` and `Worker/assignments/<slug>.json`
    must exist: if only the md exists, there's nothing to resume (it has
    never been dispatched); if only the assignment exists, dispatch never
    created the task and resuming is futile. The message states which one
    is missing — a human needs to tell apart a typo from "hasn't run yet".
    """
    root = Path(vault_root)
    slug = (slug or "").strip()
    if not slug:
        raise ResumeError("slug가 비어 있습니다")
    task_md = root / "Todo" / "items" / f"{slug}.md"
    if not task_md.is_file():
        raise ResumeError(f"태스크 파일이 없습니다: {task_md} — slug 오타인지 확인하세요")
    assignment = root / "Worker" / "assignments" / f"{slug}.json"
    if not assignment.is_file():
        raise ResumeError(
            f"assignment가 없습니다: {assignment} — 아직 디스패치되지 않은 태스크는 "
            f"재개할 것이 없습니다(worker_mode를 auto로 두면 그냥 큐에 들어갑니다)")
    if expire_hours <= 0:
        raise ResumeError(f"--expire-hours는 0보다 커야 합니다: {expire_hours}")

    when = now or _now()
    record = {
        "slug": slug,
        "after_commit": (after_commit or "").strip(),
        "created_ts": _iso(when),
        "expires_ts": _iso(when + dt.timedelta(hours=expire_hours)),
        "note": note or "",
    }
    path = record_path(root, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path


def _git(root: Path, args: list[str], _run=None, *, timeout: float = 30):
    run = _run or subprocess.run
    return run(["git", *args], cwd=root, capture_output=True, text=True,
               timeout=timeout)


def current_branch(root: str | Path, _run=None) -> str | None:
    """The live checkout's current branch. `None` if unknown (= caller should wait).

    Same shape as `_pull_main`'s HEAD==main guard (`orchestrator.py:1680`).
    """
    try:
        p = _git(Path(root), ["rev-parse", "--abbrev-ref", "HEAD"], _run)
    except Exception:
        return None
    if getattr(p, "returncode", 1) != 0:
        return None
    return (p.stdout or "").strip() or None


def head_contains(root: str | Path, sha: str, _run=None) -> bool | None:
    """Is `sha` an ancestor of HEAD. `True`/`False`/`None` (can't tell).

    **Distinguishing rc 1 from every other rc is the whole point.**
    `git merge-base --is-ancestor` exits rc 1 when it's not an ancestor, and
    dies with rc 128 for a ref that doesn't exist. Lumping both into
    `False` makes "a sha that hasn't reached the vault yet" read as a
    permanent rejection; lumping both into `True` resumes unconditionally
    either way. For a sha that doesn't exist, "don't know yet" is the
    correct answer.
    """
    if not sha:
        return None
    try:
        p = _git(Path(root), ["merge-base", "--is-ancestor", sha, "HEAD"], _run)
    except Exception:
        return None
    rc = getattr(p, "returncode", None)
    if rc == 0:
        return True
    if rc == 1:
        return False
    return None


def load_records(vault_root: str | Path) -> tuple[list[tuple[Path, dict]],
                                                  list[tuple[Path, str]]]:
    """Two lists: (records read, records that failed to read).

    A file that fails to parse is **never deleted**. Silently deleting a
    file whose intent can't be recovered would make a human's reservation
    vanish without a trace — the caller surfaces it with a one-line log and
    leaves the judgment call to a human. JSON that isn't a dict (a list, a
    number) also counts as unreadable: better than letting the `record.get`
    below blow up with an AttributeError.
    """
    good: list[tuple[Path, dict]] = []
    bad: list[tuple[Path, str]] = []
    directory = resume_dir(vault_root)
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


class _Tally:
    """Caps repeated identical skip lines within ONE sweep.

    Plays the same role with the same cap as `orchestrator._SkipTally`, but
    **does not import it**: `orchestrator` calls this module, so importing
    in the reverse direction would be circular. A fresh one is made each
    sweep and discarded with it — a tally that outlives a tick goes quiet
    starting from the second tick, and that doesn't stop the flood, it just
    makes the flood **invisible**.
    """

    def __init__(self, emit, cap: int = _SKIP_LINE_CAP):
        self.emit = emit
        self.cap = cap
        self.counts: dict[str, int] = {}

    def record(self, subject: str, cause: str) -> None:
        n = self.counts.get(cause, 0) + 1
        self.counts[cause] = n
        if n <= self.cap:
            self.emit(f"{subject} 건너뜀: {cause}")

    def flush(self) -> None:
        for cause, n in self.counts.items():
            if n > self.cap:
                self.emit(f"…같은 원인으로 {n - self.cap}건 더 건너뜀: {cause}")


def _default_log(msg: str) -> None:
    print(f"[orchestrator] resume: {msg}")


def consume(ledger, vault_root: str | Path, *, backdate_s: float,
            now: dt.datetime | None = None, notify=None, log=None,
            _run=None) -> list[str]:
    """Consume reservation records. Returns the list of resumed slugs. **Never raises.**

    This function runs **before** the orchestrator tick's dispatch, so a
    single exception raised here would stop that whole tick's dispatch. So
    there's one outer `try/except` and one per record, skips are capped via
    `_Tally` with only a log left behind (the same structure as
    `reconcile_unparked`'s per-row isolation — the structure that stopped
    the incident where one bad row silently starved every row queued after
    it).

    When the verdict is `RESUME`, the only fields touched are the
    **4 the acceptance criteria name + 2 history entries**:

        refail_count = 0  →  state = FAILED  →  blocked_on = ""  →  updated_ts = backdated

    **Order matters.** Flipping `state` to FAILED before first dropping
    `refail_count` to 0 makes the next tick's dispatch see the accumulated
    refail count and immediately re-quarantine it (poison quarantine). If
    `blocked_on` is left set, the re-dispatch machine itself stays stopped.
    `updated_ts` is backdated because dispatch anchors the fail cooldown
    there (`orchestrator.py:357`) — stamping the current time would make a
    human's "resume now" wait through one more cooldown window. The
    backdate width isn't a baked-in constant; the caller supplies it via
    `backdate_s`: only the orchestrator knows its own cooldown cap.

    `timeout_streak`·`timed_out`·`capacity_*`·`job_running`·`checkout_busy`
    are **left untouched** (out of scope). So a row that still has
    `timeout_streak >= 2` can get re-parked immediately by the next
    `collect()` even after being resumed — a known limitation, written down
    in `Worker/README.md`.

    `notify(msg, event_key=...)` is optional. It's called **after** the
    ledger save, so a notification failure never undoes the resume, and the
    call itself swallows its own errors.
    """
    resumed: list[str] = []
    emit = log or _default_log
    try:
        root = Path(vault_root)
        moment = now or _now()
        tally = _Tally(emit)
        records, unreadable = load_records(root)
        for path, cause in unreadable:
            # Not deleted — a human's reservation intent can't be recovered, so surface and keep it.
            tally.record(f"{path.name} (읽기 실패, 보존)", cause)
        for path, record in records:
            try:
                slug = str(record.get("slug") or path.stem)
                a = ledger.load(slug)
                sha = str(record.get("after_commit") or "")
                if is_expired(record, moment):
                    verdict = EXPIRED
                elif a is None:
                    # No ledger row means there's nothing to resume — discard the reservation only.
                    verdict = STALE
                else:
                    verdict = evaluate(
                        record,
                        branch=current_branch(root, _run),
                        head_contains=head_contains(root, sha, _run) if sha else None,
                        state=a.state,
                        now=moment,
                    )

                if verdict == WAIT:
                    # Keep the record. Leave one line per tick on what the condition was —
                    # even if it's buried under digest alerts, the run log must show "why hasn't it run yet".
                    emit(f"{slug}: 아직 아님 — 대기 유지 "
                         f"(after_commit={sha or '-'}, expires={record.get('expires_ts')})")
                    continue

                if verdict == RESUME:
                    a.refail_count = 0
                    a.state = FAILED
                    a.blocked_on = ""
                    a.updated_ts = _iso(moment - dt.timedelta(seconds=backdate_s))
                    a.last_note = (
                        f"예약 재개: 볼트 main이 {sha or 'HEAD'}를 반영 — 재디스패치")[:200]
                    a.history.append({"ts": _iso(moment), "type": RESUMED_SCHEDULED,
                                      "from": "orchestrator", "after_commit": sha,
                                      "note": str(record.get("note") or "")})
                    ledger.save(a)
                    path.unlink(missing_ok=True)
                    resumed.append(slug)
                    emit(f"{slug}: 조건 충족 — refail=0/state=failed/blocked_on='' "
                         f"+ updated_ts -{backdate_s:.0f}s, 예약 소비 "
                         f"(after_commit={sha or '-'})")
                    _notify(notify, f"♻️ 예약 재개: {slug} — 볼트 main이 "
                                    f"{sha or 'HEAD'}를 반영해 재디스패치합니다",
                            f"resume:{slug}", emit)
                elif verdict == STALE:
                    # Leave the assignment alone. Someone already resumed it, or it's done.
                    path.unlink(missing_ok=True)
                    state = getattr(a, "state", None)
                    emit(f"{slug}: state={state or '행 없음'} — 이미 처리됨, "
                         f"assignment 미변경·예약만 폐기")
                elif verdict == EXPIRED:
                    path.unlink(missing_ok=True)
                    emit(f"{slug}: 예약 만료(expires={record.get('expires_ts')}) — "
                         f"assignment 미변경·예약 폐기")
                    _notify(notify, f"⌛ 예약 재개 만료: {slug} — 조건이 만료 전에 "
                                    f"충족되지 않아 예약을 지웠습니다(재개 안 됨)",
                            f"resume-expired:{slug}", emit)
            except Exception as e:
                tally.record(path.name, f"{type(e).__name__}: {e}")
        tally.flush()
    except Exception as e:
        # Outer net: even if load_records·path computation·the logger itself fails, don't raise into the tick.
        emit(f"sweep aborted: {type(e).__name__}: {e}")
    return resumed


def _notify(notify, msg: str, event_key: str, emit) -> None:
    """Notification is a side effect — a failure does not undo a resume already saved."""
    if notify is None:
        return
    try:
        notify(msg, event_key=event_key)
    except Exception as e:
        emit(f"알림 실패(무시): {type(e).__name__}: {e}")
