"""Run the one-click actions a triage card offers — through the existing paths only.

`POST /api/action` (scripts/macp_dashboard.py) diagnoses the task again at click
time and calls run_action() only for an action that fresh card still offers
(triage.py holds the contract). Each action is a path the engine or a human
already uses:

* merge    -> `gh pr merge <n> --repo <owner/repo> --squash` on the task's OWN PR:
              `gh pr view` first, and only an OPEN PR whose head branch is
              `worker/<slug>` (or a `-rN` retry branch) is merged. Never
              `--admin` / `--auto` / `--delete-branch`. The ledger is left to
              reconcile_merged, which closes the task on the next tick.
* resume   -> resume.schedule(vault, slug), the body of `cli resume`; the next
              tick's reconcile_resumes re-dispatches.
* cancel   -> the task md gets `status: cancelled`, `updated` and a Progress Log
              line, and that one file is committed.
* instruct -> a 「사용자 지시」 Progress Log line (and `updated`), committed the
              same way, then resume.schedule(after_commit=<that commit>).

The md is the source of truth, so nothing here writes todo.db (the todo-sync
daemon absorbs the md) or pushes (todo-sync pushes the vault). An md write is
a compare-and-swap like tasks.mark_done, under the vault git lock, only on
`main`, only for a tracked file, committed with `git commit --only` so no other
staged change rides along, and rolled back if the commit fails (an uncommitted
task md stalls the worker queue).

run_action() returns (ok, message): a sentence in the UI language with no raw
error text — gh stderr, HTTP codes and paths go to the log only.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path

from .. import i18n
from ..vault_git_lock import vault_git_lock
from . import resume
from .triage import pr_ref

# The dashboard's audit log: one place to read what a click did.
logger = logging.getLogger("macp.dashboard")

LOCK_TIMEOUT_S = 20.0     # wait for the vault git lock (todo-sync, the tick) before giving up
_CAS_ATTEMPTS = 3         # tasks._CLOSE_WRITE_ATTEMPTS: a vault merge lands once
_GIT_TIMEOUT_S = 60
_GH_VIEW_TIMEOUT_S = 30
_GH_MERGE_TIMEOUT_S = 120
_PROGRESS = "## Progress Log"
# A merge refused because the branches conflict. GitHub says "not mergeable" for
# a branch-policy refusal too, so that phrase alone is not a conflict.
_CONFLICT_RE = re.compile(r"cannot be cleanly created|merge conflict|\bconflict", re.I)


def _msg(key: str, **fmt) -> str:
    return i18n.t(f"triage.result.{key}", **fmt)


def _one_line(text, limit: int = 300) -> str:
    return " ".join(str(text or "").split())[:limit]


def run_action(action: str, slug: str, facts: dict | None, *, vault_root, items_dir,
               text: str | None = None, run=None, schedule=None,
               today: str | None = None) -> tuple[bool, str]:
    """Run one triage action for `slug`. `facts` are the click-time facts
    (triage.gather) the card was diagnosed from. `run` replaces subprocess.run
    (gh and git), `schedule` replaces resume.schedule and `today` the date —
    test seams; the defaults are the real paths."""
    run = run or subprocess.run
    schedule = schedule or resume.schedule
    today = today or dt.date.today().isoformat()
    if action == "merge":
        return _merge(slug, facts or {}, run)
    if action == "resume":
        return _resume(slug, vault_root, schedule)
    if action == "cancel":
        return _cancel(slug, vault_root, items_dir, run, today)
    if action == "instruct":
        return _instruct(slug, vault_root, items_dir, text, run, schedule, today)
    return False, _msg("not_offered")


# --- merge -----------------------------------------------------------------------

def _is_task_branch(head: str, slug: str) -> bool:
    return head == f"worker/{slug}" or re.fullmatch(rf"worker/{re.escape(slug)}-r\d+", head) is not None


def _gh(run, argv: list[str], timeout: int):
    # cwd outside any checkout: `--repo` names the repository, never the cwd.
    return run(argv, cwd=tempfile.gettempdir(), capture_output=True, text=True, timeout=timeout)


def _merge(slug: str, facts: dict, run) -> tuple[bool, str]:
    ref = pr_ref(facts.get("pr_url"))
    if ref is None:
        return False, _msg("not_offered")
    repo, number = ref
    try:
        view = _gh(run, ["gh", "pr", "view", str(number), "--repo", repo,
                         "--json", "state,headRefName"], _GH_VIEW_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("dashboard action merge: pr view failed task=%s err=%s", slug,
                       type(e).__name__)
        return False, _msg("pr_lookup_failed")
    if getattr(view, "returncode", 1) != 0:
        logger.warning("dashboard action merge: pr view rc=%s task=%s err=%s",
                       getattr(view, "returncode", "?"), slug, _one_line(view.stderr))
        return False, _msg("pr_lookup_failed")
    try:
        data = json.loads(view.stdout or "")
    except ValueError:
        data = None
    if not isinstance(data, dict):
        logger.warning("dashboard action merge: pr view not JSON task=%s", slug)
        return False, _msg("pr_lookup_failed")
    head = str(data.get("headRefName") or "")
    if not _is_task_branch(head, slug):
        logger.warning("dashboard action merge: %s#%s head=%s is not worker/%s", repo, number,
                       head, slug)
        return False, _msg("pr_not_task")
    state = str(data.get("state") or "").upper()
    if state == "MERGED":
        return True, _msg("already_merged")
    if state != "OPEN":
        return False, _msg("pr_closed")
    try:
        r = _gh(run, ["gh", "pr", "merge", str(number), "--repo", repo, "--squash"],
                _GH_MERGE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("dashboard action merge failed task=%s pr=%s#%s err=%s", slug, repo,
                       number, type(e).__name__)
        return False, _msg("merge_failed")
    if getattr(r, "returncode", 1) != 0:
        logger.warning("dashboard action merge failed task=%s pr=%s#%s rc=%s err=%s", slug,
                       repo, number, r.returncode, _one_line(r.stderr))
        return False, _msg("merge_conflict" if _CONFLICT_RE.search(r.stderr or "")
                           else "merge_failed")
    return True, _msg("merged")


# --- resume ----------------------------------------------------------------------

def _resume(slug: str, vault_root, schedule) -> tuple[bool, str]:
    try:
        schedule(vault_root, slug, note="dashboard")
    except resume.ResumeError as e:
        logger.warning("dashboard action resume refused task=%s err=%s", slug, e)
        return False, _msg("resume_failed")
    except Exception as e:
        logger.warning("dashboard action resume failed task=%s err=%s", slug, type(e).__name__)
        return False, _msg("internal")
    return True, _msg("resumed")


# --- md edits: cancel / instruct ---------------------------------------------------

def _with_log_line(text: str, line: str) -> str:
    """`text` with `line` appended under `## Progress Log` (created when absent) —
    tasks._with_close_trace's shape."""
    text = text.rstrip("\n")
    if _PROGRESS in text:
        return f"{text}\n{line}\n"
    return f"{text}\n\n{_PROGRESS}\n\n{line}\n"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _git(root: Path, run, *args: str):
    try:
        return run(["git", "-C", str(root), *args], capture_output=True, text=True,
                   timeout=_GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("dashboard action git %s failed err=%s", args[0], type(e).__name__)
        return None


def _ok(p) -> bool:
    return p is not None and getattr(p, "returncode", 1) == 0


def _md_commit(slug: str, vault_root, items_dir, run, patch, message: str
               ) -> tuple[bool, str, str]:
    """Apply `patch` (a pure function of the md's current text) to the task md
    and commit that file alone. (ok, failure message, commit sha)."""
    root = Path(vault_root)
    path = Path(items_dir) / f"{slug}.md"
    if not path.is_file():
        return False, _msg("md_missing"), ""
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        logger.warning("dashboard action: task md %s is outside the vault", path)
        return False, _msg("internal"), ""
    with vault_git_lock(root, LOCK_TIMEOUT_S) as held:
        if not held:
            return False, _msg("vault_busy"), ""
        head = _git(root, run, "rev-parse", "--abbrev-ref", "HEAD")
        if not _ok(head) or (head.stdout or "").strip() != "main":
            # a live vault on a worker branch strands what is committed there
            return False, _msg("vault_not_main"), ""
        if not _ok(_git(root, run, "ls-files", "--error-unmatch", "--", rel)):
            return False, _msg("md_untracked"), ""
        for _ in range(_CAS_ATTEMPTS):
            before = _read_text(path)
            after = patch(before)
            if after == before:
                logger.warning("dashboard action: patch left %s unchanged", rel)
                return False, _msg("internal"), ""
            if _read_text(path) != before:
                continue                     # something landed inside the window — re-derive
            path.write_text(after, encoding="utf-8")
            break
        else:
            return False, _msg("md_changed"), ""
        commit = _git(root, run, "commit", "-q", "--only", "-m", message, "--", rel)
        if not _ok(commit):
            logger.warning("dashboard action commit failed task=%s err=%s", slug,
                           _one_line(getattr(commit, "stderr", "") or "no result"))
            try:
                if _read_text(path) == after:
                    path.write_text(before, encoding="utf-8")
            except OSError as e:
                logger.warning("dashboard action rollback failed task=%s err=%s", slug,
                               type(e).__name__)
            return False, _msg("commit_failed"), ""
        sha = _git(root, run, "rev-parse", "HEAD")
        return True, "", (sha.stdout or "").strip() if _ok(sha) else ""


def _has_frontmatter(text: str) -> bool:
    return text.startswith("---\n") and "\n---\n" in text


def _cancel(slug: str, vault_root, items_dir, run, today: str) -> tuple[bool, str]:
    from todo_system import cli          # helpers only; cli.py itself is not changed

    def patch(text: str) -> str:
        if not _has_frontmatter(text):
            return text                  # no status line to set — refused as unchanged
        out = cli.update_frontmatter_value(text, "status", "cancelled")
        out = cli.update_frontmatter_value(out, "updated", today)
        return _with_log_line(out, i18n.t("triage.md.cancel", today=today))

    ok, msg, _sha = _md_commit(slug, vault_root, items_dir, run, patch,
                               f"chore: cancel {slug} (dashboard)")
    return (True, _msg("cancelled")) if ok else (False, msg)


def _instruct(slug: str, vault_root, items_dir, text, run, schedule, today: str
              ) -> tuple[bool, str]:
    from todo_system import cli

    line = " ".join(str(text or "").split())
    if not line:
        return False, _msg("bad_text")

    def patch(md: str) -> str:
        if not _has_frontmatter(md):
            return md
        out = cli.update_frontmatter_value(md, "updated", today)
        return _with_log_line(out, i18n.t("triage.md.instruction", today=today, text=line))

    ok, msg, sha = _md_commit(slug, vault_root, items_dir, run, patch,
                              f"chore: user instruction for {slug} (dashboard)")
    if not ok:
        return False, msg
    try:
        schedule(vault_root, slug, after_commit=sha or None, note="dashboard: user instruction")
    except Exception as e:
        logger.warning("dashboard action instruct: resume not reserved task=%s err=%s", slug, e)
        return False, _msg("instructed_no_resume")
    return True, _msg("instructed")
