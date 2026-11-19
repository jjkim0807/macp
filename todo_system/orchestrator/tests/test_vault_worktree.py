"""The vault task's worktree: where it lives, how it is cut, and what a run does with it.

A task with no `repo:` works on the vault repository itself. Its branch is no
longer checked out in the live vault (that moved the live checkout's HEAD for the
whole run — see test_vault_run_keeps_live_head.py); the runner cuts a linked
worktree of the vault under the worker root instead and removes it when the run
ends.

Everything here runs against temporary repositories. The suite-wide fixture
(`todo_system/conftest.py`, `_vault_worktree_isolated`) keeps any worktree out
of the real worker root and off the real vault; its own behaviour is pinned at
the bottom of the helpers section.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from todo_system import paths
from todo_system.orchestrator import repos, stages

MAIN_REF = "refs/heads/main"


def _git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=check, capture_output=True, text=True)


def _commit(repo, name, text, msg):
    path = Path(repo) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _init(repo: Path, *, commit: bool = True) -> Path:
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        _git(repo, "config", k, v)
    if commit:
        _commit(repo, "main.txt", "v1", "init")
    return repo


def _head_ref(repo) -> str:
    return _git(repo, "symbolic-ref", "-q", "HEAD", check=False).stdout.strip()


def _worktrees(repo) -> dict[str, dict[str, str]]:
    """`git worktree list --porcelain` as {resolved path: {key: value}}."""
    out: dict[str, dict[str, str]] = {}
    for block in _git(repo, "worktree", "list", "--porcelain").stdout.strip().split("\n\n"):
        rec: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            rec[key] = value
        if "worktree" in rec:
            out[str(Path(rec["worktree"]).resolve())] = rec
    return out


@pytest.fixture
def vault(tmp_path) -> Path:
    """A vault with one pushed commit and a bare origin, like the live layout."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    repo = tmp_path / "vault"
    subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        _git(repo, "config", k, v)
    _commit(repo, "main.txt", "v1", "init")
    _git(repo, "push", "origin", "main")
    return repo


# --- where it lives -----------------------------------------------------------------


def test_the_path_is_worker_root_vault_wt_slug(tmp_path):
    assert repos.vault_worktree_path("t", worker_root=tmp_path) == tmp_path / "vault.wt" / "t"
    assert repos.VAULT_WORKTREE_DIRNAME == "vault.wt"


def test_the_shape_is_what_identifies_a_vault_worktree(tmp_path):
    assert repos.is_vault_worktree(repos.vault_worktree_path("t", worker_root=tmp_path))
    assert repos.is_vault_worktree(str(tmp_path / "vault.wt" / "some-slug"))
    assert not repos.is_vault_worktree(tmp_path / "vault")              # the vault itself
    assert not repos.is_vault_worktree(tmp_path / "demo.wt" / "t")      # an external repo's
    assert not repos.is_vault_worktree(tmp_path / "vault.wt")           # the parent, not a leaf
    assert not repos.is_vault_worktree("")
    assert not repos.is_vault_worktree(None)                            # never raises


# --- how it is cut ------------------------------------------------------------------


def test_the_worktree_is_added_detached_and_the_vault_head_does_not_move(vault, tmp_path):
    log: list[str] = []
    main_sha = _git(vault, "rev-parse", "main").stdout.strip()
    before = (_head_ref(vault), _git(vault, "status", "--porcelain").stdout)

    wt = repos.ensure_vault_worktree(vault, "t", worker_root=tmp_path / "wr", log=log)

    assert wt == tmp_path / "wr" / "vault.wt" / "t"
    rec = _worktrees(vault)[str(wt.resolve())]
    assert "detached" in rec and rec["HEAD"] == main_sha
    assert (_head_ref(vault), _git(vault, "status", "--porcelain").stdout) == before
    assert before[0] == MAIN_REF
    assert (wt / "main.txt").read_text(encoding="utf-8") == "v1"
    assert "added" in log[-1]


def test_uncommitted_work_in_the_vault_is_left_exactly_as_it_is(vault, tmp_path):
    (vault / "main.txt").write_text("half-written by a person", encoding="utf-8")
    (vault / "draft.md").write_text("untracked", encoding="utf-8")
    status = _git(vault, "status", "--porcelain").stdout

    wt = repos.ensure_vault_worktree(vault, "t", worker_root=tmp_path / "wr")

    assert wt is not None
    assert (vault / "main.txt").read_text(encoding="utf-8") == "half-written by a person"
    assert (vault / "draft.md").read_text(encoding="utf-8") == "untracked"
    assert _git(vault, "status", "--porcelain").stdout == status
    # the worktree holds what is COMMITTED, not the live tree's edits
    assert (wt / "main.txt").read_text(encoding="utf-8") == "v1"
    assert not (wt / "draft.md").exists()


