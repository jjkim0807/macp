"""One-shot session-end checklist for second-brain (CLAUDE.md §10).

``session-end`` runs the manual "세션 마무리" ("wrap up the session") / "macp 동기화"
("sync macp") checklist in a single command, so the steps in CLAUDE.md §10.1–§10.3
don't have to be re-discovered each time:

  1. **Classify** ``git status`` into ``ignorable`` / ``must_commit`` / ``ignored``
     — but decide ``Memory/index.md`` by its *actual diff*, not by path: a churn
     that touches only the ``last_synced:`` line is ignorable (the todo-sync daemon
     rewrites it every tick), while any other changed line makes it ``must_commit``.
  2. **Record** the session journal via ``session_log log --repo-dir . --commit``
     (journal only — content files are never auto-committed here; the human commits
     those with a real message).
  3. **Report** sync state (``git rev-list --left-right --count origin/main...HEAD``)
     and the secondbrain launchd daemons' PIDs. Report only: never ``push`` (ahead>0
     is normal — the todo-sync daemon pushes ``chore: sync vault state``), never
     restart a daemon (they auto-kickstart within ~5 min); a dead daemon is warned.

Exit code is non-zero only when non-exempt (``must_commit``) files remain
uncommitted after the journal is recorded — those would stall the 5-minute queue
worker. ``--dry-run`` classifies and reports without recording or committing.

CLAUDE.md §10 is the authoritative spec; if this module and §10 disagree, §10 wins.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from . import paths
from . import session_log
from todo_system import daemonctl

# Vault root via the shared resolver (env → .macp marker → repo-root fallback).
# The start= anchor is paths.vault_search_start(): the engine root in a checkout,
# so with MACP_VAULT_ROOT unset it stays byte-identical to the old
# Path(__file__).resolve().parents[1]; None in an installed wheel, where the
# cwd-upward search replaces a site-packages anchor. Read at *call* time inside
# cmd_session_end (never captured into a default argument) so tests can
# monkeypatch it, mirroring how session_log.cmd_log reads session_log.ROOT.
ROOT = paths.vault_root(start=paths.vault_search_start())

_INDEX_PATH = "Memory/index.md"
# git status paths under these prefixes are runner/orchestrator runtime churn
# (§10.1: "?? Worker/** → ignore"). Kept as a tuple for str.startswith().
_IGNORABLE_PREFIXES = ("Worker/",)
_DAEMON_MATCH = "secondbrain"
# daemonctl.UNITS labels all start with the org prefix (e.g. "com.macp.secondbrain.
# orchestrator"); loaded_pids() filters by startswith, so the bare _DAEMON_MATCH
# substring above always misses on the systemd path. Read the prefix from daemonctl so
# it tracks the configured label prefix (MACP_LABEL_PREFIX / .macp config) instead of
# re-hard-coding it, and stays in parity with the darwin substring match.
_DAEMON_UNIT_PREFIX = daemonctl.SECONDBRAIN_PREFIX


# ---------------------------------------------------------------------------
# git helpers
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str | None:
    """Run git in *root*; return stripped stdout, or None on error/absence.

    Mirrors ``session_log._git`` exactly — the same tolerant contract (None on a
    CalledProcessError or a missing git binary) the rest of the vault relies on.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out.stdout.strip()


def _index_only_last_synced(root: Path) -> bool:
    """True iff every changed line in ``Memory/index.md`` is a ``last_synced:`` line.

    Uses the real diff against HEAD rather than the path: the todo-sync daemon
    rewrites only the ``last_synced:`` timestamp on most ticks (ignorable churn the
    daemon itself absorbs next tick), but a genuine index change touches other lines
    and must be committed. An untracked or absent index.md produces an empty diff →
    False → treated as ``must_commit`` by the caller.
    """
    diff = _git(root, "diff", "HEAD", "--", _INDEX_PATH)
    if not diff:
        return False
    changed = []
    for line in diff.splitlines():
        if line.startswith(("+++", "---")):  # file headers, not content
            continue
        if line.startswith(("+", "-")):
            changed.append(line[1:])
    if not changed:
        return False
    return all(ln.lstrip().startswith("last_synced:") for ln in changed)


