"""The maintenance-tick cache prune (todo_system/maintenance_disk.py).

Every seam is injected: a fake home in a tmp dir (Gradle caches, wrapper
dists, ~/Worker wrappers, the protected neighbours), a fake ledger and jobs dir
patched into disk_guard's seams, a fake disk whose free space really rises as
files under the fake home disappear (1 byte = 1 MiB), a fake `run` that answers
only `uv cache prune`, a fake /proc and a fake clock. Nothing here touches the
real ~/.gradle, ~/.cache/uv, the ledger or a real uv. The suite-wide
`_disk_prune_isolated` fixture (todo_system/conftest.py) backs that up, and
SuiteIsolationTest pins that it is live.

The safety cases assert the positive half too — an allowlisted cache IS
deleted — so a prune that refused everything could not pass them by doing
nothing.
"""

from __future__ import annotations

import ast
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from todo_system import maintenance_disk as md
from todo_system.orchestrator import disk_guard as dg
from todo_system.orchestrator.assignment import Assignment

G = md.GIB
MIB = 1024 ** 2
DAY = 86400
REPO = Path(__file__).resolve().parents[2]
HASH = "{:032x}"
UV = "/fake/bin/uv"


class FakeDisk:
    """free = base + extra + MiB × (bytes deleted under the fake home)."""

    def __init__(self, home: Path, base: int, total: int = 96 * G):
        self.home, self.base, self.total, self.extra = home, base, total, 0
        self.initial = self.size()
        self.calls: list[str] = []

    def size(self) -> int:
        n = 0
        for dirpath, _dirs, files in os.walk(self.home):
            for f in files:
                st = os.lstat(os.path.join(dirpath, f))
                if stat.S_ISREG(st.st_mode):
                    n += st.st_size
        return n

    @property
    def free(self) -> int:
        return self.base + self.extra + MIB * (self.initial - self.size())

    def __call__(self, path):
        self.calls.append(path)
        free = self.free
        return SimpleNamespace(free=free, total=self.total, used=self.total - free)


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeRun:
    """subprocess.run stand-in that answers only `<uv> cache prune`."""

    def __init__(self, disk, clock, *, rc=0, raise_=None, gain=0, cost=0.0):
        self.disk, self.clock = disk, clock
        self.rc, self.raise_, self.gain, self.cost = rc, raise_, gain, cost
        self.calls: list[tuple[list, dict]] = []

    def __call__(self, argv, **kw):
        argv = list(argv)
        self.calls.append((argv, kw))
        if argv[1:] != ["cache", "prune"]:
            raise AssertionError(f"the prune ran something it never should: {argv}")
        self.clock.t += self.cost
        if self.raise_ is not None:
            raise self.raise_
        if self.rc == 0:
            self.disk.extra += self.gain
        return subprocess.CompletedProcess(argv, self.rc, "", "")


def _write(path: Path, nbytes: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * nbytes)
    return path


def _age(path: Path, when: float) -> None:
    """Set the mtime of `path` and everything below it."""
    for dirpath, dirs, files in os.walk(path):
        for name in dirs + files:
            os.utime(os.path.join(dirpath, name), (when, when), follow_symlinks=False)
    os.utime(path, (when, when), follow_symlinks=False)


def _wrapper(checkout: Path, version: str) -> None:
    _write(checkout / "gradle" / "wrapper" / "gradle-wrapper.properties", 0).write_text(
        "distributionBase=GRADLE_USER_HOME\n"
        f"distributionUrl=https\\://services.gradle.org/distributions/gradle-{version}-bin.zip\n",
        encoding="utf-8")


