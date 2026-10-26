"""Orchestrator core — route tasks to agents, dispatch, and track replies.

Router     : task slug -> owning agent (registry).
Dispatcher : send TASK_ASSIGN, open an Assignment (dedup against active ones).
Tracker    : consume replies from the orchestrator inbox, advance assignments.
Humans     : dispatch parks as awaiting_human, nudged via kakao with a bounded
             backoff; never timed out to failure.

Mac-side (uses registry/human). A single-writer Lease guards against two
orchestrators running at once (cloud-ready seam).
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from . import human, message as m, stages
from .. import i18n, kakao_format, paths
from .dashboard import _MERGED_BUT_BLOCKED
from .assignment import (
    ACCEPTED, AWAITING_HUMAN, DISPATCHED, DONE, FAILED, IN_PROGRESS, REJECTED,
    Assignment, Ledger,
)
from .lease import Lease

from .mailbox import DEFAULT_ROOT, Mailbox, open_mailbox
from .registry import Agent, Registry, load_registry
from .state_store import open_state_store

_IN_FLIGHT = (DISPATCHED, ACCEPTED, IN_PROGRESS)

# Queue-view wait verdicts — WHY a dispatchable task is not out yet. Emitted by
# queue_snapshot() into /api/status `queue[].wait` and `cli queue --json`, and
# rendered by the web/app dashboards: treat these strings as wire format.
WAIT_NEXT_TICK = "next_tick"
WAIT_CHECKOUT_BUSY = "checkout_busy"
WAIT_GRADLE_CAP = "gradle_cap"
WAIT_USAGE_HOLD = "usage_hold"
WAIT_PAUSED = "paused"
WAIT_BUDGET = "budget"
WAIT_COOLDOWN = "cooldown"
WAIT_CAPACITY = "capacity"
# Outermost gate wins when several apply (lower rank = further out). Display
# priority only — tick() still evaluates the gates in its own order.
WAIT_RANK = {
    WAIT_PAUSED: 0,
    WAIT_USAGE_HOLD: 1, WAIT_BUDGET: 1,
    WAIT_COOLDOWN: 2, WAIT_CAPACITY: 2,
    WAIT_CHECKOUT_BUSY: 3, WAIT_GRADLE_CAP: 3,
    WAIT_NEXT_TICK: 4,
}


def hold_wait(payload: dict | None, *, paused: bool, usage_hold: str | None,
              budget_exceeded: bool) -> str | None:
    """Which tick-wide hold keeps this task out of `runnable`, or None.

    Pure: the three hold inputs are passed in, so tick() feeds its own reads and
    the queue view feeds read-only ones (it must never call `usage_hold()`, which
    refreshes usage state). PAUSED stops everything — a human saying stop, which
    a task file cannot countermand. `bypass_guard: true` lets a task through the
    COST holds (quota pacing, daily run budget) only. When both cost holds apply,
    usage_hold is reported: they share one display tier.
    """
    if paused:
        return WAIT_PAUSED
    if (payload or {}).get("bypass_guard"):
        return None
    if usage_hold:
        return WAIT_USAGE_HOLD
    if budget_exceeded:
        return WAIT_BUDGET
    return None


class LedgerGate(NamedTuple):
    """dispatch()'s read-only verdict on a task's existing ledger row.

    kind: "done" | "busy" (non-terminal: in-flight or AWAITING_HUMAN) | "poison"
    (would be quarantined now) | "poll" (deferred row inside the job-poll
    cooldown) | "capacity" (capacity_limited, inside its backoff) | "cooldown"
    (inside the fail backoff) | "go". `until` = epoch the cooldown ends, else None.
    """
    kind: str
    until: float | None = None
    deferred: bool = False
    capacity: bool = False


class CheckoutGate(NamedTuple):
    """_dispatch_serial_per_checkout's verdict for one task against `live`.

    wait: WAIT_CHECKOUT_BUSY | WAIT_GRADLE_CAP | None (free). holder = slug of the
    first in-flight sibling for checkout_busy; reason = `_hold_reason` text;
    gradle_runs = live gradle-run count for gradle_cap.
    """
    wait: str | None
    holder: str = ""
    reason: str = ""
    gradle_runs: int = 0

# What one agent's mailbox read can legitimately raise: an unreachable or
# unreadable root, a failed git subprocess, an unknown transport, or a
# malformed envelope out of receive(). Anything else is a bug and propagates.
_MAILBOX_READ_ERRORS = (OSError, ValueError, LookupError, TypeError, subprocess.SubprocessError)

# Key for "runs in the vault checkout" — tasks with no external `repo`. Not a
# repo URL, so it can never collide with one.
_VAULT_CHECKOUT = "<vault>"

DEFAULT_LEDGER = "Worker/assignments"
DEFAULT_LEASE = "Worker/orchestrator.lock"

# Alert framings — stamped on the assignment at the exact moment a human-facing
# alert fires: one per _notify_review branch, plus the poison quarantine. The
# stamp records which branch ACTUALLY fired (never inferred from state later),
# so even a branch believed unreachable gets audited if it regresses into
# firing. reconcile_merged compares the stamp against how the task finally
# closes: a FAILURE framing followed by a benign close is a false alarm and
# gets self-recorded (see _record_false_alarm). These strings land in the
# git-tracked assignment JSON — treat them as wire format.
ALERT_BLOCKED = "blocked"                       # blocked — precondition unmet
ALERT_REVIEW_PR = "review-pr"                   # PR open, awaiting review/merge
ALERT_RESULT_NO_PR = "result-no-pr"             # completed normally, 0-commit
ALERT_FAILURE_SUSPECTED = "failure-suspected"   # suspected failure, needs investigation
ALERT_QUARANTINE = "quarantine"                 # quarantined after repeated failures
ALERT_TIMEOUT_PARK = "timeout-park"             # 2 consecutive stage timeouts — cannot continue
ALERT_CLOSED_UNMERGED = "closed-unmerged"       # delegate PR closed without merging — needs a decision
# History-type stamp reconcile_merged leaves on a parked delegate row whose PR a
# human CLOSED without merging — the one-time "it will never merge, decide" alert's
# park-once guard, sibling to _MERGED_BUT_BLOCKED. Also wire format (git-tracked
# assignment JSON): never re-spell it.
_CLOSED_UNMERGED = "closed-unmerged-alerted"
# ALERT_TIMEOUT_PARK is deliberately NOT in FAILURE_ALERT_KINDS: like ALERT_BLOCKED
# it is a legitimate park (the task is too big for one stage, work is safe on
# origin), not a suspected code defect, so reconcile_merged must not log a benign
# close of it as a false alarm.
# ALERT_RESCOPE_PENDING: a blocked park whose run dir carries a machine-readable,
# auto-executable split plan (rescope.json). collect() stamps it INSTEAD of
# ALERT_BLOCKED and suppresses the ⛔ human ping; reconcile_rescope then auto-splits
# the task. Like the two parks above it is benign (not a defect), so it is NOT in
# FAILURE_ALERT_KINDS. Mirrors rescope.ALERT_RESCOPE_PENDING (wire format).
ALERT_RESCOPE_PENDING = "rescope-pending"
FAILURE_ALERT_KINDS = frozenset({ALERT_FAILURE_SUSPECTED, ALERT_QUARANTINE})

# The same task re-parking on the same gate re-fires _notify_review every time
# the re-dispatch machine buys another run — 2026-08-18 that meant 10 review
# pings in a day, half of them the same task on the same gate. Within this
# window such repeats merge into one delivery ("+N more" on the window's next
# send). Keyed per (framing, task): a REAL state change (blocked → PR open)
# changes the framing and therefore alerts immediately.
REVIEW_RENOTIFY_COOLDOWN_S = 6 * 3600.0

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(s: str) -> float:
    """'1d' -> 86400.0, '30m' -> 1800.0, '45' -> 45.0 (seconds)."""
    s = str(s).strip()
    if s and s[-1] in _UNITS:
        return float(s[:-1]) * _UNITS[s[-1]]
    return float(s)


def _parse_ts(ts) -> float:
    # Tolerate a numeric epoch or a malformed/missing value: one bad ledger entry
    # must never crash the whole auto tick (which would freeze ALL dispatch).
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        return dt.datetime.fromisoformat(ts).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _local_iso(ts) -> str:
    """A ledger timestamp as local naive ISO to the second; "" if unparseable."""
    epoch = _parse_ts(ts)
    if not epoch:
        return ""
    return dt.datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


def _clock(epoch: float | None, now: float) -> str:
    """`HH:MM` for a same-day epoch, `MM/DD HH:MM` otherwise; "?" when unknown."""
    if not epoch:
        return "?"
    when = dt.datetime.fromtimestamp(epoch)
    same_day = when.date() == dt.datetime.fromtimestamp(now).date()
    return when.strftime("%H:%M" if same_day else "%m/%d %H:%M")


@dataclass
class Task:
    slug: str
    payload: dict = field(default_factory=dict)  # outcome/next_action/branch/requires/summary


# Per-row error isolation prints one line per swallowed exception. A SYSTEMATIC
# defect (`tasks.task_status` raising for every row) therefore prints one line
# per ledger row, on every 5-minute tick, burying every other signal. Three keeps
# the first occurrences debuggable; the rest become a count.
_SKIP_LINE_CAP = 3
# The summary line quotes the cause, and `str(e)` is unbounded — an unabridged
# key would re-create the flood the cap exists to end.
_SKIP_KEY_MAXLEN = 120


class _SkipTally:
    """Caps repeated identical per-row skip lines within ONE reconcile sweep.

    Deliberately sweep-local: built at the top of each sweep and discarded with
    it. A tally that outlived the sweep would go quiet from the second tick
    onward, which does not stop a flood — it only stops it being VISIBLE, and a
    systematic defect would then be indistinguishable from a healthy tick.

    Isolation behaviour is untouched: every exception is still caught per row
    and still skips only that row. This changes how much of that gets printed.
    """

    def __init__(self, sweep: str, cap: int = _SKIP_LINE_CAP):
        self.sweep = sweep
        self.cap = cap
        self.counts: dict[str, int] = {}

    def record(self, tid: str, e: BaseException) -> None:
        """Print the first `cap` skips of each distinct cause; tally the rest.

        The key is (exception type, message) so two different defects hitting the
        same sweep stay separately visible instead of masking each other.
        """
        key = f"{type(e).__name__}: {e}"
        n = self.counts.get(key, 0) + 1
        self.counts[key] = n
        if n <= self.cap:
            # Byte-identical to the pre-cap line: the row name must stay in it.
            print(f"[orchestrator] {self.sweep}: {tid} skipped: {e}")

    def flush(self) -> None:
        """One summary line per SUPPRESSED cause — silent when nothing was capped.

        Staying silent matters: a tick where every row reconciled cleanly, or
        where fewer than `cap` rows failed, must not gain a new line.
        """
        for key, n in self.counts.items():   # insertion order = first-seen order
            if n <= self.cap:
                continue
            short = key if len(key) <= _SKIP_KEY_MAXLEN else key[:_SKIP_KEY_MAXLEN] + "…"
            print(f"[orchestrator] {self.sweep}: 같은 원인으로 {n - self.cap}건 "
                  f"추가 skip ({short})")


# --- touches overlap -------------------------------------------------------
# A pure, stdlib-only predicate for "may these two same-repo tasks run at the
# same time?". It lives at module level (no Orchestrator state) and sits on the
# 5-minute tick path, so — like rescope.py — it uses ONLY stdlib and stays a
# pure string computation: no fnmatch, no filesystem, no network.
#
# Glob-vs-glob overlap is undecidable in general, so this is deliberately
# CONSERVATIVE: when in doubt it reports overlap, which serialises the tasks —
# the safe default the serial-per-repo invariant already guarantees. It is only
# ever consulted when max_parallel_per_repo > 1; at cap 1 the dispatch
# cap short-circuits before any of this runs.

def _as_glob_list(v) -> list[str]:
    """Coerce a payload field (touches / depends_on) to a list of non-empty
    strings. Anything else — a bare string, None, a mapping — yields []."""
    if not isinstance(v, (list, tuple)):
        return []
    return [s.strip() for s in v if isinstance(s, str) and s.strip()]


def _static_prefix(glob: str) -> str:
    """The leading path components of a glob BEFORE its first wildcard.

    `a/b/*.py` -> `a/b`, `a/b.py` -> `a/b.py`, `**/x` -> `` (empty: a leading
    wildcard can match anywhere). Pure split on `/`; a component is static only
    when it contains none of the glob metacharacters `*?[`.
    """
    prefix: list[str] = []
    for comp in glob.strip("/").split("/"):
        if any(ch in comp for ch in "*?["):
            break
        prefix.append(comp)
    return "/".join(prefix)


def _globs_overlap(a: str, b: str) -> bool:
    """Conservative overlap of two globs via static-prefix containment.

    An empty static prefix (leading wildcard) can reach anywhere, so it overlaps
    everything. Otherwise two prefixes overlap when they are equal or one is a
    path-component ancestor of the other (the trailing `/` guard stops `a/b`
    from being read as an ancestor of the sibling `a/bc`).
    """
    pa, pb = _static_prefix(a), _static_prefix(b)
    if pa == "" or pb == "":
        return True
    if pa == pb:
        return True
    return pb.startswith(pa + "/") or pa.startswith(pb + "/")


def _touches_disjoint(a_globs: list[str], b_globs: list[str]) -> bool:
    """True iff no glob in one set overlaps any glob in the other."""
    return not any(_globs_overlap(a, b) for a in a_globs for b in b_globs)


class Orchestrator:
    def __init__(self, mailbox: Mailbox, registry: Registry, ledger: Ledger,
                 notify: human.Notifier = human.kakao_notifier, max_attempts: int = 2,
                 fail_cooldown_s: float = 6 * 3600, stale_timeout_s: float = 3600,
                 fail_cooldown_base_s: float = 900, fail_backoff: float = 4.0,
                 poison_threshold: int = 3, daily_run_budget: int = 40,
                 max_parallel_per_repo: int = 2,
                 max_parallel_gradle_builds: int = 0,
                 capacity_cooldown_s: float = 1800,
                 capacity_cooldown_max_s: float = 6 * 3600,
                 job_poll_cooldown_s: float = 300,
                 default_uri: str = f"git://{DEFAULT_ROOT}",
                 mailbox_base: Path | None = None,
                 alert_route=None):
        self.mb = mailbox                 # default; used for collect (pulls replies)
        self.reg = registry
        self.ledger = ledger
        self.notify = notify
        # Noise-tier router in front of `notify` (scripts/KAKAO_ALERTS.md).
        # NOT named `route` — that is the task→agent router method below.
        # Injectable so tests pin state files to a tmp dir; the default reads
        # its paths from KAKAO_VAULT_DIR at call time (same seam kakao_send.sh
        # honours). `notify` keeps its (text, link) shape — injected fakes only
        # ever see send-verdict deliveries.
        self.alert_route = alert_route or kakao_format.route
        self.max_attempts = max_attempts
        self.fail_cooldown_s = fail_cooldown_s          # cap on the tiered cooldown
        self.fail_cooldown_base_s = fail_cooldown_base_s  # 1st re-dispatch delay
        self.fail_backoff = fail_backoff                # escalation factor
        self.poison_threshold = poison_threshold        # re-fails before quarantine
        self.daily_run_budget = daily_run_budget        # max task-runs/day (0=∞)
        # Per-repo concurrent in-flight cap (default 2 = concurrency on; leaf 5 flipped
        # it 1→2). Only same-repo tasks whose touches do not overlap get dispatched
        # concurrently; the rest stay serial. A sub-constraint of the global
        # usage_guard slot (#191) — does not raise the global cap or double-count.
        self.max_parallel_per_repo = max_parallel_per_repo
        # Cross-checkout concurrent gradle-capable-run global cap (memory guard, 8GB;
        # [[runner-cgroup-memhigh-freeze-orphan-gradle]]). 0/negative = unlimited
        # (dormant); only enforced at dispatch when > 0. Separate from the usage_guard
        # quota slot and the per-repo cap, and does not double-count (counts only
        # gradle runs). Default is 0, so today's behavior is unchanged.
        self.max_parallel_gradle_builds = max_parallel_gradle_builds
        self.capacity_cooldown_s = capacity_cooldown_s  # re-check delay after a
                                                        # usage/rate limit (no refail)
        self.capacity_cooldown_max_s = capacity_cooldown_max_s  # cap on that backoff
        self.job_poll_cooldown_s = job_poll_cooldown_s  # re-check delay while a
                                                        # detached long_job runs
        self.stale_timeout_s = stale_timeout_s
        self.default_uri = default_uri
        # Base for RELATIVE mailbox uris opened later, per agent (`_mailbox_for`).
        # None = open_mailbox's own default (the cwd), so every existing caller
        # and test is unchanged; `_main` passes the vault root so a registry's
        # relative `mailbox: file://Worker/agents` resolves against the vault
        # instead of wherever the process was started.
        self._mb_base = mailbox_base
        self._agent_mb: dict[str, Mailbox] = {}

    def _runs_today(self) -> int:
        """Cost proxy: task pipeline runs started today (Worker/runs/<YYYYMMDD…>)."""
        day = dt.datetime.now().strftime("%Y%m%d")
        runs = self.ledger.root.parent / "runs"
        if not runs.exists():
            return 0
        return sum(1 for p in runs.iterdir() if p.name.startswith(day))

    def budget_exceeded(self) -> bool:
        return bool(self.daily_run_budget) and self._runs_today() >= self.daily_run_budget

    def paused(self) -> bool:
        """Manual quiesce switch: touch Worker/_state/PAUSED to stop starting new
        work (dispatch) while still collecting in-flight replies — e.g. to drain
        the system to zero before an upgrade. Remove the file to resume."""
        return (self.ledger.root.parent / "_state" / "PAUSED").exists()

    # -- Agent liveness (control-plane health mgmt) -----------------------
    def _seen_path(self) -> Path:
        return self.ledger.root.parent / "_state" / "agent_seen.json"

    def _record_seen(self, agent_id: str) -> None:
        """Any inbound message = a liveness signal from that agent. Recorded to a
        gitignored state file so the control plane knows who's alive without SSH."""
        if not agent_id or agent_id == m.ORCHESTRATOR:
            return
        try:
            if self.reg.get(agent_id).is_human:
                return      # humans send no heartbeat (same rule as agent_liveness)
        except Exception:
            pass            # unregistered sender / broken registry: record as before
        try:
            p = self._seen_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            try:
                seen = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                seen = {}
            seen[agent_id] = m.now_iso()
            p.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def agent_liveness(self, stale_after_s: float = 900,
                       now: float | None = None) -> dict[str, dict]:
        """Per-(non-human)-agent health: online < stale_after < stale < 3x < dead;
        'unknown' if never seen. Humans aren't heartbeat-monitored."""
        now = now if now is not None else dt.datetime.now().timestamp()
        try:
            seen = json.loads(self._seen_path().read_text(encoding="utf-8"))
        except Exception:
            seen = {}
        out: dict[str, dict] = {}
        for ag in self.reg.agents:
            if ag.is_human:
                continue
            ts = seen.get(ag.id)
            if not ts:
                out[ag.id] = {"status": "unknown", "last_seen": None, "age_s": None}
                continue
            age = now - _parse_ts(ts)
            status = ("online" if age < stale_after_s
                      else "stale" if age < 3 * stale_after_s else "dead")
            out[ag.id] = {"status": status, "last_seen": ts, "age_s": round(age)}
        return out

    def _fail_cooldown(self, refail_count: int) -> float:
        """Tiered backoff: a first failure retries fast (transient stage flakes
        shouldn't stall a project for hours), and repeated failures back off
        toward the full cap (a genuinely stuck task shouldn't churn). E.g. base
        15m -> 1h -> 4h -> capped at fail_cooldown_s."""
        cd = self.fail_cooldown_base_s * (self.fail_backoff ** max(0, refail_count))
        return min(cd, self.fail_cooldown_s)

    def _capacity_cooldown(self, capacity_retry_count: int) -> float:
        """Backoff for consecutive usage/rate-limit deferrals.

        A capacity limit is not the task's fault, so it never poisons the task and
        the task must resume by itself once quota returns — but the re-check has to
        back off. At the flat 30min it used to re-check, a task whose every attempt
        hit an exhausted weekly quota re-dispatched ~40x/day (the daily_run_budget
        cap) for days, each one spending a full pipeline just to rediscover the
        limit. Exponential from capacity_cooldown_s, capped, so it stays
        self-healing but cheap.
        """
        cd = self.capacity_cooldown_s * (self.fail_backoff ** max(0, capacity_retry_count))
        return min(cd, self.capacity_cooldown_max_s)

    def _mailbox_for(self, agent: Agent) -> Mailbox:
        """Dispatch to each agent via ITS declared transport (registry
        `mailbox`): a local agent uses file:// (no git churn), a remote agent
        git:// (synced through the vault repo). The orchestrator only addresses
        the agent id — location-agnostic. Falls back to the default mailbox."""
        uri = getattr(agent, "mailbox", None)
        if not uri or uri == self.default_uri:
            return self.mb
        if uri not in self._agent_mb:
            self._agent_mb[uri] = open_mailbox(uri, base=self._mb_base)
        return self._agent_mb[uri]

    # -- Router -----------------------------------------------------------
    def route(self, slug: str) -> Agent | None:
        return self.reg.for_project(slug)

    # -- Dispatcher -------------------------------------------------------
    def dispatch(self, task: Task, now: float | None = None) -> Assignment | None:
        """Send a task to its owning agent. No-op if already active (dedup),
        unroutable, or recently failed (cooldown — so an open-but-unresolvable
        task is not re-processed every tick; that once looped expensive
        opus+max-effort pipelines on already-done tasks)."""
        existing = self.ledger.load(task.slug)
        g = self._ledger_gate(existing, now)
        if g.kind in ("done", "busy"):
            return None
        # Bound here, not just inside the FAILED/REJECTED block, because the
        # re-dispatch block further down reads it under the same condition — and a
        # future reorder must not turn that into a NameError.
        deferred = g.deferred
        if g.kind == "poison":
            # Quarantine it (park for a human) once and stop churning cost.
            existing.state = AWAITING_HUMAN
            existing.updated_ts = m.now_iso()
            existing.history.append({"ts": m.now_iso(), "type": "quarantined-poison",
                                     "from": "orchestrator"})
            # Failure framing, stamped before the save below — _notify_poison
            # runs after it, so a set inside the notifier would never persist.
            existing.last_alert_kind = ALERT_QUARANTINE
            self.ledger.save(existing)
            self._notify_poison(existing)
            return None
        if g.kind in ("poll", "capacity", "cooldown"):
            return None
        agent = self.route(task.slug)
        if agent is None:
            return None
        env = m.Envelope(to=agent.id, type=m.TASK_ASSIGN, task_id=task.slug,
                         payload=task.payload)
        self._mailbox_for(agent).send(env)   # via the agent's own transport
        a = Assignment(task_id=task.slug, agent_id=agent.id, corr_id=env.corr_id,
                       kind=agent.kind, payload=task.payload)
        if existing and existing.state in (FAILED, REJECTED):
            # carry the accumulated count forward; only a real failure escalates it
            # (a capacity limit preserves it but doesn't count against the task).
            a.refail_count = existing.refail_count
            # Carry the timeout streak too, so consecutive timeouts across a
            # cooldown re-dispatch (not just the in-place _retry) still reach the
            # 2× park. collect() resets it on any non-timeout outcome.
            a.timeout_streak = getattr(existing, "timeout_streak", 0)
            if deferred:
                # Polling a job we asked for, or re-trying a run the checkout guard
                # held back. Neither a defect (no refail) nor a quota problem (no
                # capacity backoff) — the wait is expected and bounded by the job
                # (or the sibling run), so re-check on the flat cooldown.
                pass
            elif getattr(existing, "capacity_limited", False):
                # Consecutive quota deferrals: don't blame the task (refail stays
                # put) but do escalate the re-check interval, else this re-dispatches
                # forever while quota is out.
                a.capacity_retry_count = getattr(existing, "capacity_retry_count", 0) + 1
            else:
                a.refail_count += 1
                a.capacity_retry_count = 0   # a real outcome clears the quota streak
        if agent.is_human:
            a.state = AWAITING_HUMAN
            self._nudge(agent, a)
        self.ledger.save(a)
        return a

    def _ledger_gate(self, existing: Assignment | None,
                     now: float | None = None) -> LedgerGate:
        """dispatch()'s dedup/poison/cooldown verdict on `existing`, with no side
        effects — dispatch() performs the quarantine writes itself, and the queue
        view (queue_snapshot) reads the same verdict to explain a wait. `now` is
        resolved only for a FAILED/REJECTED row past the poison check, exactly
        where dispatch() always resolved it."""
        if existing and existing.state == DONE:
            # already completed — never re-run (dedup even if the todo status
            # wasn't marked done, e.g. a failed mark_done; this is what let a
            # merged task re-run into a duplicate PR)
            return LedgerGate("done")
        if existing and not existing.is_terminal:
            # Includes every AWAITING_HUMAN row, which is why parking is absolute:
            # a parked task is skipped here no matter what its markdown says. Do
            # NOT relax this (and do not add AWAITING_HUMAN to assignment.TERMINAL)
            # — it is what keeps a PR review or a NEEDS_INPUT question waiting for
            # the human who owes an answer. Un-parking is reconcile_unparked()'s
            # job: it moves the row to a terminal state first, on evidence, and
            # only then does it reach this line as re-dispatchable.
            return LedgerGate("busy")
        if existing and existing.state in (FAILED, REJECTED):
            capacity = getattr(existing, "capacity_limited", False)
            # A detached long_job in flight is a wait we asked for: not a defect,
            # so it must never poison, and it clears by itself.
            job_running = getattr(existing, "job_running", False)
            # Ditto a run the checkout guard held back: it never started, so there
            # is no evidence about the task at all. Counting that as a failure
            # would let a busy sibling quarantine a perfectly healthy task.
            checkout_busy = getattr(existing, "checkout_busy", False)
            deferred = job_running or checkout_busy
            if not (capacity or deferred) \
                    and existing.refail_count >= self.poison_threshold \
                    and not existing.kind.startswith("human"):
                # poison task: don't retry a task that keeps failing forever — the
                # tiered cooldown alone would re-dispatch it every 6h indefinitely.
                # Capacity limits never poison — they aren't the task's fault.
                return LedgerGate("poison", None, deferred, capacity)
            now = now if now is not None else dt.datetime.now().timestamp()
            # a usage/rate limit just needs to wait for capacity, not the escalating
            # fail backoff (which is for genuinely broken tasks). A long_job poll is
            # cheaper still — no agent is spawned — so it re-checks on a flat, short
            # interval: the capacity backoff would leave a finished job idle for up
            # to its full window before anyone noticed.
            if deferred:
                cooldown, kind = self.job_poll_cooldown_s, "poll"
            elif capacity:
                cooldown, kind = (self._capacity_cooldown(
                    getattr(existing, "capacity_retry_count", 0)), "capacity")
            else:
                cooldown, kind = self._fail_cooldown(existing.refail_count), "cooldown"
            started = _parse_ts(existing.updated_ts)
            if now - started < cooldown:
                return LedgerGate(kind, started + cooldown, deferred, capacity)
            return LedgerGate("go", None, deferred, capacity)
        return LedgerGate("go")

    @staticmethod
    def _checkout_key(payload: dict | None) -> str:
        """Which working tree a task will run in. Tasks carrying the same key
        share one checkout on disk and therefore must not run at the same time.

        External repos key on their clone URL (one ~/Worker/<name> per repo);
        everything else keys on the vault, which is itself a single checkout.
        """
        return str((payload or {}).get("repo") or "") or _VAULT_CHECKOUT

    def _checkout_owners(self) -> dict[str, str]:
        """checkout key -> slug of the live assignment holding it. Terminal and
        parked (awaiting_human) assignments hold nothing — only in-flight work
        does. Two in-flight rows on one key should not exist (that is what the
        serial dispatch prevents); if they do, either slug names the owner."""
        return {self._checkout_key(a.payload): a.task_id for a in self.ledger.all()
                if a.state in _IN_FLIGHT}

    def _checkouts_in_flight(self) -> set[str]:
        """Checkouts currently owned by a live assignment (keys of
        _checkout_owners)."""
        return set(self._checkout_owners())

    def _checkout_inflight(self) -> dict[str, list[Assignment]]:
        """checkout key -> ALL live assignment rows holding it (slug + payload).

        `_checkout_owners` (key -> single slug) is kept for the serial path and
        its two callers; this sibling is what the per-repo cap needs when
        max_parallel_per_repo > 1 — the FULL owner rows, so the overlap
        predicate can read each owner's `touches`/`depends_on` off its payload.
        Same liveness filter as `_checkout_owners`: only in-flight rows own a
        checkout (terminal and parked rows own nothing)."""
        out: dict[str, list[Assignment]] = {}
        for a in self.ledger.all():
            if a.state in _IN_FLIGHT:
                out.setdefault(self._checkout_key(a.payload), []).append(a)
        return out

    def _is_gradle_checkout(self, key: str) -> bool:
        """Does this checkout key resolve to a Gradle project (a `gradlew` at its
        shared clone root)? The vault key and any non-URL key are never Gradle; a
        URL whose shared clone is not on disk yet → False (conservative — the
        dormant default means this never bites, and the protective cap wants a
        KNOWN gradle checkout, not a guess). Lazy sibling import + stdlib only, like
        the rest of the dispatch tick path (rescope.py)."""
        if not key or key == _VAULT_CHECKOUT:
            return False
        try:
            from . import repos
            return (repos.local_path(key) / "gradlew").exists()
        except Exception:
            return False

    @staticmethod
    def _can_run_concurrently(a_slug: str, a_payload: dict,
                              b_slug: str, b_payload: dict) -> bool:
        """May two tasks that share one checkout run at the SAME time?

        Pure predicate (no Orchestrator state) — the dispatch cap's inner test.
        True ONLY when: both tasks declare a `touches` glob list, those glob
        sets are disjoint (static-prefix conservative), and neither task
        `depends_on` the other. A missing/empty `touches`, any overlap, or a
        `depends_on` edge -> False -> serialise. Consulted only when
        max_parallel_per_repo > 1; at cap 1 the cap short-circuits
        before it, so this never changes serial behaviour.
        """
        a_t = _as_glob_list(a_payload.get("touches"))
        b_t = _as_glob_list(b_payload.get("touches"))
        if not a_t or not b_t:
            return False
        if b_slug in _as_glob_list(a_payload.get("depends_on")):
            return False
        if a_slug in _as_glob_list(b_payload.get("depends_on")):
            return False
        return _touches_disjoint(a_t, b_t)

    @staticmethod
    def _hold_reason(t_slug: str, t_payload: dict, held: list, cap: int) -> str:
        """WHY is `t` held behind the siblings already in `held`? A short phrase
        in the configured UI language (i18n `hold.*`) for the deferred row's
        note, so the dashboards can tell a plain cap hit from a 'touches
        undetermined' hold from a real overlap.

        Mirrors the dispatch guard's own branches (cap first, then the first
        sibling `_can_run_concurrently` rejects, classified in that predicate's
        own order: empty `touches` on either side > a depends_on edge > a genuine
        glob overlap). Pure — no Orchestrator state, stdlib + same-module helpers.
        """
        if len(held) >= cap:
            return i18n.t("hold.cap", cap=cap)
        t_touch = _as_glob_list((t_payload or {}).get("touches"))
        for o in held:
            o_payload = o.payload or {}
            if Orchestrator._can_run_concurrently(
                    t_slug, t_payload or {}, o.task_id, o_payload):
                continue
            if not t_touch or not _as_glob_list(o_payload.get("touches")):
                return i18n.t("hold.no_touches")
            if (o.task_id in _as_glob_list((t_payload or {}).get("depends_on"))
                    or t_slug in _as_glob_list(o_payload.get("depends_on"))):
                return i18n.t("hold.depends_on")
            return i18n.t("hold.overlap")
        return i18n.t("hold.serial")      # unreachable (we only hold when a sibling blocks)

    @staticmethod
    def _checkout_label(key: str) -> str:
        """Short, human-readable name for a checkout key: the repo name of a
        clone URL (`.../if-then-habits.git` -> `if-then-habits`), else the key."""
        tail = key.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        return tail.removesuffix(".git") or key

    def _note_checkout_hold(self, t: Task, key: str, owner: str,
                            reason: str | None = None) -> None:
        """Make a held task's ledger row say WHY it is still waiting.

        `reason` (from `_hold_reason`) distinguishes a cap hit from a
        'touches undetermined' hold from a real overlap, so a dashboard reader
        can tell "this is just serial-per-repo" from "these two were judged to
        overlap". It flows onto the row's `last_note`, which both dashboards
        already render.

        Only a deferred row (A.is_deferred: FAILED + checkout_busy/job_running)
        is rewritten. Its note is the runner's "held by a LIVE sibling run"
        from an earlier attempt, which goes stale the moment this hold takes
        over — and the hold itself is otherwise only printed to a log nobody
        reads. A GENUINE failure in the hold keeps its note: that is the
        failure's diagnosis (Orchestrator.md and the brief escalate on it), and
        the row may still be inside its fail cooldown, so "waiting on a
        sibling" would not even be the true reason.

        Presentation only: state, history, and `updated_ts` are left alone —
        `updated_ts` is the deferral cooldown's clock, and bumping it on every
        note would silently push the re-dispatch back. Saves only when the note
        actually changes, so an unchanged hold costs no write per tick.
        """
        a = self.ledger.load(t.slug)
        if a is None or not a.is_deferred:
            return
        if reason is None:
            reason = i18n.t("hold.serial")
        other = f"`{owner}`" if owner and owner != t.slug else i18n.t("hold.other_task")
        note = i18n.t("hold.note", label=self._checkout_label(key), other=other,
                      reason=reason)[:200]
        if a.last_note == note:
            return
        a.last_note = note
        self.ledger.save(a)

    def _checkout_gate(self, t: Task, key: str, live: dict[str, list[Assignment]],
                       cap: int) -> CheckoutGate:
        """Is `t`'s checkout free given the in-flight rows in `live`? Pure read
        (no ledger write, no print) — the serial dispatch loop and the queue view
        share it. Per-repo cap / touches overlap first, then the global gradle cap.
        """
        held = [o for o in live.get(key, []) if o.task_id != t.slug]
        if held and (len(held) >= cap or not all(
                self._can_run_concurrently(
                    t.slug, t.payload or {}, o.task_id, o.payload or {})
                for o in held)):
            reason = self._hold_reason(t.slug, t.payload or {}, held, cap)
            return CheckoutGate(WAIT_CHECKOUT_BUSY, held[0].task_id, reason)
        # Global gradle-build ceiling (AC b): a dispatch-time cap on concurrent
        # gradle-capable runs across ALL checkouts — a memory guard (8GB;
        # [[runner-cgroup-memhigh-freeze-orphan-gradle]]) layered BELOW the
        # per-repo cap above and SEPARATE from usage_guard's quota slots, double-
        # counting neither (it counts live gradle RUNS only). Dormant at the
        # default 0 (= unlimited); enforced only when > 0, so cap-1 behaviour is
        # bit-for-bit (AC4). `live` already carries this tick's earlier
        # dispatches, so the count holds across several gradle repos in one tick.
        if self.max_parallel_gradle_builds > 0 and self._is_gradle_checkout(key):
            gradle_runs = sum(len(rows) for k, rows in live.items()
                              if self._is_gradle_checkout(k))
            if gradle_runs >= self.max_parallel_gradle_builds:
                return CheckoutGate(WAIT_GRADLE_CAP, gradle_runs=gradle_runs)
        return CheckoutGate(None)

    def _dispatch_serial_per_checkout(self, runnable: list[Task]) -> list[Assignment]:
        """Dispatch, but never put two tasks in one checkout at the same time.

        Every consumer of a checkout assumes it owns it: `prepare_external_branch`
        does `checkout -f` + `checkout -B`, and stages run `git clean -fd`. Those
        are safe only under the invariant stages.py states outright — "runs are
        serial per repo". Nothing enforced it. `tick()` dispatched every runnable
        task at once, so two tasks on the same repo raced one `.git`.

        That is what happened on 2026-08-14: leaf 16 and leaf 17 (both
        ai-stock-trading, both worker_mode auto) went out together and ended up
        with 12 concurrent `claude -p` stages on one checkout. Leaf 17's finished,
        self-tested implementation was fast-forwarded onto its branch and a racing
        branch-prep reset the branch back to origin/main seconds later — the work
        survived only because the stage had tagged it. Leaf 16's long_job artifact
        was `git clean -fd`'d away by leaf 17's stage in the same window.

        Holding a task back is free: it stays open and the next tick dispatches it
        once the checkout is quiescent. A wedged in-flight assignment cannot block
        its checkout forever — reap_stale() runs first in tick() and moves it to a
        terminal state, which releases the key.
        """
        # cap=1: any non-empty `held` trips `len(held) >= cap`, which
        # short-circuits the `or` BEFORE _can_run_concurrently is ever evaluated,
        # so the concurrency path stays dormant and this loop behaves exactly
        # like the pre-cap serial one (bit-for-bit). leaf 5 flipped the default to
        # 2 (concurrency on); at cap 2 a task with no/overlapping `touches` still
        # serialises via _can_run_concurrently, only disjoint-touches pairs run
        # together — so a profile can still pin cap=1 to force every repo serial.
        live = self._checkout_inflight()
        cap = max(1, int(self.max_parallel_per_repo))   # guard against 0/negative: never below serial
        dispatched: list[Assignment] = []
        for t in runnable:
            key = self._checkout_key(t.payload)
            cg = self._checkout_gate(t, key, live, cap)
            if cg.wait == WAIT_CHECKOUT_BUSY:
                print(f"[orchestrator] {t.slug}: deferred — checkout "
                      f"{key} is busy (serial per repo; {cg.reason})")
                try:
                    self._note_checkout_hold(t, key, cg.holder, cg.reason)
                except OSError:
                    pass      # a cosmetic note must never break the dispatch loop
                continue
            if cg.wait == WAIT_GRADLE_CAP:
                print(f"[orchestrator] {t.slug}: deferred — global gradle-build "
                      f"cap {self.max_parallel_gradle_builds} reached "
                      f"({cg.gradle_runs} live)")
                continue
            a = self.dispatch(t)
            if a is None:
                continue          # dedup/cooldown/unroutable: claims no checkout
            dispatched.append(a)
            live.setdefault(key, []).append(a)
        return dispatched

    # -- Queue view (read-only) -------------------------------------------
    def queue_snapshot(self, tasks: list[Task], *, now: float | None = None,
                       paused: bool = False, usage_hold: str | None = None,
                       budget_exceeded: bool = False) -> list[dict]:
        """Why each dispatchable task is not out yet, in tick() order.

        Read-only: no ledger/mailbox write, no print, no usage refresh — the hold
        inputs are passed in. It runs the SAME gates tick() runs (hold_wait →
        _checkout_gate → _ledger_gate) and simulates this tick's claims the way
        _dispatch_serial_per_checkout does, so a later same-checkout task reads
        `checkout_busy` behind an earlier `next_tick` one. Rows that tick() can
        never dispatch — DONE, in-flight, AWAITING_HUMAN, unroutable — are left
        out rather than shown as `next_tick`. When several gates apply the
        outermost (WAIT_RANK) is reported.
        """
        now = now if now is not None else dt.datetime.now().timestamp()
        live = self._checkout_inflight()
        cap = max(1, int(self.max_parallel_per_repo))
        out: list[dict] = []
        for t in tasks:
            existing = self.ledger.load(t.slug)
            lg = self._ledger_gate(existing, now)
            if lg.kind in ("done", "busy") or self.route(t.slug) is None:
                continue
            key = self._checkout_key(t.payload)
            h = hold_wait(t.payload, paused=paused, usage_hold=usage_hold,
                          budget_exceeded=budget_exceeded)
            cg = self._checkout_gate(t, key, live, cap) if h is None else CheckoutGate(None)
            lw = {"poll": WAIT_COOLDOWN, "cooldown": WAIT_COOLDOWN,
                  "poison": WAIT_COOLDOWN, "capacity": WAIT_CAPACITY}.get(lg.kind)
            waits = [w for w in (h, lw, cg.wait) if w]
            wait = min(waits, key=WAIT_RANK.__getitem__) if waits else WAIT_NEXT_TICK
            if h is None and cg.wait is None and lg.kind == "go":
                # what dispatch() would return this tick — claims the checkout for
                # the rest of the pass, never saved
                live.setdefault(key, []).append(Assignment(
                    task_id=t.slug, agent_id="", corr_id="", payload=t.payload))
            holder = cg.holder if wait == WAIT_CHECKOUT_BUSY else ""
            out.append({
                "slug": t.slug,
                "wait": wait,
                "wait_text": self._wait_text(wait, lg, existing, cg, key, usage_hold, now),
                "holder": holder,
                "checkout": self._checkout_label(key),
                "since": _local_iso(existing.updated_ts) if existing else "",
                "payload": t.payload or {},
            })
        return out

    def _wait_text(self, wait: str, lg: LedgerGate, existing: Assignment | None,
                   cg: CheckoutGate, key: str, usage_hold: str | None,
                   now: float) -> str:
        """One Korean-language line explaining `wait` (queue view only)."""
        if wait == WAIT_PAUSED:
            return "PAUSED — 수동 정지 중"
        if wait == WAIT_USAGE_HOLD:
            return f"사용량 HOLD — {usage_hold}"
        if wait == WAIT_BUDGET:
            return f"일일 실행 예산 도달 ({self._runs_today()}/{self.daily_run_budget})"
        if wait == WAIT_COOLDOWN:
            if lg.kind == "poison":
                return "반복 실패 — 다음 틱에 격리 예정"
            if lg.kind == "poll":
                return f"재확인 대기 — {_clock(lg.until, now)}"
            n = existing.refail_count if existing else 0
            return f"실패 쿨다운 — {_clock(lg.until, now)} 재시도 예정 (↻{n})"
        if wait == WAIT_CAPACITY:
            return f"capacity 재시도 대기 — {_clock(lg.until, now)}"
        if wait == WAIT_CHECKOUT_BUSY:
            return (f"같은 체크아웃({self._checkout_label(key)})에서 `{cg.holder}` "
                    f"실행 중; 사유: {cg.reason}")
        if wait == WAIT_GRADLE_CAP:
            return (f"gradle 동시 빌드 상한 {self.max_parallel_gradle_builds} 도달 "
                    f"({cg.gradle_runs} 실행 중)")
        return "다음 틱에 디스패치"

    # -- Tracker ----------------------------------------------------------
    def collect(self) -> list[Assignment]:
        """Consume agent replies from the orchestrator inbox, advance state."""
        updated: list[Assignment] = []
        for msg in self.mb.receive(m.ORCHESTRATOR):
            self._record_seen(msg.frm)      # every message = a liveness signal
            if msg.type == m.HEARTBEAT:      # idle liveness ping, no assignment
                self.mb.ack(m.ORCHESTRATOR, msg)
                continue
            a = self.ledger.load(msg.task_id)
            if a and (not a.corr_id or a.corr_id == msg.corr_id):
                if a.apply_reply(msg):
                    orphaned = (msg.payload or {}).get("orphaned_commits")
                    orphaned = orphaned if isinstance(orphaned, dict) else None
                    if msg.type == m.FAIL and orphaned:
                        # Before the outcome is routed, so no park/retry branch
                        # below can swallow it: branch prep rewound a prior
                        # attempt's unpushed commits and THIS attempt did not
                        # finish the task either, so that work may be all there is.
                        self._notify_orphaned_commits(a, msg)
                    elif msg.type == m.RESULT and orphaned:
                        # Not a loss. The run that did the rewind went on to finish
                        # the SAME task through its own verify, so the dead
                        # attempt's commits are superseded. Paging for them asks a
                        # human to judge work already redone and merged, and
                        # cherry-picking them would stack a duplicate on main
                        # (mulkko-39, 2026-09-17: 5 "rewound" commits, same 4 files
                        # rewritten and merged as PR #77). Ancestry cannot see this
                        # — the redo has different shas and different bytes — so it
                        # is decided by outcome here. The shas stay in the run's
                        # stages.json; this leaves a trace in history too.
                        a.history.append({"ts": m.now_iso(), "type": "orphans-superseded",
                                          "from": "orchestrator",
                                          "tip": str(orphaned.get("tip", ""))[:12],
                                          "count": orphaned.get("count")})
                        print(f"[orchestrator] {a.task_id}: {orphaned.get('count')} "
                              "rewound commit(s) superseded by this successful run "
                              "— not alerting")
                    if msg.type == m.FAIL and (msg.payload or {}).get("capacity_limited") \
                            and (msg.payload or {}).get("account_switch"):
                        # Leaf 4: the runner rotated the Claude account (or ran
                        # out of them). Announce once; the park/retry branches
                        # below are untouched — the capacity deferral re-dispatches
                        # exactly as before. apply_reply does not keep
                        # `account_switch`, so it is read off the payload here.
                        self._notify_account_switch(a, msg)
                    # Timeout streak: a task that keeps hitting the stage cap must
                    # not re-dispatch into the same wall forever. Count CONSECUTIVE
                    # runner-reported timeouts (checkpoint-pushed, work safe); any
                    # other terminal outcome resets it. Managed here (not in
                    # apply_reply) so an ACCEPT/PROGRESS on the retry cannot reset
                    # the count that the park branch below reads.
                    if msg.type == m.RESULT:
                        a.timeout_streak = 0
                    elif msg.type == m.FAIL:
                        a.timeout_streak = (a.timeout_streak + 1
                                            if (msg.payload or {}).get("timed_out") else 0)
                    if msg.type == m.NEEDS_INPUT:
                        a.state = AWAITING_HUMAN  # AI asked for a human decision
                    elif msg.type == m.RESULT:
                        if (msg.payload or {}).get("review_required"):
                            a.state = AWAITING_HUMAN      # PR opened, not merged —
                            self._notify_review(a, msg)   # park for human approval
                        else:
                            self._close_with_evidence(a)  # done -> close the todo task
                    elif a.state == FAILED and getattr(a, "blocked_on", ""):
                        # Verify named an external blocker. Retrying cannot clear
                        # it — the precondition lives outside this task — so park
                        # it where a human sees the blocker instead of re-buying
                        # the pipeline every cooldown (bean-cards-2 paid for two
                        # full runs against an unmerged upstream, 2026-08-05).
                        a.state = AWAITING_HUMAN
                        a.last_note = f"blocked on {a.blocked_on} — not a defect"[:200]
                        # Record this run's dir so reconcile_rescope can find the
                        # machine-readable split plan the run may have left. Store the
                        # run-id basename only (host-independent — the git-tracked
                        # ledger must not carry an absolute per-host path).
                        run_dir = str((msg.payload or {}).get("run_dir") or "").strip()
                        if run_dir:
                            a.payload["last_run_dir"] = Path(run_dir).name
                        # Auto-rescope: if the park carries a viable plan, suppress the
                        # ⛔ (stamp ALERT_RESCOPE_PENDING instead) and let
                        # reconcile_rescope split the task; otherwise alert as before.
                        if self._rescope_pending(a, run_dir):
                            a.last_alert_kind = ALERT_RESCOPE_PENDING
                            a.history.append({"ts": m.now_iso(),
                                              "type": "rescope-pending", "from": "orchestrator"})
                            print(f"[orchestrator] {a.task_id}: park carries a viable "
                                  f"rescope plan — ⛔ suppressed, reconcile_rescope will split")
                        else:
                            self._notify_review(a, msg)
                    elif a.state == FAILED and a.timed_out and a.timeout_streak >= 2:
                        # Two consecutive stage timeouts. The work is safe on
                        # origin (checkpoint_push), so this is not a loss — but a
                        # third re-dispatch just re-buys a pipeline that gets cut at
                        # the same wall. Park for a human to split the task or move
                        # it to a long_job. The first timeout (streak==1) still
                        # falls through to _retry below and resumes on origin/<branch>.
                        a.state = AWAITING_HUMAN
                        a.last_alert_kind = ALERT_TIMEOUT_PARK
                        a.last_note = (f"{a.timeout_streak}회 연속 stage timeout — "
                                       "이어가기 불가")[:200]
                        self._notify_timeout_park(a)
                    elif a.state == FAILED and a.kind.startswith("ai") \
                            and a.attempts < self.max_attempts:
                        self._retry(a)             # bounded re-dispatch
                    self.ledger.save(a)
                    updated.append(a)
            else:
                # #3: this reply matches no active assignment (or carries a stale
                # corr_id) and is about to be ACKed and dropped. That is exactly
                # the reaper-then-late-runner-reply race — a completed task's
                # RESULT/FAIL vanished without a trace. HEARTBEATs already
                # `continue`d above, so this only fires on real drops (RESULT/FAIL
                # /etc.). Leave a log line so the loss is at least observable.
                if a is None:
                    print(f"[orchestrator] collect: dropping {msg.type} for "
                          f"{msg.task_id} from {msg.frm} — no assignment on record")
                else:
                    print(f"[orchestrator] collect: dropping {msg.type} for "
                          f"{msg.task_id} from {msg.frm} — corr_id mismatch "
                          f"(expected {a.corr_id}, got {msg.corr_id})")
            self.mb.ack(m.ORCHESTRATOR, msg)
        return updated

    def _no_pr_artifact_hint(self, a: Assignment, p: dict) -> str:
        """Point at the artifact this run actually produced. The payload's
        `stages` list names what ran (delegate/quick pipelines never write
        report.md); without it, name only what is known — a run dir, or failing
        that the task id — rather than guess a filename."""
        completed = p.get("stages") or []
        art = stages.STAGE_ARTIFACT.get(str(completed[-1])) if completed else None
        run_dir = p.get("run_dir") or ""
        if run_dir:
            base = f"Worker/runs/{Path(run_dir).name}"
            return f"{base}/{art}" if art else base
        if art:
            return i18n.t("alert.no_pr_hint_art", task=a.task_id, art=art)
        return i18n.t("alert.no_pr_hint", task=a.task_id)

    def _task_title(self, slug: str) -> str:
        """Best-effort human title for line 2 of an alert — the task md's first
        `# ` heading, else the slug. Never raises: an alert must not depend on a
        readable file (a slug like `play-store-9-ifthen-targetsdk-36` is still a
        valid fallback identity)."""
        try:
            # Resolved per call (not a module const): the daemon is long-lived and
            # tests redirect MACP_VAULT_ROOT, so freezing this at import would pin
            # the wrong Todo/items. Follows the env; engine-root when unset in a
            # checkout (vault_search_start()), cwd-upward search in a wheel.
            path = (paths.vault_root(start=paths.vault_search_start())
                    / "Todo" / "items" / f"{slug}.md")
            for line in path.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s.startswith("# "):
                    return s[2:].strip() or slug
        except Exception:
            pass
        return slug

    def _notify_routed(self, text: str, link: str | None = None, *,
                       level: str | None = None, event_key: str | None = None,
                       cooldown_s: float = 0.0) -> None:
        """The ONE gate every orchestrator alert passes on its way to `notify`.

        Applies the noise tier (digest/silent never reach the notifier) and the
        event-key merge window for immediate alerts. Any routing failure falls
        open to a plain send — losing an alert costs more than a duplicate —
        and a notify failure stays swallowed exactly as it was at every call
        site before (an alert must never break collect/dispatch)."""
        try:
            verdict, final = self.alert_route(text, level=level, event_key=event_key,
                                              cooldown_s=cooldown_s)
        except Exception:
            verdict, final = "send", text
        if verdict != "send":
            return
        try:
            self.notify(final if final is not None else text, link)
        except Exception:
            pass

    def _notify_account_switch(self, a: Assignment, msg: m.Envelope) -> None:
        """Leaf 4 of macp-multi-claude-account: a capacity FAIL whose payload
        carries `account_switch` (the runner rotated the Claude account) is
        announced ONCE — one alert per switch, one per pause:

        * switch (`to` set): event key `account-switch:<from>:<to>` inside the
          router's cooldown window; the runner's `rotate_active` is idempotent
          (an already-marked account yields no second switch), so one wall
          produces one key.
        * paused (`to` None — no account left): `macp.accounts.claim_paused_alert`
          claims the runtime state's current `paused_since`, so the alert goes
          out once per pause and again only after the pause lifted and a new one
          began; the event key `account-paused` is a second belt.

        Re-dispatch is NOT touched: the capacity deferral path (cooldown, no
        refail) re-dispatches as before, on the new active account."""
        sw = (msg.payload or {}).get("account_switch")
        if not isinstance(sw, dict):
            return
        frm = str(sw.get("from") or "?")
        to = sw.get("to")
        reason = str(sw.get("reason") or "capacity")
        lines = [f"📋 {self._task_title(a.task_id)}"]
        if to:
            headline = i18n.t("alert.account_switch", frm=frm, to=to, reason=reason)
            self._notify_routed(human.compose_alert(headline, lines), None,
                                event_key=f"account-switch:{frm}:{to}",
                                cooldown_s=REVIEW_RENOTIFY_COOLDOWN_S)
            return
        try:
            from todo_system.macp import accounts as ACC
            first = ACC.claim_paused_alert()
        except Exception:
            first = True                 # a broken guard must not lose the alert
        if not first:
            return
        headline = i18n.t("alert.account_paused", frm=frm, reason=reason)
        self._notify_routed(human.compose_alert(headline, lines), None,
                            event_key="account-paused", cooldown_s=REVIEW_RENOTIFY_COOLDOWN_S)

    def _notify_review(self, a: Assignment, msg: m.Envelope) -> None:
        """Ping the human about a run that stopped short of merging.

        The message used to assert "pipeline complete · PR open (unmerged)" unconditionally.
        That is only sometimes true: gutenberg-reader-3 was reported that way on
        2026-08-07 while its report said `overall_status: fail` and `pr_url: null`
        — the human was asked to review and merge something that did not exist.
        An alert that describes an outcome it never checked is worse than none,
        because it spends attention on a fiction. Say what actually happened.
        The no-PR branch violated the same rule in the other direction until
        2026-09-04: dripcoffee-integcheck-advote finished a delegate run clean
        (0 commits, nothing to merge) and was announced as a failure pointing at
        a report.md that delegate mode never writes. Read msg.type instead.
        """
        p = msg.payload or {}
        pr = p.get("pr_url") or ""
        # Each branch stamps its framing on the assignment (outside the notify
        # try below, so a kakao failure never loses the record; persisted by the
        # caller's ledger.save). reconcile_merged reads it to self-detect false
        # alarms.
        blocker = getattr(a, "blocked_on", "")
        # The PR URL rides in the "Open" button (link arg), never in the text body,
        # so it can never be bisected. repo/branch/run path are NOT carried — they
        # are in the run record and the PR already; the phone alert only needs
        # WHAT happened + the ONE thing to do (in the headline) + which task.
        link: str | None = None
        lines = [f"📋 {self._task_title(a.task_id)}"]
        tail = ""
        if blocker:
            a.last_alert_kind = ALERT_BLOCKED
            headline = i18n.t("alert.review_blocked")
            # English blocker → word-clipped tail; run/PR has the full text
            tail = i18n.t("alert.reason", reason=blocker)
        elif pr:
            a.last_alert_kind = ALERT_REVIEW_PR
            headline = i18n.t("alert.review_pr")
            link = pr
        elif msg.type == m.RESULT:
            a.last_alert_kind = ALERT_RESULT_NO_PR
            headline = i18n.t("alert.review_result_no_pr")
            lines.append(i18n.t("alert.result", hint=self._no_pr_artifact_hint(a, p)))
        else:
            a.last_alert_kind = ALERT_FAILURE_SUSPECTED
            headline = i18n.t("alert.review_failure")
            lines.append(f"run: {self._no_pr_artifact_hint(a, p)}")
        text = human.compose_alert(headline, lines, tail=tail)
        # Immediate — a human judgement is the whole point. The event key pins
        # (framing, task): the same task re-hitting the same gate within the
        # cooldown merges into one delivery, a framing CHANGE alerts at once.
        self._notify_routed(text, link,
                            event_key=f"review:{a.last_alert_kind}:{a.task_id}",
                            cooldown_s=REVIEW_RENOTIFY_COOLDOWN_S)

    def _rescope_pending(self, a: Assignment, run_dir: str) -> bool:
        """True if this blocked park carries a machine-readable split plan that is
        safe to auto-execute — so collect() suppresses the ⛔ and reconcile_rescope
        runs it. Fail-open False → keep the ⛔ (never a silent park)."""
        try:
            from . import rescope, repos
            from .. import cli
            vault_root = Path(self.ledger.root).parent.parent
            runs_dir = Path(self.ledger.root).parent / "runs"
            rd = runs_dir / Path(run_dir).name if run_dir else None
            if rd is None or rescope.load_rescope(rd) is None:
                found = rescope.find_latest_rescope(runs_dir, a.task_id)
                rd = found[0] if found else None
            if rd is None:
                return False
            parent_fm = repos._frontmatter(Path(cli.ITEMS_DIR) / f"{a.task_id}.md")
            state_path = rescope._rescoped_state_path(vault_root)
            return rescope.plan_is_auto_viable(
                rd, parent_fm, state_path=state_path, parent_slug=a.task_id)
        except Exception:
            return False

    def _notify_orphaned_commits(self, a: Assignment, msg: m.Envelope) -> None:
        """Tell the human that branch prep rewound a prior attempt's unpushed work.

        This was detected all along — prepare_external_branch names the commits it
        is about to drop — but said it only to `progress`, which lands in
        Worker/runs/<id>/run.log. Nobody opens run.log unless they already suspect
        something, so the losses were invisible by construction.

        Deliberately NOT a repair. The commits stay dangling until git gc, and
        whether they should be replayed is a judgement (a dead attempt's work may
        be wrong, superseded, or already redone) — so this hands over the shas and
        stops. One ping; a notify failure must never break collect."""
        info = (msg.payload or {}).get("orphaned_commits") or {}
        p = msg.payload or {}
        shas = [str(s)[:8] for s in (info.get("shas") or [])]   # 8-char short shas: a value format, not a message cut
        shown, more = shas[:3], max(0, len(shas) - 3)
        count = info.get("count", len(shas))
        tip = str(info.get("tip", ""))[:8]
        # Headline carries the two facts this alert exists for — how many, and that
        # a decision is needed — so it survives any length. The tip sha and the
        # cherry-pick command ride as WHOLE lines only (compose_alert drops a line
        # that would overflow, never bisects it), so a truncated sha can never
        # masquerade as a usable one, and the full set is always in stages.json.
        headline = i18n.t("alert.orphaned", count=count)
        lines = [
            f"📋 {self._task_title(a.task_id)}",
            f"tip {tip}" if tip else "",
            f"git cherry-pick {' '.join(shown)}" + (f" (+{more})" if more else ""),
            f"branch {info.get('branch') or p.get('branch') or ''}".rstrip(),
            f"run {p.get('run_dir', '')}/stages.json" if p.get("run_dir") else "",
        ]
        text = human.compose_alert(headline, lines)
        self._notify_routed(text, None, event_key=f"orphaned:{a.task_id}")

    def _notify_poison(self, a: Assignment) -> None:
        """A task quarantined after repeated failures — hand it to a human instead
        of retrying forever. One ping; never break dispatch on a notify failure."""
        headline = i18n.t("alert.poison", n=a.refail_count)
        tail = i18n.t("alert.recent_reason", note=a.last_note) if a.last_note else ""
        text = human.compose_alert(headline, [f"📋 {self._task_title(a.task_id)}"], tail=tail)
        self._notify_routed(text, None, event_key=f"poison:{a.task_id}")

    def _notify_timeout_park(self, a: Assignment) -> None:
        """Two consecutive stage timeouts — re-dispatching just re-buys a pipeline
        that keeps getting cut at the same stage. Hand it to a human. The commits
        are safe on origin (checkpoint_push), so this is a 'too big for one stage'
        alert, not a loss alert. One ping; never break collect on a notify failure."""
        headline = i18n.t("alert.timeout_park", n=a.timeout_streak)
        lines = [f"📋 {self._task_title(a.task_id)}",
                 i18n.t("alert.timeout_checkpointed")]
        text = human.compose_alert(headline, lines)
        self._notify_routed(text, None, event_key=f"timeout-park:{a.task_id}")

    def _notify_blocker_cancelled(self, a: Assignment, blocker: str) -> None:
        """An upstream this task was waiting on got cancelled. Nobody is going to
        deliver the precondition, so the task cannot proceed — but it may also no
        longer be wanted, and only a human can say which. One ping; never break
        dispatch on a notify failure."""
        headline = i18n.t("alert.blocker_cancelled")
        lines = [f"📋 {self._task_title(a.task_id)}", i18n.t("alert.upstream", blocker=blocker)]
        text = human.compose_alert(headline, lines)
        self._notify_routed(text, None, event_key=f"blocker-cancelled:{a.task_id}")

    def _close_task(self, slug: str) -> None:
        """Close the todo task on success so it is not re-dispatched. A close
        failure must never break collect."""
        try:
            from . import tasks
            tasks.mark_done(slug)
        except Exception as e:  # noqa: BLE001 - a close failure must not break collect
            # …but it must not be silent either. play-store-3 (2026-09-10) went
            # ledger=done with its markdown still `open` and nothing said why;
            # the three leaves depending on it stayed blocked for 15 hours.
            # reconcile_closed() below re-tries; this line is what tells a
            # human which of the two is lying.
            print(f"[orchestrator] _close_task: mark_done({slug}) failed — "
                  f"{type(e).__name__}: {e}")
        self._propagate_downstream(slug)

    def _close_with_evidence(self, a: Assignment) -> None:
        """`_close_task`, after stamping the evidence reconcile_closed reads: the
        md's fingerprint as it was just BEFORE this close. If the close write is
        lost or rewound, the md still matches it; once a human reopens the task
        it never will. Persisting `a` is the caller's job — both callers save it
        right after."""
        try:
            from . import tasks
            a.close_fingerprint = tasks.md_fingerprint(a.task_id)
        except Exception:  # noqa: BLE001 - missing evidence only means the old re-close
            a.close_fingerprint = ""
        self._close_task(a.task_id)

    # A DONE row older than this is history, not a close in progress: its task
    # may legitimately have been reopened by a human since. Bounding the sweep
    # keeps a deliberate `cli reopen` from being undone weeks later. Inside the
    # window the close evidence (Assignment.close_fingerprint) is what protects a
    # reopen — the bound alone let the 2026-09-17 one be undone two minutes in.
    CLOSE_BACKSTOP_S = 24 * 3600

    def reconcile_closed(self, now: float | None = None) -> list[str]:
        """Re-close a task whose ledger row is DONE but whose todo status is still
        open — the two must agree, and `_close_task` is best-effort, so a lost
        write here (an exception it swallowed, a file the vault sync rewound)
        would otherwise leave the task un-dispatchable AND un-finished forever:
        dispatch() skips DONE rows, task_deps never sees `done`, and every
        dependant stays `blocked`. Returns the slugs it closed.

        "ledger DONE + md open" is also exactly what a human reopen looks like —
        a hand edit or `cli reopen`, neither of which touches the ledger. On
        2026-09-17 macp-dashboard-public-access was reopened at 06:49, two minutes
        after reconcile_merged closed it, and this sweep closed it again as a
        "lost" close. So a row that carries close evidence (close_fingerprint:
        the md's bytes just before the orchestrator closed it) is re-closed only
        while the md still matches it — a lost or rewound close leaves those
        bytes behind exactly. An md that changed since is someone's reopen: it
        stays open and the skip is logged once (history `reopen-respected`),
        not on every tick of the 24h window. A row with no evidence (older
        build) keeps the old unconditional re-close, without reading the md."""
        out: list[str] = []
        try:
            from . import tasks
            now = now if now is not None else dt.datetime.now().timestamp()
            for a in self.ledger.all():
                try:
                    if a.state != DONE:
                        continue
                    # A rescoped parent's ledger row is DONE but its todo is left OPEN
                    # on purpose — it is now a bundle, closed only once its children
                    # finish (AC5's reconcile_parents). Skip it so this sweep does not
                    # re-close the open bundle early.
                    if any(isinstance(h, dict) and h.get("type") == "rescoped"
                           for h in (a.history or [])):
                        continue
                    if now - _parse_ts(a.updated_ts) > self.CLOSE_BACKSTOP_S:
                        continue
                    status = tasks.task_status(a.task_id)
                    if status != "open":
                        continue          # done/cancelled/missing: nothing to heal
                    evidence = getattr(a, "close_fingerprint", "")
                    if evidence:
                        current = tasks.md_fingerprint(a.task_id)
                        if not current:
                            continue      # unreadable right now: no verdict either way
                        if current != evidence:
                            if not any(h.get("type") == "reopen-respected"
                                       for h in a.history):
                                print(f"[orchestrator] reconcile_closed: {a.task_id} is "
                                      f"ledger=done but todo=open and its markdown changed "
                                      f"after the orchestrator closed it — respecting a "
                                      f"human reopen, left open")
                                a.history.append({"ts": m.now_iso(),
                                                  "type": "reopen-respected",
                                                  "from": "orchestrator"})
                                # updated_ts deliberately untouched: it anchors the
                                # 24h window, and a note is not a new close.
                                self.ledger.save(a)
                            continue
                    if not tasks.mark_done(a.task_id):
                        continue
                    print(f"[orchestrator] reconcile_closed: {a.task_id} was ledger=done "
                          f"but todo=open — closed (the RESULT-time close did not land)")
                    out.append(a.task_id)
                except Exception as e:  # noqa: BLE001 - one row's defect
                    print(f"[orchestrator] reconcile_closed: {getattr(a, 'task_id', '?')}: "
                          f"{type(e).__name__}: {e}")
        except Exception as e:  # noqa: BLE001 - never raise into tick()
            print(f"[orchestrator] reconcile_closed: sweep aborted: {e}")
        return out

    def _compose_propagate_alert(self, proj: str, created: list[str]) -> str:
        """Self-limiting alert for downstream integration-checks just created. The
        count + the ask are in the headline; the slug list rides as a word-clipped
        tail (each slug is one whitespace-free token, so it is never bisected)."""
        headline = i18n.t("alert.propagate", proj=proj, n=len(created))
        return human.compose_alert(headline, tail=", ".join(created))

    def _propagate_downstream(self, slug: str) -> None:
        """If the just-landed task belongs to a PLATFORM project (its .macp declares
        `provides`) that other projects depend on, create one integration-check task
        per direct consumer so an upstream change doesn't silently break them.
        Best-effort + no-op unless deps are declared; dedups by existing file."""
        try:
            from ..macp import config as MC, deps as MD
            a = self.ledger.load(slug)
            wd = (a.payload or {}).get("working_dir") if a else None
            if not wd:
                return
            root = MC.find_project_root(wd)
            cfg = MC.load_project(root) if root else None
            if not cfg or not cfg.provides:          # only platform projects fan out
                return
            proj = MD.project_name(root, cfg)
            nodes = MD.scan()
            created = []
            for spec in MD.integration_task_specs(proj, nodes,
                                                  reason=f"'{proj}' 변경 머지 ({slug})"):
                path = MD.VAULT_ROOT / "Todo" / "items" / f"{spec['slug']}.md"
                if path.exists():
                    continue                          # dedup — don't recreate
                MD.write_integration_task(spec)
                created.append(spec["slug"])
            if created:
                # Digest tier: the checks were CREATED automatically and their
                # outcomes alert on their own — nothing to do right now.
                self._notify_routed(self._compose_propagate_alert(proj, created),
                                    None, level=kakao_format.LEVEL_DIGEST,
                                    event_key=f"propagate:{proj}")
        except Exception:
            pass

    def _retry(self, a: Assignment) -> None:
        # #1: re-confirm the todo is still OPEN before re-dispatching. A task can
        # land + be closed between the FAIL reply and this retry — e.g. the reaper
        # failed it while a late-but-successful runner reply was in flight, or a
        # human merged + closed it. Re-sending then burns a whole pipeline run on
        # already-done work (observed 2026-07-30: ai-stock-trading-5h merged via
        # PR #19 and closed, yet re-ran the next morning and failed on a usage
        # limit). dispatch() has a DONE guard, but _retry is reached directly from
        # collect() and bypasses it — so the guard has to live here too.
        # Fail-open: only a definite done/cancelled stops the retry; open / None /
        # a missing file still retries, so a status-read glitch never strands a
        # genuinely-failed task.
        from . import tasks
        try:
            status = tasks.task_status(a.task_id)
        except Exception:
            status = None
        if status in ("done", "cancelled", "canceled"):
            a.state = DONE
            a.updated_ts = m.now_iso()
            a.history.append({"ts": m.now_iso(), "type": "retry-skipped-closed",
                              "from": "orchestrator"})
            # Evidence for reconcile_closed: this path wrote nothing, so the md's
            # CLOSED bytes are the reference — no open md can match them, and a
            # later reopen is left alone rather than read as a lost close.
            a.close_fingerprint = tasks.md_fingerprint(a.task_id)
            return   # caller (collect) persists via ledger.save
        a.attempts += 1
        a.state = DISPATCHED
        env = m.Envelope(to=a.agent_id, type=m.TASK_ASSIGN, task_id=a.task_id,
                         payload=a.payload)
        a.corr_id = env.corr_id
        # #2: re-dispatch over the agent's OWN transport (dispatch uses
        # _mailbox_for), not the default self.mb. A remote git:// agent whose
        # retry went to the local default mailbox never received it.
        try:
            mb = self._mailbox_for(self.reg.get(a.agent_id))
        except KeyError:
            mb = self.mb   # agent gone from the registry: fall back, never crash collect
        mb.send(env)

    # -- Human parking / re-nudge (never fail) ----------------------------
    def _backoff_seconds(self, agent: Agent, idx: int) -> float:
        sched = agent.renudge_backoff or ["1d", "3d", "7d"]
        return parse_duration(sched[min(idx, len(sched) - 1)])

    def _nudge(self, agent: Agent, a: Assignment) -> None:
        env = m.Envelope(to=agent.id, type=m.TASK_ASSIGN, task_id=a.task_id,
                         payload=a.payload)
        # Immediate on purpose — unlike the first-delivery [assign] FYI
        # (human.deliver_pending, digest tier), a re-nudge only fires after a
        # human has sat on the ask for days (1d/3d/7d backoff): it IS the
        # escalation. No event key — last_nudge_ts/backoff already pace it, and
        # the router's window runs on the wall clock, which would fight the
        # injected-now clock the backoff (and its tests) are driven by.
        self._notify_routed(human.compose_kakao(env, agent), None)
        a.last_nudge_ts = m.now_iso()
        a.nudge_index += 1

    @staticmethod
    def _umbrellas_in_progress() -> set[str]:
        """Parents with at least one open child (tasks.parents_with_open_children)."""
        try:
            from . import tasks
            from .. import cli
            return tasks.parents_with_open_children(cli.DB_PATH)
        except Exception:  # noqa: BLE001 — renudge must never raise into tick()
            return set()

    def _nudge_target(self, a: Assignment) -> Agent | None:
        """Which human to nudge about a parked assignment.

        AWAITING_HUMAN is reached two ways: the task was dispatched to a human
        agent, or an AI agent worked it and handed the remainder back because
        the rest needs a real device, a credential, or an interactive login.
        This loop used to filter on the ASSIGNEE being human, which silently
        covered only the first kind — AI-parked work was skipped on every tick
        forever. parent-match-app-16b sat 10.4 days that way with its code
        complete and every reproducible gate green, and nothing ever said so
        (stale.py does not catch it either: it looks for tasks with NO
        assignment). Route by who has to act, not by who was assigned.
        """
        try:
            assignee = self.reg.get(a.agent_id)
        except KeyError:
            assignee = None
        if assignee is not None and assignee.is_human:
            return assignee
        owner = self.reg.for_project(a.task_id)
        if owner is not None and owner.is_human:
            return owner
        humans = self.reg.humans()
        return humans[0] if humans else None

    def renudge_due(self, now: float | None = None) -> list[Assignment]:
        """Re-nudge humans whose backoff has elapsed. Bounded, never fails."""
        now = now if now is not None else dt.datetime.now().timestamp()
        nudged: list[Assignment] = []
        umbrellas: set[str] | None = None
        for a in self.ledger.all():
            if a.state != AWAITING_HUMAN:
                continue
            if umbrellas is None:          # one DB read per call, only when needed
                umbrellas = self._umbrellas_in_progress()
            if a.task_id in umbrellas:
                continue                   # its children carry the work — nothing to ask
            agent = self._nudge_target(a)
            if agent is None:
                continue
            since = _parse_ts(a.last_nudge_ts or a.created_ts)
            if now - since >= self._backoff_seconds(agent, a.nudge_index):
                self._nudge(agent, a)
                self.ledger.save(a)
                nudged.append(a)
        return nudged

    def _queued_task_ids(self) -> set[str]:
        """task_ids whose TASK_ASSIGN is still sitting UNACKED in an agent's inbox.

        Such a task has been dispatched but not yet picked up — it is waiting in
        line, not stalled, and must not be reaped. Runners execute serially and a
        staged pipeline routinely runs longer than stale_timeout_s, so anything
        dispatched behind one would otherwise be failed before it ever started.
        """
        queued: set[str] = set()
        for agent in self.reg.agents:
            try:
                for env in self._mailbox_for(agent).receive(agent.id):
                    if env.type == m.TASK_ASSIGN and env.task_id:
                        queued.add(env.task_id)
            except _MAILBOX_READ_ERRORS as exc:
                # A write refused by the pytest tripwire (todo_system/conftest.py)
                # is a PermissionError, but not an unreachable mailbox.
                if getattr(exc, "never_swallow", False):
                    raise
                continue      # an unreachable mailbox must not block reaping others
        return queued

    def _stale_bound_for(self, a: Assignment) -> float:
        """The stale cutoff for one in-flight assignment, in seconds.

        A heavy task's stage legitimately runs up to stages.stage_timeout_for(its
        payload) with no PROGRESS touch (a stage emits none mid-run — only the
        runner does, between stages), so a flat stale_timeout_s reaps it the
        instant its own longer stage passes that flat bound. The run then dies as
        reaped-stale AND its unpushed commits are rewound by the next dispatch's
        branch prep (mulkko-32, 2026-09-13). Push the cutoff out to one full
        stage_timeout plus REAP_MARGIN of slack, but never below the configured
        stale_timeout_s (a caller wanting a longer floor keeps it). Defensive: a
        payload that cannot be sized falls back to stale_timeout_s rather than
        raising inside the reaper loop."""
        try:
            return max(self.stale_timeout_s,
                       stages.stage_timeout_for(a.payload) + stages.REAP_MARGIN)
        except Exception:
            return self.stale_timeout_s

    def reap_stale(self, now: float | None = None) -> list[Assignment]:
        """Mark tasks stuck in flight past their derived stale bound as failed — a
        runner that crashed mid-task would otherwise leave an assignment
        in_progress forever. Failed -> the cooldown/retry path handles it. Humans
        (parked, unbounded) are never reaped. The bound is per-row
        (_stale_bound_for), not a flat stale_timeout_s, so a heavy task's longer
        stage is not mistaken for a corpse.

        A task still queued in its agent's inbox is exempt: the timeout is meant to
        catch a runner that took the work and went silent, not one that has not
        reached the work yet. Without this, dispatching two tasks to a serial runner
        reliably failed the second — observed 2026-07-29 on
        macp-cleanup-supabase-and-links, reaped while still unacked in the inbox,
        behind a task that was executing normally.
        """
        now = now if now is not None else dt.datetime.now().timestamp()
        queued = self._queued_task_ids()
        reaped: list[Assignment] = []
        for a in self.ledger.all():
            if a.task_id in queued:
                continue      # dispatched but not yet picked up
            if a.state in _IN_FLIGHT and not a.kind.startswith("human") \
                    and now - _parse_ts(a.updated_ts) >= self._stale_bound_for(a):
                a.state = FAILED
                a.updated_ts = m.now_iso()
                a.history.append({"ts": m.now_iso(), "type": "reaped-stale",
                                  "from": "orchestrator"})
                self.ledger.save(a)
                reaped.append(a)
        return reaped

    def reconcile_merged(self) -> list[Assignment]:
        """#5: auto-close tasks whose PR already merged but whose todo stayed open.

        Until now every such case was fixed by hand (4b, 5a, 5e-1/3/4,
        worldgraph-15 …). Scans the ledger for AI assignments parked as
        AWAITING_HUMAN — the state a 'PR opened, not merged' task lands in — asks
        GitHub whether that branch's PR has since merged, and if so closes the
        todo + marks the ledger DONE.

        Ledger-scoped (not a full todo.db scan) by design: AWAITING_HUMAN is
        the 'awaiting human merge' set, and FAILED is included because a task
        whose PR merged can still end FAILED — a retry attempt after the merge
        dies on an empty diff or a usage limit, and that terminal write wins.
        Observed 2026-07-30: dancemirror-selfcam-mirror-toggle's PR #1 merged,
        but its last attempt failed on capacity, so the ledger read
        failed(capacity) and the AWAITING_HUMAN-only scan skipped it forever.
        REJECTED is swept for the same reason, and because leaving it out was an
        inconsistency rather than a decision: `dispatch()` already treats
        (FAILED, REJECTED) as one retry class and `dashboard` renders them as one
        "Failure" section, so a REJECTED assignment shows up as a failure but was the
        only kind the sweep could never clear. The state is only ever reached via
        an agent REJECT reply, so nothing further arrives to move it — and the
        one path that does close a cancelled todo (`_retry`) runs from collect(),
        i.e. only when a new reply comes. worldgraph-project-doc-sync sat
        REJECTED from 2026-07-21 to 2026-09-02 that way: diagnosed in its own note
        as "todo cancelled — task withdrawn, not a defect", yet rendered as the
        dashboard's only failure every day for 43 days. A failures section that is
        never empty stops being read.

        These states are small sets, so the `gh` calls stay bounded. Best-effort
        and gh-gated: the whole body is wrapped so it can NEVER raise into tick()
        (an exception there would freeze all dispatch), each ledger row is
        isolated so one bad row skips only itself (logged via _SkipTally), and
        it is a no-op when `gh` is absent — gh_pr_merged returns None → we
        skip. Only a definite
        True closes a task, so a task that is genuinely still awaiting merge (or
        genuinely failed with no landed work) is never closed out from under a
        human.

        A merged PR is not "the work is over" when Verify named a step still owed
        outside it (`blocked_on`). 2026-09-17 macp-dashboard-public-access: PR #129
        merged the code half, the Tailscale Funnel steps were a human's, and this
        sweep closed the task anyway — dropping it from every human-waiting list
        while the Funnel stayed off. So a merged row with a blocker whose todo is
        still open is parked (AWAITING_HUMAN, where Orchestrator.md shows the
        blocker) instead of closed; once the human closes the md, the "todo
        already closed" path below ends it."""
        reconciled: list[Assignment] = []
        skips = _SkipTally("reconcile_merged")
        try:
            from . import stages, tasks
            for a in self.ledger.all():
                try:
                    if a.state not in (AWAITING_HUMAN, FAILED, REJECTED) \
                            or a.kind.startswith("human"):
                        continue
                    branch = (a.payload or {}).get("branch") or f"worker/{a.task_id}"
                    repo = (a.payload or {}).get("repo")
                    note = "reconciled: PR merged out-of-band — task closed"
                    if stages.gh_pr_merged(branch, repo) is not True:
                        # No merged PR for this branch — but work can land WITHOUT one.
                        # ks-kmp-5a1 (2026-08-05) shipped inside a sibling task's PR
                        # #11, so its own branch was never pushed and `gh pr view`
                        # answered None forever, parking a finished task permanently:
                        # 33 re-dispatches of a task that had nothing left to do. The
                        # todo being closed is the authority on "this work is over",
                        # so trust it and stop holding a human slot for it.
                        if tasks.task_status(a.task_id) not in ("done", "cancelled"):
                            # Genuinely unfinished → leave it parked. One exception needs
                            # a human: a parked delegate whose PR a person CLOSED without
                            # merging will NEVER merge, so reconcile_merged would hold it
                            # AWAITING_HUMAN forever with no new signal. Alert once
                            # (history-guarded, like _MERGED_BUT_BLOCKED's park-once) so a
                            # person reopens/redoes/drops it. Only on a DEFINITE CLOSED
                            # from gh — None (no gh, error) stays silent, the safe side —
                            # and only for an AWAITING_HUMAN row (a FAILED/REJECTED one is
                            # a different flow). The row stays parked either way.
                            if (a.state == AWAITING_HUMAN
                                    and not any(h.get("type") == _CLOSED_UNMERGED
                                                for h in a.history)
                                    and stages.gh_pr_state(branch, repo) == "CLOSED"):
                                a.last_alert_kind = ALERT_CLOSED_UNMERGED
                                a.last_note = f"PR closed without merging — {branch}"[:200]
                                a.history.append({"ts": m.now_iso(), "type": _CLOSED_UNMERGED,
                                                  "from": "orchestrator"})
                                text = human.compose_alert(
                                    i18n.t("alert.closed_unmerged"),
                                    [f"📋 {self._task_title(a.task_id)}"])
                                self._notify_routed(
                                    text, event_key=f"closed-unmerged:{a.task_id}")
                                self.ledger.save(a)
                                print(f"[orchestrator] reconcile_merged: {a.task_id} PR "
                                      "closed unmerged — alerted a human once")
                            continue     # genuinely unfinished → leave it parked
                        note = "reconciled: todo already closed — no PR of its own to merge"
                    elif getattr(a, "blocked_on", ""):
                        # Merged, but a named step is still owed outside the PR (see
                        # the docstring). Checked only on this branch and only for rows
                        # with a blocker, so a plain merged row costs no extra md read.
                        if tasks.task_status(a.task_id) not in ("done", "cancelled"):
                            # Park once: re-park a FAILED/REJECTED row every time (else
                            # it re-dispatches onto a merged branch), but record an
                            # already-parked one only on first sight, not every tick.
                            if a.state != AWAITING_HUMAN or not any(
                                    h.get("type") == _MERGED_BUT_BLOCKED for h in a.history):
                                a.state = AWAITING_HUMAN
                                a.last_note = f"PR merged — still blocked on {a.blocked_on}"[:200]
                                a.history.append({"ts": m.now_iso(), "type": _MERGED_BUT_BLOCKED,
                                                  "from": "orchestrator"})
                                print(f"[orchestrator] reconcile_merged: {a.task_id} PR merged "
                                      f"but blocked_on is set — parked for a human, not closed")
                                self.ledger.save(a)
                            continue     # updated_ts kept: the park time stays the park time
                        note = ("reconciled: todo already closed — PR merged; the blocked_on "
                                "step was finished by a human")
                    # BEFORE the mutations below, deliberately: the episode quotes
                    # the parked-time last_note/updated_ts, which they overwrite.
                    self._record_false_alarm(a, note)
                    self._close_with_evidence(a)         # mark_done + downstream fan-out
                    a.state = DONE
                    a.updated_ts = m.now_iso()
                    # Overwrite the note too, not just the state. It still reads
                    # "auto_merge did NOT land … needs review" from the attempt that
                    # parked this — true then, false now — and Orchestrator.md renders
                    # last_note verbatim, so the resolved task keeps advertising work
                    # that no longer exists (observed on tennis-macro-monday-evening-
                    # slot-scan-failure, 2026-08-05).
                    a.last_note = note
                    # Clear the deferral flags with the state. A FAILED+job_running row
                    # closed here never gets the next runner reply that would reset
                    # them, so a stale `job_running` sat on a DONE row and made
                    # worker._runner_idle answer "busy" forever — the runner restart
                    # was deferred 63x over 6h (ai-stock-trading-lowturnover-ic-to-
                    # sharpe, 2026-09-26).
                    a.job_running = False
                    a.checkout_busy = False
                    a.history.append({"ts": m.now_iso(), "type": "reconciled-merged",
                                      "from": "orchestrator"})
                    self.ledger.save(a)
                    reconciled.append(a)
                except Exception as e:
                    # Per row, so one bad row skips only itself: a single raise
                    # (gh, todo.db, a ledger write) used to drop every merged-PR
                    # row sorted after it from the whole tick, silently.
                    tid = getattr(a, "task_id", "?") or "?"
                    skips.record(tid, e)
            skips.flush()
        except Exception as e:
            # Outer net stays: the sweep's own scaffolding (the import,
            # ledger.all()) can still fail, and this must never raise into tick().
            print(f"[orchestrator] reconcile_merged: sweep aborted: {e}")
        return reconciled

    def _record_false_alarm(self, a: Assignment, note: str) -> None:
        """Self-record a false alarm: a task parked under a FAILURE framing
        (suspected-failure / quarantine) that the sweep is now closing benignly
        ("PR merged out-of-band" / "todo already closed").

        Why: 2026-09-04 dripcoffee-integcheck-advote — a clean 0-commit RESULT
        was announced at 10:32 as "suspected failure, needs investigation" and the same assignment
        was reconciled benignly at 10:52. The refutation was machine-visible the
        whole time, but the planner's inputs carry no execution history, so this
        defect class lived outside the self-improvement loop until a human
        happened to read both messages. Episodes land in Memory/episodes/, which
        the planner already consumes.

        Contract: must run BEFORE the caller overwrites last_note/updated_ts —
        the episode quotes the parked-time values. Recurrence appends to the
        existing per-task file instead of creating another (a per-occurrence
        pattern once piled up 143 episode files and buried the signal). Own
        try/except: the caller's per-row net would otherwise skip this row, so
        an episode write error would cost the DONE transition it diagnoses — a
        diagnostic must never do that."""
        try:
            kind = getattr(a, "last_alert_kind", "")
            if kind not in FAILURE_ALERT_KINDS:
                return    # benign framing (or pre-field ledger row): not a false alarm
            # Vault root relative to the ledger (Worker/assignments -> vault) —
            # never a hardcoded path, so tests stay inside their tmp vault.
            episodes = self.ledger.root.parent.parent / "Memory" / "episodes"
            section = (
                f"## {m.now_iso()} 양성 종결 기록\n\n"
                f"- 실패 프레이밍: `{kind}` (파킹 {a.updated_ts})\n"
                f"- 파킹 사유(당시 last_note): {a.last_note or '-'}\n"
                f"- 양성 종결: {note}\n"
                f"- 참고: `Worker/assignments/{a.task_id}.json` · "
                f"Worker/runs/ 아래 {a.task_id} 최신 run\n"
            )
            # Anchored on the full task_id + the -false-alarm- literal, so a
            # slug that suffixes another can never inherit its episode.
            prior = sorted(episodes.glob(f"*-false-alarm-{a.task_id}.md"))
            if prior:
                text = prior[-1].read_text(encoding="utf-8").rstrip() + "\n\n" + section
                prior[-1].write_text(text, encoding="utf-8")
                return
            episodes.mkdir(parents=True, exist_ok=True)
            now = dt.datetime.now()
            header = (
                "---\n"
                "tags:\n  - memory\n  - episode\n  - worker\n  - false-alarm\n"
                f"event_date: {now:%Y-%m-%d}\n"
                f"recorded_date: {now:%Y-%m-%d}\n"
                f"slug: {a.task_id}\n"
                "kind: false-alarm\n"
                "---\n\n"
                f"# {now:%Y-%m-%d} — False alarm: {a.task_id}\n\n"
                "실패 프레이밍 알림으로 사람을 호출해 파킹했던 태스크가 양성 노트로\n"
                "자동 종결됐다 — 알림이 결과와 어긋난 사례. 재발하면 아래에 append된다.\n\n"
            )
            (episodes / f"{now:%Y-%m-%d-%H%M}-false-alarm-{a.task_id}.md").write_text(
                header + section, encoding="utf-8")
        except Exception:
            pass      # diagnostics never break the sweep

    def reconcile_unblocked(self) -> list[Assignment]:
        """Un-park tasks whose named blocker has since resolved.

        Parking a `blocked_on` failure stops it re-buying the pipeline against an
        unmet precondition, but on its own it trades a churn bug for a stall bug:
        nothing was watching the blocker, so a task parked on an upstream PR would
        sit in AWAITING_HUMAN forever after that PR merged. reconcile_merged only
        ever asks about a task's OWN PR and OWN todo, never about what it is
        waiting for.

        The blocker is free text whose first token is the upstream slug
        (`dripcoffee-bean-cards-1-… (PR #22, OPEN)`). If that names a task the
        vault knows and it is now done or cancelled, the wait is over: drop back
        to FAILED so the normal cooldown path re-dispatches it. Anything we cannot
        resolve to a finished task stays parked — an unrecognizable blocker really
        is a human's problem, and guessing would resurrect the churn.

        DONE and CANCELLED end the wait for opposite reasons, so they cannot share
        an exit. A done blocker delivered the precondition — requeue. A cancelled
        one never will: requeueing then buys a full pipeline run against a
        precondition nobody is going to meet, and the note it left ("<slug> is
        done — requeued") was actively false. So a cancellation keeps the task
        parked and pings a human to decide whether it is still wanted at all.
        `canceled` (one l) counts too — _retry() already reads all three spellings,
        and treating it as done here would silently re-dispatch.
        """
        out: list[Assignment] = []
        skips = _SkipTally("reconcile_unblocked")
        try:
            from . import tasks
            for a in self.ledger.all():
                try:
                    blocker = getattr(a, "blocked_on", "")
                    if a.state != AWAITING_HUMAN or not blocker:
                        continue
                    slug = blocker.split()[0].strip().rstrip(":,")
                    if slug == a.task_id:          # self-reference: never resolvable
                        continue
                    status = tasks.task_status(slug)
                    if status not in ("done", "cancelled", "canceled"):
                        continue
                    if status in ("cancelled", "canceled"):
                        # already surfaced: this branch stays true on every tick (the
                        # task keeps both AWAITING_HUMAN and its blocker), so without
                        # the guard it would re-ping forever.
                        if any(h.get("type") == "blocker-cancelled" for h in a.history):
                            continue
                        a.updated_ts = m.now_iso()
                        a.last_note = (f"상류 {slug} 취소됨 — 이 태스크가 아직 필요한지 "
                                       f"사람이 판단")[:200]
                        a.history.append({"ts": m.now_iso(), "type": "blocker-cancelled",
                                          "from": "orchestrator"})
                        self._notify_blocker_cancelled(a, slug)
                        self.ledger.save(a)
                        # deliberately NOT appended to `out`: tick() surfaces that list
                        # as `unblocked`, which means requeued. This task is still parked.
                        continue
                    a.state = FAILED               # re-enters the normal retry path
                    a.blocked_on = ""
                    a.updated_ts = m.now_iso()
                    a.last_note = f"unblocked: {slug} is done — requeued"[:200]
                    a.history.append({"ts": m.now_iso(), "type": "unblocked",
                                      "from": "orchestrator"})
                    self.ledger.save(a)
                    out.append(a)
                except Exception as e:
                    # Per row, so one bad row skips only itself: the rows queued
                    # behind it used to vanish from the whole tick. `slug` may not
                    # exist yet (`blocker.split()[0]` raises on a whitespace-only
                    # blocker), so name the row from the assignment instead.
                    tid = getattr(a, "task_id", "?") or "?"
                    skips.record(tid, e)
            skips.flush()
        except Exception as e:
            # Outer net stays: the sweep's own scaffolding (the import,
            # ledger.all()) can still fail, and this must never raise into tick().
            print(f"[orchestrator] reconcile_unblocked: sweep aborted: {e}")
        return out

    def reconcile_unparked(self) -> list[Assignment]:
        """Un-park a blocked task when the HUMAN signals the blocker is gone.

        reconcile_unblocked above can only resolve a blocker whose first token
        names a vault task. Most real blockers are not slugs at all — an absent
        `ANTHROPIC_API_KEY`, "tooling", a console setting nobody made — so those
        rows park forever. Until now the only exit was a human hand-operating the
        ledger (`python -m todo_system.orchestrator.human reply --status reject`),
        i.e. surgery on a git-tracked JSON file to say something the task file
        already said.

        The signal accepted here is that task file. A human who parks a task
        edits its `worker_mode:` to something non-dispatchable (`blocked`); when
        the blocker clears they set it back to `auto`. That transition is a
        deliberate edit of the source of truth — evidence, not inference — so it
        is safe to read as "go", and it is the one thing `if_then` asks for.

        The transition must be OBSERVED, not stamped at park time. Parking
        happens in collect(), and at that moment the md still says `auto`: the
        human only edits it after seeing the alert. So every tick records the
        currently observed mode in `parked_worker_mode`, and un-parking needs the
        PREVIOUS observation to be non-dispatchable and the current one
        dispatchable.

        Deliberately narrow, so parking that a human really must answer keeps
        waiting. `blocked_on` non-empty AND `last_alert_kind == ALERT_BLOCKED`
        structurally excludes PR-review waits, result-no-pr, failure-suspected,
        poison quarantine and NEEDS_INPUT. Every unknown fails SAFE (stay
        parked): no stamp yet (a row written by an older build), no `task_path`,
        a file that is gone, or an md whose `worker_mode` cannot be read.
        A missing observation is not evidence of a transition.

        Un-parks to FAILED — the normal retry path — and deliberately does NOT
        raise `refail_count`: an external blocker was never this task's defect.
        It also does NOT refresh `updated_ts`, which is what dispatch() anchors
        its cooldown on (:266); refreshing it would charge the human's explicit
        "go" a fresh cooldown, so the un-park would not take effect for another
        window. The un-park time is recorded honestly in `history` instead.
        (reconcile_unblocked deliberately differs and does refresh.)

        Re-stamping the new mode is what CONSUMES the signal. Without it the pair
        (stamped `blocked`, md `auto`) stays true forever, and a task that
        re-parks on the same blocker would be un-parked again on the very next
        tick — an infinite un-park loop.

        Note `AWAITING_HUMAN` stays OUT of assignment.TERMINAL. Putting it there
        would let dispatch() re-run every parked row, including the PR reviews
        and NEEDS_INPUT questions this method goes out of its way to leave alone.
        """
        out: list[Assignment] = []
        skips = _SkipTally("reconcile_unparked")
        try:
            from . import repos, tasks
            # ledger.root is Worker/assignments -> the vault root is two up.
            # `task_path` is stored vault-relative (Todo/items/<slug>.md).
            vault_root = Path(self.ledger.root).parent.parent
            for a in self.ledger.all():
                try:
                    if a.state != AWAITING_HUMAN:
                        continue
                    if not getattr(a, "blocked_on", ""):
                        continue
                    if getattr(a, "last_alert_kind", "") != ALERT_BLOCKED:
                        continue
                    raw = (a.payload or {}).get("task_path") or ""
                    if not raw:
                        continue
                    p = Path(raw)
                    if not p.is_absolute():
                        p = vault_root / p
                    if not p.is_file():
                        continue
                    mode = repos.read_task_worker_mode(p)
                    if mode is None:      # unreadable/absent field — no evidence
                        continue
                    prev = getattr(a, "parked_worker_mode", "")
                    if prev and prev not in tasks.DISPATCHABLE_MODES \
                            and mode in tasks.DISPATCHABLE_MODES:
                        a.state = FAILED               # re-enters the normal retry path
                        a.blocked_on = ""
                        a.parked_worker_mode = mode    # consume the signal — see above
                        a.last_note = (f"blocker 해소 신호: worker_mode {prev}→{mode} "
                                       f"— 재큐")[:200]
                        a.history.append({"ts": m.now_iso(), "type": "unparked-worker-mode",
                                          "from": "orchestrator",
                                          "prev_mode": prev, "mode": mode})
                        self.ledger.save(a)
                        out.append(a)
                    elif mode != prev:
                        # Only an observation: record what the md says now so a later
                        # tick can see the transition. State is untouched.
                        a.parked_worker_mode = mode
                        self.ledger.save(a)
                except Exception as e:
                    # Per row: an unreadable md or a non-string `task_path` is one
                    # row's defect, not the sweep's. Before this, the row raising
                    # here silently parked every row sorted after it, forever.
                    tid = getattr(a, "task_id", "?") or "?"
                    skips.record(tid, e)
            skips.flush()
        except Exception as e:
            # Outer net stays: the sweep's own scaffolding (the imports,
            # vault_root, ledger.all()) can still fail, and this must never raise
            # into tick().
            print(f"[orchestrator] reconcile_unparked: sweep aborted: {e}")
        return out

    def _parent_seen_path(self) -> Path:
        return self.ledger.root.parent / "_state" / "parent_reconcile.json"

    def _parent_already_surfaced(self, slug: str) -> bool:
        try:
            seen = json.loads(self._parent_seen_path().read_text(encoding="utf-8"))
        except Exception:
            seen = {}
        return slug in seen

    def _record_parent_surfaced(self, slugs: list[str]) -> None:
        """A parent has no Assignment, so there is no ledger history to dedup on —
        the state lives in a gitignored file, like agent_seen.json."""
        try:
            p = self._parent_seen_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            try:
                seen = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                seen = {}
            for slug in slugs:
                seen[slug] = m.now_iso()
            p.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _notify_parent_complete(self, rows: list[dict]) -> None:
        """One ping listing parents whose children are all finished. Never break
        the tick on a notify failure."""
        headline = i18n.t("alert.parent_complete", n=len(rows))
        row_lines = [f"- {r['slug']} ({r.get('title') or '-'})" for r in rows]
        text = human.compose_alert(headline, row_lines)
        # Immediate (a close/keep judgement is being asked); no event key —
        # _record_parent_surfaced already guarantees once-per-parent.
        self._notify_routed(text, None)

    def reconcile_parents(self) -> list[str]:
        """Surface parent tasks whose children have all finished — and AUTO-CLOSE the
        ones this orchestrator created by auto-rescoping (AC5).

        A parent is excluded from the dispatch query (`is_parent=0`), so no
        worker ever closes one; and the Orchestrator.md dashboard renders the
        Ledger, which a parent has no entry in. So a finished project could stay
        `open` indefinitely with a stale `next_action` — the only thing that ever
        caught it was a human noticing.

        For an ORDINARY parent, surfacing-only stays the rule: closing it is a
        judgement about whether the project is actually finished (children may
        have been descoped rather than done), which belongs to a human. But a
        bundle carrying `auto_rescoped: true` was created mechanically by
        reconcile_rescope from a worker's split plan, so once every child is done
        the bundle has no independent work left — it is closed automatically
        (`tasks.mark_done`). A failed close is left unrecorded so it retries next
        tick; a successful one drops out of `parents_needing_review` on its own
        (its status is no longer open). Paths resolve through `cli.*` (env-aware)
        so the daemon's cwd no longer has to be the vault root.

        Never raises into tick(): outer net + per-row isolation, like
        reconcile_split_parents. One bad parent row is skipped and named via
        _SkipTally; the rows behind it are still closed/surfaced, and the skipped
        row stays unrecorded so it retries next tick.
        """
        processed: list[str] = []
        skips = _SkipTally("reconcile_parents")
        try:
            from . import tasks, repos
            from .. import cli
            rows = [r for r in tasks.parents_needing_review(cli.DB_PATH)
                    if not self._parent_already_surfaced(r["slug"])]
            if not rows:
                return []
            closed: list[str] = []
            to_surface: list[dict] = []
            for r in rows:
                try:
                    slug = r["slug"]
                    fm = repos._frontmatter(Path(cli.ITEMS_DIR) / f"{slug}.md")
                    if str(fm.get("auto_rescoped") or "").strip().lower() in ("true", "1", "yes", "on"):
                        if tasks.mark_done(slug):
                            closed.append(slug)
                            print(f"[orchestrator] reconcile_parents: auto-closed rescope "
                                  f"bundle {slug} (all children done)")
                        # a failed close stays unrecorded below, so it retries next tick
                    else:
                        to_surface.append(r)
                except Exception as e:  # noqa: BLE001 — per-row isolation
                    skips.record((r.get("slug") if isinstance(r, dict) else None) or "?", e)
            skips.flush()
            if to_surface:
                self._notify_parent_complete(to_surface)
            processed = closed + [r["slug"] for r in to_surface]
            self._record_parent_surfaced(processed)
        except Exception as e:  # noqa: BLE001 — never raise into tick()
            print(f"[orchestrator] reconcile_parents: sweep aborted: {e}")
            return []
        return processed

    def _close_split_parent_row(self, a: Assignment, note: str, kind: str) -> None:
        """Transition a parked split-parent ledger row to DONE, reusing
        reconcile_merged's battle-tested close shape: record any false alarm (inert
        for the benign `blocked` framing), stamp close_fingerprint + mark_done via
        _close_with_evidence, then set DONE + tidy the deferral flags + history.

        _close_with_evidence is what keeps this from fighting reconcile_closed: it
        stamps the md's bytes just BEFORE the close, and mark_done appends CLOSE_TRACE
        on a real open->done close, so a later human reopen diverges from the stamp
        and reconcile_closed respects it. On an already-done md (the AC3 branch)
        mark_done is a near no-op and the stamp is the done bytes — reconcile_closed
        still never re-opens, since it only acts on ledger=DONE + md=open.
        Idempotent: once state is DONE the sweep's row guard skips the row."""
        self._record_false_alarm(a, note)     # inert unless framing was a FAILURE kind
        self._close_with_evidence(a)          # stamp fingerprint + mark_done (+ fan-out)
        a.state = DONE
        a.updated_ts = m.now_iso()
        a.last_note = note[:200]
        # Clear the deferral flags with the state, exactly as reconcile_merged does:
        # a FAILED+job_running row closed here never gets the next runner reply that
        # would reset them, so a stale flag would otherwise sit on a DONE row.
        a.job_running = False
        a.checkout_busy = False
        a.history.append({"ts": m.now_iso(), "type": kind, "from": "orchestrator"})
        self.ledger.save(a)

    def reconcile_split_parents(self) -> list[str]:
        """Auto-close a parent a human split BY HAND, and tidy its parked ledger row.

        When Implement parks a task as "too big for one stage — re-scope it" and a
        human (or an interactive session) splits it by hand into children — each
        child `parent: "[[<parent>]]"`, with NO `auto_rescoped`/`is_parent` stamp —
        nothing closed the parent once every child finished:
        `parents_needing_review` only sees `is_parent=1` bundles (cause ①), so the
        parent's ledger row sat AWAITING_HUMAN forever, re-nudged by renudge_due and
        stuck in the dashboard's "awaiting human" list. That is exactly what happened to
        dancemirror-unified-projects (2026-10-10~11).

        This is the ledger-DRIVEN, gh-INDEPENDENT twin of reconcile_merged's "todo
        already closed" path. Per row (AWAITING_HUMAN/FAILED, non-human):

          - AC3 — md already done/cancelled (a human closed it directly): tidy the
            ledger row to DONE so renudge_due stops and it drops out of "awaiting human".
            This generalizes reconcile_merged's close but without the `gh` call, so a
            `gh` hiccup can no longer abort it mid-tick.
          - AC1/AC2 — md still open: close the parent md AND tidy the ledger, but
            ONLY when the park is an oversize re-scope park (last_alert_kind ==
            ALERT_BLOCKED and re-scope wording in blocked_on/last_note — see
            rescope.is_oversize_rescope_park) AND every child is done. The
            "children >=1, all done" co-condition (tasks.child_status_rollup) excludes
            external-blocker parks (which have no children) on its own; anything short
            of it stays parked — false negative over false positive, the requirement's bias.
            A `result-no-pr` row (a 0-commit run whose only output was splitting
            the task into children) counts as the same re-scope signal: the
            children existing is the evidence, and they must all be done.

        Runs BEFORE reconcile_merged in tick() so the row is already DONE when that
        gh-gated sweep iterates and its state guard skips it (no double close). An
        ordinary project umbrella has no parked re-scope row at all, so it is never
        seen here and stays surfaced-only (reconcile_parents). Never raises into
        tick(): outer net + per-row isolation, like reconcile_unblocked/unparked.
        Lazy sibling imports only (engine isolation).
        """
        out: list[str] = []
        skips = _SkipTally("reconcile_split_parents")
        try:
            from . import rescope, tasks
            from .. import cli
            for a in self.ledger.all():
                try:
                    if a.state not in (AWAITING_HUMAN, FAILED) or a.kind.startswith("human"):
                        continue
                    status = tasks.task_status(a.task_id)
                    if status in ("done", "cancelled", "canceled"):
                        # AC3: the md is already closed; only the ledger lags behind.
                        self._close_split_parent_row(
                            a, "reconciled: todo already closed — ledger tidied",
                            "reconciled-ledger-tidy")
                        out.append(a.task_id)
                        continue
                    # AC1/AC2: md still open — close it only on the oversize-rescope
                    # signal AND the all-children-done co-condition.
                    kind = getattr(a, "last_alert_kind", "")
                    if kind == ALERT_RESULT_NO_PR:
                        # A 0-commit run whose output was the split itself: the
                        # worker created the children and handed the parent back
                        # (mulkko-95, 2026-10-13). The children ARE the re-scope.
                        pass
                    elif kind != ALERT_BLOCKED or not rescope.is_oversize_rescope_park(
                            getattr(a, "blocked_on", ""), getattr(a, "last_note", "")):
                        continue
                    roll = tasks.child_status_rollup(a.task_id, cli.DB_PATH)
                    if not roll["all_closed"]:
                        continue      # not all children done (or none) -> stay parked
                    self._close_split_parent_row(
                        a, f"manual-split parent auto-closed: {roll['n']} children done",
                        "reconciled-manual-split")
                    out.append(a.task_id)
                except Exception as e:  # noqa: BLE001 — per-row isolation
                    skips.record(getattr(a, "task_id", "?") or "?", e)
            skips.flush()
        except Exception as e:  # noqa: BLE001 — never raise into tick()
            print(f"[orchestrator] reconcile_split_parents: sweep aborted: {e}")
        return out

    def reconcile_queue(self) -> list[str]:
        """#22: close out `Worker/queue.json` entries whose task is DONE.

        `queue.json` lost its consumer in the 2026-07-29 v2 cutover — the v1
        `<prefix>secondbrain.worker` job that used to claim `pending` entries
        was retired, but the todo-sync daemon still calls `scan_queue()` to fill
        it, and the dashboard still renders it. So entries were only ever
        appended: a task could be merged and DONE in the ledger while its queue
        entry sat `pending` forever, which is what made the queue view show
        phantom work (issue #22, re-observed 2026-07-31).

        DONE only, deliberately. A FAILED ledger state means "the last attempt
        failed", not "this task is over" — a capacity/rate-limit failure is
        retried once quota returns, so `pending` is the honest queue rendering.
        Mirroring FAILED here would also fight `worker.doctor_recover_queue`,
        which resets a failed entry back to pending for its own auto-retry:
        the two would flip the same entry on every tick.

        Best-effort and lock-guarded: wrapped so it can never raise into tick(),
        since an exception here would freeze all dispatch. Per-entry isolation
        inside the lock: one bad entry is skipped and named via _SkipTally, the
        rest are still reconciled, and save_queue still runs once under the lock.
        Each entry's verdict and values are computed before it is mutated, so a
        skipped entry is never left half-written.
        """
        changed: list[str] = []
        skips = _SkipTally("reconcile_queue")
        try:
            from .. import worker as _worker
            done = {a.task_id for a in self.ledger.all() if a.state == DONE}
            assigned = {a.task_id for a in self.ledger.all()}
            with _worker.queue_lock():
                queue = _worker.load_queue()
                for run in queue.get("runs", []):
                    try:
                        if run.get("status") not in ("pending", "in_progress"):
                            continue
                        slug = run.get("slug")
                        finished = run.get("finished_at") or m.now_iso()
                        if slug in done:
                            run["status"] = "completed"
                            run["finished_at"] = finished
                            changed.append(slug)
                            continue
                        if self._unclaimable(run, assigned):
                            run["status"] = "skipped"
                            run["finished_at"] = finished
                            run["failure_reason"] = "task is no longer dispatchable"
                            changed.append(slug)
                    except Exception as e:  # noqa: BLE001 — per-row isolation
                        skips.record((run.get("slug") if isinstance(run, dict) else None) or "?", e)
                skips.flush()
                if changed:
                    _worker.save_queue(queue)
        except Exception as e:  # noqa: BLE001 — never raise into tick()
            print(f"[orchestrator] reconcile_queue: sweep aborted: {e}")
        return changed

    def _unclaimable(self, run: dict, assigned: set[str]) -> bool:
        """A pending queue run that nothing can ever claim or close.

        `load_dispatchable` only yields tasks whose worker_mode is dispatchable,
        so a task edited down to human-only (the field removed, or set to
        `blocked`/`manual`) drops out of every future candidate set — but its
        old `pending` run stays in the view forever, because reconcile only ever
        closed runs the ledger had marked DONE. macp_monitor reads that view and
        reports the leftover as "pending with no assignment → dispatch is not
        happening (routing failure suspected)", which is a false alarm about a
        task that is simply a person's to do: linkedin-profile-refresh sat there
        85h that way, human-owned with no worker_mode at all (2026-09-06).

        Fail-closed on doubt, unlike the loader's md gate: a missing or
        unreadable markdown returns False and the run is left pending. Voiding a
        real run because a file could not be read would be far worse than
        leaving one stale entry in a derived view. A run whose task has ANY
        ledger assignment is never touched — that is live work, and its reply
        path is what closes it.
        """
        from . import repos
        from .tasks import DISPATCHABLE_MODES

        slug = run.get("slug")
        if not slug or slug in assigned:
            return False
        path = run.get("task_path")
        if not path:
            return False
        if not Path(path).is_file():
            return self._task_md_never_on_head(path)
        return repos.read_task_worker_mode(path) not in DISPATCHABLE_MODES

    def _task_md_never_on_head(self, raw: str) -> bool:
        """A queued run's markdown is missing on disk — is that *definitive*?

        Only when git itself answers: the vault is a work tree and HEAD has no
        such path. That is the 2026-10-21 shape — `dripcoffee-discovery-20260920`
        (738h) and `notify-channel-1-telegram-backend` (211h) were queued from a
        checkout sitting on a worker branch, and their md only ever existed on
        those never-merged branches, so nothing could claim or close the runs.
        Anything short of a clean git answer (not a repo, git missing, timeout,
        path outside the tree) keeps the fail-closed default: stay pending.
        """
        import subprocess
        try:
            vault_root = Path(self.ledger.root).parent.parent
            p = Path(raw)
            if not p.is_absolute():
                p = vault_root / p
            if p.exists():
                return False
            top = subprocess.run(["git", "-C", str(vault_root), "rev-parse", "--show-toplevel"],
                                 capture_output=True, text=True, timeout=15)
            if top.returncode != 0:
                return False
            rel = p.resolve().relative_to(Path(top.stdout.strip()).resolve())
            probe = subprocess.run(["git", "-C", str(vault_root), "cat-file", "-e", f"HEAD:{rel.as_posix()}"],
                                   capture_output=True, text=True, timeout=15)
            return probe.returncode != 0 and not probe.stdout and "Not a valid object name HEAD" not in probe.stderr
        except Exception:
            return False

    def usage_hold(self) -> str | None:
        """Reason string when the Claude-usage guard says to stop starting new
        work (subscription quota pacing — see todo_system.usage_guard), else
        None. Fail-open and never raises: a guard failure must not freeze the
        tick. After the verdict, the inactive accounts' snapshots are refreshed
        (leaf 4, `usage_guard.refresh_inactive_accounts`: TTL ×4, their own
        lock/backoff) for the dashboard and the runner's `exhausted_until` —
        never part of the verdict, and its failure is swallowed on its own."""
        try:
            from todo_system import usage_guard
            d = usage_guard.check()
            try:
                usage_guard.refresh_inactive_accounts()
            except Exception:
                pass
            if not d.get("allowed", True):
                return d.get("reason") or "usage guard blocked"
        except Exception:
            pass
        return None

    def _advote_bridge_tick(self) -> None:
        """Bridge advote user suggestions into the pipeline (promote a ticket + drop
        a blocked task) and reverse-report macp run state back to advote. Fully
        self-contained and fail-open: no secrets, network down, or any error is
        swallowed here so a feedback bridge can never stall a tick. The min-poll
        interval and the HEAD==main guard live inside run_once. The bridge is an
        author-only opt-in: with no <vault>/.secrets/advote.json we return before
        even importing the module, so a bare checkout's tick does no work at all.
        See todo_system.advote_bridge."""
        try:
            # ledger.root is Worker/assignments -> the vault root is two up.
            vault_root = Path(self.ledger.root).parent.parent
            if not (vault_root / ".secrets" / "advote.json").exists():
                return                            # opt-in off → silent, no import per tick
            from .. import advote_bridge
            from ..kakao_format import LEVEL_DIGEST
            advote_bridge.run_once(
                vault_root=vault_root,
                notify=lambda msg: self._notify_routed(
                    msg, None, level=LEVEL_DIGEST, event_key="advote-bridge"),
            )
        except Exception as e:
            print(f"[orchestrator] advote bridge tick skipped (fail-open): {e}")

    def _beta_sweep_tick(self) -> None:
        """Start the beta catch-up sweep when it is due — publish a main that the
        merge-time publisher missed (DanceMirror 35cdd60, 2026-09-25).

        Returns immediately: the gate only writes a state file and spawns a
        DETACHED process, because a sweep contains a gradle build that would
        otherwise freeze dispatch for up to 30 minutes. The min-poll interval,
        the HEAD==main guard and the "already running" check all live inside
        spawn_sweep_if_due. Fail-open like the advote bridge above — a publisher
        problem must never stall a tick. See todo_system.orchestrator.beta_publish.
        """
        try:
            from . import beta_publish
            vault_root = Path(self.ledger.root).parent.parent
            beta_publish.spawn_sweep_if_due(
                vault_root=vault_root,
                progress=lambda msg: print(f"[orchestrator] {msg}"))
        except Exception as e:
            print(f"[orchestrator] beta sweep tick skipped (fail-open): {e}")

    def _project_stall_tick(self) -> list[str]:
        """Open ONE human decision task for a project that has quietly stalled.

        Deterministic (no LLM): project_stall.run_once re-derives the dashboard's
        umbrella/leaf rollup, classifies each live project, and writes a single
        `<project>-next-direction.md` + one deduped alert when a project has gone
        quiet (all leaves done, or no auto leaf + no human action + idle 48h+).
        The 1h throttle, the open-task dedup and the 14-day re-suppression all
        live inside run_once. Fail-open like the advote/beta sweeps — a stall
        detector must never stall a tick. Returns the created slugs. See
        todo_system.orchestrator.project_stall."""
        try:
            from . import project_stall
            # ledger.root is Worker/assignments -> the vault root is two up.
            vault_root = Path(self.ledger.root).parent.parent
            return project_stall.run_once(
                vault_root=vault_root,
                # A project-direction decision is a human call with no urgency and
                # the event key already guarantees once-per-project, so route it
                # like reconcile_parents' surfacing ping (default tier, deduped).
                notify=lambda msg, *, event_key: self._notify_routed(
                    msg, None, event_key=event_key),
            )
        except Exception as e:
            print(f"[orchestrator] project stall tick skipped (fail-open): {e}")
            return []

    def reconcile_resumes(self) -> list[str]:
        """Consume conditional deferred-resume reservations (`cli resume`).

        A human who fixes a task's md — adding a `long_job:`, clearing a bad
        `allowed_tools` — has to wait for the vault to carry that commit before a
        re-dispatch is worth buying, because `tasks.py` reads the payload from the
        LIVE checkout's md. Until now that wait was hand-operated: poll, then edit
        `Worker/assignments/<slug>.json` by hand. `resume.consume` does it from a
        reservation file instead.

        `backdate_s` overshoots the tiered fail cooldown on purpose. dispatch()
        anchors that cooldown on `updated_ts` (:357), so a resume that stamped
        `now` would charge the human's explicit "go" a whole fresh window.
        `_fail_cooldown` returns the BASE at `refail_count=0` (which is what the
        resume sets), but passing the CAP keeps this correct if the tiering is
        ever retuned. `state=FAILED` is in TERMINAL, so reap_stale cannot mistake
        the back-dated timestamp for a stalled run.

        Fail-open like the bridge above: `consume` already swallows per-record and
        sweep-level errors, and this wrapper catches whatever is left (the import,
        `ledger.root`) so a reservation file can never stall dispatch.
        """
        try:
            from ..kakao_format import LEVEL_DIGEST
            from . import resume
            # ledger.root is Worker/assignments -> the vault root is two up.
            vault_root = Path(self.ledger.root).parent.parent
            return resume.consume(
                self.ledger, vault_root,
                backdate_s=max(self.fail_cooldown_s, self.fail_cooldown_base_s) + 60,
                # digest, not immediate: a resume needs no human decision — it is
                # the human's own earlier decision finally taking effect. The
                # per-slug event_key keeps two reasons for the same task separate.
                notify=lambda msg, *, event_key: self._notify_routed(
                    msg, None, level=LEVEL_DIGEST, event_key=event_key),
            )
        except Exception as e:
            print(f"[orchestrator] resume sweep skipped (fail-open): {e}")
            return []

    def reconcile_rescope(self) -> list[str]:
        """Auto-execute parked rescope plans into serial sub-tasks (fail-open).

        A task parked with ALERT_RESCOPE_PENDING carries a machine-readable split
        plan; this runs it — creating the sub-tasks, flipping the parent to an off
        bundle, tidying the parked assignment — so the pipeline keeps moving without
        a human. The info alert is digest (nothing to do right now); the fallback
        ⛔ for a plan that turns out not auto-viable is immediate. Mirrors
        reconcile_resumes' wrapper: vault root from the ledger, notify through the
        routing gate, any failure swallowed so it can never stall dispatch.
        """
        try:
            from ..kakao_format import LEVEL_DIGEST
            from . import rescope
            vault_root = Path(self.ledger.root).parent.parent
            return rescope.execute_rescope(
                self.ledger, vault_root,
                notify=lambda msg, *, event_key, level=LEVEL_DIGEST: self._notify_routed(
                    msg, None, level=level, event_key=event_key),
            )
        except Exception as e:
            print(f"[orchestrator] rescope sweep skipped (fail-open): {e}")
            return []

    def tick(self, tasks: list[Task], now: float | None = None) -> dict:
        reaped = self.reap_stale(now)
        usage_hold = self.usage_hold()
        # stop starting new work; in-flight replies below are still collected
        paused = self.paused()
        budget = self.budget_exceeded()
        hold = budget or paused or bool(usage_hold)
        # `bypass_guard: true` lets one task through a COST hold — quota pacing or
        # the daily run budget — for work that is worth spending out of turn. It
        # deliberately does NOT override an explicit pause: that is a human saying
        # stop, and a task file should not be able to countermand it. The verdict
        # is hold_wait(), which the queue view shares.
        runnable = tasks if not hold else [
            t for t in tasks if hold_wait(t.payload, paused=paused, usage_hold=usage_hold,
                                          budget_exceeded=budget) is None]
        if hold and runnable:
            for t in runnable:
                print(f"[orchestrator] {t.slug}: bypass_guard — dispatching despite "
                      f"hold ({usage_hold or 'daily budget'})")
        # BEFORE dispatch on purpose. This is the only reconcile that can make a
        # row dispatchable *this* tick: it leaves updated_ts at the park time, so
        # the fail cooldown is already spent and the freed task goes out below
        # instead of waiting a whole extra tick. Runs even under a hold — it only
        # edits ledger state, and the hold below still gates the actual dispatch.
        unparked = self.reconcile_unparked()   # human cleared the blocker -> requeue
        # Same reason, same placement: the reservation back-dates `updated_ts` past
        # the fail cooldown, so the freed task goes out in THIS tick's dispatch
        # below instead of waiting another whole window.
        resumed = self.reconcile_resumes()     # `cli resume` condition met -> requeue
        # Same placement as the two sweeps above: run BEFORE dispatch so the parent
        # is tidied and the first sub-task's DB row is written this tick (dispatch
        # picks it up next tick). Fail-open — never raises into the tick.
        rescoped = self.reconcile_rescope()    # parked rescope plan -> auto-split
        dispatched = self._dispatch_serial_per_checkout(runnable)
        updated = self.collect()          # still collect replies from in-flight work
        # AFTER collect (a child closed THIS tick is already in todo.db — mark_done
        # refreshes it synchronously) and BEFORE reconcile_merged, so this
        # gh-INDEPENDENT close transitions the parked row to DONE first and
        # reconcile_merged's state guard then skips it (no double close). Closes a
        # hand-split parent once its children are all done and tidies the parked
        # ledger row; fail-open, never raises into the tick.
        split_parents = self.reconcile_split_parents()
        reconciled = self.reconcile_merged()   # #5: close merged-but-open PR tasks
        closed = self.reconcile_closed(now)     # ledger done but todo open -> close
        unblocked = self.reconcile_unblocked()  # blocker resolved -> stop waiting
        parents = self.reconcile_parents()     # children all done -> tell a human
        requeued = self.reconcile_queue()      # #22: terminal ledger -> queue view
        self._advote_bridge_tick()             # advote feedback → task bridge (fail-open, min-poll+main guarded)
        self._beta_sweep_tick()                # unpublished main → beta catch-up (fail-open, detached)
        project_stalls = self._project_stall_tick()  # stalled project → human-decision task (fail-open, 1h throttle)
        nudged = self.renudge_due(now)
        return {"reaped": reaped, "dispatched": dispatched, "updated": updated,
                "reconciled": reconciled, "closed": closed, "unblocked": unblocked,
                "unparked": unparked,
                "resumed": resumed,
                "rescoped": rescoped,
                "split_parents": split_parents,
                "parents": parents,
                "project_stalls": project_stalls,
                "requeued": requeued, "renudged": nudged,
                "budget_reached": self.budget_exceeded(), "paused": self.paused(),
                "usage_hold": usage_hold}


