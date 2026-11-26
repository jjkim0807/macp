"""worker.pull_engine_checkout(): the maintenance tick ff-only pulls a separate
engine checkout before restarting the daemons onto it.

Ports ops/macp_engine_pull.sh into Python (Notes/macp-public-split-step7-design
§3). Every git/uv subprocess goes through an injected `run` seam, so these tests
assert on the *arguments* of a fake — no real network git, no real `uv sync`, no
real checkout. The path seam (`engine=` / `vault=`) points the function at a
throwaway tmp dir with (or without) a fake `.git`.

Contract under test:
  * no-op (empty list, zero subprocess calls) for the combined layout
    (engine == vault) or any non-checkout (no engine/.git),
  * skip (empty list, no fetch/pull) when HEAD != main or a *tracked* file is
    modified; untracked files alone never block (checked against a real local
    origin too), unless an incoming commit writes a path one occupies,
  * three consecutive skips open one dashboard ops task; a pull that goes
    through clears the count and closes it,
  * fetch + ff-only pull on a clean, behind checkout, and `uv sync` *only* when
    pyproject.toml / uv.lock changed,
  * a git/uv returncode != 0 is reported as a non-empty reason list (so the
    caller raises the tick rc) — not swallowed and not raised,
  * cmd_maintenance() turns that reason list into rc == 1 without dying,
  * a failed / timed-out / uv-missing `uv sync` leaves a "sync pending" marker
    under vault/Worker/_state, so every later tick retries the sync even when the
    pull is a no-op and pyproject/uv.lock did not change; a successful sync
    clears it, and a tick with no marker stays a zero-diff, zero-uv no-op.
"""
from __future__ import annotations

import subprocess

import pytest

from todo_system import worker


def cp(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class FakeRun:
    """Records every call; returns a configured CompletedProcess per command.

    `responses` is keyed by the git subcommand (argv[3]: "branch", "status",
    "rev-parse", "fetch", "pull", "diff") or by "uv" for `uv sync`. A list value
    is consumed in call order — used for the two `rev-parse --short HEAD`
    reads (before / after). A missing key defaults to success with empty stdout.
    A response that is an exception instance is raised instead of returned.
    """

    def __init__(self, responses: dict | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[list[str], dict]] = []
        self.keys: list[str] = []

    @staticmethod
    def _key(argv: list[str]) -> str:
        return "uv" if argv[0] == "uv" else argv[3]

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        key = self._key(list(argv))
        self.keys.append(key)
        resp = self.responses.get(key)
        if isinstance(resp, list):
            resp = resp.pop(0)
        if resp is None:
            return cp(0, "")
        if isinstance(resp, BaseException):
            raise resp
        return resp


def _checkout(tmp_path):
    engine = tmp_path / "macp"
    (engine / ".git").mkdir(parents=True)
    vault = tmp_path / "vault"
    vault.mkdir()
    return engine, vault


def _clean_behind_responses():
    return {
        "branch": cp(0, "main\n"),
        "status": cp(0, ""),
        "rev-parse": [cp(0, "aaaaaaa"), cp(0, "bbbbbbb")],
        "fetch": cp(0, ""),
        "pull": cp(0, ""),
        "diff": cp(0, ""),  # exit 0 = pyproject/uv.lock unchanged
    }


def _no_change_responses():
    """A clean checkout already at origin/main: the pull moves nothing."""
    responses = _clean_behind_responses()
    responses["rev-parse"] = [cp(0, "bbbbbbb"), cp(0, "bbbbbbb")]
    return responses


def _marker(vault):
    return vault / "Worker" / "_state" / "engine-uv-sync-pending"


def _set_marker(vault):
    _marker(vault).parent.mkdir(parents=True, exist_ok=True)
    _marker(vault).write_text("pending\n")


def test_clean_behind_pulls(tmp_path):
    engine, vault = _checkout(tmp_path)
    fake = FakeRun(_clean_behind_responses())

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=fake)

    assert reasons == []
    assert "fetch" in fake.keys
    assert "pull" in fake.keys
    assert "uv" not in fake.keys  # no dependency change → no uv sync


