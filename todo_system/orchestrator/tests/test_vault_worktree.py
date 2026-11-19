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

import contextlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from todo_system import paths
from todo_system.orchestrator import message as m
from todo_system.orchestrator import repos, stages
from todo_system.orchestrator.runner import StagedExecutor

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


# --- what the stages are told ---------------------------------------------------------

WT = "/w/vault.wt/t"                 # a vault worktree by shape; nothing reads the disk
OVERRIDE = 'overrides every "vault root" git step'


def _prompt(stage, **kw) -> str:
    """The prompt text `build_stage_argv` builds (argv[2]) for a vault-ish run."""
    base = dict(task_id="t", run_id="R", run_dir=Path("/v/Worker/runs/R"),
                branch="worker/t")
    base.update(kw)
    return stages.build_stage_argv(stage, **base)[2]


def _metadata(prompt: str) -> str:
    return prompt.split("# Run metadata (injected by orchestrator)", 1)[1]


@pytest.mark.parametrize("stage", stages.VAULT_WORKTREE_WRITE_STAGES)
def test_a_stage_that_changes_the_branch_is_pointed_at_the_worktree(stage):
    meta = _metadata(_prompt(stage, working_dir=WT))
    assert f"- working_dir: `{WT}`\n" in meta
    assert OVERRIDE in meta
    assert f"`git -C {WT} …`" in meta
    assert f"`git -C {WT} branch --show-current` does" in meta
    assert "follow the EXTERNAL-repo steps" in meta
    assert "never run `git checkout`" in meta
    assert "removes the worktree" in meta
    # the PR is the report/quick stage's to open and merge; a delegate's is the runner's
    assert ("--repo <owner/repo>" in meta) == (stage in stages.VAULT_WORKTREE_GH_STAGES)
    assert ("Running the project's build/tests" in meta) == (
        stage in stages.VAULT_WORKTREE_CHECK_STAGES)
    assert "External repo task" not in meta


@pytest.mark.parametrize("stage", ["research", "plan"])
def test_a_read_only_stage_is_told_where_the_branch_is_and_to_write_nothing_there(stage):
    meta = _metadata(_prompt(stage, working_dir=WT))
    assert f"- working_dir: `{WT}`\n" in meta
    assert f"This stage writes nothing under `{WT}`" in meta
    assert OVERRIDE not in meta
    assert "git commit" not in meta


def test_a_stage_with_no_branch_work_gets_only_the_working_dir_line():
    with_wt = _metadata(_prompt("discover", working_dir=WT))
    without = _metadata(_prompt("discover"))
    assert with_wt.replace(f"- working_dir: `{WT}`\n", "", 1) == without


@pytest.mark.parametrize("stage", sorted(stages.STAGE_ARTIFACT))
def test_a_vault_run_without_a_worktree_gets_the_prompt_it_always_got(stage):
    """`working_dir` alone opens nothing: only the worktree SHAPE does. The vault
    root — what the runner passed before, and still passes for a discover run —
    leaves the prompt as it is with no `working_dir` at all."""
    plain = _prompt(stage)
    assert _prompt(stage, working_dir="/v") == plain
    assert _prompt(stage, working_dir="/v/vault.wt") == plain       # the parent, not a leaf
    assert "working_dir" not in _metadata(plain)


def test_an_external_repo_task_never_gets_the_vault_block():
    """Even when the checkout's path has the vault worktree's shape — a repo
    literally named `vault` keeps its run worktrees under `vault.wt/` too."""
    meta = _metadata(_prompt("implement", repo="https://h/o/vault.git", working_dir=WT))
    assert "- **External repo task**" in meta
    assert "Vault task" not in meta
    assert meta.count("Running the project's build/tests") == 1


# --- a run, end to end: StagedExecutor.execute with a fake agent ----------------------


def _no_gh(argv):
    return subprocess.CompletedProcess(argv, 1, "", "no pull requests found")


