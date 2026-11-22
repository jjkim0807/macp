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

And the one action of a decision card (triage.py, "Decision cards"), which the
route offers by its own click-time check (triage.decision_card), not by a
triage card:

* answer   -> a 「결정(대시보드)」 Progress Log line carrying the choice, the
              decision marker lowered (`needs_decision: false`, the `decision`
              tag and `decision_options` removed), `next_action` rewritten to
              say what was chosen, and `updated` — one file, committed the same
              way. When the ledger row is a human assignment still
              awaiting_human, one `needs-input` reply goes to the orchestrator
              (human.reply, what `/decide` sends), so the row carries the
              answer and stays parked.

              An answer only records. It never closes, merges, resumes,
              cancels or dispatches, whatever the chosen words say: a choice
              that names a payment, a commitment, a deletion or a submission is
              written down like any other, and carrying it out stays with a
              follow-up task or a person. `status` is left as it is because the
              server cannot tell a choice that ends the task from one that
              leaves a step to do.

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
from .assignment import AWAITING_HUMAN, Ledger
from .triage import DECISION_TAG, _clip, _tags, md_decision, pr_ref

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
               today: str | None = None, reply=None, assign_dir=None) -> tuple[bool, str]:
    """Run one action for `slug`. `facts` are the click-time facts
    (triage.gather) a triage card was diagnosed from; `answer` reads none.
    `text` is the instruction (instruct) or the chosen words (answer). `run`
    replaces subprocess.run (gh and git), `schedule` replaces resume.schedule,
    `reply` replaces the reply to the orchestrator and `today` the date — test
    seams; the defaults are the real paths. `assign_dir` is the ledger
    directory (default <vault_root>/Worker/assignments)."""
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
    if action == "answer":
        return _answer(slug, vault_root, items_dir, text, run, today, reply, assign_dir)
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


# --- answer: record a decision (it only records — see the module docstring) ----------

_TAGS_SENTINEL = "\x00"    # stands in for the tags value while the list is rewritten
_NEXT_ACTION_MAX = 200     # chars of the rewritten next_action; the log line keeps it all
_REPLY_NOTE_MAX = 200      # assignment.apply_reply keeps 200 chars of a note
_SAID_MAX = 80             # chars of the choice quoted back in the result sentence


def _fm_quote(value: str) -> str:
    """`value` as a double-quoted one-line frontmatter scalar every line parser
    here reads back whole: no backslash, inner double quotes made single."""
    return '"' + value.replace("\\", "").replace('"', "'")[:_NEXT_ACTION_MAX] + '"'


def _drop_tag(text: str, fm: dict, tag: str) -> str:
    """`text` with `tag` (any case) taken out of its frontmatter `tags`. The tags
    that stay are written back as a block list, whatever form the field had; a
    list left empty loses the key. Unchanged when the tag is not there."""
    from todo_system import cli

    tags = [t for t in _tags(fm.get("tags")) if t]
    keep = [t for t in tags if t.lower() != tag]
    if len(keep) == len(tags):
        return text
    if not keep:
        return cli.remove_frontmatter_key(text, "tags")
    out = cli.update_frontmatter_value(text, "tags", _TAGS_SENTINEL)
    return out.replace(f"tags: {_TAGS_SENTINEL}",
                       "tags:" + "".join(f"\n  - {t}" for t in keep), 1)


def _human_reply(vault_root, row, slug: str, note: str) -> None:
    """Tell the orchestrator a person answered `slug`: one reply on the row's own
    corr_id (collect() drops any other), put in the orchestrator's inbox under
    the vault.

    `needs-input`, on purpose. collect() closes the todo on `done`, and an
    answer does not know whether the task is over. `progress` would leave a
    human row in_progress, which nothing reaps and which the checkout gate
    counts as in flight for good. `needs-input` stores the note and collect()
    parks the row again (awaiting_human), which is all an answer may do.

    A file mailbox, not the git one: the envelope is a file on this host,
    where the orchestrator's next tick reads it, and the vault sync commits it
    with the other mailbox files. Nothing here commits or pushes for it."""
    from . import human
    from .mailbox import DEFAULT_ROOT, open_mailbox

    human.reply(open_mailbox(f"file://{DEFAULT_ROOT}", base=Path(vault_root)),
                row.agent_id, slug, "needs-input", note, row.corr_id)


def _answer(slug: str, vault_root, items_dir, choice, run, today: str, reply, assign_dir
            ) -> tuple[bool, str]:
    from todo_system import cli

    from ..session_log import parse_frontmatter

    line = " ".join(str(choice or "").split())
    if not line:
        return False, _msg("bad_answer")
    path = Path(items_dir) / f"{slug}.md"
    if not path.is_file():
        return False, _msg("md_missing")

    def raises_decision(md: str) -> bool:
        return _has_frontmatter(md) and md_decision(parse_frontmatter(md)[0])

    if not raises_decision(_read_text(path)):
        return False, _msg("not_offered")
    answered_meanwhile = False

    def patch(md: str) -> str:
        nonlocal answered_meanwhile
        if not raises_decision(md):
            answered_meanwhile = True        # answered inside the window: write nothing
            return md
        fm, _ = parse_frontmatter(md)
        out = cli.update_frontmatter_value(md, "needs_decision", "false")
        out = cli.remove_frontmatter_key(out, "decision_options")
        out = _drop_tag(out, fm, DECISION_TAG)
        out = cli.update_frontmatter_value(
            out, "next_action", _fm_quote(i18n.t("triage.md.answer_next", text=line)))
        out = cli.update_frontmatter_value(out, "updated", today)
        return _with_log_line(out, i18n.t("triage.md.answer", today=today, text=line))

    ok, msg, _sha = _md_commit(slug, vault_root, items_dir, run, patch,
                               f"chore: record decision for {slug} (dashboard)")
    if not ok:
        return False, (_msg("not_offered") if answered_meanwhile else msg)
    # The record is committed. A human assignment that is still waiting hears
    # about it once; any other row (a worker's, a finished one, none) hears nothing.
    try:
        row = Ledger(assign_dir or Path(vault_root) / "Worker" / "assignments").load(slug)
        if (row is not None and str(row.kind or "").startswith("human")
                and row.state == AWAITING_HUMAN):
            (reply or _human_reply)(vault_root, row, slug,
                                    f"answered via dashboard: {line}"[:_REPLY_NOTE_MAX])
    except Exception as e:
        # the answer itself stays out of the log, like an instruction's text
        logger.warning("dashboard action answer: reply not sent task=%s err=%s", slug,
                       type(e).__name__)
        return False, _msg("answered_no_reply")
    return True, _msg("answered", choice=_clip(line, _SAID_MAX))