def test_it_is_cut_from_the_local_default_branch_not_from_origin(vault, tmp_path):
    """A commit that is not pushed yet (the task file a person just committed, a
    sync commit) has to be in the run's tree."""
    sha = _commit(vault, "Todo/items/t.md", "# t\n", "task md, not pushed")
    wt = repos.ensure_vault_worktree(vault, "t", worker_root=tmp_path / "wr")
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == sha
    assert (wt / "Todo" / "items" / "t.md").exists()


def test_a_live_checkout_that_is_elsewhere_is_not_the_base(vault, tmp_path):
    main_sha = _git(vault, "rev-parse", "main").stdout.strip()
    _git(vault, "checkout", "-b", "someone/feature")
    _commit(vault, "feature.txt", "x", "feature work")

    wt = repos.ensure_vault_worktree(vault, "t", worker_root=tmp_path / "wr")

    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == main_sha
    assert not (wt / "feature.txt").exists()
    assert _head_ref(vault) == "refs/heads/someone/feature"       # and it stays where it was


def test_a_second_call_returns_the_same_worktree(vault, tmp_path):
    first = repos.ensure_vault_worktree(vault, "t", worker_root=tmp_path / "wr")
    (first / "wip.txt").write_text("in progress", encoding="utf-8")
    again = repos.ensure_vault_worktree(vault, "t", worker_root=tmp_path / "wr")
    assert again == first
    assert (first / "wip.txt").read_text(encoding="utf-8") == "in progress"
    assert len(_worktrees(vault)) == 2                    # the vault + one worktree


def test_a_vault_with_no_commit_gets_none_and_a_reason(tmp_path):
    repo = _init(tmp_path / "vault", commit=False)
    log: list[str] = []
    assert repos.ensure_vault_worktree(repo, "t", worker_root=tmp_path / "wr", log=log) is None
    assert "no commit" in log[-1]
    assert not (tmp_path / "wr" / "vault.wt" / "t").exists()


def test_a_directory_that_is_not_a_repository_gets_none(tmp_path):
    plain = tmp_path / "vault"
    plain.mkdir()
    log: list[str] = []
    assert repos.ensure_vault_worktree(plain, "t", worker_root=tmp_path / "wr", log=log) is None
    assert "not the top of a git checkout" in log[-1]


def test_a_vault_inside_another_repository_never_gets_that_repositorys_worktree(tmp_path):
    """The gate command's `MACP_VAULT_ROOT=vault-template` is exactly this shape:
    a directory inside the engine checkout. `git -C <it>` answers for the engine."""
    outer = _init(tmp_path / "engine")
    inner = outer / "vault-template"
    inner.mkdir()
    (inner / "README.md").write_text("skeleton", encoding="utf-8")
    _git(outer, "add", "-A")
    _git(outer, "commit", "-m", "skeleton")
    before = _worktrees(outer)
    log: list[str] = []

    assert repos.ensure_vault_worktree(inner, "t", worker_root=tmp_path / "wr", log=log) is None

    assert "not the top of a git checkout" in log[-1]
    assert _worktrees(outer) == before
    assert not (tmp_path / "wr").exists()


def test_a_path_that_is_already_something_else_is_never_touched(vault, tmp_path):
    target = tmp_path / "wr" / "vault.wt" / "t"
    target.mkdir(parents=True)
    (target / "notes.txt").write_text("someone's file", encoding="utf-8")
    log: list[str] = []

    assert repos.ensure_vault_worktree(vault, "t", worker_root=tmp_path / "wr", log=log) is None

    assert "add failed" in log[-1]
    assert (target / "notes.txt").read_text(encoding="utf-8") == "someone's file"
    assert len(_worktrees(vault)) == 1


def test_another_repositorys_worktree_at_the_path_is_refused(vault, tmp_path):
    """A repo literally named `vault` keeps ITS run worktrees under the same
    `vault.wt/`. Branch prep must never run in another repository's tree."""
    other = _init(tmp_path / "other")
    target = tmp_path / "wr" / "vault.wt" / "t"
    target.parent.mkdir(parents=True)
    _git(other, "worktree", "add", "--detach", str(target))
    log: list[str] = []

    assert repos.ensure_vault_worktree(vault, "t", worker_root=tmp_path / "wr", log=log) is None

    assert "not a worktree of" in log[-1]
    assert str(target.resolve()) in _worktrees(other)          # still the other repo's
    assert len(_worktrees(vault)) == 1


