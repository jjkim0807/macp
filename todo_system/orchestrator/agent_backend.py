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

import json
import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..macp import config
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


def missing_capabilities(backend, stage: str) -> list[str]:
    """The capabilities ``stage`` requires (``stages.required_capabilities``) that
    ``backend`` does NOT support, sorted. Empty = the backend may run the stage.

    The capability gate (leaf ④, AC #3): a dispatcher calls this before routing a
    stage and refuses/warns on a non-empty result rather than silently running the
    stage with a weaker guarantee (e.g. a backend with no tool allowlist taking a
    read-only ``research``/``verify``). ``ClaudeAgentBackend`` supports all eight,
    so this is always ``[]`` for the claude path — the positive path is pinned by a
    test so the gate can never pass vacuously."""
    return [cap for cap in sorted(stages.required_capabilities(stage))
            if not backend.supports(cap)]


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


def neutral_run_metadata(*, run_id: str, task_id: str, run_dir: Path, branch: str,
                         stage: str, auto_merge: bool, repo: str | None = None,
                         working_dir: str | None = None) -> str:
    """The vendor-neutral run-metadata block appended to a CLI backend's prompt.

    A trimmed sibling of the block ``stages.build_stage_argv`` builds for claude:
    the run context (run_id/slug/run_dir/branch/stage/auto_merge + the external-repo
    note) WITHOUT claude-specific lines — no ``--allowed-tools`` authoritative line
    (a non-claude backend enforces its allowlist through its own config, not the
    prompt) and none of the macp-browser/emulator/remote-gate wrapper rules (those
    are claude-tool-allowlist entries). Kept here, not shared with build_stage_argv,
    so the claude prompt stays byte-identical and this one can evolve on its own."""
    lines = [
        "\n\n---\n\n# Run metadata (injected by orchestrator)\n\n",
        f"- run_id: `{run_id}`\n",
        f"- slug: `{task_id}`\n",
        f"- run_dir: `{run_dir}`\n",
        f"- branch: `{branch}`\n",
        f"- stage: `{stage}`\n",
        f"- auto_merge: `{str(auto_merge).lower()}`\n",
    ]
    if repo:
        lines.append(f"- repo: `{repo}`\n")
        if working_dir:
            lines.append(f"- working_dir: `{working_dir}`\n")
        lines.append("- **External repo task**: operate on the checkout; vault "
                     "run_dir artifacts are read-only references.\n")
    return "".join(lines)


# ---- Gemini usage: `--output-format json` stats → stages.USAGE_KEYS ----------
#
# The runner hands ``report_usage`` only the argv and ``run_dir`` (never stdout),
# so ``GeminiAgentBackend.run_stage`` saves the CLI's ``stats`` (+ ``session_id``)
# to a per-stage sidecar under ``run_dir`` and ``report_usage`` reads it back —
# the same read-back-from-disk shape as claude's transcript. The sidecar never
# holds the ``response`` text.
GEMINI_STATS_SUFFIX = ".gemini-stats.json"


def gemini_stats_path(run_dir: Path, stage: str) -> Path:
    """Where ``run_stage`` saves one stage's Gemini stats for ``report_usage``."""
    return Path(run_dir) / f"{stage}{GEMINI_STATS_SUFFIX}"


def _output_format(argv) -> str | None:
    """The ``--output-format`` value in ``argv`` (``--output-format X``,
    ``--output-format=X`` or ``-o X``; the last one wins), else None."""
    fmt = None
    toks = [str(t) for t in argv] if isinstance(argv, (list, tuple)) else []
    for i, tok in enumerate(toks):
        if tok in ("--output-format", "-o") and i + 1 < len(toks):
            fmt = toks[i + 1]
        elif tok.startswith("--output-format="):
            fmt = tok.split("=", 1)[1]
    return fmt