class _Case(unittest.TestCase):
    """A fake home at 6 GiB free (trigger 8, target 10), kill switch lifted."""

    BASE_FREE = 6 * G

    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in (md.KILL_ENV, md.MIN_FREE_ENV, md.TARGET_ENV, md.MAX_AGE_ENV,
                     md.UV_BIN_ENV, "UV", "GRADLE_USER_HOME"):
            os.environ.pop(name, None)
        os.environ[md.UV_BIN_ENV] = UV
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.now = time.time()
        h = self.home = self.tmp / "home"
        gh = self.gh = h / ".gradle"
        c = self.caches = gh / "caches"
        _write(c / "8.7" / "fileHashes" / "fileHashes.lock", 1024)          # not current
        _write(c / "8.11.1" / "fileHashes" / "fileHashes.lock", 512)        # current
        _write(c / "modules-2" / "files-2.1" / "x" / "a.jar", 2048)
        _write(c / "jars-9" / "a.jar", 64)
        _write(c / "transforms-4" / "t.bin", 64)
        bc = self.bc = c / "build-cache-1"
        self.old1 = _write(bc / HASH.format(1), 100)
        self.old2 = _write(bc / HASH.format(2), 100)
        self.recent = _write(bc / HASH.format(3), 100)
        self.gc_props = _write(bc / "gc.properties", 10)
        self.lock = _write(bc / "build-cache-1.lock", 10)
        self.hexdir = bc / HASH.format(4)
        _write(self.hexdir / "inner", 10)
        _write(gh / "wrapper" / "dists" / "gradle-8.11.1-bin" / "h" / "gradle.zip", 300)
        _write(gh / "wrapper" / "dists" / "gradle-8.7-bin" / "h" / "gradle.zip", 300)
        self.worker = h / "Worker"
        _wrapper(self.worker / "app", "8.11.1")
        _wrapper(self.worker / "macp.wt" / "x", "8.11.1")
        for rel in ("Trading/live.db", ".config/a.conf", ".cache/ms-playwright/c",
                    ".cache/macp-browser/c", "vault/note.md"):
            _write(h / rel, 10)
        _age(self.old1, self.now - 40 * DAY)
        _age(self.old2, self.now - 35 * DAY)
        _age(self.gc_props, self.now - 90 * DAY)
        _age(self.lock, self.now - 90 * DAY)
        _age(self.hexdir, self.now - 90 * DAY)
        _age(self.recent, self.now - 1 * DAY)
        self.ledger = self.tmp / "assignments"
        self.ledger.mkdir()
        self.jobs = self.tmp / "jobs"
        self.proc = self.tmp / "proc"
        self.proc.mkdir()
        self.state = self.tmp / "state"
        self.disk = FakeDisk(h, self.BASE_FREE)
        self.clock = FakeClock()
        self.run = FakeRun(self.disk, self.clock)
        self.sent: list[str] = []
        for target, attr, value in (
                (md, "HOME_DIR", h), (md, "GRADLE_HOME_DIR", gh), (md, "STATE_DIR", self.state),
                (md, "VAULT_ROOT", h / "vault"), (md, "PROC_DIR", str(self.proc)),
                (md, "_disk_usage", self.disk), (md, "_run", self.run),
                (md, "_clock", self.clock), (md, "_now", lambda: self.now),
                (dg, "WORKER_ROOT", self.worker), (dg, "ASSIGNMENTS_DIR", self.ledger),
                (dg, "JOBS_DIR", self.jobs)):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)

    @property
    def kw(self):
        return {"gradle_home": self.gh, "home": self.home, "vault_root": self.home / "vault"}

    def prune(self, now=None, notify="default"):
        return md.check_and_prune(notify=self.sent.append if notify == "default" else notify,
                                  now=self.now if now is None else now)

    def row(self, task_id, state, repo=None, **kw):
        a = Assignment(task_id=task_id, agent_id="w", corr_id="c", state=state,
                       payload={"repo": f"https://github.com/o/{repo}.git"} if repo else {}, **kw)
        (self.ledger / f"{task_id}.json").write_text(a.to_json(), encoding="utf-8")

    def state_json(self):
        return json.loads(md.state_path().read_text(encoding="utf-8"))


class ThresholdTest(_Case):
    def test_at_the_threshold_nothing_runs_and_nothing_is_said(self):
        # (a) 정상이면 무음: one statvfs, then nothing — no subprocess, no state
        self.disk.base = 8 * G
        before = self.disk.size()
        self.assertIsNone(self.prune())
        self.assertEqual(self.run.calls, [])
        self.assertEqual(len(self.disk.calls), 1)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.disk.size(), before)
        self.assertEqual(self.sent, [])

    def test_one_byte_below_runs_a_pass(self):
        # (a') the positive half
        self.disk.base = 8 * G - 1
        line = self.prune()
        self.assertIsNotNone(line)
        self.assertEqual(len(self.run.calls), 1)
        self.assertFalse((self.caches / "8.7").exists())

    def test_defaults(self):
        self.assertEqual(md.min_free_bytes(), 8 * G)
        self.assertEqual(md.target_bytes(), 10 * G)
        self.assertEqual(md.max_age_days(), 30)
        self.assertEqual(md.KILL_ENV, "MACP_DISK_PRUNE")
        os.environ[md.MIN_FREE_ENV] = "12"
        self.assertEqual(md.target_bytes(), 14 * G)
        os.environ[md.TARGET_ENV] = "5"            # never below the trigger
        self.assertEqual(md.target_bytes(), 12 * G)
        os.environ[md.MIN_FREE_ENV] = "junk"
        self.assertEqual(md.min_free_bytes(), 8 * G)