class _Run:
    """One `StagedExecutor.execute` over a temporary vault, with a fake agent.

    Every real `git` goes through (and is recorded with its cwd); `gh` is
    answered by `gh` (default: nothing found); anything else is a stage. Each
    stage records what it could see, then calls `act(ctx)` — return False to
    leave the stage's artifact unwritten, i.e. to fail the run there.
    """

    def __init__(self, vault: Path, runs: Path):
        self.vault, self.runs = vault, runs
        self.seen: list[SimpleNamespace] = []
        self.git: list[list[str]] = []
        self.gh: list[tuple[list[str], str]] = []
        self.lines: list[str] = []

    def stage(self, name: str) -> SimpleNamespace:
        return next(s for s in self.seen if s.stage == name)

    def run_dir(self) -> Path:
        (only,) = list(self.runs.iterdir())
        return only

    def progress_json(self) -> dict:
        return json.loads((self.run_dir() / stages.STAGE_PROGRESS).read_text(encoding="utf-8"))

    def execute(self, payload=None, act=None, gh=None, patches=()):
        real_run = subprocess.run
        real_argv = stages.build_stage_argv
        pending: dict = {}

        def g(repo, *args):
            return real_run(["git", "-C", str(repo), *args],
                            capture_output=True, text=True)

        def spy_argv(stage, **kw):
            pending["stage"], pending["kw"] = stage, kw
            return real_argv(stage, **kw)

        def fake_run(argv, **kw):
            argv = list(argv)
            if argv and argv[0] == "git":
                self.git.append(argv)
                return real_run(argv, **kw)
            if argv and argv[0] == "gh":
                self.gh.append((argv, str(kw.get("cwd"))))
                return (gh or _no_gh)(argv)
            stage, skw = pending["stage"], pending["kw"]
            wd = Path(skw["working_dir"])
            ctx = SimpleNamespace(
                stage=stage, wd=wd, run_dir=Path(skw["run_dir"]), vault=self.vault, g=g,
                live_head=g(self.vault, "symbolic-ref", "-q", "HEAD").stdout.strip(),
                wd_branch=g(wd, "branch", "--show-current").stdout.strip(),
                wd_status=g(wd, "status", "--porcelain").stdout,
                wd_log=g(wd, "log", "--format=%s", "-5").stdout.splitlines())
            self.seen.append(ctx)
            wrote = act(ctx) if act is not None else None
            # implement's artifact is run.log, which the runner writes itself
            if wrote is not False and stage != "implement":
                (ctx.run_dir / stages.STAGE_ARTIFACT[stage]).write_text(
                    "overall_status: pass\nmerged: no\n", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "ok", "")

        msg = m.Envelope(to="w", type="TASK_ASSIGN", task_id="t", corr_id="C",
                         payload=payload or {"worker_mode": "auto"})
        with contextlib.ExitStack() as stack:
            for p in (mock.patch.object(stages, "build_stage_argv", side_effect=spy_argv),
                      mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_run),
                      mock.patch("subprocess.run", side_effect=fake_run), *patches):
                stack.enter_context(p)
            return StagedExecutor().execute(msg, mock.Mock(), self.lines.append)


@pytest.fixture
def live(vault, tmp_path, monkeypatch) -> _Run:
    """`vault` installed as the live vault of the run under test."""
    runs = tmp_path / "runs"            # outside the vault: never a vault change
    runs.mkdir()
    monkeypatch.setattr(stages, "_VAULT_DATA_ROOT", vault)
    monkeypatch.setattr(stages, "RUNS_DIR", runs)
    return _Run(vault, runs)


def _agent_commits(ctx):
    """An implement stage that commits its work where it was told to work."""
    if ctx.stage == "implement":
        (ctx.wd / "agent.md").write_text("agent work", encoding="utf-8")
        ctx.g(ctx.wd, "add", "agent.md")
        ctx.g(ctx.wd, "commit", "-m", "agent work")


def test_an_auto_run_works_in_the_worktree_and_removes_it(live, vault):
    wt = repos.vault_worktree_path("t")

    out = live.execute(act=_agent_commits)

    assert out.ok, out.summary
    assert [s.stage for s in live.seen] == list(stages.STAGES_AUTO)
    for s in live.seen:
        assert s.wd == wt and repos.is_vault_worktree(s.wd)
        assert s.wd_branch == "worker/t"
        assert s.live_head == MAIN_REF
        # run artifacts stay in the live vault's Worker/runs, not in the worktree
        assert s.run_dir.parent == live.runs
        assert not (s.wd / "Worker" / "runs").exists()
    # gone afterwards — directory and registration — while the branch and its commit stay
    assert not wt.exists()
    assert list(_worktrees(vault)) == [str(vault.resolve())]
    assert _git(vault, "log", "--format=%s", "main..worker/t").stdout.split("\n")[0] == "agent work"
    assert _head_ref(vault) == MAIN_REF
    assert any(line.startswith("worktree removed:") for line in live.lines)


