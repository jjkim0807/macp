"""``ClaudeAgentBackend`` unit tests (agent-backends leaf ②).

The byte-identical guarantee the refactor rests on, stated directly: the argv
``ClaudeAgentBackend.run_stage`` runs is the SAME argv ``stages.build_stage_argv``
produces (session-id uuid aside, which is random per call). Plus: the subprocess
sees the cwd/env/timeout it was handed, a timeout propagates with the argv on
``.cmd``, ``supports`` is all eight capabilities, ``classify_exit`` is the
3-way, and ``report_usage`` is a pass-through to ``stages.record_stage_usage``.

No ``claude`` binary runs — ``stages.run_stage_subprocess`` /
``record_stage_usage`` are patched. No personal identifier appears here (the
engine tree is scanned by the public-identifier gate).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from todo_system.orchestrator import agent_backend as ab
from todo_system.orchestrator import stages

_MODEL = "MODEL-SENTINEL"
_EFFORT = "EFFORT-SENTINEL"


@pytest.fixture
def fixed(monkeypatch, tmp_path):
    """Pin the environment-dependent inputs of build_stage_argv (model/effort,
    no browser mcp, usage on) and give a worker_root that exists."""
    monkeypatch.setattr(stages, "stage_model", lambda stage="implement", tier=None: _MODEL)
    monkeypatch.setattr(stages, "stage_effort", lambda stage="implement", tier=None: _EFFORT)
    monkeypatch.setattr(stages, "browser_mcp_server", lambda stage: None)
    monkeypatch.setenv(stages.USAGE_ENV, "1")
    wr = tmp_path / "worker_root"
    wr.mkdir()
    return wr


def _norm_sid(argv: list[str]) -> list[str]:
    out = list(argv)
    for i, tok in enumerate(out):
        if tok == stages.SESSION_ID_FLAG and i + 1 < len(out):
            out[i + 1] = "<SID>"
    return out


_KW = dict(task_id="slug", run_id="RID", run_dir=Path("/rd"), branch="worker/slug")


@pytest.mark.parametrize("stage", ["research", "implement", "verify", "quick", "delegate"])
def test_run_stage_argv_is_byte_identical_to_build_stage_argv(fixed, monkeypatch, stage):
    captured = {}

    def capture(argv, **kw):
        captured["argv"] = argv
        captured["kw"] = kw
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(stages, "run_stage_subprocess", capture)
    r = ab.ClaudeAgentBackend().run_stage(
        stage, worker_root=fixed, cwd="/trusted", env={"X": "1"},
        timeout=123, progress=None, **_KW)
    golden = stages.build_stage_argv(stage, worker_root=fixed, **_KW)
    # The argv the backend ran == the argv build_stage_argv builds, modulo the
    # per-call session-id uuid.
    assert _norm_sid(captured["argv"]) == _norm_sid(golden)
    # And it is handed back unchanged on the CompletedProcess (the usage handle).
    assert r.args is captured["argv"]
    assert r.returncode == 0 and r.stdout == "ok"


def test_run_stage_forwards_cwd_env_timeout_progress(fixed, monkeypatch):
    captured = {}

    def capture(argv, **kw):
        captured.update(kw)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(stages, "run_stage_subprocess", capture)

    def sentinel_progress(s):
        return None

    ab.ClaudeAgentBackend().run_stage(
        "implement", worker_root=fixed, cwd="/trusted/cwd",
        env={"CLAUDE_CONFIG_DIR": "/x"}, timeout=456, progress=sentinel_progress, **_KW)
    assert captured["cwd"] == "/trusted/cwd"
    assert captured["env"] == {"CLAUDE_CONFIG_DIR": "/x"}
    assert captured["timeout"] == 456
    assert captured["progress"] is sentinel_progress


def test_run_stage_ignores_account_for_byte_identity(fixed, monkeypatch):
    seen = []
    monkeypatch.setattr(stages, "run_stage_subprocess",
                        lambda argv, **kw: seen.append(argv) or
                        subprocess.CompletedProcess(argv, 0, "", ""))
    b = ab.ClaudeAgentBackend()
    b.run_stage("implement", worker_root=fixed, account=None, **_KW)
    b.run_stage("implement", worker_root=fixed, account=object(), **_KW)
    # account changes nothing — claude's env is in `env`, not derived here.
    assert _norm_sid(seen[0]) == _norm_sid(seen[1])


def test_run_stage_propagates_timeout_with_argv_on_cmd(fixed, monkeypatch):
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout"),
                                        output="partial", stderr="")

    monkeypatch.setattr(stages, "run_stage_subprocess", boom)
    with pytest.raises(subprocess.TimeoutExpired) as ei:
        ab.ClaudeAgentBackend().run_stage("implement", worker_root=fixed,
                                          timeout=99, **_KW)
    # .cmd carries the argv so the runner's timeout arm can record usage off it.
    assert ei.value.cmd[0] == "claude"
    assert stages.SESSION_ID_FLAG in ei.value.cmd


def test_supports_is_all_eight_and_rejects_unknown():
    b = ab.ClaudeAgentBackend()
    assert all(b.supports(c) for c in ab.CAPABILITIES)
    assert len(ab.CAPABILITIES) == 8
    assert not b.supports("telepathy")
    assert not b.supports("")


@pytest.mark.parametrize("rc,stdout,expected", [
    (0, "finished cleanly", ab.EXIT_SUCCESS),
    (0, "You've reached your limit. Run /usage-credits", ab.EXIT_EXHAUSTED),
    (0, "rate limit exceeded", ab.EXIT_EXHAUSTED),
    (1, "traceback: boom", ab.EXIT_ERROR),
    (2, "please run /login", ab.EXIT_EXHAUSTED),
    (1, "", ab.EXIT_ERROR),
])
def test_classify_exit_three_way(rc, stdout, expected):
    assert ab.ClaudeAgentBackend().classify_exit(rc, stdout) == expected


def test_report_usage_delegates_to_record_stage_usage(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(stages, "record_stage_usage",
                        lambda run_dir, stage, argv, **kw: calls.append((run_dir, stage, argv, kw))
                        or {"input_tokens": 5})
    handle = ["claude", "-p", "x", stages.SESSION_ID_FLAG, "u"]
    out = ab.ClaudeAgentBackend().report_usage(
        "implement", tmp_path, handle, env={"E": "1"}, returncode=0, duration_ms=42)
    assert out == {"input_tokens": 5}
    (run_dir, stage, argv, kw) = calls[0]
    assert run_dir == tmp_path and stage == "implement" and argv is handle
    assert kw == {"env": {"E": "1"}, "returncode": 0, "duration_ms": 42, "timed_out": False}


def test_report_usage_timeout_path(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(stages, "record_stage_usage",
                        lambda run_dir, stage, argv, **kw: calls.append(kw) or None)
    out = ab.ClaudeAgentBackend().report_usage("implement", tmp_path, ["a"], timed_out=True)
    assert out is None
    assert calls[0]["timed_out"] is True


def test_report_usage_returns_none_for_stub_argv_without_session_id(tmp_path):
    # the real record_stage_usage: a stub argv has no --session-id, so nothing is
    # recorded and None comes back (the Solar-shaped inert case).
    assert ab.ClaudeAgentBackend().report_usage("implement", tmp_path, ["true"]) is None


def test_backend_satisfies_the_protocol():
    assert isinstance(ab.ClaudeAgentBackend(), ab.AgentBackend)


# ---- GeminiAgentBackend: usage wiring + meta blocks ----

_GEM_JSON_HANDLE = ["gemini", "--yolo", "-p", "PROMPT", "--output-format", "json"]


def _gem_doc():
    return {
        "session_id": "s-1",
        "stats": {"models": {
            "gemini-2.5-pro": {
                "api": {"totalRequests": 2, "totalErrors": 0, "totalLatencyMs": 10},
                "tokens": {"prompt": 1000, "candidates": 50, "total": 1470,
                           "cached": 400, "thoughts": 20, "tool": 0}},
            "gemini-2.5-flash": {
                "api": {"totalRequests": 1, "totalErrors": 0, "totalLatencyMs": 5},
                "tokens": {"prompt": 100, "candidates": 10, "total": 110,
                           "cached": 0, "thoughts": 0, "tool": 0}},
        }},
    }


def _write_sidecar(run_dir, stage, doc):
    import json
    ab.gemini_stats_path(run_dir, stage).write_text(json.dumps(doc), encoding="utf-8")


def test_gemini_stats_usage_maps_documented_shape():
    rec = ab.gemini_stats_usage(_gem_doc(), duration_ms=99)
    assert rec == {"input_tokens": 700, "output_tokens": 80,
                   "cache_creation_input_tokens": 0, "cache_read_input_tokens": 400,
                   "num_turns": 3, "duration_ms": 99, "model": "gemini-2.5-pro",
                   "session_id": "s-1", "total_cost_usd": None}
    assert list(rec) == list(stages.USAGE_KEYS)


def test_gemini_stats_usage_prefers_explicit_input_field():
    doc = {"stats": {"models": {"m": {"tokens": {"input": 7, "prompt": 1000,
                                                 "cached": 10, "candidates": 3}}}}}
    rec = ab.gemini_stats_usage(doc)
    assert rec["input_tokens"] == 7
    assert rec["output_tokens"] == 3 and rec["cache_read_input_tokens"] == 10
    assert rec["num_turns"] is None and rec["session_id"] is None
    assert rec["model"] == "m"


@pytest.mark.parametrize("doc", [
    {}, {"stats": None}, {"stats": {"models": {}}}, {"stats": {"models": {"m": "x"}}},
    "str", None,
])
def test_gemini_stats_usage_none_on_malformed(doc):
    assert ab.gemini_stats_usage(doc) is None


def test_gemini_report_usage_reads_sidecar_and_records(monkeypatch, tmp_path):
    import json
    monkeypatch.setenv(stages.USAGE_ENV, "1")
    _write_sidecar(tmp_path, "quick", _gem_doc())
    rec = ab.GeminiAgentBackend().report_usage(
        "quick", tmp_path, _GEM_JSON_HANDLE, returncode=0, duration_ms=1234)
    assert "unavailable" not in rec
    assert list(rec) == list(stages.USAGE_KEYS)
    assert rec["duration_ms"] == 1234 and rec["input_tokens"] == 700
    saved = json.loads((tmp_path / stages.STAGE_PROGRESS).read_text(encoding="utf-8"))
    assert saved["usage"]["quick"] == rec


def test_gemini_report_usage_timeout_records_unavailable(monkeypatch, tmp_path):
    import json
    monkeypatch.setenv(stages.USAGE_ENV, "1")
    _write_sidecar(tmp_path, "quick", _gem_doc())   # never credited on a timeout
    rec = ab.GeminiAgentBackend().report_usage(
        "quick", tmp_path, _GEM_JSON_HANDLE, timed_out=True)
    assert rec == {"unavailable": "timeout"}
    saved = json.loads((tmp_path / stages.STAGE_PROGRESS).read_text(encoding="utf-8"))
    assert saved["usage"]["quick"] == {"unavailable": "timeout"}


def test_gemini_report_usage_missing_or_bad_sidecar_is_not_a_quota_signature(
        monkeypatch, tmp_path):
    monkeypatch.setenv(stages.USAGE_ENV, "1")
    g = ab.GeminiAgentBackend()
    missing = g.report_usage("quick", tmp_path, _GEM_JSON_HANDLE, returncode=1)
    assert missing == {"unavailable": "gemini stats not captured (rc=1)"}
    ab.gemini_stats_path(tmp_path, "quick").write_text("not json{", encoding="utf-8")
    bad = g.report_usage("quick", tmp_path, _GEM_JSON_HANDLE, returncode=0)
    assert bad == {"unavailable": "gemini stats unparseable"}
    assert stages.usage_shows_no_api_work(missing) is False
    assert stages.usage_shows_no_api_work(bad) is False


def test_gemini_report_usage_kill_switch_returns_none_and_writes_nothing(
        monkeypatch, tmp_path):
    monkeypatch.setenv(stages.USAGE_ENV, "0")
    _write_sidecar(tmp_path, "quick", _gem_doc())
    assert ab.GeminiAgentBackend().report_usage(
        "quick", tmp_path, _GEM_JSON_HANDLE, returncode=0) is None
    assert not (tmp_path / stages.STAGE_PROGRESS).exists()


def test_gemini_report_usage_explicit_text_format_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv(stages.USAGE_ENV, "1")
    handle = ["gemini", "--output-format", "text", "-p", "x"]
    assert ab.GeminiAgentBackend().report_usage("quick", tmp_path, handle) is None
    assert not (tmp_path / stages.STAGE_PROGRESS).exists()


def test_gemini_report_usage_never_raises(monkeypatch, tmp_path):
    monkeypatch.setenv(stages.USAGE_ENV, "1")
    _write_sidecar(tmp_path, "quick", _gem_doc())

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(ab, "gemini_stats_usage", boom)
    rec = ab.GeminiAgentBackend().report_usage("quick", tmp_path, _GEM_JSON_HANDLE)
    assert rec == {"unavailable": "usage reader error: RuntimeError"}
    # a nonexistent run_dir never raises either
    rec2 = ab.GeminiAgentBackend().report_usage("quick", Path("/rd"), _GEM_JSON_HANDLE)
    assert rec2 == {"unavailable": "gemini stats not captured"}
