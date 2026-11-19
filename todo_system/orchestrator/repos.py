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

from .. import paths
from .statuses import CLOSED_STATUSES as CLOSED_STATUSES  # the shared set; leaf, no cycle


def _worker_root(worker_root: str | Path | None) -> Path:
    """The clone root: an explicit argument, else :func:`paths.worker_root`.

    Resolved HERE, at call time, and never as a function default. A
    ``worker_root: Path = DEFAULT_WORKER_ROOT`` default — what every function
    below used to carry — is evaluated once at import, so ``MACP_WORKER_ROOT``
    (and the ``worker_root`` config key) could never take effect in a process
    that imported this module before the env was read. That is the exact trap
    this helper exists to close; keep the resolution inside the function body.
    """
    return Path(worker_root) if worker_root is not None else paths.worker_root()

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


def task_max_turns_from_fm(fm: dict) -> int | None:
    """A task's `max_turns:` as a positive int, else None. The legacy worker
    honoured it as a per-task turn budget (worker.py); the staged pipeline
    dropped it until 2026-11-17 (dripcoffee-dial-1 hit plan 40 / verify 50
    with `max_turns: 150` written). Garbage and non-positive values are None so
    the stage defaults apply."""
    v = fm.get("max_turns")
    if isinstance(v, bool):
        return None
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


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
    quietly promote it into a one-element list and make the two disagree.

    A YAML flow list (`touches: [a, b]`) already arrives as a list from
    `yaml.safe_load`. The only way a `"[a, b]"` string reaches here is a QUOTED
    value (`touches: "[a, b]"`), which YAML reads as a string — so [] is right,
    and it matches `frontmatter_lists.parse_inline_list`, which the hand-rolled
    parsers use and which also leaves a quoted `"[a, b]"` a string."""
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


def task_status_from_fm(fm: dict) -> str | None:
    """A task's `status:` lowercased, or None when absent/blank/placeholder/non-string."""
    v = _clean(fm.get("status"))
    return v.lower() if v else None


def task_closed_from_fm(fm: dict) -> str | None:
    """The closed status (done/cancelled/canceled) the md states, else None.

    Dispatch gates read todo.db, which lags the markdown, and an un-acked
    TASK_ASSIGN envelope bypasses dispatch entirely (2026-10-30: a 10-27 envelope
    ran a task a /focus session had closed on 10-28). The md is the source of
    truth, so the runner and the dispatcher re-check it. Fail-open: None unless
    the md explicitly says closed."""
    st = task_status_from_fm(fm)
    return st if st in CLOSED_STATUSES else None


def repo_name(url: str) -> str:
    """Local checkout name from a git URL, mirroring `git clone` defaults:
    https://github.com/example-org/agent-commander.git -> agent-commander"""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".git") else name


def local_path(url: str, worker_root: str | Path | None = None) -> Path:
    return _worker_root(worker_root) / repo_name(url)


def ensure_local(url: str, worker_root: str | Path | None = None,
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

    # Resolve once, then pass the resolved root down, so one call never reads
    # the config twice (and cannot see two different answers mid-call).
    dest = local_path(url, _worker_root(worker_root))
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
                  worker_root: str | Path | None = None) -> Path:
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
    base = local_path(url, _worker_root(worker_root))
    return base.parent / f"{base.name}.wt" / slug