def test_dirty_skips(tmp_path, capsys):
    engine, vault = _checkout(tmp_path)
    fake = FakeRun({"branch": cp(0, "main\n"), "status": cp(0, " M todo_system/x.py\n")})

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=fake)

    assert reasons == []
    assert "fetch" not in fake.keys
    assert "pull" not in fake.keys
    assert "dirty" in capsys.readouterr().out


def test_dirty_check_ignores_untracked_files(tmp_path):
    """The dirty probe asks git for tracked changes only — an untracked runtime
    log must not count (2026-11-26: logs/kakao_* stalled every pull)."""
    engine, vault = _checkout(tmp_path)
    fake = FakeRun(_clean_behind_responses())

    worker.pull_engine_checkout(engine=engine, vault=vault, run=fake)

    status_argv = next(argv for argv, _ in fake.calls if argv[3] == "status")
    assert "--untracked-files=no" in status_argv


def test_untracked_overlapping_incoming_skips(tmp_path, capsys):
    engine, vault = _checkout(tmp_path)
    responses = _clean_behind_responses()
    responses["ls-files"] = cp(0, "logs/\nnotes.txt\n")
    responses["diff"] = cp(0, "logs/new.log\ntodo_system/x.py\n")
    fake = FakeRun(responses)

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=fake)

    assert reasons == []
    assert "fetch" in fake.keys
    assert "pull" not in fake.keys
    assert "겹침" in capsys.readouterr().out


def test_untracked_not_overlapping_pulls(tmp_path):
    engine, vault = _checkout(tmp_path)
    responses = _clean_behind_responses()
    responses["ls-files"] = cp(0, "logs/\n")
    responses["diff"] = [cp(0, "todo_system/x.py\n"), cp(0, "")]  # overlap probe, deps probe
    fake = FakeRun(responses)

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=fake)

    assert reasons == []
    assert "pull" in fake.keys


def test_untracked_overlap_matches_dir_prefix_both_ways():
    assert worker._untracked_overlap(["logs/"], ["logs/a.log"]) == ["logs/"]
    assert worker._untracked_overlap(["tools"], ["tools/x.py"]) == ["tools"]
    assert worker._untracked_overlap(["a/b.txt"], ["a"]) == ["a/b.txt"]
    assert worker._untracked_overlap(["logs/"], ["logsx/a", "todo_system/x.py"]) == []


# --- consecutive skips → one dashboard alert ---------------------------------


def _ops_task(name="engine-pull-stalled"):
    from todo_system import ops_alerts
    return ops_alerts.vault_root() / "Todo" / "items" / f"ops-{name}.md"


def test_three_consecutive_skips_open_one_alert(tmp_path, monkeypatch):
    engine, vault = _checkout(tmp_path)
    opened = []
    from todo_system import ops_alerts
    real_open = ops_alerts.open_alert
    monkeypatch.setattr(ops_alerts, "open_alert",
                        lambda *a, **kw: (opened.append(a[0]), real_open(*a, **kw)))

    def dirty_tick():
        fake = FakeRun({"branch": cp(0, "main\n"), "status": cp(0, " M x.py\n")})
        assert worker.pull_engine_checkout(engine=engine, vault=vault, run=fake) == []

    dirty_tick()
    dirty_tick()
    assert opened == []                       # two skips: still quiet
    dirty_tick()
    assert opened == ["engine-pull-stalled"]  # third: exactly one alert
    assert "status: open" in _ops_task().read_text(encoding="utf-8")
    dirty_tick()
    dirty_tick()
    assert opened == ["engine-pull-stalled"]  # no repeat while it keeps skipping

    # The next pull that goes through clears the count and closes the task.
    worker.pull_engine_checkout(engine=engine, vault=vault, run=FakeRun(_clean_behind_responses()))
    assert not (vault / "Worker" / "_state" / "engine-pull-skips").exists()
    assert "status: done" in _ops_task().read_text(encoding="utf-8")

    # A fresh streak counts from zero again.
    dirty_tick()
    dirty_tick()
    assert opened == ["engine-pull-stalled"]


