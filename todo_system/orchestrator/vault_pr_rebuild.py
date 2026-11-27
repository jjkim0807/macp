"""Rebuild a vault worker PR without the engine's own sync commits, then merge.

A vault task's worktree is the vault repo itself, so while `worker/<slug>` is
checked out the todo-sync, planner and beta-watermark daemons can commit onto
that branch. The same content then reaches `main` as different commits, and the
PR reads CONFLICTING on files nobody in the run touched — the live queue, the
assignments ledger, `Memory/index.md`. Known since PR #160 (2026-09-21); #433 and
#434 stranded together on 2026-11-27 and were moved by hand: pick the run's own
commits, cherry-pick them onto `origin/main`, merge that.

This module is that hand procedure, run by the engine when a vault delegate PR's
squash merge is refused for a conflict:

1. Walk `merge-base(origin/main, branch)..branch`, drop commits whose subject is
   one of the engine's own (`ENGINE_COMMIT_PREFIXES` — the one list).
2. Replay the rest onto `origin/main` *inside the object database* with
   `git merge-tree --write-tree --merge-base=<parent>` — the cherry-pick
   3-way merge with no checkout, no index and no half-finished state to clean
   up. A commit whose change is already on main replays to an unchanged tree
   and is skipped. A commit that genuinely conflicts stops the rebuild: nothing
   is pushed and the caller parks the task with the conflicting files.
3. Publish the result on the SAME branch as a fast-forward: a graft commit whose
   parents are the old branch tip and the rebuilt tip and whose tree is the
   rebuilt tree. The PR's merge-base becomes `origin/main`, so its diff is the
   run's own work and nothing else, and the squash lands exactly that tree.
   Fast-forward rather than force-with-lease because the vault's git dir carries
   the engine's FF-only pre-push guard (`stages.install_push_guard`) and
   `--no-verify` is banned; a FF push is also lease-safe by itself — the server
   refuses it if the branch moved.
4. Ask for the merge again (`stages.merge_pr`, the same `--squash
   --delete-branch`; never `--admin`, never `--auto`), retrying briefly because
   GitHub recomputes mergeability after a push.

Only ever called for a vault task's own `worker/*` branch; an external code
repo's PR never reaches it (the runner gates on `repo_url`). Never raises.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

# Subjects of commits the engine and its daemons write into the vault on their
# own. Kept here, in one place; the producers are worker.py `_sync_vault_state`
# (`chore: sync vault state (N files)`), the planner agent (`chore: planner …` /
# `chore(planner): …` — the subjects scripts/planner_tick.sh already recognises
# as the planner's own), beta_publish.py (`chore: beta watermark …`),
# mailbox.py (`chore(mailbox): …`), session_log.py (`chore(session): …`),
# worker.py vault-pin (`chore: bump macp pin …`) and health_report.py
# (`chore(health): …`). The test suite scans those sources so a renamed subject
# cannot drift away from this list silently.
#
# Deliberately NOT here: `chore(worker): …` (the run's own phase / rescue
# commits — the real work) and the dashboard's `chore: … (dashboard)` commits (a
# person's decision; dropping one could lose it — if it is already on main it
# replays empty and is skipped anyway).
ENGINE_COMMIT_PREFIXES: tuple[str, ...] = (
    "chore: sync vault state",
    "chore: planner",
    "chore(planner):",
    "chore: beta watermark",
    "chore(mailbox):",
    "chore(session):",
    "chore: bump macp pin",
    "chore(health):",
)

# What a refused merge says when the branches conflict. "not mergeable" alone is
# also a branch-policy refusal, so it does not count (triage_actions._CONFLICT_RE).
_CONFLICT_RE = re.compile(r"cannot be cleanly created|merge conflict|conflict", re.I)

_GIT_TIMEOUT_S = 60
_PUSH_TIMEOUT_S = 120
MERGE_ATTEMPTS = 3
MERGE_RETRY_DELAY_S = 10.0


def is_engine_commit(subject: str) -> bool:
    """True when `subject` is one the engine's own daemons write."""
    s = (subject or "").strip()
    return any(s.startswith(p) for p in ENGINE_COMMIT_PREFIXES)


def looks_like_conflict(note: str | None) -> bool:
    """True when a `merge_pr` failure note reads as a merge conflict."""
    return bool(_CONFLICT_RE.search(note or ""))


def _git(repo_dir: Path, *args: str, env: dict | None = None, stdin: str | None = None,
         timeout: float = _GIT_TIMEOUT_S) -> subprocess.CompletedProcess:
    """`git -C <repo_dir> <args>`; a spawn failure comes back as rc 127."""
    full_env = None
    if env:
        full_env = dict(os.environ)
        full_env.update(env)
    try:
        return subprocess.run(["git", "-C", str(repo_dir), *args], input=stdin,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, env=full_env)
    except (OSError, subprocess.SubprocessError) as e:
        return subprocess.CompletedProcess(args, 127, "", f"{type(e).__name__}: {e}")


