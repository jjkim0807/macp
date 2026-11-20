"""Batch verify — the integration-gate job (`batch_gate.step`).

Quick-verified tasks wait as `batch_ready`; one job per repo then builds a local
integration branch from them, runs the repo's full gate ONCE, and merges their
PRs in order. These tests drive that job one `step` at a time.

The git is real: a bare origin, a seed clone that stands in for everybody else
pushing, and the shared clone the job works in — all under tmp_path, with the
global and system git config switched off. Three worker branches exist: `a` and
`b` each add a file, `c` edits `shared.txt`, which the default branch then edits
too, so only `c` conflicts. The gate runner, the PR merge, `gh` and the clock are
stand-ins handed in through `batch_gate.Env`; nothing here starts a detached job
or reaches a network.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from todo_system import kakao_format
from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import batch_gate, beta_publish, jobs, repos, stages, tasks
from todo_system.orchestrator import message as m
from todo_system.orchestrator.assignment import Assignment, Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import (
    ALERT_BATCH_GATE_FAILED,
    WAIT_GRADLE_CAP,
    CheckoutGate,
    Orchestrator,
    Task,
    _parse_ts,
)
from todo_system.orchestrator.registry import Agent, Registry

URL = "https://example.test/acme/batchrepo.git"
NAME = "batchrepo"
GATE = {"full_gate": [["sh", "-c", "exit 0"]]}
EMPTY = {"started": [], "merged": [], "returned": [], "closed": []}
T0 = 1_800_000_000.0        # rows become ready at T0, T0 + 1, …
FRESH = T0 + 60             # a moment when nobody has waited long


@pytest.fixture(autouse=True)
def closes(monkeypatch) -> list[str]:
    """Closing a task writes the real Todo/items: record the slug instead."""
    done: list[str] = []
    monkeypatch.setattr(tasks, "mark_done", lambda slug, *a, **k: done.append(slug))
    monkeypatch.setattr(tasks, "md_fingerprint", lambda slug, *a, **k: f"fp-{slug}")
    return done


# --- the repo -----------------------------------------------------------------

def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(cwd), *args],
                          check=True, capture_output=True, text=True)
    return done.stdout.strip()


class _Repo:
    """origin.git + seed (the outside world) + the shared clone `worker/batchrepo`."""

    def __init__(self, tmp: Path, monkeypatch):
        # No global/system git config: a host-wide hooksPath, signing rule or
        # identity must not decide whether these tests pass.
        (tmp / "gitconfig").write_text("", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp / "gitconfig"))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        self.origin = tmp / "origin.git"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.origin)],
                       check=True, capture_output=True)
        self.seed = tmp / "seed"
        subprocess.run(["git", "clone", str(self.origin), str(self.seed)],
                       check=True, capture_output=True)
        _git(self.seed, "config", "user.email", "t@example.test")
        _git(self.seed, "config", "user.name", "t")
        _git(self.seed, "symbolic-ref", "HEAD", "refs/heads/main")
        self._commit("shared.txt", "base\n")
        _git(self.seed, "push", "-q", "origin", "main")
        # The shared clone is made BEFORE the branches exist, so the job's own
        # fetch is what brings them in. It gets no git identity on purpose: the
        # integration merge has to carry its own.
        self.clone = tmp / "worker" / NAME
        self.clone.parent.mkdir()
        subprocess.run(["git", "clone", str(self.origin), str(self.clone)],
                       check=True, capture_output=True)
        self.heads = {slug: self.push_branch(slug, rel, text) for slug, rel, text in (
            ("a", "a.txt", "from a\n"), ("b", "b.txt", "from b\n"),
            ("c", "shared.txt", "c's edit\n"))}
        self.push_main("shared.txt", "main's edit\n")

    def _commit(self, rel: str, text: str) -> str:
        (self.seed / rel).write_text(text, encoding="utf-8")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-q", "-m", f"edit {rel}")
        return _git(self.seed, "rev-parse", "HEAD")

    def push_branch(self, slug: str, rel: str, text: str,
                    base: str = "origin/main") -> str:
        """Commit one file on worker/<slug> (cut from `base`) and push it."""
        _git(self.seed, "checkout", "-q", "-B", f"worker/{slug}", base)
        sha = self._commit(rel, text)
        _git(self.seed, "push", "-q", "origin", f"worker/{slug}")
        return sha

    def push_main(self, rel: str, text: str) -> str:
        """Somebody else lands a commit on the default branch."""
        _git(self.seed, "fetch", "-q", "origin")
        _git(self.seed, "checkout", "-q", "-B", "main", "origin/main")
        sha = self._commit(rel, text)
        _git(self.seed, "push", "-q", "origin", "main")
        return sha

    def squash_merge(self, branch: str) -> str:
        """What a server-side `gh pr merge --squash --delete-branch` leaves."""
        _git(self.seed, "fetch", "-q", "origin")
        _git(self.seed, "checkout", "-q", "-B", "main", "origin/main")
        _git(self.seed, "merge", "-q", "--squash", f"origin/{branch}")
        _git(self.seed, "commit", "-q", "-m", f"squash {branch}")
        _git(self.seed, "push", "-q", "origin", "main")
        _git(self.seed, "push", "-q", "origin", "--delete", branch)
        return _git(self.seed, "rev-parse", "HEAD")

    def delete_branch(self, branch: str) -> None:
        _git(self.seed, "push", "-q", "origin", "--delete", branch)

    def origin_sha(self, ref: str = "main") -> str:
        return _git(self.origin, "rev-parse", ref)

    def integ_branches(self) -> list[str]:
        return _git(self.clone, "for-each-ref", "--format=%(refname:short)",
                    "refs/heads/integ/").split()

    def integ_worktrees(self) -> list[str]:
        root = self.clone.parent / f"{NAME}.wt"
        return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


def _row(slug: str, head_sha: str, *, ready: float = T0, n: int = 1,
         url: str = URL) -> Assignment:
    """A BATCH_READY ledger row, made by the transition collect() uses."""
    a = Assignment(task_id=slug, agent_id="w", corr_id=f"c-{slug}", kind="ai-local",
                   state=A.DONE,
                   payload={"summary": slug, "repo": url, "worker_mode": "auto",
                            "task_path": "", "heavy": False})
    result = {"pr_url": f"https://example.test/acme/batchrepo/pull/{n}", "repo": url,
              "branch": f"worker/{slug}", batch_gate.KEY_BATCH: {"head_sha": head_sha}}
    stamp = dt.datetime.fromtimestamp(ready).astimezone().isoformat()
    assert batch_gate.mark_ready(a, result, now_iso=stamp)
    return a


class _FakeGate:
    """A gate runner that starts nothing: it notes what the worktree held when
    it was asked to start, and answers `verdict` to every poll."""

    def __init__(self):
        self.verdict = batch_gate.GATE_RUNNING
        self.fail_start = False
        self.started: list[dict] = []
        self.stopped: list[str] = []
        self.cleaned: list[str] = []

    def start(self, job) -> dict:
        if self.fail_start:
            raise RuntimeError("no job runner")
        tree = Path(job["worktree"])
        self.started.append({
            "branch": job["branch"],
            "files": sorted(p.name for p in tree.iterdir() if p.name != ".git"),
            "shared": (tree / "shared.txt").read_text(encoding="utf-8"),
            "members": [member["slug"] for member in job["members"]],
            "full_gate": job["full_gate"]})
        return {"job": batch_gate.gate_job_slug(job["repo_name"]),
                "log": f"Worker/jobs/{batch_gate.gate_job_slug(job['repo_name'])}.log"}

    def poll(self, job) -> str:
        return self.verdict

    def stop(self, job) -> None:
        self.stopped.append(job["branch"])

    def cleanup(self, job) -> None:
        self.cleaned.append(job["branch"])


class _World:
    """One repo, its waiting rows, and every seam `step` is handed."""

    def __init__(self, tmp: Path, monkeypatch, slugs: str = "abc"):
        self.repo = _Repo(tmp, monkeypatch)
        self.vault = tmp / "vault"
        self.gate = _FakeGate()
        self.now = FRESH
        self.rows = {slug: _row(slug, self.repo.heads[slug], ready=T0 + i, n=i + 1)
                     for i, slug in enumerate(slugs)}
        self.clone_exists = True
        self.real_merge = False            # merge_pr really squashes on origin
        self.after_merge = {}              # branch -> callable run after its merge
        self.fail_git: set[str] = set()    # git subcommands answered rc 1
        self.merge_answers: dict[str, list[dict]] = {}
        self.pr_states: dict[str, str | None] = {}
        self.pr_merged: dict[str, bool | None] = {}
        self.merge_shas: dict[str, str] = {}
        self.git_calls: list[tuple] = []
        self.merge_calls: list[tuple] = []
        self.pr_state_calls: list[str] = []
        self.logs: list[str] = []
        self.closed: list[str] = []
        self.returned: list[tuple] = []
        self.parked: list[str] = []
        self.alerts: list[tuple] = []
        self.registries: list[str] = []

    # -- the seams ---------------------------------------------------------
    def git(self, cwd, *args, timeout=None):
        self.git_calls.append(args)
        if args and args[0] in self.fail_git:
            return subprocess.CompletedProcess(args, 1, "", "simulated git failure\n")
        return subprocess.run(["git", "-C", str(cwd), *args],
                              capture_output=True, text=True)

    def _merge_pr(self, clone, branch, repo_url) -> dict:
        self.merge_calls.append((Path(clone), branch, repo_url))
        answers = self.merge_answers.get(branch)
        if answers:
            return answers.pop(0)
        if self.real_merge:
            self.merge_shas[branch] = self.repo.squash_merge(branch)
        if branch in self.after_merge:
            self.after_merge[branch]()
        return {"merged": True, "note": f"squash-merged {branch}"}

    def _pr_state(self, branch, repo_url):
        self.pr_state_calls.append(branch)
        return self.pr_states.get(branch, "OPEN")

    def env(self) -> batch_gate.Env:
        return batch_gate.Env(
            git=self.git, gate=self.gate, merge_pr=self._merge_pr,
            pr_state=self._pr_state,
            pr_merged=lambda branch, repo_url: self.pr_merged.get(branch),
            merge_sha=lambda clone, branch, default: self.merge_shas.get(branch),
            remote_head=lambda clone, name: batch_gate.remote_head(self.git, clone, name),
            now=lambda: self.now, log=self.logs.append,
            clone_for=lambda repo_url: self.repo.clone if self.clone_exists else None,
            worktree_for=lambda repo_url, name: (
                self.repo.clone.parent / f"{NAME}.wt" / name),
            remove_registry=self.registries.append,
            default_branch=lambda clone: "main")

    def hooks(self) -> batch_gate.Hooks:
        def close(a, note):
            self.closed.append(a.task_id)
            a.state, a.last_note = A.DONE, note

        def returned(a, reason):
            self.returned.append((a.task_id, reason))
            batch_gate.mark_returned(a, reason, now_iso=m.now_iso())

        def park(a, note):
            self.parked.append(a.task_id)
            a.state, a.last_note = A.AWAITING_HUMAN, note

        def alert(kind, repo_name, slugs, detail):
            self.alerts.append((kind, repo_name, list(slugs), dict(detail)))

        return batch_gate.Hooks(close=close, returned=returned, park=park, alert=alert)

    # -- drive + read back ---------------------------------------------------
    def waiting(self) -> list[Assignment]:
        return [a for a in self.rows.values() if a.state == A.BATCH_READY]

    def step(self, gate_cfg=GATE, **kw) -> dict:
        return batch_gate.step(URL, self.waiting(), gate_cfg, self.env(), self.hooks(),
                               vault_root=self.vault, **kw)

    def job(self) -> dict | None:
        return batch_gate.load_job(self.vault, NAME)

    def run_gate(self, verdict: str = batch_gate.GATE_PASSED) -> dict:
        """Start the gate, then let it finish with `verdict` and step again."""
        self.step()
        self.gate.verdict = verdict
        return self.step()

    def assert_torn_down(self) -> None:
        assert self.repo.integ_worktrees() == []
        assert self.repo.integ_branches() == []


@pytest.fixture
def world(tmp_path, monkeypatch) -> _World:
    return _World(tmp_path, monkeypatch)


@pytest.fixture
def pair(tmp_path, monkeypatch) -> _World:
    """Only `a` and `b` wait — the two that merge cleanly."""
    return _World(tmp_path, monkeypatch, slugs="ab")


# --- the trigger (pure) --------------------------------------------------------

def _ready(slug: str, at: float) -> Assignment:
    return _row(slug, "0" * 40, ready=at)


def test_trigger_waits_for_a_second_task_or_for_the_wait_to_run_out():
    one = [_ready("a", T0)]
    assert not batch_gate.trigger_due([], T0 + 10 * batch_gate.MAX_WAIT_S)
    assert not batch_gate.trigger_due(one, T0 + 1)
    assert not batch_gate.trigger_due(one, T0 + batch_gate.MAX_WAIT_S - 1)
    assert batch_gate.trigger_due(one, T0 + batch_gate.MAX_WAIT_S)
    assert batch_gate.trigger_due(one + [_ready("b", T0 + 5)], T0 + 6)
    # the OLDEST row decides the wait
    assert batch_gate.trigger_due([_ready("new", T0 + batch_gate.MAX_WAIT_S)],
                                  T0 + batch_gate.MAX_WAIT_S + 1) is False
    assert batch_gate.MIN_BATCH == 2 and batch_gate.MAX_WAIT_S == 1800


def test_trigger_is_due_at_once_when_a_rebuild_is_owed():
    assert batch_gate.trigger_due([_ready("a", T0)], T0 + 1, restart_due=True)
    assert not batch_gate.trigger_due([], T0 + 1, restart_due=True)


def test_limits_take_positive_overrides_from_the_repo_entry():
    assert batch_gate.limits(None) == batch_gate.limits({}) == {
        "min_batch": 2, "max_wait_s": 1800, "max_batch": 5, "gate_timeout_s": 7200}
    lim = batch_gate.limits({"max_wait_min": 5, "max_batch": 2, "gate_timeout_min": 10})
    assert (lim["max_wait_s"], lim["max_batch"], lim["gate_timeout_s"]) == (300, 2, 600)
    for bad in (0, -3, "30", None, True):
        assert batch_gate.limits({"max_wait_min": bad, "max_batch": bad,
                                  "gate_timeout_min": bad}) == batch_gate.limits(None)
    one = [_ready("a", T0)]
    assert batch_gate.trigger_due(one, T0 + 301, max_wait_s=lim["max_wait_s"])
    assert not batch_gate.trigger_due(one, T0 + 299, max_wait_s=lim["max_wait_s"])


def test_pick_takes_the_oldest_first_up_to_the_cap():
    rows = [_ready("late", T0 + 30), _ready("b-tie", T0 + 10), _ready("a-tie", T0 + 10),
            _ready("first", T0)]
    assert [a.task_id for a in batch_gate.pick(rows)] == [
        "first", "a-tie", "b-tie", "late"]
    assert [a.task_id for a in batch_gate.pick(rows, 2)] == ["first", "a-tie"]
    assert [a.task_id for a in batch_gate.pick(rows, 0)] == ["first"]   # never nobody
    assert batch_gate.pick([]) == []


def test_member_reads_the_recorded_branch_and_never_guesses_one():
    a = _row("a", "f" * 40, n=3)
    assert batch_gate.member_of(a) == {
        "slug": "a", "branch": "worker/a", "head_sha": "f" * 40,
        "pr_url": "https://example.test/acme/batchrepo/pull/3"}
    a.payload[batch_gate.KEY_BATCH]["branch"] = ""
    assert batch_gate.member_of(a)["branch"] == ""          # not worker/a
    del a.payload[batch_gate.KEY_BATCH]
    assert batch_gate.member_of(a) == {"slug": "a", "branch": "", "head_sha": "",
                                       "pr_url": ""}
    assert batch_gate.ready_epoch(a) == 0.0


# --- nothing to do -------------------------------------------------------------

def test_step_below_the_trigger_touches_nothing(tmp_path, monkeypatch):
    w = _World(tmp_path, monkeypatch, slugs="a")
    assert w.step() == EMPTY
    assert w.git_calls == [] and w.pr_state_calls == [] and w.logs == []
    assert w.gate.started == []
    assert not batch_gate.state_dir(w.vault).exists()
    assert not batch_gate.wants_step(w.vault, NAME, w.waiting(), GATE, w.now)
    # …and the same row, once it has waited, is worth a step
    late = T0 + batch_gate.MAX_WAIT_S + 1
    assert batch_gate.wants_step(w.vault, NAME, w.waiting(), GATE, late)
    assert not batch_gate.wants_step(w.vault, NAME, w.waiting(), None, late)


def test_step_starts_a_lone_task_once_it_has_waited(tmp_path, monkeypatch):
    w = _World(tmp_path, monkeypatch, slugs="a")
    w.now = T0 + batch_gate.MAX_WAIT_S + 1
    assert w.step()["started"] == ["a"]
    assert w.gate.started[0]["members"] == ["a"]


# --- building the integration branch ---------------------------------------------

def test_step_gates_the_tasks_that_merge_and_returns_the_one_that_conflicts(world):
    result = world.step()

    assert result == {**EMPTY, "started": ["a", "b"], "returned": ["c"]}
    assert world.returned == [("c", batch_gate.RETURN_CONFLICT)]
    [started] = world.gate.started                       # ONE gate for the batch
    assert started["members"] == ["a", "b"]              # ready order
    assert started["files"] == ["a.txt", "b.txt", "shared.txt"]
    assert started["shared"] == "main's edit\n"          # c's edit is not in the tree
    assert started["full_gate"] == GATE["full_gate"]
    job = world.job()
    assert job["state"] == batch_gate.JOB_RUNNING
    assert [member["slug"] for member in job["members"]] == ["a", "b"]
    assert job["members"][0] == batch_gate.member_of(world.rows["a"])
    assert job["base_sha"] == job["expect_base"] == world.repo.origin_sha("main")
    assert job["head_sha"] == _git(world.repo.clone, "rev-parse", job["branch"])
    assert job["branch"].startswith("integ/batchrepo/")
    assert Path(job["worktree"]).name.startswith("integ-batchrepo-")
    assert job["started_ts"] == world.now and job["restarts"] == 0
    assert batch_gate.running_jobs(world.vault) == [job]
    # the conflicting task is on its way to an individual full verify
    c = world.rows["c"]
    assert c.state == A.FAILED and A.is_batch_return_pending(c)
    assert c.payload[batch_gate.KEY_VERIFY_FULL] is True
    assert world.rows["a"].state == world.rows["b"].state == A.BATCH_READY
    # nobody was told anything yet, and nothing was merged
    assert world.alerts == [] and world.merge_calls == [] and world.closed == []


def test_the_integration_branch_never_leaves_this_host(world):
    world.step()
    assert _git(world.repo.origin, "for-each-ref", "refs/heads/integ/") == ""
    assert _git(world.repo.clone, "ls-remote", "origin", "refs/heads/integ/*") == ""
    assert not any("push" in call for call in world.git_calls)
    # and it does not track the default branch
    branch = world.job()["branch"]
    done = subprocess.run(["git", "-C", str(world.repo.clone), "config", "--get",
                           f"branch.{branch}.merge"], capture_output=True, text=True)
    assert done.stdout.strip() == ""


def test_a_branch_that_moved_after_the_quick_verify_is_left_out(pair):
    pair.repo.push_branch("a", "a2.txt", "later\n", base="origin/worker/a")
    result = pair.step()
    assert result == {**EMPTY, "started": ["b"], "returned": ["a"]}
    assert pair.returned == [("a", batch_gate.RETURN_BRANCH_MOVED)]
    assert pair.gate.started[0]["files"] == ["b.txt", "shared.txt"]


def test_a_branch_that_is_gone_from_origin_is_left_out(pair):
    pair.repo.delete_branch("worker/b")
    result = pair.step()
    assert result == {**EMPTY, "started": ["a"], "returned": ["b"]}
    assert pair.returned == [("b", batch_gate.RETURN_BRANCH_MISSING)]


def test_a_row_with_no_recorded_branch_is_left_out_not_guessed(pair):
    pair.rows["a"].payload[batch_gate.KEY_BATCH]["branch"] = ""
    assert pair.step()["returned"] == ["a"]
    assert pair.returned == [("a", batch_gate.RETURN_BRANCH_MISSING)]
    assert "" not in pair.pr_state_calls                 # gh is not asked about nothing


def test_no_shared_clone_sends_every_task_back(world):
    world.clone_exists = False
    result = world.step()
    assert result == {**EMPTY, "returned": ["a", "b", "c"]}
    assert {reason for _slug, reason in world.returned} == {batch_gate.RETURN_NO_CHECKOUT}
    assert world.git_calls == [] and world.pr_state_calls == []
    assert world.job() is None and world.gate.started == []


def test_nothing_mergeable_ends_without_a_gate(tmp_path, monkeypatch):
    w = _World(tmp_path, monkeypatch, slugs="c")
    w.now = T0 + batch_gate.MAX_WAIT_S + 1
    assert w.step() == {**EMPTY, "returned": ["c"]}
    assert w.gate.started == [] and w.job() is None
    w.assert_torn_down()


def test_a_git_failure_decides_nothing_and_is_retried(world):
    world.fail_git = {"fetch"}
    assert world.step() == EMPTY
    assert world.returned == [] and world.closed == [] and world.alerts == []
    assert world.job() is None and world.gate.started == []
    assert len(world.waiting()) == 3
    assert "retrying next tick" in world.logs[-1]
    world.assert_torn_down()

    world.fail_git = set()
    assert world.step()["started"] == ["a", "b"]


def test_a_gate_that_does_not_start_is_retried(pair):
    pair.gate.fail_start = True
    assert pair.step() == EMPTY
    assert pair.job() is None and len(pair.waiting()) == 2
    assert "gate did not start" in pair.logs[-1]
    pair.assert_torn_down()

    pair.gate.fail_start = False
    assert pair.step()["started"] == ["a", "b"]


def test_a_new_build_clears_what_an_earlier_job_left_behind(pair):
    clone = pair.repo.clone
    stale = clone.parent / f"{NAME}.wt" / "integ-batchrepo-20200101T000000"
    _git(clone, "worktree", "add", "-q", "-B", "integ/batchrepo/20200101T000000",
         str(stale), "origin/main")
    _git(clone, "branch", "worker/keep", "origin/main")
    assert pair.repo.integ_worktrees() == [stale.name]

    pair.step()

    job = pair.job()
    assert pair.repo.integ_worktrees() == [Path(job["worktree"]).name]
    assert pair.repo.integ_branches() == [job["branch"]]
    assert _git(clone, "rev-parse", "--verify", "worker/keep")   # not an integ name


# --- PRs a person dealt with while they waited --------------------------------------

def test_a_pr_merged_or_closed_by_a_person_is_not_gated(world):
    world.pr_states = {"worker/a": "MERGED", "worker/b": "CLOSED"}
    result = world.step()
    # a is closed, b is a person's to decide; c alone has not waited long enough
    assert result == {**EMPTY, "closed": ["a"]}
    assert world.closed == ["a"] and world.parked == ["b"]
    assert world.rows["c"].state == A.BATCH_READY
    assert world.gate.started == [] and world.git_calls == []
    assert world.job() is None


def test_an_unknown_pr_state_keeps_the_task_in_the_batch(pair):
    pair.pr_states = {"worker/a": None}                  # gh could not say
    assert pair.step()["started"] == ["a", "b"]
    assert sorted(pair.pr_state_calls) == ["worker/a", "worker/b"]


# --- holds on starting ---------------------------------------------------------------

def test_paused_holds_a_new_gate_but_not_one_that_is_out(pair):
    assert pair.step(paused=True) == EMPTY
    assert pair.gate.started == [] and pair.git_calls == [] and pair.job() is None

    pair.step()
    pair.gate.verdict = batch_gate.GATE_PASSED
    assert pair.step(paused=True)["merged"] == ["a", "b"]


def test_a_reached_gradle_cap_defers_the_start(pair):
    asked = []

    def busy():
        asked.append(1)
        return True

    assert pair.step(gradle_busy=busy) == EMPTY
    assert asked == [1]
    assert pair.gate.started == [] and pair.git_calls == [] and pair.job() is None
    assert len(pair.waiting()) == 2
    assert "gradle build cap" in pair.logs[-1]
    assert pair.step(gradle_busy=lambda: False)["started"] == ["a", "b"]


def test_the_gradle_cap_is_not_asked_below_the_trigger(tmp_path, monkeypatch):
    w = _World(tmp_path, monkeypatch, slugs="a")
    assert w.step(gradle_busy=lambda: pytest.fail("asked with nothing to start")) == EMPTY


# --- the gate's verdict -----------------------------------------------------------------

def test_a_running_gate_is_only_waited_for(pair):
    pair.step()
    calls = len(pair.git_calls)
    assert pair.step() == EMPTY
    assert len(pair.git_calls) == calls                  # a poll, no git
    assert pair.job()["state"] == batch_gate.JOB_RUNNING
    assert pair.merge_calls == [] and pair.gate.stopped == []


def test_a_passed_gate_merges_the_prs_in_order_and_closes_the_tasks(pair):
    pair.step()
    job = pair.job()
    pair.gate.verdict = batch_gate.GATE_PASSED

    result = pair.step()

    assert result == {**EMPTY, "merged": ["a", "b"]}
    assert pair.merge_calls == [(pair.repo.clone, "worker/a", URL),
                                (pair.repo.clone, "worker/b", URL)]
    assert pair.closed == ["a", "b"]
    assert pair.returned == []
    assert pair.alerts == [(batch_gate.NOTICE_PASSED, NAME, ["a", "b"],
                            {"branch": job["branch"]})]
    # nothing is left on the host
    assert pair.job() is None and not batch_gate.has_jobs(pair.vault)
    pair.assert_torn_down()
    assert pair.gate.cleaned == [job["branch"]]
    assert pair.registries == [Path(job["worktree"]).name]
    assert pair.step() == EMPTY                          # and nothing is left to do


def test_a_failed_gate_returns_every_member_with_one_notice(pair):
    pair.step()
    job = pair.job()
    pair.gate.verdict = batch_gate.GATE_FAILED

    result = pair.step()

    assert result == {**EMPTY, "returned": ["a", "b"]}
    assert pair.returned == [("a", batch_gate.RETURN_GATE_FAILED),
                             ("b", batch_gate.RETURN_GATE_FAILED)]
    assert pair.alerts == [(batch_gate.NOTICE_FAILED, NAME, ["a", "b"], {
        "branch": job["branch"], "log": "Worker/jobs/batch-gate-batchrepo.log",
        "reason": batch_gate.RETURN_GATE_FAILED})]
    assert pair.merge_calls == [] and pair.closed == []
    assert pair.job() is None
    pair.assert_torn_down()
    for a in pair.rows.values():
        assert a.state == A.FAILED and a.display_state == "deferred"
        assert a.refail_count == 0
    # a second look changes nothing and tells nobody again
    assert pair.step() == EMPTY
    assert len(pair.alerts) == 1


def test_a_gate_past_its_time_limit_is_stopped_and_counts_as_failed(pair):
    pair.step()
    branch = pair.job()["branch"]
    pair.now += batch_gate.GATE_TIMEOUT_S - 1
    assert pair.step() == EMPTY                          # still inside the limit
    assert pair.gate.stopped == []

    pair.now += 1
    result = pair.step()

    assert result == {**EMPTY, "returned": ["a", "b"]}
    assert pair.gate.stopped == [branch]
    assert {reason for _slug, reason in pair.returned} == {batch_gate.RETURN_GATE_TIMEOUT}
    [(kind, _name, slugs, detail)] = pair.alerts
    assert (kind, slugs, detail["reason"]) == (
        batch_gate.NOTICE_FAILED, ["a", "b"], batch_gate.RETURN_GATE_TIMEOUT)
    assert pair.job() is None
    pair.assert_torn_down()


def test_the_time_limit_follows_the_repo_entry(pair):
    cfg = {**GATE, "gate_timeout_min": 1}
    pair.step(cfg)
    pair.now += 61
    assert pair.step(cfg)["returned"] == ["a", "b"]


# --- the default branch moves -------------------------------------------------------------

def test_a_moved_default_branch_restarts_the_job_instead_of_merging(pair):
    pair.step()
    first = pair.job()
    pair.gate.verdict = batch_gate.GATE_PASSED
    moved = pair.repo.push_main("elsewhere.txt", "someone else\n")

    assert pair.step() == EMPTY

    assert pair.merge_calls == [] and pair.closed == [] and pair.returned == []
    assert pair.job() == {"repo": URL, "repo_name": NAME, "state": batch_gate.JOB_IDLE,
                          "restart_due": True, "restarts": 1}
    pair.assert_torn_down()
    assert len(pair.waiting()) == 2                      # still batch_ready
    assert batch_gate.running_jobs(pair.vault) == []

    # the next step rebuilds on the new base and gates again — the gate's first
    # answer covered a tree that no longer exists
    pair.gate.verdict = batch_gate.GATE_RUNNING
    pair.now += 300
    assert pair.step()["started"] == ["a", "b"]
    second = pair.job()
    assert second["base_sha"] == moved != first["base_sha"]
    assert second["restarts"] == 1 and second["state"] == batch_gate.JOB_RUNNING
    assert len(pair.gate.started) == 2
    assert pair.gate.started[1]["files"] == ["a.txt", "b.txt", "elsewhere.txt",
                                             "shared.txt"]


def test_a_rebuild_does_not_wait_for_the_trigger_again(tmp_path, monkeypatch):
    w = _World(tmp_path, monkeypatch, slugs="a")
    w.now = T0 + batch_gate.MAX_WAIT_S + 1
    w.run_gate(batch_gate.GATE_RUNNING)
    w.repo.push_main("elsewhere.txt", "x\n")
    w.gate.verdict = batch_gate.GATE_PASSED
    w.step()                                             # restart 1
    assert w.job()["restart_due"] is True
    # one row, and a config that would make it wait a week: the owed rebuild wins
    patient = {**GATE, "max_wait_min": 7 * 24 * 60}
    w.gate.verdict = batch_gate.GATE_RUNNING
    assert w.step(patient)["started"] == ["a"]


def test_a_default_branch_that_keeps_moving_sends_the_tasks_back(pair):
    pair.gate.verdict = batch_gate.GATE_PASSED
    for n in range(1, batch_gate.MAX_RESTARTS + 1):
        pair.step()                                      # build + start
        pair.repo.push_main(f"move{n}.txt", "x\n")
        assert pair.step() == EMPTY                      # passed, but main moved
        assert pair.job()["restarts"] == n
    assert pair.alerts == [] and pair.returned == []

    pair.step()
    last = pair.job()
    pair.repo.push_main("again.txt", "x\n")
    result = pair.step()

    assert result == {**EMPTY, "returned": ["a", "b"]}
    assert pair.returned == [("a", batch_gate.RETURN_RESTARTS),
                             ("b", batch_gate.RETURN_RESTARTS)]
    [(kind, name, slugs, detail)] = pair.alerts
    assert (kind, name, slugs) == (batch_gate.NOTICE_RESTARTS, NAME, ["a", "b"])
    assert detail["branch"] == last["branch"]
    assert pair.job() is None and pair.merge_calls == []
    pair.assert_torn_down()
    assert len(pair.gate.started) == batch_gate.MAX_RESTARTS + 1


def test_our_own_squash_merge_is_not_a_move(pair):
    pair.real_merge = True                               # merge_pr squashes on origin
    pair.step()
    gated = pair.job()["head_sha"]
    pair.gate.verdict = batch_gate.GATE_PASSED

    assert pair.step()["merged"] == ["a", "b"]

    assert pair.closed == ["a", "b"] and pair.job() is None
    assert len(pair.gate.started) == 1                   # no second gate
    # what landed on the default branch is exactly the tree the gate ran on
    assert pair.repo.origin_sha("main^{tree}") == _git(
        pair.repo.clone, "rev-parse", f"{gated}^{{tree}}")
    assert pair.repo.origin_sha("main") == pair.merge_shas["worker/b"]


def test_an_unknown_squash_sha_falls_back_to_what_origin_shows(pair):
    pair.real_merge = True
    real = pair._merge_pr

    def forgetful(clone, branch, repo_url):
        answer = real(clone, branch, repo_url)
        pair.merge_shas.clear()                          # gh cannot name the commit
        return answer

    pair._merge_pr = forgetful
    assert pair.run_gate()["merged"] == ["a", "b"]
    assert pair.job() is None


def test_somebody_elses_push_between_our_merges_restarts_the_rest(pair):
    pair.real_merge = True
    pair.after_merge["worker/a"] = lambda: pair.repo.push_main("elsewhere.txt", "x\n")

    result = pair.run_gate()

    assert result == {**EMPTY, "merged": ["a"]}
    assert pair.closed == ["a"]                          # a landed and stays closed
    assert [call[1] for call in pair.merge_calls] == ["worker/a"]
    assert pair.rows["b"].state == A.BATCH_READY         # b is gated again
    assert pair.job() == {"repo": URL, "repo_name": NAME, "state": batch_gate.JOB_IDLE,
                          "restart_due": True, "restarts": 1}
    pair.assert_torn_down()

    pair.gate.verdict = batch_gate.GATE_RUNNING
    assert pair.step()["started"] == ["b"]
    assert pair.gate.started[1]["files"] == ["a.txt", "b.txt", "elsewhere.txt",
                                             "shared.txt"]


# --- a merge that does not go through --------------------------------------------------------

REFUSED = {"merged": False, "note": "gh pr merge of the branch failed: not mergeable"}


def test_an_unconfirmed_merge_stops_there_and_continues_next_time(pair):
    pair.merge_answers["worker/b"] = [dict(REFUSED)]

    result = pair.run_gate()

    assert result == {**EMPTY, "merged": ["a"]}
    assert pair.closed == ["a"]                          # b is NOT closed on an open PR
    job = pair.job()
    assert job["state"] == batch_gate.JOB_PASSED
    assert job["merged"] == ["a"] and job["merge_attempts"] == {"b": 1}
    assert pair.rows["b"].state == A.BATCH_READY and pair.alerts == []

    assert pair.step() == {**EMPTY, "merged": ["b"]}
    assert [call[1] for call in pair.merge_calls] == ["worker/a", "worker/b", "worker/b"]
    assert pair.closed == ["a", "b"]                     # a was not merged twice
    assert pair.job() is None
    assert len(pair.gate.started) == 1                   # the one gate answer was enough


def test_a_merge_gh_reports_as_landed_counts(pair):
    pair.merge_answers["worker/a"] = [dict(REFUSED)]
    pair.pr_merged["worker/a"] = True                    # e.g. the reply was lost
    assert pair.run_gate()["merged"] == ["a", "b"]
    assert pair.closed == ["a", "b"]


def test_a_pr_that_will_not_merge_goes_back_after_three_tries(pair):
    pair.merge_answers["worker/a"] = [dict(REFUSED) for _ in range(batch_gate.MERGE_ATTEMPTS)]
    pair.run_gate()
    assert pair.job()["merge_attempts"] == {"a": 1}
    pair.step()
    assert pair.job()["merge_attempts"] == {"a": 2} and pair.returned == []

    result = pair.step()

    assert result == {**EMPTY, "returned": ["a"]}
    assert pair.returned == [("a", batch_gate.RETURN_MERGE_FAILED)]
    assert pair.closed == []                             # b was not merged without a
    assert [call[1] for call in pair.merge_calls] == ["worker/a"] * 3
    # b is rebuilt and gated on its own
    assert pair.job()["state"] == batch_gate.JOB_IDLE and pair.job()["restarts"] == 1
    assert pair.rows["b"].state == A.BATCH_READY


def test_a_member_branch_that_moved_after_the_gate_is_not_merged(pair):
    pair.step()
    pair.repo.push_branch("a", "a2.txt", "late push\n", base="origin/worker/a")
    pair.gate.verdict = batch_gate.GATE_PASSED

    result = pair.step()

    assert result == {**EMPTY, "returned": ["a"]}
    assert pair.returned == [("a", batch_gate.RETURN_BRANCH_MOVED)]
    assert pair.merge_calls == []                        # the gate never saw that commit
    assert pair.job()["restart_due"] is True


def test_an_unreadable_origin_pauses_the_merge_without_deciding(pair):
    pair.step()
    pair.gate.verdict = batch_gate.GATE_PASSED
    pair.fail_git = {"ls-remote"}

    assert pair.step() == EMPTY

    assert pair.merge_calls == [] and pair.returned == []
    assert pair.job()["state"] == batch_gate.JOB_PASSED
    pair.fail_git = set()
    assert pair.step()["merged"] == ["a", "b"]


# --- rows that leave while a job is out ---------------------------------------------------------

def test_a_job_whose_rows_are_all_gone_is_dropped(pair):
    pair.step()
    branch = pair.job()["branch"]
    for a in pair.rows.values():
        a.state = A.DONE                                 # e.g. closed by hand
    assert pair.step() == EMPTY
    assert pair.job() is None and pair.gate.stopped == [branch]
    assert pair.returned == [] and pair.alerts == []
    pair.assert_torn_down()


def test_a_member_that_leaves_mid_gate_restarts_the_batch(pair):
    pair.step()
    branch = pair.job()["branch"]
    pair.rows["a"].state = A.FAILED                      # e.g. expired by the sweep
    assert pair.step() == EMPTY
    assert pair.gate.stopped == [branch]                 # that gate answers for nothing
    assert pair.job()["state"] == batch_gate.JOB_IDLE and pair.job()["restarts"] == 1
    assert pair.rows["b"].state == A.BATCH_READY
    pair.assert_torn_down()


def test_a_repo_that_left_the_config_drops_its_job(pair):
    pair.step()
    assert pair.step(None) == EMPTY
    assert pair.job() is None and len(pair.gate.stopped) == 1
    assert len(pair.waiting()) == 2                      # the sweep returns them, not step
    pair.assert_torn_down()


def test_a_record_with_an_unknown_state_is_dropped(pair):
    batch_gate.save_job(pair.vault, {"repo": URL, "repo_name": NAME, "state": "odd"})
    assert pair.step() == EMPTY
    assert pair.job() is None


# --- the job record ---------------------------------------------------------------------------

def test_job_records_round_trip_and_bad_files_are_ignored(tmp_path):
    vault = tmp_path / "vault"
    assert batch_gate.load_job(vault, NAME) is None
    assert batch_gate.all_jobs(vault) == [] and not batch_gate.has_jobs(vault)
    assert not batch_gate.state_dir(vault).exists()      # reading creates nothing

    running = {"repo": URL, "repo_name": NAME, "state": batch_gate.JOB_RUNNING,
               "members": [{"slug": "a"}], "restarts": 2}
    idle = {"repo": "https://example.test/acme/other.git", "repo_name": "other",
            "state": batch_gate.JOB_IDLE, "restart_due": True, "restarts": 1}
    batch_gate.save_job(vault, running)
    batch_gate.save_job(vault, idle)
    (batch_gate.state_dir(vault) / "broken.json").write_text("{not json", encoding="utf-8")
    (batch_gate.state_dir(vault) / "list.json").write_text("[1, 2]", encoding="utf-8")

    assert batch_gate.job_path(vault, NAME) == vault / "Worker/_state/batch_gate/batchrepo.json"
    assert batch_gate.load_job(vault, NAME) == running
    assert batch_gate.load_job(vault, "broken") is None
    assert batch_gate.load_job(vault, "list") is None
    assert batch_gate.all_jobs(vault) == [running, idle]
    assert batch_gate.running_jobs(vault) == [running]
    assert batch_gate.has_jobs(vault)

    batch_gate.clear_job(vault, NAME)
    batch_gate.clear_job(vault, NAME)                    # twice is fine
    assert batch_gate.load_job(vault, NAME) is None
    assert batch_gate.running_jobs(vault) == []


@pytest.mark.parametrize("bad", ["", ".hidden", None])
def test_a_repo_name_that_cannot_name_a_file_is_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        batch_gate.job_path(tmp_path, bad)


def test_repo_names_are_made_path_safe(tmp_path):
    assert batch_gate.job_path(tmp_path, "a/b c").name == "a_b_c.json"
    assert batch_gate.gate_job_slug("a/b") == "batch-gate-a_b"


def test_the_repo_lock_is_exclusive_and_does_not_wait(tmp_path):
    with batch_gate.repo_lock(tmp_path, NAME) as first:
        assert first is True
        with batch_gate.repo_lock(tmp_path, NAME) as second:
            assert second is False                       # held elsewhere: skip, don't wait
        with batch_gate.repo_lock(tmp_path, "other") as other:
            assert other is True                         # per repo
    with batch_gate.repo_lock(tmp_path, NAME) as again:
        assert again is True                             # released on exit


# --- LocalDetachedGate ------------------------------------------------------------------------

class _JobsSpy:
    """jobs.py and the daemon cleanup, recorded instead of run."""

    def __init__(self, monkeypatch):
        self.calls: list[tuple] = []
        self.state = "running"
        self.pid: object = 4242
        monkeypatch.setattr(jobs, "clear", lambda slug: self.calls.append(("clear", slug)))
        monkeypatch.setattr(jobs, "start", self._start)
        monkeypatch.setattr(jobs, "poll", lambda slug: {"state": self.state})
        monkeypatch.setattr(jobs, "read", lambda slug: {"slug": slug, "pid": self.pid})
        monkeypatch.setattr(jobs, "stop", lambda slug: self.calls.append(("stop", slug)))
        monkeypatch.setattr(stages, "cleanup_build_daemons", self._daemons)

    def _start(self, slug, cmd, cwd=None):
        self.calls.append(("start", slug, cmd, str(cwd)))
        return {"slug": slug, "pid": self.pid}

    def _daemons(self, repo_dir, progress=None):
        self.calls.append(("daemons", str(repo_dir)))
        return {"ran": False, "note": ""}


def _gate_job(tmp_path: Path) -> dict:
    clone = tmp_path / NAME
    worktree = tmp_path / f"{NAME}.wt" / "integ-batchrepo-20260101T000000"
    clone.mkdir()
    worktree.mkdir(parents=True)
    return {"repo_name": NAME, "clone": str(clone), "worktree": str(worktree),
            "full_gate": [["./gradlew", ":app:testDebugUnitTest"],
                          ["./gradlew", "lint", "-Pflag=a b"]]}


def test_local_gate_starts_the_full_gate_as_one_detached_job(tmp_path, monkeypatch):
    spy = _JobsSpy(monkeypatch)
    monkeypatch.setenv("GRADLE_OPTS", "-Xmx1g")
    job = _gate_job(tmp_path)
    (Path(job["clone"]) / "local.properties").write_text("sdk.dir=/sdk\n", encoding="utf-8")

    info = batch_gate.LocalDetachedGate().start(job)

    assert info == {"job": "batch-gate-batchrepo",
                    "log": "Worker/jobs/batch-gate-batchrepo.log"}
    assert [call[0] for call in spy.calls] == ["clear", "start"]
    _kind, slug, cmd, cwd = spy.calls[1]
    assert (slug, cwd) == ("batch-gate-batchrepo", job["worktree"])
    assert cmd.startswith("export GRADLE_OPTS='-Xmx1g " + stages.WORKER_GRADLE_OPTS)
    # the daemons register under THIS worktree's name, so cleanup stops only them
    assert str(stages.gradle_registry_base(job["worktree"])) in cmd
    assert cmd.endswith("; ./gradlew :app:testDebugUnitTest && "
                        "./gradlew lint '-Pflag=a b'")
    # the gitignored build input a fresh worktree lacks
    assert (Path(job["worktree"]) / "local.properties").read_text(
        encoding="utf-8") == "sdk.dir=/sdk\n"


def test_local_gate_start_without_inherited_gradle_opts(tmp_path, monkeypatch):
    spy = _JobsSpy(monkeypatch)
    monkeypatch.delenv("GRADLE_OPTS", raising=False)
    job = _gate_job(tmp_path)
    batch_gate.LocalDetachedGate().start(job)
    assert spy.calls[1][2].startswith("export GRADLE_OPTS='" + stages.WORKER_GRADLE_OPTS)
    assert not (Path(job["worktree"]) / "local.properties").exists()


@pytest.mark.parametrize("state, verdict", [
    ("running", batch_gate.GATE_RUNNING), ("done", batch_gate.GATE_PASSED),
    ("failed", batch_gate.GATE_FAILED), ("absent", batch_gate.GATE_FAILED)])
def test_local_gate_poll_maps_the_job_state(tmp_path, monkeypatch, state, verdict):
    spy = _JobsSpy(monkeypatch)
    spy.state = state
    assert batch_gate.LocalDetachedGate().poll(_gate_job(tmp_path)) == verdict


def _group_spy(monkeypatch, pgid: int, own: int = 77) -> list[tuple]:
    killed: list[tuple] = []
    monkeypatch.setattr(os, "getpgid", lambda pid: pgid)
    monkeypatch.setattr(os, "getpgrp", lambda: own)
    monkeypatch.setattr(os, "killpg", lambda group, sig: killed.append((group, sig)))
    return killed


def test_local_gate_stop_signals_the_whole_job_group(tmp_path, monkeypatch):
    spy = _JobsSpy(monkeypatch)
    killed = _group_spy(monkeypatch, pgid=9000)
    batch_gate.LocalDetachedGate().stop(_gate_job(tmp_path))
    assert killed == [(9000, signal.SIGTERM)]
    assert spy.calls == [("stop", "batch-gate-batchrepo")]


@pytest.mark.parametrize("pid, pgid, state", [
    (4242, 77, "running"),       # the job shares OUR process group
    (4242, 1, "running"),        # group 1
    (4242, 0, "running"),        # group 0 = the caller's own
    (-1, 9000, "running"),       # jobs.start's "never launched"
    (1, 9000, "running"),        # init
    (True, 9000, "running"),     # a bool is not a pid
    ("4242", 9000, "running"),   # nor is a string
    (4242, 9000, "done"),        # the job ended: that pid may be anything now
    (4242, 9000, "absent"),
])
def test_local_gate_stop_never_signals_anything_else(tmp_path, monkeypatch, pid, pgid, state):
    spy = _JobsSpy(monkeypatch)
    spy.pid, spy.state = pid, state
    killed = _group_spy(monkeypatch, pgid=pgid)
    batch_gate.LocalDetachedGate().stop(_gate_job(tmp_path))
    assert killed == []


def test_local_gate_stop_swallows_a_vanished_process(tmp_path, monkeypatch):
    _JobsSpy(monkeypatch)

    def gone(pid):
        raise ProcessLookupError(pid)

    monkeypatch.setattr(os, "getpgid", gone)
    batch_gate.LocalDetachedGate().stop(_gate_job(tmp_path))     # does not raise


def test_local_gate_cleanup_stops_the_daemons_then_forgets_the_job(tmp_path, monkeypatch):
    spy = _JobsSpy(monkeypatch)
    job = _gate_job(tmp_path)
    batch_gate.LocalDetachedGate().cleanup(job)
    assert spy.calls == [("daemons", job["worktree"]), ("clear", "batch-gate-batchrepo")]


def test_default_env_wires_the_engine_helpers_at_call_time(tmp_path, monkeypatch):
    # The defaults must find stages.* when CALLED: a later patch is honoured.
    env = batch_gate.default_env(now=lambda: 5.0, log=lambda msg: None)
    seen = []
    monkeypatch.setattr(stages, "merge_pr",
                        lambda repo_dir, branch, repo=None: seen.append(
                            (repo_dir, branch, repo)) or {"merged": True, "note": ""})
    monkeypatch.setattr(stages, "gh_pr_state", lambda branch, repo=None: "OPEN")
    monkeypatch.setattr(stages, "gh_pr_merged", lambda branch, repo=None: None)
    assert env.merge_pr(str(tmp_path), "worker/a", URL) == {"merged": True, "note": ""}
    assert seen == [(tmp_path, "worker/a", URL)]
    assert env.pr_state("worker/a", URL) == "OPEN"
    assert env.pr_merged("worker/a", URL) is None
    assert env.now() == 5.0
    # the shared clone is found only when it is a git checkout
    monkeypatch.setattr(repos, "local_path", lambda url, worker_root=None: tmp_path / NAME)
    assert env.clone_for(URL) is None
    (tmp_path / NAME / ".git").mkdir(parents=True)
    assert env.clone_for(URL) == tmp_path / NAME
    assert env.worktree_for(URL, "integ-batchrepo-x") == (
        tmp_path / f"{NAME}.wt" / "integ-batchrepo-x")


def test_default_env_takes_the_squash_sha_from_gh_only(tmp_path, monkeypatch):
    # resolve_merge_sha falls back to the clone's origin/<default>, which is
    # stale right after a server-side merge. That half must answer "unknown".
    asked: list[list[str]] = []
    answer = {"rc": 0, "out": "a" * 40 + "\n"}

    def run(argv, **kw):
        asked.append(list(argv))
        return subprocess.CompletedProcess(argv, answer["rc"], answer["out"], "")

    monkeypatch.setattr(beta_publish, "_run", run)
    env = batch_gate.default_env()
    assert env.merge_sha(str(tmp_path), "worker/a", "main") == "a" * 40
    answer.update(rc=1, out="")
    assert env.merge_sha(str(tmp_path), "worker/a", "main") is None
    assert [argv[0] for argv in asked] == ["gh", "gh"]   # git was never run for it

    def boom(argv, **kw):
        raise OSError("gh is not installed")

    monkeypatch.setattr(beta_publish, "_run", boom)
    assert env.merge_sha(str(tmp_path), "worker/a", "main") is None


# =============================================================================
# Orchestrator.tick() → reconcile_batch_gate → batch_gate.step
# =============================================================================
# The same repo, now reached the way production reaches it: rows in a ledger, the
# config in a vault, and `orch.tick([])`. Only the tick's OTHER sweeps are
# stubbed. The job runs on the real default Env; what it would do outside this
# host is swapped at the engine's own seams (the gate runner, stages.merge_pr,
# the two gh readers, the squash-sha lookup) and the shared clone is found by
# patching repos.local_path.

OTHER_URL = "https://example.test/acme/otherapp.git"
CONFIG = {"enabled": True, "repos": {NAME: GATE}}

_TICK_STUBS = (
    "reroute_remote", "reap_stale", "reconcile_unparked", "reconcile_resumes",
    "reconcile_rescope", "_dispatch_serial_per_checkout", "collect",
    "reconcile_split_parents", "reconcile_merged",
    "reconcile_closed", "reconcile_unblocked", "reconcile_parents",
    "reconcile_human_ready", "reconcile_queue", "_advote_bridge_tick",
    "_beta_sweep_tick", "_project_stall_tick", "renudge_due",
)


class _TickWorld:
    def __init__(self, tmp: Path, monkeypatch, *, slugs: str = "abc",
                 config: dict | None = CONFIG, gradle_cap: int = 0):
        self.tmp = tmp
        self.repo = _Repo(tmp, monkeypatch)
        self.vault = tmp / "vault"
        self.ledger = Ledger(self.vault / "Worker" / "assignments")
        self.mb = FileMailbox(self.vault / "Worker" / "agents")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"],
                                   capabilities=[], repo="~/x")])
        self.notified: list[tuple] = []
        self.routed: list[dict] = []

        def notify(text, link=None):
            self.notified.append((text, link))

        def route(text, *, level=None, event_key=None, cooldown_s=0.0):
            self.routed.append({"text": text, "level": level, "event_key": event_key})
            if level == kakao_format.LEVEL_DIGEST:
                return "digest", None                    # queued, never sent now
            return "send", text

        self.orch = Orchestrator(self.mb, self.reg, self.ledger, notify=notify,
                                 alert_route=route,
                                 max_parallel_gradle_builds=gradle_cap)
        monkeypatch.setattr(self.orch, "_task_title", lambda slug: slug)
        if config is not None:
            path = self.vault / batch_gate.CONFIG_REL
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(config), encoding="utf-8")

        self.gate = _FakeGate()
        monkeypatch.setattr(batch_gate, "default_gate", lambda: self.gate)
        self.merge_pr = mock.Mock(return_value={"merged": True, "note": "squash-merged"})
        monkeypatch.setattr(stages, "merge_pr", self.merge_pr)
        self.pr_states: dict[str, str] = {}
        monkeypatch.setattr(stages, "gh_pr_state",
                            lambda branch, repo=None, cwd=None: self.pr_states.get(branch, "OPEN"))
        monkeypatch.setattr(stages, "gh_pr_merged", lambda branch, repo=None: None)
        monkeypatch.setattr(beta_publish, "resolve_merge_sha", lambda *a, **k: None)
        self.jobs_start = mock.Mock(side_effect=AssertionError("a real job was started"))
        monkeypatch.setattr(jobs, "start", self.jobs_start)
        # Where each repo's shared clone is on this "host".
        self.other_clone = tmp / "worker" / "otherapp"
        self.clones = {URL: self.repo.clone, OTHER_URL: self.other_clone}
        monkeypatch.setattr(repos, "local_path", lambda url, worker_root=None: self.clones.get(
            url, tmp / "worker" / "no-such-clone"))
        self.git_calls: list[tuple] = []
        real_git = stages._git

        def counted(repo_dir, *args, **kw):
            self.git_calls.append(args)
            return real_git(repo_dir, *args, **kw)

        monkeypatch.setattr(stages, "_git", counted)
        self.now = FRESH
        for i, slug in enumerate(slugs):
            self.ledger.save(_row(slug, self.repo.heads[slug], ready=T0 + i, n=i + 1))

    def tick(self, now: float | None = None, *, paused: bool = False,
             live: tuple = ()) -> dict:
        """`orch.tick([])` with every other sweep stubbed (except those `live`
        names) and the holds answered."""
        patches = [mock.patch.object(self.orch, name, return_value=[])
                   for name in _TICK_STUBS if name not in live]
        patches += [mock.patch.object(self.orch, "usage_hold", return_value=None),
                    mock.patch.object(self.orch, "paused", return_value=paused),
                    mock.patch.object(self.orch, "budget_exceeded", return_value=False)]
        for patch in patches:
            patch.start()
        try:
            return self.orch.tick([], self.now if now is None else now)["batch_gate"]
        finally:
            for patch in reversed(patches):
                patch.stop()

    def row(self, slug: str) -> Assignment:
        return self.ledger.load(slug)

    def job(self) -> dict | None:
        return batch_gate.load_job(self.vault, NAME)

    def gradle(self, *clones: Path) -> None:
        """Make these shared clones Gradle projects (a `gradlew` at the root)."""
        for clone in clones:
            clone.mkdir(parents=True, exist_ok=True)
            (clone / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")

    def alerts(self, prefix: str) -> list[dict]:
        return [r for r in self.routed if str(r["event_key"]).startswith(prefix)]


@pytest.fixture
def tw(tmp_path, monkeypatch) -> _TickWorld:
    return _TickWorld(tmp_path, monkeypatch)


def _task_payload() -> dict:
    return {"summary": "c", "repo": URL, "worker_mode": "auto", "task_path": "",
            "heavy": False}


# --- AC 2: three branches, one conflicts ------------------------------------------

def test_tick_gates_two_of_three_tasks_once_and_merges_them_in_order(tw, closes):
    first = tw.tick()

    assert first == {**EMPTY, "started": ["a", "b"], "returned": ["c"]}
    [started] = tw.gate.started                          # ONE gate run
    assert started["members"] == ["a", "b"]
    assert started["files"] == ["a.txt", "b.txt", "shared.txt"]
    assert started["shared"] == "main's edit\n"
    assert started["full_gate"] == GATE["full_gate"]
    tw.jobs_start.assert_not_called()
    tw.merge_pr.assert_not_called()                      # nothing merges before the gate
    assert tw.job()["state"] == batch_gate.JOB_RUNNING
    # the conflicting task is queued for an individual full verify
    c = tw.row("c")
    assert c.state == A.FAILED
    assert c.history[-1]["type"] == A.BATCH_RETURNED
    assert c.payload[batch_gate.KEY_VERIFY_FULL] is True
    assert c.payload[batch_gate.KEY_RETURN_REASON] == batch_gate.RETURN_CONFLICT
    assert c.display_state == "deferred"
    assert tw.orch._ledger_gate(c, _parse_ts(c.updated_ts) + 1).deferred
    assert tw.routed == []                               # a conflict pages nobody

    tw.gate.verdict = batch_gate.GATE_PASSED
    second = tw.tick()

    assert second == {**EMPTY, "merged": ["a", "b"]}
    assert tw.merge_pr.call_args_list == [
        mock.call(tw.repo.clone, "worker/a", repo=URL),
        mock.call(tw.repo.clone, "worker/b", repo=URL)]
    for slug in ("a", "b"):
        row = tw.row(slug)
        assert row.state == A.DONE
        assert row.history[-1]["type"] == batch_gate.HISTORY_MERGED == "batch-merged"
        assert row.close_fingerprint == f"fp-{slug}"     # closed with evidence
    assert closes == ["a", "b"]                          # the task mds, in merge order
    assert len(tw.gate.started) == 1
    assert tw.job() is None and not batch_gate.has_jobs(tw.vault)
    assert tw.repo.integ_worktrees() == [] and tw.repo.integ_branches() == []
    [notice] = tw.alerts("batch-gate-passed:batchrepo:integ/batchrepo/")
    assert notice["level"] == kakao_format.LEVEL_DIGEST
    assert tw.notified == []                             # digest only: nothing sent now
    # a third tick has nothing left to do
    assert tw.tick() == EMPTY


def test_tick_merges_through_real_squash_commits_without_a_second_gate(
        tmp_path, monkeypatch, closes):
    # The recorder above never moves origin. Here each merge is a real squash
    # commit on the default branch (and the branch is deleted), as the server
    # leaves it: our own commits must not read as "the default branch moved".
    w = _TickWorld(tmp_path, monkeypatch, slugs="ab")
    squashed: dict[str, str] = {}

    def squash(repo_dir, branch, repo=None):
        squashed[branch] = w.repo.squash_merge(branch)
        return {"merged": True, "note": f"squash-merged {branch}"}

    w.merge_pr.side_effect = squash
    monkeypatch.setattr(beta_publish, "resolve_merge_sha",
                        lambda repo_dir, branch, **_k: squashed.get(branch))
    w.tick()
    gated = w.job()["head_sha"]
    w.gate.verdict = batch_gate.GATE_PASSED

    assert w.tick() == {**EMPTY, "merged": ["a", "b"]}

    assert [call.args[1] for call in w.merge_pr.call_args_list] == ["worker/a", "worker/b"]
    assert closes == ["a", "b"]
    assert len(w.gate.started) == 1 and w.job() is None
    assert w.repo.origin_sha("main") == squashed["worker/b"]
    # what landed is exactly the tree the one gate run judged
    assert w.repo.origin_sha("main^{tree}") == _git(
        w.repo.clone, "rev-parse", f"{gated}^{{tree}}")


def test_tick_parked_closed_pr_gets_its_one_alert_from_reconcile_merged(
        tw, monkeypatch):
    # The batch sweep parks a task whose PR a person closed and says nothing
    # itself: reconcile_merged, later in the SAME tick, owns that alert.
    tw.pr_states = {"worker/b": "CLOSED"}
    monkeypatch.setattr(tasks, "task_status", lambda slug, *a, **k: "open")

    result = tw.tick(live=("reconcile_merged",))

    assert result == {**EMPTY, "started": ["a"], "returned": ["c"]}
    b = tw.row("b")
    assert b.state == A.AWAITING_HUMAN
    assert b.history[-1]["type"] == "closed-unmerged-alerted"
    assert [r["event_key"] for r in tw.routed] == ["closed-unmerged:b"]
    assert len(tw.notified) == 1
    # the next tick does not say it again, and the gate for `a` is untouched
    tw.tick(live=("reconcile_merged",))
    assert len(tw.notified) == 1
    assert tw.job()["state"] == batch_gate.JOB_RUNNING
    assert [member["slug"] for member in tw.job()["members"]] == ["a"]


def test_tick_sends_the_conflicting_task_out_again_for_a_full_verify(tw):
    tw.tick()
    c = tw.row("c")
    due = _parse_ts(c.updated_ts) + tw.orch.job_poll_cooldown_s + 1

    sent = tw.orch.dispatch(Task("c", _task_payload()), now=due)

    assert sent is not None and sent.state == A.DISPATCHED
    assert sent.refail_count == 0                        # a deferral, not a failure
    assert sent.payload[batch_gate.KEY_VERIFY_FULL] is True
    assert sent.payload[batch_gate.KEY_RETURN_REASON] == batch_gate.RETURN_CONFLICT
    [envelope] = [e for e in tw.mb.receive("w") if e.type == m.TASK_ASSIGN]
    assert envelope.payload[batch_gate.KEY_RETURN_REASON] == batch_gate.RETURN_CONFLICT


# --- AC 1 at the tick: failure → back to individual verify, ONE alert ---------------

def test_tick_failed_gate_returns_the_members_and_alerts_once(tw, closes):
    tw.tick()
    branch = tw.job()["branch"]
    tw.gate.verdict = batch_gate.GATE_FAILED

    result = tw.tick()

    assert result == {**EMPTY, "returned": ["a", "b"]}
    for slug in ("a", "b"):
        row = tw.row(slug)
        assert row.state == A.FAILED
        assert row.history[-1]["type"] == A.BATCH_RETURNED
        assert row.payload[batch_gate.KEY_RETURN_REASON] == batch_gate.RETURN_GATE_FAILED
        assert row.last_alert_kind == ALERT_BATCH_GATE_FAILED
        assert row.refail_count == 0 and row.display_state == "deferred"
    [alert] = tw.alerts("batch-gate-failed")
    assert alert["event_key"] == f"batch-gate-failed:batchrepo:{branch}"
    assert alert["level"] == kakao_format.LEVEL_IMMEDIATE
    assert "batchrepo" in alert["text"] and "2" in alert["text"]
    assert "📋 a" in alert["text"] and "📋 b" in alert["text"]
    assert "Worker/jobs/batch-gate-batchrepo.log" in alert["text"]
    assert "gate-failed" in alert["text"]
    assert len(tw.notified) == 1                         # sent, once
    tw.merge_pr.assert_not_called()
    assert closes == []
    assert tw.job() is None and tw.repo.integ_worktrees() == []

    assert tw.tick() == EMPTY
    assert len(tw.alerts("batch-gate-failed")) == 1 and len(tw.notified) == 1


def test_tick_restart_limit_returns_the_tasks_with_one_digest_notice(tw):
    tw.gate.verdict = batch_gate.GATE_PASSED
    for n in range(batch_gate.MAX_RESTARTS + 1):
        tw.tick()                                        # build + start
        tw.repo.push_main(f"move{n}.txt", "x\n")
        tw.tick()                                        # passed, main moved
    for slug in ("a", "b"):
        assert tw.row(slug).payload[batch_gate.KEY_RETURN_REASON] == batch_gate.RETURN_RESTARTS
        assert tw.row(slug).last_alert_kind == ""        # not a gate failure
    [notice] = tw.alerts("batch-restarts:batchrepo:")
    assert notice["level"] == kakao_format.LEVEL_DIGEST
    assert str(batch_gate.MAX_RESTARTS) in notice["text"]
    tw.merge_pr.assert_not_called()
    assert tw.notified == [] and tw.job() is None


# --- the safety net runs first ------------------------------------------------------

def test_tick_expires_members_before_stepping_and_then_drops_the_job(tw):
    tw.tick()
    branch = tw.job()["branch"]
    late = T0 + batch_gate.EXPIRE_S + 5

    result = tw.tick(late)

    assert result == {**EMPTY, "returned": ["a", "b"]}
    for slug in ("a", "b"):
        assert tw.row(slug).payload[batch_gate.KEY_RETURN_REASON] == batch_gate.RETURN_EXPIRED
    assert [r["event_key"] for r in tw.routed] == ["batch-expired:a", "batch-expired:b"]
    assert "24" in tw.routed[0]["text"]
    # the job no longer has a row to gate: stopped, torn down, forgotten
    assert tw.gate.stopped == [branch]
    assert tw.job() is None and tw.repo.integ_worktrees() == []
    tw.merge_pr.assert_not_called()


def test_tick_drops_a_job_whose_repo_left_the_config(tw, capsys):
    tw.tick()
    (tw.vault / batch_gate.CONFIG_REL).unlink()
    capsys.readouterr()

    result = tw.tick()

    assert result == {**EMPTY, "returned": ["a", "b"]}
    assert tw.row("a").payload[batch_gate.KEY_RETURN_REASON] == batch_gate.RETURN_NOT_ENABLED
    assert tw.job() is None and len(tw.gate.stopped) == 1
    assert "job dropped" in capsys.readouterr().out
    # with the job gone the sweep is back to doing nothing at all
    capsys.readouterr()
    assert tw.tick() == EMPTY
    assert capsys.readouterr().out == ""


def test_tick_closes_a_pr_someone_merged_and_parks_one_someone_closed(tw, closes):
    tw.pr_states = {"worker/a": "MERGED", "worker/b": "CLOSED"}

    result = tw.tick()

    assert result == {**EMPTY, "closed": ["a"]}
    a, b = tw.row("a"), tw.row("b")
    assert a.state == A.DONE and a.history[-1]["type"] == batch_gate.HISTORY_MERGED
    assert closes == ["a"]
    # b: a person's call. Parked with the branch reconcile_merged looks the PR
    # up by, and with no history entry or alert of this sweep's own.
    assert b.state == A.AWAITING_HUMAN
    assert b.payload["branch"] == "worker/b"
    assert b.history[-1]["type"] == batch_gate.HISTORY_READY
    assert tw.routed == [] and tw.gate.started == []
    assert tw.row("c").state == A.BATCH_READY            # alone now: it waits


def test_tick_paused_starts_no_gate(tw):
    assert tw.tick(paused=True) == EMPTY
    assert tw.gate.started == [] and tw.git_calls == [] and tw.job() is None
    assert tw.tick()["started"] == ["a", "b"]


def test_tick_skips_a_repo_another_process_is_stepping(tw, capsys):
    with batch_gate.repo_lock(tw.vault, NAME) as held:
        assert held
        assert tw.tick() == EMPTY
    assert tw.gate.started == [] and tw.git_calls == []
    assert "stepped by another process" in capsys.readouterr().out
    assert tw.tick()["started"] == ["a", "b"]


def test_tick_survives_a_step_that_raises_and_still_tends_the_other_repo(
        tmp_path, monkeypatch, capsys):
    config = {"enabled": True, "repos": {NAME: GATE, "otherapp": GATE}}
    w = _TickWorld(tmp_path, monkeypatch, config=config)
    for i, slug in enumerate(("o1", "o2")):
        w.ledger.save(_row(slug, "0" * 40, ready=T0 + i, n=10 + i, url=OTHER_URL))
    stepped = []

    def flaky(repo_url, rows, *_a, **_k):
        stepped.append(repo_url)
        if repo_url == URL:
            raise RuntimeError("clone is on fire")
        return {**EMPTY, "started": [a.task_id for a in rows]}

    monkeypatch.setattr(batch_gate, "step", flaky)

    result = w.tick()

    assert stepped == [URL, OTHER_URL]                   # the raise skipped only its repo
    assert result == {**EMPTY, "started": ["o1", "o2"]}
    assert "reconcile_batch_gate: batchrepo skipped: clone is on fire" in capsys.readouterr().out
    assert w.row("a").state == A.BATCH_READY             # kept for the next tick


# --- AC 3: the gate and the Gradle build cap -----------------------------------------

def test_tick_defers_the_gate_while_the_gradle_cap_is_reached(tmp_path, monkeypatch):
    w = _TickWorld(tmp_path, monkeypatch, slugs="ab", gradle_cap=1)
    w.gradle(w.repo.clone, w.other_clone)
    busy = Assignment(task_id="other-task", agent_id="w", corr_id="c-o", kind="ai-local",
                      state=A.IN_PROGRESS, payload={"repo": OTHER_URL})
    w.ledger.save(busy)                                  # one Gradle run is live

    assert w.tick() == EMPTY

    assert w.gate.started == [] and w.job() is None
    assert w.git_calls == []                             # deferred before any git
    assert w.row("a").state == w.row("b").state == A.BATCH_READY

    busy.state = A.DONE                                  # the run finished
    w.ledger.save(busy)
    assert w.tick()["started"] == ["a", "b"]


def test_a_running_gate_counts_toward_the_gradle_cap_at_dispatch(tmp_path, monkeypatch, capsys):
    w = _TickWorld(tmp_path, monkeypatch, slugs="ab", gradle_cap=1)
    w.gradle(w.repo.clone, w.other_clone)
    task = Task("other-task", {"summary": "x", "repo": OTHER_URL, "heavy": False})
    free = CheckoutGate(None)
    assert w.orch._checkout_gate(task, OTHER_URL, w.orch._checkout_inflight(), 2) == free

    w.tick()                                             # the gate is out now
    assert w.job()["state"] == batch_gate.JOB_RUNNING

    assert w.orch._batch_gradle_jobs() == 1
    gate = w.orch._checkout_gate(task, OTHER_URL, w.orch._checkout_inflight(), 2)
    assert (gate.wait, gate.gradle_runs) == (WAIT_GRADLE_CAP, 1)
    [queued] = w.orch.queue_snapshot([task], now=w.now)
    assert queued["wait"] == "gradle_cap"
    assert w.orch._dispatch_serial_per_checkout([task]) == []
    assert "global gradle-build cap 1 reached (1 live)" in capsys.readouterr().out
    assert w.ledger.load("other-task") is None           # held back, nothing sent

    # the cap switched off: the gate is not even counted
    w.orch.max_parallel_gradle_builds = 0
    assert w.orch._checkout_gate(task, OTHER_URL, w.orch._checkout_inflight(), 2) == free
    w.orch.max_parallel_gradle_builds = 1
    # a job that is merging or waiting to rebuild runs no build
    job = w.job()
    for state in (batch_gate.JOB_PASSED, batch_gate.JOB_IDLE):
        batch_gate.save_job(w.vault, {**job, "state": state})
        assert w.orch._batch_gradle_jobs() == 0
        assert w.orch._checkout_gate(task, OTHER_URL, w.orch._checkout_inflight(), 2) == free
    # and once the gate is gone the task goes out
    batch_gate.clear_job(w.vault, NAME)
    [sent] = w.orch._dispatch_serial_per_checkout([task])
    assert sent.task_id == "other-task"


def test_a_running_gate_of_a_non_gradle_repo_is_not_a_gradle_build(tmp_path, monkeypatch):
    w = _TickWorld(tmp_path, monkeypatch, slugs="ab", gradle_cap=1)
    w.gradle(w.other_clone)                              # batchrepo has no gradlew
    w.tick()
    assert w.job()["state"] == batch_gate.JOB_RUNNING
    assert w.orch._batch_gradle_jobs() == 0
    assert not w.orch._batch_gate_gradle_busy(URL)


def test_gradle_accounting_never_raises_on_a_ledger_without_a_directory(tw):
    tw.orch.ledger = object()                            # e.g. another ledger backend
    assert tw.orch._batch_gradle_jobs() == 0


# --- AC 4: nothing to do = nothing done -------------------------------------------------

def test_idle_tick_with_no_config_and_no_rows_does_nothing(tmp_path, monkeypatch, capsys):
    w = _TickWorld(tmp_path, monkeypatch, slugs="", config=None)
    load_config = mock.Mock(return_value={})
    monkeypatch.setattr(batch_gate, "load_config", load_config)
    capsys.readouterr()

    assert w.tick() == EMPTY

    load_config.assert_not_called()
    assert w.git_calls == []
    w.jobs_start.assert_not_called()
    assert not batch_gate.state_dir(w.vault).exists()    # no job file, no lock
    assert not (w.vault / "Worker" / "jobs").exists()
    assert capsys.readouterr().out == ""
    assert w.routed == [] and w.gate.started == []


def test_idle_tick_with_one_fresh_row_leaves_no_trace(tmp_path, monkeypatch, capsys):
    w = _TickWorld(tmp_path, monkeypatch, slugs="a")
    before = w.row("a").to_json()
    capsys.readouterr()

    assert w.tick() == EMPTY

    assert w.git_calls == []
    assert not batch_gate.state_dir(w.vault).exists()    # not even a lock file
    assert capsys.readouterr().out == ""
    assert w.row("a").to_json() == before
    assert w.routed == [] and w.gate.started == []


def test_no_config_tick_only_returns_the_waiting_rows(tmp_path, monkeypatch, capsys):
    w = _TickWorld(tmp_path, monkeypatch, config=None)
    capsys.readouterr()

    assert w.tick() == {**EMPTY, "returned": ["a", "b", "c"]}

    for slug in "abc":
        assert w.row(slug).payload[batch_gate.KEY_RETURN_REASON] == batch_gate.RETURN_NOT_ENABLED
    assert w.git_calls == []
    w.jobs_start.assert_not_called()
    assert not batch_gate.state_dir(w.vault).exists()
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3 and all("returned to an individual full verify" in ln for ln in lines)
    assert w.routed == [] and w.gate.started == []
