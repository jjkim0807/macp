"""worker.bump_vault_engine_pin(): after the engine pull, the maintenance tick
moves the vault's own macp git pin (its uv.lock) up to the engine checkout HEAD.

A vault that pins macp as a git dependency otherwise keeps running a stale
engine from `uv run python -m todo_system.cli …` — 56 commits behind on
2026-10-27 — because pull_engine_checkout() moves the daemons' checkout, not
the pin. Every git/uv subprocess goes through an injected `run` seam, so these
tests assert on the *arguments* of a fake: no real git, uv or network.

Contract under test:
  * bump order uv lock → uv sync → git add uv.lock → commit (limited to uv.lock),
    no push,
  * skip with one log line when the vault HEAD is not main, pyproject.toml /
    uv.lock are dirty, or the index differs from HEAD,
  * zero subprocess calls when the vault has no pyproject.toml / uv.lock, no
    macp git source in the lock, or there is no separate engine checkout,
  * a failure after `uv lock` restores uv.lock from HEAD and is reported as a
    reason (so cmd_maintenance raises the tick rc) — not raised.
"""
from __future__ import annotations

import subprocess

import pytest

from todo_system import worker

OLD = "a" * 40
NEW = "b" * 40

_PYPROJECT = """\
[project]
name = "vault"
version = "0"
dependencies = ["macp @ git+https://example.invalid/macp"]
"""


def _lock(macp_source: str) -> str:
    return f"""\
version = 1
requires-python = ">=3.12"

[[package]]
name = "macp"
version = "0.1.0"
source = {macp_source}

[[package]]
name = "pyyaml"
version = "6.0"
source = {{ registry = "https://pypi.org/simple" }}
"""


def _git_lock(sha: str) -> str:
    return _lock(f'{{ git = "https://example.invalid/macp#{sha}" }}')