def _n(v) -> int:
    """A token count: int/float (not bool) → int, anything else → 0."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    return int(v)


def gemini_stats_usage(doc, *, duration_ms: int | None = None) -> dict | None:
    """Map a Gemini CLI ``--output-format json`` document (or the sidecar subset —
    same shape) onto a ``stages.USAGE_KEYS`` dict; None when it carries no usable
    per-model stats.

    Reference shape (Gemini CLI docs, design doc §3, checked 2026-10-22 — not yet
    captured from a live run, so every field is optional and odd values count 0)::

        {"session_id": "...",
         "stats": {"models": {"<model>": {
             "api": {"totalRequests", "totalErrors", "totalLatencyMs"},
             "tokens": {"prompt", "candidates", "total", "cached",
                        "thoughts", "tool"[, "input"]}}}}}

    Mapping (claude's meaning for each key):
    - ``input_tokens`` = Σ ``input`` when present, else ``max(prompt − cached, 0)``
      (claude's input excludes cache reads);
    - ``output_tokens`` = Σ ``candidates + thoughts`` (claude's output includes thinking);
    - ``cache_creation_input_tokens`` = 0; ``cache_read_input_tokens`` = Σ ``cached``;
    - ``num_turns`` = Σ ``api.totalRequests`` (None if no model reports it);
    - ``model`` = the model with the most ``tokens.total`` (fallback
      prompt+candidates+thoughts), ties → alphabetical;
    - ``duration_ms`` = the runner's wall clock (kwarg); ``session_id`` = the
      top-level ``session_id`` if a string; ``total_cost_usd`` = None (no cost field)."""
    if not isinstance(doc, dict):
        return None
    stats = doc.get("stats")
    if not isinstance(stats, dict):
        return None
    models = stats.get("models")
    if not isinstance(models, dict) or not models:
        return None
    inp = out = cached_sum = 0
    turns = None
    totals: dict[str, int] = {}
    for name, m in models.items():
        if not isinstance(m, dict):
            continue
        tokens = m.get("tokens")
        tokens = tokens if isinstance(tokens, dict) else {}
        api = m.get("api")
        api = api if isinstance(api, dict) else {}
        prompt, cached = _n(tokens.get("prompt")), _n(tokens.get("cached"))
        cand, thoughts = _n(tokens.get("candidates")), _n(tokens.get("thoughts"))
        explicit_in = tokens.get("input")
        if isinstance(explicit_in, (int, float)) and not isinstance(explicit_in, bool):
            inp += int(explicit_in)
        else:
            inp += max(prompt - cached, 0)
        out += cand + thoughts
        cached_sum += cached
        req = api.get("totalRequests")
        if isinstance(req, (int, float)) and not isinstance(req, bool):
            turns = (turns or 0) + int(req)
        total = tokens.get("total")
        totals[str(name)] = (_n(total) if isinstance(total, (int, float))
                             and not isinstance(total, bool)
                             else prompt + cand + thoughts)
    if not totals:
        return None
    model = sorted(totals, key=lambda k: (-totals[k], k))[0]
    sid = doc.get("session_id")
    got = {"input_tokens": inp, "output_tokens": out,
           "cache_creation_input_tokens": 0, "cache_read_input_tokens": cached_sum,
           "num_turns": turns, "duration_ms": duration_ms, "model": model,
           "session_id": sid if isinstance(sid, str) else None,
           "total_cost_usd": None}
    return {k: got.get(k) for k in stages.USAGE_KEYS}


class GeminiAgentBackend:
    """First non-claude CLI backend: Gemini CLI (user decision 2026-11-04).

    A thin wrapper over ``config.build_invocation`` (the vendor-neutral
    ``(argv, env)`` seam): ``run_stage`` assembles the stage prompt + neutral run
    metadata, lets ``build_invocation`` turn the account's ``command`` template
    into argv (``{prompt}`` substituted) with ``account.env``, injects
    ``GEMINI_MODEL`` for the task tier when the account pins one, and runs it with
    the SAME ``stages.run_stage_subprocess`` reaper the claude path uses.

    No effort flag, no ``--session-id`` transcript, no usage-guard pacing — those
    are claude-subscription-only (design §4 Q2); a non-claude backend runs without
    them. The model flag (``-m``/``--model``) is unconfirmed on the Gemini CLI
    (design §3 ⚠️), so model selection goes through ``GEMINI_MODEL`` in the env
    (``account.env`` by default, or the account's ``models[tier]`` pin here) — the
    path ``build_invocation`` already returns the account env on.

    Capabilities (design §3 ✅): headless (``-p``), tool_allowlist (``tools.core``),
    turn_cap (``maxSessionTurns``), model_select, usage_report (``--output-format
    json`` stats, parsed by :func:`gemini_stats_usage` from the per-stage sidecar
    :func:`gemini_stats_path` and recorded into stages.json; None under the
    ``MACP_STAGE_USAGE=0`` kill switch) and exit_quota (its own rate-limit
    signal). NOT effort or add_dir."""

    name = "gemini"
    _CAPS = frozenset({"headless", "tool_allowlist", "turn_cap",
                       "model_select", "usage_report", "exit_quota"})
    # Gemini/Google API rate-limit & quota signals → "exhausted" (its own signal,
    # mapped onto the common 3-way). An exit-53 "Turn limit exceeded" is NOT here:
    # that is the turn cap tripping, an "error" (the stage did not finish), not a
    # transient capacity wall.
    _EXHAUSTED_MARKERS = ("rate limit", "resource_exhausted", "resource exhausted",
                          "quota exceeded", "quota exhausted", "too many requests",
                          "status 429", "error 429")

    def supports(self, capability: str) -> bool:
        return capability in self._CAPS

    def run_stage(self, stage: str, *, task_id: str, run_id: str, run_dir: Path,
                  branch: str, repo: str | None = None,
                  working_dir: str | None = None, auto_merge: bool = False,
                  account=None, tier: str | None = None,
                  project_context: str = "", retry_context: str = "",
                  allowed_tools: str | None = None, worker_root: Path | None = None,
                  cwd: str | None = None, env: dict | None = None,
                  timeout: float | None = None,
                  progress=None) -> subprocess.CompletedProcess:
        full = (stages.strip_frontmatter(stages.load_stage_prompt(stage))
                + neutral_run_metadata(run_id=run_id, task_id=task_id, run_dir=run_dir,
                                       branch=branch, stage=stage, auto_merge=auto_merge,
                                       repo=repo, working_dir=working_dir))
        # build_invocation ignores claude extra_args for a non-claude agent, so the
        # invocation shape lives entirely in account.command (the admin configures
        # e.g. `gemini --yolo -p {prompt}`); we add no claude flags.
        argv, inv_env = config.build_invocation(account, full)
        merged_env = {**(env or {}), **inv_env}   # account.env (+ key) wins over the base
        model = self._model_for(account, tier)
        if model:
            merged_env["GEMINI_MODEL"] = model
        return stages.run_stage_subprocess(
            argv, cwd=(cwd or working_dir), env=merged_env,
            timeout=timeout, progress=progress)

    @staticmethod
    def _model_for(account, tier: str | None) -> str | None:
        """The account's model pin for this tier (``Account.models[tier]``), or None
        — None leaves whatever ``GEMINI_MODEL`` account.env already carries."""
        models = getattr(account, "models", None) or {}
        return models.get(tier) if tier else None

    def classify_exit(self, rc: int, stdout: str) -> str:
        t = (stdout or "").lower()
        if any(mrk in t for mrk in self._EXHAUSTED_MARKERS):
            return EXIT_EXHAUSTED
        return EXIT_SUCCESS if rc == 0 else EXIT_ERROR

    def report_usage(self, stage: str, run_dir: Path, handle, *,
                     env: dict | None = None, returncode: int | None = None,
                     duration_ms: int | None = None,
                     timed_out: bool = False) -> dict | None:
        """The stage's ``stages.USAGE_KEYS`` record from the ``--output-format json``
        stats ``run_stage`` saved to :func:`gemini_stats_path`, also recorded into
        stages.json ``usage.<stage>`` (as claude's ``record_stage_usage`` does).

        ``handle`` is the argv the stage ran. None — record nothing — only when
        usage is off (``MACP_STAGE_USAGE=0``) or ``handle`` did not ask for json
        output (the mirror of claude's "no ``--session-id`` → None"). Otherwise
        always a dict: the usage, or ``{"unavailable": "<why>"}``. No reason starts
        with ``"no usage in transcript"``, so ``stages.usage_shows_no_api_work``
        stays False and the runner's rc==0 capacity arm stays inert for gemini.
        Never raises."""
        try:
            if not stages.usage_enabled() or _output_format(handle) != "json":
                return None
            suffix = f" (rc={returncode})" if returncode not in (0, None) else ""
            if timed_out:
                rec = {"unavailable": "timeout"}
            else:
                try:
                    p = gemini_stats_path(run_dir, stage)
                    if not p.is_file():
                        rec = {"unavailable": "gemini stats not captured" + suffix}
                    else:
                        try:
                            doc = json.loads(p.read_text(encoding="utf-8"))
                        except (OSError, ValueError):
                            doc = None
                        rec = gemini_stats_usage(doc, duration_ms=duration_ms)
                        if rec is None:
                            rec = {"unavailable": "gemini stats unparseable" + suffix}
                except Exception as e:
                    rec = {"unavailable": f"usage reader error: {type(e).__name__}"}
            stages.record_usage(run_dir, stage, rec)
            return rec
        except Exception:
            return None
