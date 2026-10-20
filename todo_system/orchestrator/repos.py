"""Per-task repo resolution for the general-purpose runner.

Legacy-worker parity: a task's markdown frontmatter may declare `repo` (a git
URL) and `working_dir`. If set, the task runs against that external repo (the
worker clones it); if absent, it is a vault task. The general runner needs the
same resolution so it can route ANY auto task to a catch-all agent and run the
session in the right repo.

Mac-side (PyYAML for frontmatter). Repo derivation is pure/tested; cloning is
gated (clone=False by default) so nothing is fetched unless a live runner asks.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

DEFAULT_WORKER_ROOT = Path.home() / "Worker"

# Mirrors mailbox.py's GIT_TIMEOUT_* split: a clone talks to a remote and can
# wedge on a stalled credential helper or dead network, so it gets the long
# ceiling; worktree prune/add never leave local disk.
GIT_TIMEOUT_CLONE_S = 300
GIT_TIMEOUT_WORKTREE_S = 180


def _frontmatter_block(text: str) -> str | None:
    """The raw YAML block between the leading `---` and the next `---`, or None
    when there is no closed frontmatter (no leading `---`, or no closing `---`).
    None means fail-open at every call site — the field is simply absent. The
    slice is byte-identical to the historical `_frontmatter` (`text[3:end]`,
    `end = text.find("\\n---", 3)`)."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    return text[3:end]


def _yaml_error_reason(e) -> str:
    """One-line reason from a `yaml.YAMLError`. When it carries a `problem_mark`
    the file line is reported 1-based: the block fed to the parser starts right
    after the leading `---` (file line 1), so its line 0 is the newline after
    `---` and its line N maps to file line N+1 — hence `mark.line + 1`. Newlines
    are collapsed so the reason stays a single line."""
    mark = getattr(e, "problem_mark", None)
    problem = getattr(e, "problem", None)
    if mark is not None:
        detail = problem or (" ".join(str(e).split()) or "YAML error")
        return f"line {mark.line + 1}: {detail}"
    return " ".join(str(e).split()) or "YAML error"


def _parse_frontmatter_block(block: str) -> tuple[dict, str | None]:
    """Parse a frontmatter YAML block into (dict, reason). `reason` is non-None
    ONLY when YAML raises — the one case this module treats as a hard parse
    error rather than fail-open. A parse that succeeds but is not a dict
    (string/list/int) stays ({}, None), preserving the historical fail-open."""
    import yaml

    try:
        parsed = yaml.safe_load(block)
    except yaml.YAMLError as e:
        return {}, _yaml_error_reason(e)
    return (parsed if isinstance(parsed, dict) else {}), None


def frontmatter_error_from_text(text: str) -> str | None:
    """A one-line reason iff this task markdown's frontmatter is strict-YAML
    broken (`yaml.YAMLError`), else None. Shared by `load_dispatchable`'s
    dispatch gate and `task_validate`'s ERROR so the two always agree on which
    files are broken. Missing/unclosed frontmatter and non-dict YAML are NOT
    errors here — those stay fail-open."""
    block = _frontmatter_block(text)
    if block is None:
        return None
    return _parse_frontmatter_block(block)[1]


def frontmatter_with_error(md_path: str | Path) -> tuple[dict, str | None]:
    """Like `_frontmatter` but also returns a reason string when the frontmatter
    is strict-YAML broken. (fm, None) for every fail-open case (missing file,
    no/unclosed frontmatter, non-dict YAML); ({}, reason) only on a YAMLError."""
    p = Path(md_path)
    if not p.exists():
        return {}, None
    block = _frontmatter_block(p.read_text(encoding="utf-8"))
    if block is None:
        return {}, None
    return _parse_frontmatter_block(block)


def _frontmatter(md_path: str | Path) -> dict:
    """A task markdown's frontmatter, or {} for anything unreadable. Every caller
    treats a parse failure the same way — the field is simply absent — so the
    failure modes collapse here instead of at each call site. A caller that must
    distinguish a YAML parse error from the fail-open cases uses
    `frontmatter_with_error` instead."""
    return frontmatter_with_error(md_path)[0]


def _clean(v) -> str | None:
    """Frontmatter string or None. '-' is the vault's written-out placeholder."""
    v = str(v).strip() if isinstance(v, str) else None
    return v if v and v != "-" else None


