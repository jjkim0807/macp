"""In-flight brake (2026-11-18): the usage gates only stopped NEW runs, so runs
already in flight rode a 5h window from 58% to 100%. Before an in-flight run
starts its next Claude stage, the runner now waits while the session window is
at its stage cap (usage_guard.stage_gate).

Unit tests drive `_wait_for_session_headroom` with a fake gate/clock; the wiring
test runs the real StagedExecutor stage loop with only the stage subprocess and
git plumbing mocked (harness: test_plan_rescope_wiring)."""
from __future__ import annotations

import contextlib
import subprocess
import time
from unittest import mock

from todo_system.orchestrator import message as m
from todo_system.orchestrator import repos, stages
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator.runner import StagedExecutor
from todo_system.orchestrator.tests.test_plan_rescope import REPO_URL
from todo_system.orchestrator.tests.test_plan_rescope_wiring import _agent, vault  # noqa: F401


class _Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _gate_seq(*answers):
    it = iter(answers)
    calls = []

    def gate():
        calls.append(1)
        try:
            return next(it)
        except StopIteration:
            return None
    gate.calls = calls
    return gate


def test_waits_while_held_then_goes():
    c = _Clock()
    hold = ("session 85% >= stage cap 80%", c.t + 3600)
    gate = _gate_seq(hold, hold, hold, None)
    notes = []
    assert runner_mod._wait_for_session_headroom(
        "implement", {}, notes.append, gate=gate, sleep=c.sleep, clock=c) is True
    assert len(gate.calls) == 4
    assert any("pausing before stage implement" in n for n in notes)
    assert notes[-1].startswith("usage: session headroom back")


def test_no_hold_no_wait_no_note():
    c = _Clock()
    notes = []
    assert runner_mod._wait_for_session_headroom(
        "implement", {}, notes.append, gate=lambda: None, sleep=c.sleep, clock=c)
    assert notes == [] and c.t == 1_000_000.0


def test_progress_is_periodic_so_the_run_is_not_reaped():
    c = _Clock()
    hold = ("session 90% >= stage cap 80%", c.t + 4 * 3600)
    gate = _gate_seq(*([hold] * 40))                     # 40 polls = 40 min
    notes = []
    runner_mod._wait_for_session_headroom("verify", {}, notes.append, gate=gate,
                                          sleep=c.sleep, clock=c)
    pauses = [n for n in notes if "pausing before stage" in n]
    assert len(pauses) == 4                              # every 10 min
    assert runner_mod.STAGE_GATE_PROGRESS_S < 90 * 60    # < the flat stale-reap floor


def test_bounded_by_the_window_reset_and_the_cap():
    c = _Clock()
    gate = _gate_seq(*([("x", c.t + 120)] * 1000))
    notes = []
    assert runner_mod._wait_for_session_headroom("verify", {}, notes.append, gate=gate,
                                                 sleep=c.sleep, clock=c)
    assert "proceeding" in notes[-1] and c.t - 1_000_000.0 <= 5 * 60
    c2 = _Clock()
    gate2 = _gate_seq(*([("x", c2.t + 10 * 3600)] * 10_000))
    assert runner_mod._wait_for_session_headroom("verify", {}, lambda _n: None, gate=gate2,
                                                 sleep=c2.sleep, clock=c2)
    assert c2.t - 1_000_000.0 <= runner_mod.STAGE_GATE_MAX_S + 60


def test_bypass_guard_and_switch_never_wait(monkeypatch):
    c = _Clock()
    gate = _gate_seq(("x", c.t + 3600))
    assert runner_mod._wait_for_session_headroom("verify", {"bypass_guard": True},
                                                 lambda _n: None, gate=gate,
                                                 sleep=c.sleep, clock=c)
    assert gate.calls == []
    monkeypatch.setenv(runner_mod.STAGE_GATE_ENV, "0")
    assert runner_mod._wait_for_session_headroom("verify", {}, lambda _n: None, gate=gate,
                                                 sleep=c.sleep, clock=c)
    assert gate.calls == []


def test_cancel_during_wait_stops():
    c = _Clock()
    gate = _gate_seq(*([("x", c.t + 3600)] * 10))
    with mock.patch.object(runner_mod, "_cancel_requested_now", return_value=True):
        assert runner_mod._wait_for_session_headroom(
            "verify", {}, lambda _n: None, gate=gate, sleep=c.sleep, clock=c) is False


def test_gate_error_fails_open():
    def boom():
        raise RuntimeError("guard down")
    assert runner_mod._wait_for_session_headroom("verify", {}, lambda _n: None, gate=boom)


def test_stage_loop_pauses_before_a_claude_stage(vault, tmp_path):  # noqa: F811
    """Wiring: StagedExecutor's real stage loop consults the gate BEFORE the
    stage subprocess runs, waits while held, then runs the stage."""
    root, _items, _templates, runs, _ledger = vault
    clone = tmp_path / "clone"
    clone.mkdir()
    order: list[str] = []
    answers = iter([("session 88% >= stage cap 80%", time.time() + 3600), None])

    def gate(decision=None, now=None):
        a = next(answers, None)
        order.append("gate:held" if a else "gate:go")
        return a

    def fake_run(argv, **kw):
        order.append("stage")
        return subprocess.CompletedProcess(argv, 0, "", "")

    vp = subprocess.CompletedProcess([], 0, "", "")
    vp.problem = None
    vp.orphaned_commits = None
    msg = m.Envelope(to="worker-default", type=m.TASK_ASSIGN, task_id="example-gate",
                     corr_id="c1",
                     payload={"repo": REPO_URL, "auto_merge": True, "model_tier": "light"})
    progress: list[str] = []
    with contextlib.ExitStack() as stack:
        for p in (
            mock.patch.object(repos, "ensure_worktree", return_value=clone),
            mock.patch.object(stages, "restore_main"),
            mock.patch.object(stages, "install_push_guard"),
            mock.patch.object(stages, "default_branch", return_value="main"),
            mock.patch.object(stages, "prepare_external_branch",
                              return_value=subprocess.CompletedProcess([], 0, "", "")),
            mock.patch.object(stages, "prepare_vault_branch", return_value=vp),
            mock.patch.object(stages, "remote_branch_exists", return_value=True),
            mock.patch.object(stages, "_VAULT_DATA_ROOT", root),
            mock.patch.object(stages, "RUNS_DIR", runs),
            mock.patch.object(stages, "record_related", return_value=None),
            mock.patch.object(stages, "build_stage_argv", return_value=["true"]),
            mock.patch.object(stages, "rescue_uncommitted",
                              return_value={"committed": False, "sha": None,
                                            "note": "nothing to rescue"}),
            mock.patch.object(stages, "checkpoint_push", return_value={}),
            mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")),
            mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
            mock.patch.object(StagedExecutor, "_acquire_checkout_lease", return_value=None),
            mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
            mock.patch.object(StagedExecutor, "_pipeline_for", return_value=("implement",)),
            mock.patch("todo_system.worker.runner_heartbeat_start"),
            mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run),
            mock.patch("subprocess.run", side_effect=fake_run),
            mock.patch("todo_system.usage_guard.stage_gate", side_effect=gate),
            mock.patch.object(runner_mod, "STAGE_GATE_POLL_S", 0.0),
        ):
            stack.enter_context(p)
        StagedExecutor().execute(msg, _agent(), progress=progress.append)
    first_stage = order.index("stage")
    assert order[:first_stage] == ["gate:held", "gate:go"], order
    assert any("pausing before stage implement" in p for p in progress), progress