def test_mixed_skip_kinds_share_one_streak(tmp_path):
    engine, vault = _checkout(tmp_path)
    worker.pull_engine_checkout(engine=engine, vault=vault,
                                run=FakeRun({"branch": cp(0, "worker/x\n")}))
    worker.pull_engine_checkout(engine=engine, vault=vault,
                                run=FakeRun({"branch": cp(0, "main\n"), "status": cp(0, " M x\n")}))
    assert not _ops_task().exists()
    responses = _clean_behind_responses()
    responses["ls-files"] = cp(0, "logs/\n")
    responses["diff"] = cp(0, "logs/a.log\n")
    worker.pull_engine_checkout(engine=engine, vault=vault, run=FakeRun(responses))
    assert _ops_task().exists()


def test_fetch_failure_does_not_reset_streak(tmp_path):
    engine, vault = _checkout(tmp_path)
    counter = vault / "Worker" / "_state" / "engine-pull-skips"
    worker.pull_engine_checkout(engine=engine, vault=vault,
                                run=FakeRun({"branch": cp(0, "worker/x\n")}))
    assert counter.read_text(encoding="utf-8").startswith("1 ")
    responses = _clean_behind_responses()
    responses["fetch"] = cp(1, "")
    worker.pull_engine_checkout(engine=engine, vault=vault, run=FakeRun(responses))
    assert counter.read_text(encoding="utf-8").startswith("1 ")


# --- real git: a local origin, no network ------------------------------------


def _g(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True)