def test_a_vault_checkout_sitting_on_the_runs_branch_is_refused(vault, tmp_path):
    _git(vault, "checkout", "-b", "worker/t")
    log: list[str] = []
    wt = repos.ensure_vault_worktree(vault, "t", worker_root=tmp_path / "wr", log=log,
                                     branch="worker/t")
    assert wt is None
    assert "is on worker/t" in log[-1]
    assert _head_ref(vault) == "refs/heads/worker/t"           # not this function's to move
    assert len(_worktrees(vault)) == 1


def test_a_git_timeout_is_a_reason_not_an_exception(vault, tmp_path):
    log: list[str] = []
    with mock.patch("subprocess.run",
                    side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=1)):
        assert repos.ensure_vault_worktree(vault, "t", worker_root=tmp_path / "wr",
                                           log=log) is None
    assert "timed out" in log[-1]


# --- the branch goes on the worktree, never on the vault ------------------------------


def test_branch_prep_on_the_worktree_leaves_the_vault_on_main(vault, tmp_path):
    wt = repos.ensure_vault_worktree(vault, "t", worker_root=tmp_path / "wr")

    vp = stages.prepare_vault_branch(wt, "worker/t")

    assert vp.problem is None
    assert _git(wt, "branch", "--show-current").stdout.strip() == "worker/t"
    assert _head_ref(vault) == MAIN_REF
    # and git itself now refuses the stage-prompt line that used to move the vault
    assert _git(vault, "checkout", "worker/t", check=False).returncode != 0
    assert _head_ref(vault) == MAIN_REF


def test_branch_prep_on_the_worktree_continues_a_pushed_branch(vault, tmp_path):
    """The retry rule is unchanged by the move: origin/<branch> is continued, not
    re-cut, or the next push could never fast-forward."""
    _git(vault, "checkout", "-b", "worker/t")
    prior = _commit(vault, "prior.txt", "prior attempt", "prior attempt commit")
    _git(vault, "push", "origin", "worker/t")
    _git(vault, "checkout", "main")
    _git(vault, "branch", "-D", "worker/t")
    _git(vault, "update-ref", "-d", "refs/remotes/origin/worker/t")
    wt = repos.ensure_vault_worktree(vault, "t", worker_root=tmp_path / "wr")

    vp = stages.prepare_vault_branch(wt, "worker/t")

    assert vp.problem is None and vp.base == "origin/worker/t"
    assert _git(wt, "rev-parse", "HEAD").stdout.strip() == prior
    assert _head_ref(vault) == MAIN_REF


# --- the suite-wide isolation ---------------------------------------------------------


def test_the_default_location_is_redirected_out_of_the_real_worker_root():
    real_root = paths.worker_root().resolve()
    here = repos.vault_worktree_path("t").resolve()
    assert real_root not in here.parents
    assert here.parent.name == repos.VAULT_WORKTREE_DIRNAME     # the shape is kept


def test_a_real_root_gets_a_stand_in_and_no_git(real_vault_worktree):
    real_run = subprocess.run
    calls: list[list[str]] = []

    def spy(argv, *a, **kw):
        calls.append(list(argv))
        return real_run(argv, *a, **kw)

    with mock.patch("subprocess.run", side_effect=spy):
        got = repos.ensure_vault_worktree(stages._VAULT_DATA_ROOT, "t", branch="worker/t")
        removed = stages.remove_worktree(stages._VAULT_DATA_ROOT, got)

    assert calls == []
    assert got.is_dir() and not (got / ".git").exists()
    assert not repos.is_vault_worktree(got)
    assert removed is False
    assert str(Path(stages._VAULT_DATA_ROOT).resolve()) in real_vault_worktree["roots"]


def test_a_test_that_replaced_branch_prep_gets_the_stand_in_for_its_own_vault(vault):
    with mock.patch.object(stages, "prepare_vault_branch"):
        got = repos.ensure_vault_worktree(vault, "t")
    assert not (got / ".git").exists()
    assert len(_worktrees(vault)) == 1                    # nothing was registered
    # with the real branch prep back, the same vault gets a real worktree
    real = repos.ensure_vault_worktree(vault, "t")
    assert (real / ".git").exists() and repos.is_vault_worktree(real)
    assert stages.remove_worktree(vault, real) is True
    assert len(_worktrees(vault)) == 1
