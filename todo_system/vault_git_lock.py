"""One flock per vault repository, held around each engine git sequence.

Four engine code paths run multi-step git sequences in the live vault checkout,
from three daemons: the orchestrator's `_pull_main` (fetch → merge), the
worker's `sync_vault_state` (guards → add → commit → push/rebase), and the git
mailbox's `_pull` and `_commit_push`. Each is safe on its own; none is safe
interleaved with another. 2026-11-17: a tick's `merge --autostash` stashed the
dirty tree and reset it to the HEAD of that moment; within the same second this
host's sync committed, the merge failed, its abort failed, and the stash was
never re-applied. HEAD kept a just-committed task md while the disk went back
to the bytes from before it, and the next sync commit published that as a
revert nobody made. This module is the lock those four sequences now share.

Why `fcntl.flock`:
  * it locks an open file description, so two `open()`s in one process — two
    threads, or a nested acquire — contend exactly like two processes do.
    `lockf` / `fcntl(F_SETLK)` locks belong to the whole process and would wave
    a second thread straight through;
  * the kernel releases it when the holder exits or dies, so a crash leaves no
    stale lock to clean up. `orchestrator/lease.py`'s O_EXCL lease file is a
    different mechanism for a different job; do not mix the two.

Where: ``<git-common-dir>/macp-vault-git.lock``. One file per repository — its
root, any subdirectory and every linked worktree resolve to the same file — and
inside the git dir, so it never shows in ``git status`` and no ``git add -A``
can sweep it up.

Never unlink the lock file, not even to "clean up". A waiter blocked on the old
inode and a newcomer that creates a fresh file would both hold "the" lock. An
empty file in the git dir costs nothing.

Not re-entrant. A holder that calls another function which takes this lock
waits on itself until the timeout and then skips. Code inside the ``with`` runs
git directly and calls no other lock-taking function.

Fail-open: no root, no repository, no ``fcntl`` (Windows), a lock file that
cannot be opened, or a flock that fails for any reason other than contention —
the body runs unlocked, silently. Every caller ran unlocked until this module
existed, and a lock that cannot be resolved must not stop the vault syncing.
Only contention past the timeout says "skip".

The module prints nothing; callers log a busy skip under their own prefix.
Stdlib-only.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import time
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock: run unlocked
    fcntl = None  # type: ignore[assignment]

LOCK_FILENAME = "macp-vault-git.lock"

# Wall-clock ceiling on the `git rev-parse --git-common-dir` that locates the
# lock: a wedged git must not wedge the tick that asked. Read at call time.
RESOLVE_TIMEOUT_S = 10.0

# The lock's own git runs through this reference, bound at import — not through
# a caller's `_run` / `_git` seam, and not through `subprocess.run` looked up at
# call time. The lock is not part of the sequence it guards, and callers' tests
# count that sequence's git calls exactly, by injecting a fake or by replacing
# the global `subprocess.run`. Tests of this module patch `_resolve_run` itself.
_resolve_run = subprocess.run


def lock_path(root) -> Path | None:
    """``<git-common-dir>/macp-vault-git.lock`` for the repository holding
    `root`, or None when there is none to lock: `root` is not a directory, it
    is not inside a repository git will work in, or git cannot say. Never
    raises.
    """
    try:
        root = Path(root)
        if not root.is_dir():
            return None
        r = _resolve_run(["git", "rev-parse", "--git-common-dir"], cwd=root,
                         capture_output=True, text=True,
                         timeout=RESOLVE_TIMEOUT_S)
        out = (r.stdout or "").strip()
        if r.returncode != 0 or not out:
            return None
        # The answer may be relative to the cwd git ran in (".git", or
        # "../../.git" from a subdirectory): resolve it against that cwd, so
        # every caller of one repository lands on the same file.
        return (root / out).resolve() / LOCK_FILENAME
    except Exception:  # noqa: BLE001 - an unresolvable lock fails open
        return None


def _open_lock_file(root) -> int | None:
    if fcntl is None:
        return None
    path = lock_path(root)
    if path is None:
        return None
    try:
        # os.open fds are non-inheritable (PEP 446), so no git child spawned
        # inside the `with` can carry the lock past our release.
        return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None


def _acquire(fd: int, timeout: float, poll_s: float) -> bool | None:
    """True once held, False if still contended at the deadline, None if flock
    itself is unusable here (e.g. a filesystem without lock support)."""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            pass                         # someone else holds it
        except OSError:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll_s, remaining))


@contextlib.contextmanager
def vault_git_lock(root, timeout: float, *, poll_s: float = 0.05) -> Iterator[bool]:
    """Hold the engine git lock of the repository at `root` for the body.

    Yields True when the body may run git — the lock is held, or it could not
    be resolved at all (fail-open, see the module docstring) — and False when
    another holder kept it for `timeout` seconds. On False the caller runs no
    git, logs one line under its own prefix, and leaves the work to its next
    tick. `timeout <= 0` tries exactly once. Raises nothing of its own; an
    exception from the body propagates after the lock is released.
    """
    fd = _open_lock_file(root)
    if fd is None:
        yield True
        return
    try:
        held = _acquire(fd, timeout, poll_s)
        if held is None:
            yield True                   # fail-open: flock unusable, run unlocked
            return
        if not held:
            yield False
            return
        try:
            yield True
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