class AllowlistTest(_Case):
    """(b) Every path outside the allowlist is refused before any deleting call,
    and stays on disk; the allowlisted ones really go."""

    def test_refuses_everything_outside_the_allowlist_and_deletes_the_rest(self):
        os.symlink(self.home / "Trading", self.caches / "8.6")
        os.symlink(self.home / "Trading" / "live.db", self.bc / HASH.format(9))
        _write(self.caches / "modules-2" / "8.7" / "f", 1)          # version-named, wrong parent
        outside = self.tmp / "elsewhere" / "8.7"                    # version-named, not a cache
        _write(outside / "f", 1)
        refused = [
            self.caches / "modules-2",
            self.caches / "modules-2" / "files-2.1" / "x" / "a.jar",
            self.caches / "modules-2" / "8.7",
            self.gh / "wrapper" / "dists" / "gradle-8.11.1-bin",
            self.gh / "wrapper" / "dists" / "gradle-8.7-bin",
            self.gh / "wrapper",
            self.caches,
            self.gh,
            self.worker / "app",
            self.worker,
            self.home / "Trading",
            self.home / "vault",
            self.home / ".config",
            self.home / ".cache" / "ms-playwright",
            self.home / ".cache" / "macp-browser",
            self.caches / "jars-9",
            self.caches / "transforms-4",
            self.bc,
            self.gc_props,
            self.lock,
            self.hexdir,
            Path(str(self.caches / "8.7") + "/../modules-2"),
            self.caches / "8.6",                                   # symlink → Trading
            self.bc / HASH.format(9),                              # symlink → a file outside
            self.home,
            outside,                       # outside the gradle home (tmp, never a real dir)
        ]
        with mock.patch.object(md.shutil, "rmtree", wraps=shutil.rmtree) as rmtree, \
             mock.patch.object(md.os, "unlink", wraps=os.unlink) as unlink:
            for target in refused:
                with self.subTest(target=str(target)):
                    existed = os.path.lexists(target)
                    with self.assertRaises(md.Refused):
                        md._delete(target, **self.kw)
                    self.assertEqual(os.path.lexists(target), existed)
            rmtree.assert_not_called()
            unlink.assert_not_called()
            self.assertTrue((self.home / "Trading" / "live.db").exists())
            # the positive half: the allowlisted shapes really are deleted
            self.assertGreater(md._delete(self.caches / "8.7", **self.kw), 0)
            md._delete(self.old1, **self.kw)
            rmtree.assert_called_once_with(str(self.caches / "8.7"))
            unlink.assert_any_call(str(self.old1))   # rmtree's own unlinks count too
        self.assertFalse((self.caches / "8.7").exists())
        self.assertFalse(self.old1.exists())
        self.assertTrue((self.caches / "8.11.1").exists())
        self.assertTrue(self.old2.exists())

    def test_a_gradle_home_of_home_or_root_refuses_everything(self):
        for gh in (self.home, Path("/")):
            with self.subTest(gh=str(gh)):
                with self.assertRaises(md.Refused):
                    md._delete(self.caches / "8.7", gradle_home=gh, home=self.home,
                               vault_root=self.home / "vault")
        self.assertTrue((self.caches / "8.7").exists())

    def test_a_protected_root_inside_the_target_is_refused(self):
        # the second belt: a vault (or ~/Worker) that somehow lives in a cache dir
        with self.assertRaises(md.Refused) as cm:
            md._delete(self.caches / "8.7", gradle_home=self.gh, home=self.home,
                       vault_root=self.caches / "8.7" / "fileHashes")
        self.assertIn("protected vault", str(cm.exception))
        self.assertTrue((self.caches / "8.7").exists())


