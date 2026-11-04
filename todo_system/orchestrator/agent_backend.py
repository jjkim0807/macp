"""Vendor-neutral stage execution behind an ``AgentBackend`` interface.

macp calls itself a "control plane for coding agents" (docs/concepts.md), but
stage execution was hard-wired to ``claude -p`` (``stages.build_stage_argv`` +
``stages.run_stage_subprocess``) with one in-engine HTTP exception
(``solar_stage.run`` for upstage quick/delegate). This module introduces the
seam the design note (docs/design/agent-backends.md §4) draws: a small protocol
a dispatcher can talk to without knowing which vendor runs the stage.

Leaf ② (this file's first landing) is **additive** — ``ClaudeAgentBackend`` is a
thin delegate to the existing ``stages`` helpers and *nobody calls it yet*
(the runner is migrated onto it in leaf ③). The claude argv/env stays
byte-identical because ``run_stage`` builds the argv with the same
``stages.build_stage_argv`` call and runs it with the same
``stages.run_stage_subprocess`` call the runner used inline — pinned by
``test_agent_backend_golden.py`` and ``test_agent_backend.py``.

Design decisions carried here (design §4 Q2): claude-subscription-only machinery
(the model ladder / ``_probe`` / usage-guard pacing / account rotation) stays
OUTSIDE the backend, in the runner and ``model_select``/``usage_guard`` as today;
a non-claude backend runs with no effort flag and no pacing. The eight named
capabilities (``supports``) are the §2 list; the capability gate that consumes
them is leaf ④.

Stdlib + engine-local only (``stages`` is imported through the package so a test
that patches ``stages.build_stage_argv`` / ``run_stage_subprocess`` /
``record_stage_usage`` still intercepts). No personal identifier appears here —
the engine tree is scanned by the public-identifier gate.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

from . import stages

# The eight capabilities a stage needs from a backend (design §2). A dispatcher
# asks ``backend.supports(cap)`` before it routes a stage, and refuses/warns on a
# miss rather than letting the guarantee evaporate silently (leaf ④, AC #3).
CAPABILITIES = frozenset({
    "headless",        # (1) run one prompt non-interactively to completion
    "tool_allowlist",  # (2) restrict shell/file tools to a whitelist
    "turn_cap",        # (3) bound a stage by a turn budget
    "model_select",    # (4) pick a model for the task tier
    "effort",          # (4) reasoning-effort level (claude-only concept)
    "add_dir",         # (5) grant an extra read path outside the cwd
    "usage_report",    # (7) report token usage into stages.json
    "exit_quota",      # (6) signal "quota exhausted" distinctly from "error"
})

# The three exit meanings every backend maps its own signals onto (design §2-6).
EXIT_SUCCESS = "success"
EXIT_EXHAUSTED = "exhausted"
EXIT_ERROR = "error"


@runtime_checkable
class AgentBackend(Protocol):
    """One coding agent, able to run a worker stage.

    The interface sits at ``run_stage(...) -> CompletedProcess`` rather than a
    low-level ``(argv, env)`` builder because the two real shapes already meet at
    that height: a CLI subprocess (``claude -p`` / any ``config.build_invocation``
    vendor) and an in-engine HTTP tool loop (``solar_stage.run``) both return a
    ``CompletedProcess`` the runner's shared tail reads for ``returncode`` /
    ``stdout`` (design §4)."""

    name: str  # vendor id: "claude" | "upstage" | "gemini" | ...

    def supports(self, capability: str) -> bool:
        """Whether this backend can fulfil a named stage capability (one of
        :data:`CAPABILITIES`). The dispatcher calls this BEFORE running a stage;
        an unsupported capability a task needs is refused/warned, never silently
        dropped (design §2-2, AC #3)."""
        ...

    def run_stage(self, stage: str, **kwargs) -> subprocess.CompletedProcess:
        """Run one stage headless and return a ``CompletedProcess`` (``returncode``
        + ``stdout``), having written the stage artifact
        (``stages.STAGE_ARTIFACT[stage]``) under ``run_dir``. A timeout propagates
        as ``subprocess.TimeoutExpired`` (its ``.cmd`` carries the argv) so the
        runner's existing timeout arm is unchanged."""

    def classify_exit(self, rc: int, stdout: str) -> str:
        """``"success" | "exhausted" | "error"`` (design §2-6)."""

    def report_usage(self, stage: str, run_dir: Path, handle, **kwargs) -> dict | None:
        """A ``stages.USAGE_KEYS``-shaped token dict, or ``None`` when the backend
        has no usage channel (then the runner's rc==0 capacity arm stays inert,
        exactly as Solar's ``usage_rec=None`` does)."""


class ClaudeAgentBackend:
    """The default backend: ``claude -p`` via the existing ``stages`` helpers.

    Stateless and byte-identical to the pre-refactor inline path — ``run_stage``
    makes the SAME ``stages.build_stage_argv`` call and the SAME
    ``stages.run_stage_subprocess`` call the runner used, and ``report_usage``
    the same ``stages.record_stage_usage`` call. The argv it ran is carried on the
    returned ``CompletedProcess.args`` (and on a ``TimeoutExpired.cmd``), which is
    the ``handle`` the runner hands back to ``report_usage`` — so the backend
    keeps no per-run state and one instance serves every stage/run safely.
    """

    name = "claude"
    _CAPS = CAPABILITIES  # claude fulfils all eight

    def supports(self, capability: str) -> bool:
        return capability in self._CAPS

    def run_stage(self, stage: str, *, task_id: str, run_id: str, run_dir: Path,
                  branch: str, repo: str | None = None,
                  working_dir: str | None = None, auto_merge: bool = False,
                  project_context: str = "", retry_context: str = "",
                  allowed_tools: str | None = None, tier: str | None = None,
                  worker_root: Path | None = None, account=None,
                  cwd: str | None = None, env: dict | None = None,
                  timeout: float | None = None,
                  progress=None) -> subprocess.CompletedProcess:
        """Build the claude argv and run it. ``account`` is accepted for interface
        parity but unused: the claude account's env (``CLAUDE_CONFIG_DIR`` +
        ``CLAUDE_CODE_OAUTH_TOKEN``) is already layered into ``env`` by the runner,
        so passing it here would change nothing — and must not, for byte-identity.

        One argv is built and run; the keyword set is 1:1 with
        ``stages.build_stage_argv`` (design §4 drop-in). ``TimeoutExpired`` is NOT
        caught — it carries the argv in ``.cmd`` and the runner's timeout arm owns
        the checkpoint/record."""
        argv = stages.build_stage_argv(
            stage, task_id=task_id, run_id=run_id, run_dir=run_dir, branch=branch,
            repo=repo, working_dir=working_dir, auto_merge=auto_merge,
            worker_root=worker_root, project_context=project_context,
            retry_context=retry_context, allowed_tools=allowed_tools, tier=tier)
        return stages.run_stage_subprocess(
            argv, cwd=cwd, env=env, timeout=timeout, progress=progress)

    def classify_exit(self, rc: int, stdout: str) -> str:
        """claude prints a capacity/auth notice even at rc==0 (the 2026-07-31 blind
        spot), so a marker in stdout means ``exhausted`` whatever the code; a clean
        rc==0 is ``success``; any other non-zero exit is ``error``. Uses the same
        ``stages.is_capacity_limit`` the runner's capacity gate reads."""
        if stages.is_capacity_limit(stdout or ""):
            return EXIT_EXHAUSTED
        return EXIT_SUCCESS if rc == 0 else EXIT_ERROR

    def report_usage(self, stage: str, run_dir: Path, handle, *,
                     env: dict | None = None, returncode: int | None = None,
                     duration_ms: int | None = None,
                     timed_out: bool = False) -> dict | None:
        """``handle`` is the argv the stage ran (``CompletedProcess.args`` on the
        success path, ``TimeoutExpired.cmd`` on the timeout path). Delegates to the
        same ``stages.record_stage_usage`` the runner called inline, which reads
        the ``--session-id`` back off ``handle`` — so a stub argv (no session-id)
        records nothing, exactly as before."""
        return stages.record_stage_usage(
            run_dir, stage, handle, env=env, returncode=returncode,
            duration_ms=duration_ms, timed_out=timed_out)