def test_an_implement_that_did_not_commit_is_rescued_before_verify(live, vault):
    def act(ctx):
        if ctx.stage == "implement":
            (ctx.wd / "green-but-uncommitted.md").write_text("work", encoding="utf-8")

    out = live.execute(act=act)

    assert out.ok, out.summary
    assert live.stage("implement").wd_status == ""            # clean when it started
    verify = live.stage("verify")
    assert verify.wd_status == "", "Verify was handed a dirty tree"
    assert verify.wd_log[0] == "chore(worker): t WIP rescue after implement"
    assert "=== implement WIP rescue ===" in (live.run_dir() / "run.log").read_text(
        encoding="utf-8")
    assert "green-but-uncommitted.md" in _git(
        vault, "show", "--stat", "--format=", "worker/t").stdout
    # a rescue is a local commit: nothing was pushed for it
    assert not [a for a in live.git if "push" in a]
    assert not (vault / "green-but-uncommitted.md").exists()   # never in the live tree


def test_a_failed_run_keeps_its_uncommitted_work_on_the_branch(live, vault):
    """Every way out removes the worktree, so every way out commits first."""
    wt = repos.vault_worktree_path("t")

    def act(ctx):
        if ctx.stage == "plan":
            (ctx.wd / "half.md").write_text("half-finished", encoding="utf-8")
            return False                # dies without its artifact

    out = live.execute(act=act)

    assert not out.ok and "missing" in out.summary
    assert not wt.exists()
    assert list(_worktrees(vault)) == [str(vault.resolve())]
    assert _git(vault, "show", "worker/t:half.md").stdout == "half-finished"
    assert _git(vault, "status", "--porcelain").stdout == ""   # the live tree saw nothing
    assert _head_ref(vault) == MAIN_REF


def _delegate_gh(merge_rc: int, state: str | None):
    def gh(argv):
        if argv[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(argv, 0, "https://github.com/o/r/pull/9\n", "")
        if argv[:3] == ["gh", "pr", "merge"]:
            return subprocess.CompletedProcess(
                argv, merge_rc, "",
                "" if merge_rc == 0 else "failed to run git: 'main' is already used by worktree")
        if argv[:3] == ["gh", "pr", "view"] and "state" in argv and state:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"state": state}), "")
        return _no_gh(argv)             # no PR open yet for `--json url,state`
    return gh


def _delegate_commits(ctx):
    (ctx.wd / "Todo").mkdir(exist_ok=True)
    (ctx.wd / "Todo" / "findings.md").write_text("findings", encoding="utf-8")
    ctx.g(ctx.wd, "add", "-A")
    ctx.g(ctx.wd, "commit", "-m", "chore(delegate): t — findings")


URL = "https://example.com/o/r.git"


def test_a_delegate_run_is_pushed_and_merged_from_the_worktree(live, vault):
    """The merge that exits non-zero AFTER landing: gh tidies up locally and
    cannot switch a linked worktree to the default branch. The PR's real state
    decides, not the exit code."""
    wt = repos.vault_worktree_path("t")
    out = live.execute(
        payload={"worker_mode": "delegate"}, act=_delegate_commits,
        gh=_delegate_gh(merge_rc=1, state="MERGED"),
        patches=[mock.patch.object(StagedExecutor, "_origin_repo_arg",
                                   staticmethod(lambda _checkout: URL))])

    assert out.ok, out.summary
    assert live.stage("delegate").wd == wt
    assert ["git", "-C", str(wt), "push", "origin", "worker/t"] in live.git
    assert not [a for a in live.git if "push" in a and str(vault) in a]
    # every gh ran inside the worktree, and the merge named the repo
    assert live.gh and {cwd for _argv, cwd in live.gh} == {str(wt)}
    (merge,) = [argv for argv, _cwd in live.gh if argv[:3] == ["gh", "pr", "merge"]]
    assert merge[-2:] == ["--repo", URL] and "--squash" in merge
    assert out.payload.get("pr_url") == "https://github.com/o/r/pull/9"
    assert not out.payload.get("review_required"), out.summary
    assert any("treated as merged" in line for line in live.lines)
    # the branch reached origin; the worktree is gone; the live vault never moved
    assert _git(vault, "ls-remote", "--heads", "origin", "worker/t").stdout.strip()
    assert not wt.exists() and _head_ref(vault) == MAIN_REF


