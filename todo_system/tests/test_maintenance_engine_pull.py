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
  * skip (empty list, no fetch/pull) when HEAD != main or the tree is dirty,
  * fetch + ff-only pull on a clean, behind checkout, and `uv sync` *only* when
    pyproject.toml / uv.lock changed,
  * a git/uv returncode != 0 is reported as a non-empty reason list (so the
    caller raises the tick rc) — not swallowed and not raised,
  * cmd_maintenance() turns that reason list into rc == 1 without dying.
"""
from __future__ import annotations

import subprocess

from todo_system import worker


def cp(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class FakeRun:
    """Records every call; returns a configured CompletedProcess per command.

    `responses` is keyed by the git subcommand (argv[3]: "branch", "status",
    "rev-parse", "fetch", "pull", "diff") or by "uv" for `uv sync`. A list value
    is consumed in call order — used for the two `rev-parse --short HEAD`
    reads (before / after). A missing key defaults to success with empty stdout.
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
            return resp.pop(0)
        if resp is None:
            return cp(0, "")
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
    fake = FakeRun({"branch": cp(0, "main\n"), "status": cp(0, "?? scratch\n")})

    reasons = worker.pull_engine_checkout(engine=engine, vault=vault, run=fake)

    assert reasons == []
    assert "fetch" not in fake.keys
    assert "pull" not in fake.keys
    assert "dirty" in capsys.readouterr().out


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
    monkeypatch.setattr(worker, "IS_REMOTE_HOST", False)
    monkeypatch.setattr(worker, "pull_engine_checkout", lambda: ["engine-pull: boom"])
    # Neutralise every other upkeep step so rc == 1 can only come from engine-pull.
    monkeypatch.setattr(worker, "autoclose_stale_reviews_local", lambda: 0)
    monkeypatch.setattr(worker, "sweep_remote_completed_runs", lambda: 0)
    monkeypatch.setattr(worker, "sync_vault_state", lambda: 0)
    monkeypatch.setattr(worker, "rotate_logs", lambda: [])
    monkeypatch.setattr(worker, "check_unapplied_migrations", lambda: [])
    monkeypatch.setattr(worker, "restart_stale_daemons", lambda: [])
    monkeypatch.setattr(worker, "check_dispatch_silence", lambda: None)
    monkeypatch.setattr(worker, "check_work_starvation", lambda: None)
    monkeypatch.setattr(worker.browser_watch, "check_login_watch", lambda **kw: None)

    assert worker.cmd_maintenance() == 1
