"""Runner — the always-on process that stands by for a project's agent.

It subscribes to its agent's mailbox and, on TASK_ASSIGN, *wakes the agent*:
sends ACCEPT (or REJECT), runs an Executor, streams PROGRESS, then RESULT/FAIL.
The runner itself is cheap and long-lived; the expensive agent session (Claude
Code) is only spawned per task and torn down after — matching the design's
"cheap standby runner vs. on-demand agent" split.

Execution modes:
  shadow (default): don't spawn anything — log the intended action and return a
    synthetic success. Lets us verify the runner/messaging loop with zero risk,
    e.g. while a human is manually developing the same project.
  live: ClaudeCodeExecutor spawns `claude` in an isolated git worktree.

Registry/YAML is used Mac-side; the transport (message/mailbox) stays
stdlib-only so this same loop can later run on a bare remote host.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from ..claude_auth import claude_env
from ..kakao_format import compose_alert
from .. import paths
from .. import worker as _worker
from . import background
from . import beta_publish
from . import claims as claims_mod
from . import disk_guard
from . import followups
from . import lease as lease_mod
from . import message as m
from . import repos
from . import screen_check
from . import solar_stage
from . import stages
from . import worktree_sweep
from .assignment import DONE, Ledger
from .mailbox import DEFAULT_ROOT, Mailbox, open_mailbox
from .registry import Agent, KIND_AI_LOCAL, load_registry
from .state_store import open_state_store

ProgressFn = Callable[[str], None]
# Called once, as soon as an executor knows which run directory this envelope
# became — the hook that lets a claim point at `Worker/runs/<run_id>`.
RunStartFn = Callable[[str, Path], None]

# How long a code-timestamp answer is reused. The poll loop runs every 15s and
# the answer is a `git log` subprocess, so an unthrottled check would spawn four
# per minute forever; once per minute is plenty to notice a merge. The flag this
# feeds is sticky, so the TTL only ever delays a True, never re-opens one.
_CODE_CHECK_TTL = 60.0


@dataclass
class ExecOutcome:
    ok: bool
    summary: str
    payload: dict = field(default_factory=dict)


class Executor(Protocol):
    mode: str

    def execute(self, msg: m.Envelope, agent: Agent, progress: ProgressFn,
                on_run_start: RunStartFn | None = None) -> ExecOutcome:
        ...


# ---------------------------------------------------------------------------
# Executors
# ---------------------------------------------------------------------------


class ShadowExecutor:
    """No-op executor: logs what it *would* do and returns success. Safe to run
    against a project a human is actively developing."""

    mode = "shadow"

    def execute(self, msg: m.Envelope, agent: Agent, progress: ProgressFn,
                on_run_start: RunStartFn | None = None) -> ExecOutcome:
        branch = ClaudeCodeExecutor.branch_for(msg)
        # best-effort repo resolution (no clone) so the dashboard shows where it
        # WOULD run — the general runner routes many projects to one agent.
        would_repo = repos.resolve(msg.payload, agent.repo, clone=False) \
            or (msg.payload or {}).get("repo") or agent.repo
        progress(f"shadow: would spawn Claude Code for '{msg.task_id}' on {branch} "
                 f"(repo={would_repo})")
        return ExecOutcome(
            ok=True,
            summary=f"shadow run for '{msg.task_id}' (no agent spawned)",
            payload={"mode": "shadow", "branch": branch, "would_repo": str(would_repo)},
        )


class ClaudeCodeExecutor:
    """Live executor: spawn `claude` in an isolated worktree of the agent's repo.

    Unproven until the first live rollout — gated behind `--mode live`. The pure
    parts (branch/argv) are unit-tested; the git-worktree + subprocess path is
    validated during P2 live activation.
    """

    mode = "live"
    clone = False   # a live general runner sets this to fetch external repos

    def __init__(self, allowed_tools: tuple[str, ...] = ("Read", "Write", "Edit", "Bash"),
                 max_turns: int = 40):
        self.allowed_tools = allowed_tools
        self.max_turns = max_turns

    @staticmethod
    def branch_for(msg: m.Envelope) -> str:
        # `worker/` prefix matches the repos' existing CI trigger filters
        # (on: push: branches: worker/**), set up for the legacy worker — an
        # `agent/` prefix silently skips CI, so verify can never see green.
        return (msg.payload or {}).get("branch") or f"worker/{msg.task_id}"

    def resolve_repo(self, msg: m.Envelope, agent: Agent) -> Path | None:
        """Per-task repo (task frontmatter repo URL) beats the agent's fixed
        repo — so one catch-all agent covers every project."""
        return repos.resolve(msg.payload, agent.repo, clone=self.clone)

    def _prompt(self, msg: m.Envelope) -> str:
        p = msg.payload or {}
        return p.get("prompt") or (
            f"You are the agent for task '{msg.task_id}'.\n"
            f"Outcome: {p.get('outcome') or p.get('summary') or '(see task)'}\n"
            f"Do the work, commit, and stop."
        )

    def build_argv(self, msg: m.Envelope) -> list[str]:
        return [
            "claude", "-p", self._prompt(msg),
            "--max-turns", str(self.max_turns),
            "--allowed-tools", *self.allowed_tools,
        ]

    def _run(self, argv: list[str], cwd: Path, msg: m.Envelope, agent: Agent,
             progress: ProgressFn) -> tuple[int, dict]:
        """Run the session and return (returncode, extra_result_payload).
        Overridden by the observable variant to stream instead of capture."""
        r = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1800,
                           # worker.py와 같은 이유로 장수 토큰 주입 (파일 없으면 무변경).
                           env=claude_env())
        return r.returncode, {"stdout_tail": r.stdout[-500:]}

    def execute(self, msg: m.Envelope, agent: Agent, progress: ProgressFn,
                on_run_start: RunStartFn | None = None) -> ExecOutcome:
        repo = self.resolve_repo(msg, agent)
        if repo is None:
            return ExecOutcome(False, f"no repo resolved for '{msg.task_id}' "
                               f"(repo={(msg.payload or {}).get('repo')}, clone={self.clone})", {})
        branch = self.branch_for(msg)
        worktree = repo.parent / f".worktrees/{agent.id}-{msg.task_id}"
        progress(f"{self.mode}: worktree {worktree} on {branch}")
        add = subprocess.run(["git", "-C", str(repo), "worktree", "add", "-B", branch,
                              str(worktree)], capture_output=True, text=True,
                             encoding="utf-8", errors="replace")
        if add.returncode != 0:
            return ExecOutcome(False, f"worktree add failed: {add.stderr.strip()}",
                               {"branch": branch})
        try:
            rc, extra = self._run(self.build_argv(msg), worktree, msg, agent, progress)
            ok = rc == 0
            return ExecOutcome(
                ok=ok,
                summary=("done" if ok else f"claude exited {rc}"),
                payload={"branch": branch, **extra},
            )
        finally:
            subprocess.run(["git", "-C", str(repo), "worktree", "remove",
                            "--force", str(worktree)], capture_output=True)


class BackgroundClaudeExecutor(ClaudeCodeExecutor):
    """live-remote: dispatch a Claude Code *background agent* — it runs
    autonomously and stays observable/controllable from claude.ai/code, the
    mobile app (Code tab), and the terminal (`claude attach|logs|stop <id>`) —
    then watch it to completion.

    Verified invocation: `claude --bg '<prompt>' --worktree <branch>` (see
    background.py). The RESULT carries the watch pointer (session id + how to
    attach). argv/parse are unit-tested; the watched run is validated at live
    activation."""

    mode = "live-remote"
    poll_interval = 15.0

    def execute(self, msg: m.Envelope, agent: Agent, progress: ProgressFn,
                on_run_start: RunStartFn | None = None) -> ExecOutcome:
        repo = self.resolve_repo(msg, agent)
        if repo is None:
            return ExecOutcome(False, f"no repo resolved for '{msg.task_id}' "
                               f"(repo={(msg.payload or {}).get('repo')}, clone={self.clone})", {})
        branch = self.branch_for(msg)
        progress(f"dispatching background agent (worktree {branch}) …")
        sid, proc = background.dispatch(self._prompt(msg), cwd=repo, worktree=branch)
        if not sid:
            detail = (proc.stderr or proc.stdout or "").strip()[:200]
            return ExecOutcome(False, f"background dispatch failed: {detail}",
                               {"branch": branch})
        watch = background.watch_pointer(sid)
        progress(f"background agent {sid} live — watch {watch['watch_url']} | {watch['attach']}")
        while background.session_active(sid):
            time.sleep(self.poll_interval)
        return ExecOutcome(True, f"background agent {sid} completed",
                           {"branch": branch, **watch})


def _stop_build_daemons(repo: Path, progress: ProgressFn) -> None:
    """`stages.cleanup_build_daemons` behind a second try/except.

    The helper is never-raise by contract, but this is called from inside the
    stage loop as well as from `_execute`'s `finally`, so a fault in cleanup must
    never be able to replace a stage's real result (or the `TimeoutExpired` the
    timeout path re-raises). Two belts, one function, three call sites.

    A no-op on any clone without a `gradlew` — every vault task included — so it
    is safe to call unconditionally after every stage.
    """
    try:
        stages.cleanup_build_daemons(repo, progress=progress)
    except Exception as e:      # noqa: BLE001 — cleanup must not mask the outcome
        progress(f"build daemon cleanup skipped ({type(e).__name__})")


def _solar_task_prompt(payload: dict) -> str:
    """The user prompt handed to the Solar harness — the task's summary, outcome
    and next_action, the same fields a claude stage reads from the metadata. No
    secrets, no account data; `solar_stage.run` wraps it with the tool-use
    system prompt."""
    parts = []
    if payload.get("summary"):
        parts.append(f"Task: {payload['summary']}")
    if payload.get("outcome"):
        parts.append(f"Done when: {payload['outcome']}")
    if payload.get("next_action"):
        parts.append(f"Next action: {payload['next_action']}")
    return "\n\n".join(parts) or "Complete the task using the file tools."


class StagedExecutor(ClaudeCodeExecutor):
    """live-staged: run the legacy 5-stage pipeline (research → plan → implement
    → verify → report) via the existing stage prompts, in an isolated worktree,
    and gate success on the verify stage's CI verdict. The faithful port of the
    worker's quality pipeline into v2 — the missing piece for retiring the
    legacy worker (vs. the single-shot Background/ClaudeCode executors). Stage
    prompts do the work; this orchestrates + gates. The pipeline run is
    validated at live activation; pure parts (stages.py) are unit-tested."""

    mode = "live-staged"
    pipeline = stages.STAGES_AUTO   # default; delegate tasks use STAGES_DELEGATE
    stage_timeout = 3600
    clone = True             # external-repo tasks are cloned under ~/Worker
    auto_merge = True         # policy A: report merges the PR on CI green -> the
                              # task's work lands in main and the orchestrator
                              # closes the todo task (close-on-done)

    def _pipeline_for(self, payload: dict) -> tuple[str, ...]:
        """Right-size the pipeline to the task:
        - delegate (research / update-the-task-file): one stage.
        - discover (planner-fed project exploration, discovery.py): one stage.
        - trivial/quick (small, well-defined change): one combined `quick` stage
          instead of the full 5 — cuts cost ~5x for one-liners. Triggered by
          worker_mode 'quick' or a 'trivial'/'quick' tag.
        - otherwise: the full research→plan→implement→verify→report pipeline."""
        p = payload or {}
        wm = (p.get("worker_mode") or "").lower()
        tags = [str(t).lower() for t in (p.get("tags") or [])]
        if wm == "delegate":
            return stages.STAGES_DELEGATE
        if wm == "discover":
            return stages.STAGES_DISCOVER
        if wm == "quick" or "trivial" in tags or "quick" in tags:
            return stages.STAGES_QUICK
        return self.pipeline

    @staticmethod
    def _model_tier_for(payload: dict) -> tuple[str, str]:
        """The task's model tier + the reason (model_select.classify_tier).
        Best-effort: a classification error must not kill a run, it just gets
        the heavy ladder the pipeline always used."""
        try:
            from .model_select import classify_tier
            return classify_tier(payload or {})
        except Exception as e:   # pragma: no cover — defensive
            return stages.DEFAULT_STAGE_TIER, f"classify failed: {e!r}"

    @staticmethod
    def _account_env(msg: m.Envelope, repo: Path | None, *,
                     assigned: str | None = None) -> tuple[dict | None, str | None]:
        """Resolve the Claude Code account this task runs as and build its stage
        env. The account is the ONE current account (`macp.current_account`,
        anchored at the task's repo, else the vault): the project's `.macp` pin
        → the profile's `active_account` → the lowest priority. The env comes
        from `macp.claude_env_for`: CLAUDE_CONFIG_DIR for a non-legacy config dir
        (the legacy single dir, accounts.LEGACY_CONFIG_DIR, is left unset —
        forcing it makes claude look THERE for creds instead of its normal store,
        the macOS keychain → "Not logged in" → every stage fails), plus that
        account's automation-token as CLAUDE_CODE_OAUTH_TOKEN — or that key
        REMOVED when the account has no usable token, so a process-wide token
        never leaks into another account's stages. (None, None) = no account
        configured → the stage inherits the process env, exactly as before.
        `msg` stays in the signature for the callers and their mocks; the
        per-task round-robin it fed (`account_for`) is gone — one active account
        at a time.

        `assigned` (multi-account capacity): the account the runner's
        `poll_parallel` picked for this run to balance fan-out across accounts.
        When None (the default) it is read from the per-thread carrier
        `_ASSIGNED_ACCOUNT` — unset on the serial / direct-call paths, so those
        stay byte-identical. A set `assigned` that is NOT the resolved account
        and is itself available wins over active/override/priority — but NEVER
        over a project pin (a pin is a hard routing; the runner's balance is a
        preference). Byte-identical whenever `assigned` is None, equals the
        resolved account, or the resolution was a pin."""
        if assigned is None:
            assigned = getattr(_ASSIGNED_ACCOUNT, "name", None)
        try:
            from todo_system import macp
            start = str(repo) if repo else str(stages._VAULT_DATA_ROOT)
            acct, source = macp.current_account(start)
            if acct is None:
                return None, None
            if assigned and assigned != acct.name and source != "pin":
                repl = _account_named(start, assigned)
                if repl is not None:
                    acct = repl
            return macp.claude_env_for(acct, os.environ), acct.name
        except Exception:
            pass
        return None, None

    @staticmethod
    def _solar_account(payload: dict, repo: Path | None, task_id: str = ""):
        """The upstage `Account` a quick/delegate stage should route to Solar, or
        None. Returns an account ONLY when the task's payload carries
        `agent: upstage` (repos.task_agent_from_fm wired it from frontmatter) AND
        the resolved profile actually has an upstage account — `config.account_for`
        with `agent="upstage"`, the vendor filter that keeps the claude
        round-robin stable (accounts.py). Anchored at the repo like `_account_env`.

        Best-effort: anything other than a configured upstage task — no `agent`,
        a claude task, no upstage account, or any resolution error — returns None,
        so the `use_solar` branch simply never engages and the claude path is
        byte-identical. Never raises, never reads the key (that is deferred to
        `solar_stage.run`); the key never touches this resolution."""
        if (payload.get("agent") or "").lower() != "upstage":
            return None
        try:
            from todo_system import macp
            start = str(repo) if repo else str(stages._VAULT_DATA_ROOT)
            ctx = macp.resolve(start)
            return macp.account_for(ctx.get("profile"), ctx.get("project"),
                                    task_id, agent="upstage")
        except Exception:
            return None

    @staticmethod
    def _maybe_rotate_account(acct: str | None, repo: Path | None, text: str,
                              progress: ProgressFn) -> dict | None:
        """Leaf 4 of macp-multi-claude-account: on a capacity deferral, classify
        the stage output and — for an exhausted / auth / no_subscription
        signature — rotate the profile's active claude account
        (`macp.accounts.rotate_active`: mark `acct` unavailable, the next
        available account becomes the runtime override, or a PAUSED stamp when
        none is left) and drop the model-choice cache (it is account-agnostic:
        a model dead on the old account must not stay cached as dead for the
        new one). Returns the switch `{from, to|None, reason}` for the
        ExecOutcome details, else None.

        Runs only from inside the deferral branch — the existing gate decides
        THAT a capacity wall was hit; this only decides what to do about it.
        `acct` is the name `_account_env` resolved for this run, the account the
        stage actually ran as, never re-resolved here. Nothing is written and
        None is returned (details byte-identical) when the classification is
        transient / None, no account ran the stage, the profile has fewer than
        two claude accounts (a single account's quota stays the usage guard's
        and the capacity cooldown's business, exactly as before), `acct` is not
        one of them (the test mocks' "acct"), or the account is already marked
        (a sibling run rotated first). Any error is swallowed: the deferral
        must return as it always did."""
        try:
            if not acct:
                return None
            from todo_system import macp
            from todo_system.macp import accounts as ACC
            kind = ACC.classify_capacity(text)
            if kind not in ACC.SWITCH_REASONS:
                return None
            start = str(repo) if repo else str(stages._VAULT_DATA_ROOT)   # _account_env's anchor
            ctx = macp.resolve(start)
            prof, proj = ctx.get("profile"), ctx.get("project")
            cands = [a for a in (getattr(prof, "accounts", None) or [])
                     if getattr(a, "agent", "claude") == "claude"]
            if len(cands) < 2 or acct not in {a.name for a in cands}:
                return None
            until = None
            if kind == ACC.CAPACITY_EXHAUSTED:
                try:
                    from todo_system import usage_guard
                    until = usage_guard.exhausted_until(acct)   # hard-cap window reset, else 5h
                except Exception:
                    until = None
            switch = ACC.rotate_active(acct, kind, until=until, profile=prof, project=proj)
            if switch is None:
                return None
            try:
                from .model_select import invalidate_cache
                invalidate_cache()
            except Exception:
                pass
            progress(f"account: {switch['from']} → {switch['to'] or 'none'} ({kind})")
            return switch
        except Exception:
            return None

    @staticmethod
    def _review_required(payload: dict) -> bool:
        """Middle-risk tier: run the full pipeline + open the PR, but do NOT
        auto-merge — park it for human approval (kakao). Triggered by
        worker_mode 'review' or a 'review-required'/'review' tag."""
        p = payload or {}
        wm = (p.get("worker_mode") or "").lower()
        tags = [str(t).lower() for t in (p.get("tags") or [])]
        return wm == "review" or "review-required" in tags or "review" in tags

    @staticmethod
    def _payload_auto_merge(payload: dict) -> bool:
        """Task's frontmatter `auto_merge`, propagated via the dispatch payload
        (default True — opt out per task with `auto_merge: false`). False = a
        finance/review gate: run the pipeline + open the PR but DON'T merge;
        park it for a human (same end-state as worker_mode:review). Before this
        was wired through, the runner used the class default (True) and emitted a
        misleading 'auto_merge did NOT land (conflict/CI/perm)' note for every
        auto_merge:false task that correctly parked for review."""
        v = (payload or {}).get("auto_merge", True)
        if isinstance(v, str):
            return v.strip().lower() not in ("false", "0", "no")
        return bool(v)

    @classmethod
    def _merge_decision(cls, payload: dict) -> tuple[bool, bool]:
        """Resolve (review_required, auto_merge) for a task payload. Review-
        required when the task asks for it (worker_mode:review / review tag) OR
        opts out of auto-merge (auto_merge:false) — both mean 'PR, no merge'."""
        p = payload or {}
        if (p.get("worker_mode") or "").lower() == "discover":
            # A discovery run's deliverable is an episode report, not a PR: it
            # commits nothing by design, so "auto_merge:false → park for a
            # human to merge" has nothing to park for. Without this the first
            # discover run (dancemirror, 2026-09-11) sat in awaiting_human as
            # "정상 완료 — 머지할 PR 없음, 태스크를 닫아주세요" until a human replied.
            return False, False
        review = cls._review_required(payload) or not cls._payload_auto_merge(payload)
        return review, (cls.auto_merge and not review)

    @staticmethod
    def _long_job_gate(task_id: str, payload: dict, working_dir: str,
                       progress: ProgressFn,
                       done_state: dict | None = None,
                       sync_repo: Path | None = None) -> ExecOutcome | None:
        """Run the task's `long_job` detached; return an outcome while it is
        unfinished, or None to let the pipeline proceed.

        Some work is simply longer than a stage — a decade of candles for 100
        tickers is ~75 minutes against a 60-minute stage cap. Backgrounding it
        from inside a stage does not help: the child dies with the one-shot
        subprocess (ai-stock-trading-8b stopped at 15/100 the instant implement
        exited). So the job is started in its own session and merely observed.

        `done` falls through so the pipeline runs ONCE, against complete data —
        and is cleared on the way out, symmetric with `failed`. Without the
        clear the `.rc` file lingers, `poll` keeps returning `done`, and every
        later dispatch re-promotes the SAME completed data into a full pipeline
        (branch-prep = git reset --hard + git clean -fd), splitting one slug
        into concurrent runs that overwrite each other — the ai-stock-trading-13
        triple-run incident. This is the third path the dispatch dedup guards
        (DONE / not-terminal) cannot catch: the assignment is FAILED+job_running,
        so it is terminal (passes the not-terminal guard) yet never DONE.
        `failed` is a real failure — including a job that vanished without an
        exit code, because a truncated collection reported as success is exactly
        the outcome this gate exists to prevent.

        `done_state`, if given, is filled with the poll state on the way out of
        the done branch — an out-param rather than a richer return type, because
        the None/ExecOutcome contract is what five existing gate tests assert and
        the caller only needs this on the one path that falls through. It carries
        the job's `cwd` and `started_ts`, which the caller needs to identify this
        job's output BEFORE branch prep deletes it, and which `clear()` two lines
        below is about to erase."""
        from . import jobs
        st = jobs.poll(task_id)
        if st["state"] == "absent":
            if sync_repo is not None:
                # The gate runs before branch prep, so nothing else brings the
                # clone up to date: a job whose script landed in the PREVIOUS
                # run's PR started on the pre-merge tree and died "No such file"
                # three times into quarantine (2026-10-05, ai-stock-trading
                # all-pair-sweep-finish vs #159).
                note = stages.ff_default_branch(sync_repo)
                if note:
                    progress(note)
            started = jobs.start(task_id, payload["long_job"], working_dir)
            progress(f"long job started (pid {started['pid']})")
            return ExecOutcome(False, "long job started — deferred until it exits",
                               {"job_running": True, "job_log": str(jobs.log_path(task_id))})
        if st["state"] == "running":
            mins = st.get("elapsed_s", 0) // 60
            progress(f"long job running ({mins}m)")
            return ExecOutcome(False, f"long job running ({mins}m) — deferred",
                               {"job_running": True, "job_log": str(jobs.log_path(task_id))})
        if st["state"] == "failed":
            rc = st.get("rc")
            why = f"rc={rc}" if rc is not None else "killed without an exit code"
            jobs.clear(task_id)          # let a retry start a fresh attempt
            return ExecOutcome(False, f"long job failed ({why}) — see {jobs.log_path(task_id)}",
                               {"job_log": str(jobs.log_path(task_id))})
        progress(f"long job done in {st.get('elapsed_s', 0) // 60}m — running pipeline")
        if done_state is not None:
            done_state.update(st)    # cwd/started_ts, captured before clear() drops them
        jobs.clear(task_id)          # consume once; later polls see `absent`, not `done`
        return None

    @staticmethod
    def _live_sibling_hold(msg: m.Envelope, agent: Agent) -> tuple[str, str] | None:
        """Hold this run back while HEAD belongs to a sibling run that is alive.

        `restore_main` forces past a dirty working tree (`checkout -f`). That is
        right when the leftovers belong to a *finished* run and destructive when
        they do not: the vault is ONE checkout shared by the live daemon and any
        manual `run-next`, so a second run starting here rips HEAD off
        `worker/<other-slug>` and deletes that run's uncommitted edits mid-stage.

        Returns `(reason, held_by_slug)` to defer, or None to proceed.

        Every rung falls open on purpose — no branch, no matching claim, ORPHAN,
        UNKNOWN all proceed exactly as before. Reading "cannot tell" as "someone
        is alive" would wedge the whole queue on one stale file, a worse failure
        than the forced checkout it would avert. Only a positive LIVE verdict
        from `claims.classify` holds, and that ladder is itself biased against
        concluding life. No new liveness notion is invented here.

        The blanket `except` is the same rule taken to its conclusion. This runs
        on the start path of EVERY run, and it is advisory — it protects work, it
        does not produce any. A guard that can itself abort a run is strictly
        worse than no guard, so an unreadable claims dir, a missing git, or an
        agent whose id is not a string all mean "proceed", never "crash".
        """
        try:
            cur = stages.current_branch(stages._VAULT_DATA_ROOT)
            if not cur:
                return None      # detached HEAD or git failed — cannot tell, proceed
            store = claims_mod.ClaimStore(DEFAULT_ROOT, agent.id)
            for c in store.all():
                slug = c.get("task_id")
                # Skip our OWN claim: poll_once writes it before handle() is called,
                # so it is always present and always LIVE (it is this very pid).
                # Excluding it lets a retry/resume of THIS task take its own branch.
                if not slug or slug == msg.task_id:
                    continue
                # The claim records its run's payload `branch` override, so a
                # sibling on a non-`worker/` branch is now caught rather than
                # missed. Claims written before that field existed (and runs that
                # never overrode it) carry None and fall back to the slug —
                # `branch_for`'s two cases, mirrored. A non-str override falls
                # back too. What still fails open: a pre-field claim whose run
                # DID override, which the recomputation cannot match; guessing
                # the branch off HEAD's name instead would stall a healthy queue.
                recorded = c.get("branch")
                ref = recorded if isinstance(recorded, str) and recorded else f"worker/{slug}"
                if ref != cur:
                    continue
                verdict, evidence = claims_mod.classify(c)
                if verdict != claims_mod.LIVE:
                    continue     # ORPHAN/UNKNOWN — keep looking, then fail open
                # Several claims can share a slug (redeliveries); one LIVE is enough.
                return (f"deferred — HEAD is on {cur}, held by a LIVE sibling run "
                        f"{slug!r} ({evidence}); not restoring main over its "
                        f"working tree", slug)
        except Exception:
            return None
        return None

    @staticmethod
    def _acquire_checkout_lease(repo_root: Path):
        """Take the checkout lease guarding the live vault's HEAD-moving branch
        prep against the planner tick's claude call (the SAME `Worker/checkout.lock`
        the planner holds in planner_tick.sh). Vault-path callers only.

        Returns the held `Lease` (release it right after branch prep), `False` when
        another live holder owns it (the caller defers with `checkout_busy`, like
        `_live_sibling_hold`), or `None` on any lease error — fail open and proceed
        unguarded, because a guard that can itself wedge the queue is worse than the
        race it prevents (the codebase rule; cf. `_live_sibling_hold`'s blanket
        except). A leaked lock — should branch prep raise before release — is
        cleared by the lease TTL + same-host dead-PID reclaim, never permanently."""
        try:
            lz = lease_mod.Lease(Path(repo_root) / lease_mod.CHECKOUT_LEASE_PATH,
                                 ttl=lease_mod.CHECKOUT_LEASE_TTL)
            return lz if lz.acquire() else False
        except Exception:      # noqa: BLE001 — advisory guard, fail open
            return None

    def execute(self, msg: m.Envelope, agent: Agent, progress: ProgressFn,
                on_run_start: RunStartFn | None = None) -> ExecOutcome:
        """Thin wrapper over `_execute` whose only job is to make sure a loss
        report survives every way the run can end.

        Branch prep can rewind a prior attempt's unpushed commits, and it detects
        that BEFORE the run directory exists — at which point `_execute` has
        eight different returns, one of them an early bail on a failed checkout.
        Threading the fact through each of them is eight chances to miss one, so
        `_execute` drops it in a sink and this merges it once, on the way out.
        The sink is created per call: a shared one would make one task's loss
        reappear on the next task's run, and an alarm about commits that are not
        missing is worse than no alarm at all."""
        sink: dict = {}
        outcome = self._execute(msg, agent, progress, on_run_start, sink)
        orphaned = sink.get("orphaned_commits")
        if orphaned:
            # Rides the existing structured channel: _send splats payload into the
            # RESULT/FAIL envelope, and the orchestrator notifies from there. No
            # key normally, so a clean run adds nothing anywhere.
            outcome.payload["orphaned_commits"] = orphaned
        # Same channel, same reason: the delegate path opens its PR in `_execute`'s
        # `finally`, after the outcome object already exists. Never overwrite a
        # pr_url the report stage already parsed — that one is the run's own PR.
        if sink.get("pr_url") and not outcome.payload.get("pr_url"):
            outcome.payload["pr_url"] = sink["pr_url"]
        # The delegate `finally` tried a server-side merge and could not confirm
        # it: flip the run to review-required so collect() parks it AWAITING_HUMAN
        # instead of closing done on a still-open PR. This is the ONLY writer that
        # overturns the extra's `review_required=False`, so it is strictly
        # conditional — a clean run (no key in the sink) changes nothing. The note
        # rides as the summary, which apply_reply reads into last_note (the "사유"
        # the dashboard's parked row shows).
        if sink.get("review_required"):
            outcome.payload["review_required"] = True
            if sink.get("review_note"):
                outcome.summary = sink["review_note"]
        return outcome

    def _publish_beta(self, repo, repo_url: str, branch: str, run_dir: Path,
                      payload: dict, task_id: str, progress: ProgressFn) -> str:
        """Publish a Firebase App Distribution beta for a just-merged app-repo PR.

        Called only once the merge is confirmed. Returns a merge_note suffix (the
        published/aborted line, or "" when there is nothing to do). Deliberately
        fail-open: a repo with no beta_publish_config entry is a no-op, and any
        exception is surfaced (progress + kakao) WITHOUT reverting the merge or
        turning the outcome into review — the merged PR stands, retryable.
        """
        try:
            # Config first: an unconfigured repo (most of them) needs no merge sha,
            # and resolving one costs a `gh pr view` that can time out and page a
            # human for a repo that was never going to publish (2026-10-04,
            # ai-stock-trading #150).
            if beta_publish.repo_config(repos.repo_name(repo_url)) is None:
                return ""
            merge_sha = beta_publish.resolve_merge_sha(
                repo, branch, default_branch=stages.default_branch(repo))
            if not merge_sha:
                progress("beta-publish: could not resolve merge sha — skipped")
                return ""
            task_md = stages._VAULT_DATA_ROOT / "Todo" / "items" / f"{task_id}.md"
            # Release notes prefer the task's own outcome (user-language, one line);
            # build_release_notes falls back to the slug if the file is unreadable.
            notes_payload = dict(payload)
            notes_payload["task_id"] = task_id
            try:
                fm, _ = _worker.parse_frontmatter(task_md.read_text(encoding="utf-8"))
                if fm.get("outcome"):
                    notes_payload.setdefault("outcome", fm["outcome"])
            except OSError:
                pass
            note = beta_publish.publish_merge_in_worktree(
                repos.repo_name(repo_url), repo, merge_sha, notes_payload,
                progress=progress, notes_dir=run_dir, task_path=task_md)
            return f" — {note}" if note else ""
        except Exception as e:      # noqa: BLE001 — publish must not flip the merge outcome
            warn = f"beta publish failed for {repo_url}: {type(e).__name__}: {e}"
            progress(warn)          # full detail to run.log
            try:
                # headline carries what + the ask; the English exception rides as a
                # word-clipped tail, so a long stack message is never blind-cut.
                _worker.kakao_notify(compose_alert(
                    f"❌ 베타 발행 실패: {repos.repo_name(repo_url)} — 런 기록 확인이 필요합니다",
                    tail=f"{type(e).__name__}: {e}"))
            except Exception:       # noqa: BLE001 — notification is best-effort
                pass
            return ""

    def _app_screen_check(self, repo, payload: dict, run_dir: Path,
                          progress: ProgressFn) -> None:
        """Opt-in app screen check for a just-verified app-repo task.

        The peer of :meth:`_publish_beta`, run the instant the verify stage wrote
        ``verify.md``. Builds the debug APK, captures the app's first screen on the
        shared emulator node (``scripts/app-test-node``), always stops the node,
        and appends a one-line note to ``verify.md``. Completely fail-open: a task
        with no ``screen_check`` flag is a no-op (unflagged tasks behave exactly as
        before), and NOTHING here — a flag-off, a non-app repo, a node failure, or
        an exception — ever raises or changes the verify verdict (the note is
        appended below the frontmatter ``parse_verify_verdict`` reads).
        """
        try:
            if not screen_check.requested(payload):
                return                               # not opted in → no-op, no note
            if not screen_check.is_app_repo(repo):
                screen_check.append_note(
                    run_dir, "화면 확인 불가(앱 리포 아님 — gradlew 없음)", progress)
                return
            note = screen_check.run_check(
                repo, run_dir,
                cap_s=screen_check.cap_seconds(payload),
                lock_path=stages._VAULT_DATA_ROOT / "Worker" / "screen-test-node.lock",
                node_script=stages._ENGINE_ROOT / "scripts" / "app-test-node",
                gradle_task=screen_check.gradle_task(payload),
                progress=progress)
            screen_check.append_note(run_dir, note, progress)
        except Exception as e:      # noqa: BLE001 — a screen check never flips verify
            progress(f"screen check skipped ({type(e).__name__})")

    def _execute(self, msg: m.Envelope, agent: Agent, progress: ProgressFn,
                 on_run_start: RunStartFn | None = None,
                 sink: dict | None = None) -> ExecOutcome:
        payload = msg.payload or {}
        sink = sink if sink is not None else {}
        repo_url = payload.get("repo")
        review, auto_merge = self._merge_decision(payload)   # honors auto_merge:false gate
        # Execution model mirrors the legacy worker: run claude with cwd=VAULT
        # (the trusted workspace) and reach the code via --add-dir; external
        # repos are cloned under ~/Worker, vault tasks work in the vault. This
        # avoids the untrusted-worktree trust wall that would ignore
        # --allowed-tools and burn the turn budget.
        if repo_url:
            prep_log: list[str] = []
            # Per-RUN worktree, not the shared clone: every run of the same repo
            # gets its own `<shared clone>.wt/<slug>` linked worktree, so two runs
            # can no longer touch each other's branch/files (the shared-checkout
            # collision — worker-shared-workingdir-collision, worker-sibling-run-
            # branch-prep-orphans-your-commits — is now structurally impossible).
            # The shared clone stays the fetch/object-store source underneath. On
            # success the worktree is removed in the `finally`; on failure/park it
            # is kept as evidence and the retry's ensure_worktree reuses it. `repo`
            # IS the worktree from here on, so branch prep, `git clean`, the agent's
            # build/test cwd (working_dir below) and the Verify gates all target it.
            repo = repos.ensure_worktree(repo_url, msg.task_id, clone=self.clone, log=prep_log)
            if repo is None:
                reason = prep_log[-1] if prep_log else ""
                summary = f"could not prepare repo {repo_url}" + (f": {reason}" if reason else "")
                return ExecOutcome(False, summary, {})
            working_dir = str(repo / payload["working_dir"]) if payload.get("working_dir") else str(repo)
        else:
            repo = stages._VAULT_DATA_ROOT
            working_dir = str(repo)
        branch = self.branch_for(msg)
        # A `long_job` gate runs BEFORE anything expensive: no branch prep, no
        # account, no agent. While the job runs this costs one poll per dispatch
        # instead of a pipeline, and the stages only ever see finished data.
        job_snapshot: dict | None = None
        if payload.get("long_job"):
            done_state: dict = {}
            gate = self._long_job_gate(msg.task_id, payload, working_dir, progress,
                                       done_state,
                                       sync_repo=Path(repo) if repo_url else None)
            if gate is not None:
                return gate
            if repo_url and done_state:
                # The gate just fell through, which means the job finished and its
                # output is sitting untracked in the clone — where
                # prepare_external_branch's `git clean -fd` is about to delete it
                # (see stages.snapshot_job_artifacts). Lift it out first. Vault
                # tasks never reach branch prep, so they never reach this either.
                job_snapshot = stages.snapshot_job_artifacts(
                    repo, done_state.get("cwd") or working_dir,
                    done_state.get("started_ts"))
                if job_snapshot["problems"]:
                    # Stop BEFORE branch prep: nothing has been deleted yet, so the
                    # data is still on disk for a human. Proceeding on a snapshot we
                    # could not take is the rc=0-and-no-files bug this fixes.
                    return ExecOutcome(
                        False,
                        "long job output could not be preserved: "
                        + "; ".join(job_snapshot["problems"])[:300],
                        {"branch": branch, "long_job_stash": job_snapshot["dir"]})
                progress(f"long job output staged out of the clone: "
                         f"{len(job_snapshot['files'])} file(s)")
        claude_env, acct = self._account_env(msg, repo)   # multi-account (CLAUDE_CONFIG_DIR)
        if acct:
            progress(f"account: {acct}")
        # Tighten Gradle for the worker stages only (60s daemon idle-timeout +
        # in-process Kotlin compiler) via GRADLE_OPTS in the STAGE env — never
        # ~/.gradle — so a human's manual builds are untouched. Always a dict
        # (copies os.environ when claude_env is None), so the stage still inherits
        # the parent env exactly as `env=None` did before. `stage_env()` (not
        # `stage_env_with_gradle()` directly) so the background-tasks-disabled
        # override (mulkko-39) also applies here. `repo_dir=repo` gives this run a
        # per-run Gradle daemon registry base (AC a: `cleanup_build_daemons`' --stop
        # then reaps only this run's daemons). `repo` is the per-run worktree for an
        # external repo and the vault root otherwise — both set above (:639/:646).
        stage_env = stages.stage_env(claude_env, repo_dir=repo)
        project_ctx = ""                                   # project's .macp/memory
        try:
            from todo_system import macp
            project_ctx = macp.read_memory(repo) if repo else ""
        except Exception:
            pass
        # CRITICAL (incident 2026-07-20): stages run cwd=vault and the agents
        # `git checkout` there, so start from main and ALWAYS restore main in a
        # finally — never leave the live vault (and its daemons) on a task
        # branch. BOTH paths pre-create their branch here — external via
        # prepare_external_branch, vault via prepare_vault_branch — and the vault
        # one is abandoned on restore, its work living on the branch / PR. It used
        # to be that only external repos got one and the vault's was left to a
        # prompt line in worker-implement.md, which silently did not run
        # (2026-08-24, 2/2) and let the stages commit straight onto main.
        # …but never over a sibling run that is still using that working tree.
        # This guard is on the START path ONLY. The `finally` restore below is a
        # different act — it cleans up after OUR run — and stays unconditional,
        # because that is the invariant keeping the live vault off a task branch.
        # Returning here is ahead of the `try:`, so that `finally` does not fire
        # and HEAD is left exactly where the sibling put it. Applied regardless of
        # `repo_url`: an external-repo task still moves the VAULT's HEAD on the
        # very next line.
        hold = self._live_sibling_hold(msg, agent)
        if hold is not None:
            reason, held_by = hold
            progress(reason)
            return ExecOutcome(False, reason,
                               {"branch": branch, "checkout_busy": True,
                                "held_by": held_by})
        # Serialize the live vault's HEAD-moving branch prep against the planner
        # tick's claude call. The planner commits what it writes (CLAUDE.md §10.1);
        # without this, its self-commit can land on `worker/<slug>` after
        # prepare_vault_branch's `checkout -B` moves HEAD there, to be orphaned by
        # the next sibling's prep (observed ed395e1b). The planner holds the SAME
        # `Worker/checkout.lock` across its call (planner_tick.sh), so a held lease
        # here means it (or a sibling's prep) is mid-flight → DEFER without touching
        # HEAD, reusing the `checkout_busy` deferral (no refail, flat cooldown,
        # collect/report keep running). Released right after prepare_vault_branch so
        # the planner waits at most that window, never the whole pipeline. Vault
        # tasks ONLY — an external clone is a different checkout the planner never
        # touches, and restore_main→main below is the safe direction regardless.
        # Fail-open (None) on any lease error, like the sibling hold above; a leaked
        # lock is cleared by the lease TTL + dead-PID reclaim, never permanently.
        checkout_lease = None
        if not repo_url:
            acquired = self._acquire_checkout_lease(stages._VAULT_DATA_ROOT)
            if acquired is False:
                reason = ("deferred — checkout lease held (planner tick or sibling "
                          "vault branch prep); not moving the live vault HEAD")
                progress(reason)
                return ExecOutcome(False, reason,
                                   {"branch": branch, "checkout_busy": True})
            checkout_lease = acquired   # Lease to release after prep, or None (fail-open)
        # Free root-disk space BEFORE anything below writes to it — branch prep's
        # `git fetch`, then every stage's build. The HQ host's root filesystem is
        # shared by ~/Worker and /tmp, and at 160M free (2026-09-19) every Android
        # run died of ENOSPC. Here, after the long-job and sibling-hold defers, a
        # dispatch that only polls pays nothing. The guard cleans idle ~/Worker
        # clones only, never this run's own repo. Silent when the disk is healthy
        # (one statvfs, no subprocess, no progress line); when it acts it says so
        # in one line, and stages.json keeps the record once run_dir exists.
        # Fail-open: the helper never raises, and this except is a second belt,
        # because a guard that could abort the run is worse than no guard.
        disk_record: dict | None = None
        try:
            dg = disk_guard.ensure_free_space(exclude=(repo,), progress=progress)
            if isinstance(dg, dict) and dg.get("triggered"):
                disk_record = dg
        except Exception as e:      # noqa: BLE001 — advisory guard, fail open
            progress(f"disk guard skipped ({type(e).__name__})")
        stages.restore_main(stages._VAULT_DATA_ROOT)
        # Guard the VAULT's own .git too — not just external clones. The external
        # path installs the hook inside prepare_external_branch (on the clone),
        # but a vault task (no repo_url) never enters that branch and would push
        # its worker branch un-guarded. This call targets stages._VAULT_DATA_ROOT's git
        # dir on every run; it's idempotent (write_text overwrite) and harmless on
        # the external path where the clone already has its own guard.
        stages.install_push_guard(stages._VAULT_DATA_ROOT)
        if repo_url:
            # branch from the LATEST origin/default, not the previous task's
            # leftover HEAD (the ~/Worker clone is reused serially) — otherwise
            # branches stack and PRs conflict / can't auto-merge.
            # `progress` so that commits a dead prior attempt left unpushed are
            # named in the run log before this rewind drops them — the recovery
            # (cherry-pick) is a human's, but only if they are told the shas.
            cr = stages.prepare_external_branch(repo, branch, progress=progress)
            # Capture BEFORE the rc check: the rewind (and the loss) already
            # happened, so a failed checkout is the case where the commits are
            # most orphaned and the run has the least to say about it. run_dir
            # does not exist yet, so this has to live in a local for now.
            if getattr(cr, "orphaned_commits", None):
                sink["orphaned_commits"] = cr.orphaned_commits
            if cr.returncode != 0:
                return ExecOutcome(False, f"could not branch {repo} from origin: "
                                   f"{(cr.stderr or cr.stdout or '').strip()[:200]}",
                                   {"branch": branch})
            if job_snapshot is not None:
                # The clean is done; put the job's data back before any stage runs.
                lost = stages.restore_job_artifacts(repo, job_snapshot)
                if lost:
                    return ExecOutcome(
                        False,
                        "long job output was not restored after branch prep: "
                        + "; ".join(lost)[:300]
                        + f" — recover from {job_snapshot['dir']}",
                        {"branch": branch, "long_job_stash": job_snapshot["dir"]})
                progress(f"long job output restored: {len(job_snapshot['files'])} file(s)")
        else:
            # The vault needs its branch made here for the same reason the clone
            # does — left to a stage prompt, it silently did not happen. A
            # SEPARATE function, not the one above with a flag: that one opens
            # with `reset --hard` + `clean -fd`, which is right for a throwaway
            # clone and would delete the human's uncommitted work in here.
            vp = stages.prepare_vault_branch(stages._VAULT_DATA_ROOT, branch,
                                             progress=progress)
            # Branch prep is done — HEAD has stopped moving, so free the checkout
            # lease NOW (before the stages run and before any early return below):
            # the planner waits only this window, not the whole pipeline. The
            # branch pin in worker-planner.md, not this lease, covers a planner
            # that starts while the stages hold HEAD on worker/<slug>.
            if checkout_lease is not None:
                checkout_lease.release()
                checkout_lease = None
            # Capture BEFORE the failure check, same reason as the external side:
            # the rewind already happened, and a failed checkout is exactly when
            # the commits are most orphaned and the run has least to say about it.
            if getattr(vp, "orphaned_commits", None):
                sink["orphaned_commits"] = vp.orphaned_commits
            if vp.problem:
                # Loud, not silent. The bug being closed is a run that carried on
                # with HEAD still on main and let every stage commit there.
                return ExecOutcome(
                    False, f"could not prepare vault branch {branch}: {vp.problem}",
                    {"branch": branch})
        run_id = f"{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}-{msg.task_id}"
        run_dir = stages.RUNS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        if sink.get("orphaned_commits"):
            # Now that the run has a directory, put the shas somewhere a human can
            # still read them after the console output has scrolled away.
            stages.record_orphaned_commits(run_dir, sink["orphaned_commits"])
        if disk_record is not None:
            stages.record_disk_guard(run_dir, disk_record)
        if acct:
            # Structured counterpart to the `account: <name>` progress line above
            # (resolved at :741, recorded now that run_dir exists) — multi-account.
            stages.record_account(run_dir, acct)
        if on_run_start is not None:
            # Tell the claim which run this envelope became, the moment it exists.
            # This is the back-reference that replaces the report stage guessing
            # its own run from slug + task_path + nearest-earlier timestamp.
            on_run_start(run_id, run_dir)
        # Retry fix-forward: feed the prior attempt's verify diagnosis into the
        # creative stages so they fix the exact failure instead of re-implementing
        # blind (which just re-verifies the same red CI). Empty on first attempt.
        retry_ctx = stages.previous_failure_context(msg.task_id, run_id)
        # A prior TIMEOUT is fix-forward too, but of a different shape: the code is
        # already on origin/<branch>, so the creative stages must CONTINUE it, not
        # re-implement blind. Fold both notes in — a run can have a verify FAIL and,
        # a retry later, a timeout — so neither clobbers the other.
        timeout_ctx = stages.previous_timeout_context(msg.task_id, run_id)
        if timeout_ctx:
            retry_ctx = f"{retry_ctx}\n\n{timeout_ctx}" if retry_ctx else timeout_ctx
        # A prior CAPACITY deferral is the same shape as a timeout: checkpoint_push
        # already put the partial commits on origin/<branch>, so the creative stages
        # must CONTINUE them, not re-implement blind. Fold it in like the others.
        capacity_ctx = stages.previous_capacity_context(msg.task_id, run_id)
        if capacity_ctx:
            retry_ctx = f"{retry_ctx}\n\n{capacity_ctx}" if retry_ctx else capacity_ctx
        if retry_ctx:
            progress("retry: prior failure/timeout/capacity context loaded")
        # Resume a crashed run instead of re-deriving it. `implement` is only
        # inheritable once its work is safe on the remote: prepare_external_branch
        # continues origin/<branch>, so a pushed branch means the code is there to
        # verify. Without that proof (never pushed, or a vault task with no branch
        # to check) we re-implement — skipping it would hand verify an empty diff.
        inherited: list[str] = []
        prior = stages.resumable_run(msg.task_id, run_id)
        if prior:
            prior_dir, candidates = prior
            if not (repo_url and stages.remote_branch_exists(repo, branch)):
                candidates = [s for s in candidates if s != "implement"]
            inherited = stages.inherit_artifacts(prior_dir, run_dir, candidates)
            if inherited:
                progress(f"resume: inheriting {'+'.join(inherited)} from {prior_dir.name}")
        # Difficulty/type → model tier, once per run so every stage of the run
        # agrees (a probe result is cached per tier anyway). Logged and written
        # to stages.json: the tier explains a model in run.log that a reader
        # did not expect, and the reason names the field that chose it.
        tier, tier_reason = self._model_tier_for(payload)
        progress(f"model tier: {tier} ({tier_reason})")
        stages.record_model_tier(run_dir, tier, tier_reason)
        # Size the per-stage subprocess timeout to the task tier — a heavy task
        # legitimately needs longer than the historical flat 3600. The class
        # constant stays a FLOOR so nothing ever gets less than before. The
        # stale-reaper (orchestrator._stale_bound_for) and the runner heartbeat
        # idle-gate (worker.RUNNER_HEARTBEAT_STALE_S) are both held clear of this
        # value by the STALE invariant (stages.REAP_MARGIN + the pinned tests).
        stage_timeout = max(self.stage_timeout, stages.stage_timeout_for(payload))
        if stage_timeout != self.stage_timeout:
            progress(f"stage timeout: {stage_timeout}s (tier {tier})")
        # Vendor routing (macp-solar-run-single-stage-task): resolve the upstage
        # account ONCE, before the loop. None for every task without
        # `agent: upstage` AND an upstage account configured — which is every
        # task today — so `use_solar` below is False and the claude argv/env is
        # byte-identical. When set, a quick/delegate stage runs via solar_stage
        # instead of `claude -p` (stages.route_to_solar gates the stage).
        solar_acct = self._solar_account(payload, repo, msg.task_id)
        if solar_acct is not None:
            progress(f"solar account: {getattr(solar_acct, 'name', '?')} "
                     f"(upstage — quick/delegate stages route to Solar)")
        completed: list[str] = []
        # Set True only on the single terminal success below. The `finally` reads
        # it to decide the external worktree's fate: removed on success (its
        # commits are already on origin), kept as evidence on every failure/defer/
        # timeout/park. Declared before the `try` so the `finally` never sees it
        # unbound on an exception that escapes mid-pipeline.
        run_ok = False
        try:
            for stage in self._pipeline_for(payload):
                if stage in inherited:
                    completed.append(stage)
                    stages.record_stage(run_dir, stage)
                    continue
                progress(f"stage {stage} …")
                # Re-touch per stage. This is what justifies a stale cutoff of
                # 90 minutes rather than one sized to a whole 5-stage pipeline:
                # the longest legitimate gap between two touches is one
                # run_stage_subprocess(timeout=self.stage_timeout) below, not the sum.
                _worker.runner_heartbeat_start()
                use_solar = (solar_acct is not None
                             and stages.route_to_solar(payload.get("agent"), stage))
                if use_solar:
                    # Solar (upstage) single-stage path — an in-engine read/write/
                    # finish tool loop, NOT `claude -p`. No argv, no subprocess, no
                    # per-stage token transcript: usage_rec=None keeps the rc==0
                    # capacity arm inert (usage_shows_no_api_work(None) is False),
                    # and the key is read inside solar_stage.run and used only in
                    # the request header. `r` is CompletedProcess-shaped so the
                    # shared tail below reads r.returncode/r.stdout unchanged, and
                    # solar_stage writes the stage artifact (delegate.md) itself.
                    # The deadline doubles as the loop's wall-clock bound; a Solar
                    # run cannot TimeoutExpired, so there is no timeout arm here.
                    r = solar_stage.run(
                        stage, task_prompt=_solar_task_prompt(payload),
                        working_dir=working_dir, account=solar_acct, tier=tier,
                        artifact_path=run_dir / stages.STAGE_ARTIFACT[stage],
                        env=stage_env, deadline_s=stage_timeout, progress=progress)
                    usage_rec = None
                    with (run_dir / "run.log").open("a", encoding="utf-8") as f:
                        f.write(f"\n=== {stage} (rc={r.returncode}) ===\n{r.stdout[-2000:]}\n")
                else:
                    argv = stages.build_stage_argv(
                        stage, task_id=msg.task_id, run_id=run_id, run_dir=run_dir,
                        branch=branch, repo=repo_url, working_dir=working_dir,
                        auto_merge=auto_merge, project_context=project_ctx,
                        allowed_tools=payload.get("allowed_tools"), tier=tier,
                        retry_context=(retry_ctx if stage in ("research", "plan", "implement") else ""))
                    # Token usage → stages.json `usage.<stage>`. argv carries a fresh
                    # --session-id unless MACP_STAGE_USAGE=0, and record_stage_usage
                    # reads that session's transcript back once the process is gone —
                    # ahead of every early return below, so a failed stage still gets
                    # its row. It never raises and changes nothing this loop reads:
                    # r.returncode, r.stdout and r.stderr reach run.log, the model-cache
                    # hook, the capacity check and the artifact check exactly as they
                    # did before (why not --output-format json: the comment above
                    # stages.USAGE_ENV).
                    stage_started = time.monotonic()
                    try:
                        # run_stage_subprocess, not subprocess.run: the stage leads its
                        # own session, so the timeout below reaps the WHOLE tree
                        # (`claude -p` → Bash → `./gradlew` → a 2~8G Gradle/Kotlin JVM).
                        # subprocess.run kills only the direct child and then blocks in a
                        # second, unbounded communicate() on the pipes those grandchildren
                        # still hold — so it left the JVM in the runner cgroup above
                        # `memory.high` AND waited on it (2026-09-25). Capture/text/encoding
                        # are fixed inside the helper; cwd/env/timeout stay keywords so the
                        # returned CompletedProcess and the raised TimeoutExpired are
                        # shape-identical to what this call site read before.
                        r = stages.run_stage_subprocess(
                            argv, cwd=str(stages._VAULT_DATA_ROOT),   # trusted cwd
                            # per-account CLAUDE_CONFIG_DIR + worker Gradle limits; stage_env
                            # is built before run_id exists, so the browser AND emulator
                            # lease owners (= run_id, their own stages only) are layered
                            # on here — each strips its key on every other stage.
                            env=stages.with_emulator_owner(
                                stage,
                                stages.with_browser_owner(stage, stage_env, run_id),
                                run_id),
                            timeout=stage_timeout, progress=progress)
                    except subprocess.TimeoutExpired as e:
                        # The stage cap fired. BEFORE this catch, TimeoutExpired escaped
                        # the whole try/finally: the finally's restore_main walked HEAD
                        # back to main and the NEXT dispatch's branch prep rewound the
                        # stage's unpushed commits — the work was cut AND erased
                        # (mulkko-32, 2026-09-13). Capture it: (1) log the timeout in
                        # run.log's own `=== stage … ===` idiom, (2) checkpoint the
                        # external branch to origin so the commits survive the rewind,
                        # and (3) return timed_out=True so the orchestrator's streak
                        # guard can park a task that keeps timing out and the retry
                        # resumes on origin/<branch> rather than restarting from scratch.
                        stages.record_stage_usage(run_dir, stage, argv, timed_out=True)
                        partial = getattr(e, "stdout", None) or ""
                        if isinstance(partial, bytes):
                            partial = partial.decode("utf-8", "replace")
                        with (run_dir / "run.log").open("a", encoding="utf-8") as f:
                            f.write(f"\n=== {stage} (timeout after {stage_timeout}s) ===\n"
                                    f"{partial[-2000:]}\n")
                        # Stop this clone's Gradle daemons BEFORE the push below. The
                        # group kill above cannot reach a daemon that put itself in its
                        # own session, and a 2~8G JVM left running holds the runner
                        # cgroup over `memory.high` — where the very next git call sits
                        # in `__mem_cgroup_handle_over_high` for tens of minutes
                        # (2026-09-25; the git half of that incident is the `_run_git`
                        # ceilings in stages.py). So: reap, then checkpoint.
                        _stop_build_daemons(repo, progress)
                        cp: dict = {}
                        # `report` runs only AFTER a squash-merge deleted origin/<branch>,
                        # so it holds no unpushed worker commits; a checkpoint there would
                        # base off origin/<default> (remote_branch_exists False), count the
                        # already-merged commits as ahead, and re-push the merged branch as
                        # a duplicate-PR shape (defect B, 2026-09-20). Skip it — implement
                        # (:844) and quick still checkpoint, plan/research/verify are no-ops.
                        if repo_url and repo is not None and stage != "report":
                            cp = stages.checkpoint_push(repo, branch)
                            if cp.get("note"):
                                progress(f"timeout checkpoint: {cp['note']}")
                        stages.record_timeout(run_dir, stage, cp.get("tip"))
                        note = cp.get("note") if cp else "vault task — no external branch pushed"
                        return ExecOutcome(
                            False,
                            f"stage {stage}: timed out after {stage_timeout}s — {note}",
                            {"branch": branch, "run_dir": str(run_dir),
                             "stages": completed, "timed_out": True,
                             "timeout_stage": stage, "checkpoint_sha": cp.get("tip"),
                             "repo": repo_url})
                    usage_rec = stages.record_stage_usage(
                        run_dir, stage, argv, env=stage_env, returncode=r.returncode,
                        duration_ms=int((time.monotonic() - stage_started) * 1000))
                    with (run_dir / "run.log").open("a", encoding="utf-8") as f:
                        f.write(f"\n=== {stage} (rc={r.returncode}) ===\n{r.stdout[-2000:]}\n")
                # Per stage, not once per run: a stage that finished cleanly can
                # still have left a Gradle daemon holding gigabytes, and the next
                # stage (or the implement checkpoint push below) is what pays for it.
                # The cold-daemon cost is small — `WORKER_GRADLE_OPTS` already sets a
                # 60s idle timeout, so a daemon rarely survives the gap between two
                # stages anyway — and a frozen runner costs incomparably more.
                _stop_build_daemons(repo, progress)
                # invalidate runs ahead of (and rc-blind of) the capacity gate: a
                # dead *model* is worth re-probing whatever the exit code, and it
                # reads stderr too (run.log keeps only stdout, but _probe sees both).
                stages.invalidate_model_cache_if_unavailable(
                    f"{r.stdout or ''}\n{r.stderr or ''}")
                # Capacity deferral catches both exit-code shapes of a usage/rate
                # limit or lost auth:
                #   • rc != 0 + a capacity marker — the ordinary failed-call case.
                #   • rc == 0 + a capacity marker + a usage record proving ZERO API
                #     work — the 2026-07-31 blind spot: the CLI printed the
                #     out-of-credits notice yet exited 0, so an rc-only gate let the
                #     refused, empty implement fall through to the artifact check
                #     (run.log the runner just wrote above) and be promoted to
                #     `completed`, and verify then judged partial work.
                # The rc=0 discriminator is the usage record, NOT a wider stdout
                # grep: a refused call logs no API message (only the CLI's
                # <synthetic> notice), so usage_shows_no_api_work is True; a green
                # stage that merely quotes a marker (e.g. this very task's edits to
                # stages._CAPACITY_MARKERS) did real API work → real usage dict →
                # False, so it is never mis-deferred. With MACP_STAGE_USAGE off
                # usage_rec is None and the rc=0 arm is inert (default is on).
                # is_capacity_limit is ANDed LAST so the common green stage (rc 0,
                # real usage) short-circuits on the cheap rc/usage check before the
                # stdout scan — and so the rc != 0 arm stays byte-for-byte (usage is
                # consulted only in the rc == 0 or-branch, never for a failed call).
                if ((r.returncode != 0 or stages.usage_shows_no_api_work(usage_rec))
                        and stages.is_capacity_limit(r.stdout)):
                    # usage/rate limit or lost auth — a transient capacity signal,
                    # not a task defect: defer WITHOUT a refail so a healthy task
                    # isn't poison-quarantined for being unlucky on capacity.
                    # Checkpoint the partial commits to origin FIRST (external repo
                    # only), the same FF-only, never-raise push the timeout path uses
                    # (:784-789): without it, a stage that committed real work before
                    # the limit hit has that work rewound into orphans by the next
                    # dispatch's prepare_external_branch, and the retry re-implements
                    # blind instead of continuing origin/<branch>.
                    cp: dict = {}
                    # Skip the checkpoint in `report` for the same reason the timeout
                    # path does (:791): post-squash-merge it would re-push the merged
                    # branch as a duplicate-PR shape (defect B).
                    if repo_url and repo is not None and stage != "report":
                        cp = stages.checkpoint_push(repo, branch)
                        if cp.get("note"):
                            progress(f"capacity checkpoint: {cp['note']}")
                    stages.record_capacity(run_dir, stage, cp.get("tip"))
                    details = {"branch": branch, "run_dir": str(run_dir),
                               "stages": completed, "capacity_limited": True,
                               "checkpoint_sha": cp.get("tip"), "repo": repo_url}
                    # Leaf 4: rotate the account on an exhausted / auth / no-subscription
                    # signature — only here, inside the deferral the gate above decided,
                    # on stdout+stderr (the gate itself keeps reading stdout alone), and
                    # only as an ADDED key. `acct` is the account this stage ran as.
                    switch = self._maybe_rotate_account(
                        acct, repo, f"{r.stdout or ''}\n{r.stderr or ''}", progress)
                    if switch:
                        details["account_switch"] = switch
                    return ExecOutcome(False, f"stage {stage}: usage/rate limit — deferred (no penalty)",
                                       details)
                artifact = run_dir / stages.STAGE_ARTIFACT[stage]
                if not artifact.exists():
                    return ExecOutcome(False, f"stage {stage}: missing {artifact.name}",
                                       {"branch": branch, "run_dir": str(run_dir),
                                        "stages": completed})
                completed.append(stage)
                stages.record_stage(run_dir, stage)   # so a retry can inherit it
                if stage == "implement" and repo_url and repo is not None:
                    # Checkpoint the worker branch to origin the instant implement
                    # is recorded done — BEFORE verify runs. resumable_run inherits
                    # implement ONLY when origin/<branch> exists (:701-711), so if
                    # verify then dies (max-turns, subprocess timeout, missing
                    # verify.md) the retry CONTINUES these commits via
                    # prepare_external_branch instead of rewinding them into
                    # orphans. Same FF-only, never-raise checkpoint the timeout
                    # path uses (:786), called WITHOUT `progress` to avoid a double
                    # log line; the try is a second belt (disk guard :612
                    # precedent) so a push hiccup never sinks an otherwise-good run.
                    # Inherited implements never reach here — they `continue` at
                    # :734 — so a resumed run does not re-push.
                    try:
                        cp = stages.checkpoint_push(repo, branch)
                        if cp.get("note"):
                            progress(f"implement checkpoint: {cp['note']}")
                    except Exception as e:      # noqa: BLE001 — best-effort, fail open
                        progress(f"implement checkpoint skipped ({type(e).__name__})")
                if stage == "verify" and repo_url and repo is not None:
                    # Opt-in app screen check (external app repos only): the verify
                    # stage has just written verify.md — capture the app's first
                    # screen on the emulator node and append a note. The repo_url
                    # gate skips vault tasks entirely. Fail-open and placed BEFORE
                    # the verdict read below: the note lands beneath the frontmatter
                    # and never contains overall_status:, so parse_verify_verdict's
                    # first-match-only read can't be flipped by it.
                    self._app_screen_check(repo, payload, run_dir, progress)
                if stage in ("verify", "quick", "discover"):
                    # all declare overall_status in frontmatter; the quick stage
                    # self-verifies + self-merges, so honor its verdict too, and
                    # a discover run that could not clone/run is a FAIL, not done.
                    text = artifact.read_text(encoding="utf-8")
                    verdict = stages.parse_verify_verdict(text)
                    progress(f"{stage} verdict: {verdict}")
                    if verdict == "FAIL":
                        # A FAIL that names an external blocker is not a defect to
                        # fix-forward — retrying just re-buys the pipeline against
                        # the same unmet precondition. Surface it instead.
                        blocker = stages.parse_blocked_on(text)
                        if blocker:
                            progress(f"blocked on {blocker}")
                            return ExecOutcome(False, f"blocked on {blocker} — not a defect",
                                               {"branch": branch, "run_dir": str(run_dir),
                                                "verify": "FAIL", "stages": completed,
                                                "blocked_on": blocker})
                        return ExecOutcome(False, f"{stage} FAIL — not merged",
                                           {"branch": branch, "run_dir": str(run_dir),
                                            "verify": "FAIL", "stages": completed})
            merge_note = ""
            # #4 (vault half — deliberately NOT gated here, by design): vault
            # tasks (no repo_url) skip the landed-check on purpose. Their merge
            # semantics are unlike a PR — work can land as direct commits to main
            # with no PR for `gh pr view` to find, and the artifact regex is no
            # more trustworthy — so a gate here would false-negative and re-park
            # every vault task. Rather than close on no evidence (an old bug),
            # vault tasks close via the normal RESULT path; reconcile_merged() in
            # the orchestrator backstops any that DID open a PR that later merged.
            # Only repo-backed tasks get the landed gate below.
            if auto_merge and repo_url:
                # auto_merge was expected — verify it actually landed. Ask GitHub
                # first (gh pr view on the branch = ground truth); fall back to the
                # LAST stage's artifact regex only when gh can't answer. If it
                # didn't land, DON'T silently mark done: surface as review-required.
                last_stage = completed[-1] if completed else "report"
                merged = stages.merge_landed(run_dir, last_stage,
                                             branch=branch, repo=repo_url)
                if merged is not True:
                    review = True
                    # Three causes, one park — but they need different notes.
                    # (1) A LATER concurrent same-repo PR that could not rebase
                    # cleanly onto a moved origin/main: the report stage aborted
                    # the rebase (NEVER auto-resolved) and stamped the marker, so
                    # the overlap judge is what to reconcile — the fixed reason
                    # string is owned HERE, not by the agent's prose. Checked
                    # first so a rebase conflict is never mislabelled as a plain
                    # merge failure. (2) A deliberate hold (agent recommended
                    # `review` because the task's own guardrails outrank the
                    # injected auto_merge) is the pipeline working. (3) Anything
                    # else is a real 'conflict/CI/perm' merge failure.
                    if stages.rebase_conflict_park(run_dir, last_stage):
                        merge_note = (" — 동시 실행 충돌 — 겹침 오판 "
                                      "(rebase 충돌; 자동 해결 안 함, 사람 검토)")
                    elif stages.review_hold(run_dir, last_stage):
                        merge_note = " — merge withheld for human review (agent's call, not a failure)"
                    else:
                        merge_note = " — auto_merge did NOT land (conflict/red check/perm); needs review"
                else:
                    # Merge confirmed — the deterministic, non-LLM "it landed" event
                    # an app-repo Firebase beta follows. Regression-safe: a no-op
                    # (empty note) for any repo without a beta_publish_config entry,
                    # so a non-app merge behaves exactly as before. A publish failure
                    # never reverts the merge or flips the outcome to review.
                    merge_note = self._publish_beta(
                        repo, repo_url, branch, run_dir, payload, msg.task_id, progress)
            # The report's own verdict was never read — only verify's. So a report
            # that said `overall_status: fail` still produced "staged pipeline
            # complete", and the human was pinged to merge a PR the same report
            # recorded as `pr_url: null` (gutenberg-reader-3, 2026-08-07). The
            # last stage's judgement of its own run is evidence; use it.
            report_art = run_dir / stages.STAGE_ARTIFACT["report"]
            report_txt = report_art.read_text(encoding="utf-8", errors="replace") \
                if report_art.exists() else ""
            # Durably record any follow-ups the report named (## 다음 단계 /
            # ## 사람이 검토해야 할 부분) so the planner can pick them up on a LATER
            # tick even when it misses the run-completed scan window — the exact
            # path that silently dropped 3 follow-ups in the week to 2026-08-26.
            # Guarded on non-empty report_txt so quick/delegate (no report.md)
            # naturally no-op; best-effort so a logging hiccup never fails a
            # pipeline that otherwise completed.
            if report_txt:
                try:
                    followups.append_followups(
                        report_txt, source_task=msg.task_id, source_run=run_id)
                except Exception:
                    pass
            pr_url = stages.parse_report_pr_url(report_txt)
            extra = {"branch": branch, "run_dir": str(run_dir), "stages": completed,
                     "review_required": review, "repo": repo_url}
            if acct:
                extra["account"] = acct     # the claude account this run spent on
            if pr_url:
                extra["pr_url"] = pr_url
            if stages.parse_verify_verdict(report_txt) == "FAIL":
                return ExecOutcome(False, f"report declared the run failed{merge_note}",
                                   extra)
            run_ok = True   # the one terminal success — gates the worktree removal below
            return ExecOutcome(True, f"staged pipeline complete ({len(completed)} stages){merge_note}",
                               extra)
        finally:
            # A delegate run's output lives ONLY on `worker/<slug>`: it is a single
            # stage with no report stage behind it to push, and the restore below
            # then walks HEAD back to main and abandons the branch. Three runs'
            # work was lost that way. worker-delegate.md's ":43 Never push." rule
            # is about the AGENT and stays true — this is the runner pushing.
            #
            # In the `finally` rather than beside the success `return`, because the
            # decisive case is the 60-minute stage timeout: `TimeoutExpired`
            # escapes the `try` entirely, and that is exactly when a stage has
            # committed and has no way to say so. A failed run's branch becoming a
            # PR someone closes is the cheaper mistake — nothing auto-merges it.
            #
            # NOTHING in this block may `return` or `raise`: both would swallow the
            # original exception (or override the real outcome) on the way out.
            # Gated on delegate + vault deliberately. `auto`/`quick` have their own
            # merge semantics (report stage, landed gate), and an external delegate
            # task's branch lives in the ~/Worker clone, not here.
            if not repo_url and (payload.get("worker_mode") or "").lower() == "delegate":
                try:
                    # auto_merge:true delegate is merged like every other lane (the
                    # Report stage's server-side squash); auto_merge:false opens the
                    # PR and parks (review=True from _merge_decision). Before this,
                    # the delegate `finally` ignored auto_merge entirely: it ALWAYS
                    # opened a 'Not auto-merged' PR and left `review` False, so an
                    # auto_merge:true delegate closed `done` without a merge and
                    # auto-unblocked its downstream (the 2026-10-12 incident).
                    want_merge = self._payload_auto_merge(payload)
                    body = (f"Delegate run for `{msg.task_id}`.\n\n"
                            "Opened by the worker so the run's commits are not "
                            "stranded on a local branch. ") + (
                        "Auto-merge was attempted (task `auto_merge: true`); if it "
                        "did not land (conflict/perm), the task parks for a human."
                        if want_merge else
                        "**Not auto-merged** — a human reviews and merges.")
                    pub = stages.publish_branch(
                        stages._VAULT_DATA_ROOT, branch, body=body, progress=progress)
                    if pub.get("note"):
                        progress(f"publish: {pub['note']}")
                    if pub.get("pr_url"):
                        # Out-of-band like `orphaned_commits`: the ExecOutcome was
                        # built before this `finally` ran, and the early returns
                        # each construct their own dict, so there is no one object
                        # to attach it to. `execute` merges the sink on the way out.
                        sink["pr_url"] = pub["pr_url"]
                    if want_merge and pub.get("pr_url"):
                        mp = stages.merge_pr(stages._VAULT_DATA_ROOT, branch,
                                             progress=progress)
                        if mp.get("note"):
                            progress(f"merge: {mp['note']}")
                        if not mp.get("merged"):
                            # Not confirmed merged (md-only conflict, perm, no gh).
                            # Park — closing `done` on a still-open PR is the very
                            # bug this fixes. Same out-of-band sink channel; this
                            # block may not return/raise. `execute` flips the
                            # outcome's review_required (built as False upstream).
                            sink["review_required"] = True
                            sink["review_note"] = (
                                f"delegate PR 미머지 — 검토 대기: "
                                f"{pub.get('pr_url') or branch} "
                                f"({mp.get('note') or 'merge not confirmed'})")[:200]
                except Exception as e:      # noqa: BLE001 — courtesy step, fail open
                    progress(f"publish: skipped ({type(e).__name__})")
            stages.restore_main(stages._VAULT_DATA_ROOT)  # vault back to main, always
            # The external repo is no longer a shared clone parked on the task
            # branch, so there is nothing to `restore_main` here anymore — it is a
            # per-RUN worktree (repos.ensure_worktree) and the shared clone under
            # ~/Worker is never put on a branch. The trap this block used to defend
            # against (a merged branch left checked out showing a tree origin/main
            # does not have — bean-cards-2, 2026-08-05) therefore cannot arise. The
            # worktree's own fate is decided by the removal at the very end.
            # Stop this run's build daemons so Gradle/Kotlin daemons do not
            # accumulate in the runner cgroup run-over-run and OOM the host (the
            # runner + its in-flight run died together, 2026-09-14). The stage loop
            # now does this after every stage too, but this call stays: it is the
            # ONLY one on the paths that never reach the loop — a failed branch
            # prep, a held checkout lease, a long_job deferral — and on any exit
            # that leaves the `try` by a route the loop's own call did not cover.
            # No-ops on a vault clone (no gradlew).
            if repo is not None:
                _stop_build_daemons(repo, progress)
            # LAST act: a SUCCEEDED external run's worktree is pure residue — its
            # commits are already on origin (report push / stage checkpoint) — so
            # reap it, AFTER the daemon-stop above so nothing is still building
            # inside it. Any non-success (verify-FAIL, timeout, capacity defer,
            # checkout-busy, park, or an exception escaping the `try`) leaves
            # run_ok False and the worktree standing as evidence; the retry's
            # ensure_worktree reuses it idempotently. Its gradle-registry dir
            # (stages.gradle_registry_base) goes with it; if the worktree removal
            # failed, both stay and worktree_sweep's doctor pass reclaims them
            # after the retention window. Both helpers are best-effort and never
            # raise, so this is safe as the final statement of this `finally`.
            if repo_url and repo is not None and run_ok:
                if stages.remove_worktree(repos.local_path(repo_url), repo, progress=progress):
                    worktree_sweep.remove_registry(Path(repo).name, progress=progress)


def make_executor(mode: str) -> Executor:
    if mode == "shadow":
        return ShadowExecutor()
    if mode == "live":
        return ClaudeCodeExecutor()
    if mode == "live-remote":
        return BackgroundClaudeExecutor()
    if mode == "live-staged":
        return StagedExecutor()
    raise ValueError(f"unknown executor mode: {mode!r}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _checkout_key(payload: dict | None, slug: str | None = None) -> str:
    """Checkout/busy key for the runner's parallel poll. External repos key on
    the *per-run worktree* (slug): same-repo runs live in distinct
    `<repo>.wt/<slug>` worktrees (repos.ensure_worktree) so they share no
    checkout and may run together. The vault is one live checkout, so every
    vault run keys on "<vault>" regardless of slug and stays serial. Equal keys
    must never run together.

    This is deliberately NOT the same as Orchestrator._checkout_key, which stays
    repo-URL keyed because its key is the grouping basis for the cap
    (`max_parallel_per_repo`) and touches sets. This is the runner-side half,
    below the dispatch cap, where per-run isolation is what matters.

    The key is only ever compared with other keys inside one runner's `_inflight`
    table — never used as a real filesystem path — so `worktree_path`'s default
    worker_root is harmless: every call shares it, so (repo, slug) -> one key and
    distinct slugs -> distinct keys hold exactly. An empty slug collapses
    same-repo runs onto one key: a safe (over-serial) fallback, not a collision."""
    repo = str((payload or {}).get("repo") or "")
    if not repo:
        return "<vault>"
    return str(repos.worktree_path(repo, slug or ""))


def _mem_available_mb() -> float | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        return None
    return None


# Carries the account `poll_parallel` assigned a run to, across the thread
# boundary into `_account_env` (which runs deep inside `_execute` on that
# thread). A thread-local, so one run's assignment never leaks into another's;
# unset (serial poll_once, direct test calls, no account model) reads as None →
# `_account_env` takes its byte-identical pre-assignment path.
_ASSIGNED_ACCOUNT = threading.local()


def _account_named(start: str, name: str):
    """The profile's claude account named `name` and currently available at
    `start`, or None — the object `_account_env` swaps in when the runner
    assigned this run to a specific account. Never raises."""
    try:
        from todo_system.macp import accounts as _acc
        from todo_system.macp import config as _cfg
        ctx = _cfg.resolve(start)
        cands = [a for a in (getattr(ctx.get("profile"), "accounts", None) or [])
                 if getattr(a, "agent", "claude") == "claude"]
        avail = _acc.available_accounts(cands, _acc.load_runtime_state())
        return next((a for a in avail if getattr(a, "name", "") == name), None)
    except Exception:
        return None


_FALSEY = frozenset({"false", "0", "no", "off", "none", ""})


def _truthy(v) -> bool:
    """A frontmatter flag (same reading as rescope's): YAML bools as-is, strings
    by value, absent/None false."""
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() not in _FALSEY


class Runner:
    # Class-level defaults that make the drain guard INERT rather than absent.
    # A Runner assembled without __init__ (tests build them with __new__ and set
    # only the fields they exercise) would otherwise raise AttributeError from
    # inside poll_once — and a guard that can introduce a new crash into the poll
    # path is not fail-open, whatever its except clause says. Primed + no baseline
    # is the "never drain, never look anything up" state; __init__ arms it.
    _drain_requested = False
    _code_primed = True
    _code_baseline: float | None = None
    _code_checked_at: float | None = None
    ledger_root: Path | None = None

    def __init__(self, mailbox: Mailbox, agent: Agent, executor: Executor,
                 log: Callable[[str], None] = print,
                 claims: claims_mod.ClaimStore | None = None,
                 code_time_fn: Callable[[], float | None] | None = None,
                 ledger_root: Path | None = None):
        self.mailbox = mailbox
        self.agent = agent
        self.executor = executor
        self.log = log
        # None = beside the mailbox root (<vault>/Worker/assignments); see _skip_reason.
        self.ledger_root = ledger_root
        # Derived from the mailbox root, so the claims land beside the cursor in
        # the gitignored `_state/` tree and a test's tempdir mailbox carries its
        # own claims automatically. The Mailbox contract itself is untouched.
        self.claims = claims or claims_mod.ClaimStore(mailbox.root, agent.id)
        # Parallel mode (run_forever with max_parallel > 1) runs envelopes on
        # threads. The lock serializes what they share: the mailbox cursor
        # (ack is a read-modify-write of one JSON file), claim files and the
        # in-flight table. It is never held across handle() itself.
        self._lock = threading.RLock()
        self._inflight: dict[str, str] = {}   # msg_id -> checkout key
        self._inflight_acct: dict[str, str | None] = {}  # msg_id -> assigned claude account
        self._busy = 0                        # runs holding the heartbeat
        self._mem_hold_logged = False
        # Self-drain on code change (see _code_changed). `code_time_fn` is NOT
        # called here: tests build Runners by the dozen and __init__ must stay free
        # of subprocesses. The baseline is primed on entry to run_forever, else
        # lazily on the first check. This is where the class defaults above get armed.
        self._code_time_fn = code_time_fn or _worker.code_change_time
        self._code_baseline = None
        self._code_primed = False
        self._code_checked_at = None   # None = never asked

    def _prime_code_baseline(self) -> None:
        """Remember which commit this process is running, once.

        A None baseline is a permanent, deliberate opt-out: every later comparison
        is False, so a host where the timestamp cannot be read keeps the old
        back-to-back behaviour instead of draining blindly. `_code_primed` is set
        either way so the failing lookup is not retried on every poll."""
        if self._code_primed:
            return
        self._code_primed = True
        try:
            self._code_baseline = self._code_time_fn()
        except Exception as e:  # noqa: BLE001 - fail open, see _code_changed
            self.log(f"drain: code baseline unavailable (continuing): {e}")
            self._code_baseline = None

    def _code_changed(self) -> bool:
        """Has the code moved past the commit this process booted on?

        A resident runner keeps whatever source was on disk when it started, and
        `worker.restart_stale_daemons` only kickstarts it when it looks idle — which
        a full queue never allows. So the runner answers the same question itself
        and stops taking new work, letting systemd's `Restart=always` bring the new
        code up. The judgment comes from `worker.code_change_time()`, i.e. from
        scripts/health_report.py; this is not a second opinion.

        Sticky: once True it stays True without further lookups, because the loop's
        only remaining job is to finish what is in flight and exit.

        Never raises and never drains on doubt. A None from either side (no git,
        health_report will not load, unreadable HEAD) is read as "no change" — a
        missed restart costs one stale cycle, whereas a guard that throws in the
        poll path takes down the process that holds the queue.

        The comparison is strict `>` against a baseline re-read at every boot, so
        it cannot loop: a run that moves the vault HEAD onto its own worker branch
        may trip this falsely, and the replacement process — reading a HEAD that
        has been restored to main — simply finds a timestamp that is not newer than
        its own fresh baseline. The cost of that false positive is one restart
        nobody needed; no run is ever interrupted by it.
        """
        try:
            if self._drain_requested:
                return True
            now_mono = time.monotonic()
            if (self._code_checked_at is not None
                    and now_mono - self._code_checked_at < _CODE_CHECK_TTL):
                return False
            self._code_checked_at = now_mono
            self._prime_code_baseline()
            baseline = self._code_baseline
            if baseline is None:
                return False
            current = self._code_time_fn()
            if current is None or not current > baseline:
                return False
            self._drain_requested = True
            return True
        except Exception as e:  # noqa: BLE001 - a guard must not kill the loop
            self.log(f"drain: code check failed (continuing): {e}")
            return False

    def _send(self, in_reply_to: m.Envelope, type: str, **payload) -> None:
        with self._lock:
            self.mailbox.send(in_reply_to.reply(type, payload=payload or {}))

    def _accept_reason(self, msg: m.Envelope) -> str | None:
        """Return a rejection reason, or None to accept.

        An envelope without `task_path` carries nothing to run: every real
        dispatch builds its payload in tasks.load_dispatchable(), which always
        sets the key (its value may be "" -- a md-less discovery run is still a
        real dispatch, so only the key's absence counts). A bare
        `dispatch --payload '{}'` envelope used to start a full five-stage run
        on nothing. A ledger row resent from before task_path existed is
        rejected too and recovers through the orchestrator's normal re-dispatch.
        """
        payload = msg.payload or {}
        requires = payload.get("requires", [])
        missing = [c for c in requires if c not in self.agent.capabilities]
        if missing:
            return f"missing capabilities: {missing}"
        if "task_path" not in payload:
            return "no dispatch payload (task_path missing)"
        return None

    def _vault_layout(self) -> tuple[Path, Path]:
        """(ledger dir, vault root), both derived from the `<vault>/Worker/agents`
        mailbox root unless a ledger dir was given."""
        worker_dir = self.mailbox.root.parent
        return (Path(self.ledger_root) if self.ledger_root else worker_dir / "assignments",
                worker_dir.parent)

    def _skip_reason(self, msg: m.Envelope) -> str | None:
        """Why this TASK_ASSIGN must not run at all, or None to run it.

        Last line behind the mailbox tombstones: an envelope that a git restore
        brought back after its ack can reach here looking brand new. Re-running it
        redid a whole pipeline on a task that had already finished or had since
        been re-scoped into a parent bundle.

        - Ledger row DONE. `Orchestrator.dispatch` returns before `send()` on a
          DONE row and `_retry` sets DISPATCHED before sending, so no legitimate
          TASK_ASSIGN exists while the row is DONE. corr_id is only logged — a
          normal re-dispatch carries a new one and must keep running.
        - The dispatched task's OWN md is `worker_mode: off`, `auto_rescoped` or
          `is_parent` (the task later became the rescoped parent; parents are
          never dispatchable). The `parent:` ancestor is deliberately NOT read:
          every child of a split bundle has an `off` parent by construction, so
          that rule would silently stop all split work.

        The caller sends no reply on skip. FAIL would trigger `_retry`,
        NEEDS_INPUT would overwrite DONE with AWAITING_HUMAN, and ACCEPT or even a
        HEARTBEAT rewrites the row via `apply_reply`. Fail-open: anything that
        cannot be read runs exactly as before.
        """
        try:
            if not msg.task_id:
                return None
            ledger_root, vault_root = self._vault_layout()
            # Route through the configured backend when no explicit ledger dir was
            # set: in git mode open_state_store(vault_root).ledger() is
            # Ledger(vault_root/Worker/assignments) == Ledger(ledger_root), so this
            # is byte-identical; external mode reads the SQLite ledger instead. An
            # explicit ledger_root keeps the direct Ledger construction.
            if self.ledger_root is None:
                row = open_state_store(vault_root).ledger().load(msg.task_id)
            else:
                row = Ledger(ledger_root).load(msg.task_id)
            if row is not None and row.state == DONE:
                return (f"ledger row already done (row corr={row.corr_id}, "
                        f"envelope corr={msg.corr_id})")
            tp = (msg.payload or {}).get("task_path") or ""
            if tp:
                p = Path(tp)
                p = p if p.is_absolute() else vault_root / p
                if p.is_file():
                    fm = repos._frontmatter(p)
                    # task_worker_mode_from_fm also maps a bare `off` (YAML boolean False).
                    if repos.task_worker_mode_from_fm(fm) == "off":
                        return "task md worker_mode: off"
                    if _truthy(fm.get("auto_rescoped")) or _truthy(fm.get("is_parent")):
                        return "task md is a rescoped parent (auto_rescoped/is_parent)"
        except Exception as e:  # noqa: BLE001 - fail-open: run as before
            self.log(f"  skip-guard failed (running anyway): {e}")
        return None

    def handle(self, msg: m.Envelope) -> None:
        self.log(f"recv {msg.type} task={msg.task_id} from={msg.frm}")
        if msg.type == m.TASK_ASSIGN:
            reason = self._accept_reason(msg)
            if reason:
                self._send(msg, m.REJECT, reason=reason)
                self.log(f"  REJECT: {reason}")
                return
            skip = self._skip_reason(msg)
            if skip:
                self.log(f"  SKIP (no run, no reply; envelope consumed): {skip}")
                return
            self._send(msg, m.ACCEPT)
            outcome = self.executor.execute(
                msg, self.agent,
                progress=lambda t: self._send(msg, m.PROGRESS, note=t),
                on_run_start=lambda rid, rdir: self.claims.attach_run(msg.msg_id, rid, rdir),
            )
            if outcome.ok:
                self._send(msg, m.RESULT, summary=outcome.summary, **outcome.payload)
                self.log(f"  RESULT: {outcome.summary}")
            else:
                self._send(msg, m.FAIL, reason=outcome.summary, **outcome.payload)
                self.log(f"  FAIL: {outcome.summary}")
        elif msg.type in (m.PING, m.QUERY):
            self._send(msg, m.HEARTBEAT, mode=self.executor.mode, status="idle")
        elif msg.type == m.CANCEL:
            self.log(f"  CANCEL for {msg.task_id} (nothing running in sync model)")
        # WAKE / others: just acknowledged by consumption

    def _classify_redelivery(self, msg: m.Envelope, claim: dict) -> str:
        """A claimed envelope came back: say what happened instead of re-running it.

        This is the whole point of the claim. An envelope is only ever redelivered
        because the ack never happened, and with a claim on disk we know the run
        did start — so re-entering `handle()` would redo a pipeline that may
        already have committed, pushed and merged.

        `NEEDS_INPUT`, deliberately **not** `FAIL`: the orchestrator routes FAIL
        into `_retry()` (orchestrator.py:354), which would re-dispatch the task and
        cause exactly the re-run this exists to prevent. NEEDS_INPUT parks it as
        AWAITING_HUMAN — non-terminal, so dispatch's dedup still refuses to re-send
        it, and `reap_stale` ignores it (AWAITING_HUMAN ∉ _IN_FLIGHT).
        """
        verdict, evidence = claims_mod.classify(claim)
        self.log(f"redelivered {msg.type} task={msg.task_id} msg={msg.msg_id} "
                 f"-> {verdict}: {evidence}")
        self.claims.note_redelivery(msg.msg_id, verdict, evidence)
        if verdict != claims_mod.LIVE:
            self._send(msg, m.NEEDS_INPUT,
                       reason=f"redelivered envelope, execution already started ({verdict}): {evidence}",
                       redelivered=True, verdict=verdict, evidence=evidence,
                       claim=claim)
        return verdict

    def poll_once(self, only_bypass: bool = False) -> int:
        """Consume the mailbox. `only_bypass` handles just the messages whose task
        carries `bypass_guard`, leaving the rest queued and un-acked.

        Both the orchestrator and this loop enforce the usage guard, so a bypass
        that cleared only the dispatch side would put the message in the mailbox
        and leave it there — dispatched on paper, never run.

        Claim-before-handle: the fact that execution STARTED is written to disk
        before `handle()` is entered, because `finally` only covers exceptions.
        SIGKILL, a reboot or a daemon restart skips the ack entirely, and
        `Mailbox.receive()` — whose sole redelivery gate is the cursor — hands the
        same envelope back on the next idle poll. An envelope that arrives with no
        claim has genuinely never run and keeps its at-least-once redelivery,
        untouched; only a claimed one is diverted.

        Self-drain: a batch is also where this process gets replaced. The code check
        sits after `_run_claimed` — i.e. after the ack, before the next claim — and
        nowhere else. Earlier would abandon an envelope mid-pipeline; later (at poll
        entry only) would never fire, because the defect being fixed is precisely
        that a full queue keeps this loop inside one batch. What is left stays
        un-acked, so `receive()` hands it to whatever process comes up next."""
        with self._lock:
            msgs = self.mailbox.receive(self.agent.id)
        handled = 0
        for i, msg in enumerate(msgs):
            if only_bypass and not (msg.payload or {}).get("bypass_guard"):
                continue                      # stays queued, un-acked, for later
            # NOTE: after the only_bypass filter on purpose — a bypass run that
            # dies mid-pipeline is the same accident and gets the same treatment.
            prior = self.claims.read(msg.msg_id)
            if prior is not None:
                self._divert_redelivery(msg, prior)
                handled += 1
                continue                      # NOT handled — no re-run
            self._run_claimed(msg)
            handled += 1
            # `i` indexes the original batch and is used for nothing but "is there
            # anything left to hand over" — a last envelope has nobody to drain for,
            # and skipping the lookup is what keeps the common one-message poll from
            # paying for a `git log` at all.
            if i + 1 < len(msgs) and self._code_changed():
                self.log(f"drain: code changed — leaving {len(msgs) - i - 1} "
                         f"message(s) queued for the next process")
                break
        return handled

    def _divert_redelivery(self, msg: m.Envelope, prior: dict) -> None:
        verdict = self._classify_redelivery(msg, prior)
        with self._lock:
            self.mailbox.ack(self.agent.id, msg)
            if verdict != claims_mod.LIVE:
                # The run is over and now surfaced; drop the record so _state
                # does not grow forever. A LIVE claim belongs to the process
                # still executing — leave it to close its own.
                self.claims.close(msg.msg_id)

    def _run_claimed(self, msg: m.Envelope) -> None:
        """Claim, handle, ack — one envelope, start to finish (see poll_once)."""
        try:
            with self._lock:
                self.claims.open(msg)
        except OSError as e:
            # Losing the record must not stop the work; it only means a crash
            # from here would be indistinguishable from a never-started task,
            # i.e. today's behaviour.
            self.log(f"claim write failed for {msg.msg_id} (continuing): {e}")
        # Heartbeat: from here until handle() returns, a stage really is
        # executing, and worker._runner_idle must refuse to SIGTERM us.
        # Deliberately NOT at poll entry — that fires every 15s whether or
        # not there is work, which would make the file permanently fresh and
        # rebuild the constant-busy defect this replaced. Deliberately after
        # the only_bypass filter and the redelivery `continue` too: neither
        # path does any work, so neither may hold the heartbeat.
        self._work_begin()
        try:
            self.handle(msg)
        finally:
            # Released first, and never able to raise (it swallows OSError),
            # so a heartbeat cannot outlive its work even if the ack below
            # throws — otherwise the 90-minute stale cutoff would be the only
            # thing releasing the restart gate.
            self._work_end()
            with self._lock:
                self.mailbox.ack(self.agent.id, msg)  # at-least-once consumed
                self.claims.close(msg.msg_id)   # only reached if the ack returned

    def _work_begin(self) -> None:
        # Refcounted: with parallel runs the file is one shared "busy" flag, so
        # the first run to finish must not clear it under the others. Every
        # start rewrites it, which also refreshes its mtime for the stale cutoff.
        with self._lock:
            self._busy += 1
            _worker.runner_heartbeat_start()

    def _work_end(self) -> None:
        with self._lock:
            self._busy = max(0, self._busy - 1)
            if self._busy == 0:
                _worker.runner_heartbeat_clear()

    def poll_parallel(self, slots: int, only_bypass: bool = False,
                      cap: dict | None = None) -> int:
        """Non-blocking poll: start up to `slots` concurrent runs on threads.

        The same claim/handle/ack sequence as poll_once, per envelope, plus:
          - an envelope already running here is skipped (it stays un-acked, so
            receive() keeps returning it until its thread acks);
          - never two runs in one worktree — same-repo runs on distinct per-run
            worktrees (slug keys) may run together, while vault runs share the
            single "<vault>" checkout and stay serial; the orchestrator still
            only dispatches overlap-ineligible pairs together (its own cap), this
            is the runner-side half of that invariant;
          - a run beyond the first also needs free memory (see _mem_ok).
        Non-TASK_ASSIGN envelopes (PING/CANCEL/…) are instant and run inline.

        `cap` (multi-account capacity): the `usage_guard.account_slots` dict the
        parallel loop computed this poll. When it carries an account model
        (`order`/`per_account` non-empty), each new run is ALSO assigned to a
        claude account (`_assign_account`: project pin, else the widest-slack
        account with a free per-account slot), recorded in `_inflight_acct` and
        carried to `_account_env` via the per-thread `_ASSIGNED_ACCOUNT`. When
        `cap` is None / empty (serial, direct test calls, no profile) the
        assignment is skipped entirely and the dispatch is byte-identical.
        Returns how many envelopes it started or consumed this poll."""
        has_accounts = bool(cap and (cap.get("order") or cap.get("per_account")))
        with self._lock:
            if self._busy:
                _worker.runner_heartbeat_start()   # keep the busy flag fresh
            msgs = self.mailbox.receive(self.agent.id)
            inflight_counts = Counter(a for a in self._inflight_acct.values() if a)
        handled = 0
        for msg in msgs:
            with self._lock:
                if msg.msg_id in self._inflight:
                    continue
                running = len(self._inflight)
                busy_keys = set(self._inflight.values())
            if only_bypass and not (msg.payload or {}).get("bypass_guard"):
                continue
            prior = self.claims.read(msg.msg_id)
            if prior is not None:
                self._divert_redelivery(msg, prior)
                handled += 1
                continue
            if msg.type != m.TASK_ASSIGN:
                self._run_claimed(msg)
                handled += 1
                continue
            # Before the slots check, not after: a queue that is permanently full
            # is the situation this exists for, and a flag that could only go up
            # on a poll with a free slot would never go up at all. _CODE_CHECK_TTL
            # is what keeps this from costing a `git log` per envelope.
            if self._code_changed():
                self.log(f"drain: code changed — no new slots, waiting for "
                         f"{running} in-flight run(s)")
                break
            if running >= slots:
                break                      # FIFO: the rest wait for a free slot
            key = _checkout_key(msg.payload, msg.task_id)
            if key in busy_keys:
                continue                   # same worktree busy; later ones may go
            if running and not self._mem_ok():
                break
            acct = None
            if has_accounts:
                acct = self._assign_account(msg.payload, msg.task_id, cap, inflight_counts)
                if acct is None:
                    continue               # no account has a free slot for this
                                           # msg; a later one (pin / free acct) may
            with self._lock:
                self._inflight[msg.msg_id] = key
                if has_accounts:
                    self._inflight_acct[msg.msg_id] = acct
            if acct:
                inflight_counts[acct] += 1   # balance within this same poll
            threading.Thread(target=self._run_threaded, args=(msg,),
                             name=f"run-{msg.task_id}", daemon=True).start()
            self.log(f"parallel: started task={msg.task_id} "
                     f"({running + 1}/{slots} slot(s))"
                     + (f" acct={acct}" if acct else ""))
            handled += 1
        return handled

    def _assign_account(self, payload: dict | None, task_id: str, cap: dict,
                        inflight_counts: Counter) -> str | None:
        """The claude account a new run starts on, or None. A project pin wins
        (the task's anchor `.macp` `account`, when it names an account the cap
        knows — ignoring the per-account count, since a pin is a hard routing,
        not load balancing). Else the first account in `cap["order"]` (widest
        slack, then priority) whose in-flight count is below its per-account slot
        cap. None = nothing fits this poll → the caller skips this envelope (a
        later one may still land on a free account or its pin). The anchor is the
        existing external clone if present, else the vault — a clone is never
        forced here. Never raises (→ None, caller gates nothing)."""
        per = cap.get("per_account") or {}
        order = cap.get("order") or []
        try:
            repo_url = (payload or {}).get("repo")
            anchor = None
            if repo_url:
                existing = repos.resolve(payload, self.agent.repo, clone=False)
                anchor = str(existing) if existing else None
            else:
                anchor = str(stages._VAULT_DATA_ROOT)
            if anchor:
                from todo_system import macp
                pin = getattr(macp.resolve(anchor).get("project"), "account", None)
                if pin and pin in per:
                    return pin                 # pin wins, count ignored
        except Exception:
            pass
        for name in order:
            if inflight_counts.get(name, 0) < per.get(name, 0):
                return name
        return None

    def _run_threaded(self, msg: m.Envelope) -> None:
        # Hand this thread the account poll_parallel assigned it, for _account_env
        # to read deep inside _execute (None when no account model → unchanged).
        _ASSIGNED_ACCOUNT.name = self._inflight_acct.get(msg.msg_id)
        try:
            self._run_claimed(msg)
        except Exception as e:  # a bad run must not take the thread table down
            self.log(f"run error task={msg.task_id} (continuing): {e}")
        finally:
            with self._lock:
                self._inflight.pop(msg.msg_id, None)
                self._inflight_acct.pop(msg.msg_id, None)
            _ASSIGNED_ACCOUNT.name = None   # never let an assignment outlive its run

    def _mem_ok(self) -> bool:
        """Room for one more run next to those already going? Gradle builds peak
        at ~3.4GB each and have OOM-killed the whole runner unit on the 8GB HQ
        (2026-09-13), so an extra run waits until MemAvailable clears the floor.
        Unreadable meminfo (non-Linux) fails open."""
        try:
            floor = float(os.environ.get("RUNNER_PARALLEL_MIN_AVAIL_MB", 3000))
        except ValueError:
            floor = 3000.0
        avail = _mem_available_mb()
        ok = avail is None or avail >= floor
        if not ok and not self._mem_hold_logged:
            self.log(f"parallel: holding extra slot — MemAvailable {avail:.0f}MB "
                     f"< {floor:.0f}MB")
        self._mem_hold_logged = not ok
        return ok

    def _heartbeat(self) -> None:
        """Proactively tell the control plane 'I'm alive' when idle (active work
        already emits PROGRESS = a liveness signal). Lets the orchestrator report
        agent health without a manual SSH check. Never break the loop on failure."""
        try:
            self.mailbox.send(m.Envelope(
                to=m.ORCHESTRATOR, frm=self.agent.id, type=m.HEARTBEAT,
                payload={"mode": self.executor.mode, "status": "idle",
                         "capabilities": list(self.agent.capabilities)}))
        except Exception as e:
            self.log(f"heartbeat failed (continuing): {e}")

    def _usage_blocked(self) -> str | None:
        """Reason when the Claude-usage guard says not to start work, else None.

        Evaluated per poll (NOT at import/startup — a resident daemon must not
        freeze a decision the way the old import-time STAGE_MODEL did). Fail-open
        and never raises: guard trouble must not kill the standby loop.
        """
        try:
            from todo_system import usage_guard
            d = usage_guard.check()
            if not d.get("allowed", True):
                return d.get("reason") or "usage guard blocked"
        except Exception:
            pass
        return None

    def _max_parallel(self) -> int:
        """Configured concurrency ceiling: RUNNER_MAX_PARALLEL, else the
        registry's `concurrency`. 1 = the original serial loop, untouched."""
        raw = os.environ.get("RUNNER_MAX_PARALLEL")
        try:
            n = int(raw) if raw else int(getattr(self.agent, "concurrency", 1) or 1)
        except ValueError:
            n = int(getattr(self.agent, "concurrency", 1) or 1)
        return max(1, n)

    def _per_account_max(self) -> int:
        """Per-account concurrency ceiling: `RUNNER_MAX_PARALLEL_PER_ACCOUNT`,
        else the registry's `concurrency` (the historical single-account ceiling).
        >= 1."""
        raw = os.environ.get("RUNNER_MAX_PARALLEL_PER_ACCOUNT")
        try:
            n = int(raw) if raw else int(getattr(self.agent, "concurrency", 1) or 1)
        except ValueError:
            n = int(getattr(self.agent, "concurrency", 1) or 1)
        return max(1, n)

    def _global_cap(self) -> int | None:
        """The GLOBAL sum ceiling — `RUNNER_MAX_PARALLEL` *when it is set*, else
        None (no cut). New meaning under multi-account sizing: the env caps the
        SUM across accounts, not one run. Unset (the default) = the sum stands,
        so added accounts actually add capacity. `_max_parallel` keeps its own,
        unchanged reading of the same env for the serial/parallel-mode gate."""
        raw = os.environ.get("RUNNER_MAX_PARALLEL")
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _account_slots(self) -> dict:
        """The usage guard's per-account slot cap right now
        (`usage_guard.account_slots`): `{total, per_account, slack, order,
        active}`. `total` is the summed-and-capped slot count the parallel loop
        uses (0 = every usable account at capacity → the hold branch). Fail-soft
        to an empty cap (total 0, no account model) so a guard error never kills
        the poll; the empty cap makes `poll_parallel` fall back to its
        pre-existing, account-agnostic dispatch."""
        try:
            from todo_system import usage_guard
            return usage_guard.account_slots(self._per_account_max(), self._global_cap())
        except Exception:
            return {"total": 0, "per_account": {}, "slack": {}, "order": [], "active": None}

    def _write_slots_snapshot(self, cap: dict) -> None:
        """Best-effort: publish this poll's live-slot picture
        (`usage_guard.write_runner_slots`) for the dashboard / `macp account list`
        to read — the runner is the single source of truth for live slots, so it
        reports both the used (in-flight) and max (granted) count per account.
        Never raises; a write hiccup must not sink the loop."""
        try:
            from todo_system import usage_guard
            with self._lock:
                used = Counter(a for a in self._inflight_acct.values() if a)
                total_used = len(self._inflight)
            per = cap.get("per_account") or {}
            usage_guard.write_runner_slots({
                "updated": time.time(),
                "total_used": total_used,
                "total_max": cap.get("total"),
                "per_account": {name: {"used": used.get(name, 0), "max": mx}
                                for name, mx in per.items()},
            })
        except Exception:
            pass

    def run_forever(self, interval: float = 15.0,
                    heartbeat_interval: float = 300.0) -> None:
        self.log(f"runner up: agent={self.agent.id} mode={self.executor.mode} "
                 f"interval={interval}s")
        last_beat = 0.0
        usage_block_logged: str | None = None
        max_n = self._max_parallel()
        slots_logged: int | None = None
        if max_n > 1:
            self.log(f"parallel mode: up to {max_n} concurrent run(s), "
                     f"paced by the usage guard")
        # One `git log` at boot, so the first real check has something to compare
        # against and pays nothing itself.
        self._prime_code_baseline()
        while True:
            # Outside the try on purpose: this is the one path that ends the
            # process, and inside it the `except` below would log the return as
            # "poll error (continuing)" the day someone raises here. While the
            # flag is up no poll_* runs at all, so nothing new is ever claimed —
            # we only wait out the runs already on threads. systemd's
            # Restart=always brings the process back on the new code.
            if self._drain_requested:
                with self._lock:
                    idle = not self._inflight and self._busy == 0
                if idle:
                    self.log("drain: code changed — exiting for restart")
                    return
                time.sleep(interval)
                continue
            try:
                # Usage guard: while blocked, leave already-dispatched messages
                # queued in the mailbox (unclaimed, un-acked) instead of burning
                # quota on them. They resume untouched when the guard clears.
                if max_n > 1:
                    # Parallel: the per-account slot SUM decides the hold, not the
                    # single active account's check(). An account with slack runs
                    # even while another is blocked (that is the capacity this
                    # task adds); the whole loop holds only when every usable
                    # account is at its cap (total 0), and then still lets a
                    # bypass_guard task through.
                    cap = self._account_slots()
                    self._write_slots_snapshot(cap)   # dashboard / CLI read this
                    slots = cap["total"]
                    if slots <= 0:
                        blocked = self._usage_blocked() or "all accounts at capacity"
                        if blocked != usage_block_logged:
                            self.log(f"usage guard blocked — deferring queued work: {blocked}")
                            usage_block_logged = blocked
                        handled = self.poll_parallel(1, only_bypass=True)
                        if handled:
                            self.log(f"bypass_guard: ran {handled} task(s) despite the hold")
                    else:
                        if usage_block_logged is not None:
                            self.log("usage guard cleared — resuming")
                            usage_block_logged = None
                        if slots != slots_logged:
                            self.log(f"parallel slots: {slots}/{max_n}")
                            slots_logged = slots
                        handled = self.poll_parallel(slots, cap=cap)
                else:
                    # Serial: one active account, byte-identical to before.
                    blocked = self._usage_blocked()
                    if blocked:
                        if blocked != usage_block_logged:
                            self.log(f"usage guard blocked — deferring queued work: {blocked}")
                            usage_block_logged = blocked
                        # …except a task explicitly marked worth spending out of turn.
                        handled = self.poll_once(only_bypass=True)
                        if handled:
                            self.log(f"bypass_guard: ran {handled} task(s) despite the hold")
                    else:
                        if usage_block_logged is not None:
                            self.log("usage guard cleared — resuming")
                            usage_block_logged = None
                        handled = self.poll_once()
                now = time.time()
                # heartbeat only when idle + throttled, to bound git-mailbox churn
                if (handled == 0 and not self._inflight
                        and now - last_beat >= heartbeat_interval):
                    self._heartbeat()
                    last_beat = now
            except Exception as e:  # a bad message must not kill the standby loop
                self.log(f"poll error (continuing): {e}")
            time.sleep(interval)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def resolve_agent(args, base: Path | None = None) -> Agent | None:
    """Agent identity from the registry (--agent) or inline flags (--agent-id).
    The inline path loads no registry, so a bare-python3 remote host needs no
    PyYAML — only the stdlib transport.

    ``base`` resolves a relative ``--registry`` (and the relative
    ``DEFAULT_REGISTRY``); ``None`` keeps ``load_registry``'s own cwd default, so
    existing callers behave exactly as before.
    """
    if getattr(args, "agent", None):
        return load_registry(args.registry, base=base).get(args.agent)
    if getattr(args, "agent_id", None):
        def _split(s):
            return [x for x in (s or "").split(",") if x]
        return Agent(id=args.agent_id, kind=args.kind, repo=args.repo,
                     projects=_split(args.projects),
                     capabilities=_split(args.capabilities))
    return None


def _main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="python -m todo_system.orchestrator.runner")
    # Identity: either look one up in the registry (--agent, Mac-side, needs
    # PyYAML) OR pass it inline (--agent-id + flags) so a bare-python3 remote
    # host runs without the registry at all (transport stays stdlib-only).
    p.add_argument("--agent", help="agent id to look up in the registry")
    p.add_argument("--agent-id", help="run this agent id inline, no registry")
    p.add_argument("--kind", default="ai-remote")
    p.add_argument("--repo", default=None)
    p.add_argument("--projects", default="", help="comma-separated")
    p.add_argument("--capabilities", default="", help="comma-separated")
    p.add_argument("--uri", default=f"git://{DEFAULT_ROOT}")
    p.add_argument("--registry", default=None)
    p.add_argument("--mode", choices=["shadow", "live", "live-remote", "live-staged"],
                   default="shadow")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="one poll cycle then exit")
    g.add_argument("--daemon", action="store_true", help="stand by and loop")
    p.add_argument("--interval", type=float, default=15.0)
    args = p.parse_args(argv)

    # `--uri`/`--registry` default to vault-relative values, which used to mean
    # "relative to the cwd" and pinned this command to the vault directory. The
    # vault root is the right base; cwd stays the fallback, so a cwd-at-the-vault
    # invocation (what the units do) is byte-identical.
    base = paths.vault_root_or_cwd()

    agent = resolve_agent(args, base)
    if agent is None:
        print("error: pass --agent <id> (registry) or --agent-id <id> (inline)")
        return 2
    if agent.is_human:
        print(f"error: {agent.id} is a human agent — use human.py, not runner.py")
        return 2
    if agent.kind == KIND_AI_LOCAL:
        # HQ host guard — an ai-local runner is the HQ's own data plane; on any
        # other host it would re-execute the shared inbox (envelopes are
        # git-tracked, the ack cursor is local-only). ai-remote runners are a
        # remote host's own plane and stay unguarded. No marker = no change
        # (fail-open — see hq_host.py).
        from . import hq_host
        # The vault holds the HQ marker, so the check reads it from there — from
        # a cwd outside the vault it found no marker and fell open.
        ok, reason = hq_host.check(base)
        if not ok:
            print(f"hq-host guard: {reason}")
            return 3
    # Route the mailbox through the configured backend at the default URI: git
    # mode is byte-identical to open_mailbox(default uri, base), external mode
    # hands back the SQLite mailbox. A non-default --uri keeps open_mailbox.
    if args.uri == f"git://{DEFAULT_ROOT}":
        mailbox = open_state_store(base).mailbox()
    else:
        mailbox = open_mailbox(args.uri, base=base)
    runner = Runner(mailbox, agent, make_executor(args.mode))

    if args.daemon:
        runner.run_forever(args.interval)
        return 0
    n = runner.poll_once()
    print(f"processed {n} message(s) for {agent.id} (mode={args.mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