# Each `read_task_*` below re-reads and re-parses the file, which is right for a
# single lookup. A caller that needs several fields of the same task (the
# exporter reads five, load_dispatchable seven) parses once with `_frontmatter` and hands the dict to the
# `task_*_from_fm` cores, so the interpretation still lives in one place.


def task_repo_from_fm(fm: dict) -> tuple[str | None, str | None]:
    return _clean(fm.get("repo")), _clean(fm.get("working_dir"))


def read_task_repo(md_path: str | Path) -> tuple[str | None, str | None]:
    """Return (repo_url, working_dir) from a task markdown's frontmatter.
    A placeholder '-' or missing field yields None."""
    return task_repo_from_fm(_frontmatter(md_path))


def task_allowed_tools_from_fm(fm: dict) -> str | None:
    return _clean(fm.get("allowed_tools"))


def read_task_allowed_tools(md_path: str | Path) -> str | None:
    """A task's `allowed_tools:` — the implement stage's tool whitelist.

    Documented in Worker/README.md since v1 but wired to nothing in v2: the
    payload never carried it, so every task's list was decorative and a
    capability could only be granted to ALL tasks or none."""
    return task_allowed_tools_from_fm(_frontmatter(md_path))


def task_bypass_guard_from_fm(fm: dict) -> bool:
    v = fm.get("bypass_guard")
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def read_task_bypass_guard(md_path: str | Path) -> bool:
    """A task's `bypass_guard:` — run it even while a COST hold is in force
    (usage-quota pacing or the daily run budget). An explicit pause still wins;
    see Orchestrator.tick. Absent/false means normal pacing applies."""
    return task_bypass_guard_from_fm(_frontmatter(md_path))


def task_long_job_from_fm(fm: dict) -> str | None:
    return _clean(fm.get("long_job"))


def read_task_long_job(md_path: str | Path) -> str | None:
    """A task's `long_job:` — a command that has to finish before the pipeline
    can do anything useful, and that is too long to live inside a stage (the cap
    is 60 min; ai-stock-trading-8b's collection needed ~75). The runner starts it
    detached and defers the task until it exits — see orchestrator/jobs.py."""
    return task_long_job_from_fm(_frontmatter(md_path))


def task_model_tier_from_fm(fm: dict) -> str | None:
    v = _clean(fm.get("model_tier"))
    return v.lower() if v else None


def read_task_model_tier(md_path: str | Path) -> str | None:
    """A task's `model_tier:` — heavy | standard | light, the author's explicit
    call on which model ladder the pipeline runs (model_select.classify_tier
    consults it first). Lowercased; None when absent/placeholder. Any other
    value is passed through so the classifier can ignore it and the validator
    can name it."""
    return task_model_tier_from_fm(_frontmatter(md_path))


def task_agent_from_fm(fm: dict) -> str | None:
    v = _clean(fm.get("agent"))
    return v.lower() if v else None


def read_task_agent(md_path: str | Path) -> str | None:
    """A task's `agent:` — the vendor id the stages run as ('claude' default, or
    'upstage' for the Solar single-stage path; see orchestrator.solar_stage).
    Lowercased; None when absent/placeholder. Like `model_tier` this only RIDES
    the payload (tasks.load_dispatchable) — the runner decides what to do with
    it; a value the runner does not route simply runs the normal claude path."""
    return task_agent_from_fm(_frontmatter(md_path))


def task_estimate_p90_from_fm(fm: dict) -> str | None:
    return _clean(fm.get("estimate_p90"))


def read_task_estimate_p90(md_path: str | Path) -> str | None:
    """A task's `estimate_p90:` as written ("90m", "4h"). todo.db has the
    column too, but the loader reads the markdown so a freshly edited estimate
    reaches the tier decision before the sync daemon catches up."""
    return task_estimate_p90_from_fm(_frontmatter(md_path))


def _str_list(v) -> list[str]:
    """A frontmatter value as a list of non-empty strings. Only a real YAML list
    counts — a bare string yields [] on purpose: the dispatcher's overlap
    predicate (`orchestrator._as_glob_list`) reads a bare string as "nothing
    declared", and task_validate flags it as an ERROR, so the loader must not
    quietly promote it into a one-element list and make the two disagree."""
    if not isinstance(v, (list, tuple)):
        return []
    return [x.strip() for x in v if isinstance(x, str) and x.strip()]


def task_touches_from_fm(fm: dict) -> list[str]:
    return _str_list(fm.get("touches"))


