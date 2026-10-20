"""Unit tests for the beta-publish catch-up sweep.

The merge-time publisher only fires while a worker auto-merge is happening, so a
main that landed any other way (config entry temporarily missing, a build that
raised, a human merge) stayed unpublished forever — DanceMirror `35cdd60`,
2026-09-25. The sweep re-discovers those and publishes them on a later tick.

⚠ This package's conftest has NO "touched the real vault → fail the run" safety
net (the one in `todo_system/tests/conftest.py` does). A single default-argument
call of `sweep_unpublished()` / `maybe_publish()` here would run a real gradle
build and push a real beta to testers' phones. So every path argument
(config/watermark/lock/state/worktree/worker-root) is injected under `tmp_path`
and the publisher itself is always a spy — never the real `maybe_publish`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from todo_system.orchestrator import beta_publish as bp
from todo_system.orchestrator.assignment import Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import Orchestrator
from todo_system.orchestrator.registry import Registry

REPO = "DanceMirror"
REPO2 = "gutenberg-reader"
REPO3 = "KnowScroll"
SHA1 = "a" * 40
SHA2 = "b" * 40
NOW = 1_700_000_000.0


def _cfg_entry(project: str = "dancemirror-app") -> dict:
    return {
        "app_id": "1:1:android:deadbeef",
        "project": project,
        "group": "beta-testers",
        "build_cmd": ["./gradlew", ":app:assembleBeta"],
        "apk_path": "app/build/outputs/apk/beta/app-beta.apk",
        "migration_globs": ["*Database*.kt"],
    }


class FakeRun:
    """Records every argv; git succeeds unless a per-verb rc is configured.

    Signature matches beta_publish's default runner: (argv, *, cwd, env, timeout).
    """

    def __init__(self, *, rc_for: dict[str, int] | None = None, log_out: str = ""):
        self.calls: list[dict] = []
        self.rc_for = rc_for or {}
        self.log_out = log_out

    def __call__(self, argv, *, cwd=None, env=None, timeout=None):
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
        for verb, rc in self.rc_for.items():
            if verb in argv:
                return subprocess.CompletedProcess(argv, rc, "", "boom")
        if "log" in argv:
            return subprocess.CompletedProcess(argv, 0, self.log_out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def has(self, *needles: str) -> bool:
        return any(all(n in c["argv"] for n in needles) for c in self.calls)

    def count(self, *needles: str) -> int:
        return sum(1 for c in self.calls if all(n in c["argv"] for n in needles))


class PublishSpy:
    """Stands in for `maybe_publish`. NEVER the real one — see module docstring."""

    def __init__(self, *, error: Exception | None = None):
        self.calls: list[dict] = []
        self.error = error

    def __call__(self, repo_name, repo_dir, sha, payload, **kw):
        self.calls.append({"repo": repo_name, "dir": Path(repo_dir), "sha": sha,
                           "payload": payload, "kw": kw})
        if self.error is not None:
            raise self.error
        return f"베타 발행 · {repo_name} · main {sha[:9]}"


@pytest.fixture
def env(tmp_path):
    """config + watermarks + checkouts + state, every one of them under tmp_path."""
    cfg = tmp_path / "beta_publish_config.json"
    cfg.write_text(json.dumps({"repos": {
        REPO: _cfg_entry(),
        REPO2: _cfg_entry("gutenberg-app"),
        REPO3: _cfg_entry("knowscroll-app"),
    }}), encoding="utf-8")
    wm = tmp_path / "beta_watermarks.json"
    wm.write_text(json.dumps({"repos": {
        REPO: {"version_code": 12, "last_published_sha": None, "published_shas": []},
        REPO2: {"version_code": 3, "last_published_sha": None, "published_shas": []},
        REPO3: {"version_code": 7, "last_published_sha": None, "published_shas": []},
    }}), encoding="utf-8")
    worker_root = tmp_path / "Worker"
    for name in (REPO, REPO2, REPO3):
        (worker_root / name / ".git").mkdir(parents=True)

    class E:
        config_path = cfg
        watermark_path = wm
        lock_dir = tmp_path / "locks"
        state_path = tmp_path / "state" / "beta_sweep.json"
        worktree_root = tmp_path / "state" / "beta_worktrees"
        root = worker_root

        @staticmethod
        def set_watermark(repo: str, **fields):
            data = json.loads(wm.read_text(encoding="utf-8"))
            data["repos"][repo].update(fields)
            wm.write_text(json.dumps(data), encoding="utf-8")

        @staticmethod
        def state() -> dict:
            return json.loads(E.state_path.read_text(encoding="utf-8"))

    return E()


def _sweep(env, *, ls_remote, publish, run=None, notify=None, progress=None,
           now=NOW, max_publishes=1):
    return bp.sweep_unpublished(
        config_path=env.config_path, watermark_path=env.watermark_path,
        lock_dir=env.lock_dir, state_path=env.state_path,
        worktree_root=env.worktree_root, worker_root=env.root,
        ls_remote=ls_remote, publish=publish, run=run or FakeRun(),
        notify=notify or (lambda _m: None), progress=progress or (lambda _m: None),
        now=now, max_publishes=max_publishes)


def _only(repo: str, sha: str | None = SHA1):
    """An ls_remote that answers for one repo and returns None for everything else."""
    return lambda name: sha if name == repo else None


# --- the incident: configured repo, main never published --------------------

def test_unpublished_main_is_published_once(env):
    """DanceMirror shape — config O, watermark O, origin/main is a sha nobody
    published. One publish, in an isolated worktree, with tmp_path canon paths."""
    spy = PublishSpy()
    notes = _sweep(env, ls_remote=_only(REPO), publish=spy)

    assert len(spy.calls) == 1
    call = spy.calls[0]
    assert call["repo"] == REPO
    assert call["sha"] == SHA1
    assert call["dir"].parent == env.worktree_root      # isolated worktree, not the checkout
    assert SHA1[:9] in call["dir"].name
    # The canon the spy would have written is tmp_path's, never the real vault.
    assert call["kw"]["config_path"] == env.config_path
    assert call["kw"]["watermark_path"] == env.watermark_path
    assert call["kw"]["cumulative_notes"] is True
    assert len(notes) == 1 and REPO in notes[0]


def test_publish_runs_in_a_worktree_that_is_always_cleaned_up(env):
    """`worktree add` before the publish, `worktree remove` after it — and the
    remove happens even when the publish raises."""
    run = FakeRun()
    spy = PublishSpy(error=bp.BetaPublishError("build failed"))
    _sweep(env, ls_remote=_only(REPO), publish=spy, run=run)

    assert run.has("worktree", "add")
    assert run.has("worktree", "remove")
    assert run.has("fetch")                     # the sha must exist locally first


# --- the three "publishes nothing" gates (acceptance criterion) -------------

def test_already_published_sha_publishes_nothing(env):
    env.set_watermark(REPO, last_published_sha=SHA1, published_shas=[SHA1])
    spy = PublishSpy()
    assert _sweep(env, ls_remote=_only(REPO), publish=spy) == []
    assert spy.calls == []


def test_repo_without_config_entry_publishes_nothing(env, tmp_path):
    """A watermark-only repo (the config entry vanished) is NOT a sweep target —
    the config stays the single gate on what is publishable."""
    cfg = tmp_path / "empty_config.json"
    cfg.write_text(json.dumps({"repos": {}}), encoding="utf-8")
    env.config_path = cfg
    spy = PublishSpy()
    assert _sweep(env, ls_remote=_only(REPO), publish=spy) == []
    assert spy.calls == []


def test_ls_remote_failure_publishes_nothing(env):
    """No answer for origin/main (network down, rc≠0) → skip, never guess."""
    spy = PublishSpy()
    assert _sweep(env, ls_remote=lambda _n: None, publish=spy) == []
    assert spy.calls == []


def test_missing_checkout_is_skipped_with_a_reason(env, tmp_path):
    """The sweep never clones inside a tick; no checkout → no build host → skip."""
    env.root = tmp_path / "no-such-worker-root"
    logged: list[str] = []
    spy = PublishSpy()
    assert _sweep(env, ls_remote=_only(REPO), publish=spy, progress=logged.append) == []
    assert spy.calls == []
    assert any(REPO in line for line in logged)


# --- failure backoff + one alert per sha ------------------------------------

def test_failed_publish_backs_off_for_the_same_sha(env):
    alerts: list[str] = []
    spy = PublishSpy(error=bp.BetaPublishError("gradle rc=1"))
    _sweep(env, ls_remote=_only(REPO), publish=spy, notify=alerts.append)
    assert len(spy.calls) == 1                   # failure did not propagate
    assert len(alerts) == 1
    assert env.state()["repos"][REPO]["sha"] == SHA1

    # Same sha, inside the window → no second gradle build and no repeat alert.
    _sweep(env, ls_remote=_only(REPO), publish=spy, notify=alerts.append,
           now=NOW + 60)
    assert len(spy.calls) == 1
    assert len(alerts) == 1

    # Past the window → retried; still only the one alert for this sha.
    _sweep(env, ls_remote=_only(REPO), publish=spy, notify=alerts.append,
           now=NOW + bp.SWEEP_FAIL_BACKOFF_S + 1)
    assert len(spy.calls) == 2
    assert len(alerts) == 1


def test_a_new_sha_retries_immediately(env):
    """The backoff is per-sha: a fresh main is a fresh attempt, not a wait."""
    spy = PublishSpy(error=bp.BetaPublishError("gradle rc=1"))
    _sweep(env, ls_remote=_only(REPO), publish=spy)
    _sweep(env, ls_remote=_only(REPO, SHA2), publish=spy, now=NOW + 60)
    assert [c["sha"] for c in spy.calls] == [SHA1, SHA2]


def test_success_clears_the_failure_state(env):
    failing = PublishSpy(error=bp.BetaPublishError("gradle rc=1"))
    _sweep(env, ls_remote=_only(REPO), publish=failing)
    ok = PublishSpy()
    _sweep(env, ls_remote=_only(REPO), publish=ok,
           now=NOW + bp.SWEEP_FAIL_BACKOFF_S + 1)
    assert len(ok.calls) == 1
    assert REPO not in env.state().get("repos", {})


# --- per-tick cap -----------------------------------------------------------

def test_max_publishes_caps_one_repo_per_sweep(env):
    """Three repos are unpublished the day this lands; they must not all build
    at once. Config order decides who goes first; the rest wait for a later sweep."""
    logged: list[str] = []
    spy = PublishSpy()
    notes = _sweep(env, ls_remote=lambda _n: SHA1, publish=spy, progress=logged.append)
    assert [c["repo"] for c in spy.calls] == [REPO]
    assert len(notes) == 1
    assert any(REPO2 in line for line in logged)   # deferred, and said so


def test_max_publishes_two_takes_the_first_two_in_config_order(env):
    spy = PublishSpy()
    _sweep(env, ls_remote=lambda _n: SHA1, publish=spy, max_publishes=2)
    assert [c["repo"] for c in spy.calls] == [REPO, REPO2]


def test_one_repos_failure_does_not_block_the_next(env):
    """A raising publish is contained to its repo — the sweep keeps going."""
    class Flaky(PublishSpy):
        def __call__(self, repo_name, repo_dir, sha, payload, **kw):
            if repo_name == REPO:
                self.calls.append({"repo": repo_name, "dir": Path(repo_dir),
                                   "sha": sha, "payload": payload, "kw": kw})
                raise bp.BetaPublishError("gradle rc=1")
            return super().__call__(repo_name, repo_dir, sha, payload, **kw)

    spy = Flaky()
    _sweep(env, ls_remote=lambda _n: SHA1, publish=spy, max_publishes=2)
    assert [c["repo"] for c in spy.calls] == [REPO, REPO2]


# --- find_unpublished (pure) ------------------------------------------------

def test_find_unpublished_keeps_config_order_and_carries_the_base(env):
    config = bp.load_config(env.config_path)
    env.set_watermark(REPO, last_published_sha=SHA2, published_shas=[SHA2])
    wm = json.loads(env.watermark_path.read_text(encoding="utf-8"))
    cands = bp.find_unpublished(config, wm, ls_remote=lambda _n: SHA1)
    assert [c["repo"] for c in cands] == [REPO, REPO2, REPO3]
    assert cands[0]["base"] == SHA2              # cumulative notes range starts here
    assert cands[1]["base"] is None


def test_find_unpublished_skips_a_repo_without_a_watermark_entry(env):
    config = bp.load_config(env.config_path)
    cands = bp.find_unpublished(config, {"repos": {}}, ls_remote=lambda _n: SHA1)
    assert cands == []


# --- cumulative release notes ----------------------------------------------

def test_detect_migration_argv_is_unchanged_without_diff_base(env):
    """The merge-time caller passes no diff_base — its git argv must not move."""
    run = FakeRun()
    bp.detect_migration(env.root / REPO, SHA1, ["*Database*.kt"], run=run)
    assert run.calls[0]["argv"][:3] == ["git", "diff", f"{SHA1}^..{SHA1}"]


def test_detect_migration_uses_the_cumulative_range_when_given_a_base(env):
    run = FakeRun()
    bp.detect_migration(env.root / REPO, SHA1, ["*Database*.kt"], run=run, diff_base=SHA2)
    assert run.calls[0]["argv"][:3] == ["git", "diff", f"{SHA2}..{SHA1}"]


def test_collect_commit_subjects_reads_the_base_range(env):
    run = FakeRun(log_out="루틴 카드 추가\n크래시 수정\n")
    subjects = bp.collect_commit_subjects(env.root / REPO, SHA2, SHA1, run=run)
    assert subjects == ["루틴 카드 추가", "크래시 수정"]
    assert f"{SHA2}..{SHA1}" in run.calls[0]["argv"]


def test_collect_commit_subjects_is_empty_on_git_failure(env):
    run = FakeRun(rc_for={"log": 128})
    assert bp.collect_commit_subjects(env.root / REPO, SHA2, SHA1, run=run) == []


def test_release_notes_summarize_several_commits(env):
    notes = bp.build_release_notes({"task_id": "catch-up"}, migration=False,
                                   subjects=["루틴 카드 추가", "크래시 수정"])
    assert notes.splitlines()[0].startswith("루틴 카드 추가")
    assert "크래시 수정" in notes


def test_release_notes_without_subjects_are_byte_identical_to_before(env):
    payload = {"pr_title": "로그인 화면 정리"}
    assert (bp.build_release_notes(payload, migration=True, subjects=())
            == bp.build_release_notes(payload, migration=True))
    assert bp.build_release_notes(payload, migration=True).splitlines()[0] == bp.BACKUP_WARNING


# --- spawn gate -------------------------------------------------------------

class SpawnSpy:
    def __init__(self, pid=4242):
        self.calls = 0
        self.pid = pid

    def __call__(self):
        self.calls += 1
        return self.pid


def _write_state(path: Path, **data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _due(env, *, spawn, now=NOW, head_branch="main", is_alive=lambda _p: False,
         progress=None, config_path=None):
    return bp.spawn_sweep_if_due(
        state_path=env.state_path, min_interval_s=bp.SWEEP_MIN_INTERVAL_S,
        config_path=config_path if config_path is not None else env.config_path,
        now=now, head_branch=head_branch, spawn=spawn, is_alive=is_alive,
        progress=progress or (lambda _m: None))


def test_spawn_is_skipped_inside_the_min_poll_interval(env):
    _write_state(env.state_path, last_tick_ts=NOW - 60)
    spawn = SpawnSpy()
    assert _due(env, spawn=spawn) is False
    assert spawn.calls == 0


def test_spawn_is_skipped_when_the_vault_head_is_a_worker_branch(env):
    """The sweep commits the watermark in the vault repo; doing that while HEAD
    sits on worker/<slug> strands the canon on a feature branch."""
    spawn = SpawnSpy()
    assert _due(env, spawn=spawn, head_branch="worker/some-task") is False
    assert spawn.calls == 0


def test_spawn_is_skipped_while_a_previous_sweep_is_alive(env):
    _write_state(env.state_path, pid=999, pid_ts=NOW - 120)
    spawn = SpawnSpy()
    assert _due(env, spawn=spawn, is_alive=lambda _p: True) is False
    assert spawn.calls == 0


def test_spawn_ignores_a_pid_older_than_a_whole_build(env):
    """A pid recycled onto some unrelated long-lived process must not wedge the
    sweep forever — past build-timeout + slack, the record is stale."""
    _write_state(env.state_path, pid=999, pid_ts=NOW - (bp._BUILD_TIMEOUT + 3600))
    spawn = SpawnSpy()
    assert _due(env, spawn=spawn, is_alive=lambda _p: True) is True
    assert spawn.calls == 1


def test_spawn_runs_and_records_the_pid(env):
    spawn = SpawnSpy()
    assert _due(env, spawn=spawn) is True
    assert spawn.calls == 1
    state = env.state()
    assert state["pid"] == 4242
    assert state["last_tick_ts"] == NOW


def test_spawn_is_skipped_silently_when_no_config(env, tmp_path):
    """Beta publishing is author-only: a checkout with no (or empty) config must
    print nothing and spawn nothing — this is the empty-vault tick-noise fix."""
    spawn = SpawnSpy()
    lines: list[str] = []

    missing = tmp_path / "no-such" / "beta_publish_config.json"
    assert _due(env, spawn=spawn, config_path=missing,
                progress=lines.append) is False

    empty = tmp_path / "empty_config.json"
    empty.write_text(json.dumps({"repos": {}}), encoding="utf-8")
    assert _due(env, spawn=spawn, config_path=empty,
                progress=lines.append) is False

    assert spawn.calls == 0
    assert lines == []


# --- tick wiring ------------------------------------------------------------

def _orch(tmp_path):
    orch = Orchestrator(FileMailbox(tmp_path / "agents"), Registry([]),
                        Ledger(tmp_path / "assignments"),
                        notify=lambda *a, **k: None)
    orch.usage_hold = lambda: None
    orch.budget_exceeded = lambda: False
    orch.paused = lambda: False
    return orch


def test_beta_sweep_tick_is_fail_open(tmp_path, monkeypatch):
    """A sweep-gate error must never break a tick (the _advote_bridge_tick rule)."""
    def boom(**_kw):
        raise RuntimeError("state file is garbage")

    monkeypatch.setattr(bp, "spawn_sweep_if_due", boom)
    _orch(tmp_path)._beta_sweep_tick()           # must not raise


def test_cli_rejects_anything_but_sweep(capsys):
    """The detached child is started with a fixed argv; anything else is a bug
    upstream and must not be read as a request to publish."""
    assert bp.main(["please-publish-everything"]) == 2
    assert "usage:" in capsys.readouterr().out


def test_cli_sweep_runs_the_sweep_and_prints_its_notes(monkeypatch, capsys):
    seen: list[dict] = []

    def fake_sweep(**kw):
        seen.append(kw)
        return ["베타 13 발행 · DanceMirror"]

    monkeypatch.setattr(bp, "sweep_unpublished", fake_sweep)
    assert bp.main(["sweep"]) == 0
    assert "베타 13 발행" in capsys.readouterr().out
    assert len(seen) == 1


def test_tick_calls_the_beta_sweep(tmp_path):
    orch = _orch(tmp_path)
    called: list[int] = []
    orch._beta_sweep_tick = lambda: called.append(1)
    orch.tick([])
    assert called == [1]


# --- merge-time publish ------------------------------------------------------

def test_merge_publish_builds_in_a_worktree_pinned_to_the_merge_sha(env):
    """The runner's checkout is not at the merge sha after a squash merge, so the
    merge-time publish must build from a worktree of that sha — never from the
    checkout's working tree (if-then-habits #124 shipped #121's tree)."""
    run, publish = FakeRun(), PublishSpy()
    checkout = env.root / "dancemirror"
    note = bp.publish_merge_in_worktree(
        "dancemirror", checkout, SHA1, {"task_id": "t"},
        worktree_root=env.worktree_root, run=run, publish=publish, progress=lambda _m: None)

    assert note is not None
    call = publish.calls[0]
    assert call["dir"].parent == env.worktree_root and call["dir"] != checkout
    assert call["sha"] == SHA1 and call["payload"] == {"task_id": "t"}
    assert "cumulative_notes" not in call["kw"]           # merge-time notes stay per-merge
    add = next(c["argv"] for c in run.calls if "worktree" in c["argv"] and "add" in c["argv"])
    assert add[-1] == SHA1
    assert run.has("worktree", "remove")
