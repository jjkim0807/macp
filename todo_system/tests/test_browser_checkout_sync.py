"""worker.pull_browser_checkout(): the maintenance tick ff-only pulls the
macp-browser checkout and runs `npm ci` when the lock changed or node_modules
drifted from it.

Every git/npm subprocess goes through an injected `run` seam, so these tests
assert on the *arguments* of a fake — no real network git, no real `npm ci`.
The path seam (`browser_dir=`) points the function at a throwaway tmp dir with
(or without) a fake `.git`, lock file and node_modules.

Contract under test:
  * no-op (empty list, zero subprocess calls) when the dir is missing or is not
    a git checkout — including the default dir the conftest points elsewhere,
  * skip (empty list, one line, no fetch/pull/npm) when HEAD != main or a
    tracked file is dirty,
  * `npm ci` when the pull changed package-lock.json, or when node_modules is
    missing a direct dependency / holds a different version; no npm otherwise,
  * git/npm failures and timeouts come back as a non-empty reason list; npm
    missing is a quiet skip,
  * cmd_maintenance() keeps running later steps (daemon restart) when this step
    fails or raises.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from todo_system import worker


def cp(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class FakeRun:
    """Records every call; returns a configured response per command.

    `responses` is keyed by "npm" (any argv whose argv[0] basename is npm) or by
    the git subcommand (argv[3]: "branch", "status", "rev-parse", "fetch",
    "pull", "diff"). A list value is consumed in call order; an Exception
    instance is raised. A missing key defaults to success with empty stdout.
    """

    def __init__(self, responses: dict | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[list[str], dict]] = []
        self.keys: list[str] = []

    @staticmethod
    def _key(argv: list[str]) -> str:
        return "npm" if Path(argv[0]).name == "npm" else argv[3]

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        key = self._key(list(argv))
        self.keys.append(key)
        resp = self.responses.get(key)
        if isinstance(resp, list):
            resp = resp.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        if resp is None:
            return cp(0, "")
        return resp


_DEPS = {"@modelcontextprotocol/sdk": "1.2.0", "zod": "4.1.0"}


def _checkout(tmp_path, *, lock: bool = True, installed: bool = True) -> Path:
    d = tmp_path / "macp-browser"
    (d / ".git").mkdir(parents=True)
    if lock:
        packages = {"": {"name": "macp-browser",
                         "dependencies": {n: f"^{v}" for n, v in _DEPS.items()}}}
        for name, version in _DEPS.items():
            packages[f"node_modules/{name}"] = {"version": version}
        (d / "package-lock.json").write_text(
            json.dumps({"lockfileVersion": 3, "packages": packages}), encoding="utf-8")
    if installed:
        for name, version in _DEPS.items():
            _install(d, name, version)
    return d


def _install(d: Path, name: str, version: str) -> None:
    pkg = d / "node_modules" / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "package.json").write_text(
        json.dumps({"name": name, "version": version}), encoding="utf-8")


def _clean_behind_responses():
    return {
        "branch": cp(0, "main\n"),
        "status": cp(0, ""),
        "rev-parse": [cp(0, "aaaaaaa"), cp(0, "bbbbbbb")],
        "fetch": cp(0, ""),
        "pull": cp(0, ""),
        "diff": cp(0, ""),  # exit 0 = package-lock.json unchanged
    }


def _no_pull_responses():
    responses = _clean_behind_responses()
    responses["rev-parse"] = [cp(0, "aaaaaaa"), cp(0, "aaaaaaa")]  # before == after
    return responses


def _npm_calls(fake: FakeRun):
    return [(argv, kw) for argv, kw in fake.calls if Path(argv[0]).name == "npm"]


def test_lock_changed_runs_npm_ci(tmp_path, capsys):
    d = _checkout(tmp_path)
    responses = _clean_behind_responses()
    responses["diff"] = cp(1, "")  # exit 1 = package-lock.json changed
    fake = FakeRun(responses)

    reasons = worker.pull_browser_checkout(browser_dir=d, run=fake)

    assert reasons == []
    npm = _npm_calls(fake)
    assert len(npm) == 1
    argv, kwargs = npm[0]
    assert argv[1] == "ci"
    assert kwargs.get("cwd") == str(d)
    assert kwargs.get("timeout") == worker._NPM_CI_TIMEOUT_S
    diff_argv = next(argv for argv, _ in fake.calls if argv[3] == "diff")
    assert diff_argv[-2:] == ["--", "package-lock.json"]
    out = capsys.readouterr().out
    assert "pulled aaaaaaa -> bbbbbbb + npm ci" in out


def test_lock_unchanged_and_modules_match_skips_npm(tmp_path, capsys):
    d = _checkout(tmp_path)
    fake = FakeRun(_clean_behind_responses())

    reasons = worker.pull_browser_checkout(browser_dir=d, run=fake)

    assert reasons == []
    assert "fetch" in fake.keys
    assert "pull" in fake.keys
    assert "npm" not in fake.keys
    assert capsys.readouterr().out.strip() == "browser-pull: pulled aaaaaaa -> bbbbbbb"


def test_no_pull_but_module_missing_runs_npm_ci(tmp_path, capsys):
    # The 2026-10-28 second half: checkout current, @modelcontextprotocol/sdk absent.
    d = _checkout(tmp_path, installed=False)
    _install(d, "zod", _DEPS["zod"])
    fake = FakeRun(_no_pull_responses())

    reasons = worker.pull_browser_checkout(browser_dir=d, run=fake)

    assert reasons == []
    assert "diff" not in fake.keys  # no pull → no lock diff
    assert len(_npm_calls(fake)) == 1
    assert "npm ci" in capsys.readouterr().out


def test_no_pull_but_version_mismatch_runs_npm_ci(tmp_path):
    # Retry path: an earlier npm ci after a lock change failed, old versions remain.
    d = _checkout(tmp_path)
    _install(d, "zod", "3.0.0")
    fake = FakeRun(_no_pull_responses())

    reasons = worker.pull_browser_checkout(browser_dir=d, run=fake)

    assert reasons == []
    assert len(_npm_calls(fake)) == 1


def test_steady_state_silent(tmp_path, capsys):
    d = _checkout(tmp_path)
    fake = FakeRun(_no_pull_responses())

    reasons = worker.pull_browser_checkout(browser_dir=d, run=fake)

    assert reasons == []
    assert "npm" not in fake.keys
    assert capsys.readouterr().out == ""


def test_head_not_main_skips(tmp_path, capsys):
    d = _checkout(tmp_path)
    fake = FakeRun({"branch": cp(0, "worker/foo\n")})

    reasons = worker.pull_browser_checkout(browser_dir=d, run=fake)

    assert reasons == []
    assert fake.keys == ["branch"]
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1
    assert "main" in out


def test_dirty_skips(tmp_path, capsys):
    d = _checkout(tmp_path)
    fake = FakeRun({"branch": cp(0, "main\n"), "status": cp(0, " M mcp/server.mjs\n")})

    reasons = worker.pull_browser_checkout(browser_dir=d, run=fake)

    assert reasons == []
    assert fake.keys == ["branch", "status"]
    status_argv = fake.calls[1][0]
    assert "--untracked-files=no" in status_argv
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1
    assert "dirty" in out


def test_missing_dir_noop(tmp_path):
    fake = FakeRun()

    reasons = worker.pull_browser_checkout(browser_dir=tmp_path / "absent", run=fake)

    assert reasons == []
    assert fake.calls == []


def test_non_git_dir_noop(tmp_path):
    d = tmp_path / "macp-browser"
    d.mkdir()  # an install that is not a git checkout
    (d / "package-lock.json").write_text("{}", encoding="utf-8")
    fake = FakeRun()

    reasons = worker.pull_browser_checkout(browser_dir=d, run=fake)

    assert reasons == []
    assert fake.calls == []


def test_default_dir_from_env_noop(capsys):
    # conftest points MACP_BROWSER_DIR at a path that does not exist.
    fake = FakeRun()

    reasons = worker.pull_browser_checkout(run=fake)

    assert reasons == []
    assert fake.calls == []
    assert capsys.readouterr().out == ""


def test_fetch_failure_reason(tmp_path):
    d = _checkout(tmp_path)
    responses = _clean_behind_responses()
    responses["fetch"] = cp(1, "")
    fake = FakeRun(responses)

    reasons = worker.pull_browser_checkout(browser_dir=d, run=fake)

    assert reasons == ["browser-pull: fetch 실패"]
    assert "pull" not in fake.keys
    assert "npm" not in fake.keys


def test_pull_failure_reason(tmp_path):
    d = _checkout(tmp_path)
    responses = _clean_behind_responses()
    responses["pull"] = cp(1, "")
    fake = FakeRun(responses)

    reasons = worker.pull_browser_checkout(browser_dir=d, run=fake)

    assert reasons and reasons[0].startswith("browser-pull: ff-only pull 실패")
    assert "npm" not in fake.keys


def test_npm_failure_reason(tmp_path):
    d = _checkout(tmp_path)
    responses = _clean_behind_responses()
    responses["diff"] = cp(1, "")
    responses["npm"] = cp(1, "")
    fake = FakeRun(responses)

    reasons = worker.pull_browser_checkout(browser_dir=d, run=fake)

    assert reasons == ["browser-pull: npm ci 실패"]


def test_npm_missing_quiet_skip(tmp_path, capsys):
    d = _checkout(tmp_path)
    responses = _clean_behind_responses()
    responses["diff"] = cp(1, "")
    responses["npm"] = FileNotFoundError("npm")
    fake = FakeRun(responses)

    reasons = worker.pull_browser_checkout(browser_dir=d, run=fake)

    assert reasons == []
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1
    assert "npm 없음" in out


def test_npm_timeout_reason(tmp_path):
    d = _checkout(tmp_path)
    responses = _clean_behind_responses()
    responses["diff"] = cp(1, "")
    responses["npm"] = subprocess.TimeoutExpired(["npm"], worker._NPM_CI_TIMEOUT_S)
    fake = FakeRun(responses)

    reasons = worker.pull_browser_checkout(browser_dir=d, run=fake)

    assert reasons == ["browser-pull: npm ci 타임아웃"]


def test_git_timeout_reason(tmp_path):
    d = _checkout(tmp_path)
    responses = _clean_behind_responses()
    responses["fetch"] = subprocess.TimeoutExpired(["git"], worker._GIT_TIMEOUT_S)
    fake = FakeRun(responses)

    reasons = worker.pull_browser_checkout(browser_dir=d, run=fake)

    assert reasons == ["browser-pull: git 타임아웃"]
    assert "npm" not in fake.keys


def test_unparsable_lock_skips_npm(tmp_path, capsys):
    d = _checkout(tmp_path, lock=False, installed=False)
    (d / "package-lock.json").write_text("{", encoding="utf-8")
    fake = FakeRun(_no_pull_responses())

    reasons = worker.pull_browser_checkout(browser_dir=d, run=fake)

    assert reasons == []
    assert "npm" not in fake.keys
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1
    assert "파싱 실패" in out


def test_no_lock_skips_npm(tmp_path, capsys):
    d = _checkout(tmp_path, lock=False, installed=False)
    fake = FakeRun(_clean_behind_responses())

    reasons = worker.pull_browser_checkout(browser_dir=d, run=fake)

    assert reasons == []
    assert "npm" not in fake.keys
    assert "diff" in fake.keys
    assert "pulled aaaaaaa -> bbbbbbb" in capsys.readouterr().out


def test_npm_bin_next_to_node(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "npm").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("MACP_BROWSER_NODE", str(bindir / "node"))
    assert worker._browser_npm_bin() == str(bindir / "npm")

    monkeypatch.setenv("MACP_BROWSER_NODE", "node")
    assert worker._browser_npm_bin() == "npm"

    monkeypatch.setenv("MACP_BROWSER_NODE", str(tmp_path / "elsewhere" / "node"))
    assert worker._browser_npm_bin() == "npm"  # no npm beside it

    monkeypatch.delenv("MACP_BROWSER_NODE", raising=False)
    assert worker._browser_npm_bin() == "npm"