def _porcelain_lines(root: Path) -> list[str]:
    """``git status --porcelain --ignored`` lines, WITHOUT stripping.

    Porcelain v1 lines are ``XY<space>PATH`` with a fixed two-column status code,
    so the leading space of an unstaged-modification code (`` M``) is *significant*.
    ``_git`` strips its whole stdout, which would shift the first line's columns
    (`` M Memory/index.md`` → ``M Memory/index.md`` → path ``emory/index.md``), so
    porcelain must be read raw: ``splitlines()`` keeps every line's leading space.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--ignored"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return proc.stdout.splitlines()


def classify_status(root: Path) -> dict[str, list[str]]:
    """Bucket ``git status --porcelain --ignored`` per CLAUDE.md §10.1.

    - ``!!`` (git-ignored, e.g. ``Todo/items/**``, ``Todo/todo.db``) → ``ignored``
      (no commit needed).
    - under ``Worker/`` → ``ignorable`` (runner/orchestrator runtime churn).
    - ``Memory/index.md`` → ``ignorable`` if only its ``last_synced:`` line changed,
      else ``must_commit``.
    - everything else (``Notes/``, ``Decisions/``, ``Memory/entities/``,
      ``Memory/sessions/``, ``CLAUDE.md``, …) → ``must_commit`` (must be committed).

    Returns ``{"ignorable": [...], "must_commit": [...], "ignored": [...]}`` of paths.
    """
    buckets: dict[str, list[str]] = {"ignorable": [], "must_commit": [], "ignored": []}
    for line in _porcelain_lines(root):
        if not line:
            continue
        code, path = line[:2], line[3:]
        if " -> " in path:  # rename/copy entry: classify by the destination path
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if code == "!!":
            buckets["ignored"].append(path)
        elif path.startswith(_IGNORABLE_PREFIXES):
            buckets["ignorable"].append(path)
        elif path == _INDEX_PATH:
            key = "ignorable" if _index_only_last_synced(root) else "must_commit"
            buckets[key].append(path)
        else:
            buckets["must_commit"].append(path)
    return buckets


def sync_status(root: Path) -> tuple[int, int] | None:
    """``(behind, ahead)`` vs ``origin/main``, or None if the ref is absent/unreadable.

    ahead>0 is normal — the todo-sync daemon pushes on its next tick — so this is
    reported, never acted on.
    """
    out = _git(root, "rev-list", "--left-right", "--count", "origin/main...HEAD")
    if not out:
        return None
    parts = out.split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# daemon liveness (report only — never restart; they auto-kickstart)
# ---------------------------------------------------------------------------


def _parse_daemon_pids(text: str) -> dict[str, int | None]:
    """Map each secondbrain launchd label → PID (None = loaded but not running).

    ``launchctl list`` prints ``PID<TAB>Status<TAB>Label``; a ``-`` in the PID
    column means the job is loaded but has no live process. The header row
    (``PID Status Label``) is skipped because it lacks the ``secondbrain`` match.
    """
    pids: dict[str, int | None] = {}
    for line in text.splitlines():
        if _DAEMON_MATCH not in line:
            continue
        cols = line.split("\t") if "\t" in line else line.split()
        if len(cols) < 2:
            continue
        pid_str, label = cols[0].strip(), cols[-1].strip()
        try:
            pids[label] = int(pid_str)
        except ValueError:
            pids[label] = None  # "-" or non-int → loaded but not running
    return pids


def _daemon_pids() -> dict[str, int | None] | None:
    """secondbrain daemon PIDs — ``launchctl list`` on the Mac, systemd --user on
    the cloud HQ; None if the platform controller is absent."""
    if not daemonctl.IS_DARWIN:
        return daemonctl.loaded_pids(prefix=_DAEMON_UNIT_PREFIX)
    try:
        proc = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        return None
    return _parse_daemon_pids(proc.stdout)


# ---------------------------------------------------------------------------
# journal recording (delegates to session_log; journal only, never content)
# ---------------------------------------------------------------------------


def run_session_log(
    root: Path, project: str, note: str | None, tasks: list[str] | None
) -> int:
    """Record the session journal and commit *only* it (``Memory/sessions/``).

    Delegates to ``session_log`` in-process (no subprocess); ``--commit`` there
    stages the journal alone, never content files. Returns session_log's exit code.
    """
    argv = ["log", "--project", project, "--repo-dir", str(root), "--commit"]
    if note:
        argv += ["--note", note]
    for task in tasks or []:
        argv += ["--task", task]
    return session_log.main(argv)


# ---------------------------------------------------------------------------
# human-readable summary
# ---------------------------------------------------------------------------


def _format_summary(
    buckets: dict[str, list[str]],
    committed: list[str],
    sync: tuple[int, int] | None,
    daemons: dict[str, int | None] | None,
    dry_run: bool,
) -> list[str]:
    """Render the "무시 N / 커밋 N / 처리할 것 N" (ignored N / committed N / to-handle N)
    summary as a list of lines.

    - 무시 (ignored) = ignorable + ignored (noise the human need not touch).
    - 커밋 (committed) = files this command committed for you (the journal).
    - 처리할 것 (to-handle) = must_commit files that remain — the human's
      responsibility, and what drives the exit code.
    """
    must = buckets["must_commit"]
    n_ignore = len(buckets["ignorable"]) + len(buckets["ignored"])
    lines = [
        "=== 세션 마무리 (CLAUDE.md §10) ===" + (" [dry-run]" if dry_run else ""),
        f"무시 {n_ignore} / 커밋 {len(committed)} / 처리할 것 {len(must)}",
    ]
    if committed:
        lines.append(f"커밋됨(저널): {', '.join(committed)}")
    if must:
        lines.append("처리할 것 (반드시 커밋 — 안 하면 5분 큐 워커가 정지):")
        lines += [f"  - {p}" for p in must]
    else:
        lines.append("처리할 것: 없음 (must_commit 잔여 없음).")

    if sync is None:
        lines.append("동기화: origin/main 원격추적 ref 없음 — 상태 미확인.")
    else:
        behind, ahead = sync
        lines.append(
            f"동기화: behind {behind} / ahead {ahead} "
            "— ahead>0은 정상(todo-sync 데몬이 push). 수동 push 불필요."
        )

    if daemons is None:
        lines.append("데몬: launchctl 사용 불가 — 생존 확인 생략.")
    elif not daemons:
        lines.append("데몬: secondbrain 항목 없음 — 로드되지 않음(주의).")
    else:
        # Report-only. A ``-`` PID (미실행, "not running") is normal for the
        # *periodic* jobs (brief/health/…), which are loaded but idle between
        # scheduled runs, so we surface the raw PID column rather than crying
        # "dead" on every idle job — the human judges the persistent daemons
        # (todo-sync·runner) per §10.3.
        rendered = [
            f"{label}={'PID ' + str(pid) if pid is not None else '미실행'}"
            for label, pid in sorted(daemons.items())
        ]
        lines.append("데몬: " + " · ".join(rendered))
        if any(pid is None for pid in daemons.values()):
            lines.append(
                "  ↳ '미실행'은 주기 작업(brief/health 등)이면 정상. "
                "todo-sync·runner가 미실행이면 사람이 확인(재시작 금지 — auto-kickstart)."
            )
    return lines


# ---------------------------------------------------------------------------
# command
# ---------------------------------------------------------------------------


def cmd_session_end(args: argparse.Namespace) -> int:
    root = ROOT  # module global; resolved at call time so tests can patch it

    committed: list[str] = []
    if not args.dry_run:
        before = _git(root, "rev-parse", "HEAD")
        run_session_log(root, args.project, args.note, args.task)
        after = _git(root, "rev-parse", "HEAD")
        if after and after != before:
            names = _git(root, "show", "--name-only", "--format=", after)
            committed = [n for n in (names or "").splitlines() if n.strip()]

    # Classify *after* recording, so the just-committed journal is not itself
    # counted as an outstanding must_commit file.
    buckets = classify_status(root)
    sync = sync_status(root)
    daemons = _daemon_pids()

    print("\n".join(_format_summary(buckets, committed, sync, daemons, args.dry_run)))
    return 1 if buckets["must_commit"] else 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m todo_system.session_end",
        description="Run the CLAUDE.md §10 session-end checklist (classify → journal → sync report).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser(
        "session-end",
        help="Classify status, record the journal, and report sync/daemon state",
    )
    p.add_argument("--project", required=True, help="Project slug for the session journal")
    p.add_argument("--note", help="Freeform note recorded on the journal checkpoint")
    p.add_argument("--task", action="append", help="Related Todo slug to link (repeatable)")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify and report only; record/commit nothing",
    )
    p.set_defaults(func=cmd_session_end)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
