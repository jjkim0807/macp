"""planner_tick.sh keep-alive bypass — the real script, fake tools.

A starved keep-alive project (keep_alive.py) bypasses exactly two gates — the
queue-depth skip and the exhausted backoff — and its block rides in the
planner prompt. With nothing starved the tick is unchanged: same skip lines,
prompt exactly ``/worker-planner``. The hard gates (usage guard, PAUSED, daily
budget) still exit first and never even ask the keep-alive check.

Harness shape copied from test_split_engine_root_13b_planner.py (installed
layout: no pyproject, a fake ``MACP_PYTHON``). The fake interpreter records its
argv and answers ``keep_alive queue-depth`` / ``emit-context`` from env, so
nothing is imported. Neutral prefixes only: this file sits in the tree the
public identifier gate scans.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from todo_system.orchestrator import keep_alive as ka

_REAL_ENGINE = Path(__file__).resolve().parents[2]
_PLANNER = "planner_tick.sh"

_FAKE_PY = f'''#!{sys.executable}
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["SEAM_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"tool": "py", "argv": argv}}) + "\\n")
mod = argv[argv.index("-m") + 1] if "-m" in argv else ""
if mod == "todo_system.usage_guard" and "check" in argv and os.environ.get("SEAM_USAGE_BLOCK") == "1":
    sys.exit(3)
if mod == "todo_system.orchestrator.keep_alive":
    if "queue-depth" in argv:
        sys.stdout.write(os.environ.get("SEAM_QUEUE_DEPTH", "0") + "\\n")
    elif "emit-context" in argv and os.environ.get("SEAM_KA_CTX_FILE"):
        with open(os.environ["SEAM_KA_CTX_FILE"], encoding="utf-8") as fh:
            sys.stdout.write(fh.read())
'''

_FAKE_CLAUDE = f'''#!{sys.executable}
import json, os, sys
with open(os.environ["SEAM_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"tool": "claude", "argv": sys.argv[1:]}}) + "\\n")
'''

_FAKE_SQLITE = "#!/bin/sh\nexit 0\n"

_CTX = ka.render_context([{"prefix": "proj-a-", "decision": "Decisions/proj-a.md"}])


def _write_exec(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


class _Tick:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.log = tmp / "calls.jsonl"
        self.bin = tmp / "bin"
        self.home = tmp / "home"
        self.home.mkdir()
        _write_exec(self.bin / "fakepy", _FAKE_PY)
        _write_exec(self.bin / "claude", _FAKE_CLAUDE)
        _write_exec(self.bin / "sqlite3", _FAKE_SQLITE)
        self.vault = tmp / "vault"
        (self.vault / ".macp").mkdir(parents=True)
        (self.vault / ".macp" / "config.toml").write_text("", encoding="utf-8")
        scripts = tmp / "site" / "todo_system" / "_engine" / "scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(_REAL_ENGINE / "scripts" / _PLANNER, scripts / _PLANNER)
        self.script = scripts / _PLANNER
        self.ctx_file = tmp / "ka_ctx.txt"
        self.ctx_file.write_text(_CTX, encoding="utf-8")

    def run(self, *, depth: int = 0, starved: bool = False, **env: str) -> subprocess.CompletedProcess:
        full = {
            "PATH": f"{self.bin}{os.pathsep}/usr/bin:/bin",
            "HOME": str(self.home),
            "SEAM_TEST_LOG": str(self.log),
            "GIT_CEILING_DIRECTORIES": str(self.tmp),
            "PYTHONUTF8": "1",
            "TZ": "UTC",
            "MACP_VAULT_ROOT": str(self.vault),
            "MACP_PYTHON": str(self.bin / "fakepy"),
            "SEAM_QUEUE_DEPTH": str(depth),
            **env,
        }
        if starved:
            full["SEAM_KA_CTX_FILE"] = str(self.ctx_file)
        return subprocess.run(["bash", str(self.script)], env=full, capture_output=True,
                              text=True, errors="replace", timeout=60)

    def calls(self, tool: str) -> list[dict]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()
                if line.strip()]
        return [r for r in rows if r["tool"] == tool]

    def prompts(self) -> list[str]:
        return [c["argv"][c["argv"].index("-p") + 1] for c in self.calls("claude")]

    def asked_keep_alive(self) -> bool:
        return any("todo_system.orchestrator.keep_alive" in c["argv"] and "emit-context" in c["argv"]
                   for c in self.calls("py"))


@pytest.fixture
def tick(tmp_path) -> _Tick:
    return _Tick(tmp_path)


def test_depth_limit_without_starved_still_skips(tick):
    r = tick.run(depth=5)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "[planner-tick] actionable auto-queue 5 >= 5; skip" in r.stdout, r.stdout
    assert "keep-alive" not in r.stdout, r.stdout
    assert tick.calls("claude") == []
    assert tick.asked_keep_alive()


def test_depth_limit_with_starved_plans_with_the_block(tick):
    r = tick.run(depth=5, starved=True)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "; skip" not in r.stdout, r.stdout
    assert "[planner-tick] keep-alive starved: 1 project(s)" in r.stdout, r.stdout
    assert ("[planner-tick] actionable auto-queue 5 at the depth limit, but keep-alive "
            "project(s) starved; planning anyway") in r.stdout, r.stdout
    assert "[planner-tick] mode=normal (" in r.stdout, r.stdout
    assert "[planner-tick] injecting 1 keep-alive starved project(s) into planner context" in r.stdout
    assert tick.prompts() == ["/worker-planner\n\n" + _CTX.rstrip("\n")]
    assert "- [proj-a-] decision: Decisions/proj-a.md" in tick.prompts()[0]
    assert "[planner-tick] done" in r.stdout, r.stdout


def test_nothing_starved_keeps_the_prompt_byte_identical(tick):
    r = tick.run(depth=0)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert tick.prompts() == ["/worker-planner"]
    assert "[planner-tick] auto-queue=0 (< 5); invoking planner agent…" in r.stdout, r.stdout
    assert "keep-alive" not in r.stdout, r.stdout


def test_garbage_queue_depth_counts_as_zero(tick):
    r = tick.run(SEAM_QUEUE_DEPTH="oops")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "[planner-tick] auto-queue=0 (< 5); invoking planner agent…" in r.stdout, r.stdout
    assert len(tick.calls("claude")) == 1


def test_starved_ignores_the_exhausted_backoff(tick):
    first = tick.run()
    assert first.returncode == 0 and len(tick.calls("claude")) == 1, first.stdout
    assert (tick.vault / "Worker" / "_state" / "planner_tick_state.json").is_file()

    again = tick.run()
    assert "skip (exhausted)" in again.stdout, again.stdout
    assert len(tick.calls("claude")) == 1

    starved = tick.run(starved=True)
    assert starved.returncode == 0, (starved.stdout, starved.stderr)
    assert "skip (exhausted)" not in starved.stdout, starved.stdout
    assert ("[planner-tick] open-task state unchanged, but keep-alive project(s) starved; "
            "ignoring the 24h backoff") in starved.stdout, starved.stdout
    assert "[planner-tick] auto-queue=0; keep-alive starved — invoking planner agent…" in starved.stdout
    assert len(tick.calls("claude")) == 2


def _pause(tick: _Tick) -> dict:
    p = tick.vault / "Worker" / "_state" / "PAUSED"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")
    return {}


def _budget(tick: _Tick) -> dict:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    (tick.vault / "Worker" / "runs" / f"{today}T000000-x").mkdir(parents=True)
    return {"MAESTRO_DAILY_RUN_BUDGET": "1"}


@pytest.mark.parametrize("setup,line", [
    (lambda t: {"SEAM_USAGE_BLOCK": "1"}, "[planner-tick] usage guard blocked; skip planning"),
    (_pause, "[planner-tick] control plane PAUSED"),
    (_budget, "[planner-tick] daily budget reached"),
], ids=["usage-guard", "paused", "daily-budget"])
def test_hard_gates_win_over_a_starved_project(tick, setup, line):
    env = setup(tick)
    r = tick.run(depth=5, starved=True, **env)
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert line in r.stdout, r.stdout
    assert tick.calls("claude") == []
    assert not tick.asked_keep_alive()           # ordering: never even consulted
