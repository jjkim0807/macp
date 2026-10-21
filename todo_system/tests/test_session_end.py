"""Tests for ``todo_system.session_end`` — the CLAUDE.md §10 one-shot checklist.

Convention mirrors ``test_session_log.py``: real throwaway git repos in
``tmp_path`` rather than mocks (the failure mode worth catching is "session-end
mis-commits or mis-classifies against a real working tree"), and the shared
``isolated_vault`` fixture for anything that records/commits a journal, so an
unpatched run can never ``git commit`` into the developer's real vault.

Pure classification/sync/daemon helpers are exercised against bare ``tmp_path``
git repos with an explicit ``root`` argument, so they need no vault redirection.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from todo_system import paths, session_end, session_log


# ---------------------------------------------------------------------------
# git helpers (real repos in tmp_path) — copied from test_session_log to keep
# that module untouched.
# ---------------------------------------------------------------------------


def _run_git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(path)], capture_output=True, check=True
    )
    _run_git(path, "config", "user.email", "test@example.com")
    _run_git(path, "config", "user.name", "Test User")
    _run_git(path, "config", "commit.gpgsign", "false")
    _run_git(path, "config", "core.hooksPath", str(path / ".no-hooks"))
    return path


def _commit(repo: Path, filename: str, subject: str) -> str:
    (repo / filename).write_text(filename, encoding="utf-8")
    _run_git(repo, "add", filename)
    _run_git(repo, "commit", "-q", "-m", subject)
    return _run_git(repo, "rev-parse", "HEAD")


def _write(repo: Path, rel: str, content: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# classify_status (AC: the §10.1 buckets)
# ---------------------------------------------------------------------------


def test_classify_buckets_worker_as_ignorable_and_notes_as_must_commit(tmp_path):
    # Seed the dirs as tracked first — as they are in the real vault — so git
    # reports individual paths (`` M Notes/keep.md``, ``?? Worker/foo``) rather than
    # collapsing a wholly-untracked dir to ``?? Notes/``. The modified tracked file
    # also exercises the leading-space `` M`` porcelain code (the strip regression).
    repo = _init_repo(tmp_path / "repo")
    _write(repo, "Notes/keep.md", "existing\n")
    _write(repo, "Worker/keep", "existing\n")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "seed vault dirs")

    _write(repo, "Notes/keep.md", "edited\n")  # ` M Notes/keep.md`
    _write(repo, "Worker/foo", "runtime churn\n")  # `?? Worker/foo`

    buckets = session_end.classify_status(repo)

    assert buckets["must_commit"] == ["Notes/keep.md"]
    assert buckets["ignorable"] == ["Worker/foo"]


def test_classify_marks_gitignored_paths_as_ignored(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write(repo, ".gitignore", "Todo/todo.db\n")
    _write(repo, "Todo/keep.md", "existing\n")  # keeps Todo/ from collapsing
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "seed gitignore")
    _write(repo, "Todo/todo.db", "binary-ish\n")

    buckets = session_end.classify_status(repo)

    assert "Todo/todo.db" in buckets["ignored"]
    assert "Todo/todo.db" not in buckets["must_commit"]


# ---------------------------------------------------------------------------
# Memory/index.md is classified by its DIFF, not its path (AC#5 / R1 §4)
# ---------------------------------------------------------------------------

_INDEX_BEFORE = "---\nlast_synced: 2026-09-04T10:00:00\nfoo: bar\n---\n\nbody\n"


def _seed_index(repo: Path) -> None:
    _write(repo, "Memory/index.md", _INDEX_BEFORE)
    _run_git(repo, "add", "Memory/index.md")
    _run_git(repo, "commit", "-q", "-m", "seed index")


def test_index_md_last_synced_only_change_is_ignorable(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _seed_index(repo)
    _write(repo, "Memory/index.md", _INDEX_BEFORE.replace("10:00:00", "11:00:00"))

    assert session_end._index_only_last_synced(repo) is True
    buckets = session_end.classify_status(repo)
    assert buckets["ignorable"] == ["Memory/index.md"]
    assert buckets["must_commit"] == []


def test_index_md_other_line_change_is_must_commit(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _seed_index(repo)
    changed = _INDEX_BEFORE.replace("10:00:00", "11:00:00").replace("foo: bar", "foo: CHANGED")
    _write(repo, "Memory/index.md", changed)

    assert session_end._index_only_last_synced(repo) is False
    buckets = session_end.classify_status(repo)
    assert buckets["must_commit"] == ["Memory/index.md"]
    assert buckets["ignorable"] == []


def test_index_md_untracked_is_must_commit(tmp_path):
    """An index.md that never made it into HEAD has an empty diff → not the
    daemon's last_synced churn → the human must commit it."""
    repo = _init_repo(tmp_path / "repo")
    _write(repo, "Memory/keep.md", "existing\n")  # keeps Memory/ from collapsing
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-q", "-m", "seed")
    _write(repo, "Memory/index.md", _INDEX_BEFORE)  # untracked

    assert session_end._index_only_last_synced(repo) is False
    assert "Memory/index.md" in session_end.classify_status(repo)["must_commit"]


