"""Assignment ledger — the orchestrator's durable record of who is doing what.

Each task in flight has one Assignment with a small state machine. Stored as
git-tracked JSON under Worker/assignments/ so a cloud takeover inherits the
exact in-flight state. Stdlib-only.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import message as m
from .atomic_io import atomic_write_text

# States
DISPATCHED = "dispatched"
ACCEPTED = "accepted"
IN_PROGRESS = "in_progress"
DONE = "done"
FAILED = "failed"
REJECTED = "rejected"
AWAITING_HUMAN = "awaiting_human"   # human parked, or AI escalated for input
NEEDS_INPUT = "needs_input"

TERMINAL = frozenset({DONE, FAILED, REJECTED})

# DISPLAY-only state — never stored in the ledger, never compared by the
# orchestration logic. A FAILED row carrying `checkout_busy` (the run never
# started: a live sibling owned the checkout) or `job_running` (a detached
# long_job is still going) is a wait, not a failure: the orchestrator already
# treats it that way (no refail, flat poll cooldown, never poisons). A row a
# scheduled resume just re-queued (is_resume_pending) is waiting too. Every
# user-facing view must say so too, or a healthy task reads as broken.
DEFERRED = "deferred"


def _field(row, name: str):
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


RESUMED_SCHEDULED = "resumed-scheduled"


def is_resume_pending(row) -> bool:
    """True for a FAILED row that a scheduled resume (resume.py) has just put
    back in line. The sweep reuses FAILED so the ordinary re-dispatch path picks
    the row up. The last history entry stays `resumed-scheduled` until that
    dispatch replaces the row. If a sibling holds the checkout, the row can sit
    like this for hours, and showing it as "failed" made a queued retry look
    broken (2026-10-08, k24-2-sweep)."""
    if _field(row, "state") != FAILED:
        return False
    history = _field(row, "history")
    if not isinstance(history, list) or not history:
        return False
    last = history[-1]
    return isinstance(last, dict) and last.get("type") == RESUMED_SCHEDULED


def is_deferred(row) -> bool:
    """True for a ledger row (Assignment or its JSON dict) that is only WAITING:
    stored FAILED but flagged `checkout_busy` or `job_running`, or queued again
    by a scheduled resume. Only FAILED — a REJECTED row is a real refusal, and
    the runner never sets these flags on a REJECT."""
    return (_field(row, "state") == FAILED
            and bool(_field(row, "checkout_busy") or _field(row, "job_running")
                     or is_resume_pending(row)))


def display_state(row) -> str:
    """The state to SHOW for a ledger row: DEFERRED for a deferred row, else the
    stored state. Presentation only — orchestration keeps reading `.state`."""
    if is_deferred(row):
        return DEFERRED
    return str(_field(row, "state") or "")

# reply type -> new state (None = record but don't change state)
_TRANSITIONS = {
    m.ACCEPT: ACCEPTED,
    m.PROGRESS: IN_PROGRESS,
    m.RESULT: DONE,
    m.FAIL: FAILED,
    m.REJECT: REJECTED,
    m.NEEDS_INPUT: NEEDS_INPUT,
    m.HEARTBEAT: None,
}


@dataclass
class Assignment:
    task_id: str
    agent_id: str
    corr_id: str
    state: str = DISPATCHED
    kind: str = ""                       # agent kind (human/ai-*) — parking policy
    attempts: int = 1
    refail_count: int = 0                # re-dispatches after a terminal failure —
                                         # drives the tiered fail cooldown (fast
                                         # first retry, escalating backoff)
    created_ts: str = field(default_factory=m.now_iso)
    updated_ts: str = field(default_factory=m.now_iso)
    last_nudge_ts: str | None = None
    nudge_index: int = 0
    last_note: str = ""                   # reason/summary from the latest reply —
                                         # so the dashboard can show WHY, no digging
    capacity_retry_count: int = 0        # CONSECUTIVE capacity-limited re-dispatches.
                                         # Deliberately separate from refail_count (a
                                         # usage limit is not the task's fault, so it
                                         # must not poison it) but still bounded: it
                                         # drives an exponential re-check backoff.
                                         # Without it a task whose every attempt hits
                                         # the limit re-dispatched every 30min forever
                                         # — 168 runs of one slug over 5 days, capped
                                         # only by daily_run_budget. Resets on any
                                         # non-capacity outcome.
    capacity_limited: bool = False        # latest failure was a usage/rate limit —
                                         # a transient capacity constraint, NOT a task
                                         # defect: skip refail + never poison-quarantine
    job_running: bool = False             # a detached `long_job` is still going.
                                         # Like capacity this is not a defect, and
                                         # unlike blocked_on it clears by itself —
                                         # so keep re-polling on a cheap cadence
                                         # (no agent is spawned) and never poison.
    checkout_busy: bool = False           # the run never started: a LIVE sibling run
                                         # owned the shared checkout, so restoring
                                         # main would have destroyed its working
                                         # tree. Same shape as job_running — not a
                                         # defect, clears by itself when the sibling
                                         # finishes — so poll, never poison.
    blocked_on: str = ""                  # verify named an EXTERNAL blocker (an
                                         # upstream PR that never landed, a console
                                         # setting nobody made). Not the task's defect
                                         # — but unlike capacity, waiting does not
                                         # clear it: something outside the run has to
                                         # change. So park for a human rather than
                                         # re-buy the pipeline on a cooldown.
    timed_out: bool = False               # latest failure was a STAGE TIMEOUT — the
                                         # runner caught TimeoutExpired, checkpoint-
                                         # pushed the branch, and set this in the FAIL
                                         # payload. Distinct from a reaped-stale FAIL,
                                         # which carries no such marker, so the streak
                                         # below counts only genuine, work-preserving
                                         # timeouts and not every silent runner.
    timeout_streak: int = 0               # CONSECUTIVE stage timeouts for this task.
                                         # Managed by the orchestrator's collect()
                                         # (a RESULT or a non-timeout FAIL resets it),
                                         # never by apply_reply — an intervening
                                         # ACCEPT/PROGRESS on the retry must not
                                         # silently clear it. At 2 the task parks for
                                         # a human instead of re-dispatching into the
                                         # same wall forever.
    last_alert_kind: str = ""             # which human-alert framing actually FIRED
                                         # last for this task (orchestrator.ALERT_*:
                                         # blocked / review-pr / result-no-pr /
                                         # failure-suspected / quarantine). Stamped
                                         # by the orchestrator at notify time, never
                                         # from a reply payload — apply_reply must
                                         # not touch it. reconcile_merged compares
                                         # it with how the task finally closes to
                                         # self-detect false alarms.
    close_fingerprint: str = ""           # sha256 of the task md's bytes just
                                         # BEFORE the orchestrator closed it
                                         # (tasks.md_fingerprint). reconcile_closed
                                         # re-closes a ledger=done / todo=open task
                                         # only while the md still matches it — a
                                         # lost or rewound close — and leaves an md
                                         # that changed since alone: that is a human
                                         # reopen (2026-09-17 macp-dashboard-public-
                                         # access was re-closed over one). Stamped
                                         # by the orchestrator at close time, never
                                         # from a reply payload — apply_reply must
                                         # not touch it. Empty = no evidence (a row
                                         # from an older build) -> the old
                                         # unconditional re-close.
    fallback_agent: str = ""              # set ONLY when the heavy router sent this
                                         # task to a heavy-capable remote instead of
                                         # its for_project owner: the owner's id, so
                                         # the reroute sweep knows where to bring it
                                         # back. Empty = the ordinary owner routing
                                         # (never rerouted — a project-owned remote
                                         # task must stay on its remote). Stamped by
                                         # the orchestrator; apply_reply must not
                                         # touch it.
    rerouted_from: dict = field(default_factory=dict)
                                         # the latest reroute away from a heavy
                                         # remote: {"agent", "reason", "ts",
                                         # "corr_id"}. Late replies from that agent
                                         # on that corr_id are logged and ignored.
                                         # Not a failure — refail_count is untouched.
    history: list[dict] = field(default_factory=list)
    payload: dict = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL

    @property
    def is_deferred(self) -> bool:
        return is_deferred(self)

    @property
    def display_state(self) -> str:
        return display_state(self)

    def apply_reply(self, msg: m.Envelope) -> bool:
        """Advance state from an agent reply. Returns True if it was a known
        reply type. Humans are never driven to FAILED here."""
        if msg.type not in _TRANSITIONS:
            return False
        self.history.append({"ts": m.now_iso(), "type": msg.type, "from": msg.frm})
        new = _TRANSITIONS[msg.type]
        if new is not None:
            self.state = new
        self.updated_ts = m.now_iso()
        p = msg.payload or {}
        note = p.get("reason") or p.get("summary") or p.get("note")
        if note:
            self.last_note = str(note)[:200]
        self.capacity_limited = bool(p.get("capacity_limited"))
        self.job_running = bool(p.get("job_running"))
        self.checkout_busy = bool(p.get("checkout_busy"))
        self.blocked_on = str(p.get("blocked_on") or "")
        # `timed_out` mirrors the latest reply; the STREAK is deliberately NOT
        # touched here — collect() owns it, so a retry's ACCEPT/PROGRESS between two
        # timing-out attempts cannot reset the count that drives the park.
        self.timed_out = bool(p.get("timed_out"))
        return True

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Assignment":
        data = json.loads(text)
        # Drop unknown keys instead of splatting them: in a daemon-restart window
        # an OLD build can read JSON a NEWER build wrote, and the TypeError from
        # one future field would make Ledger.load/all() treat the row as corrupt
        # — the assignment silently vanishes from every sweep.
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


class Ledger:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, task_id: str) -> Path:
        return self.root / f"{task_id}.json"

    def save(self, a: Assignment) -> None:
        atomic_write_text(self._path(a.task_id), a.to_json(), encoding="utf-8")

    def load(self, task_id: str) -> Assignment | None:
        p = self._path(task_id)
        if not p.exists():
            return None
        try:
            return Assignment.from_json(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def all(self) -> list[Assignment]:
        if not self.root.exists():
            return []
        out = []
        for f in sorted(self.root.glob("*.json")):
            try:
                out.append(Assignment.from_json(f.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def active(self, task_id: str) -> Assignment | None:
        a = self.load(task_id)
        return a if a and not a.is_terminal else None
