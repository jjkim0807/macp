"""SolarAgentBackend contract + the runner's one vendor → (backend, account) selector.

Pins:
  1. ``SolarAgentBackend`` implements the ``AgentBackend`` protocol and is a
     transparent pass-through to ``solar_stage.run`` — the kwargs it receives are
     exactly the caller's (no added defaults, nothing dropped), and the result is
     returned as-is.
  2. Capabilities: headless + model_select only, so the capability gate passes on
     Solar's quick/delegate lane and refuses a read-only stage.
  3. ``classify_exit`` / ``report_usage`` (None, writes nothing — keeps the rc==0
     capacity arm inert, as the old inline ``usage_rec = None`` did).
  4. ``runner._select_backend`` truth table (routes_to_vendor + resolved account).
  5. End to end through ``StagedExecutor``: an ``agent: upstage`` delegate run calls
     ``solar_stage.run`` with exactly the 8 kwargs the pre-seam inline call passed
     and writes the same run.log block, with no stages.json usage row.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from todo_system.macp import config
from todo_system.macp import upstage as UP
from todo_system.orchestrator import agent_backend as ab
from todo_system.orchestrator import message as m
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator import solar_stage, stages
from todo_system.orchestrator.registry import Agent
from todo_system.orchestrator.runner import StagedExecutor

# The keyword set the runner's inline solar_stage.run call passed before the seam.
RUNNER_SOLAR_KWARGS = {"task_prompt", "working_dir", "account", "tier",
                       "artifact_path", "env", "deadline_s", "progress"}


# ---- 1. protocol + pass-through --------------------------------------------

def test_implements_agent_backend_protocol():
    b = ab.SolarAgentBackend()
    assert isinstance(b, ab.AgentBackend)
    assert b.name == "upstage"


def _recorder(monkeypatch):
    calls = []
    sentinel = subprocess.CompletedProcess(["solar", "delegate", "m"], 0, "out", "")

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(solar_stage, "run", fake_run)
    return calls, sentinel


def test_run_stage_passes_runner_kwargs_through_exactly(monkeypatch, tmp_path):
    calls, sentinel = _recorder(monkeypatch)
    progress = lambda _msg: None  # noqa: E731
    acct = object()
    kw = {"task_prompt": "Task: x", "working_dir": str(tmp_path), "account": acct,
          "tier": "light", "artifact_path": tmp_path / "delegate.md",
          "env": {"A": "1"}, "deadline_s": 1234, "progress": progress}
    r = ab.SolarAgentBackend().run_stage("delegate", **kw)
    assert r is sentinel
    assert len(calls) == 1
    args, got = calls[0]
    assert args == ("delegate",)
    assert got == kw                      # no timeout/api_key/... added
    assert set(got) == RUNNER_SOLAR_KWARGS
    assert got["account"] is acct and got["progress"] is progress


def test_run_stage_forwards_extra_kwargs(monkeypatch, tmp_path):
    calls, _ = _recorder(monkeypatch)
    ab.SolarAgentBackend().run_stage(
        "quick", task_prompt="p", working_dir=None,
        artifact_path=tmp_path / "quick.md", max_iters=3)
    args, got = calls[0]
    assert args == ("quick",)
    assert got == {"task_prompt": "p", "working_dir": None,
                   "artifact_path": tmp_path / "quick.md", "max_iters": 3}


def test_run_stage_looks_up_solar_stage_run_at_call_time(monkeypatch, tmp_path):
    # built BEFORE the patch — the patch must still intercept
    b = ab.SolarAgentBackend()
    calls, sentinel = _recorder(monkeypatch)
    assert b.run_stage("delegate", task_prompt="p", working_dir=None,
                       artifact_path=tmp_path / "d.md") is sentinel
    assert len(calls) == 1


# ---- 2. capabilities ----------------------------------------------------------

def test_capabilities():
    b = ab.SolarAgentBackend()
    assert b.supports("headless")
    assert b.supports("model_select")
    for cap in ("tool_allowlist", "turn_cap", "effort", "add_dir",
                "usage_report", "exit_quota"):
        assert not b.supports(cap), cap
    assert not b.supports("not-a-capability")


def test_capability_gate_passes_on_solar_lane_and_refuses_readonly():
    b = ab.SolarAgentBackend()
    for stage in stages.SOLAR_STAGES:      # the positive path — gate not vacuous
        assert ab.missing_capabilities(b, stage) == [], stage
    assert ab.missing_capabilities(b, "research") == ["tool_allowlist"]
    assert ab.missing_capabilities(b, "verify") == ["tool_allowlist"]


# ---- 3. classify_exit / report_usage ------------------------------------------

@pytest.mark.parametrize("rc, stdout, want", [
    (0, "", ab.EXIT_SUCCESS),
    (0, "--- metrics ---\nerror_kind: none", ab.EXIT_SUCCESS),
    (1, "--- metrics ---\nerror_kind: " + UP.KIND_RATE_LIMIT, ab.EXIT_EXHAUSTED),
    (1, "--- metrics ---\nerror_kind: " + UP.KIND_AUTH, ab.EXIT_ERROR),
    (1, "--- metrics ---\nerror_kind: deadline", ab.EXIT_ERROR),
    (1, "", ab.EXIT_ERROR),
    (1, None, ab.EXIT_ERROR),
])
def test_classify_exit(rc, stdout, want):
    assert ab.SolarAgentBackend().classify_exit(rc, stdout) == want


def test_report_usage_is_none_and_writes_nothing(tmp_path):
    b = ab.SolarAgentBackend()
    assert b.report_usage("delegate", tmp_path, ["solar", "delegate", "m"]) is None
    assert b.report_usage("quick", tmp_path, None, returncode=1, timed_out=True) is None
    assert list(tmp_path.iterdir()) == []
    # the runner's rc==0 capacity arm reads this — None keeps it inert
    assert stages.usage_shows_no_api_work(None) is False


# ---- 4. runner._select_backend ----------------------------------------------

SOLAR, GEMINI, CLAUDE = object(), object(), object()
S_ACCT, G_ACCT = object(), object()


@pytest.mark.parametrize("agent, stage, solar_acct, gemini_acct, want", [
    ("upstage", "delegate", S_ACCT, None, (SOLAR, S_ACCT)),
    ("upstage", "quick", S_ACCT, None, (SOLAR, S_ACCT)),
    ("UPSTAGE", "delegate", S_ACCT, None, (SOLAR, S_ACCT)),   # case-insensitive
    ("upstage", "delegate", None, None, (CLAUDE, None)),       # no account → claude
    ("upstage", "research", S_ACCT, None, (CLAUDE, None)),     # off-lane → claude
    ("upstage", "implement", S_ACCT, None, (CLAUDE, None)),
    ("gemini", "quick", None, G_ACCT, (GEMINI, G_ACCT)),
    ("gemini", "delegate", None, G_ACCT, (CLAUDE, None)),      # gemini lane = quick
    ("gemini", "quick", None, None, (CLAUDE, None)),
    ("gemini", "quick", S_ACCT, None, (CLAUDE, None)),         # wrong vendor's acct
    (None, "delegate", S_ACCT, G_ACCT, (CLAUDE, None)),
    ("", "quick", S_ACCT, G_ACCT, (CLAUDE, None)),
    ("claude", "quick", S_ACCT, G_ACCT, (CLAUDE, None)),
    ("unknown", "quick", S_ACCT, G_ACCT, (CLAUDE, None)),
])
def test_select_backend(agent, stage, solar_acct, gemini_acct, want):
    vendors = {"upstage": (SOLAR, solar_acct), "gemini": (GEMINI, gemini_acct)}
    got = runner_mod._select_backend(agent, stage, vendors, CLAUDE)
    assert got[0] is want[0] and got[1] is want[1]


# ---- 5. end to end through StagedExecutor -----------------------------------

def _drive(tmp_path, *, agent, solar_acct):
    """A full VAULT delegate run with everything but the stage stubbed (the same
    shape as test_solar_stage.ExecuteRoutingTest, copied — not shared — so that
    file stays untouched)."""
    runs = tmp_path / "runs"
    runs.mkdir()

    def run_dir() -> Path:
        dirs = list(runs.iterdir())
        assert len(dirs) == 1, dirs
        return dirs[0]

    vp = subprocess.CompletedProcess([], 0, "", "")
    vp.problem = None
    vp.orphaned_commits = None

    def write_art(*a, **kw):
        (run_dir() / stages.STAGE_ARTIFACT["delegate"]).write_text(
            "# delegate\n", encoding="utf-8")
        return subprocess.CompletedProcess(["solar", "delegate", "m"], 0,
                                           "stage stdout", "")

    solar_run = mock.Mock(side_effect=write_art)
    claude_run = mock.Mock(side_effect=write_art)
    payload = {"worker_mode": "delegate", "auto_merge": True, "summary": "do it"}
    if agent:
        payload["agent"] = agent
    msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="t", payload=payload)
    with contextlib.ExitStack() as stack:
        for p in (
            mock.patch.object(stages, "restore_main"),
            mock.patch.object(stages, "install_push_guard"),
            mock.patch.object(stages, "default_branch", return_value="main"),
            mock.patch.object(stages, "prepare_vault_branch", return_value=vp),
            mock.patch.object(stages, "remote_branch_exists", return_value=False),
            mock.patch.object(stages, "RUNS_DIR", runs),
            mock.patch.object(stages, "build_stage_argv", return_value=["true"]),
            mock.patch.object(stages, "cleanup_build_daemons",
                              return_value={"ran": True, "note": ""}),
            mock.patch.object(stages, "run_stage_subprocess", claude_run),
            mock.patch.object(solar_stage, "run", solar_run),
            mock.patch.object(stages, "publish_branch",
                              mock.Mock(return_value={"pr_url": None, "note": ""})),
            mock.patch.object(stages, "merge_pr",
                              mock.Mock(return_value={"merged": True, "note": ""})),
            mock.patch.object(StagedExecutor, "_account_env", return_value=(None, "")),
            mock.patch.object(StagedExecutor, "_solar_account", return_value=solar_acct),
            mock.patch.object(StagedExecutor, "_live_sibling_hold", return_value=None),
            mock.patch.object(StagedExecutor, "_acquire_checkout_lease", return_value=None),
            mock.patch.object(StagedExecutor, "_pipeline_for", return_value=("delegate",)),
            mock.patch.object(runner_mod.disk_guard, "ensure_free_space", return_value={}),
            mock.patch("todo_system.worker.runner_heartbeat_start"),
        ):
            stack.enter_context(p)
        ex = StagedExecutor()
        out = ex.execute(msg, Agent(id="w", kind="ai-local", capabilities=["git"],
                                    repo="~/Worker/x"),
                         progress=lambda _: None)
    return out, solar_run, claude_run, run_dir(), ex


def test_upstage_delegate_dispatches_the_same_8_kwargs(tmp_path):
    acct = config.Account(name="solar", agent="upstage")
    out, solar_run, claude_run, rd, ex = _drive(tmp_path, agent="upstage",
                                                solar_acct=acct)
    assert out.ok, out.summary
    claude_run.assert_not_called()
    solar_run.assert_called_once()
    args, kw = solar_run.call_args
    assert args == ("delegate",)
    assert set(kw) == RUNNER_SOLAR_KWARGS          # no timeout=, no api_key=
    assert kw["account"] is acct
    assert kw["task_prompt"] == runner_mod._solar_task_prompt(
        {"summary": "do it"})
    assert kw["artifact_path"] == rd / stages.STAGE_ARTIFACT["delegate"]
    assert kw["deadline_s"] >= ex.stage_timeout
    # plain stage_env: the claude-only browser/emulator owner (= this run's
    # run_id) is not layered on. The ambient env may carry an unrelated owner
    # (e.g. a worker stage running this suite), so compare against the run_id.
    run_id = rd.name
    for k in (stages.BROWSER_OWNER_ENV, stages.EMULATOR_OWNER_ENV):
        assert (kw["env"] or {}).get(k) != run_id, k
    log = (rd / "run.log").read_text(encoding="utf-8")
    assert "\n=== delegate (rc=0) ===\nstage stdout\n" in log
    sj = rd / "stages.json"
    if sj.exists():
        usage = json.loads(sj.read_text(encoding="utf-8")).get("usage") or {}
        assert "delegate" not in usage


def test_claude_delegate_unchanged(tmp_path):
    out, solar_run, claude_run, rd, _ = _drive(tmp_path, agent=None, solar_acct=None)
    assert out.ok, out.summary
    claude_run.assert_called_once()
    solar_run.assert_not_called()
    # positive control for the Solar env check above: the claude path DOES
    # layer the run_id owner onto a browser-tool stage's env
    assert "delegate" in stages.BROWSER_TOOL_STAGES
    assert claude_run.call_args.kwargs["env"][stages.BROWSER_OWNER_ENV] == rd.name