# ---------------------------------------------------------------------------
# sync_status (report only; ahead>0 is normal)
# ---------------------------------------------------------------------------


def test_sync_status_reports_behind_and_ahead(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    base = _commit(repo, "a.txt", "base")
    _run_git(repo, "update-ref", "refs/remotes/origin/main", base)
    assert session_end.sync_status(repo) == (0, 0)

    _commit(repo, "b.txt", "ahead one")
    assert session_end.sync_status(repo) == (0, 1)


def test_sync_status_is_none_without_an_origin_main_ref(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _commit(repo, "a.txt", "base")
    assert session_end.sync_status(repo) is None


# ---------------------------------------------------------------------------
# daemon liveness (report only; never restart)
# ---------------------------------------------------------------------------


def test_parse_daemon_pids_reads_the_pid_column():
    text = (
        "PID\tStatus\tLabel\n"
        "12345\t0\tcom.example.secondbrain.todo-sync\n"
        "-\t0\tcom.example.secondbrain.worker\n"
        "999\t0\tcom.apple.something.else\n"  # non-secondbrain → skipped
    )
    assert session_end._parse_daemon_pids(text) == {
        "com.example.secondbrain.todo-sync": 12345,
        "com.example.secondbrain.worker": None,  # "-" → loaded but not running
    }


def test_daemon_pids_parses_a_fake_launchctl_listing(monkeypatch):
    fake = subprocess.CompletedProcess(
        args=["launchctl", "list"],
        returncode=0,
        stdout="123\t0\tcom.example.secondbrain.todo-sync\n",
        stderr="",
    )
    # Exercise the launchctl branch on either host: on Linux _daemon_pids would
    # otherwise early-return via daemonctl.loaded_pids and never reach the injected
    # subprocess.run. Forcing IS_DARWIN keeps the original mac assertion live.
    monkeypatch.setattr(session_end.daemonctl, "IS_DARWIN", True)
    monkeypatch.setattr(session_end.subprocess, "run", lambda *a, **k: fake)
    assert session_end._daemon_pids() == {"com.example.secondbrain.todo-sync": 123}


def test_daemon_pids_returns_none_when_launchctl_is_absent(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("launchctl not installed")

    # Force the launchctl branch so the FileNotFoundError path is what runs on
    # Linux too (else _daemon_pids early-returns via daemonctl.loaded_pids → {}).
    monkeypatch.setattr(session_end.daemonctl, "IS_DARWIN", True)
    monkeypatch.setattr(session_end.subprocess, "run", _boom)
    assert session_end._daemon_pids() is None


def test_daemon_pids_on_systemd_path_matches_the_org_prefix(monkeypatch):
    """Regression: daemonctl.loaded_pids filters by str.startswith(prefix), but
    every daemonctl.UNITS label starts with the org prefix (here the injected
    "com.example."), not the bare substring "secondbrain" that _DAEMON_MATCH
    holds. Passing that substring as the prefix made the non-darwin branch always
    return {}, silently hiding every live daemon in the session-end report.
    The prefix is injected (UNITS + _DAEMON_UNIT_PREFIX) so the case does not
    lean on the cwd vault's config."""
    monkeypatch.setattr(session_end.daemonctl, "IS_DARWIN", False)
    monkeypatch.setattr(session_end.daemonctl, "UNITS",
                        session_end.daemonctl.build_units("com.example."))
    monkeypatch.setattr(session_end, "_DAEMON_UNIT_PREFIX", "com.example.secondbrain.")

    def _fake_run(cmd, timeout):
        unit = cmd[-1]
        if unit == "macp-todo-sync.service":
            stdout = "LoadState=loaded\nMainPID=456\n"
        else:
            stdout = "LoadState=not-found\nMainPID=0\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(session_end.daemonctl, "_run", _fake_run)
    pids = session_end._daemon_pids()
    assert pids == {"com.example.secondbrain.todo-sync": 456}


# ---------------------------------------------------------------------------
# cmd_session_end — dry-run (classify/report only)
# ---------------------------------------------------------------------------


def test_dry_run_classifies_without_recording_or_committing(isolated_vault, monkeypatch, capsys):
    # Guard: both roots point at the tmp vault before anything can git-write.
    assert session_log.ROOT == isolated_vault.root
    monkeypatch.setattr(session_end, "ROOT", isolated_vault.root)
    _init_repo(isolated_vault.root)
    _write(isolated_vault.root, "Notes/keep.md", "existing\n")  # tracked so Notes/ won't collapse
    _run_git(isolated_vault.root, "add", "-A")
    _run_git(isolated_vault.root, "commit", "-q", "-m", "seed")
    base = _run_git(isolated_vault.root, "rev-parse", "HEAD")
    _write(isolated_vault.root, "Notes/wip.md", "unsaved\n")  # a must_commit file

    rc = session_end.main(["session-end", "--project", "demo", "--dry-run"])

    assert rc == 1  # non-exempt file present → non-zero even in dry-run
    assert _run_git(isolated_vault.root, "rev-parse", "HEAD") == base  # no new commit
    assert list(isolated_vault.sessions.glob("*.md")) == []  # no journal written
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "무시" in out and "처리할 것" in out
    assert "Notes/wip.md" in out


# ---------------------------------------------------------------------------
# cmd_session_end — exit code contract
# ---------------------------------------------------------------------------


def test_exit_nonzero_when_a_must_commit_file_remains_after_recording(
    isolated_vault, monkeypatch, capsys
):
    assert session_log.ROOT == isolated_vault.root
    monkeypatch.setattr(session_end, "ROOT", isolated_vault.root)
    root = isolated_vault.root
    _init_repo(root)
    _write(root, "Notes/keep.md", "existing\n")  # tracked so Notes/ won't collapse
    _run_git(root, "add", "-A")
    _run_git(root, "commit", "-q", "-m", "seed")
    _write(root, "Notes/wip.md", "unsaved content\n")  # not staged by the journal commit

    rc = session_end.main(["session-end", "--project", "demo", "--note", "n"])

    assert rc == 1
    # the journal WAS committed, but the content file was deliberately left for the human
    assert (
        _run_git(root, "log", "-1", "--format=%s")
        == "chore(session): demo manual-work checkpoint"
    )
    assert "?? Notes/wip.md" in _run_git(root, "status", "--porcelain")
    assert "Notes/wip.md" in capsys.readouterr().out


def test_exit_zero_on_a_clean_tree_after_the_journal_commit(isolated_vault, monkeypatch, capsys):
    assert session_log.ROOT == isolated_vault.root
    monkeypatch.setattr(session_end, "ROOT", isolated_vault.root)
    root = isolated_vault.root
    _init_repo(root)
    _commit(root, "seed.txt", "seed")

    rc = session_end.main(["session-end", "--project", "demo", "--note", "n"])

    assert rc == 0  # journal committed; nothing left in must_commit
    assert "처리할 것: 없음" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_session_end — ahead>0 regression (AC#4): ahead of origin ≠ failure
# ---------------------------------------------------------------------------


def test_ahead_of_origin_still_exits_zero_and_reports_ahead(isolated_vault, monkeypatch, capsys):
    assert session_log.ROOT == isolated_vault.root
    monkeypatch.setattr(session_end, "ROOT", isolated_vault.root)
    root = isolated_vault.root
    _init_repo(root)
    base = _commit(root, "seed.txt", "seed")
    _run_git(root, "update-ref", "refs/remotes/origin/main", base)
    _commit(root, "feature.txt", "ahead of origin")  # ahead=1 before session-end runs

    rc = session_end.main(["session-end", "--project", "demo", "--note", "n"])

    assert rc == 0  # ahead>0 must NOT force a non-zero exit
    behind, ahead = session_end.sync_status(root)
    assert (behind, ahead) == (0, 2)  # the pre-existing commit + the journal commit
    assert f"ahead {ahead}" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_session_end — end-to-end journal recording (journal only, never content)
# ---------------------------------------------------------------------------


def test_end_to_end_records_and_commits_only_the_journal(isolated_vault, monkeypatch, capsys):
    assert session_log.ROOT == isolated_vault.root
    monkeypatch.setattr(session_end, "ROOT", isolated_vault.root)
    monkeypatch.setenv("MACP_HOST_ID", "host-a")  # journal names carry the host
    root = isolated_vault.root
    _init_repo(root)
    _commit(root, "seed.txt", "seed")

    rc = session_end.main(
        ["session-end", "--project", "demo", "--note", "did stuff", "--task", "some-task"]
    )

    assert rc == 0
    assert (
        _run_git(root, "log", "-1", "--format=%s")
        == "chore(session): demo manual-work checkpoint"
    )
    committed = _run_git(root, "show", "--name-only", "--format=", "HEAD").split()
    assert len(committed) == 1
    assert committed[0].startswith("Memory/sessions/") and committed[0].endswith("-demo.host-a.md")

    journals = list(isolated_vault.sessions.glob("*-demo.*.md"))
    assert len(journals) == 1
    text = journals[0].read_text(encoding="utf-8")
    assert "did stuff" in text
    assert "[[Todo/items/some-task]]" in text

    out = capsys.readouterr().out
    assert "커밋됨(저널):" in out
    assert "처리할 것: 없음" in out

    # The committed journal is not itself reported as an outstanding must_commit.
    assert session_end.classify_status(root)["must_commit"] == []


# ---------------------------------------------------------------------------
# CLI wiring (imported lazily — cli.py pulls google deps present only under uv)
# ---------------------------------------------------------------------------


def test_cli_parser_wires_the_session_end_subcommand():
    from todo_system import cli

    args = cli.build_parser().parse_args(
        ["session-end", "--project", "demo", "--task", "t1", "--dry-run"]
    )
    assert args.func is session_end.cmd_session_end
    assert args.project == "demo"
    assert args.dry_run is True
    assert args.task == ["t1"]


# ---------------------------------------------------------------------------
# ROOT now flows through the shared paths.vault_root resolver (public-split
# follow-up). Two properties pin the migration: (b) env-unset it is byte-identical
# to the old Path(__file__).resolve().parents[1], and (a) the exact expression the
# module uses is redirectable via MACP_VAULT_ROOT. ROOT is an import-time constant,
# so we assert against the resolver called with the module's own start= anchor
# rather than reloading (reload would risk polluting the module globals the rest of
# the suite monkeypatches).
# ---------------------------------------------------------------------------


# byte-identity holds only in the combined vault layout (a .macp/config.toml marker
# at the repo root); an engine-only export tree has no marker and vault_root() raises
# by design, so this case skips there (public-split-10 AC2 vault-only / AC4). The live
# vault has the marker, so it runs the assertion exactly as before.
_vault_only_byte_identity = pytest.mark.skipif(
    not (Path(paths.__file__).resolve().parents[1] / ".macp" / "config.toml").is_file(),
    reason="vault-only: byte-identity holds only in the combined vault layout "
           "(.macp/config.toml at the repo root); the engine-only export tree has no marker.",
)


@_vault_only_byte_identity
def test_root_is_byte_identical_repo_root_when_env_unset(monkeypatch):
    env = os.environ.get("MACP_VAULT_ROOT")    # what the module saw at import
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    install = Path(session_end.__file__).resolve().parents[1]
    # The .macp marker at the install root makes the resolver return that root
    # unchanged — exactly the value the old hard-coded parents[1] produced.
    assert paths.vault_root(start=install) == install
    assert (install / "todo_system" / "session_end.py").is_file()  # a real root
    # ...and the module actually holds that value (the env vault instead when
    # a worker stage set MACP_VAULT_ROOT before import).
    assert session_end.ROOT == (Path(env).expanduser() if env else install)


def test_root_is_redirectable_via_macp_vault_root_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_VAULT_ROOT", str(tmp_path))
    install = Path(session_end.__file__).resolve().parents[1]
    # Same expression session_end.py:38 evaluates — env wins over the anchor.
    assert paths.vault_root(start=install) == tmp_path
