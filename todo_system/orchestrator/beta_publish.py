"""Deterministic post-merge Firebase App Distribution beta publisher.

Runs in the orchestrator RUNTIME (VM: node22 + firebase-tools + gradle), NOT
inside a ``claude -p`` stage — an LLM's only role here is release-note prose, and
even that is done deterministically below. ``runner.StagedExecutor._execute``
calls :func:`maybe_publish` the instant ``stages.merge_landed(...) is True`` for
an app-repo PR the worker just auto-merged. A repo with no entry in
``.macp/beta_publish_config.json`` is skipped (returns ``None``) so unconfigured
repos behave exactly as before. That is the regression-safe contract.

Why a machine-readable canon instead of prose: the versionCode watermark used to
live only in a human sentence (the ``firebase-app-distribution-provisioning``
memory), which code cannot parse. Here it is ``.macp/beta_watermarks.json`` — read →
bump +1 → commit after each success. ``published_shas`` dedups so the same main
sha is never published twice (re-dispatch / already-merged re-runs). An app-level
``flock`` serializes back-to-back merges of one app so two publishes cannot race
the watermark into a lost update. A publish failure NEVER reverts the merge — it
surfaces (raise → the runner's ``except`` pings kakao) as a retryable state.
When the live vault HEAD is on a worker branch, the watermark commit is also
mirrored onto main without a checkout; branch and main then hold the same blob,
so the later squash merge has nothing to conflict on.

Tester-count gate: ``gutenberg-reader-app`` once shipped six releases to a
``beta-testers`` group with **zero** testers — every distribute reported
``success`` and reached no device. So we read ``appdistribution:group:list``
first and refuse to distribute unless the group's Tester Count is a confirmed
> 0 (an unparseable count is treated as abort, not as "probably fine").

Two triggers, one publisher. The merge-time call above only fires while a worker
auto-merge is happening, so a main that landed any other way — config entry
momentarily missing, a publish that raised, a human merge — used to stay
unpublished until the next worker merge of that app (DanceMirror ``35cdd60``,
2026-09-25: the watermark said "never published"). The catch-up sweep at the
bottom of this module is the second trigger: an orchestrator tick calls
:func:`spawn_sweep_if_due`, which — gated on a 15-min min-poll, a vault HEAD that
is main, and no sweep already running — spawns a DETACHED process that asks each
configured repo for ``origin/<default>``, and publishes whatever the watermark
never saw. It builds in a throwaway ``git worktree`` (the runner's checkout is
left alone), publishes at most one repo per sweep, and puts a repo that fails
into a per-sha backoff with exactly one kakao alert. Both triggers end in the
same :func:`maybe_publish`, under the same lock and the same dedup.

Testability: every external effect — the gradle build, firebase-tools
``group:list`` + ``distribute``, the ``git diff`` for migration detection, and
the watermark git commit — goes through the injected ``run=`` callable, so the
unit suite passes a fake and never shells out. All canon / lock / notes / task
paths are injectable too, keeping tests inside ``tmp_path`` (the todo_system
test conftest fails the whole run if the real vault is touched).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Iterator
from pathlib import Path

from .. import paths
from ..kakao_format import compose_alert
from .. import worker as _worker
from . import jobs
from . import repos

# Engine root — paths.engine_root(): the repo root in a development checkout,
# todo_system/_engine in an installed wheel; immune to MACP_VAULT_ROOT. In a
# checkout the per-install scratch below (locks, sweep state) hangs off it:
# scratch is not data, so it stays pinned here even when the vault-data root is
# redirected. An installed wheel has no writable Worker/ beside site-packages,
# so there the scratch moves to the vault (see _SCRATCH_ROOT).
_ENGINE_ROOT = paths.engine_root()      # repo root (dev) / todo_system/_engine (wheel)
# Copy-source root — the vault the runner reads gitignored build inputs from
# (.secrets/, gradle-prop JSON). Resolves through MACP_VAULT_ROOT so a redirected
# vault is read from the right place; byte-identical to _ENGINE_ROOT when unset.
# The start= anchor is paths.vault_search_start(): the engine root in a checkout,
# None in an installed wheel (cwd-upward search instead of a site-packages anchor).
_VAULT_DATA_ROOT = paths.vault_root(start=paths.vault_search_start())
# The config/watermark canon is VAULT DATA (operator-owned app list, runtime
# versionCode ledger), not engine source — it lives in the vault's .macp/ and
# follows _VAULT_DATA_ROOT so a redirected vault is published from its own canon.
CONFIG_PATH = _VAULT_DATA_ROOT / ".macp" / "beta_publish_config.json"
WATERMARK_PATH = _VAULT_DATA_ROOT / ".macp" / "beta_watermarks.json"
# Per-install scratch (locks, sweep state/log/worktrees). A checkout keeps it
# under its own gitignored Worker/_state — byte-identical to before, and immune
# to a redirected vault (test_paths_engine_root pins LOCK_DIR under
# engine_root()). An installed wheel's engine root is inside site-packages, so
# its scratch goes to the vault's Worker/_state (gitignored there as well).
_SCRATCH_ROOT = _VAULT_DATA_ROOT if paths.is_installed() else _ENGINE_ROOT
LOCK_DIR = _SCRATCH_ROOT / "Worker" / "_state" / "beta_locks"
# Catch-up sweep runtime (see the sweep section at the bottom of this module).
# Same gitignored Worker/_state/ tree as the locks above: nothing here is canon,
# it is all reconstructible, and committing any of it would stall the queue.
SWEEP_STATE_PATH = _SCRATCH_ROOT / "Worker" / "_state" / "beta_sweep.json"
SWEEP_WORKTREE_DIR = _SCRATCH_ROOT / "Worker" / "_state" / "beta_worktrees"
SWEEP_LOG_PATH = _SCRATCH_ROOT / "Worker" / "_state" / "beta_sweep.log"
# Discovery is ~one ls-remote per configured repo; 15 min keeps that off every
# ~5-min tick. A failed publish waits 6h before the same sha burns another build.
SWEEP_MIN_INTERVAL_S = 900
SWEEP_FAIL_BACKOFF_S = 6 * 3600

# Prepended as the release-note first line when the merge carries a DB
# migration, per beta-publish-standing-approval (kept as the exception even after
# the 2026-09-15 "publish anyway" correction — the warning still ships).
BACKUP_WARNING = "⚠️ 설치 전에 내보내기로 백업해 주세요. 앱을 지우지 말고 덮어 설치하세요."

# Build timeout: assembleBeta is minutes, not the 45-min stage cap; distribute is
# seconds. Kept generous but bounded so a hung gradle daemon can't wedge the run.
_BUILD_TIMEOUT = 1800
_FIREBASE_TIMEOUT = 600
_GIT_TIMEOUT = 60

# Beta builds never attach to a shared Gradle daemon. A daemon spawned by the
# runner's post-merge publish lives in the runner's systemd cgroup; a later sweep
# build reusing it died with "the daemon has disappeared" when maintenance
# restarted the runner (worldgraph-android 716092d1e, 2026-10-16). --no-daemon
# keeps the build JVM in the caller's own process tree.
_NO_DAEMON = "--no-daemon"


def build_argv(config: dict, props: list[str]) -> list[str]:
    """``build_cmd`` + ``--no-daemon`` (gradlew only, once) + ``-P`` props."""
    cmd = list(config["build_cmd"])
    if cmd and cmd[0].endswith("gradlew") and _NO_DAEMON not in cmd:
        cmd.append(_NO_DAEMON)
    return [*cmd, *props]
_FETCH_TIMEOUT = 300                             # a full fetch is not a 60s job

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class BetaPublishError(RuntimeError):
    """A publish attempt failed AFTER the config/dedup/tester gates passed (build
    or distribute returned non-zero). The merge stands; the runner surfaces this
    as a retryable state rather than reverting anything."""


def _run(argv: list[str], *, cwd: str | None = None, env: dict | None = None,
         timeout: int | None = None) -> "subprocess.CompletedProcess[str]":
    """Default subprocess runner — the single seam every external effect flows
    through. Never raises on a non-zero exit (callers inspect ``returncode``)."""
    return subprocess.run(argv, cwd=cwd, env=env, timeout=timeout,
                          capture_output=True, text=True, check=False,
                          encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Config + watermark canon (git-tracked JSON under the vault's .macp/)
# ---------------------------------------------------------------------------

def load_config(config_path: Path | str = CONFIG_PATH) -> dict[str, dict]:
    """The ``repos`` map from the static config, or ``{}`` when the file is
    missing/unreadable (so a stray deploy can only ever *skip*, never crash)."""
    try:
        data = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data.get("repos", {}) if isinstance(data, dict) else {}


def repo_config(repo_name: str, config_path: Path | str = CONFIG_PATH) -> dict | None:
    """This repo's publish config, or ``None`` (= not a publishable repo → skip)."""
    return load_config(config_path).get(repo_name)


