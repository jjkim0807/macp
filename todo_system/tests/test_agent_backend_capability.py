"""Capability gate — refuse/warn before dispatch, never silently route
(agent-backends leaf ④, AC #3).

Two layers:

* the pure decision — ``stages.required_capabilities`` (read-only / test-only
  stages need ``tool_allowlist``; write stages need only ``headless``) and
  ``agent_backend.missing_capabilities`` (what a backend lacks for a stage);
* the runner wiring — ``StagedExecutor.execute`` refuses a stage (ok=False, a
  ``capability_gate`` detail) and never runs the subprocess when the chosen
  backend misses a required capability, and proceeds when it does not. The
  positive path (claude supports all eight) is asserted explicitly so the gate
  can never pass vacuously ([[authz-denial-only-gates-pass-vacuously]]).

No ``claude`` binary runs. No personal identifier appears here (the engine tree
is scanned by the public-identifier gate).
"""

from __future__ import annotations

import contextlib
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import agent_backend as ab
from todo_system.orchestrator import message as m
from todo_system.orchestrator import repos
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator import stages
from todo_system.orchestrator.registry import Agent
from todo_system.orchestrator.runner import StagedExecutor

_PIPELINE_STAGES = ("research", "plan", "implement", "verify", "report",
                    "delegate", "quick", "discover")


class _Backend:
    """A minimal backend that supports exactly ``caps``."""

    name = "dummy"

    def __init__(self, caps):
        self._caps = frozenset(caps)

    def supports(self, capability):
        return capability in self._caps


# ---- the pure decision ------------------------------------------------------

def test_required_capabilities_readonly_stages_need_allowlist():
    for stage in ("research", "plan", "verify", "discover"):
        assert stages.required_capabilities(stage) == frozenset({"headless", "tool_allowlist"})
    for stage in ("implement", "report", "delegate", "quick"):
        assert stages.required_capabilities(stage) == frozenset({"headless"})
    # unknown stage → the headless floor, never raises
    assert stages.required_capabilities("totally-new-stage") == frozenset({"headless"})


def test_missing_capabilities_flags_allowlist_gap():
    allow_all_only = _Backend({"headless"})           # no tool_allowlist
    # read-only stages: refused, with the exact missing capability named
    assert ab.missing_capabilities(allow_all_only, "research") == ["tool_allowlist"]
    assert ab.missing_capabilities(allow_all_only, "verify") == ["tool_allowlist"]
    # write stages: allowed (headless is enough)
    assert ab.missing_capabilities(allow_all_only, "implement") == []
    assert ab.missing_capabilities(allow_all_only, "quick") == []


def test_missing_capabilities_headless_is_universally_required():
    useless = _Backend(set())                          # not even headless
    for stage in _PIPELINE_STAGES:
        assert "headless" in ab.missing_capabilities(useless, stage)


def test_claude_backend_supports_every_stage_positive_path():
    """The gate is not vacuous: claude clears EVERY stage (nothing missing)."""
    claude = ab.ClaudeAgentBackend()
    for stage in _PIPELINE_STAGES:
        assert ab.missing_capabilities(claude, stage) == [], stage


# ---- the runner wiring ------------------------------------------------------

def _drive(monkeypatch, tmp_path, stage, *, missing):
    """Drive one external-repo pass through StagedExecutor.execute for `stage`,
    with the dispatch machinery stubbed (the `_run_stage` patch set), and
    ``agent_backend.missing_capabilities`` forced to `missing`. Returns
    (ExecOutcome, run_stage_subprocess mock) so a caller can assert whether the
    stage subprocess ran."""
    runs = Path(tempfile.mkdtemp(dir=tmp_path))
    clone = tmp_path / "clone"
    clone.mkdir(exist_ok=True)
    vp = subprocess.CompletedProcess([], 0, "", "")
    vp.problem = None
    vp.orphaned_commits = None
    prep = subprocess.CompletedProcess([], 0, "", "")
    prep.orphaned_commits = None
    msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="t",
                     payload={"repo": "git@github.com:x/y.git", "auto_merge": False,
                              "model_tier": "standard"})
    run_sub = mock.Mock(side_effect=lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 0, "ok", ""))
    patches = [
        mock.patch.object(repos, "ensure_worktree", return_value=clone),
        mock.patch.object(stages, "restore_main"),
        mock.patch.object(stages, "install_push_guard"),
        mock.patch.object(stages, "default_branch", return_value="main"),
        mock.patch.object(stages, "prepare_external_branch", return_value=prep),
        mock.patch.object(stages, "prepare_vault_branch", return_value=vp),
        mock.patch.object(stages, "remote_branch_exists", return_value=True),
        mock.patch.object(stages, "resumable_run", return_value=None),
        mock.patch.object(stages, "inherit_artifacts", return_value=[]),
        mock.patch.object(stages, "RUNS_DIR", runs),
        mock.patch.object(stages, "build_stage_argv", return_value=["true"]),
        mock.patch.object(stages, "checkpoint_push",
                          return_value={"pushed": False, "tip": None, "note": ""}),
        mock.patch.object(stages, "cleanup_build_daemons"),
        mock.patch.object(stages, "remove_worktree"),
        mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
        mock.patch.object(StagedExecutor, "_acquire_checkout_lease", return_value=None),
        mock.patch.object(StagedExecutor, "_account_env", return_value=(None, None)),
        mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
        mock.patch.object(StagedExecutor, "_pipeline_for", return_value=(stage,)),
        mock.patch("todo_system.worker.runner_heartbeat_start"),
        mock.patch.object(stages, "run_stage_subprocess", run_sub),
        mock.patch("subprocess.run", side_effect=lambda *a, **k:
                   subprocess.CompletedProcess(a[0] if a else [], 0, "", "")),
        mock.patch.object(ab, "missing_capabilities", return_value=missing),
    ]
    agent = Agent(id="w", kind="ai-local", capabilities=["git"], repo="~/x")
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        out = StagedExecutor().execute(msg, agent, progress=lambda _s: None)
    return out, run_sub


def test_runner_refuses_stage_when_backend_misses_capability(monkeypatch, tmp_path):
    out, run_sub = _drive(monkeypatch, tmp_path, "research", missing=["tool_allowlist"])
    assert out.ok is False
    assert "capability" in out.summary and "tool_allowlist" in out.summary
    gate = out.payload.get("capability_gate")
    assert gate == {"backend": "claude", "stage": "research", "missing": ["tool_allowlist"]}
    # the gate fired BEFORE dispatch — the stage subprocess never ran.
    run_sub.assert_not_called()


def test_runner_dispatches_when_backend_supports_all(monkeypatch, tmp_path):
    # Empty missing list → the gate passes and the stage subprocess runs (the
    # positive path; the stage then fails later on the absent artifact, which is
    # not what we assert here).
    out, run_sub = _drive(monkeypatch, tmp_path, "research", missing=[])
    run_sub.assert_called_once()
    assert out.payload.get("capability_gate") is None