@pytest.mark.parametrize("state", ["OPEN", None])
def test_a_delegate_merge_that_did_not_land_still_parks(live, state):
    """OPEN, or no answer at all: only a positive MERGED overrides a failed merge."""
    out = live.execute(payload={"worker_mode": "delegate"}, act=_delegate_commits,
                       gh=_delegate_gh(merge_rc=1, state=state))
    assert out.payload.get("review_required") is True
    assert "미머지" in out.summary
    assert not [line for line in live.lines if "treated as merged" in line]


def test_a_delegate_run_publishes_what_the_stage_left_uncommitted(live, vault):
    """A backend with no shell (it cannot `git commit`) leaves its edits in the
    tree. They are committed before the push, so the PR carries them."""
    def act(ctx):
        (ctx.wd / "notes.md").write_text("written, never committed", encoding="utf-8")

    out = live.execute(payload={"worker_mode": "delegate", "auto_merge": False},
                       act=act, gh=_delegate_gh(merge_rc=0, state=None))

    assert out.ok, out.summary
    remote = _git(vault, "ls-remote", "--heads", "origin", "worker/t").stdout.split()[0]
    assert _git(vault, "show", f"{remote}:notes.md").stdout == "written, never committed"
    assert not [argv for argv, _cwd in live.gh if argv[:3] == ["gh", "pr", "merge"]]


def test_a_discover_run_gets_no_branch_and_no_worktree(live, vault):
    out = live.execute(payload={"worker_mode": "discover"})

    assert out.ok, out.summary
    (stage,) = live.seen
    assert stage.wd == vault                      # the live vault: where its report goes
    assert stage.live_head == MAIN_REF
    assert _git(vault, "rev-parse", "--verify", "--quiet", "refs/heads/worker/t",
                check=False).returncode != 0
    assert not repos.vault_worktree_path("t").exists()
    assert list(_worktrees(vault)) == [str(vault.resolve())]
    assert not [a for a in live.git if "worktree" in a and "add" in a]


def test_a_leftover_worktree_is_saved_and_replaced(live, vault):
    """A run that died before its `finally` leaves its worktree behind. The next
    run of the task commits what was in it, removes it, and starts clean."""
    old = repos.ensure_vault_worktree(vault, "t")
    assert stages.prepare_vault_branch(old, "worker/t").problem is None
    (old / "left-behind.md").write_text("from the dead run", encoding="utf-8")

    out = live.execute(act=_agent_commits)

    assert out.ok, out.summary
    first = live.seen[0]
    assert first.wd == old and first.wd_status == ""
    assert not (first.wd / "left-behind.md").exists()         # a fresh tree
    # the leftover's work is a commit the run names, not a deleted file
    lost = out.payload["orphaned_commits"]
    assert lost["count"] == 1
    assert _git(vault, "show", f"{lost['shas'][0]}:left-behind.md").stdout == "from the dead run"
    assert any(line.startswith("leftover vault worktree: rescued") for line in live.lines)
    assert not old.exists()


def test_a_worktree_whose_work_cannot_be_committed_is_kept_not_deleted(live, vault):
    """Detached HEAD: there is no branch to commit to, and a forced removal would
    delete the only copy. It stays, the run says so, and the task's next run
    refuses to start over it."""
    wt = repos.vault_worktree_path("t")

    def act(ctx):
        if ctx.stage == "implement":
            ctx.g(ctx.wd, "checkout", "--detach")
            (ctx.wd / "only-copy.md").write_text("irreplaceable", encoding="utf-8")

    live.execute(act=act)

    assert (wt / "only-copy.md").read_text(encoding="utf-8") == "irreplaceable"
    assert any("vault worktree kept" in line for line in live.lines)

    again = _Run(vault, live.runs).execute()
    assert not again.ok
    assert "leftover vault worktree is in the way" in again.summary
    assert (wt / "only-copy.md").read_text(encoding="utf-8") == "irreplaceable"


