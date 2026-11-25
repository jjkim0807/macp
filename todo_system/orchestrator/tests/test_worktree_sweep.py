"""worktree_sweep: parked `.wt/<slug>` worktrees + gradle-registry dirs.

Real git on a tmp worker root laid out like production: a shared clone
`<root>/demo`, worktrees added `--detach` under `<root>/demo.wt/<slug>` and then
put on `worker/<slug>` (repos.ensure_worktree + branch prep). Every seam the
sweep reads is pointed at tmp here, on top of the suite-wide conftest redirect.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from todo_system.orchestrator import disk_guard, stages
from todo_system.orchestrator import worktree_sweep as ws

DAY = 86400
_REAL_PGREP_FREE = ws._pgrep_free
_GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *_GIT_ID, "-C", str(cwd), *args],
                       capture_output=True, text=True, check=True)
    return r.stdout


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).astimezone().replace(microsecond=0).isoformat()


class Env:
    def __init__(self, tmp: Path):
        self.root = tmp / "Worker"
        self.adir = tmp / "assignments"
        self.jdir = tmp / "jobs"
        self.reg = tmp / "gradle-registry"
        for d in (self.root, self.adir, self.reg):
            d.mkdir()
        self.clone = self.root / "demo"
        self.clone.mkdir()
        _git(self.clone, "init", "-q", "-b", "main")
        (self.clone / "README").write_text("x\n")
        _git(self.clone, "add", "README")
        _git(self.clone, "commit", "-q", "-m", "init")

    def wt(self, slug: str) -> Path:
        return self.root / "demo.wt" / slug

    def add(self, slug: str, state: str | None = "failed", updated: float | None = None,
            detach: bool = False, **row) -> Path:
        wt = self.wt(slug)
        _git(self.clone, "worktree", "add", "-q", "--detach", str(wt))
        if not detach:
            _git(wt, "checkout", "-q", "-B", f"worker/{slug}")
        reg = self.reg / slug / "8.x"
        reg.mkdir(parents=True)
        (reg / "registry.bin").write_text("r")
        if state is not None:
            self.row(slug, state, updated, **row)
        return wt

    def row(self, slug: str, state: str, updated: float | None = None, **extra) -> None:
        data = {"task_id": slug, "state": state,
                "updated_ts": _iso(time.time() if updated is None else updated),
                "payload": {"repo": "https://example.invalid/demo.git"}, **extra}
        (self.adir / f"{slug}.json").write_text(json.dumps(data))

    def listed(self) -> str:
        return _git(self.clone, "worktree", "list", "--porcelain")

    def branches(self) -> str:
        return _git(self.clone, "branch", "--list")


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path)
    monkeypatch.setattr(disk_guard, "WORKER_ROOT", e.root)
    monkeypatch.setattr(disk_guard, "ASSIGNMENTS_DIR", e.adir)
    monkeypatch.setattr(disk_guard, "JOBS_DIR", e.jdir)
    monkeypatch.setattr(ws, "REGISTRY_DIR", e.reg)
    monkeypatch.setattr(ws, "_pgrep_free", lambda p: True)
    monkeypatch.setattr(ws, "_proc_cwds", lambda: [])
    return e


def _past() -> float:
    return time.time() + 8 * DAY


def _done(lines, path) -> bool:
    return any(line.startswith(f"정리 완료: {path} ") for line in lines)


def test_failed_within_window_is_kept(env):
    wt = env.add("s1")
    out = ws.reclaim_parked_worktrees()
    assert out == []
    assert wt.is_dir() and str(wt) in env.listed()
    assert (env.reg / "s1").is_dir()


def test_recent_ledger_update_keeps_old_worktree(env):
    now = _past()
    wt = env.add("s1", updated=now - DAY)
    assert not _done(ws.reclaim_parked_worktrees(now=now), wt)
    assert wt.is_dir() and (env.reg / "s1").is_dir()


def test_failed_past_window_removes_worktree_and_registry(env):
    wt = env.add("s1")
    out = ws.reclaim_parked_worktrees(now=_past())
    assert [line for line in out if line.startswith("정리 완료")] == [
        f"정리 완료: {wt} (parked worktree+registry 제거, idle 8d)"]
    assert not wt.exists()
    assert str(wt) not in env.listed()
    assert not (env.reg / "s1").exists()
    assert "worker/s1" in env.branches()       # committed work survives on the ref


@pytest.mark.parametrize("state", ["dispatched", "accepted", "in_progress"])
def test_active_states_kept_regardless_of_window(env, state):
    wt = env.add("s1", state=state)
    out = ws.reclaim_parked_worktrees(now=_past())
    assert out == [f"정리 보류: {wt} (active {state})"]
    assert wt.is_dir() and (env.reg / "s1").is_dir()


def test_job_running_row_kept(env):
    wt = env.add("s1", state="failed", job_running=True)
    assert ws.reclaim_parked_worktrees(now=_past()) == [f"정리 보류: {wt} (job_running)"]
    assert wt.is_dir() and (env.reg / "s1").is_dir()


def test_unreadable_ledger_holds_everything(env):
    wt = env.add("s1")
    (env.adir / "zz-bad.json").write_text("{not json")
    out = ws.reclaim_parked_worktrees(now=_past())
    assert out == ["parked worktree 정리 전체 보류 (zz-bad.json: JSONDecodeError)"]
    assert wt.is_dir() and (env.reg / "s1").is_dir()


def test_missing_ledger_dir_holds_everything(env):
    wt = env.add("s1", state=None)
    env.adir.rmdir()
    out = ws.reclaim_parked_worktrees(now=_past())
    assert len(out) == 1 and out[0].startswith("parked worktree 정리 전체 보류 (no ledger dir")
    assert wt.is_dir()


def test_live_process_cwd_holds_with_positive_control(env, monkeypatch):
    held = env.add("held")
    free = env.add("free")
    monkeypatch.setattr(ws, "_proc_cwds", lambda: [(4242, str(held / "sub"))])
    out = ws.reclaim_parked_worktrees(now=_past())
    assert f"정리 보류: {held} (pid 4242 cwd)" in out
    assert held.is_dir() and (env.reg / "held").is_dir()
    assert _done(out, free) and not free.exists()


def test_pgrep_match_or_unknown_holds_with_positive_control(env, monkeypatch):
    matched = env.add("matched")
    unknown = env.add("unknown")
    free = env.add("free")
    monkeypatch.setattr(ws, "_pgrep_free", _REAL_PGREP_FREE)
    real_run = subprocess.run

    def fake_run(argv, *a, **k):
        if argv[0] == "pgrep":
            target = argv[-1]
            if target.endswith("/matched"):
                return subprocess.CompletedProcess(argv, 0, "123\n", "")
            if target.endswith("/unknown"):
                return subprocess.CompletedProcess(argv, 2, "", "boom")
            return subprocess.CompletedProcess(argv, 1, "", "")
        return real_run(argv, *a, **k)

    monkeypatch.setattr(ws, "_run", fake_run)
    out = ws.reclaim_parked_worktrees(now=_past())
    assert f"정리 보류: {matched} (in-use)" in out
    assert f"정리 보류: {unknown} (in-use)" in out
    assert matched.is_dir() and unknown.is_dir()
    assert _done(out, free) and not free.exists() and not (env.reg / "free").exists()


def test_long_job_cwd_holds(env):
    wt = env.add("s1")
    env.jdir.mkdir()
    (env.jdir / "s1.json").write_text(json.dumps({"slug": "s1", "cwd": str(wt)}))
    assert ws.reclaim_parked_worktrees(now=_past()) == [f"정리 보류: {wt} (long_job cwd)"]
    assert wt.is_dir()


def test_dirty_worktree_held_not_forced(env):
    wt = env.add("s1")
    (wt / "notes.txt").write_text("only copy")
    out = ws.reclaim_parked_worktrees(now=_past())
    assert out == [f"정리 보류: {wt} (remove 실패: dirty?)"]
    assert (wt / "notes.txt").read_text() == "only copy"
    assert (env.reg / "s1").is_dir()


def test_non_targets_untouched(env, monkeypatch):
    stash = env.root / "demo.wt" / ".s0-longjob-stash"
    locked = env.add("locked")
    _git(env.clone, "worktree", "lock", str(locked))
    mine = env.add("mine")
    stash.mkdir()
    (stash / "data.csv").write_text("d")
    stray = env.root / "demo.wt" / "stray"
    stray.mkdir()
    (stray / "f").write_text("f")
    monkeypatch.chdir(mine)
    out = ws.reclaim_parked_worktrees(now=_past())
    assert f"정리 제외: {locked} (locked)" in out
    assert f"정리 보류: {mine} (this process)" in out
    assert not any(line.startswith("정리 완료") for line in out)
    for p in (locked, mine, stash / "data.csv", stray / "f", env.clone / "README"):
        assert p.exists(), p


def test_detached_worktree_needs_referenced_head(env):
    reachable = env.add("reach", detach=True)
    lonely = env.add("lonely", detach=True)
    (lonely / "README").write_text("y\n")
    _git(lonely, "commit", "-q", "-am", "only here")
    out = ws.reclaim_parked_worktrees(now=_past())
    assert _done(out, reachable) and not reachable.exists()
    assert f"정리 보류: {lonely} (detached, unreferenced HEAD)" in out
    assert lonely.is_dir()


def test_remove_registry_containment(env, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("k")
    (env.reg / "link").symlink_to(outside)
    (env.reg / ".hidden").mkdir()
    (env.reg / "a").mkdir()
    (env.reg / "a" / "b").mkdir()
    (env.reg / "plainfile").write_text("p")
    for name in ("..", ".", "a/b", ".hidden", "", "link", "missing", "plainfile"):
        assert ws.remove_registry(name) is False, name
    assert (outside / "keep").exists() and (env.reg / "link").is_symlink()
    assert (env.reg / "a" / "b").is_dir() and (env.reg / ".hidden").is_dir()
    seen = []
    assert ws.remove_registry("a", progress=seen.append) is True
    assert not (env.reg / "a").exists()
    assert seen == [f"gradle-registry removed: {env.reg / 'a'}"]


def test_orphan_registry_swept_past_window_only(env):
    for name in ("old-slug", "demo", stages._VAULT_DATA_ROOT.name, "busy-slug"):
        (env.reg / name / "8.x").mkdir(parents=True)
    env.row("busy-slug", "in_progress")
    assert ws.reclaim_parked_worktrees() == []          # within window: silent, kept
    assert (env.reg / "old-slug").is_dir()
    out = ws.reclaim_parked_worktrees(now=_past())
    assert out == [
        f"정리 보류: {env.reg / 'busy-slug'} (active in_progress)",
        f"정리 완료: {env.reg / 'old-slug'} (orphan gradle-registry 제거, idle 8d)",
    ]
    assert not (env.reg / "old-slug").exists()
    for name in ("demo", stages._VAULT_DATA_ROOT.name, "busy-slug"):
        assert (env.reg / name).is_dir(), name


def test_registry_of_kept_worktree_is_not_an_orphan(env):
    env.add("s1", state="in_progress")
    ws.reclaim_parked_worktrees(now=_past())
    assert (env.reg / "s1").is_dir()


def test_fail_open(env, monkeypatch, tmp_path):
    wt = env.add("s1")

    def boom(*a, **k):
        raise OSError("no git")

    monkeypatch.setattr(ws, "_run", boom)
    assert ws.reclaim_parked_worktrees(now=_past()) == []
    assert wt.is_dir()
    afile = tmp_path / "rootfile"
    afile.write_text("x")
    monkeypatch.setattr(disk_guard, "WORKER_ROOT", afile)
    assert ws.reclaim_parked_worktrees(now=_past()) == []


def test_empty_root_is_silent(env, monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(disk_guard, "WORKER_ROOT", empty)
    assert ws.reclaim_parked_worktrees(now=_past()) == []


def test_per_tick_cap(env):
    wts = [env.add(f"s{i}") for i in range(7)]
    out = ws.reclaim_parked_worktrees(now=_past())
    assert sum(line.startswith("정리 완료") for line in out) == 5
    assert out[-1] == "parked worktree 정리 후보 2개 더 있음 (다음 틱)"
    assert sum(w.exists() for w in wts) == 2
    out2 = ws.reclaim_parked_worktrees(now=_past())
    assert sum(line.startswith("정리 완료") for line in out2) == 2
    assert not any(w.exists() for w in wts)


def test_budget_exhausted_is_declared(env, monkeypatch):
    env.add("s1")
    ticks = iter([0.0, ws.BUDGET_S + 1.0] + [ws.BUDGET_S + 2.0] * 10)
    monkeypatch.setattr(ws, "_monotonic", lambda: next(ticks))
    out = ws.reclaim_parked_worktrees(now=_past())
    assert out == [f"parked worktree 1개는 검사하지 않음 (시간 예산 {ws.BUDGET_S}초 초과)"]
    assert env.wt("s1").is_dir()


def test_registry_in_use_after_worktree_removal_is_kept(env, monkeypatch):
    wt = env.add("s1")
    monkeypatch.setattr(ws, "_pgrep_free", lambda p: not str(p).startswith(str(env.reg)))
    out = ws.reclaim_parked_worktrees(now=_past())
    assert out == [f"정리 완료: {wt} (parked worktree+registry 보류: in-use, idle 8d)"]
    assert not wt.exists() and (env.reg / "s1").is_dir()
    assert os.path.isdir(env.reg / "s1" / "8.x")