def _out(r: subprocess.CompletedProcess) -> str:
    return (r.stdout or "").strip()


def _err(r: subprocess.CompletedProcess) -> str:
    return ((r.stderr or r.stdout or "").strip()[:200]) or "no output"


def _tree(repo_dir: Path, rev: str) -> str:
    r = _git(repo_dir, "rev-parse", f"{rev}^{{tree}}")
    return _out(r) if r.returncode == 0 else ""


def rebuild_onto_base(repo_dir: Path, branch: str, base_ref: str) -> dict:
    """Replay `branch`'s own commits onto `base_ref`, dropping engine commits.

    Writes commits to the object database only — no ref, no checkout, no index.
    Returns::

        {"ok": bool, "tip": sha|None, "kept": [sha], "dropped": [sha],
         "skipped_empty": [sha], "conflict_files": [path], "note": str}

    `ok` with `tip` None means every real commit was already on the base (nothing
    left to merge). `ok` False with `conflict_files` is a genuine conflict of the
    run's own work; `ok` False without them is a git failure (see `note`)."""
    out: dict = {"ok": False, "tip": None, "kept": [], "dropped": [],
                 "skipped_empty": [], "conflict_files": [], "note": ""}
    tip = _out(_git(repo_dir, "rev-parse", "--verify", f"{branch}^{{commit}}"))
    base = _out(_git(repo_dir, "rev-parse", "--verify", f"{base_ref}^{{commit}}"))
    if not tip or not base:
        out["note"] = f"cannot resolve {branch} / {base_ref}"
        return out
    mb_r = _git(repo_dir, "merge-base", base, tip)
    mb = _out(mb_r)
    if mb_r.returncode != 0 or not mb:
        out["note"] = f"no merge-base between {base_ref} and {branch}"
        return out
    log = _git(repo_dir, "log", "--reverse", "--format=%H%x00%P%x00%s", f"{mb}..{tip}")
    if log.returncode != 0:
        out["note"] = f"git log {mb[:9]}..{branch} failed: {_err(log)}"
        return out
    commits = []
    for line in (log.stdout or "").splitlines():
        if not line.strip():
            continue
        sha, parents, subject = (line.split("\x00") + ["", ""])[:3]
        commits.append((sha, parents.split(), subject))
    if not commits:
        out["note"] = f"{branch} has no commits over {base_ref}"
        return out
    cur = base
    cur_tree = _tree(repo_dir, cur)
    for sha, parents, subject in commits:
        if is_engine_commit(subject):
            out["dropped"].append(sha)
            continue
        if len(parents) != 1:
            # A merge commit has no single parent to replay against. Rare enough
            # on a worker branch that guessing a mainline is not worth the risk.
            out["note"] = f"merge commit {sha[:9]} on {branch} — not rebuilt"
            return out
        mt = _git(repo_dir, "merge-tree", "--write-tree", "--name-only",
                  f"--merge-base={parents[0]}", cur, sha)
        lines = (mt.stdout or "").splitlines()
        if mt.returncode == 1:
            files: list[str] = []
            for f in lines[1:]:
                if not f.strip():
                    break
                if f not in files:
                    files.append(f)
            out["conflict_files"] = files
            out["note"] = (f"{sha[:9]} ({subject[:60]}) conflicts with {base_ref}: "
                           + (", ".join(files) or "unknown files"))
            return out
        if mt.returncode != 0 or not lines:
            out["note"] = f"merge-tree for {sha[:9]} failed: {_err(mt)}"
            return out
        tree = lines[0].strip()
        if tree == cur_tree:
            out["skipped_empty"].append(sha)     # already on the base
            continue
        meta = _git(repo_dir, "log", "-1", "--format=%an%x00%ae%x00%aI%x00%B", sha)
        an, ae, ad, body = ((meta.stdout or "").split("\x00", 3) + ["", "", "", ""])[:4]
        msg = body.rstrip("\n") + f"\n\n(cherry picked from commit {sha})\n"
        env = {"GIT_AUTHOR_NAME": an, "GIT_AUTHOR_EMAIL": ae, "GIT_AUTHOR_DATE": ad} \
            if an and ae else None
        ct = _git(repo_dir, "commit-tree", tree, "-p", cur, "-F", "-", env=env, stdin=msg)
        new = _out(ct)
        if ct.returncode != 0 or not new:
            out["note"] = f"commit-tree for {sha[:9]} failed: {_err(ct)}"
            return out
        out["kept"].append(sha)
        cur, cur_tree = new, tree
    out["ok"] = True
    out["tip"] = cur if cur != base else None
    out["note"] = (f"rebuilt {len(out['kept'])} commit(s) onto {base_ref}; dropped "
                   f"{len(out['dropped'])} engine commit(s), skipped "
                   f"{len(out['skipped_empty'])} already on {base_ref}")
    return out