def test_a_rescue_that_fails_says_why_and_the_worktree_is_kept(live, vault):
    """A stage killed mid-git leaves `index.lock` behind, and then nothing can be
    staged. The work stays on disk and the run names the reason."""
    wt = repos.vault_worktree_path("t")
    lock: dict = {}

    def act(ctx):
        if ctx.stage == "plan":
            (ctx.wd / "work.md").write_text("not committed", encoding="utf-8")
            gitdir = Path(ctx.g(ctx.wd, "rev-parse", "--absolute-git-dir").stdout.strip())
            lock["path"] = gitdir / "index.lock"
            lock["path"].write_text("", encoding="utf-8")
            return False

    out = live.execute(act=act)

    assert not out.ok
    assert (wt / "work.md").read_text(encoding="utf-8") == "not committed"
    assert any(line.startswith("vault worktree: not rescued — git add -A failed")
               for line in live.lines), live.lines
    assert any("vault worktree kept" in line for line in live.lines)
    # once the lock is cleared, the task's next run saves the work and carries on
    lock["path"].unlink()
    again = _Run(vault, live.runs)
    out2 = again.execute(act=_agent_commits)
    assert out2.ok, out2.summary
    saved = out2.payload["orphaned_commits"]["shas"][0]
    assert _git(vault, "show", f"{saved}:work.md").stdout == "not committed"
    assert not wt.exists()


def test_a_stage_that_moves_the_live_head_is_caught_at_the_stage_boundary(live, vault):
    def act(ctx):
        if ctx.stage == "research":
            ctx.g(ctx.vault, "checkout", "-b", "stray")       # what the old prompt line did

    out = live.execute(act=act)

    assert out.ok, out.summary                                # the outcome is not changed
    assert live.stage("research").live_head == MAIN_REF
    assert live.stage("plan").live_head == MAIN_REF, "the next stage ran on a moved HEAD"
    assert live.progress_json()["live_head_moved"] == {
        "stage": "research", "from": MAIN_REF, "to": "refs/heads/stray", "restored": True}
    assert "=== live vault HEAD moved (research) ===" in (
        live.run_dir() / "run.log").read_text(encoding="utf-8")
    assert any("live vault HEAD moved during stage research" in line for line in live.lines)
    assert _head_ref(vault) == MAIN_REF


def test_the_stage_prompts_own_checkout_line_cannot_move_the_live_head(live, vault):
    """worker-delegate.md and worker-quick.md still say `git checkout -b
    worker/<slug> || git checkout worker/<slug>` at the vault root. Git refuses
    both halves while the run's worktree holds the branch."""
    tried: dict = {}

    def act(ctx):
        tried["create"] = ctx.g(ctx.vault, "checkout", "-b", "worker/t").returncode
        tried["switch"] = ctx.g(ctx.vault, "checkout", "worker/t").returncode
        tried["head"] = ctx.g(ctx.vault, "symbolic-ref", "-q", "HEAD").stdout.strip()

    out = live.execute(payload={"worker_mode": "delegate", "auto_merge": False}, act=act)

    assert out.ok, out.summary
    assert tried["create"] != 0 and tried["switch"] != 0
    assert tried["head"] == MAIN_REF
    assert "live_head_moved" not in live.progress_json()


def test_a_live_checkout_already_on_the_runs_branch_fails_the_run(live, vault):
    _git(vault, "checkout", "-b", "worker/t")
    # restore_main would put the vault back on main first; here it could not
    out = live.execute(patches=[mock.patch.object(stages, "restore_main")])

    assert not out.ok
    assert "could not prepare vault worktree" in out.summary
    assert "is on worker/t" in out.summary
    assert live.seen == []                                    # no stage ran
    assert not repos.vault_worktree_path("t").exists()