def read_task_touches(md_path: str | Path) -> list[str]:
    """A task's `touches:` — the repo-relative path globs it will edit. Two tasks
    of the same repo may run at once only when BOTH declare disjoint lists
    (Orchestrator._can_run_concurrently). Until 2026-10-17 the loader never
    copied this field into the payload, so the predicate always saw [] and every
    same-repo pair was serialised regardless of what the markdown said."""
    return task_touches_from_fm(_frontmatter(md_path))


def task_depends_on_from_fm(fm: dict) -> list[str]:
    return _str_list(fm.get("depends_on"))


def read_task_depends_on(md_path: str | Path) -> list[str]:
    """A task's `depends_on:` slugs — the same predicate serialises a pair when
    either depends on the other, and it reads the edge off the payload."""
    return task_depends_on_from_fm(_frontmatter(md_path))


def task_worker_mode_from_fm(fm: dict) -> str | None:
    raw = fm.get("worker_mode")
    # A bare `worker_mode: off` (also no/false) is YAML 1.1 boolean False;
    # `_clean` would drop it as a non-string and the task would fail open.
    # `is False`, not `== False`: `worker_mode: 0` must not read as off.
    if raw is False:
        return "off"
    v = _clean(raw)
    return v.lower() if v else None


def read_task_worker_mode(md_path: str | Path) -> str | None:
    """A task's `worker_mode:` read from its markdown, lowercased — or None when
    the field is absent/blank/placeholder or the file is unreadable.

    load_dispatchable gates on todo.db's worker_mode, but the DB lags the markdown
    (the todo-sync daemon rebuilds it). In the window after a task is edited to a
    non-dispatchable mode (e.g. `blocked`) but before the DB catches up, the stale
    row still says `auto`; re-reading the source-of-truth markdown here lets the
    loader skip that drift. Returns None → the caller trusts the DB (fail-open);
    only a mode the md *explicitly* names drives a skip. Lowercased so a capitalized
    dispatchable value ("Auto") can't be misread as non-dispatchable. A bare
    `off` (YAML 1.1 boolean False) reads as 'off'; True and other non-strings
    stay None (fail-open)."""
    return task_worker_mode_from_fm(_frontmatter(md_path))


def repo_name(url: str) -> str:
    """Local checkout name from a git URL, mirroring `git clone` defaults:
    https://github.com/example-org/agent-commander.git -> agent-commander"""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".git") else name


def local_path(url: str, worker_root: str | Path = DEFAULT_WORKER_ROOT) -> Path:
    return Path(worker_root) / repo_name(url)


