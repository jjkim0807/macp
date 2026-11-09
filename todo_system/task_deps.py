"""Task-level dependencies — a blocked task auto-activates once every task it
declares in `depends_on` is done. The task-level companion to the project-level
graph in `macp/deps.py` (GitHub issue #21).

Todo-item frontmatter fields:
    depends_on: [slug, ...]          # upstream tasks that must be done first
    on_unblock: auto|delegate|quick|discover|review|manual   # mode to flip to (default: auto)
    auto_activate: false             # opt OUT of auto-flip (default: true)
    human_gate: true                 # a person must look before this runs, even
                                     # with every dep done — never auto-flipped

Two spellings of "blocked" exist in the vault and both are honoured:
    worker_mode: blocked   → always eligible; the flip rewrites worker_mode.
    status: blocked        → eligible only when `on_unblock:` is written out; the
                             flip rewrites status back to `open` *and* worker_mode.
Rewriting both lines is what makes the flip stick — leave status at `blocked` and
the task is re-reported on every pass forever.

Design note: matches the vault philosophy that the human decides *project* stage
transitions (create_decision_gates is deliberately human-only). This only chains
**pure code tasks** the author explicitly wired with `depends_on`; anything
operational stays safe with `auto_activate: false`, or by leaving `on_unblock`
off a status-blocked task, or `worker_mode: manual` on one that is merely open.
Todo/items/*.md is the source of truth, so we flip the md and let import-md sync.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import sqlite3
from pathlib import Path

from . import not_before as _nb
from .cli import DB_PATH, ITEMS_DIR, parse_frontmatter

# `review` = run nothing until a human looks (not in DISPATCHABLE_MODES). It was
# missing here, and an unknown value fell through to "auto" — so
# `on_unblock: review` on play-store-7 (2026-09-11) auto-dispatched the one
# leaf its author had gated on a person. Unknown values now fall to `manual`:
# the safe direction for a field whose whole job is to say how much autonomy.
VALID_MODES = ("auto", "delegate", "quick", "discover", "review", "manual")
UNKNOWN_MODE_FALLBACK = "manual"
# Every worker_mode the orchestrator dispatches on its own. Kept byte-identical to
# `scripts/macp_dashboard.py:AUTO_MODES` and `orchestrator.tasks.DISPATCHABLE_MODES`
# — a mode in this list is a machine's job, so its owner is never pinged.
AUTO_WORKER_MODES = ("auto", "delegate", "quick", "discover")
# Modes that mean "a person does the work by hand". `review` runs nothing until a
# human looks (deliberately absent from DISPATCHABLE_MODES), so it belongs here.
HUMAN_WORK_MODES = ("manual", "review")
DECISION_TAG = "decision"
_WM_RE = re.compile(r"^worker_mode:.*$", re.M)
# count=1 + a column-0 anchor is enough to stay inside the frontmatter: it sits at
# the top of the file, and a Progress Log entry is written as "- YYYY-MM-DD: ...".
_ST_RE = re.compile(r"^status:.*$", re.M)


def _clean(x: str) -> str:
    # strip quotes, then wikilink brackets (`[[slug]]`) and an optional `|alias`
    # suffix (`[[slug|alias]]`) — depends_on is written in the same `[[slug]]`
    # convention as `parent:`, and must resolve to the bare slug to match done().
    s = str(x).strip().strip('"').strip("'").strip()
    s = s.strip("[]").strip()
    if "|" in s:
        s = s.split("|", 1)[0].strip()
    return s


def _as_list(v) -> list[str]:
    """Accept a YAML block list (already a list), or an inline `[a, b]` / `a, b`
    string (cli.parse_frontmatter returns inline lists as a raw string)."""
    if isinstance(v, list):
        return [_clean(x) for x in v if _clean(x)]
    if isinstance(v, str) and v.strip():
        return [_clean(x) for x in v.strip().strip("[]").split(",") if _clean(x)]
    return []


def _as_bool(v, default: bool = True) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() not in ("false", "no", "0", "off")


def _fm_str(fm: dict, key: str) -> str:
    """A scalar frontmatter value with any inline `# comment` stripped.

    The frontmatter parser does not strip them (CLAUDE.md §3 warns authors), so a
    `status: open  # open | done` would otherwise read as the whole comment.
    """
    return str(fm.get(key) or "").split("#")[0].strip()


def scan_items(items_dir: Path | str = ITEMS_DIR) -> dict[str, dict]:
    """{slug: {status, worker_mode, depends_on, on_unblock, auto_activate,
    human_gate, path, tags, needs_decision, is_parent, not_before, next_action,
    if_then}}.

    The last six are ADDITIVE (2026-11-09, macp-human-action-ready-alerts): they
    feed `human_deps_ready`'s "is this a person's job, and is it actionable yet"
    test. No pre-existing key changed name or meaning, so `apply_unblocks` and
    `task_export` read exactly what they did before.
    """
    tasks: dict[str, dict] = {}
    d = Path(items_dir)
    if not d.exists():
        return tasks
    for p in d.glob("*.md"):
        try:
            fm, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        # strip any inline "# comment" — the frontmatter parser doesn't, and a stray
        # comment must not silently downgrade the safety-critical on_unblock mode.
        # An absent/blank field parses to "" (a bare `on_unblock:` yields []), which
        # keeps `on_unblock_explicit` false — that flag is what admits a status-blocked
        # task, so it must mean "the author wrote a value", not "we defaulted one".
        raw_unblock = str(fm.get("on_unblock") or "").split("#")[0].strip().lower()
        # Same inline-comment strip as on_unblock (the frontmatter parser leaves
        # `# ...` on the value): `human_gate: true  # 사람 확인` must read True, not
        # downgrade to False. Default False — only a task that WRITES the flag is
        # protected, so no existing blocked+deps-done task changes behaviour.
        raw_gate = str(fm.get("human_gate") or "").split("#")[0].strip()
        raw_decision = _fm_str(fm, "needs_decision")
        raw_parent = _fm_str(fm, "is_parent")
        tasks[p.stem] = {
            "path": p,
            "status": str(fm.get("status", "")).strip().lower(),
            "worker_mode": str(fm.get("worker_mode", "")).strip().lower(),
            "depends_on": _as_list(fm.get("depends_on")),
            "on_unblock": (raw_unblock if raw_unblock in VALID_MODES
                           else ("auto" if not raw_unblock else UNKNOWN_MODE_FALLBACK)),
            "on_unblock_explicit": bool(raw_unblock),
            "auto_activate": _as_bool(fm.get("auto_activate"), True),
            "human_gate": _as_bool(raw_gate, False) if raw_gate else False,
            # --- additive, for human_deps_ready -----------------------------
            "tags": [t.strip().lower() for t in _as_list(fm.get("tags")) if t.strip()],
            "needs_decision": _as_bool(raw_decision, False) if raw_decision else False,
            "is_parent": _as_bool(raw_parent, False) if raw_parent else False,
            "not_before": _fm_str(fm, "not_before"),
            # Prose, NOT comment-stripped: `#` is legitimate inside them ("PR #390
            # 리뷰"), and _fm_str would cut the line at it. Only the structured
            # fields above can carry a trailing `# comment` worth removing.
            "next_action": str(fm.get("next_action") or "").strip(),
            "if_then": str(fm.get("if_then") or "").strip(),
        }
    return tasks


def _is_blocked(t: dict) -> bool:
    """Whether a task is dependency-blocked. Two shapes exist in the vault:

    - `worker_mode: blocked` — always eligible (the mode says nothing but "waiting").
    - `status: blocked` — eligible only with an explicit `on_unblock:`. Such a task
      keeps a real worker_mode of its own, so without that field we cannot tell
      "waiting on a dep" from "operational, parked by a human"; requiring it keeps
      the opt-in deliberate instead of flipping parked work to the `auto` default.
    """
    if t["worker_mode"] == "blocked":
        return True
    return t["status"] == "blocked" and t.get("on_unblock_explicit", False)


def _in_cycle(slug: str, tasks: dict[str, dict]) -> bool:
    """True if `slug` participates in a depends_on cycle (DFS with a path stack)."""
    path: set[str] = set()

    def visit(s: str) -> bool:
        if s in path:
            return True
        path.add(s)
        for up in tasks.get(s, {}).get("depends_on", []):
            if visit(up):
                return True
        path.discard(s)
        return False

    return visit(slug)


def _db_status(db: Path | str) -> dict[str, str]:
    m: dict[str, str] = {}
    try:
        con = sqlite3.connect(f"file:{Path(db)}?mode=ro", uri=True)
        for slug, status in con.execute("SELECT slug, status FROM tasks"):
            m[slug] = (status or "").lower()
        con.close()
    except Exception:
        pass
    return m


def _done_test(tasks: dict[str, dict], dbstat: dict[str, str]):
    """The ONE "is this dependency satisfied" predicate.

    A dependency counts as done if its md OR the db says so (a done+archived
    upstream still unblocks). Extracted so `resolve_unblocks` (which flips a
    blocked task to a worker mode) and `human_deps_ready` (which pings a person)
    can never drift apart: a dep that unblocks a worker must be the same dep that
    makes a human's turn arrive, or the two views of the same chain disagree.
    """
    def done(dep: str) -> bool:
        return tasks.get(dep, {}).get("status") == "done" or dbstat.get(dep) == "done"

    return done


def _known(tasks: dict[str, dict], dbstat: dict[str, str], dep: str) -> bool:
    """Whether `dep` names a task that exists at all (md or db)."""
    return dep in tasks or dep in dbstat


def resolve_unblocks(tasks: dict[str, dict], db: Path | str = DB_PATH
                     ) -> tuple[list[tuple[str, str]], list[str]]:
    """(flips, cyclic) — flips = [(slug, new_mode)] for blocked tasks whose every
    dependency is done and that aren't opted out; cyclic = slugs skipped for cycles.
    A dependency counts as done if its md OR the db says so (a done+archived upstream
    still unblocks)."""
    dbstat = _db_status(db)
    done = _done_test(tasks, dbstat)

    flips, cyclic = [], []
    for slug, t in tasks.items():
        if not _is_blocked(t) or not t["depends_on"]:
            continue
        if not t["auto_activate"]:                 # explicit opt-out
            continue
        if t.get("human_gate"):
            # A person must look before this runs — even with every dep done. The
            # deps-done auto-flip is exactly what the 2026-10-12 incident rode: a
            # delegate PR closed `done` out from under a reviewer let task_deps
            # auto-activate a leaf its author had gated on a human. Scoped to the
            # flip decision only (not _is_blocked / dispatch eligibility), so the
            # task stays visible as blocked and a human still unblocks it by hand.
            continue
        if _in_cycle(slug, tasks):
            cyclic.append(slug)
            continue
        if all(done(d) for d in t["depends_on"]):
            flips.append((slug, t["on_unblock"]))
    return flips, cyclic


def _has_decision_marker(t: dict) -> bool:
    """An EXPLICIT "a person must decide this" marker — the `decision` tag or
    `needs_decision: true`. Same contract as the dashboard's `_is_decision`
    (docs/dashboard-parity.md "human why"): markers only, never title prose."""
    return DECISION_TAG in (t.get("tags") or []) or bool(t.get("needs_decision"))


def is_human_task(t: dict, *, human_owned: bool = False) -> bool:
    """Whether finishing this task is a PERSON's job (so its owner is worth paging).

    The truth table (pinned by HumanDepsReadyTest):

    | worker_mode                        | extra condition            | human? |
    |------------------------------------|----------------------------|--------|
    | auto / delegate / quick / discover | —                          | ✗      |
    | blocked                            | on_unblock is auto-ish AND |        |
    |                                    | no decision marker         | ✗      |
    | blocked                            | on_unblock manual/review,  |        |
    |                                    | or a decision marker       | ✓      |
    | manual / review                    | —                          | ✓      |
    | unset / unknown                    | decision marker or a       |        |
    |                                    | registry human owner       | ✓      |
    | unset / unknown                    | neither                    | ✗      |

    The last row is the one that keeps this quiet: a task with no `worker_mode`
    and no owner is unrouted backlog, and paging someone the moment its deps
    close would turn every finished chain into noise. An auto-ish `blocked` task
    is excluded for the same reason — `resolve_unblocks` is about to hand it to a
    worker, so nobody needs to act.
    """
    mode = (t.get("worker_mode") or "").strip().lower()
    if mode in AUTO_WORKER_MODES:
        return False
    if mode in HUMAN_WORK_MODES:
        return True
    if mode == "blocked":
        # Where it lands once the deps close decides whose job it is.
        return t.get("on_unblock") in HUMAN_WORK_MODES or _has_decision_marker(t)
    return _has_decision_marker(t) or bool(human_owned)


def _fingerprint(deps: list[str]) -> str:
    """A stable short hash of the dependency SET (order-independent).

    This, not a timestamp, is what makes the once-only alert re-arm: storing only
    "we pinged at T" cannot tell "already handled" from "a dep was reopened and
    closed again". Hashing the sorted slugs means adding or removing a dep is a
    new fingerprint, hence a new alert.
    """
    return hashlib.sha1("|".join(sorted(deps)).encode("utf-8")).hexdigest()[:12]


def human_deps_ready(tasks: dict[str, dict], db: Path | str = DB_PATH, *,
                     is_human_owned=None, dep_done_at=None, now=None
                     ) -> tuple[list[dict], list[str]]:
    """(rows, warnings) — open tasks that are a PERSON's job and whose every
    `depends_on` is now done. "It is your turn" in pure-function form.

    Nothing here reads the clock, the ledger or the network: `is_human_owned(slug)
    -> bool` and `dep_done_at(dep) -> str | None` are injected (the orchestrator
    passes registry- and ledger-backed versions; tests pass dicts), so the whole
    decision is reproducible from its arguments.

    Each row: `{slug, depends_on (sorted), last_dep, last_dep_at, fingerprint,
    deferred, next_action, if_then}`.

    - `last_dep` / `last_dep_at` — the dependency that closed LAST, per
      `dep_done_at`. With no resolver, or when it knows no timestamp, the
      alphabetically last slug stands in and `last_dep_at` is None (the caller
      treats an unknown time conservatively).
    - `deferred` — the task's `not_before` is still in the future. Such a row is
      RETURNED rather than dropped: the caller must not alert on it, but must
      also not forget it (dropping it would let the dedup state be pruned and the
      alert fire twice later). Respecting a human's deferral is the point.

    A dependency that names no known task makes the task not-ready and adds a
    warning — a typo'd slug can never be "done", and silently treating it as a
    satisfied dep would page someone for work still blocked in reality.

    Never raises: one bad task becomes a warning and is skipped.
    """
    dbstat = _db_status(db)
    done = _done_test(tasks, dbstat)
    rows: list[dict] = []
    warnings: list[str] = []
    for slug, t in sorted(tasks.items()):
        try:
            deps = t.get("depends_on") or []
            if t.get("status") != "open" or not deps or t.get("is_parent"):
                continue
            owned = False
            if is_human_owned is not None:
                try:
                    owned = bool(is_human_owned(slug))
                except Exception:
                    owned = False
            if not is_human_task(t, human_owned=owned):
                continue
            unknown = [d for d in deps if not _known(tasks, dbstat, d)]
            if unknown:
                warnings.append(f"{slug}: unknown dep {', '.join(repr(d) for d in unknown)}")
                continue
            if not all(done(d) for d in deps):
                continue
            if _in_cycle(slug, tasks):
                warnings.append(f"{slug}: dependency cycle — skipped")
                continue
            last_dep, last_at = sorted(deps)[-1], None
            if dep_done_at is not None:
                stamped = []
                for d in deps:
                    try:
                        at = dep_done_at(d)
                    except Exception:
                        at = None
                    if at:
                        stamped.append((str(at), d))
                if stamped:
                    last_at, last_dep = max(stamped)
            rows.append({
                "slug": slug,
                "depends_on": sorted(deps),
                "last_dep": last_dep,
                "last_dep_at": last_at,
                "fingerprint": _fingerprint(deps),
                "deferred": _nb.is_deferred(t.get("not_before"), now),
                "next_action": t.get("next_action") or "",
                "if_then": t.get("if_then") or "",
            })
        except Exception as e:  # noqa: BLE001 — one bad task must not kill the sweep
            warnings.append(f"{slug}: skipped ({type(e).__name__}: {e})")
    return rows, warnings


def apply_unblocks(items_dir: Path | str = ITEMS_DIR, db: Path | str = DB_PATH
                   ) -> tuple[list[tuple[str, str]], list[str]]:
    """Flip eligible blocked tasks in place (rewrite worker_mode + progress note).
    A status-blocked task also has its `status:` put back to `open`, without which
    the flip would not stick and the resolver would re-report it every cycle.
    Returns (flipped, cyclic-skipped). Caller runs import-md to sync the db."""
    tasks = scan_items(items_dir)
    flips, cyclic = resolve_unblocks(tasks, db)
    today = dt.date.today().isoformat()
    for slug, mode in flips:
        p = tasks[slug]["path"]
        s = p.read_text(encoding="utf-8")
        deps = ", ".join(tasks[slug]["depends_on"])
        was_status_blocked = tasks[slug]["status"] == "blocked"
        s2, n = _WM_RE.subn(f"worker_mode: {mode}", s, count=1)
        if was_status_blocked:
            # No worker_mode line to rewrite? Add one under status — leaving it out
            # reads as `off`, which would void the very flip we just decided on.
            repl = "status: open" if n else f"status: open\nworker_mode: {mode}"
            s2, n_status = _ST_RE.subn(repl, s2, count=1)
            n += n_status
        if n == 0:
            continue                               # neither line present — leave it
        flipped = "status open, worker_mode" if was_status_blocked else "worker_mode"
        note = f"\n- {today}: auto-unblocked (deps done: {deps}) → {flipped} {mode}"
        if "## Progress Log" in s2:
            s2 = s2.replace("## Progress Log\n", f"## Progress Log\n{note}\n", 1)
        else:
            s2 = s2.rstrip() + f"\n\n## Progress Log\n{note}\n"
        p.write_text(s2, encoding="utf-8")
    return flips, cyclic


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="task-deps",
                                 description="auto-activate blocked tasks whose deps are done")
    ap.add_argument("--dry-run", action="store_true", help="report only, don't flip")
    ap.add_argument("--items", default=str(ITEMS_DIR))
    ap.add_argument("--db", default=str(DB_PATH))
    a = ap.parse_args(argv)
    if a.dry_run:
        flips, cyclic = resolve_unblocks(scan_items(a.items), a.db)
    else:
        flips, cyclic = apply_unblocks(a.items, a.db)
    for slug, mode in flips:
        print(f"{'would unblock' if a.dry_run else 'unblocked'}: {slug} → worker_mode {mode}")
    for slug in cyclic:
        print(f"⚠ skipped (dependency cycle): {slug}")
    if not flips and not cyclic:
        print("no blocked tasks ready to activate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