def cp(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class FakeRun:
    """Records every call; returns a configured response per command.

    Keys: "uv lock" / "uv sync" for uv, the git subcommand (argv[3]) otherwise,
    with the two `diff` forms told apart as "diff --cached" and "diff -- uv.lock".
    A value may be a CompletedProcess, a list (consumed in call order), an
    exception instance (raised) or a callable(argv, kwargs) for side effects.
    A missing key defaults to success with empty stdout.
    """

    def __init__(self, responses: dict | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[list[str], dict]] = []
        self.keys: list[str] = []

    @staticmethod
    def _key(argv: list[str]) -> str:
        if argv[0] == "uv":
            return f"uv {argv[1]}"
        sub = argv[3]
        if sub == "diff":
            return "diff --cached" if "--cached" in argv else "diff -- uv.lock"
        return sub

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        key = self._key(argv)
        self.keys.append(key)
        resp = self.responses.get(key)
        if isinstance(resp, list):
            resp = resp.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        if callable(resp):
            return resp(argv, kwargs)
        if resp is None:
            return cp(0, "")
        return resp


def _layout(tmp_path, lock: str | None = None, pyproject: bool = True):
    engine = tmp_path / "engine"
    (engine / ".git").mkdir(parents=True)
    vault = tmp_path / "vault"
    (vault / ".git").mkdir(parents=True)
    if pyproject:
        (vault / "pyproject.toml").write_text(_PYPROJECT)
    if lock is not None:
        (vault / "uv.lock").write_text(lock)
    return engine, vault


def _behind_responses(vault):
    def _lock_moves(argv, kwargs):
        (vault / "uv.lock").write_text(_git_lock(NEW))
        return cp(0, "")

    return {
        "branch": cp(0, "main\n"),
        "status": cp(0, ""),
        "diff --cached": cp(0, ""),
        "rev-parse": cp(0, NEW + "\n"),
        "merge-base": cp(0, ""),  # rc 0 = pin is an ancestor of engine HEAD
        "uv lock": _lock_moves,
        "diff -- uv.lock": cp(1, ""),  # rc 1 = uv.lock changed
        "uv sync": cp(0, ""),
    }


def _uv_keys(fake: FakeRun) -> list[str]:
    return [k for k in fake.keys if k.startswith("uv ")]


def test_bump_order_lock_sync_add_commit(tmp_path, capsys):
    engine, vault = _layout(tmp_path, _git_lock(OLD))
    fake = FakeRun(_behind_responses(vault))

    reasons = worker.bump_vault_engine_pin(engine=engine, vault=vault, run=fake)

    assert reasons == []
    k = fake.keys
    assert k.index("uv lock") < k.index("uv sync") < k.index("add") < k.index("commit")
    assert "push" not in k
    assert "checkout" not in k  # no rollback on the happy path

    for argv, kwargs in fake.calls:
        if argv[0] == "uv":
            assert kwargs.get("cwd") == str(vault)
        else:
            assert argv[:2] == ["git", "-C"]
            assert argv[2] in (str(vault), str(engine))
    by_key = dict(zip(fake.keys, (argv for argv, _ in fake.calls)))
    assert by_key["uv lock"][1:] == ["lock", "-q", "--upgrade-package", "macp"]
    assert by_key["rev-parse"][2] == str(engine)
    assert by_key["merge-base"][2:] == [str(engine), "merge-base", "--is-ancestor", OLD, NEW]
    assert by_key["add"] == ["git", "-C", str(vault), "add", "uv.lock"]
    commit = by_key["commit"]
    assert commit[2] == str(vault)
    assert commit[-2:] == ["--", "uv.lock"]
    assert "--no-verify" not in commit
    assert "chore: bump macp pin aaaaaaa -> bbbbbbb" in commit
    assert "aaaaaaa -> bbbbbbb" in capsys.readouterr().out


def test_head_not_main_skips(tmp_path, capsys):
    engine, vault = _layout(tmp_path, _git_lock(OLD))
    responses = _behind_responses(vault)
    responses["branch"] = cp(0, "worker/x\n")
    fake = FakeRun(responses)

    reasons = worker.bump_vault_engine_pin(engine=engine, vault=vault, run=fake)

    assert reasons == []
    assert _uv_keys(fake) == []
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1 and "main" in out[0]


def test_dirty_lock_or_pyproject_skips(tmp_path, capsys):
    engine, vault = _layout(tmp_path, _git_lock(OLD))
    responses = _behind_responses(vault)
    responses["status"] = cp(0, " M uv.lock\n")
    fake = FakeRun(responses)

    reasons = worker.bump_vault_engine_pin(engine=engine, vault=vault, run=fake)

    assert reasons == []
    assert _uv_keys(fake) == []
    status = next(argv for argv, _ in fake.calls if argv[0] == "git" and argv[3] == "status")
    assert status[-3:] == ["--", "pyproject.toml", "uv.lock"]  # not the whole tree
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 1 and "dirty" in out[0]


def test_index_not_at_head_skips(tmp_path, capsys):
    engine, vault = _layout(tmp_path, _git_lock(OLD))
    responses = _behind_responses(vault)
    responses["diff --cached"] = cp(1, "")
    fake = FakeRun(responses)

    reasons = worker.bump_vault_engine_pin(engine=engine, vault=vault, run=fake)

    assert reasons == []
    assert _uv_keys(fake) == []
    assert "commit" not in fake.keys
    assert len(capsys.readouterr().out.splitlines()) == 1


def test_no_pyproject_noop(tmp_path):
    engine, vault = _layout(tmp_path, _git_lock(OLD), pyproject=False)
    fake = FakeRun()

    assert worker.bump_vault_engine_pin(engine=engine, vault=vault, run=fake) == []
    assert fake.calls == []


def test_no_uv_lock_noop(tmp_path):
    engine, vault = _layout(tmp_path, lock=None)
    fake = FakeRun()

    assert worker.bump_vault_engine_pin(engine=engine, vault=vault, run=fake) == []
    assert fake.calls == []


@pytest.mark.parametrize(
    "lock",
    [
        _lock('{ registry = "https://pypi.org/simple" }').replace('name = "macp"', 'name = "other"'),
        _lock('{ registry = "https://pypi.org/simple" }'),
        _lock('{ editable = "../macp" }'),
        _lock('{ git = "https://example.invalid/macp" }'),  # no #sha fragment
        _lock('{ git = "https://example.invalid/macp#main" }'),  # not a 40-hex sha
        "this is [[ not toml",
    ],
    ids=["no-macp", "registry", "editable", "no-fragment", "non-sha", "malformed"],
)
def test_lock_without_macp_git_source_noop(tmp_path, lock):
    engine, vault = _layout(tmp_path, lock)
    fake = FakeRun()

    assert worker.bump_vault_engine_pin(engine=engine, vault=vault, run=fake) == []
    assert fake.calls == []


def test_no_engine_checkout_noop(tmp_path):
    engine, vault = _layout(tmp_path, _git_lock(OLD))
    (engine / ".git").rmdir()  # wheel install / non-checkout
    fake = FakeRun()

    assert worker.bump_vault_engine_pin(engine=engine, vault=vault, run=fake) == []
    assert worker.bump_vault_engine_pin(engine=vault, vault=vault, run=fake) == []  # combined
    assert fake.calls == []


def test_pin_already_at_engine_head_noop(tmp_path, capsys):
    engine, vault = _layout(tmp_path, _git_lock(OLD))
    responses = _behind_responses(vault)
    responses["rev-parse"] = cp(0, OLD + "\n")
    fake = FakeRun(responses)

    assert worker.bump_vault_engine_pin(engine=engine, vault=vault, run=fake) == []
    assert _uv_keys(fake) == []
    assert "merge-base" not in fake.keys
    assert capsys.readouterr().out == ""  # healthy tick stays quiet


def test_engine_behind_pin_skips(tmp_path):
    engine, vault = _layout(tmp_path, _git_lock(OLD))
    responses = _behind_responses(vault)
    responses["merge-base"] = cp(1, "")  # pin ahead of / diverged from engine HEAD
    fake = FakeRun(responses)

    assert worker.bump_vault_engine_pin(engine=engine, vault=vault, run=fake) == []
    assert _uv_keys(fake) == []


def test_uv_missing_skips_quietly(tmp_path, capsys):
    engine, vault = _layout(tmp_path, _git_lock(OLD))
    responses = _behind_responses(vault)
    responses["uv lock"] = FileNotFoundError("uv")
    fake = FakeRun(responses)

    assert worker.bump_vault_engine_pin(engine=engine, vault=vault, run=fake) == []
    assert "add" not in fake.keys
    assert "commit" not in fake.keys
    assert "uv 없음" in capsys.readouterr().out


def test_lock_unchanged_no_commit(tmp_path):
    engine, vault = _layout(tmp_path, _git_lock(OLD))
    responses = _behind_responses(vault)
    responses["uv lock"] = cp(0, "")  # origin not there yet: lock did not move
    responses["diff -- uv.lock"] = cp(0, "")
    fake = FakeRun(responses)

    assert worker.bump_vault_engine_pin(engine=engine, vault=vault, run=fake) == []
    assert "uv sync" not in fake.keys
    assert "add" not in fake.keys
    assert "commit" not in fake.keys


def _assert_restored(fake: FakeRun, vault) -> None:
    restores = [argv for argv, _ in fake.calls if argv[0] == "git" and argv[3] == "checkout"]
    assert restores == [["git", "-C", str(vault), "checkout", "HEAD", "--", "uv.lock"]]


def test_uv_lock_failure_reports_and_restores(tmp_path):
    engine, vault = _layout(tmp_path, _git_lock(OLD))
    responses = _behind_responses(vault)
    responses["uv lock"] = cp(1, "")
    fake = FakeRun(responses)

    reasons = worker.bump_vault_engine_pin(engine=engine, vault=vault, run=fake)

    assert reasons
    assert "uv sync" not in fake.keys
    _assert_restored(fake, vault)


def test_uv_sync_failure_reports_and_restores(tmp_path):
    engine, vault = _layout(tmp_path, _git_lock(OLD))
    responses = _behind_responses(vault)
    responses["uv sync"] = cp(1, "")
    fake = FakeRun(responses)

    reasons = worker.bump_vault_engine_pin(engine=engine, vault=vault, run=fake)

    assert reasons
    assert "add" not in fake.keys
    assert "commit" not in fake.keys
    _assert_restored(fake, vault)


def test_commit_failure_reports_and_restores(tmp_path):
    engine, vault = _layout(tmp_path, _git_lock(OLD))
    responses = _behind_responses(vault)
    responses["commit"] = cp(1, "")
    fake = FakeRun(responses)

    reasons = worker.bump_vault_engine_pin(engine=engine, vault=vault, run=fake)

    assert reasons
    _assert_restored(fake, vault)