class SourcePinTest(unittest.TestCase):
    def test_the_only_deleting_calls_sit_in_delete(self):
        # (c) rmtree and unlink once each, both inside _delete; nothing else deletes
        tree = ast.parse(Path(md.__file__).read_text(encoding="utf-8"))

        def calls(node) -> list[str]:
            out = []
            for n in ast.walk(node):
                if isinstance(n, ast.Call):
                    f = n.func
                    out.append(f.attr if isinstance(f, ast.Attribute)
                               else f.id if isinstance(f, ast.Name) else "")
            return out

        everywhere = calls(tree)
        self.assertEqual(everywhere.count("rmtree"), 1)
        self.assertEqual(everywhere.count("unlink"), 1)
        for name in ("remove", "rmdir", "removedirs"):
            self.assertNotIn(name, everywhere)
        delete = next(n for n in tree.body
                      if isinstance(n, ast.FunctionDef) and n.name == "_delete")
        self.assertIn("rmtree", calls(delete))
        self.assertIn("unlink", calls(delete))
        self.assertIn("_check_allowed", calls(delete))
        strings = {n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertEqual(strings & {"rm", "-rf", "-fd", "rmdir"}, set())


class CandidateTest(_Case):
    def test_non_current_version_goes_first_and_current_recent_stays(self):
        # (d)
        current, why = md.wrapper_versions(self.worker)
        self.assertEqual((current, why), ({"8.11.1"}, ""))
        rows = md.classify_versions(self.gh, current, 30 * DAY, self.now)
        self.assertEqual(rows[0], (self.caches / "8.7", None))
        self.assertEqual(rows[1][0], self.caches / "8.11.1")
        self.assertIn("current wrapper version", rows[1][1])
        self.assertEqual([p.name for p, _ in rows], ["8.7", "8.11.1"])   # jars-9 etc. never listed

    def test_a_current_version_unused_for_n_days_is_a_candidate(self):
        _age(self.caches / "8.11.1", self.now - 31 * DAY)
        got = md.version_candidates(self.gh, {"8.11.1"}, 30 * DAY, self.now)
        self.assertEqual([p.name for p in got], ["8.7", "8.11.1"])

    def test_a_version_named_by_a_worktree_wrapper_is_current(self):
        _wrapper(self.worker / "macp.wt" / "x", "8.7")
        current, _ = md.wrapper_versions(self.worker)
        self.assertEqual(current, {"8.11.1", "8.7"})
        self.assertEqual(md.version_candidates(self.gh, current, 30 * DAY, self.now), [])

    def test_an_unparseable_wrapper_makes_the_set_unknown(self):
        (self.worker / "app" / "gradle" / "wrapper" / "gradle-wrapper.properties").write_text(
            "distributionUrl=https\\://example.invalid/custom.zip\n", encoding="utf-8")
        current, why = md.wrapper_versions(self.worker)
        self.assertIsNone(current)
        self.assertIn("app/gradle/wrapper/gradle-wrapper.properties: no distributionUrl version", why)
        self.assertEqual(md.version_candidates(self.gh, None, 30 * DAY, self.now), [])
        line = self.prune()
        self.assertIn("skipped versions=wrapper set unknown (app/gradle/wrapper", line)
        self.assertTrue((self.caches / "8.7").exists())
        self.assertFalse(self.old1.exists())          # build-cache still pruned

    def test_no_wrapper_at_all_is_unknown(self):
        shutil.rmtree(self.worker)
        current, why = md.wrapper_versions(self.worker)
        self.assertIsNone(current)
        self.assertIn("no gradle-wrapper.properties", why)

    def test_build_cache_old_hashes_oldest_first_and_nothing_else(self):
        got = md.build_cache_candidates(self.gh, 30 * DAY, self.now)
        self.assertEqual(got, [self.old1, self.old2])

    def test_max_age_is_configurable_and_bad_values_fall_back(self):
        os.environ[md.MAX_AGE_ENV] = "0.5"
        self.assertEqual(md.max_age_days(), 0.5)
        got = md.build_cache_candidates(self.gh, md.max_age_days() * DAY, self.now)
        self.assertEqual(got, [self.old1, self.old2, self.recent])
        for bad in ("junk", "0", "-3"):
            os.environ[md.MAX_AGE_ENV] = bad
            self.assertEqual(md.max_age_days(), 30)


class BusyTest(_Case):
    """(e) Busy skips every Gradle deletion with a one-line reason; uv still runs."""

    def _assert_gradle_skipped(self, reason):
        line = self.prune()
        self.assertIn(f"skipped gradle={reason}", line)
        self.assertTrue((self.caches / "8.7").exists())
        self.assertTrue(self.old1.exists())
        self.assertEqual(len(self.run.calls), 1)          # uv did run
        self.assertIn("uv cache prune ok", line)
        self.assertEqual(line.count("\n"), 0)

    def test_an_in_progress_run(self):
        self.row("task-a", "in_progress", repo="app")
        self._assert_gradle_skipped("held by task-a (in_progress)")

    def test_a_running_long_job_row(self):
        self.row("task-j", "failed", repo="app", job_running=True)
        self._assert_gradle_skipped("held by task-j (job_running)")

    def test_a_long_job_state_file(self):
        self.jobs.mkdir()
        (self.jobs / "lj.json").write_text(json.dumps(
            {"slug": "lj", "cwd": str(self.worker / "app")}), encoding="utf-8")
        self._assert_gradle_skipped("long_job lj cwd")

    def test_an_unreadable_ledger(self):
        shutil.rmtree(self.ledger)
        self._assert_gradle_skipped("ledger unreadable (no ledger dir")

    def test_a_gradle_daemon_process(self):
        (self.proc / "4242").mkdir()
        (self.proc / "4242" / "cmdline").write_bytes(
            b"java\0-cp\0gradle.jar\0org.gradle.launcher.daemon.bootstrap.GradleDaemon\0")
        self._assert_gradle_skipped("GradleDaemon pid 4242")

    def test_no_proc(self):
        with mock.patch.object(md, "PROC_DIR", str(self.tmp / "nowhere")):
            self._assert_gradle_skipped("no /proc")

    def test_idle_deletes(self):
        # the positive half: a finished row, a vault task and an unrelated pid hold nothing
        self.row("done", "done", repo="app")
        self.row("vault", "in_progress")
        (self.proc / "77").mkdir()
        (self.proc / "77" / "cmdline").write_bytes(b"python3\0-m\0http.server\0")
        line = self.prune()
        self.assertNotIn("gradle=", line)
        self.assertFalse((self.caches / "8.7").exists())
        self.assertFalse(self.old1.exists())
        self.assertFalse(self.old2.exists())
        self.assertTrue(self.recent.exists())
        self.assertTrue((self.caches / "8.11.1").exists())

    def test_busy_is_re_read_before_each_version_dir(self):
        _write(self.caches / "8.5" / "f", 1024)
        _age(self.caches / "8.5", self.now - 5 * DAY)
        real_delete = md._delete

        def delete_then_dispatch(path, **kw):
            freed = real_delete(path, **kw)
            self.row("late", "dispatched", repo="app")
            return freed

        with mock.patch.object(md, "_delete", side_effect=delete_then_dispatch):
            line = self.prune()
        self.assertFalse((self.caches / "8.5").exists())   # oldest non-current first
        self.assertTrue((self.caches / "8.7").exists())
        self.assertIn("skipped gradle=held by late (dispatched)", line)
        self.assertTrue(self.old1.exists())


class UvTest(_Case):
    def test_exact_argv_timeout_and_stdin(self):
        # (f)
        self.prune()
        self.assertEqual(len(self.run.calls), 1)
        argv, kw = self.run.calls[0]
        self.assertEqual(argv, [UV, "cache", "prune"])
        self.assertEqual(kw["timeout"], md.UV_TIMEOUT_S)
        self.assertIs(kw["stdin"], subprocess.DEVNULL)
        self.assertTrue(kw["capture_output"])

    def test_uv_from_the_uv_env_when_no_override(self):
        del os.environ[md.UV_BIN_ENV]
        os.environ["UV"] = "/via/uv-run/uv"
        self.prune()
        self.assertEqual(self.run.calls[0][0][0], "/via/uv-run/uv")

    def _assert_uv_skipped_gradle_pruned(self, reason, uv_calls=1):
        line = self.prune()
        self.assertIn(f"skipped {reason}", line)
        self.assertNotIn("uv cache prune ok", line)
        self.assertFalse((self.caches / "8.7").exists())
        self.assertFalse(self.old1.exists())
        self.assertEqual(len(self.run.calls), uv_calls)

    def test_no_uv_binary(self):
        del os.environ[md.UV_BIN_ENV]
        with mock.patch.object(md, "_which", lambda name: None):
            self._assert_uv_skipped_gradle_pruned("uv=not found", uv_calls=0)

    def test_uv_timeout(self):
        self.run.raise_ = subprocess.TimeoutExpired(["uv"], md.UV_TIMEOUT_S)
        self._assert_uv_skipped_gradle_pruned(f"uv=timed out ({md.UV_TIMEOUT_S}s)")

    def test_uv_oserror(self):
        self.run.raise_ = FileNotFoundError(2, "No such file")
        self._assert_uv_skipped_gradle_pruned("uv=FileNotFoundError")

    def test_uv_nonzero_exit(self):
        self.run.rc = 2
        self._assert_uv_skipped_gradle_pruned("uv=rc 2")

    def test_uv_alone_reaching_the_target_leaves_gradle_alone(self):
        self.disk.base = int(7.9 * G)
        self.run.gain = 3 * G
        line = self.prune()
        self.assertTrue((self.caches / "8.7").exists())
        self.assertTrue(self.old1.exists())
        self.assertIn("uv cache prune ok", line)
        self.assertNotIn("STILL", line)
        self.assertEqual(self.sent, [])


class StopTest(_Case):
    def setUp(self):
        super().setUp()
        _write(self.caches / "8.5" / "f", 1024)            # older non-current
        _age(self.caches / "8.5", self.now - 5 * DAY)
        self.disk.initial = self.disk.size()

    def test_stops_at_the_target(self):
        # (g) 7.5 → 8.5 after 8.5 goes: target 8.5 reached, the rest stays
        self.disk.base = int(7.5 * G)
        os.environ[md.TARGET_ENV] = "8.5"
        line = self.prune()
        self.assertFalse((self.caches / "8.5").exists())
        self.assertTrue((self.caches / "8.7").exists())
        self.assertTrue(self.old1.exists())
        self.assertIn("removed versions 8.5", line)
        self.assertNotIn("8.7", line)
        self.assertNotIn("build-cache entries", line)
        self.assertNotIn("STILL", line)

    def test_budget_exhausted_is_named(self):
        self.run.cost = md.BUDGET_S + 1
        line = self.prune()
        self.assertIn("stopped: budget exhausted", line)
        self.assertTrue((self.caches / "8.5").exists())
        self.assertTrue((self.caches / "8.7").exists())
        self.assertTrue(self.old1.exists())

    def test_deletion_cap(self):
        with mock.patch.object(md, "MAX_DELETIONS", 2):
            line = self.prune()
        self.assertIn("removed versions 8.5, 8.7", line)
        self.assertIn("stopped: deletion cap", line)
        self.assertTrue(self.old1.exists())
        self.assertTrue(self.old2.exists())


class SummaryTest(_Case):
    def test_one_line_with_usage_before_after_and_still_below(self):
        # (h)
        line = self.prune()
        self.assertEqual(line.count("\n"), 0)
        self.assertTrue(line.startswith("disk-prune: free 6.0G → 7.2G of 96.0G (94% → 93% used, "
                                        "trigger <8.0G, target 10.0G) — uv cache prune ok; "
                                        "removed versions 8.7; removed 2 build-cache entries"), line)
        self.assertTrue(line.endswith("— STILL below 8.0G"), line)

    def test_no_still_suffix_when_back_above(self):
        self.run.gain = int(1.5 * G)
        line = self.prune()                   # 6 + 1.5 + 1 (8.7) + 0.2 = 8.7 ≥ 8
        self.assertNotIn("STILL", line)
        self.assertNotIn("paged", line)


class AlertTest(_Case):
    def test_still_below_pages_once_with_state_written_first(self):
        # (i)
        seen = []

        def notify(msg):
            seen.append(self.state_json()["last_alert_ts"])
            self.sent.append(msg)

        line = self.prune(notify=notify)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(seen, [self.now])
        self.assertIn("paged", line)
        self.assertIn("디스크 여유 부족", self.sent[0])
        self.assertNotIn(str(self.home), self.sent[0])
        self.assertEqual(stat.S_IMODE(os.stat(md.state_path()).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700)
        # after the pass cooldown but inside 24 h: a pass, no page
        line = self.prune(now=self.now + md.PASS_INTERVAL_S + 1, notify=notify)
        self.assertIsNotNone(line)
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("paged", line)
        # after 24 h: pages again
        self.prune(now=self.now + md.ALERT_COOLDOWN_S + 1, notify=notify)
        self.assertEqual(len(self.sent), 2)

    def test_a_raising_notifier_is_swallowed_and_the_state_persists(self):
        def boom(msg):
            raise OSError("channel down")

        line = self.prune(notify=boom)
        self.assertIn("notify failed (OSError)", line)
        self.assertEqual(self.state_json()["last_alert_ts"], self.now)
        self.prune(now=self.now + md.PASS_INTERVAL_S + 1, notify=boom)   # no second attempt
        self.assertEqual(self.state_json()["last_alert_ts"], self.now)

    def test_above_the_threshold_after_the_pass_does_not_page(self):
        self.run.gain = 3 * G
        self.prune()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.state_json()["last_alert_ts"], 0.0)


class PassCooldownTest(_Case):
    def test_one_pass_per_interval_while_still_low(self):
        # (j)
        self.assertIsNotNone(self.prune())
        self.assertEqual(len(self.run.calls), 1)
        self.assertIsNone(self.prune(now=self.now + md.PASS_INTERVAL_S - 1))
        self.assertEqual(len(self.run.calls), 1)
        self.assertIsNotNone(self.prune(now=self.now + md.PASS_INTERVAL_S))
        self.assertEqual(len(self.run.calls), 2)

    def test_a_stamp_in_the_future_does_not_suppress(self):
        self.prune(now=self.now + 10 * DAY)
        self.assertIsNotNone(self.prune())
        self.assertEqual(len(self.run.calls), 2)

    def test_corrupt_state_counts_as_zeros(self):
        self.state.mkdir()
        md.state_path().write_text("{not json", encoding="utf-8")
        self.assertIsNotNone(self.prune())


class CoolingNeverNotifiedTest(unittest.TestCase):
    def test_last_zero_does_not_suppress_under_injected_clock(self):
        # Injected clock returns a small `now`; `last=0.0` means "never
        # notified" and must not be mistaken for "just notified".
        self.assertFalse(md._cooling(now=5.0, last=0.0, window=600.0))

    def test_last_negative_does_not_suppress(self):
        self.assertFalse(md._cooling(now=5.0, last=-1.0, window=600.0))

    def test_real_last_within_window_still_suppresses(self):
        self.assertTrue(md._cooling(now=100.0, last=50.0, window=600.0))


class KillSwitchAndFailOpenTest(_Case):
    def test_kill_switch_skips_even_the_measurement(self):
        # (k)
        for value in ("0", " 0 "):
            os.environ[md.KILL_ENV] = value
            with mock.patch.object(md, "_disk_usage", mock.Mock(side_effect=AssertionError)) as d:
                self.assertIsNone(self.prune())
                d.assert_not_called()
        self.assertEqual(self.run.calls, [])
        for value in ("", "1", "no"):
            os.environ[md.KILL_ENV] = value
            self.assertTrue(md.enabled(), value)

    def test_unreadable_disk_is_one_line_and_no_delete(self):
        # (l)
        with mock.patch.object(md, "_disk_usage", mock.Mock(side_effect=OSError(5, "EIO"))):
            line = self.prune()
        self.assertTrue(line.startswith("disk-prune: free space unreadable under"), line)
        self.assertTrue(line.endswith("— skipped"))
        self.assertEqual(self.run.calls, [])
        self.assertTrue((self.caches / "8.7").exists())

    def test_an_unexpected_error_is_a_line_not_a_raise(self):
        with mock.patch.object(md, "classify_versions", side_effect=RuntimeError("scan died")):
            line = self.prune()
        self.assertTrue(line.startswith("disk-prune: error (RuntimeError: scan died)"), line)
        self.assertTrue((self.caches / "8.7").exists())


class DryRunTest(_Case):
    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = md.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_dry_run_prints_verdicts_and_deletes_nothing(self):
        # (m) through the module seams, as the real CLI resolves them
        before = self.disk.size()
        rc, out, _ = self._main(["--dry-run"])
        self.assertEqual(rc, 0)
        self.assertIn("below the trigger", out)
        self.assertIn("wrapper versions: 8.11.1", out)
        self.assertIn("gradle: idle", out)
        self.assertIn("  caches/8.7: would remove", out)
        self.assertIn("  caches/8.11.1: keep: current wrapper version", out)
        self.assertIn("caches/build-cache-1: 2 entries unchanged for >30d", out)
        self.assertEqual(self.run.calls, [])
        self.assertEqual(self.disk.size(), before)
        self.assertFalse(self.state.exists())

    def test_without_dry_run_it_refuses(self):
        rc, _, err = self._main([])
        self.assertEqual(rc, 2)
        self.assertIn("only --dry-run", err)
        self.assertEqual(self.run.calls, [])


class ImportWeightTest(unittest.TestCase):
    def test_import_pulls_in_no_third_party_module(self):
        # (n) stdlib + stdlib-only siblings, like disk_guard
        code = "import sys, todo_system.maintenance_disk; print('yaml' in sys.modules)"
        r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "False")


