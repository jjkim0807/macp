"""A run whose origin/<branch> holds an older, diverged lineage pushes under
`<branch>-rN` instead of stopping (or force-pushing).

The case: 2026-11-19 mulkko-171. Verify passed, but origin/worker/mulkko-171-…
still carried a desktop-wsl run's commit, Implement had rebased the work onto
main, the push was non-fast-forward, the pre-push guard refused it (correctly),
and the Report stage stopped with no push and no PR. A person pushed a `-r2`
branch by hand.

Unit tests drive `stages.resolve_push_branch` against real git (a bare origin,
a second clone that pushes the "old run"). The path test runs the real
`StagedExecutor.execute` (branch prep, implement checkpoint, pre-report push)
against that origin, then feeds its RESULT to a real Orchestrator's `collect`
and `reconcile_merged`. Nothing leaves tmp_path; global git config is off.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import message as m
from todo_system.orchestrator import stages
from todo_system.orchestrator.assignment import Assignment, Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import Orchestrator
from todo_system.orchestrator.registry import Agent, Registry
from todo_system.orchestrator.tests import test_runner_full_verify_path as fvp


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], check=check,
                          capture_output=True, text=True)


def _out(cwd: Path, *args: str) -> str:
    return _git(cwd, *args).stdout.strip()


def _identify(repo: Path) -> None:
    _git(repo, "config", "user.email", "t@example.test")
    _git(repo, "config", "user.name", "t")


def _commit(repo: Path, rel: str, text: str, msg: str) -> str:
    (repo / rel).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _out(repo, "rev-parse", "HEAD")


def _remote_sha(repo: Path, branch: str) -> str | None:
    line = _out(repo, "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    return line.split()[0] if line else None


@pytest.fixture
def git_env(tmp_path, monkeypatch):
    (tmp_path / "gitconfig").write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    other = tmp_path / "other"          # the older run on another host
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True,
                   capture_output=True)
    _identify(other)
    _commit(other, "main.txt", "v1\n", "init")
    _git(other, "push", "-q", "origin", "main")
    clone = tmp_path / "clone"          # this run's checkout
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True,
                   capture_output=True)
    _identify(clone)
    return origin, other, clone


def _push_old_run(other: Path, branch: str, rel: str = "old.txt") -> str:
    """The older run: a commit on `branch` this clone has never fetched."""
    _git(other, "checkout", "-q", "-B", branch, "origin/main")
    sha = _commit(other, rel, "old run\n", f"old run on {branch}")
    _git(other, "push", "-q", "origin", branch)
    _git(other, "checkout", "-q", "main")
    return sha


def _rebased_local(clone: Path, branch: str) -> str:
    """This run: the work cut straight off main (Implement's rebase)."""
    _git(clone, "fetch", "-q", "origin")
    _git(clone, "checkout", "-q", "-B", branch, "origin/main")
    return _commit(clone, "new.txt", "this run\n", "this run")


# -- unit: the naming rule --------------------------------------------------

def test_no_remote_branch_keeps_the_name(git_env):
    _, _, clone = git_env
    _rebased_local(clone, "worker/t")
    res = stages.resolve_push_branch(clone, "worker/t")
    assert res["branch"] == "worker/t" and res["renamed_from"] is None
    assert _out(clone, "branch", "--show-current") == "worker/t"


def test_fast_forward_keeps_the_name(git_env):
    _, _, clone = git_env
    _rebased_local(clone, "worker/t")
    _git(clone, "push", "-q", "origin", "worker/t")
    _commit(clone, "more.txt", "more\n", "more")       # local is ahead: FF
    res = stages.resolve_push_branch(clone, "worker/t")
    assert res["branch"] == "worker/t" and res["renamed_from"] is None


def test_diverged_moves_to_r2_and_leaves_the_old_remote(git_env):
    _, other, clone = git_env
    old = _push_old_run(other, "worker/t")
    tip = _rebased_local(clone, "worker/t")
    res = stages.resolve_push_branch(clone, "worker/t")
    assert res["branch"] == "worker/t-r2"
    assert res["renamed_from"] == "worker/t"
    # the local branch was renamed (HEAD follows it), not copied
    assert _out(clone, "branch", "--show-current") == "worker/t-r2"
    assert _out(clone, "rev-parse", "worker/t-r2") == tip
    assert _git(clone, "rev-parse", "--verify", "--quiet",
                "refs/heads/worker/t", check=False).returncode != 0
    # the old remote branch is untouched, and nothing was pushed yet
    assert _remote_sha(clone, "worker/t") == old
    assert _remote_sha(clone, "worker/t-r2") is None


def test_taken_r2_moves_to_r3(git_env):
    _, other, clone = git_env
    _push_old_run(other, "worker/t")
    _push_old_run(other, "worker/t-r2", rel="old2.txt")
    _rebased_local(clone, "worker/t")
    res = stages.resolve_push_branch(clone, "worker/t")
    assert res["branch"] == "worker/t-r3"


def test_diverged_r2_moves_to_r3_not_r2_r2(git_env):
    _, other, clone = git_env
    _push_old_run(other, "worker/t-r2")
    _rebased_local(clone, "worker/t-r2")
    res = stages.resolve_push_branch(clone, "worker/t-r2")
    assert res["branch"] == "worker/t-r3"


def test_default_branch_is_never_renamed(git_env):
    _, other, clone = git_env
    _commit(other, "x.txt", "x\n", "moved main")
    _git(other, "push", "-q", "origin", "main")
    _commit(clone, "y.txt", "y\n", "diverged main")
    res = stages.resolve_push_branch(clone, "main")
    assert res["branch"] == "main" and res["renamed_from"] is None


def test_unreadable_origin_keeps_the_name(git_env, tmp_path):
    _, _, clone = git_env
    _rebased_local(clone, "worker/t")
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    res = stages.resolve_push_branch(clone, "worker/t")
    assert res["branch"] == "worker/t" and res["renamed_from"] is None


def test_push_guard_still_refuses_the_old_name(git_env):
    """The guard is unchanged: the rename is how the run gets past it, not a
    hole in it. A non-FF push onto the old name is still refused; the new name
    goes up through the same FF-only checkpoint push."""
    _, other, clone = git_env
    old = _push_old_run(other, "worker/t")
    _rebased_local(clone, "worker/t")
    stages.install_push_guard(clone)
    refused = _git(clone, "push", "origin", "worker/t", check=False)
    assert refused.returncode != 0
    assert _remote_sha(clone, "worker/t") == old
    res = stages.resolve_push_branch(clone, "worker/t")
    cp = stages.checkpoint_push(clone, res["branch"])
    assert cp["pushed"] is True
    assert _remote_sha(clone, "worker/t-r2") == _out(clone, "rev-parse", "HEAD")
    assert _remote_sha(clone, "worker/t") == old
    # a forced push of the new lineage onto the old name is refused by the
    # (unchanged) pre-push guard
    forced = _git(clone, "push", "--force", "origin", "worker/t-r2:worker/t", check=False)
    assert forced.returncode != 0
    assert "pre-push: refusing non-fast-forward" in forced.stderr
    assert _remote_sha(clone, "worker/t") == old


@pytest.mark.parametrize("cand,cur,ok", [
    ("worker/t-r2", "worker/t", True),
    ("worker/t-r3", "worker/t-r2", True),
    ("worker/t", "worker/t", False),
    ("worker/t-r1", "worker/t", False),
    ("worker/u-r2", "worker/t", False),
    ("worker/t-rx", "worker/t", False),
    ("", "worker/t", False),
])
def test_is_suffix_successor(cand, cur, ok):
    assert stages.is_suffix_successor(cand, cur) is ok


# -- orchestrator: adopt the runner's rename ---------------------------------

def _orch(tmp: Path):
    mb = FileMailbox(tmp / "agents")
    ledger = Ledger(tmp / "assignments")
    reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
    return mb, ledger, Orchestrator(mb, reg, ledger, notify=lambda *a, **k: None)


def _reply(mb, task_id: str, typ: str, **payload) -> None:
    mb.send(m.Envelope(to=m.ORCHESTRATOR, frm="w", type=typ, task_id=task_id,
                       corr_id="C", payload=payload))


def test_collect_ignores_an_ordinary_branch_echo(tmp_path):
    mb, ledger, orch = _orch(tmp_path)
    ledger.save(Assignment(task_id="t", agent_id="w", corr_id="C", kind="ai-local",
                           state=A.IN_PROGRESS,
                           payload={"branch": "worker/t", "repo": "o/r"}))
    _reply(mb, "t", m.FAIL, branch="feature/other", reason="x")
    with mock.patch.object(orch, "_retry"):
        orch.collect()
    assert ledger.load("t").payload["branch"] == "worker/t"


def test_collect_adopts_r2_from_a_fail_so_the_retry_continues_it(tmp_path):
    mb, ledger, orch = _orch(tmp_path)
    ledger.save(Assignment(task_id="t", agent_id="w", corr_id="C", kind="ai-local",
                           state=A.IN_PROGRESS, payload={"repo": "o/r"}))
    _reply(mb, "t", m.FAIL, branch="worker/t-r2", reason="verify died")
    with mock.patch.object(orch, "_retry") as retry:
        orch.collect()
    row = ledger.load("t")
    assert row.payload["branch"] == "worker/t-r2"
    assert any(h.get("type") == "branch-renamed" for h in row.history)
    # _retry re-dispatches a.payload, so it carries the new name
    retry.assert_called_once()
    assert retry.call_args.args[0].payload["branch"] == "worker/t-r2"


# -- path: the real runner against a diverged origin, then reconcile ---------

OLD_BRANCH = f"worker/{fvp.SLUG}"
NEW_BRANCH = f"{OLD_BRANCH}-r2"


class _DivergedBox(fvp._Sandbox):
    """The full-verify sandbox, plus an older run already on origin/<branch>,
    an implement that rebases onto main, and a report that does what the Report
    agent does: push the metadata branch and say which PR it opened."""

    def __init__(self, tmp: Path, monkeypatch):
        super().__init__(tmp, monkeypatch)
        _git(self.seed, "checkout", "-q", "-b", OLD_BRANCH)
        self.old_sha = _commit(self.seed, "old.txt", "old run\n", "old run (other host)")
        _git(self.seed, "push", "-q", "origin", OLD_BRANCH)
        _git(self.seed, "checkout", "-q", "main")

    def _stub_argv(self, stage, **kw):
        argv = super()._stub_argv(stage, **kw)
        clone = shlex.quote(str(self.clone))
        branch = shlex.quote(kw["branch"])
        if stage == "implement":
            # Implement rebased the work onto main: the branch no longer
            # contains the old run's commit.
            argv[2] = (f"git -C {clone} fetch -q origin "
                       f"&& git -C {clone} checkout -q -B {branch} origin/main "
                       f"&& {argv[2]}")
        elif stage == "report":
            argv[2] = f"git -C {clone} push -q origin {branch} && {argv[2]}"
        return argv


@pytest.fixture
def dbox(tmp_path, monkeypatch) -> _DivergedBox:
    return _DivergedBox(tmp_path, monkeypatch)


def _landed_only_on(branch_ok: str):
    return lambda run_dir, last_stage, branch=None, repo=None: branch == branch_ok


def test_path_diverged_origin_pushes_r2_and_the_result_names_it(dbox):
    dbox.merge_landed.side_effect = _landed_only_on(NEW_BRANCH)
    out = dbox.execute(model_tier="standard")
    assert out.ok, out.summary
    assert dbox.stages_run() == list(stages.STAGES_AUTO)
    # implement ran on the dispatch name; everything after it on -r2
    assert dbox.kw("implement")["branch"] == OLD_BRANCH
    assert dbox.kw("verify")["branch"] == NEW_BRANCH
    assert dbox.kw("report")["branch"] == NEW_BRANCH
    assert f"- branch: `{NEW_BRANCH}`" in dbox.prompts["report"]
    # pushed without force: -r2 is this run's tip, the old branch is untouched
    tip = _out(dbox.clone, "rev-parse", NEW_BRANCH)
    assert _remote_sha(dbox.clone, NEW_BRANCH) == tip
    assert _remote_sha(dbox.clone, OLD_BRANCH) == dbox.old_sha
    assert _git(dbox.clone, "merge-base", "--is-ancestor", dbox.old_sha, tip,
                check=False).returncode == 1
    # the RESULT and the merge check name the new branch
    assert out.payload["branch"] == NEW_BRANCH
    assert out.payload["pr_url"] == fvp.PR_URL
    assert out.payload["review_required"] is False
    assert dbox.merge_landed.call_args.kwargs["branch"] == NEW_BRANCH
    assert any("moved to " + NEW_BRANCH in line for line in dbox.lines)


def test_path_unmerged_r2_pr_is_closed_by_reconcile_merged(dbox, tmp_path):
    """The PR on -r2 did not merge inside the run (review park). The ledger row
    must follow the RESULT onto -r2, so that when the PR merges later
    reconcile_merged finds it and closes the task."""
    dbox.merge_landed.return_value = False
    out = dbox.execute(model_tier="standard")
    assert out.ok, out.summary
    assert out.payload["branch"] == NEW_BRANCH
    assert out.payload["review_required"] is True

    mb, ledger, orch = _orch(tmp_path / "orch")
    ledger.save(Assignment(task_id=fvp.SLUG, agent_id="w", corr_id="C", kind="ai-local",
                           state=A.IN_PROGRESS,
                           payload={"branch": OLD_BRANCH, "repo": fvp.REPO_URL}))
    mb.send(m.Envelope(to=m.ORCHESTRATOR, frm="w", type=m.RESULT, task_id=fvp.SLUG,
                       corr_id="C", payload=dict(out.payload)))
    orch.collect()
    row = ledger.load(fvp.SLUG)
    assert row.state == A.AWAITING_HUMAN
    assert row.payload["branch"] == NEW_BRANCH

    merged = mock.Mock(side_effect=lambda branch, repo=None: branch == NEW_BRANCH)
    with mock.patch.object(stages, "gh_pr_merged", merged), \
         mock.patch.object(orch, "_close_task") as close:
        closed = orch.reconcile_merged()
    merged.assert_called_once_with(NEW_BRANCH, fvp.REPO_URL)
    close.assert_called_once_with(fvp.SLUG)
    assert [a.task_id for a in closed] == [fvp.SLUG]
    assert ledger.load(fvp.SLUG).state == A.DONE