def _graft(repo_dir: Path, branch: str, old_tip: str, rebuilt: str, base_ref: str) -> str:
    """A commit with the rebuilt tree whose parents are [old_tip, rebuilt], so
    publishing it on `branch` is a fast-forward. "" on failure."""
    tree = _tree(repo_dir, rebuilt)
    if not tree:
        return ""
    msg = (f"chore(worker): rebuild {branch} onto {base_ref} without engine commits\n\n"
           "The branch carried the vault daemons' own commits (sync / planner / "
           "watermark), which main holds under other shas, so the PR conflicted.\n"
           f"Tree = {base_ref} + this run's commits only (second parent).\n")
    r = _git(repo_dir, "commit-tree", tree, "-p", old_tip, "-p", rebuilt, "-F", "-",
             stdin=msg)
    return _out(r) if r.returncode == 0 else ""


def rebuild_and_merge(repo_dir: Path, branch: str, *, repo: str | None = None,
                      merge: Callable[..., dict] | None = None,
                      progress: Callable[[str], None] | None = None,
                      sleep: Callable[[float], None] = time.sleep,
                      attempts: int = MERGE_ATTEMPTS,
                      delay_s: float = MERGE_RETRY_DELAY_S) -> dict:
    """Rebuild `branch` without engine commits, push it (FF) and merge again.

    `repo_dir` is the vault task's worktree (where `branch` is checked out).
    Returns `{"merged": bool, "rebuilt": bool, "conflict_files": [path],
    "note": str}`. `merged` is True only on a merge that returned success.
    Never raises; every failure lands in `note`."""
    out: dict = {"merged": False, "rebuilt": False, "conflict_files": [], "note": ""}

    def say(text: str) -> None:
        if progress is not None:
            try:
                progress(f"rebuild: {text}")
            except Exception:   # noqa: BLE001 - logging must not break the rebuild
                pass

    try:
        if not branch.startswith("worker/"):
            out["note"] = f"refusing to rebuild non-worker branch {branch}"
            return out
        if merge is None:
            from todo_system.orchestrator import stages
            merge = stages.merge_pr
        dr = _git(repo_dir, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        default = _out(dr).split("/", 1)[-1] if dr.returncode == 0 and _out(dr) else "main"
        fr = _git(repo_dir, "fetch", "origin", default, timeout=_PUSH_TIMEOUT_S)
        if fr.returncode != 0:
            out["note"] = f"fetch origin {default} failed: {_err(fr)}"
            return out
        base_ref = f"origin/{default}"
        old_tip = _out(_git(repo_dir, "rev-parse", "--verify", f"{branch}^{{commit}}"))
        rb = rebuild_onto_base(repo_dir, branch, base_ref)
        if not rb["ok"]:
            out["conflict_files"] = rb["conflict_files"]
            out["note"] = (("실작업 커밋 충돌 — 재배치 포기: " + ", ".join(rb["conflict_files"]))
                           if rb["conflict_files"] else f"재배치 실패: {rb['note']}")
            say(out["note"])
            return out
        say(rb["note"])
        if rb["tip"] is None:
            out["note"] = f"재배치할 실작업 커밋 없음 — 전부 {base_ref}에 이미 있음"
            return out
        graft = _graft(repo_dir, branch, old_tip, rb["tip"], base_ref)
        if not graft:
            out["note"] = "재배치 그라프트 커밋 생성 실패"
            return out
        # A plain push of a descendant: fast-forward, so the pre-push guard and the
        # server both accept it, and the server refuses it if the branch moved.
        pr = _git(repo_dir, "push", "origin", f"{graft}:refs/heads/{branch}",
                  timeout=_PUSH_TIMEOUT_S)
        if pr.returncode != 0:
            out["note"] = f"재배치 push 거부: {_err(pr)}"
            say(out["note"])
            return out
        out["rebuilt"] = True
        say(f"pushed {graft[:9]} to {branch} (fast-forward)")
        # Keep the local branch (checked out in this worktree) on the pushed tip;
        # best-effort — the push is what matters.
        _git(repo_dir, "merge", "--ff-only", "-q", graft)
        last = ""
        for i in range(max(1, attempts)):
            if i:
                sleep(delay_s)        # GitHub recomputes mergeability after a push
            mp = merge(repo_dir, branch, repo=repo, progress=progress) or {}
            last = mp.get("note") or ""
            if mp.get("merged"):
                out["merged"] = True
                out["note"] = (f"재배치 후 머지 — 엔진 커밋 {len(rb['dropped'])}개 제외, "
                               f"실작업 {len(rb['kept'])}개 재적용")
                return out
        out["note"] = f"재배치 후에도 머지 실패: {last or 'merge not confirmed'}"
        return out
    except Exception as e:      # noqa: BLE001 - never raises
        out["note"] = f"재배치 중 예외: {type(e).__name__}"
        return out