class SuiteIsolationTest(unittest.TestCase):
    """(o) todo_system/conftest.py's `_disk_prune_isolated` is live. No env pop."""

    def test_the_kill_switch_is_set_for_every_test(self):
        self.assertEqual(os.environ.get(md.KILL_ENV), "0")
        self.assertFalse(md.enabled())

    def test_every_path_seam_points_into_a_tmp_dir(self):
        real_home = Path.home()
        for attr in ("HOME_DIR", "GRADLE_HOME_DIR", "STATE_DIR", "VAULT_ROOT", "PROC_DIR"):
            value = Path(getattr(md, attr))
            self.assertIn("disk-prune", str(value), attr)
            self.assertNotEqual(value, real_home, attr)
            self.assertNotEqual(value, real_home / ".gradle", attr)

    def test_an_uninjected_call_does_nothing_at_all(self):
        with mock.patch.object(md, "_disk_usage", mock.Mock(side_effect=AssertionError)) as d:
            self.assertIsNone(md.check_and_prune())
        d.assert_not_called()


def _quiet_other_steps(monkeypatch):
    from todo_system import worker
    monkeypatch.setattr(worker, "IS_REMOTE_HOST", False)
    for name in ("autoclose_stale_reviews_local", "sweep_remote_completed_runs",
                 "sync_vault_state"):
        monkeypatch.setattr(worker, name, lambda: 0)
    for name in ("rotate_logs", "check_unapplied_migrations", "pull_engine_checkout",
                 "bump_vault_engine_pin", "sync_wrappers", "pull_browser_checkout",
                 "restart_stale_daemons"):
        monkeypatch.setattr(worker, name, lambda: [])
    for name in ("check_dispatch_silence", "check_work_starvation"):
        monkeypatch.setattr(worker, name, lambda: None)
    monkeypatch.setattr(worker.browser_watch, "check_login_watch", lambda **k: None)
    return worker