def _pull_main(root: Path, _run=None, *, attempts: int = 3, _sleep=None) -> None:
    """Best-effort catch-up of local main to origin before a tick, race-hardened.

    The tick never pulled, so the OTHER host's assignments ledger / inbox / Todo
    items aged indefinitely on this one (macp-cloud-hq-migration pilot,
    2026-09-11). Only when HEAD is main: stages check out task branches in this
    same tree, and pulling under them would fight the run — the same reason
    sync_vault_state defers (worker.py).

    Two hosts push this vault, so a single `pull --rebase` kept losing a race:
    while it rebased, this host's own sync daemon committed and moved main, and
    the rebase aborted with a non-content error that the tick then swallowed in
    one truncated log line — leaving local main quietly behind origin (this
    task). So instead of rebasing under a moving HEAD, this fetches and
    fast-forwards (the pure-behind case stays linear), falls back to a real merge
    only for a genuine local-ahead divergence, and retries the whole round a few
    times to ride out a concurrent ref move. Any residual failure (offline, a
    real conflict, no git) is one UNtruncated log line, never fatal to the tick.
    `_run`/`_sleep` are test seams; tests inject fakes so no real pull executes.
    """
    run = _run or subprocess.run
    sleep = _sleep or time.sleep

    def git(*args: str):
        return run(["git", *args], cwd=root, capture_output=True,
                   text=True, timeout=120)

    try:
        head = git("rev-parse", "--abbrev-ref", "HEAD")
        if head.returncode != 0 or head.stdout.strip() != "main":
            return
        detail = ""
        for i in range(attempts):
            fetched = git("fetch", "origin", "main")
            if fetched.returncode != 0:
                detail = (fetched.stderr or fetched.stdout or "").strip()
                if i + 1 < attempts:
                    sleep(1)
                continue
            # Pure-behind (the common case) fast-forwards and stays linear.
            ff = git("merge", "--ff-only", "--autostash", "FETCH_HEAD")
            if ff.returncode == 0:
                return
            # ff-only refused: a genuine local-ahead divergence (make a merge
            # commit — sync pushes it next tick) or the ref moved under us.
            merged = git("merge", "--no-edit", "--autostash", "FETCH_HEAD")
            if merged.returncode == 0:
                return
            # Neither landed this round: never leave the tree mid-merge, wait a
            # beat for the concurrent writer to settle, then try the whole round
            # again. A persistent (non-race) failure falls out of the loop below.
            git("merge", "--abort")
            detail = (merged.stderr or merged.stdout
                      or ff.stderr or ff.stdout or "").strip()
            if i + 1 < attempts:
                sleep(1)
        print(f"hq-host pull: non-fatal — {detail}")
    except Exception as e:
        print(f"hq-host pull: non-fatal — {e}")