def test_a_failed_branch_prep_leaves_no_worktree_behind(live, vault):
    dead = subprocess.CompletedProcess(["git"], 1, "", "fatal: cannot lock ref")
    out = live.execute(patches=[mock.patch.object(stages, "git_checkout", return_value=dead)])

    assert not out.ok
    assert out.summary.startswith("could not prepare vault branch worker/t:")
    assert not repos.vault_worktree_path("t").exists()
    assert list(_worktrees(vault)) == [str(vault.resolve())]
    assert _head_ref(vault) == MAIN_REF


def test_an_external_repo_run_never_touches_the_vault_worktree(live, vault, tmp_path):
    clone = tmp_path / "clone"
    clone.mkdir()
    real_argv = stages.build_stage_argv
    seen: dict = {}

    def spy_argv(stage, **kw):
        seen["kw"], seen["stage"] = kw, stage
        return real_argv(stage, **kw)

    def fake_stage(argv, **kw):
        (Path(seen["kw"]["run_dir"]) / stages.STAGE_ARTIFACT[seen["stage"]]).write_text(
            "overall_status: pass\nmerged: yes\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    msg = m.Envelope(to="w", type="TASK_ASSIGN", task_id="t", corr_id="C",
                     payload={"repo": "https://h/o/r.git", "worker_mode": "auto"})
    with mock.patch.object(repos, "ensure_worktree", return_value=clone), \
         mock.patch.object(repos, "ensure_vault_worktree") as cut, \
         mock.patch.object(StagedExecutor, "_prepare_vault_worktree") as prep, \
         mock.patch.object(StagedExecutor, "_vault_head_guard") as watch, \
         mock.patch.object(stages, "prepare_external_branch",
                           return_value=subprocess.CompletedProcess([], 0, "", "")), \
         mock.patch.object(stages, "merge_landed", return_value=True), \
         mock.patch.object(stages, "remove_worktree"), \
         mock.patch.object(stages, "build_stage_argv", side_effect=spy_argv), \
         mock.patch.object(stages, "run_stage_subprocess", side_effect=fake_stage):
        out = StagedExecutor().execute(msg, mock.Mock(), lambda *_: None)

    assert out.ok, out.summary
    cut.assert_not_called()
    prep.assert_not_called()
    watch.assert_not_called()
    assert seen["kw"]["working_dir"] == str(clone)


def test_restoring_a_checkout_that_is_already_on_main_runs_no_checkout(vault):
    """What `restore_main` finds on every healthy run now. A plain `checkout main`
    that fails for a moment falls back to `checkout -f`, which would delete this
    edit to "restore" a branch the checkout never left."""
    (vault / "main.txt").write_text("a person's uncommitted edit", encoding="utf-8")
    real_run = subprocess.run
    calls: list[list[str]] = []

    def spy(argv, *a, **kw):
        calls.append(list(argv))
        if "checkout" in argv:          # the transient failure: index.lock is held
            return subprocess.CompletedProcess(argv, 128, "", "fatal: index.lock exists")
        return real_run(argv, *a, **kw)

    with mock.patch("subprocess.run", side_effect=spy):
        assert stages.restore_main(vault) is True

    assert not [c for c in calls if "checkout" in c]
    assert (vault / "main.txt").read_text(encoding="utf-8") == "a person's uncommitted edit"
    assert _head_ref(vault) == MAIN_REF


def test_restoring_from_another_branch_still_checks_main_out(vault):
    _git(vault, "checkout", "-b", "worker/x")
    assert stages.restore_main(vault) is True
    assert _head_ref(vault) == MAIN_REF
    # a detached HEAD is not "already on main" either
    _git(vault, "checkout", "--detach")
    assert stages.restore_main(vault) is True
    assert _head_ref(vault) == MAIN_REF


def test_origin_as_a_gh_repo_argument(vault):
    assert StagedExecutor._origin_repo_arg(vault) is None      # a path remote
    _git(vault, "remote", "set-url", "origin", URL)
    assert StagedExecutor._origin_repo_arg(vault) == URL
    _git(vault, "remote", "set-url", "origin", "git@example.com:o/r.git")
    assert StagedExecutor._origin_repo_arg(vault) == "git@example.com:o/r.git"
    _git(vault, "remote", "remove", "origin")
    assert StagedExecutor._origin_repo_arg(vault) is None


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