def _load_watermarks(path: Path | str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _save_watermarks(path: Path | str, data: dict) -> None:
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")


def already_published(entry: dict, sha: str) -> bool:
    """True if this main sha was already the target of a publish for this repo —
    the dedup that stops a re-dispatched / re-run merge from double-publishing."""
    return sha == entry.get("last_published_sha") or sha in (entry.get("published_shas") or [])


# ---------------------------------------------------------------------------
# Serialization lock (mirror worker.queue_lock's flock idiom)
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def app_lock(lock_dir: Path | str, repo_name: str) -> Iterator[None]:
    """Exclusive per-app flock. Two merges of the SAME app serialize here so the
    read→bump→write of the watermark cannot interleave; different apps take
    different lock files and publish in parallel.

    Concurrency invariant (per-run worktrees). The default ``lock_dir`` is
    ``LOCK_DIR`` — HOST SCRATCH under ``Worker/_state/beta_locks``, anchored on the
    install root, NOT on any per-run worktree. So two concurrent runs of one app in
    two different worktrees are handed the identical ``lock_dir`` and contend on the
    SAME ``<repo>.lock`` file: the second ``flock(LOCK_EX)`` blocks until the first
    releases (flock is per-open-file-description, so even two threads/processes that
    each ``open()`` the one path are mutually excluded). A per-worktree lock dir
    would instead give each run its own file and serialize nothing — the failure
    this invariant rules out."""
    lock_dir = Path(lock_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{repo_name.replace('/', '_')}.lock"
    with open(lock_path, "w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Release notes + migration detection (deterministic — no LLM)
# ---------------------------------------------------------------------------

# Added-line signatures of a Room/DB migration, scanned only within the config's
# migration_globs (Database/Migration sources + exported schemas) so an unrelated
# `version = N` elsewhere can't trip it.
_MIGRATION_ADD_RE = re.compile(r"MIGRATION_\d+_\d+|version\s*=\s*\d+|\"version\"\s*:\s*\d+")

# Release notes are read on a phone's install sheet — a catch-up release spanning
# 40 commits lists the first few, not all of them.
_NOTES_BULLET_LIMIT = 9


def detect_migration(repo_dir: Path | str, merge_sha: str, globs: list[str],
                     *, run: Runner = _run, diff_base: str | None = None) -> bool:
    """Does the diff being shipped add a schema-version bump / MIGRATION_?

    Diffs ``<sha>^..<sha>`` limited to ``globs``. A git failure (shallow clone,
    bad ref) returns False — better to omit the backup warning than fabricate one.

    ``diff_base`` widens the range to ``<base>..<sha>``, which is what the
    catch-up sweep needs: it ships every commit since the LAST published sha, so
    a migration that landed in a skipped merge must still raise the warning. The
    default keeps the merge-time caller's argv byte-identical.
    """
    rng = f"{diff_base}..{merge_sha}" if diff_base else f"{merge_sha}^..{merge_sha}"
    argv = ["git", "diff", rng]
    if globs:
        argv += ["--", *globs]
    res = run(argv, cwd=str(repo_dir), timeout=_GIT_TIMEOUT)
    if getattr(res, "returncode", 1) != 0:
        return False
    for line in (getattr(res, "stdout", "") or "").splitlines():
        if line.startswith("+") and not line.startswith("+++") and _MIGRATION_ADD_RE.search(line):
            return True
    return False


def _git_ok(argv: list[str], *, run: Runner, timeout: int) -> bool:
    """rc==0, with a timeout or spawn failure counted as a plain failure — ``_run``
    lets ``TimeoutExpired`` through, and a hung fetch must not cost the release."""
    try:
        return getattr(run(argv, timeout=timeout), "returncode", 1) == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def _rev_present(repo_dir: Path | str, rev: str, *, run: Runner) -> bool:
    return _git_ok(["git", "-C", str(repo_dir), "cat-file", "-e", f"{rev}^{{commit}}"],
                   run=run, timeout=_GIT_TIMEOUT)


def ensure_probe_range(repo_dir: Path | str, merge_sha: str, *, run: Runner = _run,
                       diff_base: str | None = None) -> bool:
    """True when every rev :func:`detect_migration` will diff resolves locally.

    ``resolve_merge_sha`` takes the squash oid from gh, and right after the merge
    the clone may not have it yet: ``<sha>^`` is a bad object, detect_migration
    returns False, and the backup warning silently drops (DripCoffee 131). One
    fetch of the sha — then of the default branch — closes that gap; if the revs
    are still missing the caller skips the probe instead of guessing.
    """
    revs = [merge_sha, diff_base] if diff_base else [merge_sha, f"{merge_sha}^"]
    if all(_rev_present(repo_dir, r, run=run) for r in revs):
        return True
    d = str(repo_dir)
    if not _git_ok(["git", "-C", d, "fetch", "origin", merge_sha], run=run, timeout=_FETCH_TIMEOUT):
        try:
            branch = _default_branch(repo_dir, run=run)
        except (subprocess.TimeoutExpired, OSError):
            branch = "main"
        _git_ok(["git", "-C", d, "fetch", "origin", branch], run=run, timeout=_FETCH_TIMEOUT)
    return all(_rev_present(repo_dir, r, run=run) for r in revs)


def build_release_notes(payload: dict, *, migration: bool,
                        subjects: "list[str] | tuple[str, ...]" = ()) -> str:
    """Short, user-language notes from the merged task (PR title / outcome). The
    migration backup warning, when present, is deliberately the FIRST line.

    ``subjects`` are commit subjects of everything this release carries — the
    catch-up sweep's release can span several merges, and "베타 업데이트" would tell
    a tester nothing about which of them they are installing. Empty (the
    merge-time default) leaves the note byte-identical to the single-merge form.
    """
    headline = ""
    for key in ("pr_title", "outcome", "title", "task_id"):
        val = (payload or {}).get(key)
        if val and str(val).strip():
            headline = str(val).strip()
            break
    subjects = [str(s).strip() for s in (subjects or ()) if str(s).strip()]
    lines: list[str] = []
    if migration:
        lines.append(BACKUP_WARNING)
    if subjects:
        extra = len(subjects) - 1
        lines.append(subjects[0] + (f" 외 {extra}건" if extra else ""))
        lines += [f"- {s}" for s in subjects[1:_NOTES_BULLET_LIMIT + 1]]
    else:
        lines.append(headline or "베타 업데이트")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Command assembly (pure — the Phase 5 argv asserts pin these)
# ---------------------------------------------------------------------------

def collect_commit_subjects(repo_dir: Path | str, base: str, merge_sha: str, *,
                            run: Runner = _run, limit: int = 20) -> list[str]:
    """Commit subjects in ``<base>..<sha>``, merges excluded, newest first.

    The material for a catch-up release note: every user-visible change since the
    last published sha, not just the last merge. A git failure returns ``[]`` (the
    same attitude as detect_migration — the caller falls back to the old headline
    rather than shipping a wrong changelog)."""
    r = run(["git", "log", "--no-merges", "--format=%s", f"{base}..{merge_sha}"],
            cwd=str(repo_dir), timeout=_GIT_TIMEOUT)
    if getattr(r, "returncode", 1) != 0:
        return []
    subjects = [ln.strip() for ln in (getattr(r, "stdout", "") or "").splitlines() if ln.strip()]
    return subjects[:limit]


def group_list_argv(config: dict) -> list[str]:
    return ["npx", "-y", "firebase-tools", "appdistribution:group:list",
            "--project", config["project"]]


def distribute_argv(config: dict, apk_path: str, notes_file: str) -> list[str]:
    return ["npx", "-y", "firebase-tools", "appdistribution:distribute", apk_path,
            "--app", config["app_id"], "--groups", config["group"],
            "--release-notes-file", notes_file]


def _cells(raw: str) -> list[str]:
    """Table cells from one firebase-tools output row, box-drawing (│) or ascii
    (|), with the border-induced empty end cells trimmed so column indices align."""
    parts = [c.strip() for c in re.split(r"[│|]", raw)]
    while parts and parts[0] == "":
        parts.pop(0)
    while parts and parts[-1] == "":
        parts.pop()
    return parts


def parse_tester_count(output: str, group_alias: str) -> int | None:
    """Tester Count for ``group_alias`` from an ``appdistribution:group:list``
    table, or ``None`` when it cannot be located. ``None`` is the *safe* answer —
    the caller aborts rather than distribute to an unconfirmed group."""
    rows = [c for c in (_cells(line) for line in output.splitlines()) if c]
    col: int | None = None
    header_i = 0
    for i, cells in enumerate(rows):
        for j, cell in enumerate(cells):
            if re.fullmatch(r"tester\s*count", cell, re.IGNORECASE):
                col, header_i = j, i
                break
        if col is not None:
            break
    if col is None:
        return None
    for cells in rows[header_i + 1:]:
        if group_alias in cells and col < len(cells):
            m = re.search(r"\d+", cells[col])
            if m:
                return int(m.group())
    return None


def parse_release_id(output: str) -> str | None:
    """The Firebase release id from distribute output (console URL, else a
    parenthesized token). ``None`` when absent — the note then reads ``릴리즈 -``."""
    m = re.search(r"/releases/([A-Za-z0-9]+)", output)
    if m:
        return m.group(1)
    m = re.search(r"release[^\n]*?\(([A-Za-z0-9]{8,})\)", output, re.IGNORECASE)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Merge sha resolution + record-keeping side effects
# ---------------------------------------------------------------------------

def resolve_merge_sha(repo_dir: Path | str, branch: str, *, run: Runner = _run,
                      default_branch: str = "main") -> str | None:
    """The merged main sha for dedup identity: the PR's mergeCommit oid (gh),
    falling back to ``origin/<default>``'s tip. ``None`` when neither answers."""
    try:
        r = run(["gh", "pr", "view", branch, "--json", "mergeCommit", "-q", ".mergeCommit.oid"],
                cwd=str(repo_dir), timeout=_GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        r = None                     # slow GitHub API → fall back to the local ref below
    oid = (getattr(r, "stdout", "") or "").strip()
    if getattr(r, "returncode", 1) == 0 and re.fullmatch(r"[0-9a-f]{7,40}", oid):
        return oid
    r2 = run(["git", "rev-parse", f"origin/{default_branch}"], cwd=str(repo_dir), timeout=_GIT_TIMEOUT)
    oid2 = (getattr(r2, "stdout", "") or "").strip()
    if getattr(r2, "returncode", 1) == 0 and re.fullmatch(r"[0-9a-f]{7,40}", oid2):
        return oid2
    return None


def stage_build_files(repo_dir: Path | str, copy_files: dict[str, str], *,
                      vault_root: Path | str = _VAULT_DATA_ROOT) -> list[Path]:
    """Copy build inputs that are not in git into the clone before the build.

    ``copy_files`` maps a repo-relative destination to a vault-relative source,
    e.g. ``{".secrets/mulkko.json": ".secrets/mulkko.json"}``. 물꼬's Supabase
    backup section only exists in a build that finds that gitignored file; the
    worker clone the runner publishes from never has it, so an automatic beta
    would silently ship with the backup section hidden. A missing source raises
    :class:`BetaPublishError` BEFORE the build — no half-configured APK reaches
    testers. Returns the files this call created, so the caller removes them after
    the build and the worker clone does not keep a copy of the secret.
    """
    repo_root = Path(repo_dir).resolve()
    vault = Path(vault_root).resolve()
    created: list[Path] = []
    for dest_rel, src_rel in (copy_files or {}).items():
        src = (vault / src_rel).resolve()
        dest = (repo_root / dest_rel).resolve()
        if not src.is_relative_to(vault) or not dest.is_relative_to(repo_root):
            raise BetaPublishError(f"copy_files entry escapes its root: {dest_rel!r} <- {src_rel!r}")
        if not src.is_file():
            raise BetaPublishError(f"required build file missing in the vault: {src_rel}")
        if dest.is_file() and dest.read_bytes() == src.read_bytes():
            continue
        existed = dest.exists()
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        dest.chmod(0o600)
        if not existed:
            created.append(dest)
    return created


def build_prop_args(config: dict, *, vault_root: Path | str = _VAULT_DATA_ROOT) -> list[str]:
    """``-P<gradle-prop>=<value>`` args read from a gitignored secret JSON, to be
    appended AFTER ``build_cmd`` — the symmetric twin of :func:`stage_build_files`
    for build inputs that are gradle *properties* rather than copied files.

    ``build_props_from`` is a vault-relative path to a gitignored secret JSON (e.g.
    ``.secrets/advote.json``). ``build_props`` maps a **gradle property name** to the
    **JSON key** whose value fills it: ``{"advote.endpoint": "endpoint"}`` reads key
    ``"endpoint"`` and emits ``-Padvote.endpoint=<that value>`` — a key-*name* map,
    never the value. DripCoffee's in-app 건의 only reaches the advote endpoint when
    the APK carries these properties; the orchestrator VM's worker clone has no
    ``.secrets/``, so without this the beta silently falls back to a share-sheet —
    the same class of fault copy_files fixed for 물꼬's backup section.

    A repo that declares neither key gets ``[]`` — the argv is byte-identical
    (regression-safe). A missing file, an unreadable / non-object JSON, or a mapped
    key that is absent / non-string / blank raises :class:`BetaPublishError` BEFORE
    the build, naming the file to create and the key that is missing (NEVER the
    secret value) so a human can fix it from the alert alone.
    """
    src_rel = config.get("build_props_from")
    build_props = config.get("build_props") or {}
    if not src_rel or not build_props:
        return []                                # unconfigured → argv unchanged
    vault = Path(vault_root).resolve()
    src = (vault / src_rel).resolve()
    if not src.is_relative_to(vault):
        raise BetaPublishError(f"build_props_from escapes the vault: {src_rel!r}")
    keys = sorted(build_props.values())
    if not src.is_file():
        raise BetaPublishError(
            f"required build-props secret missing in the vault: {src_rel} "
            f"— create it with keys {keys}")
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise BetaPublishError(f"build_props_from is not readable JSON: {src_rel}") from e
    if not isinstance(data, dict):
        raise BetaPublishError(f"build_props_from must be a JSON object: {src_rel}")
    args: list[str] = []
    for gradle_prop, json_key in build_props.items():
        val = data.get(json_key)
        if not isinstance(val, str) or not val.strip():
            raise BetaPublishError(
                f"build_props key {json_key!r} is empty or absent in {src_rel} "
                f"(needed for -P{gradle_prop})")
        args.append(f"-P{gradle_prop}={val}")
    return args


def _write_notes(notes: str, notes_dir: Path | str | None, repo_name: str) -> str:
    if notes_dir is not None:
        d = Path(notes_dir)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{repo_name}-release-notes.txt"
    else:
        fd, name = tempfile.mkstemp(prefix=f"{repo_name}-beta-", suffix=".txt")
        os.close(fd)
        path = Path(name)
    path.write_text(notes, encoding="utf-8")
    return str(path)


def _commit_watermark(watermark_path: Path | str, repo_name: str, version_code: int,
                      *, vault_root: Path | str, run: Runner = _run,
                      progress: Callable[[str], None]) -> None:
    """Commit ONLY the watermark file. It is tracked and lives outside Worker/, so
    leaving it dirty would stall the 5-min queue worker
    (worker-queue-stall-on-uncommitted-vault-files). ``git commit -- <path>``
    commits just this file's change and never sweeps up whatever else is staged.

    Runs in the VAULT repo the CALLER names via ``vault_root=``; the todo-sync
    daemon pushes it. The repo is never back-derived from ``watermark_path`` —
    ``.macp/`` happens to sit one level under the vault root, but an injected
    tmp_path canon would silently commit in the wrong directory.

    The merge-time trigger runs while a worker run has the live vault HEAD on its
    ``worker/<slug>`` branch, so the in-place commit lands there — and is orphaned
    once that branch is squash-merged and deleted. After a successful in-place
    commit, :func:`_mirror_watermark_to_main` therefore commits the same blob onto
    ``refs/heads/main`` with plumbing (no checkout) whenever HEAD is not main."""
    wm = Path(watermark_path)
    msg = f"chore: beta watermark {repo_name} -> {version_code}"
    r = run(["git", "-C", str(vault_root), "commit", "-m", msg, "--", str(wm)],
            timeout=_GIT_TIMEOUT)
    if getattr(r, "returncode", 1) != 0:
        # Non-fatal: the publish already succeeded. Surface so a human (or the
        # daemon's next tick) reconciles the dirty canon rather than silently.
        progress(f"beta-publish: watermark commit failed (rc={getattr(r, 'returncode', None)}) "
                 f"— canon written, needs a manual commit")
        return
    _mirror_watermark_to_main(wm, msg, vault_root=vault_root, run=run, progress=progress)


def _mirror_watermark_to_main(wm: Path, msg: str, *, vault_root: Path | str,
                              run: Runner, progress: Callable[[str], None]) -> None:
    """Commit ``wm``'s current blob onto ``refs/heads/main`` without touching HEAD,
    the working tree or the real index — a no-op when HEAD already is main.

    Plumbing only: ``hash-object -w`` → ``read-tree <old-main>`` / ``update-index
    --cacheinfo`` / ``write-tree`` in a throwaway ``GIT_INDEX_FILE`` under the
    system tempdir → ``commit-tree -p <old-main>`` → ``update-ref refs/heads/main
    <new> <old-main>`` (the old-value guard fails rather than clobbering a
    concurrent update). Never pushes, checks out, stashes or resets. A canon
    outside ``vault_root`` (test paths) or a repo without ``main`` is skipped
    silently; any failed step is non-fatal and leaves exactly one progress line."""
    try:
        rel = Path(wm).resolve().relative_to(Path(vault_root).resolve()).as_posix()
    except ValueError:
        return
    v = str(vault_root)
    step = "symbolic-ref"

    def _g(*args: str, env: dict | None = None) -> "subprocess.CompletedProcess[str]":
        return run(["git", "-C", v, *args], env=env, timeout=_GIT_TIMEOUT)

    def _failed(r) -> None:
        progress(f"beta-publish: watermark mirror to main failed at {step} "
                 f"(rc={getattr(r, 'returncode', None)}) — in-place commit kept")

    try:
        r = _g("symbolic-ref", "--short", "-q", "HEAD")
        head = (r.stdout or "").strip() if getattr(r, "returncode", 1) == 0 else ""
        if head == "main":
            return
        branch = head or "detached"

        step = "rev-parse"
        r = _g("rev-parse", "-q", "--verify", "refs/heads/main")
        old = (r.stdout or "").strip() if getattr(r, "returncode", 1) == 0 else ""
        if not old:
            return                                   # no main to mirror onto

        step = "hash-object"
        r = _g("hash-object", "-w", "--", str(wm))
        blob = (r.stdout or "").strip()
        if getattr(r, "returncode", 1) != 0 or not blob:
            return _failed(r)

        tmpdir = tempfile.mkdtemp(prefix="macp-wm-index-")
        try:
            # A not-yet-existing path: git rejects an empty existing index file.
            ienv = {**os.environ, "GIT_INDEX_FILE": os.path.join(tmpdir, "index")}
            step = "read-tree"
            r = _g("read-tree", old, env=ienv)
            if getattr(r, "returncode", 1) != 0:
                return _failed(r)
            step = "update-index"
            r = _g("update-index", "--add", "--cacheinfo", f"100644,{blob},{rel}", env=ienv)
            if getattr(r, "returncode", 1) != 0:
                return _failed(r)
            step = "write-tree"
            r = _g("write-tree", env=ienv)
            tree = (r.stdout or "").strip()
            if getattr(r, "returncode", 1) != 0 or not tree:
                return _failed(r)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        step = "commit-tree"
        r = _g("commit-tree", tree, "-p", old, "-m", msg)
        new = (r.stdout or "").strip()
        if getattr(r, "returncode", 1) != 0 or not new:
            return _failed(r)

        step = "update-ref"
        r = _g("update-ref", "-m", msg, "refs/heads/main", new, old)
        if getattr(r, "returncode", 1) != 0:
            return _failed(r)
    except (subprocess.TimeoutExpired, OSError) as e:
        progress(f"beta-publish: watermark mirror to main failed at {step} "
                 f"(rc=exc:{type(e).__name__}) — in-place commit kept")
        return
    progress(f"beta-publish: watermark also committed to main {new[:9]} (HEAD on {branch})")


def append_progress_log(task_path: Path | str, line: str, *, today: str | None = None) -> bool:
    """Append ``- <date>: <line>`` under the task's ``## Progress Log`` (on-disk;
    Todo/items is gitignored, so no commit needed). No-ops if the file is absent."""
    p = Path(task_path)
    if not p.exists():
        return False
    today = today or dt.date.today().isoformat()
    entry = f"- {today}: {line}"
    text = p.read_text(encoding="utf-8").rstrip("\n")
    if "## Progress Log" in text:
        text = f"{text}\n{entry}\n"
    else:
        text = f"{text}\n\n## Progress Log\n\n{entry}\n"
    p.write_text(text, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def maybe_publish(
    repo_name: str,
    repo_dir: Path | str,
    merge_sha: str,
    payload: dict,
    *,
    progress: Callable[[str], None] = lambda _m: None,
    config_path: Path | str = CONFIG_PATH,
    watermark_path: Path | str = WATERMARK_PATH,
    lock_dir: Path | str = LOCK_DIR,
    notes_dir: Path | str | None = None,
    task_path: Path | str | None = None,
    run: Runner = _run,
    notify: Callable[[str], None] = _worker.kakao_notify,
    commit_watermark: bool = True,
    vault_root: Path | str = _VAULT_DATA_ROOT,
    cumulative_notes: bool = False,
) -> str | None:
    """Publish a beta for a just-merged app-repo PR, once, serialized, deduped.

    Returns a one-line note for the run/progress log on success or a handled
    abort (0 testers); ``None`` when there is nothing to do (unconfigured repo,
    missing watermark entry, or this sha already published). Raises
    :class:`BetaPublishError` only when the build or distribute itself fails.

    ``cumulative_notes`` (the catch-up sweep's flag) widens the release notes and
    the migration probe from "this merge" to "everything since the last published
    sha". Off by default, so the merge-time caller's behaviour — and its argv —
    are unchanged.
    """
    config = repo_config(repo_name, config_path)
    if config is None:
        progress(f"beta-publish: {repo_name} has no config entry — skip")
        return None                              # not a publishable repo → skip (no regression)

    repo_dir = Path(repo_dir)
    # Serialize + dedup under one per-app lock (see app_lock's invariant). The
    # watermark read below happens only AFTER this run wins the lock, so a sibling
    # run of the same app that published first has already bumped version_code and
    # recorded its sha — already_published() then short-circuits THIS run to None.
    # That is why two concurrent same-sha publishes (a race, or a re-dispatch /
    # already-merged re-run) publish exactly once, with no lost watermark update.
    with app_lock(lock_dir, repo_name):
        wm = _load_watermarks(watermark_path)
        entry = wm.get("repos", {}).get(repo_name)
        if entry is None:
            progress(f"beta-publish: {repo_name} is configured but has no watermark entry — skip")
            return None
        if already_published(entry, merge_sha):
            progress(f"beta-publish: {repo_name} main {merge_sha[:9]} already published — skip")
            return None

        next_vc = int(entry["version_code"]) + 1

        # 1) Build with the next versionCode as GITHUB_RUN_NUMBER (the CI watermark).
        #    build_prop_args runs FIRST so a missing gradle-prop secret aborts before
        #    stage_build_files copies any secret into the clone; then out-of-git build
        #    inputs (copy_files) are staged for the build only, and -P<prop>=<val>
        #    gradle properties are appended after build_cmd.
        build_env = {**os.environ, "GITHUB_RUN_NUMBER": str(next_vc)}
        props = build_prop_args(config, vault_root=vault_root)
        staged = stage_build_files(repo_dir, config.get("copy_files") or {}, vault_root=vault_root)
        try:
            b = run(build_argv(config, props), cwd=str(repo_dir), env=build_env, timeout=_BUILD_TIMEOUT)
        finally:
            for f in staged:
                f.unlink(missing_ok=True)
        if getattr(b, "returncode", 1) != 0:
            raise BetaPublishError(
                f"{repo_name} build failed (rc={getattr(b, 'returncode', None)}): "
                f"{(getattr(b, 'stderr', '') or '')[:300]}")

        # 2) Tester Count > 0 or abort — never distribute to an unconfirmed group.
        #    An unparseable listing is almost always a transient firebase-tools / npx
        #    failure, not an empty group (DanceMirror 2026-10-06: "테스터 확인불가 —
        #    테스터 추가 필요" while the group held 2 testers), so it gets one retry and,
        #    if still unreadable, an alert that says "lookup failed", never "add testers".
        g = None
        for _ in range(2):
            g = run(group_list_argv(config), cwd=str(repo_dir), timeout=_FIREBASE_TIMEOUT)
            count = parse_tester_count(getattr(g, "stdout", "") or "", config["group"])
            if count is not None:
                break
        if count is None:
            detail = (getattr(g, "stderr", "") or getattr(g, "stdout", "") or "").strip()[-200:]
            msg = compose_alert(
                f"⚠️ [베타 보류] {repo_name} 테스터 수 조회 실패({config['group']}) — "
                f"Firebase CLI 일시 오류일 수 있음, 다음 발행/스윕에서 재시도 "
                f"(main {merge_sha[:9]}, rc={getattr(g, 'returncode', None)})"
                + (f"\n{detail}" if detail else ""))
            progress(msg)
            notify(msg)
            return msg                           # handled abort: no distribute, no watermark bump
        if count == 0:
            msg = compose_alert(
                f"🚫 [베타 중단] {repo_name} 테스터 0({config['group']}) — "
                f"콘솔에서 테스터 추가가 필요합니다 (main {merge_sha[:9]})")
            progress(msg)
            notify(msg)
            return msg                           # handled abort: no distribute, no watermark bump

        # 3) Release notes (deterministic; migration → backup warning first line).
        #    diff_base is None unless the caller asked for cumulative notes AND a
        #    previous publish exists to measure from — so the merge-time path diffs
        #    the same <sha>^..<sha> range it always did. The range is first checked
        #    (and fetched once if missing); an unresolvable range skips the probe
        #    with a warning line rather than aborting the publish.
        diff_base = entry.get("last_published_sha") if cumulative_notes else None
        if ensure_probe_range(repo_dir, merge_sha, run=run, diff_base=diff_base):
            migration = detect_migration(repo_dir, merge_sha, config.get("migration_globs") or [],
                                         run=run, diff_base=diff_base)
        else:
            progress(f"beta-publish: migration probe skipped — {merge_sha[:9]} not in clone")
            migration = False
        subjects = collect_commit_subjects(repo_dir, diff_base, merge_sha, run=run) if diff_base else []
        notes = build_release_notes(payload, migration=migration, subjects=subjects)
        notes_file = _write_notes(notes, notes_dir, repo_name)

        # 4) Distribute.
        apk = str(repo_dir / config["apk_path"])
        d = run(distribute_argv(config, apk, notes_file), cwd=str(repo_dir), timeout=_FIREBASE_TIMEOUT)
        if getattr(d, "returncode", 1) != 0:
            raise BetaPublishError(
                f"{repo_name} distribute failed (rc={getattr(d, 'returncode', None)}): "
                f"{(getattr(d, 'stderr', '') or '')[:300]}")
        release_id = parse_release_id(getattr(d, "stdout", "") or "")

        # 5) Record: bump the watermark, remember the sha, commit the canon (still
        #    under the lock so a sibling merge of this app reads the bumped value).
        entry["version_code"] = next_vc
        entry["last_published_sha"] = merge_sha
        shas = entry.setdefault("published_shas", [])
        shas.append(merge_sha)
        del shas[:-100]                          # keep the dedup tail bounded
        _save_watermarks(watermark_path, wm)
        if commit_watermark:
            _commit_watermark(watermark_path, repo_name, next_vc, vault_root=vault_root,
                              run=run, progress=progress)

    note = f"베타 {next_vc} 발행 · 릴리즈 {release_id or '-'} · main {merge_sha[:9]}"
    if task_path is not None:
        append_progress_log(task_path, note)
    progress(note)
    return note


# ---------------------------------------------------------------------------
# Catch-up sweep — discovery + per-sha failure backoff
# ---------------------------------------------------------------------------
# maybe_publish only ever runs at the instant a worker auto-merge lands, so a
# main that arrived any other way — config entry momentarily missing, a publish
# that raised, a human merge — stayed unpublished until the NEXT worker merge of
# that app (DanceMirror 35cdd60, 2026-09-25: the watermark said "never"). The
# sweep asks each configured repo what origin/<default> is today and publishes
# whatever the watermark has not seen.


def find_unpublished(
    config: dict[str, dict],
    watermarks: dict,
    *,
    ls_remote: Callable[[str], str | None],
    progress: Callable[[str], None] = lambda _m: None,
) -> list[dict]:
    """Configured repos whose ``origin/<default>`` tip was never published.

    Pure: ``ls_remote(repo_name) -> sha | None`` is the only outside answer, so
    tests drive this with a dict. ``config`` is :func:`load_config`'s map and
    ``watermarks`` the whole watermark document (as :func:`_load_watermarks`
    returns it). Iterates in config order, so a per-sweep cap always takes the
    same repo first. Every uncertainty — no watermark entry, no answer from the
    remote — skips rather than guesses: the merge-time path is still the primary
    publisher and a missed sweep costs one interval, a wrong one costs a release.
    """
    wm_repos = (watermarks or {}).get("repos", {}) if isinstance(watermarks, dict) else {}
    out: list[dict] = []
    for repo_name in config:
        entry = wm_repos.get(repo_name)
        if entry is None:
            progress(f"beta-sweep: {repo_name} is configured but has no watermark entry — skip")
            continue
        sha = ls_remote(repo_name)
        if not sha:
            progress(f"beta-sweep: {repo_name} origin HEAD unavailable — skip")
            continue
        if already_published(entry, sha):
            continue
        out.append({"repo": repo_name, "sha": sha, "base": entry.get("last_published_sha")})
    return out


def _load_sweep_state(path: Path | str) -> dict:
    """Sweep runtime state, or ``{}`` for anything unreadable (same fail-safe as
    load_config: a corrupt state file must only ever cost one extra attempt)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_sweep_state(path: Path | str, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def in_backoff(state_entry: dict | None, sha: str, now: float,
               window: float = SWEEP_FAIL_BACKOFF_S) -> bool:
    """Is this repo still serving a failure cooldown for THIS sha?

    Per-sha on purpose: a new main is new evidence and retries at once, while a
    sha that already failed does not burn a ~30-min gradle build every 15 min.
    """
    if not state_entry or state_entry.get("sha") != sha:
        return False
    failed_ts = state_entry.get("failed_ts")
    try:
        return float(failed_ts) + window > now
    except (TypeError, ValueError):
        return False


def _default_branch(repo_dir: Path | str, *, run: Runner = _run) -> str:
    """The checkout's default branch, ``main`` when git cannot say.

    Same query as ``stages.default_branch``, re-done here against the injected
    ``run`` seam — that one shells out directly, and this module's contract is
    that every external effect is fakeable by the unit suite.
    """
    r = run(["git", "-C", str(repo_dir), "symbolic-ref", "refs/remotes/origin/HEAD"],
            timeout=_GIT_TIMEOUT)
    out = (getattr(r, "stdout", "") or "").strip()
    if getattr(r, "returncode", 1) == 0 and out:
        return out.rsplit("/", 1)[-1]
    return "main"


def _remote_owner() -> str | None:
    """GitHub owner for the no-checkout ls-remote fallback: the vault's own
    ``repo_slug`` (env ``MACP_REPO_SLUG`` → ``.macp/config.toml``). ``None`` when
    only the neutral default resolves — guessing an org would ask someone else's
    repo. Reads ``_VAULT_DATA_ROOT`` at call time so tests can redirect it.
    """
    slug = paths.repo_slug(start=_VAULT_DATA_ROOT)
    if slug == paths._DEFAULT_REPO_SLUG or "/" not in slug:
        return None
    return slug.split("/", 1)[0] or None


def _ls_remote_sha(repo_name: str, *, worker_root: Path | str = repos.DEFAULT_WORKER_ROOT,
                   run: Runner = _run) -> str | None:
    """``origin/<default>``'s sha for a configured repo, in one ls-remote.

    Asks through the local checkout (``git -C <checkout> ls-remote origin``) so no
    URL has to be reconstructed and the config schema stays untouched; falls back
    to the vault-configured owner (``repo_slug``) when there is no checkout (the
    sweep skips such a repo anyway — it has nowhere to build — but discovery still
    reports it), and to ``None`` when no owner is configured.
    ``None`` for any failure: no answer is never read as "up to date".
    """
    checkout = Path(worker_root) / repo_name
    if (checkout / ".git").exists():
        branch = _default_branch(checkout, run=run)
        argv = ["git", "-C", str(checkout), "ls-remote", "origin", f"refs/heads/{branch}"]
    else:
        owner = _remote_owner()
        if owner is None:
            return None
        url = f"https://github.com/{owner}/{repo_name}.git"
        argv = ["git", "ls-remote", url, "refs/heads/main"]
    r = run(argv, timeout=_GIT_TIMEOUT)
    if getattr(r, "returncode", 1) != 0:
        return None
    m = re.match(r"^([0-9a-f]{40})\b", (getattr(r, "stdout", "") or "").strip())
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Catch-up sweep — worktree-isolated publish
# ---------------------------------------------------------------------------

def _publish_in_worktree(
    candidate: dict,
    checkout: Path | str,
    *,
    worktree_root: Path | str,
    run: Runner,
    publish: Callable[..., str | None],
    publish_kw: dict,
    payload: dict | None = None,
) -> str | None:
    """Build+publish ``candidate`` from a throwaway worktree of its checkout.

    ``~/Worker/<repo>`` is the RUNNER's working tree — a stage may be mid-run in
    it — so the sweep must not check anything out there. ``git worktree add
    --detach`` leaves that tree's HEAD and index untouched and gives the build its
    own directory, removed in ``finally`` (``rm -rf`` is a hard-rule no).

    ``local.properties`` is carried over by hand: it is gitignored, so a fresh
    worktree has none, and without it every Android build here fails to find the
    SDK.
    """
    repo_name, sha = candidate["repo"], candidate["sha"]
    checkout = Path(checkout)
    branch = _default_branch(checkout, run=run)
    f = run(["git", "-C", str(checkout), "fetch", "origin", branch], timeout=_FETCH_TIMEOUT)
    if getattr(f, "returncode", 1) != 0:
        raise BetaPublishError(
            f"{repo_name} fetch origin/{branch} failed (rc={getattr(f, 'returncode', None)})")

    # pid suffix: the detached sweep and a merge-time publish of the same sha run
    # in different processes and must not collide on one worktree path.
    wt = Path(worktree_root) / f"{repo_name}-{sha[:9]}-{os.getpid()}"
    Path(worktree_root).mkdir(parents=True, exist_ok=True)
    a = run(["git", "-C", str(checkout), "worktree", "add", "--detach", str(wt), sha],
            timeout=_GIT_TIMEOUT)
    if getattr(a, "returncode", 1) != 0:
        raise BetaPublishError(
            f"{repo_name} worktree add {sha[:9]} failed (rc={getattr(a, 'returncode', None)}): "
            f"{(getattr(a, 'stderr', '') or '')[:200]}")
    try:
        local_props = checkout / "local.properties"
        if local_props.is_file() and not (wt / "local.properties").exists():
            with contextlib.suppress(OSError):
                shutil.copyfile(local_props, wt / "local.properties")
        if payload is None:
            payload = {"task_id": f"catch-up {repo_name}"}
        return publish(repo_name, wt, sha, payload, **publish_kw)
    finally:
        run(["git", "-C", str(checkout), "worktree", "remove", "--force", str(wt)],
            timeout=_GIT_TIMEOUT)
        run(["git", "-C", str(checkout), "worktree", "prune"], timeout=_GIT_TIMEOUT)


def publish_merge_in_worktree(
    repo_name: str,
    checkout: Path | str,
    merge_sha: str,
    payload: dict,
    *,
    worktree_root: Path | str = SWEEP_WORKTREE_DIR,
    run: Runner = _run,
    publish: Callable[..., str | None] = maybe_publish,
    **publish_kw,
) -> str | None:
    """Merge-time publish, built from a worktree pinned to ``merge_sha``.

    The runner's checkout is NOT at the merge sha when a PR lands: after a
    squash merge it sits on the feature branch or on a main that was never
    pulled. Building there shipped whatever that tree held under the merge
    sha's name (2026-10-05, if-then-habits #124 built at #121, before the #123
    launcher-icon fix → AAPT ``mipmap/ic_launcher not found``).
    """
    publish_kw.setdefault("run", run)
    return _publish_in_worktree(
        {"repo": repo_name, "sha": merge_sha}, checkout, worktree_root=worktree_root,
        run=run, publish=publish, publish_kw=publish_kw, payload=payload)


def sweep_unpublished(
    *,
    config_path: Path | str = CONFIG_PATH,
    watermark_path: Path | str = WATERMARK_PATH,
    lock_dir: Path | str = LOCK_DIR,
    state_path: Path | str = SWEEP_STATE_PATH,
    worktree_root: Path | str = SWEEP_WORKTREE_DIR,
    worker_root: Path | str = repos.DEFAULT_WORKER_ROOT,
    ls_remote: Callable[[str], str | None] | None = None,
    publish: Callable[..., str | None] = maybe_publish,
    run: Runner = _run,
    notify: Callable[[str], None] = _worker.kakao_notify,
    progress: Callable[[str], None] = lambda _m: None,
    now: float | None = None,
    max_publishes: int = 1,
) -> list[str]:
    """Publish every configured repo whose main was never published — capped.

    Runs OUT of process (see :func:`spawn_sweep_if_due`): one call can contain a
    ~30-min gradle build, which must never sit inside an orchestrator tick.

    Returns the notes of the publishes it completed. Never raises: one repo's
    failure is recorded (backoff + a single kakao alert per sha) and the sweep
    moves on, because a repo that cannot build must not keep the others
    unpublished — that is the very failure this sweep exists to end.

    ``max_publishes`` is an ATTEMPT cap, not a success cap: the cost being
    rationed is the gradle build, which a failure spends just as fully.
    """
    now = time.time() if now is None else now
    resolve_sha = ls_remote or (
        lambda name: _ls_remote_sha(name, worker_root=worker_root, run=run))

    config = load_config(config_path)
    try:
        watermarks = _load_watermarks(watermark_path)
    except (OSError, json.JSONDecodeError) as e:
        progress(f"beta-sweep: watermark canon unreadable ({e}) — nothing swept")
        return []

    candidates = find_unpublished(config, watermarks, ls_remote=resolve_sha, progress=progress)
    state = _load_sweep_state(state_path)
    repo_state: dict = state.setdefault("repos", {})
    published: list[str] = []
    attempts = 0

    for cand in candidates:
        name, sha = cand["repo"], cand["sha"]
        if attempts >= max_publishes:
            progress(f"beta-sweep: {name} main {sha[:9]} deferred to the next sweep "
                     f"(cap {max_publishes}/sweep)")
            continue
        prev = repo_state.get(name)
        if in_backoff(prev, sha, now):
            progress(f"beta-sweep: {name} main {sha[:9]} failed recently — in backoff, skip")
            continue
        checkout = Path(worker_root) / name
        if not (checkout / ".git").exists():
            progress(f"beta-sweep: {name} has no checkout under {worker_root} — skip "
                     f"(the sweep never clones)")
            continue

        attempts += 1
        try:
            note = _publish_in_worktree(
                cand, checkout, worktree_root=worktree_root, run=run, publish=publish,
                publish_kw={"progress": progress, "config_path": config_path,
                            "watermark_path": watermark_path, "lock_dir": lock_dir,
                            "run": run, "notify": notify, "cumulative_notes": True})
        except Exception as e:                   # noqa: BLE001 — contained to this repo
            alerted = (prev or {}).get("alerted_sha")
            repo_state[name] = {"sha": sha, "failed_ts": now, "alerted_sha": alerted}
            msg = compose_alert(
                f"❌ [베타 catch-up 실패] {name} main {sha[:9]} — {SWEEP_FAIL_BACKOFF_S // 3600}시간 후 재시도",
                tail=f"{type(e).__name__}: {e}")
            progress(msg)
            if alerted != sha:                   # one alert per sha, not per sweep
                with contextlib.suppress(Exception):
                    notify(msg)
                repo_state[name]["alerted_sha"] = sha
            continue
        repo_state.pop(name, None)               # success clears the failure record
        if note:
            published.append(note)

    state["last_sweep_ts"] = now                 # informational; the gate's timer
                                                 # is last_tick_ts, set at spawn
    if state.get("pid") == os.getpid():          # this process is done; free the gate
        state.pop("pid", None)
        state.pop("pid_ts", None)
    _save_sweep_state(state_path, state)
    return published


# ---------------------------------------------------------------------------
# Catch-up sweep — tick gate + CLI entry point
# ---------------------------------------------------------------------------

def _pid_alive(pid: int) -> bool:
    """Signal-0 liveness probe. Non-positive pids are never probed: ``os.kill``
    reads 0/-1 as "every process in my group / every process I own", and a
    fixture that once passed -1 killed the whole desktop session
    (orchestrator/tests/conftest.py tells that story)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _vault_head_branch(vault_root: Path | str, *, run: Runner = _run) -> str:
    r = run(["git", "-C", str(vault_root), "rev-parse", "--abbrev-ref", "HEAD"],
            timeout=_GIT_TIMEOUT)
    if getattr(r, "returncode", 1) != 0:
        return ""                                # unknown → the caller refuses
    return (getattr(r, "stdout", "") or "").strip()


# How long the tick waits to learn whether ``systemd-run`` itself failed. A
# missing user bus fails well under a second; a live sweep is still in ls-remote.
_SCOPE_PROBE_S = 1.0


def _spawn_detached(log_path: Path | str = SWEEP_LOG_PATH, *,
                    scope_probe_s: float = _SCOPE_PROBE_S) -> int:
    """Start ``python -u -m todo_system.orchestrator.beta_publish sweep`` detached.

    ``start_new_session=True`` cuts it loose from the orchestrator's process
    group, so a signal to the orchestrator does not take the build down
    mid-distribute. But a new session is not a new cgroup. On the cloud host the
    tick runs as a timer-driven ``Type=oneshot`` unit with KillMode=control-group,
    and when the tick exits — seconds after spawning — systemd kills every process
    left in the unit's cgroup, the ~30-min gradle sweep included. On 2026-10-08 the
    spawns at 15:49, 16:07 and 17:28 each died within ~4 s of their tick finishing,
    leaving a 0-byte log and a stale ``last_sweep_ts``. That is the bug
    :func:`jobs._scope_prefix` fixed for long_job (there the runner restart was the
    killer), so under systemd the child runs in its own transient scope.
    ``systemd-run --scope`` execs the command, so the Popen pid is the sweep's pid
    and the ``_pid_alive`` gate keeps working. If the scope launch fails (no user
    bus, non-zero exit within ``scope_probe_s``) we fall back to the plain detached
    spawn. A sweep that crashes non-zero inside the probe window is then run a
    second time; both runs are logged, and ``app_lock`` plus ``already_published``
    still prevent a double publish.

    Output appends to a log file — a detached process has nowhere else to speak —
    and is unbuffered (``-u`` and ``PYTHONUNBUFFERED``): a block-buffered stdout
    lost everything the killed child had printed. The parent writes a "spawning"
    line first, so even a child that dies before its first byte leaves a trace.
    The cwd is the vault-data root: an installed wheel's child resolves the vault
    by the cwd-upward search when no env is set, and in the combined layout it
    is the same directory as the engine root, so ``python -m`` still imports the
    checkout exactly as before.
    """
    log = Path(log_path)
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-u", "-m", "todo_system.orchestrator.beta_publish", "sweep"]
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    prefix = jobs._scope_prefix()
    with open(log, "a", encoding="utf-8") as fh:
        def note(msg: str) -> None:
            ts = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            fh.write(f"[{ts}] beta-sweep: {msg}\n")
            fh.flush()

        def popen(cmd: list[str]) -> subprocess.Popen:
            return subprocess.Popen(             # noqa: S603 — fixed argv, no shell
                cmd, cwd=str(_VAULT_DATA_ROOT), stdin=subprocess.DEVNULL,
                stdout=fh, stderr=subprocess.STDOUT, start_new_session=True, env=env)

        note(f"spawning (scope={'yes' if prefix else 'no'})")
        if prefix:
            try:
                p = popen(prefix + argv)
                rc = p.wait(timeout=scope_probe_s)
            except subprocess.TimeoutExpired:
                return p.pid                     # running inside its own scope
            except OSError as e:
                note(f"scope launch failed (OSError {e}) — falling back to a plain "
                     f"detached spawn")
            else:
                if rc == 0:
                    return p.pid                 # already finished; the gate sees a dead pid
                note(f"scope launch failed (rc={rc}) — falling back to a plain "
                     f"detached spawn")
        p = popen(argv)
    return p.pid


def spawn_sweep_if_due(
    *,
    state_path: Path | str = SWEEP_STATE_PATH,
    min_interval_s: float = SWEEP_MIN_INTERVAL_S,
    vault_root: Path | str = _VAULT_DATA_ROOT,
    config_path: Path | str | None = None,
    now: float | None = None,
    head_branch: str | None = None,
    spawn: Callable[[], int] | None = None,
    is_alive: Callable[[int], bool] | None = None,
    run: Runner = _run,
    progress: Callable[[str], None] = lambda _m: None,
) -> bool:
    """Start a detached catch-up sweep if one is due. Returns whether it started.

    This is the whole of what a tick does — it must return in milliseconds, so
    even the ls-remote discovery lives in the child process.

    Four gates:

    * **config present** — beta publishing is an author-only opt-in: an external
      checkout with no (or empty) ``<vault>/.macp/beta_publish_config.json`` has
      nothing to sweep, so we return before any ``progress`` line or state write.
      This keeps a bare clone's every tick silent rather than printing the HEAD
      line below. Judged on the passed ``vault_root`` (not the import-time
      ``CONFIG_PATH``, which is pinned to ``_VAULT_DATA_ROOT``).
    * **min-poll** — at most one sweep per ``min_interval_s``, so a ~5-min tick
      does not queue ls-remotes it cannot use.
    * **vault HEAD is main** — a successful publish commits the watermark canon in
      the vault repo. Doing that while HEAD sits on ``worker/<slug>`` strands the
      canon on a feature branch (the live-vault-head trap), so an off-main HEAD
      means "not now" rather than "commit anyway".
    * **no sweep already running** — a recorded pid that is still alive and
      younger than a full build blocks a second one. Past that age the record is
      treated as stale (pids get recycled; a wedged record must not disable the
      sweep forever). Even if two ever overlap, ``app_lock`` plus the
      re-check of ``already_published`` inside it stop a double publish.
    """
    now = time.time() if now is None else now
    spawn = spawn or _spawn_detached
    is_alive = is_alive or _pid_alive
    if config_path is None:
        config_path = Path(vault_root) / ".macp" / "beta_publish_config.json"
    if not load_config(config_path):
        return False                             # no author config → silent skip
    state = _load_sweep_state(state_path)

    last = state.get("last_tick_ts")
    try:
        if last is not None and float(last) + min_interval_s > now:
            return False
    except (TypeError, ValueError):
        pass                                     # unreadable timer → treat as due

    head = head_branch if head_branch is not None else _vault_head_branch(vault_root, run=run)
    if head != "main":
        progress(f"beta-sweep: vault HEAD is {head or 'unknown'}, not main — not sweeping "
                 f"(the watermark commit would land off main)")
        return False

    pid, pid_ts = state.get("pid"), state.get("pid_ts")
    if pid and pid_ts is not None:
        try:
            fresh = float(pid_ts) + _BUILD_TIMEOUT + 600 > now
        except (TypeError, ValueError):
            fresh = False
        if fresh and is_alive(pid):
            progress(f"beta-sweep: a sweep (pid {pid}) is still running — skip")
            return False

    new_pid = spawn()
    state["pid"] = new_pid
    state["pid_ts"] = now
    state["last_tick_ts"] = now
    _save_sweep_state(state_path, state)
    progress(f"beta-sweep: started (pid {new_pid})")
    return True


def _log(msg: str) -> None:
    """One timestamped, flushed line — a killed child keeps everything said so far."""
    print(f"[{dt.datetime.now().astimezone().isoformat(timespec='seconds')}] {msg}",
          flush=True)


def _on_sigterm(signum, _frame) -> None:
    _log(f"beta-sweep: pid {os.getpid()} received signal {signum} — exiting "
         f"(a cgroup kill under KillMode=control-group looks like this)")
    # SystemExit unwinds through subprocess.run (which kills its gradle child)
    # and lets main()'s finally write the end line.
    raise SystemExit(128 + signum)


def main(argv: list[str] | None = None, *, handle_signals: bool | None = None) -> int:
    """``python -m todo_system.orchestrator.beta_publish sweep`` — the detached
    entry point the tick spawns. Logs a start line, progress, and an end line
    (and a traceback or the SIGTERM that ended it), each flushed at once. Exit
    code is 0 unless the sweep itself crashed (1); a repo that failed is recorded
    as backoff + alert, not as a process failure.

    The SIGTERM handler is only installed for the real CLI entry (``argv is
    None``), so tests never touch process-wide handlers."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] != ["sweep"] or len(args) > 1:
        print("usage: python -m todo_system.orchestrator.beta_publish sweep")
        return 2
    if handle_signals is None:
        handle_signals = argv is None
    if handle_signals:
        signal.signal(signal.SIGTERM, _on_sigterm)

    cgroup = "-"
    with contextlib.suppress(OSError):
        cgroup = (Path("/proc/self/cgroup").read_text(encoding="utf-8").splitlines()
                  or ["-"])[0]
    _log(f"beta-sweep: start pid={os.getpid()} exe={sys.executable} cwd={os.getcwd()} "
         f"cgroup={cgroup}")
    t0 = time.time()
    rc = 0
    try:
        for note in sweep_unpublished(progress=_log):
            _log(note)
    except Exception:                            # noqa: BLE001 — logged, then exit 1
        _log("beta-sweep: crashed\n" + traceback.format_exc().rstrip())
        rc = 1
    except BaseException as e:                   # SystemExit / KeyboardInterrupt
        rc = getattr(e, "code", 1)
        _log(f"beta-sweep: interrupted ({type(e).__name__})")
        raise
    finally:
        _log(f"beta-sweep: end pid={os.getpid()} rc={rc} elapsed={int(time.time() - t0)}s")
    return rc


if __name__ == "__main__":                       # pragma: no cover - CLI wrapper
    raise SystemExit(main())