def ensure_local(url: str, worker_root: str | Path = DEFAULT_WORKER_ROOT,
                 clone: bool = False, log: list[str] | None = None) -> Path | None:
    """Resolve a git URL to a local checkout. Reuses an existing checkout under
    worker_root; clones only if clone=True. Returns None if absent and not
    cloned.

    When clone=True and the destination exists but is not a git checkout (no
    `.git`), the spot is recovered instead of silently failing the clone: an
    empty dir is cloned into in place, a non-empty one is moved aside to
    `<dest>-stale-<YYYYMMDDHHMMSS>` (never deleted — it may hold a human's work)
    and then re-cloned. Pass `log` (a list) to collect a one-line reason for each
    recovery/clone step and, on failure, the last line of git's stderr, so the
    caller can surface *why* in its RESULT note rather than a bare
    "could not prepare repo <url>"."""
    def _note(s: str) -> None:
        if log is not None:
            log.append(s)

    dest = local_path(url, worker_root)
    if (dest / ".git").exists():
        return dest
    if not clone:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        # dest exists but has no `.git` (checked above) — recover without
        # deleting, since the contents may be a human's leftover material.
        if any(dest.iterdir()):
            ts = datetime.now().strftime("%Y%m%d%H%M%S")
            stale = dest.parent / f"{dest.name}-stale-{ts}"
            n = 1
            while stale.exists():
                stale = dest.parent / f"{dest.name}-stale-{ts}-{n}"
                n += 1
            shutil.move(str(dest), str(stale))
            _note(f"non-git dir {dest} moved to {stale}; re-cloning")
        else:
            _note(f"dest {dest} is an empty non-git dir; cloning in place")
    try:
        r = subprocess.run(["git", "clone", url, str(dest)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=GIT_TIMEOUT_CLONE_S)
    except subprocess.TimeoutExpired:
        _note(f"clone timed out after {GIT_TIMEOUT_CLONE_S}s: {url}")
        return None
    if r.returncode == 0:
        _note(f"cloned {url} -> {dest}")
        return dest
    lines = (r.stderr or "").strip().splitlines()
    last = lines[-1].strip() if lines else ""
    _note(f"clone failed: {last}")
    return None


def worktree_path(url: str, slug: str,
                  worker_root: str | Path = DEFAULT_WORKER_ROOT) -> Path:
    """Where a per-run git worktree for `slug` would live, derived from the
    shared clone's location: `<worker_root>/<repo>.wt/<slug>`.

    Pure — touches no filesystem. `repo_name`/`local_path` are reused so the
    checkout name is derived in exactly one place. The `.wt/<slug>` shape (a
    sibling `*.wt` directory, basename == slug, no `wt-` prefix) is deliberate:
    the 5-minute maintenance sweep only reclaims `<worker_root>/wt-*`, so a path
    one directory deeper whose name does not start with `wt-` is out of its
    reach. Its own reaper is worktree_sweep.reclaim_parked_worktrees (same doctor
    tick), which removes a failed / parked run's worktree only after the
    retention window and never while a run, long_job or process holds it."""
    base = local_path(url, worker_root)
    return base.parent / f"{base.name}.wt" / slug


def ensure_worktree(url: str, slug: str, worker_root: str | Path = DEFAULT_WORKER_ROOT,
                    clone: bool = False, log: list[str] | None = None) -> Path | None:
    """Ensure a per-run git worktree for `slug` exists under the shared clone and
    return its path, or None if the shared clone is absent (and not cloned) or
    the add fails.

    Live path: `orchestrator/runner.py` calls this unconditionally on every run
    (per-run isolation switchover, `macp-parallel-runs-same-repo-worktrees-2-switchover`,
    is done). It first guarantees the shared clone via `ensure_local` (reused,
    not reimplemented); if that returns None, so does this.

    The worktree is added with `--detach` — no commit-ish, so it checks out the
    shared clone's current HEAD without a branch. The plain `worktree add <path>`
    form would instead create a branch named after the path's last segment
    (`<slug>`), colliding with the real `worker/<slug>` branch; a detached record
    is also skipped outright by the maintenance sweep. An already-present
    worktree (its `.git` exists) is returned as-is, mirroring `ensure_local`'s
    early return, so re-calling for the same slug is safe. A `worktree prune`
    runs first to clear admin records whose paths were removed out of band. A
    path that exists but is not a worktree is never deleted (removal belongs to
    the switchover step, and `rm` is a hard-rule no); the add simply fails and
    None is returned with git's reason appended to `log`."""
    def _note(s: str) -> None:
        if log is not None:
            log.append(s)

    base = ensure_local(url, worker_root, clone=clone, log=log)
    if base is None:
        return None
    wt = worktree_path(url, slug, worker_root)
    if (wt / ".git").exists():
        return wt
    wt.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["git", "-C", str(base), "worktree", "prune"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=GIT_TIMEOUT_WORKTREE_S)
    except subprocess.TimeoutExpired:
        _note(f"worktree prune timed out after {GIT_TIMEOUT_WORKTREE_S}s on {base}")
        return None
    try:
        r = subprocess.run(["git", "-C", str(base), "worktree", "add", "--detach", str(wt)],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=GIT_TIMEOUT_WORKTREE_S)
    except subprocess.TimeoutExpired:
        _note(f"worktree add timed out after {GIT_TIMEOUT_WORKTREE_S}s: {wt}")
        return None
    if r.returncode == 0:
        _note(f"worktree {wt} added (detached) from {base}")
        return wt
    lines = (r.stderr or "").strip().splitlines()
    last = lines[-1].strip() if lines else ""
    _note(f"worktree add failed: {last}")
    return None


def resolve(payload: dict, agent_repo: str | None, worker_root: str | Path = DEFAULT_WORKER_ROOT,
            clone: bool = False) -> Path | None:
    """Resolve the working repo for a task: task's own repo URL wins, else the
    agent's fixed repo, else None (vault tasks are handled by the caller)."""
    url = (payload or {}).get("repo")
    if url:
        return ensure_local(url, worker_root, clone=clone)
    if agent_repo:
        p = Path(os.path.expanduser(agent_repo))
        return p if (p / ".git").exists() else None
    return None
