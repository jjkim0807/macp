"""stages.sync_for_long_job against real git: a NEW long job must run on
origin's default-branch tip even when its tree is a detached per-run worktree
cut from a stale shared clone (2026-10-27: ai-stock-trading job ran on a
16-commits-behind tree and died "No such file")."""
from __future__ import annotations

import subprocess
from pathlib import Path

from todo_system.orchestrator import stages


def _g(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    origin = tmp_path / "origin.git"
    _g(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    _g(tmp_path, "clone", "-q", str(origin), str(seed))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        _g(seed, "config", k, v)
    (seed / "a.txt").write_text("1\n")
    _g(seed, "add", "a.txt")
    _g(seed, "commit", "-qm", "base")
    _g(seed, "push", "-q", "origin", "main")
    clone = tmp_path / "clone"
    _g(tmp_path, "clone", "-q", str(origin), str(clone))
    # origin moves on: the script the job needs lands later
    (seed / "collect.py").write_text("print('ok')\n")
    _g(seed, "add", "collect.py")
    _g(seed, "commit", "-qm", "add script")
    _g(seed, "push", "-q", "origin", "main")
    wt = tmp_path / "clone.wt" / "slug"
    _g(clone, "worktree", "add", "-q", "--detach", str(wt))
    return origin, clone, wt


def test_detached_stale_worktree_moves_to_origin_tip(tmp_path):
    _, _, wt = _setup(tmp_path)
    assert not (wt / "collect.py").exists()          # RED shape: stale tree
    note = stages.sync_for_long_job(wt)
    assert (wt / "collect.py").exists(), note
    assert "origin/main" in note


def test_old_ff_was_a_noop_on_detached_worktree(tmp_path):
    # Pins the bug: the previous call left the detached worktree stale.
    _, _, wt = _setup(tmp_path)
    assert stages.ff_default_branch(wt) == ""
    assert not (wt / "collect.py").exists()


def test_dirty_detached_worktree_is_left_alone(tmp_path):
    _, _, wt = _setup(tmp_path)
    (wt / "a.txt").write_text("edited\n")
    note = stages.sync_for_long_job(wt)
    assert "local changes" in note
    assert not (wt / "collect.py").exists()


def test_branch_checkout_keeps_old_behaviour(tmp_path):
    _, clone, _ = _setup(tmp_path)
    note = stages.sync_for_long_job(clone)            # clone is ON main, clean
    assert (clone / "collect.py").exists(), note