@pytest.fixture
def real_checkout(tmp_path):
    """(engine clone on main, an upstream clone to push new commits from, vault)."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    upstream = tmp_path / "upstream"
    subprocess.run(["git", "clone", "-q", str(origin), str(upstream)], check=True)
    _g(upstream, "checkout", "-q", "-b", "main")
    (upstream / "README.md").write_text("v1\n")
    _g(upstream, "add", "README.md")
    _g(upstream, "commit", "-q", "-m", "v1")
    _g(upstream, "push", "-q", "origin", "main")
    engine = tmp_path / "engine"
    subprocess.run(["git", "clone", "-q", "-b", "main", str(origin), str(engine)], check=True)
    vault = tmp_path / "vault"
    vault.mkdir()
    return engine, upstream, vault


def _push_change(upstream, rel="README.md", text="v2\n"):
    path = upstream / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    _g(upstream, "add", rel)
    _g(upstream, "commit", "-q", "-m", f"change {rel}")
    _g(upstream, "push", "-q", "origin", "main")
    return _g(upstream, "rev-parse", "HEAD").stdout.strip()


def _head(repo):
    return _g(repo, "rev-parse", "HEAD").stdout.strip()


def test_real_git_untracked_logs_still_fast_forwards(real_checkout):
    engine, upstream, vault = real_checkout
    (engine / "logs").mkdir()
    (engine / "logs" / "kakao_digest.jsonl").write_text("{}\n")
    (engine / "logs" / "kakao_route.log").write_text("x\n")
    new = _push_change(upstream)

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault)

    assert reasons == []
    assert _head(engine) == new
    assert (engine / "logs" / "kakao_route.log").exists()  # untouched
    assert not (vault / "Worker" / "_state" / "engine-pull-skips").exists()


def test_real_git_modified_tracked_file_skips(real_checkout, capsys):
    engine, upstream, vault = real_checkout
    before = _head(engine)
    (engine / "README.md").write_text("local edit\n")
    _push_change(upstream, "other.txt", "o\n")

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault)

    assert reasons == []
    assert _head(engine) == before
    assert "dirty" in capsys.readouterr().out


def test_real_git_untracked_file_in_incoming_path_skips(real_checkout, capsys):
    engine, upstream, vault = real_checkout
    before = _head(engine)
    (engine / "logs").mkdir()
    (engine / "logs" / "a.log").write_text("mine\n")
    _push_change(upstream, "logs/a.log", "theirs\n")

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault)

    assert reasons == []  # a skip, not a failed pull
    assert _head(engine) == before
    assert (engine / "logs" / "a.log").read_text() == "mine\n"
    assert "겹침" in capsys.readouterr().out


def test_head_not_main_skips(tmp_path, capsys):
    engine, vault = _checkout(tmp_path)
    fake = FakeRun({"branch": cp(0, "worker/foo\n")})

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=fake)

    assert reasons == []
    assert "fetch" not in fake.keys
    assert "main" in capsys.readouterr().out


def test_deps_changed_runs_uv_sync(tmp_path):
    engine, vault = _checkout(tmp_path)
    responses = _clean_behind_responses()
    responses["diff"] = cp(1, "")  # exit 1 = pyproject/uv.lock changed
    responses["uv"] = cp(0, "")
    fake = FakeRun(responses)

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=fake)

    assert reasons == []
    uv_calls = [(argv, kw) for argv, kw in fake.calls if argv[0] == "uv"]
    assert len(uv_calls) == 1
    argv, kwargs = uv_calls[0]
    assert argv == ["uv", "sync", "-q"]
    assert kwargs.get("cwd") == str(engine)


def test_combined_layout_noop(tmp_path):
    engine, _ = _checkout(tmp_path)
    fake = FakeRun()

    reasons = worker.pull_engine_checkout(engine=engine, vault=engine, run=fake)

    assert reasons == []
    assert fake.calls == []  # no subprocess at all


def test_non_git_dir_noop(tmp_path):
    engine = tmp_path / "wheelish"
    engine.mkdir()  # no .git → wheel / non-checkout layout
    vault = tmp_path / "vault"
    vault.mkdir()
    fake = FakeRun()

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=fake)

    assert reasons == []
    assert fake.calls == []


def test_fetch_failure_returns_reason(tmp_path):
    engine, vault = _checkout(tmp_path)
    responses = _clean_behind_responses()
    responses["fetch"] = cp(1, "")
    fake = FakeRun(responses)

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=fake)

    assert reasons  # non-empty → caller raises rc
    assert "pull" not in fake.keys


def test_pull_failure_returns_reason(tmp_path):
    engine, vault = _checkout(tmp_path)
    responses = _clean_behind_responses()
    responses["pull"] = cp(1, "")
    fake = FakeRun(responses)

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=fake)

    assert reasons
    assert "uv" not in fake.keys


def test_uv_sync_failure_returns_reason(tmp_path):
    engine, vault = _checkout(tmp_path)
    responses = _clean_behind_responses()
    responses["diff"] = cp(1, "")
    responses["uv"] = cp(1, "")
    fake = FakeRun(responses)

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=fake)

    assert reasons


def test_no_change_noop(tmp_path):
    engine, vault = _checkout(tmp_path)
    responses = _clean_behind_responses()
    responses["rev-parse"] = [cp(0, "aaaaaaa"), cp(0, "aaaaaaa")]  # before == after
    fake = FakeRun(responses)

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=fake)

    assert reasons == []
    assert "diff" not in fake.keys  # short-circuits before the dependency check
    assert "uv" not in fake.keys


def test_cmd_maintenance_rc_on_failure(monkeypatch):
    """A non-empty reason list raises the tick rc to 1 without dying (criterion #2)."""
    monkeypatch.setattr(worker, "pull_engine_checkout", lambda: ["engine-pull: boom"])
    # Neutralise every other upkeep step so rc == 1 can only come from engine-pull.
    monkeypatch.setattr(worker, "sync_vault_state", lambda: 0)
    monkeypatch.setattr(worker, "rotate_logs", lambda: [])
    monkeypatch.setattr(worker, "bump_vault_engine_pin", lambda: [])
    monkeypatch.setattr(worker, "sync_wrappers", lambda: [])
    monkeypatch.setattr(worker, "restart_stale_daemons", lambda: [])
    monkeypatch.setattr(worker, "check_dispatch_silence", lambda: None)
    monkeypatch.setattr(worker, "check_work_starvation", lambda: None)
    monkeypatch.setattr(worker.browser_watch, "check_login_watch", lambda **kw: None)

    assert worker.cmd_maintenance() == 1


# --- retry-on-next-tick: a failed sync must not be forgotten -----------------


def _assert_retry_tick_succeeds(engine, vault):
    """The tick after a failed sync: no new commit, deps unchanged — still syncs."""
    responses = _no_change_responses()
    responses["uv"] = cp(0, "")
    fake = FakeRun(responses)

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=fake)

    assert reasons == []
    assert fake.keys.count("uv") == 1  # retried even though before == after
    assert "diff" not in fake.keys  # pending → no dependency check needed
    assert not _marker(vault).exists()  # success clears the pending state