def ensure_worktree(url: str, slug: str, worker_root: str | Path | None = None,
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

    root = _worker_root(worker_root)
    base = ensure_local(url, root, clone=clone, log=log)
    if base is None:
        return None
    wt = worktree_path(url, slug, root)
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


# The directory under the worker root that holds the worktrees of VAULT tasks —
# the vault's counterpart of an external repo's `<repo>.wt`. `is_vault_worktree`
# reads a path's parent name against it, so the shape `<...>/vault.wt/<slug>` is
# the contract, not just a default.
VAULT_WORKTREE_DIRNAME = "vault.wt"


def vault_worktree_path(slug: str,
                        worker_root: str | Path | None = None) -> Path:
    """Where the linked worktree of a vault task's run lives:
    `<worker_root>/vault.wt/<slug>`.

    Pure — touches no filesystem — and resolved at call time like every other
    path here. Under the worker root on purpose: a stage reaches it through the
    `--add-dir <worker_root>` it already gets, and it sits beside the external
    repos' `<repo>.wt/<slug>` worktrees. One directory deeper than the worker
    root and not named `wt-*`, so neither the disk guard (direct children that
    are clones) nor the legacy `wt-*` sweep ever looks at it; the run that made
    it removes it (see `ensure_vault_worktree`).

    A repo literally named `vault` shares the parent directory; slugs name one
    task each, so the leaf paths still cannot meet."""
    return _worker_root(worker_root) / VAULT_WORKTREE_DIRNAME / slug


def is_vault_worktree(path: str | Path | None) -> bool:
    """Whether `path` has the shape `vault_worktree_path` produces.

    Lexical only — no filesystem, no git — and never raises: the stage-prompt
    builder asks this about whatever `working_dir` it was handed."""
    try:
        return Path(path).parent.name == VAULT_WORKTREE_DIRNAME
    except Exception:       # noqa: BLE001 — "not a path" is simply not a worktree
        return False


def ensure_vault_worktree(vault_root: str | Path, slug: str,
                          worker_root: str | Path | None = None,
                          log: list[str] | None = None,
                          branch: str | None = None) -> Path | None:
    """Ensure a linked worktree of the VAULT repository exists for `slug` and
    return its path, or None (with the reason appended to `log`). `branch` is
    the branch the caller is about to put on it, when it knows one.

    A task with no `repo:` works on the vault itself. Its branch used to be
    checked out in the live vault, which moved that checkout's HEAD for the whole
    run — and the live vault is where a person, the planner and the sync daemon
    commit, so their commits rode the worker branch (2026-11-17). The run now
    gets this worktree instead, and the live checkout is never the one switched.

    Nothing here moves the vault's own HEAD or writes its index: `worktree add`
    only registers the new tree. The tree is cut `--detach` from the LOCAL
    default branch, named explicitly — not from whatever the live checkout has
    checked out — because the local default branch is what carries commits that
    are not pushed yet (the task file a person just committed, sync commits),
    and a live checkout that is abnormally elsewhere must not become the base.
    `stages.prepare_vault_branch` then puts `worker/<slug>` on it.

    Returns None rather than guessing when:
    * `vault_root` is not the top of its git checkout (not a repository, or a
      directory inside another one — the worktree would be that other
      repository's);
    * the local default branch does not exist yet (a repository with no commit
      has nothing to cut a worktree from);
    * the vault's own checkout is sitting on `branch`: one branch cannot be
      checked out in two worktrees, and the live checkout is not this function's
      to move;
    * the target path exists but is not a worktree of this repository. It is
      never deleted or moved — it may hold someone's work — so the add is not
      attempted over it;
    * git fails or times out.
    An existing worktree of this repository at the path is returned as-is, like
    `ensure_worktree`'s early return. Never raises."""
    def _note(s: str) -> None:
        if log is not None:
            log.append(s)

    vault = Path(vault_root)

    def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(cwd), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=GIT_TIMEOUT_WORKTREE_S)

    def _common_dir(cwd: Path) -> Path | None:
        r = _git(cwd, "rev-parse", "--git-common-dir")
        out = (r.stdout or "").strip()
        if r.returncode != 0 or not out:
            return None
        p = Path(out)
        return (p if p.is_absolute() else cwd / p).resolve()

    try:
        top = _git(vault, "rev-parse", "--show-toplevel")
        top_out = (top.stdout or "").strip()
        if top.returncode != 0 or not top_out or Path(top_out).resolve() != vault.resolve():
            _note(f"{vault} is not the top of a git checkout — a vault task needs "
                  "the vault to be its own git repository")
            return None
        # Same rule as stages.default_branch (repos does not import stages).
        head = _git(vault, "symbolic-ref", "refs/remotes/origin/HEAD")
        head_out = (head.stdout or "").strip()
        default = head_out.rsplit("/", 1)[-1] if head.returncode == 0 and head_out else "main"
        if _git(vault, "rev-parse", "--verify", "--quiet",
                f"refs/heads/{default}").returncode != 0:
            _note(f"no local {default} branch — the vault has no commit to cut a "
                  "worktree from")
            return None
        if branch:
            cur = _git(vault, "branch", "--show-current")
            if cur.returncode == 0 and (cur.stdout or "").strip() == branch:
                _note(f"the vault checkout itself is on {branch} — not cutting a "
                      "worktree over it")
                return None
        # Through the module attribute and with `worker_root` passed on untouched
        # (None stays None), so the one place that decides the location decides it
        # here too.
        wt = vault_worktree_path(slug, worker_root)
        if (wt / ".git").exists():
            ours = _common_dir(vault)
            if ours is not None and _common_dir(wt) == ours:
                return wt
            _note(f"{wt} exists but is not a worktree of {vault} — left as it is")
            return None
        wt.parent.mkdir(parents=True, exist_ok=True)
        _git(vault, "worktree", "prune")
        r = _git(vault, "worktree", "add", "--detach", str(wt), default)
    except subprocess.TimeoutExpired:
        _note(f"vault worktree git timed out after {GIT_TIMEOUT_WORKTREE_S}s: {vault}")
        return None
    except Exception as e:      # noqa: BLE001 — a prep helper reports, never raises
        _note(f"vault worktree prep errored ({type(e).__name__}): {vault}")
        return None
    if r.returncode == 0:
        _note(f"vault worktree {wt} added (detached at {default}) from {vault}")
        return wt
    lines = (r.stderr or "").strip().splitlines()
    last = lines[-1].strip() if lines else ""
    _note(f"vault worktree add failed: {last}")
    return None


def resolve(payload: dict, agent_repo: str | None,
            worker_root: str | Path | None = None,
            clone: bool = False) -> Path | None:
    """Resolve the working repo for a task: task's own repo URL wins, else the
    agent's fixed repo, else None (vault tasks are handled by the caller)."""
    url = (payload or {}).get("repo")
    if url:
        return ensure_local(url, _worker_root(worker_root), clone=clone)
    if agent_repo:
        p = Path(os.path.expanduser(agent_repo))
        return p if (p / ".git").exists() else None
    return None
