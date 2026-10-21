"""Manual work-session journaling for second-brain.

Purpose
-------
The worker (`todo_system.worker`) records everything *it* does under
`Worker/runs/` and `Memory/episodes/`. But when a human — or an external agent
running in a *separate* session outside the worker automation — develops a
project manually (e.g. the SC2 bot on a Windows host), that work is invisible
to second-brain. This module is the bridge: a dependency-free CLI an external
agent calls **periodically** to checkpoint what it has been doing, so the vault
keeps a continuous, git-synced record for later.

Design notes
------------
- **Stdlib only.** No third-party imports, so it runs from a bare `git clone`
  of the vault with plain `python3 -m todo_system.session_log ...` — the
  external agent does not need `uv` or the project's dependencies installed.
- **Git-tracked storage.** Journals live in `Memory/sessions/` (tracked, unlike
  the locally-gitignored `Worker/runs/`), so a remote clone can `commit`/`push`
  them back and the primary host picks them up on pull.
- **Auto-captures git state.** Given `--repo-dir`, each checkpoint records the
  target repo's HEAD/branch/dirtiness and the commit subjects that landed since
  the previous checkpoint — the agent only has to add a freeform note.
- **One file per host.** A journal is `Memory/sessions/<date>-<slug>.<host>.md`
  (`<date>-<id>-<slug>.<host>.md` with `--session`). With one file per day and
  project, a mac and the HQ VM recording the same project on the same day each
  created the same path, and the two commits add/add-conflicted on every
  `git rebase origin/main` — vault push stayed blocked (2026-10-19). The host is
  `orchestrator.hq_host.local_host_id()` (`MACP_HOST_ID` env > `~/.macp/host_id`
  > hostname), slugified. Older unsuffixed journals are still listed and shown
  but never appended to again.

Usage
-----
    # One checkpoint (called periodically by the external agent):
    python3 -m todo_system.session_log log \
        --project agent-commander \
        --repo-dir ~/Worker/agent-commander \
        --note "Zerg race-selector + winrate-weighted openings tuning" \
        --task agent-commander-game-mode

    # Optionally sync the vault in the same call:
    python3 -m todo_system.session_log log --project foo --note "..." --commit --push

    python3 -m todo_system.session_log list           # recent journals, every host
    python3 -m todo_system.session_log show foo        # a project's latest journal(s), every host
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
from pathlib import Path

from . import paths
from .orchestrator import hq_host

# Vault root, via the shared resolver. paths is stdlib-only, so this module
# still runs from a bare `git clone` under plain `python3` (no uv / deps), and
# with neither env nor a .macp marker it falls back to the same repo root as
# before.
ROOT = paths.vault_root()
SESSIONS_DIR = ROOT / "Memory" / "sessions"

# Canonical frontmatter key order for session journals (keys not listed are
# appended after, preserving first-seen order).
_FM_ORDER = [
    "tags",
    "project",
    "session",
    "host",
    "event_date",
    "recorded_date",
    "updated",
    "status",
    "repo",
    "ref",
    "last_head",
    "checkpoints",
    "related_tasks",
]
_LIST_KEYS = {"tags", "related_tasks"}


# ---------------------------------------------------------------------------
# time / slug helpers
# ---------------------------------------------------------------------------


def _now_local() -> dt.datetime:
    """Timezone-aware local time (vault convention is Asia/Seoul on the host)."""
    return dt.datetime.now(dt.timezone.utc).astimezone()


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")
    return slug or "session"


def _host_id(home: Path | None = None) -> str:
    """This host's id for journal filenames, from the same identity the HQ guard
    uses (`MACP_HOST_ID` > `~/.macp/host_id` > hostname). Only the first DNS
    label of a hostname is kept, since macOS flaps its `.local` suffix per
    network. `home` is the test seam `hq_host.local_host_id` already has."""
    raw = hq_host.local_host_id(home).strip().split(".", 1)[0]
    return re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-") or "unknown-host"


def _split_journal_name(path: Path) -> tuple[str, str | None]:
    """``(base, host)`` of a journal filename; host is None for legacy files.

    Host ids are ``[a-z0-9-]`` only and new filenames carry no other dot (a
    ``--session`` id's dots are dashed), so the last dot in the stem is the host
    separator."""
    base, dot, host = path.name.removesuffix(".md").rpartition(".")
    return (base, host) if dot else (host, None)


# ---------------------------------------------------------------------------
# git capture
# ---------------------------------------------------------------------------


def _git(repo_dir: Path, *args: str) -> str | None:
    """Run a git command in repo_dir; return stripped stdout, or None on error."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_dir), *args],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out.stdout.strip()


def capture_git(repo_dir: Path, ref: str, last_head: str | None) -> dict | None:
    """Capture git state for a checkpoint.

    Returns a dict with head, branch, dirty (bool) and commits (list of
    "<short> <subject>" strings that are new since ``last_head`` — or the most
    recent handful on the first checkpoint). Returns None if repo_dir is not a
    git repository.
    """
    head = _git(repo_dir, "rev-parse", ref)
    if head is None:
        return None
    short = _git(repo_dir, "rev-parse", "--short", ref) or head[:9]
    # Label with the explicit ref when one was given; otherwise the checked-out
    # branch. (Recording --ref origin/main from a feature-branch worktree should
    # read "origin/main", not the worktree's branch.)
    if ref != "HEAD":
        branch = ref
    else:
        branch = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD") or "?"
    porcelain = _git(repo_dir, "status", "--porcelain")
    dirty = bool(porcelain)

    if last_head and last_head != head:
        rng = f"{last_head}..{ref}"
    elif last_head == head:
        rng = None  # no movement since last checkpoint
    else:
        rng = f"{ref} -10"  # first checkpoint: show recent history for context

    commits: list[str] = []
    if rng is not None:
        if rng.endswith(" -10"):
            log = _git(repo_dir, "log", ref, "-10", "--format=%h %s")
        else:
            log = _git(repo_dir, "log", rng, "--format=%h %s")
        if log:
            commits = log.splitlines()

    return {
        "head": head,
        "short": short,
        "branch": branch,
        "dirty": dirty,
        "commits": commits,
        "moved": last_head != head,
        "first": not last_head,
    }


# ---------------------------------------------------------------------------
# frontmatter read / write (self-contained; matches the vault convention)
# ---------------------------------------------------------------------------


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse leading YAML-ish frontmatter into an ordered dict + body.

    Handles scalars and simple ``- item`` block lists (the only shapes this
    module writes). Mirrors the lightweight parser used elsewhere in the vault.
    """
    if not content.startswith("---\n"):
        return {}, content
    parts = content.split("\n---\n", 1)
    if len(parts) != 2:
        return {}, content
    fm_raw, body = parts
    lines = fm_raw.splitlines()[1:]
    fm: dict = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or ":" not in line:
            i += 1
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if value == "":
            items: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip().startswith("- "):
                items.append(lines[i].strip()[2:].strip().strip('"').strip("'"))
                i += 1
            fm[key] = items
        else:
            fm[key] = value.strip('"')
            i += 1
    return fm, body


def _serialize_frontmatter(fm: dict) -> str:
    keys = [k for k in _FM_ORDER if k in fm] + [k for k in fm if k not in _FM_ORDER]
    out = ["---"]
    for key in keys:
        value = fm[key]
        if key in _LIST_KEYS or isinstance(value, list):
            out.append(f"{key}:")
            for item in value:
                item = str(item)
                # Wikilinks / values with special chars get quoted.
                if item.startswith("[[") or ":" in item:
                    out.append(f'  - "{item}"')
                else:
                    out.append(f"  - {item}")
        else:
            out.append(f"{key}: {value}")
    out.append("---")
    # The trailing newline is load-bearing. ``parse_frontmatter`` splits on
    # "\n---\n" and therefore *consumes* the newline after the closing
    # delimiter, so a serializer that stopped at a bare "---" would give back
    # one fewer leading body newline on every rewrite. After three checkpoints
    # the delimiter fused with the title ("---# Manual session …") and the
    # frontmatter stopped parsing at all. Ending on "\n" restores exactly what
    # the split ate, making serialize(parse(x)) a fixed point.
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# repairing journals damaged by the old round-trip
# ---------------------------------------------------------------------------

_SEAM = "-" * 6
_CHECKPOINT_HEADING = re.compile(r"^### \d\d:\d\d:\d\d", re.MULTILINE)
# Same heading, capturing the time — used to re-derive a stalled ``updated``.
_HEADING_TIME = re.compile(r"^### (\d\d:\d\d:\d\d)", re.MULTILINE)


def _int_or_zero(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _derive_updated(event_date, time_text: str, current) -> str | None:
    """``updated`` for the last checkpoint heading on disk, or None to leave it.

    The heading carries a wall-clock time and an abbreviation ("18:14:46 KST"),
    neither of which pins a UTC offset, so the offset is reused from the
    journal's *existing* ``updated`` rather than read off the host clock — the
    same file must repair identically on any machine. Anything unreadable (no
    ``updated``, naive ``updated``, unparseable ``event_date``) means the
    timestamp cannot be derived, and it is left untouched rather than guessed.

    Returns None unless the derived value moves *forward*: a session that ran
    past midnight combines its start date with an early-morning heading and
    lands before the recorded time, and rewinding a timestamp is worse than
    leaving a stale one.
    """
    try:
        existing = dt.datetime.fromisoformat(str(current))
    except (TypeError, ValueError):
        return None
    if existing.tzinfo is None:
        return None
    try:
        derived = dt.datetime.fromisoformat(f"{event_date}T{time_text}")
    except (TypeError, ValueError):
        return None
    derived = derived.replace(tzinfo=existing.tzinfo)
    if derived <= existing:
        return None
    return derived.isoformat(timespec="seconds")


def _split_stacked_frontmatter(text: str) -> tuple[list[list[str]], str, bool] | None:
    """Split a damaged head area into its stacked frontmatter blocks.

    Returns ``(blocks, body, fused)`` where *blocks* are newest-first line lists,
    or None if the file does not open with a delimiter / never terminates one.

    The scan stays in the head area and stops at the first terminator, so a
    ``---`` horizontal rule further down the prose is never mistaken for a block
    boundary. Three shapes end a block: ``------`` (a stacking seam — another
    block follows), a bare ``---`` (a healthy close), and ``---<text>`` (the
    fused close the old serializer produced, where the body starts mid-line).
    """
    if not text.startswith("---\n"):
        return None
    lines = text.split("\n")
    blocks: list[list[str]] = []
    current: list[str] = []
    for i, line in enumerate(lines[1:], start=1):
        if line == _SEAM:
            blocks.append(current)
            current = []
        elif line == "---":
            blocks.append(current)
            return blocks, "\n".join(lines[i + 1 :]), False
        elif line.startswith("---"):
            blocks.append(current)
            return blocks, "\n".join([line[3:]] + lines[i + 1 :]), True
        else:
            current.append(line)
    return None  # unterminated: not a shape we know how to repair


def repair_journal(text: str) -> tuple[str, dict] | None:
    """Merge stacked frontmatter blocks back into one. None = nothing to repair.

    Blocks appear newest-first (each damaged write *prepended* one), so the
    newest generally wins. The exceptions carry information the newest block
    lost: ``last_head`` falls back to the newest block that still has one (its
    absence is what made every checkpoint re-run as a "first"), ``event_date``
    comes from the oldest block (when the session actually started),
    ``related_tasks`` is the union oldest→newest, and ``status`` stays sticky —
    a session closed in any block stays closed. ``checkpoints`` is recounted
    from the ``### HH:MM:SS`` headings actually on disk rather than trusted,
    since the stacked values are all the restarted counter.

    Keys the newest block dropped are *not* resurrected (they are reported as
    ``dropped``): ``ref``, for instance, legitimately goes away when the caller
    stops passing ``--ref``.

    A journal with a *single* healthy block runs the same way, because stacking
    is not the only way a counter goes stale: three journals froze at
    ``checkpoints: 3`` while the appends kept adding headings, and returning
    early on "one block, not fused" reported them as healthy forever — they
    never reached the recount below. Such a file is only rewritten when the
    recount actually disagrees with what it claims; when the two match it is
    healthy and None comes back as before, so repair stays idempotent.
    """
    split = _split_stacked_frontmatter(text)
    if split is None:
        return None
    blocks, body, fused = split
    single_block = len(blocks) == 1 and not fused

    parsed = [
        parse_frontmatter("---\n" + "\n".join(block) + "\n---\n\n")[0] for block in blocks
    ]
    parsed = [fm for fm in parsed if fm]
    if not parsed:
        return None

    newest, oldest = parsed[0], parsed[-1]
    merged = dict(newest)

    if not merged.get("last_head"):
        for fm in parsed:
            if fm.get("last_head"):
                merged["last_head"] = fm["last_head"]
                break
    if oldest.get("event_date"):
        merged["event_date"] = oldest["event_date"]
    if any(fm.get("status") == "done" for fm in parsed):
        merged["status"] = "done"

    tasks: list[str] = []
    for fm in reversed(parsed):
        for task in fm.get("related_tasks") or []:
            if task not in tasks:
                tasks.append(task)
    if tasks:
        merged["related_tasks"] = tasks

    headings = len(_CHECKPOINT_HEADING.findall(body))
    claimed = sum(_int_or_zero(fm.get("checkpoints")) for fm in parsed)
    merged["checkpoints"] = headings or claimed

    # One block and a count that already agrees: nothing to fix. Compare the
    # *resolved* count rather than the raw heading total so the ``or claimed``
    # fallback above cannot report a change it did not make (a journal with no
    # headings at all keeps its declared count, which is not a repair).
    # Stacked journals are excluded: the 18-block fixture's restarted counters
    # happen to sum to its heading count, and skipping those would leave the
    # damage this function exists to undo in place.
    if single_block and merged["checkpoints"] == claimed:
        return None

    # ``updated`` stalled with the counter, so re-derive it from the last
    # heading on disk. ``last_head`` cannot be recovered the same way — the body
    # only ever holds the *short* sha, and it is the base of the next git delta,
    # so a truncated value would silently break the delta. It is left as is.
    times = _HEADING_TIME.findall(body)
    if times:
        derived = _derive_updated(merged.get("event_date"), times[-1], merged.get("updated"))
        if derived:
            merged["updated"] = derived

    dropped = sorted({k for fm in parsed for k in fm} - set(merged))
    stats = {
        "blocks": len(parsed),
        "checkpoints": merged["checkpoints"],
        "claimed": claimed,
        "last_head": merged.get("last_head"),
        "dropped": dropped,
    }
    return _serialize_frontmatter(merged) + "\n" + body.lstrip("\n"), stats


# ---------------------------------------------------------------------------
# stale-session sweep
# ---------------------------------------------------------------------------

# A journal stays "active" only while someone is still checking in. The live
# vault's 29 active journals (2026-09-27) cluster at 0-2 elapsed days (3
# journals) and then jump straight to 6-7 days (4 journals) — nothing lands on
# 3, 4, or 5. STALE_AFTER_DAYS=4 sits in that gap: it is the one threshold the
# actual data draws for free, with no journal sitting on the boundary. See
# run.log for the full elapsed-day table.
STALE_AFTER_DAYS = 4


def find_stale_journals(now: dt.datetime | None = None) -> list[tuple[Path, dict, str]]:
    """Active journals whose last ``updated`` is more than ``STALE_AFTER_DAYS`` old.

    Only ``status: active`` journals are considered — a ``done`` session is a
    human's deliberate close and is never revisited here. A missing or
    unparsable ``updated`` is skipped rather than treated as infinitely stale,
    since guessing would risk silently closing a live session.
    """
    now = now or _now_local()
    if not SESSIONS_DIR.exists():
        return []
    hits: list[tuple[Path, dict, str]] = []
    for path in sorted(SESSIONS_DIR.glob("*.md")):
        fm, body = parse_frontmatter(path.read_text(encoding="utf-8"))
        if fm.get("status") != "active":
            continue
        try:
            updated = dt.datetime.fromisoformat(str(fm.get("updated")))
        except (TypeError, ValueError):
            continue
        if updated.tzinfo is None:
            continue
        if now - updated > dt.timedelta(days=STALE_AFTER_DAYS):
            hits.append((path, fm, body))
    return hits


def sweep_stale_journals(now: dt.datetime | None = None, *, apply: bool) -> list[Path]:
    """Mark stale active journals ``status: stale``. Dry-run unless ``apply``."""
    now = now or _now_local()
    hits = find_stale_journals(now)
    for path, fm, body in hits:
        if apply:
            fm["status"] = "stale"
            path.write_text(_serialize_frontmatter(fm) + body, encoding="utf-8")
    return [path for path, _, _ in hits]


# ---------------------------------------------------------------------------
# checkpoint rendering
# ---------------------------------------------------------------------------


def _render_checkpoint(ts: dt.datetime, note: str | None, git: dict | None) -> str:
    lines = [f"### {ts:%H:%M:%S %Z}"]
    if note:
        lines += ["", note.strip()]
    if git is not None:
        lines.append("")
        head_line = f"**Git** ({git['branch']} @ `{git['short']}`) — "
        if git["first"]:
            head_line += f"session start; recent commits ({len(git['commits'])}):"
        elif not git["moved"]:
            head_line += "no new commits since last checkpoint."
        else:
            head_line += f"{len(git['commits'])} new commit(s) since last checkpoint:"
        lines.append(head_line)
        for c in git["commits"]:
            lines.append(f"- `{c.split(' ', 1)[0]}` {c.split(' ', 1)[1] if ' ' in c else ''}".rstrip())
        lines.append(f"_(working tree: {'dirty' if git['dirty'] else 'clean'})_")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def _session_path(project: str, session: str, host: str) -> Path:
    return SESSIONS_DIR / f"{session}-{_slugify(project)}.{host}.md"


def _session_path_with_id(project: str, session_id: str, date: str, host: str) -> Path:
    return SESSIONS_DIR / f"{date}-{_file_session_id(session_id)}-{_slugify(project)}.{host}.md"


def _file_session_id(session_id: str) -> str:
    # The host suffix is split off at the last dot, so none may precede it.
    return session_id.replace(".", "-")


def _find_session_journal(project: str, session_id: str, host: str) -> Path | None:
    if not SESSIONS_DIR.exists():
        return None
    matches = sorted(
        SESSIONS_DIR.glob(
            f"*-{_file_session_id(session_id)}-{_slugify(project)}.{host}.md"
        ),
        reverse=True,
    )
    return matches[0] if matches else None


def cmd_log(args: argparse.Namespace) -> int:
    now = _now_local()
    explicit_session = args.session is not None
    session = args.session or now.date().isoformat()
    project = args.project
    host = _host_id()
    # Only this host's file is ever appended to. Unsuffixed journals from before
    # per-host naming are legacy and read-only: appending to one from two hosts
    # would just trade the add/add conflict for a modify/modify one.
    if explicit_session:
        # A session id that names an existing default-session journal of this
        # host (`{session}-{project}.{host}.md`, i.e. a date) keeps appending to
        # it rather than splitting into a second file.
        dated_path = _session_path(project, session, host)
        path = (
            dated_path
            if dated_path.exists()
            else _find_session_journal(project, session, host)
            or _session_path_with_id(project, session, now.date().isoformat(), host)
        )
    else:
        path = _session_path(project, session, host)

    repo_dir = Path(args.repo_dir).expanduser().resolve() if args.repo_dir else None
    if not args.note and repo_dir is None:
        print("error: provide --note and/or --repo-dir (nothing to record)")
        return 2

    # Load existing journal to pick up last_head and append.
    fm: dict = {}
    body = ""
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(raw)
        # A journal that opens with a delimiter but parses to nothing is the
        # stacked-block damage older versions of this module left behind.
        # Appending would prepend yet another block and restart the counter, so
        # refuse and point at `repair` instead of writing.
        if not fm and raw.startswith("---\n"):
            print(
                f"error: {path.name} has unreadable frontmatter (stacked blocks?); "
                "run: python3 -m todo_system.session_log repair --apply"
            )
            return 2

    git = None
    if repo_dir is not None:
        git = capture_git(repo_dir, args.ref, fm.get("last_head") or None)
        if git is None:
            print(f"warning: --repo-dir {repo_dir} is not a git repo; recording note only")

    # Assemble / refresh frontmatter.
    tasks = list(fm.get("related_tasks") or [])
    for slug in args.task or []:
        link = slug if slug.startswith("[[") else f"[[Todo/items/{slug}]]"
        if link not in tasks:
            tasks.append(link)

    fm.update(
        {
            "tags": ["memory", "session"],
            "project": project,
            "session": session,
            "host": host,
            "event_date": fm.get("event_date", now.date().isoformat()),
            "recorded_date": now.date().isoformat(),
            "updated": now.isoformat(timespec="seconds"),
            "status": args.status or fm.get("status", "active"),
            "checkpoints": int(fm.get("checkpoints", 0) or 0) + 1,
        }
    )
    if repo_dir is not None:
        fm["repo"] = str(repo_dir)
        if args.ref != "HEAD":
            fm["ref"] = args.ref
    if git is not None:
        fm["last_head"] = git["head"]
    if tasks:
        fm["related_tasks"] = tasks

    # Build the file. New journals get a title + intro + Checkpoints header.
    checkpoint = _render_checkpoint(now, args.note, git)
    if not path.exists():
        title = f"# Manual session — {project} ({session})"
        intro = (
            "> 사람/외부 에이전트가 second-brain 워커 자동화 **밖에서** 수동 작업한 기록. "
            "`todo_system.session_log`가 주기적으로 체크포인트를 append한다."
        )
        # One "\n" only: _serialize_frontmatter already ends with the newline
        # that closes the delimiter line, and this supplies the blank line
        # separating frontmatter from the title.
        new_body = f"\n{title}\n\n{intro}\n\n## Checkpoints\n\n{checkpoint}"
    else:
        # Ensure a Checkpoints section exists, then append.
        if "## Checkpoints" not in body:
            body = body.rstrip() + "\n\n## Checkpoints\n"
        new_body = body.rstrip() + "\n\n" + checkpoint

    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(_serialize_frontmatter(fm) + new_body, encoding="utf-8")

    rel = path.relative_to(ROOT)
    print(f"session-log: checkpoint #{fm['checkpoints']} → {rel}")
    if git is not None and git["moved"] and git["commits"]:
        print(f"session-log: captured {len(git['commits'])} commit(s) from {git['branch']}")

    if args.commit:
        _commit_and_maybe_push(path, project, push=args.push)
    return 0


def _commit_and_maybe_push(path: Path, project: str, push: bool) -> None:
    """Opt-in convenience for external agents working from a vault clone: stage
    just the session journals and commit (optionally push). Kept off by default
    so the recorder itself performs no external writes unless asked."""
    rel_dir = "Memory/sessions"
    add = _git(ROOT, "add", rel_dir)
    if add is None:
        print("session-log: git add failed (is the vault a git repo?)")
        return
    # Nothing staged? (e.g. sessions dir gitignored) — bail quietly.
    staged = _git(ROOT, "diff", "--cached", "--name-only")
    if not staged:
        print("session-log: nothing staged to commit")
        return
    msg = f"chore(session): {project} manual-work checkpoint"
    if _git(ROOT, "commit", "-m", msg) is None:
        print("session-log: git commit failed")
        return
    print(f"session-log: committed ({msg})")
    if push:
        if _git(ROOT, "push") is None:
            print("session-log: git push failed (check remote/credentials)")
        else:
            print("session-log: pushed")


def cmd_list(args: argparse.Namespace) -> int:
    if not SESSIONS_DIR.exists():
        print("(no sessions recorded yet)")
        return 0
    files = sorted(SESSIONS_DIR.glob("*.md"), reverse=True)[: args.limit]
    if not files:
        print("(no sessions recorded yet)")
        return 0
    for path in files:
        fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        status = fm.get("status", "?")
        ckpts = fm.get("checkpoints", "?")
        updated = fm.get("updated", "?")
        host = fm.get("host") or _split_journal_name(path)[1] or "-"
        print(
            f"- {path.name:<60} status={status:<7} checkpoints={ckpts:<3} "
            f"updated={updated} host={host}"
        )
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    """Unstack frontmatter blocks in damaged journals. Dry-run unless --apply."""
    if args.paths:
        paths = [Path(p).expanduser() for p in args.paths]
    else:
        paths = sorted(SESSIONS_DIR.glob("*.md")) if SESSIONS_DIR.exists() else []
    if not paths:
        print("(no session journals found)")
        return 0

    missing = [p for p in paths if not p.exists()]
    if missing:
        for path in missing:
            print(f"error: {path} does not exist")
        return 2

    damaged = 0
    for path in paths:
        text = path.read_text(encoding="utf-8")
        result = repair_journal(text)
        if result is None:
            continue
        damaged += 1
        new_text, stats = result
        head = stats["last_head"]
        # "1 blocks → 1" would be a false signal: a single-block journal is here
        # because its counter went stale, not because anything was unstacked.
        if stats["blocks"] == 1:
            detail = f"checkpoints {stats['claimed']} → {stats['checkpoints']}"
        else:
            detail = (
                f"{stats['blocks']} blocks → 1, checkpoints {stats['checkpoints']}, "
                f"last_head {head[:9] if head else 'none'}"
            )
        if stats["dropped"]:
            detail += f", dropped {', '.join(stats['dropped'])}"
        if stats["claimed"] != stats["checkpoints"]:
            print(
                f"warning: {path.name} block counters sum to {stats['claimed']} but "
                f"{stats['checkpoints']} checkpoint headings are on disk; trusting the headings"
            )
        print(f"{'repaired' if args.apply else 'would repair'}: {path.name} ({detail})")
        if args.apply:
            path.write_text(new_text, encoding="utf-8")

    if not damaged:
        print("nothing to repair")
    elif not args.apply:
        print(f"({damaged} file(s) would change; re-run with --apply to write)")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    """Mark long-quiet active journals stale. Dry-run unless --apply."""
    now = _now_local()
    hits = find_stale_journals(now)
    if not hits:
        print("nothing stale")
        return 0
    for path, fm, _ in hits:
        elapsed_days = (now - dt.datetime.fromisoformat(str(fm["updated"]))).days
        verb = "closed" if args.apply else "would close"
        print(f"{verb}: {path.name} (elapsed {elapsed_days}d, last updated {fm['updated']})")
    if args.apply:
        sweep_stale_journals(now, apply=True)
    else:
        print(f"({len(hits)} file(s) would change; re-run with --apply to write)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    if not SESSIONS_DIR.exists():
        print("(no sessions recorded yet)")
        return 1
    suffix = f"-{_slugify(args.project)}"
    # Journal key = the filename minus any host suffix, so one day's (or one
    # session's) files from every host share a key. A legacy unsuffixed name
    # whose session id held a dot splits wrongly, so fall back to the stem.
    keyed: list[tuple[str, Path]] = []
    for path in SESSIONS_DIR.glob("*.md"):
        base = _split_journal_name(path)[0]
        stem = path.name.removesuffix(".md")
        if base.endswith(suffix):
            keyed.append((base, path))
        elif stem.endswith(suffix):
            keyed.append((stem, path))
    if not keyed:
        print(f"(no session journal for project '{args.project}')")
        return 1
    latest = max(key for key, _ in keyed)
    group = sorted(path for key, path in keyed if key == latest)
    if len(group) == 1:
        print(group[0].read_text(encoding="utf-8"))
        return 0
    for path in group:
        print(f"==> {path.name} <==")
        print(path.read_text(encoding="utf-8"))
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m todo_system.session_log",
        description="Record manual work sessions (for work done outside the worker automation).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_log = sub.add_parser(
        "log",
        help="Append a checkpoint to a project's session journal "
        "(one file per host: <date>-<slug>.<host>.md)",
    )
    p_log.add_argument("--project", required=True, help="Project slug the session is about")
    p_log.add_argument("--note", help="Freeform note describing what was done since the last checkpoint")
    p_log.add_argument("--repo-dir", help="Path to the project repo; auto-captures git HEAD/branch/new-commits")
    p_log.add_argument("--ref", default="HEAD", help="Git ref to record (default: HEAD; e.g. origin/main)")
    p_log.add_argument("--task", action="append", help="Related Todo slug to wikilink (repeatable)")
    p_log.add_argument("--session", help="Session id to group under (default: today's date)")
    p_log.add_argument("--status", choices=["active", "done"], help="Set the session status")
    p_log.add_argument("--commit", action="store_true", help="git-commit the session journal (Memory/sessions/ only)")
    p_log.add_argument("--push", action="store_true", help="git push after --commit (requires --commit)")
    p_log.set_defaults(func=cmd_log)

    p_list = sub.add_parser("list", help="List recent session journals")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.set_defaults(func=cmd_list)

    p_repair = sub.add_parser(
        "repair",
        help="Merge stacked frontmatter blocks left by older versions (dry-run by default)",
    )
    p_repair.add_argument(
        "paths", nargs="*", help="Journals to repair (default: every Memory/sessions/*.md)"
    )
    p_repair.add_argument(
        "--apply", action="store_true", help="Actually write the repaired files"
    )
    p_repair.add_argument(
        "--dry-run", action="store_true", help="Report only (the default; accepted for explicitness)"
    )
    p_repair.set_defaults(func=cmd_repair)

    p_sweep = sub.add_parser(
        "sweep",
        help="Mark long-quiet active journals stale (dry-run by default)",
    )
    p_sweep.add_argument("--apply", action="store_true", help="Actually write the swept files")
    p_sweep.set_defaults(func=cmd_sweep)

    p_show = sub.add_parser(
        "show", help="Print the most recent journal(s) for a project — every host's"
    )
    p_show.add_argument("project")
    p_show.set_defaults(func=cmd_show)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "push", False) and not getattr(args, "commit", False):
        print("error: --push requires --commit")
        return 2
    if getattr(args, "apply", False) and getattr(args, "dry_run", False):
        print("error: --apply and --dry-run are mutually exclusive")
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
