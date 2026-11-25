"""The engine edits lite mode makes (docs/design/lite-mode.md, D14), each pinned
from both sides: what changes for a lite vault, and that HQ mode does not move.

* ``ProjectConfig.vault_root`` — written only when set, read back, and honoured by
  ``paths.vault_root()`` from inside the registered repository.
* ``orchestrator._pull_main`` — a vault with no ``origin`` stops after the first
  failed fetch, silently; a vault whose ``origin`` exists keeps the retry path.
* ``stages.build_stage_argv`` — the "origin is a local path" line appears only for
  a path ``repo``, never for a URL.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from todo_system import paths
from todo_system.macp import config as C
from todo_system.orchestrator import orchestrator as orch_mod
from todo_system.orchestrator import stages

LOCAL_ORIGIN_MARK = "origin is a local path"


# --- ProjectConfig.vault_root -------------------------------------------------


def test_project_file_without_vault_root_is_byte_identical(tmp_path):
    path = C.write_project(tmp_path, C.ProjectConfig(name="x"))
    assert path.read_text(encoding="utf-8") == (
        'profile = "default"\nworker_mode = "auto"\ncapabilities = []\nname = "x"\n')
    assert C.load_project(tmp_path).vault_root == ""


def test_vault_root_round_trips_and_resolves_from_inside_the_repo(tmp_path, monkeypatch):
    home = tmp_path / "home"
    vault = home / ".macp" / "vault"
    (vault / "Todo" / "items").mkdir(parents=True)
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)

    C.write_project(repo, C.ProjectConfig(vault_root="~/.macp/vault"))

    text = (repo / ".macp" / "config.toml").read_text(encoding="utf-8")
    assert text.endswith('vault_root = "~/.macp/vault"\n')
    assert C.load_project(repo).vault_root == "~/.macp/vault"
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(home))
    assert paths.vault_root(start=repo / "src") == vault


# --- _pull_main on a vault with no origin -------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _vault_repo(root: Path) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "commit", "-q", "--allow-empty", "-m", "init")
    return root


def _recording_run(calls: list):
    def run(argv, **kw):
        calls.append(list(argv[1:]))
        return subprocess.run(argv, **kw)
    return run


def test_pull_main_without_origin_returns_quietly_after_one_fetch(tmp_path, capsys):
    vault = _vault_repo(tmp_path / "vault")
    calls: list = []
    sleeps: list = []

    orch_mod._pull_main(vault, _run=_recording_run(calls), _sleep=sleeps.append)

    assert capsys.readouterr().out == ""
    assert [c[0] for c in calls] == ["rev-parse", "fetch", "remote"]
    assert sleeps == []


def test_pull_main_with_an_unreachable_origin_keeps_retrying(tmp_path, capsys):
    vault = _vault_repo(tmp_path / "vault")
    _git(vault, "remote", "add", "origin", str(tmp_path / "missing.git"))
    calls: list = []
    sleeps: list = []

    orch_mod._pull_main(vault, _run=_recording_run(calls), _sleep=sleeps.append)

    assert "hq-host pull: non-fatal" in capsys.readouterr().out
    assert [c[0] for c in calls] == ["rev-parse", "fetch", "remote", "fetch", "fetch"]
    assert sleeps == [1, 1]


# --- stage metadata for a path repo -------------------------------------------


def _quick_prompt(monkeypatch, repo: str) -> str:
    monkeypatch.setattr(stages, "stage_model", lambda stage="implement", tier=None: "m")
    argv = stages.build_stage_argv(
        "quick", task_id="t", run_id="r", run_dir=Path("/v/Worker/runs/r"),
        branch="worker/t", repo=repo, working_dir="/w/t.wt/t",
        worker_root=Path("/nonexistent-worker-root"))
    return argv[2]


def test_path_repo_prompt_says_origin_is_local(monkeypatch):
    prompt = _quick_prompt(monkeypatch, "/home/someone/repo")
    assert LOCAL_ORIGIN_MARK in prompt
    assert "do not call `gh`" in prompt


@pytest.mark.parametrize("url", ["https://github.com/o/r.git", "git@github.com:o/r.git",
                                 "ssh://git@example.test/r.git"])
def test_url_repo_prompt_is_unchanged(monkeypatch, url):
    assert LOCAL_ORIGIN_MARK not in _quick_prompt(monkeypatch, url)


def test_is_url_remote_matches_the_runner_rule():
    assert stages.is_url_remote("https://h/r.git")
    assert stages.is_url_remote("git@h:o/r.git")
    assert not stages.is_url_remote("/abs/path")
    assert not stages.is_url_remote("")
    assert not stages.is_url_remote(None)
