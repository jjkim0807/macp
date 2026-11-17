"""The engine's vault git lock (`todo_system/vault_git_lock.py`).

Four engine sequences — orchestrator `_pull_main`, worker `sync_vault_state`
and the git mailbox's pull and commit+push — run git in the live vault from
three daemons. 2026-11-17 one of them interleaved with another and published a
revert nobody made. These drive real git repos and real flocks: the failure is
about which process gets to run git when, which a mock cannot show.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from todo_system import vault_git_lock as vgl


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "seed").write_text("x", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "seed")
    return path


@contextlib.contextmanager
def _lock_held_elsewhere(root):
    """Hold `root`'s lock through a separate open file description — the way
    another daemon holds it. flock contends between two opens even inside one
    thread, which is what makes this a faithful stand-in for that daemon."""
    path = vgl.lock_path(root)
    assert path is not None, f"{root} resolves no lock — nothing would be held"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield path
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# Another process holding the lock: flock, say so, hold until stdin closes.
_HOLDER = (
    "import fcntl, os, sys\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "fcntl.flock(fd, fcntl.LOCK_EX)\n"
    "print('held', flush=True)\n"
    "sys.stdin.read()\n"
)


class VaultGitLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = _init_repo(self.tmp / "repo")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lock_file_lives_in_the_git_common_dir(self):
        # One lock per repository: the root, a subdirectory (where a mailbox
        # root sits) and a linked worktree must all land on the same file, or
        # two callers would each "hold" a different lock and exclude nothing.
        sub = self.repo / "Worker" / "agents"
        sub.mkdir(parents=True)
        wt = self.tmp / "wt"
        _git(self.repo, "worktree", "add", "-q", "-b", "side", str(wt))
        git_dir = self.repo / ".git"
        for root in (self.repo, sub, wt):
            with self.subTest(root=root):
                path = vgl.lock_path(root)
                self.assertIsNotNone(path)
                self.assertEqual(path.name, vgl.LOCK_FILENAME)
                # samefile, not string equality: macOS tmp dirs are symlinked
                self.assertTrue(os.path.samefile(path.parent, git_dir))
        # and taking it through one of them excludes the others
        with vgl.vault_git_lock(wt, 1) as outer:
            self.assertTrue(outer)
            self.assertTrue((git_dir / vgl.LOCK_FILENAME).exists())
            with vgl.vault_git_lock(sub, 0.2) as inner:
                self.assertFalse(inner)
        # inside .git: never a working-tree change
        self.assertEqual(_git(self.repo, "status", "--porcelain").stdout, "")

    def test_a_second_holder_times_out_with_false(self):
        with _lock_held_elsewhere(self.repo):
            t0 = time.monotonic()
            with vgl.vault_git_lock(self.repo, 0.2) as held:
                elapsed = time.monotonic() - t0
                self.assertFalse(held)
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertLess(elapsed, 5)

    def test_nested_acquire_in_one_thread_is_not_reentrant(self):
        # Pins why no lock-holding function may call another one: the inner
        # acquire waits on its own outer holder and gives up.
        with vgl.vault_git_lock(self.repo, 1) as outer:
            self.assertTrue(outer)
            with vgl.vault_git_lock(self.repo, 0.2) as inner:
                self.assertFalse(inner)
        with vgl.vault_git_lock(self.repo, 0) as again:
            self.assertTrue(again)                # the outer release freed it

    def test_released_on_exception_and_file_kept(self):
        path = vgl.lock_path(self.repo)
        with self.assertRaises(RuntimeError):
            with vgl.vault_git_lock(self.repo, 1) as held:
                self.assertTrue(held)
                ino = path.stat().st_ino
                raise RuntimeError("boom")
        with vgl.vault_git_lock(self.repo, 0) as held:
            self.assertTrue(held)
        # never unlinked: a fresh inode would let two holders coexist
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_ino, ino)

    def test_non_repo_and_missing_root_fail_open(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        # A ceiling keeps git from walking up into whatever repository might
        # enclose the tmp dir on some host (a stray /tmp/.git exists on one).
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.tmp)}):
            for root in (plain, Path("/nonexistent/x")):
                with self.subTest(root=root):
                    self.assertIsNone(vgl.lock_path(root))
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        with vgl.vault_git_lock(root, 0.2) as held:
                            self.assertTrue(held)
                    self.assertEqual(out.getvalue() + err.getvalue(), "")
        self.assertEqual(list(plain.iterdir()), [])  # no lock file littered there

    def test_an_unresolvable_lock_fails_open(self):
        # no git on PATH, or git failing: run unlocked rather than not at all
        for boom in (FileNotFoundError("git"),
                     subprocess.TimeoutExpired(["git"], 10)):
            with self.subTest(boom=type(boom).__name__), \
                 mock.patch.object(vgl, "_resolve_run", side_effect=boom):
                self.assertIsNone(vgl.lock_path(self.repo))
                with vgl.vault_git_lock(self.repo, 0) as held:
                    self.assertTrue(held)

    def test_resolving_the_lock_bypasses_a_patched_subprocess_run(self):
        # Callers' tests count their git sequence exactly — through an injected
        # `_run`, or by replacing the global subprocess.run — and the lock is
        # not part of that sequence, so neither may ever see its rev-parse.
        seen = []

        def recorder(*args, **kwargs):
            seen.append(args)
            return subprocess.CompletedProcess(args, 1, "", "")

        with mock.patch.object(subprocess, "run", side_effect=recorder):
            path = vgl.lock_path(self.repo)
            with vgl.vault_git_lock(self.repo, 0) as held:
                self.assertTrue(held)
        self.assertEqual(seen, [])
        self.assertIsNotNone(path)
        self.assertTrue(os.path.samefile(path.parent, self.repo / ".git"))

    def test_a_waiter_gets_the_lock_when_the_holder_releases(self):
        # AC6's "or waits": a holder that finishes inside the timeout is waited
        # for, not skipped.
        path = vgl.lock_path(self.repo)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)

        def release_later():
            time.sleep(0.3)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        t = threading.Thread(target=release_later)
        t.start()
        try:
            t0 = time.monotonic()
            with vgl.vault_git_lock(self.repo, 5) as held:
                waited = time.monotonic() - t0
                self.assertTrue(held)
        finally:
            t.join()
        self.assertGreaterEqual(waited, 0.25)
        self.assertLess(waited, 5)

    def test_another_process_holding_the_lock_blocks(self):
        # The real shape of the exclusion: separate daemons, separate processes.
        path = vgl.lock_path(self.repo)
        holder = subprocess.Popen([sys.executable, "-I", "-c", _HOLDER, str(path)],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            with vgl.vault_git_lock(self.repo, 0.2) as held:
                self.assertFalse(held)
        finally:
            holder.stdin.close()
            holder.wait(timeout=60)
            holder.stdout.close()
        # the kernel released it with the process — nothing stale to clean up
        with vgl.vault_git_lock(self.repo, 5) as held:
            self.assertTrue(held)


if __name__ == "__main__":
    unittest.main()