def _check_divergence(root: Path, _run=None, *, threshold: int = 3) -> dict:
    """Record how far local main has drifted from origin/main, and for how long.

    A pull that keeps losing the race in `_pull_main` leaves local main quietly
    behind origin — the tick logged one line and moved on, so the drift stayed
    invisible until a human noticed days-stale state. This fetches, counts
    behind/ahead, and tracks a consecutive-behind streak in a host-local state
    file the dashboard renders (ahead/behind are per-host, so the file lives in
    gitignored Worker/_state and is never synced). It alerts once the streak
    crosses `threshold`. Never raises: a divergence probe must not take the tick
    down. `_run` is a test seam; tests inject a fake so no real fetch executes.
    """
    run = _run or subprocess.run
    state_path = root / "Worker" / "_state" / "divergence.json"
    try:
        prior = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        prior = {}

    behind = ahead = 0
    detail = ""
    try:
        run(["git", "fetch", "origin", "main"], cwd=root,
            capture_output=True, text=True, timeout=120)
        counted = run(["git", "rev-list", "--left-right", "--count",
                       "origin/main...HEAD"], cwd=root,
                      capture_output=True, text=True, timeout=120)
        parts = counted.stdout.split()
        if counted.returncode == 0 and len(parts) >= 2:
            behind, ahead = int(parts[0]), int(parts[1])
        else:
            detail = (counted.stderr or counted.stdout or "").strip()
    except Exception as e:
        detail = str(e)

    try:
        prev_streak = int(prior.get("consecutive_behind", 0))
    except (TypeError, ValueError):
        prev_streak = 0
    consecutive = prev_streak + 1 if behind > 0 else 0

    state = {
        "ahead": ahead,
        "behind": behind,
        "consecutive_behind": consecutive,
        "threshold": threshold,
        "alert": consecutive >= threshold,
        "updated": dt.datetime.now().isoformat(timespec="seconds"),
        "last_detail": detail,
    }
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception as e:
        print(f"divergence-check: could not persist — {e}")
    return state


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def resolve_dispatch_limits() -> tuple:
    """(daily_run_budget, max_parallel_per_repo, max_parallel_gradle_builds) —
    the raw profile→env values `_main` builds the dispatching Orchestrator with.
    Unfloored: callers apply max(1, …) / max(0, …) themselves. Shared with the
    dashboard's queue view so it judges with the same limits the tick uses."""
    # per-profile budget + per-repo concurrency cap from ~/.macp (the active
    # profile owns cost governance). The cap defaults to 2 (concurrency on — leaf
    # 5 flipped it from 1 now that worktree isolation + the touches judge landed)
    # and is read via getattr, so a profile that has not added the field yet falls
    # back to the same 2. It is a SUB-constraint of the global usage_guard slot
    # pacing (#191), never a raise of the global ceiling.
    budget = 40
    cap = 2
    gradle_cap = 0
    try:
        from todo_system import macp
        prof = macp.load_profile(macp.default_profile())
        if prof:
            budget = prof.daily_run_budget
            cap = getattr(prof, "max_parallel_per_repo", 2)
            gradle_cap = getattr(prof, "max_parallel_gradle_builds", 0)
    except Exception:
        pass
    # env override — a per-host knob layered over the profile field; wrapped so a
    # malformed value is ignored, never crashing the daemon, and still floored at
    # 1 by max(1, ...) in _main (a host can set it to 1 to force serial-per-repo).
    import os
    env_cap = os.environ.get("MACP_MAX_PARALLEL_PER_REPO")
    if env_cap:
        try:
            cap = int(env_cap)
        except ValueError:
            pass
    # Global gradle-build cap — the cross-checkout memory guard. Same profile→env
    # ladder as the per-repo cap; default 0 = unlimited (dormant), so an older
    # profile without the field and today's behaviour are unaffected. A malformed
    # value is ignored and it is floored at 0 by max(0, ...) in _main (never negative).
    env_gradle = os.environ.get("MACP_MAX_PARALLEL_GRADLE_BUILDS")
    if env_gradle:
        try:
            gradle_cap = int(env_gradle)
        except ValueError:
            pass
    return budget, cap, gradle_cap


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    p = argparse.ArgumentParser(prog="python -m todo_system.orchestrator.orchestrator")
    p.add_argument("--uri", default=f"git://{DEFAULT_ROOT}")
    p.add_argument("--registry", default=None)
    p.add_argument("--ledger", default=DEFAULT_LEDGER)
    p.add_argument("--lease", default=None, help="acquire single-writer lease at path")
    sub = p.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("dispatch", help="Route + send a task to its agent")
    pd.add_argument("--task", required=True)
    pd.add_argument("--payload", default="{}")

    sub.add_parser("collect", help="Process agent replies into the ledger")
    sub.add_parser("renudge", help="Re-nudge parked humans whose backoff elapsed")
    sub.add_parser("status", help="List assignments")
    sub.add_parser("agents", help="Show agent liveness (health)")

    ptr = sub.add_parser(
        "trace",
        help="One task's current truth: latest run, per-artifact provenance "
             "(this run vs inherited), ledger state, branch/PR. Read-only.")
    ptr.add_argument("--task", required=True)

    pa = sub.add_parser("auto", help="Load auto tasks from todo.db and run a full tick")
    pa.add_argument("--db", default="Todo/todo.db")

    pdash = sub.add_parser("dashboard", help="Regenerate Todo/Orchestrator.md")
    pdash.add_argument("--out", default="Todo/Orchestrator.md")
    # Feeds the stale-human-task sweep. Both dashboard writers take it: the tick
    # script calls `dashboard`, a coexistence deployment calls `serve`, and a
    # section that renders in only one of them is a section nobody trusts.
    pdash.add_argument("--db", default="Todo/todo.db")
    # Feeds the decision action-item sweep, and travels with `--db` for the same
    # reason: a section that renders in only one of the two writers is a section
    # nobody trusts. Two paths because the detector takes no default directories.
    pdash.add_argument("--decisions-dir", default="Decisions")
    pdash.add_argument("--items-dir", default="Todo/items")

    ps = sub.add_parser(
        "serve",
        help="Coexistence tick: collect + renudge + dashboard, NO AI dispatch "
             "(safe to run alongside the legacy worker).")
    ps.add_argument("--out", default="Todo/Orchestrator.md")
    ps.add_argument("--db", default="Todo/todo.db")
    ps.add_argument("--decisions-dir", default="Decisions")
    ps.add_argument("--items-dir", default="Todo/items")

    args = p.parse_args(argv)

    # Every path argument above defaults to a VAULT-RELATIVE string
    # (`Worker/assignments`, `Todo/todo.db`, `git://Worker/agents`), which used
    # to mean "relative to the cwd" and so forced this command to run with its
    # cwd at the vault. Resolve them against the vault root instead; the
    # argument *strings* are untouched, so an absolute argument still wins
    # (`base / "/abs"` == `/abs`) and a cwd-at-the-vault invocation — what the
    # systemd/launchd units do — produces byte-identical values.
    base = paths.vault_root_or_cwd()

    # HQ host guard — only the marker-elected host runs the AI-dispatch
    # subcommands (`auto`, `dispatch` — the two that reach dispatch()). Before
    # the lease on purpose: a non-HQ host must not even create the lock file.
    # `serve`/`dashboard` stay allowed on any host; a missing marker changes
    # nothing (fail-open — see hq_host.py). Silent when allowed: this runs
    # every 5-minute tick.
    if args.cmd in ("auto", "dispatch"):
        from . import hq_host
        ok, reason = hq_host.check(base)
        if not ok:
            print(f"hq-host guard: {reason}")
            return 3
        if args.cmd == "auto":
            # The vault repo, not the cwd: these three read the HQ marker and
            # git-pull/diff the vault. From a cwd outside it they inspected
            # (and pulled) whatever repo the process happened to sit in.
            _pull_main(base)
            _check_divergence(base)

    if args.lease:
        lease = Lease(args.lease)
        if not lease.acquire():
            held = lease.current()
            print(f"error: orchestrator lease held by {held.holder if held else '?'}")
            return 1

    budget, cap, gradle_cap = resolve_dispatch_limits()
    # Route the mailbox + ledger through the configured backend at default args:
    # in git mode open_state_store(base).mailbox()/.ledger() are byte-identical to
    # open_mailbox(default uri, base) / Ledger(base/DEFAULT_LEDGER); external mode
    # hands back the SQLite mailbox/ledger. A non-default --uri/--ledger keeps the
    # direct construction (and never builds the external store).
    if args.uri == f"git://{DEFAULT_ROOT}" and args.ledger == DEFAULT_LEDGER:
        store = open_state_store(base)
        mailbox = store.mailbox()
        ledger = store.ledger()
    else:
        mailbox = open_mailbox(args.uri, base=base)
        ledger = Ledger(base / args.ledger)
    orch = Orchestrator(mailbox,
                        load_registry(args.registry, base=base),
                        ledger, daily_run_budget=budget,
                        max_parallel_per_repo=max(1, int(cap)),
                        max_parallel_gradle_builds=max(0, int(gradle_cap)),
                        default_uri=args.uri, mailbox_base=base)

    if args.cmd == "dispatch":
        a = orch.dispatch(Task(args.task, json.loads(args.payload)))
        if a is None:
            print(f"not dispatched (already active or unroutable): {args.task}")
            return 0
        print(f"dispatched {a.task_id} -> {a.agent_id} [{a.state}]")
        return 0

    if args.cmd == "collect":
        for a in orch.collect():
            print(f"{a.task_id} -> {a.agent_id} [{a.state}]")
        return 0

    if args.cmd == "renudge":
        for a in orch.renudge_due():
            print(f"re-nudged {a.task_id} -> {a.agent_id} (nudge #{a.nudge_index})")
        return 0

    if args.cmd == "status":
        for a in orch.ledger.all():
            extra = f" nudge#{a.nudge_index}" if a.state == AWAITING_HUMAN else ""
            print(f"{a.state:14s} {a.task_id:40s} {a.agent_id}{extra}")
        return 0

    if args.cmd == "agents":
        for aid, h in orch.agent_liveness().items():
            age = f"{h['age_s']}s ago" if h["age_s"] is not None else "never"
            print(f"{h['status']:8s} {aid:24s} last-seen: {age}")
        return 0

    if args.cmd == "trace":
        from . import trace
        # deliberately no --out: this command prints and changes nothing, which
        # is what makes it safe to run in the middle of an incident
        print(trace.render(args.task, orch.ledger))
        return 0

    if args.cmd == "auto":
        from . import tasks
        import sqlite3
        db = base / args.db
        try:
            cands = tasks.load_dispatchable(db)
        except sqlite3.OperationalError:
            if db.is_file():
                raise
            # A fresh vault has no todo.db until the first import. Say so and still
            # run the tick (collect / reap / reconcile need no task database).
            print(f"[orchestrator] no task database at {db} — run "
                  "`python -m todo_system.cli import-md` first", file=sys.stderr)
            cands = []
        result = orch.tick(cands)
        budget = f" BUDGET-REACHED (runs today={orch._runs_today()}/{orch.daily_run_budget})" \
            if result.get("budget_reached") else ""
        budget += " PAUSED" if result.get("paused") else ""
        budget += f" USAGE-HOLD ({result['usage_hold']})" if result.get("usage_hold") else ""
        print(f"candidates={len(cands)} dispatched={len(result['dispatched'])} "
              f"updated={len(result['updated'])} renudged={len(result['renudged'])} "
              f"reaped={len(result['reaped'])} "
              f"reconciled={len(result['reconciled'])} "
              f"closed={len(result.get('closed', []))} "
              f"unblocked={len(result.get('unblocked', []))} "
              f"unparked={len(result.get('unparked', []))} "
              f"split_parents={len(result.get('split_parents', []))} "
              f"parents={len(result.get('parents', []))} "
              f"requeued={len(result['requeued'])}{budget}")
        for a in result["dispatched"]:
            print(f"  dispatched {a.task_id} -> {a.agent_id}")
        return 0

    if args.cmd == "dashboard":
        from . import dashboard
        # One resolved value per path, reused in the write and in the printed
        # line, so the output never names a different file than the one written.
        out = base / args.out
        dashboard.write(orch.ledger, orch.reg, out, base / args.db,
                        base / args.decisions_dir, base / args.items_dir)
        print(f"wrote {out}")
        return 0

    if args.cmd == "serve":
        from . import dashboard
        out = base / args.out
        updated = orch.collect()
        nudged = orch.renudge_due()
        dashboard.write(orch.ledger, orch.reg, out, base / args.db,
                        base / args.decisions_dir, base / args.items_dir)
        print(f"serve: collected={len(updated)} renudged={len(nudged)} "
              f"dashboard={out} (coexistence — no AI dispatch)")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
