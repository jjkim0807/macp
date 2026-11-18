"""Is this run's task already over? The "superseded" verdict.

A run is superseded when finishing it can no longer change anything. There are
two signals, and nothing else counts:

1. `md-closed`: the task markdown says `status: done | cancelled | canceled`.
   Someone closed the task while the run was in flight.
2. `merged`: the md is not closed, but the worker branch's own work is already
   in the default branch (a squash merge included).

2026-11-18: an interactive session verified three tasks on an integration
branch, merged them and closed their md. Two worker runs that had been
dispatched just before kept going. One fought that session's build for memory
and timed out a second time; the other got as far as verify, found "0 commits,
nothing to merge" and parked for a human. Both paged a person about work that
was already finished. A run re-read neither the md nor the default branch
between stages; this module is that re-read.

Callers
-------
* The runner (`StagedExecutor._superseded_exit`) asks at stage boundaries. A
  superseded run stops there and replies RESULT with `{RESULT_KEY: <reason>}`:
  no further stage, no push, no PR, nobody paged.
* The orchestrator (`Orchestrator._superseded_reason` in `collect()`) honours
  that reply, and re-reads the md itself before a park or an alert goes out. It
  never probes git: the 5-minute tick stays off the network, and a row a remote
  runner worked has no checkout on this host.

The empty-branch trap
---------------------
A freshly cut `worker/<slug>` has no commit of its own, so "the branch adds
nothing the default branch lacks" is trivially true for it. Read as "merged",
that would stop every run right after its first stage and close the task with
nothing done. So `merged` needs all of: at least one own commit, a non-empty net
change against the merge-base, and a trial merge into the default branch whose
tree equals the default branch's tree. `classify_landed` answers False for zero
own commits before anything else is looked at.

The git half runs only after `implement`
----------------------------------------
`check_run` probes git only when its caller passes `probe_git=True`, and the
runner does that only for an external repo once `implement` is among the run's
completed stages. At run start "the branch is already in the default branch"
cannot tell a stale re-dispatch from "part of this was merged by hand, carry
on". Once implement has finished and still added nothing, the run has nothing
left to contribute.

Fail-open
---------
Every collector returns None for anything it could not read (no PyYAML, a
failed fetch, a detached HEAD, a git too old for `merge-tree --write-tree`), and
None is never a verdict: the run or the reply then goes on exactly as it did
before this module existed. Nothing here raises.

Known limits
------------
* A branch that landed by a plain merge or a fast-forward has no own commit left
  (`rev-list <base>..<branch>` is 0), which is the same picture as a freshly cut
  branch, so the git half says False. The md half covers it.
* If the default branch changed the same lines again after the merge, the trial
  merge conflicts and the answer is False.
* A merge that lands while verify or report is running is not seen until the
  next boundary; if that stage fails first and the md is still open, the old
  park and alert paths run as before.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import repos
from .statuses import is_closed

# "0" / "false" / "no" / "off" switches the whole feature off. Read at call time
# by both the runner's boundary check and the orchestrator's collect() gate.
KILL_ENV = "MACP_SUPERSEDED_CHECK"
_OFF = frozenset({"0", "false", "no", "off"})

# Wire format: RESULT_KEY rides the runner's RESULT payload (its value is one of
# the REASON_* strings); the HISTORY_* strings and the reasons land in the
# git-tracked assignment JSON. Never re-spell any of them.
RESULT_KEY = "superseded"
HISTORY_TYPE = "superseded"
HISTORY_UNCONFIRMED = "superseded-unconfirmed"
REASON_MD_CLOSED = "md-closed"
REASON_MERGED = "merged"

GIT_TIMEOUT_LOCAL_S = 30
GIT_TIMEOUT_FETCH_S = 60
# Used when the clone has no `refs/remotes/origin/HEAD` (a clone of a repo that
# was empty at the time never gets one).
_FALLBACK_BASE = "origin/main"
_OID_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

# Bound at import on purpose (the disk_guard / worktree_sweep rule): several
# _execute suites patch the global `subprocess.run` and treat every call they
# see as a stage call. This module's git probes must never land in such a fake.
_run = subprocess.run


@dataclass(frozen=True)
class Verdict:
    superseded: bool
    reason: str = ""    # a REASON_* when superseded
    detail: str = ""    # the closed status for md-closed


NOT_SUPERSEDED = Verdict(False)


# -- pure -------------------------------------------------------------------

def enabled() -> bool:
    """False when KILL_ENV switches the check off. Read on every call."""
    return os.environ.get(KILL_ENV, "1").strip().lower() not in _OFF


def judge(md_status: object, landed: bool | None) -> Verdict:
    """The verdict from already-collected inputs.

    `md_status` is the task md's status as written (None = not read); `landed`
    is `branch_landed`'s answer (None = not measured). A closed md wins whatever
    git says. Only a definite `landed is True` is a merge: False and None both
    mean "not superseded".
    """
    if is_closed(md_status):
        return Verdict(True, REASON_MD_CLOSED, str(md_status).strip().lower())
    if landed is True:
        return Verdict(True, REASON_MERGED)
    return NOT_SUPERSEDED


def classify_landed(own_commits: int | None, net_change: bool | None,
                    merge_rc: int | None, merged_tree: str | None,
                    base_tree: str | None) -> bool | None:
    """Is the branch's own work already in the default branch?

    True / False are measured answers, None is "could not measure".

    * `own_commits`: `git rev-list --count <base>..<branch>`.
    * `net_change`: does `<merge-base>..<branch>` change any file at all?
    * `merge_rc`, `merged_tree`: exit code and resulting tree of the trial merge
      `git merge-tree --write-tree <base> <branch>`.
    * `base_tree`: the default branch's own tree.

    The order is the point. Zero own commits is False before anything else (the
    empty-branch trap, see the module docstring), and so is a branch whose
    commits cancel out. `merge_rc == 1` is a real conflict, so the default
    branch does not simply contain the work. Any other non-zero code is a merge
    that never ran. Only then do the trees decide: a trial merge that leaves the
    default branch's tree unchanged means the branch has nothing left to add.
    """
    if not isinstance(own_commits, int) or isinstance(own_commits, bool):
        return None
    if own_commits < 1:
        return False
    if net_change is None:
        return None
    if not net_change:
        return False
    if merge_rc == 1:
        return False
    if merge_rc != 0 or not merged_tree or not base_tree:
        return None
    return merged_tree == base_tree


def normalize_reason(value: object) -> str:
    """A RESULT payload's RESULT_KEY value as a REASON_*, or "" when unset.

    An unknown truthy value (a newer runner's reason, a bare `true`) maps to
    REASON_MD_CLOSED: that is the reason the orchestrator confirms against its
    own md before closing anything, so an unrecognised claim is never taken on
    trust.
    """
    if not value:
        return ""
    if value == REASON_MERGED:
        return REASON_MERGED
    return REASON_MD_CLOSED


def note(reason: str, detail: str = "") -> str:
    """One line for run.log, the PROGRESS stream and the RESULT summary."""
    if reason == REASON_MERGED:
        return "superseded — branch work already in the default branch"
    return f"superseded — task md status: {detail or 'closed'}"


# -- collectors (thin, never raise) -------------------------------------------

def task_md_path(payload: dict | None, vault_root: Path | str | None) -> Path | None:
    """The task md the payload names, or None.

    Reads `task_path` only, the same way `Runner._skip_reason` and
    `Orchestrator._task_md_closed` do: a non-empty string, resolved against
    `vault_root` when relative, and it must be a file. The path is never guessed
    from the slug. A payload without `task_path` (most `_execute` tests, a bare
    CLI dispatch) must not make this read whatever vault the process runs in.
    """
    try:
        raw = (payload or {}).get("task_path")
        if not isinstance(raw, str) or not raw:
            return None
        p = Path(raw)
        if not p.is_absolute():
            if vault_root is None:
                return None
            p = Path(vault_root) / p
        return p if p.is_file() else None
    except Exception:  # noqa: BLE001 - unreadable means "no md", never an error
        return None


def md_closed_status(payload: dict | None, vault_root: Path | str | None) -> str | None:
    """The closed status (done / cancelled / canceled) the task md states, else
    None. None also covers every failure to read: no `task_path`, no file, no
    frontmatter, broken YAML, or PyYAML missing on a bare remote runner."""
    try:
        p = task_md_path(payload, vault_root)
        if p is None:
            return None
        return repos.task_closed_from_fm(repos._frontmatter(p))
    except Exception:  # noqa: BLE001 - fail-open
        return None


def _git(repo: Path, *args: str, timeout: float = GIT_TIMEOUT_LOCAL_S):
    """One git command in `repo`; None if it could not be run at all."""
    try:
        return _run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                    timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def _text(result) -> str | None:
    """Stripped stdout of a git call that exited 0, else None."""
    if result is None or result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def branch_landed(repo_dir: Path | str | None, branch: str) -> bool | None:
    """Has `branch`'s own work already landed in the clone's default branch?

    True / False are measured, None is "could not measure". Read-only in the
    sense that matters: the fetch updates the remote-tracking ref and the trial
    merge deposits tree objects in the object store, but the working tree, the
    index, HEAD and every local branch are left exactly as they were.
    """
    try:
        return _branch_landed(repo_dir, branch)
    except Exception:  # noqa: BLE001 - unmeasured, never an error on the stage path
        return None


def _branch_landed(repo_dir: Path | str | None, branch: str) -> bool | None:
    if not branch or repo_dir is None:
        return None
    repo = Path(repo_dir)
    if not (repo / ".git").exists():
        # Not a checkout (a test's placeholder path): no subprocess at all.
        return None
    dirty = _text(_git(repo, "status", "--porcelain"))
    if dirty is None:
        return None
    if dirty:
        return False        # uncommitted work has landed nowhere
    if _text(_git(repo, "symbolic-ref", "--short", "-q", "HEAD")) != branch:
        return None         # detached, or another branch: not this run's checkout state
    base = _text(_git(repo, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"))
    if not base or not base.startswith("origin/"):
        base = _FALLBACK_BASE
    default = base[len("origin/"):]
    # Fetch first, and stop if it fails: a stale tracking ref cannot show a merge
    # that happened a minute ago, and it must not be read as "not merged" either.
    if _text(_git(repo, "fetch", "origin", default, timeout=GIT_TIMEOUT_FETCH_S)) is None:
        return None
    # Pin the default branch to one commit. Another run of the same repo shares
    # these refs and may fetch between two of the commands below.
    base_oid = _text(_git(repo, "rev-parse", "--verify", "-q", f"{base}^{{commit}}"))
    if not base_oid or not _OID_RE.fullmatch(base_oid):
        return None
    ref = f"refs/heads/{branch}"
    count = _text(_git(repo, "rev-list", "--count", f"{base_oid}..{ref}"))
    if count is None or not count.isdigit():
        return None
    own_commits = int(count)
    if own_commits < 1:
        return False        # nothing of its own: a fresh branch, not a merged one
    merge_base = _text(_git(repo, "merge-base", base_oid, ref))
    if not merge_base or not _OID_RE.fullmatch(merge_base):
        return None
    diff = _git(repo, "diff", "--quiet", merge_base, ref)
    if diff is None or diff.returncode not in (0, 1):
        return None
    net_change = diff.returncode == 1
    if not net_change:
        return False        # its commits cancel out: there was nothing to land
    probe = _git(repo, "merge-tree", "--write-tree", base_oid, ref)
    if probe is None:
        return None
    out = (probe.stdout or "").strip()
    if probe.returncode != 0 and not out:
        # The merge never started. git exits 1 (the "conflicts" code) for an
        # unresolvable ref too, with empty stdout; a merge that really ran
        # always prints the new tree's oid first (see merge_state._probe_merge).
        return None
    merged_tree = out.splitlines()[0].strip() if out else ""
    if probe.returncode == 0 and not _OID_RE.fullmatch(merged_tree):
        return None
    base_tree = _text(_git(repo, "rev-parse", "--verify", "-q", f"{base_oid}^{{tree}}"))
    if not base_tree or not _OID_RE.fullmatch(base_tree):
        return None
    return classify_landed(own_commits, net_change, probe.returncode,
                           merged_tree, base_tree)


def check_run(payload: dict | None, *, vault_root: Path | str | None,
              repo_dir: Path | str | None = None, branch: str = "",
              probe_git: bool = False) -> Verdict:
    """The runner's one call per stage boundary.

    The md is read every time (one small file). Git is probed only when
    `probe_git` is set and the md is not closed; see the module docstring for
    when the runner sets it. Never raises: any failure is NOT_SUPERSEDED.
    """
    try:
        if not enabled():
            return NOT_SUPERSEDED
        status = md_closed_status(payload, vault_root)
        if status:
            return judge(status, None)
        landed = None
        if probe_git and repo_dir is not None and branch:
            landed = branch_landed(repo_dir, branch)
        return judge(None, landed)
    except Exception:  # noqa: BLE001 - advisory on the stage path, fail open
        return NOT_SUPERSEDED