def test_cmd_maintenance_runs_the_step_with_kakao_notify(monkeypatch, capsys):
    worker = _quiet_other_steps(monkeypatch)
    seen = []

    def step(**kw):
        seen.append(kw)
        return "disk-prune: free 6.0G → 9.0G of 96.0G — uv cache prune ok"

    monkeypatch.setattr(worker.maintenance_disk, "check_and_prune", step)
    assert worker.cmd_maintenance() == 0
    assert seen == [{"notify": worker.kakao_notify}]
    assert "disk-prune: free 6.0G → 9.0G" in capsys.readouterr().out


def test_cmd_maintenance_silent_step_prints_nothing(monkeypatch, capsys):
    worker = _quiet_other_steps(monkeypatch)
    monkeypatch.setattr(worker.maintenance_disk, "check_and_prune", lambda **k: None)
    assert worker.cmd_maintenance() == 0
    assert "disk-prune" not in capsys.readouterr().out


def test_cmd_maintenance_step_raising_sets_rc(monkeypatch, capsys):
    worker = _quiet_other_steps(monkeypatch)

    def boom(**kw):
        raise RuntimeError("prune exploded")

    monkeypatch.setattr(worker.maintenance_disk, "check_and_prune", boom)
    assert worker.cmd_maintenance() == 1
    assert "disk-prune: error — prune exploded" in capsys.readouterr().out


if __name__ == "__main__":
    unittest.main()
