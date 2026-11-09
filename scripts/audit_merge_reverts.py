#!/usr/bin/env python3
"""Audit a git repo for merges that silently dropped the incoming side of a file.

A read-modify-write on a file in a live checkout can leave stale bytes in the
working tree while a merge is landing; the next `git add` + `git commit` then
records those stale bytes as the merge's result. The merge commit looks ordinary
afterwards, and `git show --stat` on it does NOT reveal the loss: a merge prints
the COMBINED diff, which omits every file matching one of the parents — and a
dropped file matches the parent it was reverted to. So exactly the damaged files
are the invisible ones.

This finds them by comparing blob ids. For a two-parent merge M with parents
P_own / P_in and base = merge-base(P_own, P_in), a file where

    own      == base      (this side never touched it)
    incoming != base      (the other side did)
    merged   != incoming  (yet the result is not what came in)

lost the incoming content. The test runs in both parent orders, so which parent
is "ours" does not matter. Files both sides changed are skipped: that is ordinary
conflict resolution, which is a deliberate choice, not a loss. A path missing
from a revision is treated as its own state, so dropped additions and resurrected
deletions are judged by the same comparison.

Read-only. Only `git log`, `git diff --name-only`, `git merge-base` and
`git cat-file --batch-check` are ever run, so this is safe to point at a checkout
that daemons are actively writing to.

Usage:
    python3 -I scripts/audit_merge_reverts.py --repo /path/to/repo
    python3 -I scripts/audit_merge_reverts.py --since 2026-10-20 --path Todo/items

Exit status: 0 = no losses found, 1 = at least one loss, 2 = usage or git error.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

# A merge with more than two parents has no single "own vs incoming" reading, so
# the comparison above does not apply; they are counted and reported, not judged.
MAX_PARENTS = 2


class GitError(RuntimeError):
    pass


def _git(repo: str, args: list[str], stdin: str | None = None) -> str:
    """Run one read-only git command in `repo` and return stdout."""
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        input=stdin, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): "
                       f"{proc.stderr.strip()}")
    return proc.stdout


def _changed_paths(repo: str, a: str, b: str) -> set[str]:
    """Paths differing between two revisions. NUL-delimited so odd names survive."""
    out = _git(repo, ["diff", "--name-only", "-z", a, b])
    return {p for p in out.split("\0") if p}


def _blob_ids(repo: str, keys: list[tuple[str, str]]) -> list[str | None]:
    """[(rev, path)] -> [object id | None], input order preserved, in ONE git call.

    `cat-file --batch-check` emits one line per input line: `<oid> <type> <size>`
    for an object that exists, `<input> missing` for one that does not. Keying on
    position rather than on the echoed name keeps paths containing spaces intact.
    """
    if not keys:
        return []
    stdin = "".join(f"{rev}:{path}\n" for rev, path in keys)
    out = _git(repo, ["cat-file", "--batch-check"], stdin=stdin)
    ids: list[str | None] = []
    for line in out.splitlines():
        ids.append(None if line.endswith(" missing") else line.split(" ", 1)[0])
    if len(ids) != len(keys):
        raise GitError(f"cat-file returned {len(ids)} lines for {len(keys)} queries")
    return ids


def _merges(repo: str, since: str | None, until: str | None,
            rev_range: str) -> list[tuple[str, list[str], str]]:
    """[(sha, parents, subject)] for every merge in range, newest first."""
    args = ["log", "--merges", "--format=%H%x00%P%x00%s"]
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    args.append(rev_range)
    rows = []
    for line in _git(repo, args).splitlines():
        if not line.strip():
            continue
        sha, parents, subject = line.split("\0", 2)
        rows.append((sha, parents.split(), subject))
    return rows


def _losses_in(repo: str, sha: str, parents: list[str],
               prefixes: tuple[str, ...]) -> list[tuple[str, str, str]]:
    """Files in merge `sha` whose incoming content was dropped.

    Returns [(path, incoming_id, merged_id)] with "-" standing for absent.
    """
    p_a, p_b = parents
    base = _git(repo, ["merge-base", p_a, p_b]).strip()
    candidates = (_changed_paths(repo, base, p_a)
                  | _changed_paths(repo, base, p_b)
                  # catches an "evil merge" touching a file neither side changed
                  | _changed_paths(repo, base, sha))
    if prefixes:
        candidates = {p for p in candidates if p.startswith(prefixes)}
    paths = sorted(candidates)
    if not paths:
        return []

    revs = (base, p_a, p_b, sha)
    flat = _blob_ids(repo, [(rev, path) for rev in revs for path in paths])
    n = len(paths)
    at = {rev: flat[i * n:(i + 1) * n] for i, rev in enumerate(revs)}

    out = []
    for i, path in enumerate(paths):
        b, a_id, b_id, m = at[base][i], at[p_a][i], at[p_b][i], at[sha][i]
        # Symmetric in parent order: try each side as the one that stood still.
        for own, incoming in ((a_id, b_id), (b_id, a_id)):
            if own == b and incoming != b and m != incoming:
                out.append((path, incoming or "-", m or "-"))
                break
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Count merges that dropped the incoming side of a file.")
    ap.add_argument("--repo", default=None,
                    help="repository to audit (default: this checkout's root)")
    ap.add_argument("--since", default=None, help="only merges after this date")
    ap.add_argument("--until", default=None, help="only merges before this date")
    ap.add_argument("--path", action="append", default=[], dest="paths",
                    help="restrict to paths under this prefix (repeatable)")
    ap.add_argument("--rev-range", default="HEAD",
                    help="revision range to walk (default: HEAD)")
    args = ap.parse_args(argv)

    try:
        repo = args.repo or _git(".", ["rev-parse", "--show-toplevel"]).strip()
        merges = _merges(repo, args.since, args.until, args.rev_range)
    except GitError as exc:
        print(f"audit_merge_reverts: {exc}", file=sys.stderr)
        return 2

    prefixes = tuple(args.paths)
    # Live progress only for a human watching; redirected into a report it would
    # be one long line of every count the scan passed through.
    ticking = sys.stderr.isatty()
    bad_merges = n_files = skipped = 0
    for i, (sha, parents, subject) in enumerate(merges, 1):
        if ticking:
            print(f"\r  {i}/{len(merges)} merges scanned", end="", file=sys.stderr)
        if len(parents) != MAX_PARENTS:
            skipped += 1
            continue
        try:
            losses = _losses_in(repo, sha, parents, prefixes)
        except GitError as exc:
            # One unreadable merge must not void the whole audit.
            print(f"\n  {sha[:9]} skipped: {exc}", file=sys.stderr)
            skipped += 1
            continue
        if not losses:
            continue
        bad_merges += 1
        n_files += len(losses)
        print(f"{sha[:9]} {subject}")
        for path, incoming, merged in losses:
            print(f"    {path}\n"
                  f"      incoming {incoming[:9]} dropped; result {merged[:9]}")
    lead = "\r" if ticking else ""
    print(f"{lead}  {len(merges)} merges scanned", file=sys.stderr)

    tail = f" skipped={skipped}" if skipped else ""
    print(f"merges={len(merges)} losses={bad_merges} files={n_files}{tail}")
    return 1 if bad_merges else 0


if __name__ == "__main__":
    sys.exit(main())