def _assert_noop_tick(engine, vault):
    fake = FakeRun(_no_change_responses())

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=fake)

    assert reasons == []
    assert "diff" not in fake.keys
    assert "uv" not in fake.keys


def test_uv_sync_failure_retries_next_tick(tmp_path):
    engine, vault = _checkout(tmp_path)
    responses = _clean_behind_responses()
    responses["diff"] = cp(1, "")
    responses["uv"] = cp(1, "")

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=FakeRun(responses))

    assert reasons == ["engine-pull: uv sync 실패"]
    assert _marker(vault).exists()

    _assert_retry_tick_succeeds(engine, vault)
    _assert_noop_tick(engine, vault)  # cleared marker → back to a plain no-op


def test_uv_sync_timeout_retries_next_tick(tmp_path):
    engine, vault = _checkout(tmp_path)
    responses = _clean_behind_responses()
    responses["diff"] = cp(1, "")
    responses["uv"] = subprocess.TimeoutExpired(["uv", "sync", "-q"], 300)

    # Caught inside the function, not left for the caller's generic handler.
    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=FakeRun(responses))

    assert reasons == ["engine-pull: uv sync 타임아웃"]
    assert _marker(vault).exists()

    _assert_retry_tick_succeeds(engine, vault)
    _assert_noop_tick(engine, vault)


def test_pending_retry_failure_keeps_marker(tmp_path):
    engine, vault = _checkout(tmp_path)
    _set_marker(vault)
    responses = _no_change_responses()
    responses["uv"] = cp(1, "")
    fake = FakeRun(responses)

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=fake)

    assert reasons == ["engine-pull: uv sync 실패"]
    assert fake.keys.count("uv") == 1
    assert _marker(vault).exists()  # still pending → the next tick retries again


def test_pending_with_new_commit_skips_diff_and_syncs(tmp_path):
    engine, vault = _checkout(tmp_path)
    _set_marker(vault)
    responses = _clean_behind_responses()  # before != after, diff would say "unchanged"
    responses["uv"] = cp(0, "")
    fake = FakeRun(responses)

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=fake)

    assert reasons == []
    assert "diff" not in fake.keys
    assert fake.keys.count("uv") == 1
    assert not _marker(vault).exists()


def test_pending_dirty_skip_keeps_marker(tmp_path):
    engine, vault = _checkout(tmp_path)
    _set_marker(vault)
    fake = FakeRun({"branch": cp(0, "main\n"), "status": cp(0, " M todo_system/x.py\n")})

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=fake)

    assert reasons == []
    assert "fetch" not in fake.keys
    assert "uv" not in fake.keys
    assert _marker(vault).exists()


def test_uv_missing_sets_marker(tmp_path):
    engine, vault = _checkout(tmp_path)
    responses = _clean_behind_responses()
    responses["diff"] = cp(1, "")
    responses["uv"] = FileNotFoundError("uv")

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=FakeRun(responses))

    assert reasons
    assert _marker(vault).exists()


def test_marker_write_oserror_does_not_raise(tmp_path, capsys):
    engine, vault = _checkout(tmp_path)
    (vault / "Worker").write_text("x")  # a file where the _state dir must go
    responses = _clean_behind_responses()
    responses["diff"] = cp(1, "")
    responses["uv"] = cp(1, "")

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=FakeRun(responses))

    assert reasons == ["engine-pull: uv sync 실패"]  # the original reason survives
    assert not _marker(vault).exists()
    assert "보류 마커 기록 실패" in capsys.readouterr().out
