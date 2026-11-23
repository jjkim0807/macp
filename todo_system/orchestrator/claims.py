"""Crash-durable claims — "the execution of this envelope STARTED".

`Runner.poll_once()` used to ack only *after* `handle()` returned. `finally`
covers an exception; it does not cover SIGKILL, a reboot, or the daemon
restarting the runner. So a pipeline that died mid-flight left its envelope
un-acked, `Mailbox.receive()` (whose only redelivery gate is the cursor's `seen`
set) handed the very same envelope back on the next idle poll, and the worker
re-ran all five stages from scratch — on a task that may already have committed,
pushed and merged a PR.

A claim is the fact that survives the process: written to disk *before*
`handle()` is called, removed only after the envelope is acked. On redelivery
the runner finds the claim and classifies instead of re-running.

Layout mirrors the mailbox's own local state, so it inherits `.gitignore`'s
`Worker/agents/_state/` (never the git-synced `inbox/` transport) and a test's
tempdir root for free:

    <mailbox.root>/_state/<agent_id>/claims/<msg_id>.json

One file per envelope, not one shared `inflight.json`: a single file needs an
atomic rewrite, and one truncated write would take out the defence for *every*
envelope at once — precisely how `cursor.json` fails today (`_load_seen` swallows
`JSONDecodeError` and returns an empty set, redelivering everything). Per-file,
the blast radius of corruption is one envelope.

`read()` returning None means "no claim", so a corrupt claim degrades to today's
behaviour (keep redelivering) rather than to silence.

Stdlib-only, like the rest of the transport: this must run on a bare-python3
remote host with no PyYAML.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import time
from pathlib import Path
from typing import Callable

from . import message as m

# ---------------------------------------------------------------------------
# Verdicts — what a redelivered, already-claimed envelope means
# ---------------------------------------------------------------------------

LIVE = "live"        # the run that claimed it is still executing — leave it alone
ORPHAN = "orphan"    # the claiming process is gone; the run died mid-pipeline
UNKNOWN = "unknown"  # cannot tell (another host, no pid, possible pid reuse)

# A claim older than this with no run_dir recorded is not trusted to be LIVE even
# when its pid answers: pids get recycled. 2x StagedExecutor.stage_timeout (3600s)
# — a single stage cannot legitimately outlive that.
STALE_AFTER_S = 7200.0


def _alive_default(pid: int) -> bool:
    """Deferred import of `jobs._alive` so this module stays importable (and
    cheap) on a host that never runs long jobs."""
    from . import jobs
    return jobs._alive(pid)


def _age_s(started_at: str | None, now: float) -> float | None:
    """Seconds since `started_at`, or None when it cannot be parsed."""
    if not started_at:
        return None
    try:
        return now - dt.datetime.fromisoformat(started_at).timestamp()
    except (TypeError, ValueError):
        return None


def classify(claim: dict, *, now: float | None = None,
             alive: Callable[[int], bool] | None = None,
             stale_after: float = STALE_AFTER_S,
             hostname: str | None = None) -> tuple[str, str]:
    """Decide whether the run that wrote `claim` is still alive.

    Returns `(verdict, evidence)` where evidence is one human-readable line — it
    is written back into the claim and quoted in the report, because "we decided
    this envelope was an orphan" is worthless without the grounds.

    The ladder is deliberately biased toward *not* concluding death: a wrong
    ORPHAN re-dispatches a task whose run is still alive (two runs on one
    branch), while a wrong LIVE only waits for the orchestrator's stale reaper.
    """
    now = time.time() if now is None else now
    alive = _alive_default if alive is None else alive
    host = claim.get("host")
    me = socket.gethostname() if hostname is None else hostname
    pid = claim.get("pid")
    started = claim.get("started_at")
    age = _age_s(started, now)
    age_txt = f"{int(age)}s" if age is not None else "unknown age"

    if host and host != me:
        return UNKNOWN, (f"claimed by pid {pid} on host {host!r}, but this is {me!r} — "
                         f"another host's pid cannot be probed from here ({age_txt})")
    if not isinstance(pid, int):
        return UNKNOWN, f"claim records no pid ({age_txt}) — liveness cannot be established"
    if not alive(pid):
        # Every stage is a `subprocess.run` child of the claiming process, so the
        # parent being gone means no stage survived it either.
        return ORPHAN, (f"pid {pid} on {host or me} is gone (signal-0 probe failed) — "
                        f"its stage subprocesses died with it ({age_txt})")
    if not claim.get("run_dir") and (age is None or age >= stale_after):
        return UNKNOWN, (f"pid {pid} answers, but the claim is {age_txt} old and never "
                         f"recorded a run_dir — the pid may have been recycled")
    # `Worker/runs/<run_id>` mtime is supplementary only: stages write run.log when
    # they *finish*, so up to an hour of staleness is normal and proves nothing
    # about death. Fresh is evidence of life; stale is evidence of nothing.
    return LIVE, (f"pid {pid} on {host or me} is alive (signal-0 probe ok), "
                  f"run_dir={claim.get('run_dir') or '-'} ({age_txt})")


class ClaimStore:
    """Per-agent directory of in-flight envelope claims."""

    def __init__(self, root: str | Path, agent_id: str):
        self.root = Path(root)
        self.agent_id = agent_id

    @property
    def dir(self) -> Path:
        return self.root / "_state" / self.agent_id / "claims"

    def path(self, msg_id: str) -> Path:
        return self.dir / f"{msg_id}.json"

    # -- write ------------------------------------------------------------
    def open(self, msg: m.Envelope, *, pid: int | None = None,
             host: str | None = None, ts: str | None = None) -> dict:
        """Record that this envelope's execution is starting. Raises on I/O
        failure — the caller decides whether a claim it could not write should
        stop the work (it should not) or merely be logged (it should).

        `branch` is written for the start-path guard in `runner`, which compares
        a live sibling's branch against HEAD before restoring main over it."""
        claim = {
            "msg_id": msg.msg_id,
            "task_id": msg.task_id,                     # the todo slug
            "task_path": (msg.payload or {}).get("task_path") or None,
            # ONLY the payload override lands here. The `worker/<slug>` default is
            # `runner.branch_for`'s alone to compute — a second copy of the formula
            # in this module would be one more place for the two to drift apart.
            "branch": (msg.payload or {}).get("branch") or None,
            "corr_id": msg.corr_id,
            "agent_id": self.agent_id,
            "started_at": ts or m.now_iso(),
            "pid": os.getpid() if pid is None else pid,
            "host": socket.gethostname() if host is None else host,
            "run_id": None,                             # filled by attach_run()
            "run_dir": None,
            "redeliveries": [],
        }
        self._write(claim)
        return claim

    def attach_run(self, msg_id: str, run_id: str, run_dir: str | Path) -> None:
        """Bind the executor's run to the claim, so `Worker/runs/<run_id>` can be
        found from the envelope alone. This is what replaces the report stage's
        `slug + task_path + nearest-earlier timestamp` archaeology. Best-effort:
        losing the back-reference must never abort a run that is working."""
        claim = self.read(msg_id)
        if claim is None:
            return
        claim["run_id"] = run_id
        claim["run_dir"] = str(run_dir)
        try:
            self._write(claim)
        except OSError:
            pass

    def note_redelivery(self, msg_id: str, verdict: str, evidence: str,
                        ts: str | None = None) -> None:
        """Append a classified redelivery to the claim's own history."""
        claim = self.read(msg_id)
        if claim is None:
            return
        claim.setdefault("redeliveries", []).append(
            {"ts": ts or m.now_iso(), "verdict": verdict, "evidence": evidence})
        try:
            self._write(claim)
        except OSError:
            pass

    def close(self, msg_id: str) -> None:
        """Drop the claim once the envelope is acked. Called ONLY after a
        successful ack: a claim outliving its ack is merely re-classified on the
        next poll, but an ack outliving its claim would re-open the re-run hole
        this module exists to close."""
        try:
            self.path(msg_id).unlink()
        except OSError:
            pass

    # -- read -------------------------------------------------------------
    def read(self, msg_id: str) -> dict | None:
        """The claim, or None when there is none / it is unreadable. Unreadable
        deliberately reads as absent: the envelope then keeps being redelivered,
        which is today's behaviour, rather than being silently dropped."""
        try:
            data = json.loads(self.path(msg_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def all(self) -> list[dict]:
        """Every readable claim, oldest msg_id first (ULIDs sort by time)."""
        if not self.dir.exists():
            return []
        out: list[dict] = []
        for f in sorted(self.dir.glob("*.json")):
            claim = self.read(f.stem)
            if claim is not None:
                out.append(claim)
        return out

    # -- internal ---------------------------------------------------------
    def _write(self, claim: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path(claim["msg_id"]).write_text(
            json.dumps(claim, ensure_ascii=False, indent=2), encoding="utf-8")
