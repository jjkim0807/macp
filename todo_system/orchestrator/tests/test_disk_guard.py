"""The run-start disk guard (orchestrator/disk_guard.py).

Every seam is injected: a tmp worker root with fake clones (`.git/logs/HEAD`,
optional `gradlew`, optional `app/build/`), ledger rows written by the real
Assignment.to_json(), job state JSON, a fake disk whose free space rises with
each successful clean, a fake `run` that answers the git probes and records
every call, and injected /proc cwds and clock. Nothing here touches the real
disk, ~/Worker, git or gradle. The suite-wide `_disk_guard_isolated` fixture
(todo_system/conftest.py) backs that up, and SuiteIsolationTest pins that it
is live.

The safety cases assert the positive half too — the idle clone IS cleaned —
so a guard that refused everything could not pass them by doing nothing.
"""

from __future__ import annotations

import ast
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from todo_system import paths
from todo_system.orchestrator import disk_guard as dg
from todo_system.orchestrator import jobs, repos
from todo_system.orchestrator.assignment import Assignment

G = dg.GIB
VAULT = Path(__file__).resolve().parents[3]
URL = "https://github.com/o/{}.git"


class FakeDisk:
    """shutil.disk_usage stand-in; FakeRun raises `free` by `gain` per clean."""

    def __init__(self, free, total=48 * G, gain=0):
        self.free, self.total, self.gain = free, total, gain
        self.calls: list[str] = []

    def __call__(self, path):
        self.calls.append(path)
        return SimpleNamespace(free=self.free, total=self.total, used=self.total - self.free)


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeRun:
    """Stands in for subprocess.run. Answers the three git probes per clone,
    "runs" CLEAN_ARGV by raising the fake disk's free space, records every call,
    and refuses anything else — so an unexpected command fails the test."""

    def __init__(self, disk, clock=None, *, branch=None, origin_head=None, dirty=(),
                 git_rc=None, git_raise=None, clean_rc=None, clean_raise=None,
                 clean_cost=None, on_clean=None):
        self.disk, self.clock = disk, clock
        self.branch = branch or {}
        self.origin_head = origin_head or {}
        self.dirty = set(dirty)
        self.git_rc = git_rc or {}
        self.git_raise = git_raise or {}
        self.clean_rc = clean_rc or {}
        self.clean_raise = clean_raise or {}
        self.clean_cost = clean_cost or {}
        self.on_clean = on_clean
        self.calls: list[tuple[list, dict]] = []

    @property
    def cleans(self) -> list[str]:
        return [Path(kw["cwd"]).name for argv, kw in self.calls if tuple(argv) == dg.CLEAN_ARGV]

    @property
    def probed(self) -> set[str]:
        return {Path(argv[argv.index("-C") + 1]).name
                for argv, _ in self.calls if argv[0] == "git"}

    def __call__(self, argv, **kw):
        argv = list(argv)
        self.calls.append((argv, kw))
        if argv[0] == "git":
            name = Path(argv[argv.index("-C") + 1]).name
            sub = next(s for s in ("branch", "symbolic-ref", "status") if s in argv)
            if (name, sub) in self.git_raise:
                raise self.git_raise[name, sub]
            rc = self.git_rc.get((name, sub), 0)
            if rc:
                return subprocess.CompletedProcess(argv, rc, "", "fatal: boom\n")
            if sub == "branch":
                out = self.branch.get(name, "main") + "\n"
            elif sub == "symbolic-ref":
                ref = self.origin_head.get(name, "refs/remotes/origin/main")
                if ref is None:           # origin/HEAD never set: git exits 128
                    return subprocess.CompletedProcess(argv, 128, "", "fatal: not a symbolic ref\n")
                out = ref + "\n"
            else:
                out = " M app/src/Main.kt\n" if name in self.dirty else ""
            return subprocess.CompletedProcess(argv, 0, out, "")
        if tuple(argv) == dg.CLEAN_ARGV:
            name = Path(kw["cwd"]).name
            if self.clock is not None:
                self.clock.t += self.clean_cost.get(name, 0)
            if name in self.clean_raise:
                raise self.clean_raise[name]
            if self.on_clean is not None:
                self.on_clean(name)
            rc = self.clean_rc.get(name, 0)
            if rc == 0:
                self.disk.free += self.disk.gain
            return subprocess.CompletedProcess(argv, rc, "BUILD SUCCESSFUL\n" if rc == 0 else "", "")
        raise AssertionError(f"the guard ran something it never should: {argv}")


class _GuardCase(unittest.TestCase):
    """A tmp worker root, ledger and jobs dir, with the kill switch lifted."""

    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(dg.KILL_ENV, None)   # the conftest set it to "0"
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "Worker"
        self.root.mkdir()
        self.ledger = self.tmp / "assignments"
        self.ledger.mkdir()
        self.jobs = self.tmp / "jobs"
        self.clock = FakeClock()
        self.cwds: list[tuple[int, str]] = []
        self.lines: list[str] = []

    def clone(self, name, *, gradlew=True, build=True, linked=False, mtime=None):
        c = self.root / name
        if linked:
            c.mkdir(parents=True)
            marker = c / ".git"
            marker.write_text("gitdir: /elsewhere/.git/worktrees/x\n", encoding="utf-8")
        else:
            (c / ".git" / "logs").mkdir(parents=True)
            marker = c / ".git" / "logs" / "HEAD"
            marker.write_text("0000 1111 t <t@t> 0 +0000\tclone\n", encoding="utf-8")
        if gradlew:
            (c / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
        if build:
            (c / "app" / "build").mkdir(parents=True)
        if mtime is not None:
            os.utime(marker, (mtime, mtime))
        return c

    def row(self, task_id, state, repo=None, **kw):
        a = Assignment(task_id=task_id, agent_id="w", corr_id="c", state=state,
                       payload={"repo": URL.format(repo)} if repo else {}, **kw)
        (self.ledger / f"{task_id}.json").write_text(a.to_json(), encoding="utf-8")

    def job(self, slug, cwd):
        self.jobs.mkdir(exist_ok=True)
        (self.jobs / f"{slug}.json").write_text(json.dumps(
            {"slug": slug, "pid": 99999999, "cmd": "true",
             "cwd": str(cwd) if cwd is not None else None,
             "started_ts": "2026-09-19T23:00:00+09:00"}), encoding="utf-8")

    def guard(self, disk, run, **kw):
        kw.setdefault("proc_cwds", lambda: list(self.cwds))
        return dg.ensure_free_space(progress=self.lines.append, worker_root=self.root,
                                    assignments_dir=self.ledger, jobs_dir=self.jobs,
                                    disk_usage=disk, run=run, clock=self.clock, **kw)


class ThresholdTest(_GuardCase):
    def test_at_the_threshold_nothing_runs_and_nothing_is_said(self):
        # (a) 정상이면 무음: one statvfs, then nothing — no probe, no /proc scan
        self.clone("idle")
        disk = FakeDisk(dg.MIN_FREE_BYTES)
        run, scan = FakeRun(disk), mock.Mock(return_value=[])
        res = self.guard(disk, run, proc_cwds=scan)
        self.assertFalse(res["triggered"])
        self.assertEqual(res["free_before"], dg.MIN_FREE_BYTES)
        self.assertEqual(run.calls, [])
        scan.assert_not_called()
        self.assertEqual(self.lines, [])
        self.assertEqual(len(disk.calls), 1)

    def test_one_byte_below_triggers(self):
        self.clone("idle")
        disk = FakeDisk(dg.MIN_FREE_BYTES - 1, gain=3 * G)
        run = FakeRun(disk)
        res = self.guard(disk, run)
        self.assertTrue(res["triggered"])
        self.assertEqual(run.cleans, ["idle"])
        self.assertEqual(len(self.lines), 1)

    def test_constants(self):
        self.assertEqual(dg.MIN_FREE_BYTES, 3 * 1024 ** 3)
        self.assertEqual(dg.TARGET_FREE_BYTES, 5 * 1024 ** 3)
        self.assertLess(dg.MIN_FREE_BYTES, dg.TARGET_FREE_BYTES)   # the hysteresis gap
        self.assertEqual(dg.KILL_ENV, "MACP_DISK_GUARD")


class IdleClassificationTest(_GuardCase):
    """(b) One idle clone among one of every kind of refusal. Only the idle one
    is cleaned, and every other clone carries its EXACT reason."""

    def test_only_the_idle_clone_is_cleaned_and_every_refusal_is_named(self):
        for name in ("idle", "dirty", "onbranch", "detached", "thisrun", "held",
                     "accepted", "dispatched", "jobheld", "jobcwd", "pidheld"):
            self.clone(name)
        self.clone("nogradlew", gradlew=False)
        self.clone("nobuild", build=False)
        self.clone("linked", linked=True)
        os.symlink(self.root / "idle", self.root / "symlinked")
        self.row("task-held", "in_progress", repo="held")
        self.row("task-acc", "accepted", repo="accepted")
        self.row("task-disp", "dispatched", repo="dispatched")
        self.row("task-job", "failed", repo="jobheld", job_running=True)   # a deferred long_job
        self.row("task-done", "done", repo="idle")        # finished: holds nothing
        self.row("task-vault", "in_progress")             # vault task: no ~/Worker clone
        self.job("lj", self.root / "jobcwd" / "data")
        self.job("vault-job", None)
        self.cwds = [(4242, str(self.root / "pidheld" / "app"))]
        disk = FakeDisk(1 * G)            # gain 0: every clone gets visited
        run = FakeRun(disk, branch={"onbranch": "worker/x", "detached": ""}, dirty={"dirty"})

        res = self.guard(disk, run, exclude=(self.root / "thisrun",))

        self.assertEqual(res["cleaned"], ["idle"])
        self.assertEqual(run.cleans, ["idle"])
        self.assertEqual(res["skipped"], {
            "dirty": "dirty",
            "onbranch": "on worker/x (default main)",
            "detached": "detached HEAD",
            "thisrun": "this run",
            "held": "held by task-held (in_progress)",
            "accepted": "held by task-acc (accepted)",
            "dispatched": "held by task-disp (dispatched)",
            "jobheld": "held by task-job (job_running)",
            "jobcwd": "long_job lj cwd",
            "pidheld": "pid 4242 cwd",
            "nogradlew": "no gradlew",
            "nobuild": "no build output",
            "linked": "linked worktree",
            "symlinked": "symlink",
        })
        # cheap checks first: git only ever ran on clones that passed them
        self.assertEqual(run.probed, {"idle", "dirty", "onbranch", "detached"})
        self.assertEqual(len(self.lines), 1)
        self.assertTrue(self.lines[0].startswith("disk guard: free 1.0G → 1.0G"), self.lines[0])
        self.assertIn("cleaned idle", self.lines[0])
        self.assertTrue(self.lines[0].endswith("— STILL below 3.0G"), self.lines[0])

    def test_the_ledger_maps_a_repo_url_like_the_runner_does(self):
        self.clone("DripCoffee")
        self.row("t", "in_progress", repo="DripCoffee")
        held = dg.busy_checkouts(self.root, self.ledger, self.jobs)
        self.assertEqual(held, {os.path.realpath(repos.local_path(URL.format("DripCoffee"), self.root)):
                                "held by t (in_progress)"})

    def test_the_busy_set_is_re_read_before_every_clone(self):
        # A run dispatched onto the next clone while the first one cleans must
        # stop that clone's clean — the ledger is not read once up front.
        self.clone("first", mtime=1_000_000)
        self.clone("second", mtime=2_000_000)
        disk = FakeDisk(1 * G)
        run = FakeRun(disk, on_clean=lambda name: self.row("late", "dispatched", repo="second"))
        res = self.guard(disk, run)
        self.assertEqual(res["cleaned"], ["first"])
        self.assertEqual(res["skipped"], {"second": "held by late (dispatched)"})

    def test_default_branch_comes_from_origin_head(self):
        self.clone("trunky")
        self.clone("stale")
        disk = FakeDisk(1 * G)
        run = FakeRun(disk, branch={"trunky": "trunk", "stale": "main"},
                      origin_head={"trunky": "refs/remotes/origin/trunk",
                                   "stale": "refs/remotes/origin/trunk"})
        res = self.guard(disk, run)
        self.assertEqual(res["cleaned"], ["trunky"])
        self.assertEqual(res["skipped"], {"stale": "on main (default trunk)"})

    def test_unset_origin_head_falls_back_to_main(self):
        # stages.default_branch's rule — the branch the runner itself restores to
        self.clone("m")
        self.clone("master")
        disk = FakeDisk(1 * G)
        run = FakeRun(disk, branch={"master": "master"},
                      origin_head={"m": None, "master": None})
        res = self.guard(disk, run)
        self.assertEqual(res["cleaned"], ["m"])
        self.assertEqual(res["skipped"], {"master": "on master (default main)"})

    def test_git_probes_never_take_the_index_lock(self):
        self.clone("idle")
        disk = FakeDisk(1 * G, gain=5 * G)
        run = FakeRun(disk)
        self.guard(disk, run)
        probes = [(argv, kw) for argv, kw in run.calls if argv[0] == "git"]
        self.assertEqual(len(probes), 3)
        for argv, kw in probes:
            self.assertEqual(argv[:4], ["git", "--no-optional-locks", "-C", str(self.root / "idle")])
            self.assertEqual(kw["timeout"], dg.GIT_TIMEOUT_S)
            self.assertIs(kw["stdin"], subprocess.DEVNULL)


class OrderAndStopTest(_GuardCase):
    def test_least_recently_used_first_and_stop_at_the_target(self):
        # (c) names run against mtime so the order can only come from mtime
        self.clone("zeta", mtime=1_000_000)
        self.clone("yank", mtime=2_000_000)
        self.clone("xray", mtime=3_000_000)
        self.clone("whisky", mtime=4_000_000)
        disk = FakeDisk(int(2.5 * G), gain=1 * G)
        run = FakeRun(disk)
        res = self.guard(disk, run)
        # 2.5 → 3.5 is above the trigger but below the 5G target, so it goes on
        self.assertEqual(res["cleaned"], ["zeta", "yank", "xray"])
        self.assertEqual(res["free_after"], int(5.5 * G))
        self.assertNotIn("whisky", run.probed)          # never even probed
        self.assertEqual(res["skipped"], {})
        self.assertEqual(len(self.lines), 1)
        self.assertNotIn("STILL", self.lines[0])

    def test_still_below_after_cleaning_says_so_and_returns(self):
        # (d) the run proceeds; the one line carries the warning
        self.clone("only")
        disk = FakeDisk(1 * G, gain=G // 2)
        run = FakeRun(disk)
        res = self.guard(disk, run)
        self.assertEqual(res["cleaned"], ["only"])
        self.assertEqual(res["free_after"], int(1.5 * G))
        self.assertEqual(len(self.lines), 1)
        self.assertTrue(self.lines[0].endswith("— STILL below 3.0G"), self.lines[0])


class UnknownBusySetTest(_GuardCase):
    """(e) When the busy set cannot be read, nothing is cleaned — not even
    probed — and every clone says why."""

    def _assert_refuses_everything(self, res, run, detail):
        self.assertEqual(run.calls, [])
        self.assertEqual(res["cleaned"], [])
        self.assertEqual(res["skipped"], {"a": "ledger unreadable", "b": "ledger unreadable"})
        self.assertIn(detail, res["ledger"])
        self.assertEqual(len(self.lines), 1)
        self.assertIn(f"[ledger: {res['ledger']}]", self.lines[0])

    def setUp(self):
        super().setUp()
        self.clone("a")
        self.clone("b")

    def test_missing_ledger_dir(self):
        self.ledger = self.tmp / "no-ledger"
        disk = FakeDisk(1 * G)
        run = FakeRun(disk)
        self._assert_refuses_everything(self.guard(disk, run), run, "no ledger dir")

    def test_corrupt_ledger_row(self):
        self.row("fine", "done", repo="a")
        (self.ledger / "torn.json").write_text('{"task_id": "torn", "sta', encoding="utf-8")
        disk = FakeDisk(1 * G)
        run = FakeRun(disk)
        self._assert_refuses_everything(self.guard(disk, run), run, "torn.json")

    def test_corrupt_job_file(self):
        self.jobs.mkdir()
        (self.jobs / "lj.json").write_text("not json", encoding="utf-8")
        disk = FakeDisk(1 * G)
        run = FakeRun(disk)
        self._assert_refuses_everything(self.guard(disk, run), run, "lj.json")

    def test_active_row_with_a_non_string_repo(self):
        (self.ledger / "odd.json").write_text(json.dumps(
            {"task_id": "odd", "state": "in_progress", "payload": {"repo": 7}}), encoding="utf-8")
        disk = FakeDisk(1 * G)
        run = FakeRun(disk)
        self._assert_refuses_everything(self.guard(disk, run), run, "odd.json")

    def test_a_missing_jobs_dir_is_normal(self):
        # jobs.start creates it lazily, so its absence means "no jobs", not "unknown"
        self.assertFalse(self.jobs.exists())
        self.assertEqual(dg.busy_checkouts(self.root, self.ledger, self.jobs), {})


class FailuresAreSkipsTest(_GuardCase):
    def test_each_failure_is_recorded_and_the_sweep_goes_on(self):
        # (f)
        for i, name in enumerate(("gitfail", "slowgit", "nojava", "badclean", "hung", "ok")):
            self.clone(name, mtime=1_000_000 + i)
        disk = FakeDisk(1 * G, gain=G // 10)
        run = FakeRun(
            disk,
            git_rc={("gitfail", "status"): 128},
            git_raise={("slowgit", "branch"): subprocess.TimeoutExpired(["git"], dg.GIT_TIMEOUT_S)},
            clean_raise={"nojava": FileNotFoundError(2, "No such file", "./gradlew"),
                         "hung": subprocess.TimeoutExpired(list(dg.CLEAN_ARGV), dg.CLEAN_TIMEOUT_S)},
            clean_rc={"badclean": 1})
        res = self.guard(disk, run)
        self.assertEqual(res["skipped"], {
            "gitfail": "git error: status rc=128",
            "slowgit": "git error: branch timed out",
            "nojava": "clean FileNotFoundError",
            "badclean": "clean rc=1",
            "hung": f"clean timed out ({dg.CLEAN_TIMEOUT_S}s)",
        })
        self.assertEqual(res["cleaned"], ["ok"])
        self.assertEqual(run.cleans, ["nojava", "badclean", "hung", "ok"])
        self.assertEqual(len(self.lines), 1)

    def test_budget_caps_each_clean_and_ends_the_sweep(self):
        # (g) the clock moves only when a clean runs
        self.clone("a", mtime=1_000_000)
        self.clone("b", mtime=2_000_000)
        self.clone("c", mtime=3_000_000)
        disk = FakeDisk(1 * G)
        run = FakeRun(disk, self.clock, clean_cost={"a": 400, "b": 250})
        res = self.guard(disk, run)
        timeouts = [kw["timeout"] for argv, kw in run.calls if tuple(argv) == dg.CLEAN_ARGV]
        self.assertEqual(timeouts[0], dg.CLEAN_TIMEOUT_S)
        self.assertEqual(timeouts[1], dg.BUDGET_S - 400)   # never past the budget
        self.assertEqual(res["cleaned"], ["a", "b"])
        self.assertEqual(res["skipped"], {"c": "budget exhausted"})
        self.assertNotIn("c", run.probed)
        self.assertEqual(res["elapsed_s"], 650)


class NeverRaisesTest(_GuardCase):
    def test_kill_switch_skips_even_the_measurement(self):
        # (h)
        self.clone("idle")
        for value in ("0", " 0 "):
            os.environ[dg.KILL_ENV] = value
            disk = mock.Mock(side_effect=AssertionError("measured"))
            run = FakeRun(FakeDisk(1 * G))
            res = self.guard(disk, run)
            self.assertTrue(res["disabled"])
            self.assertFalse(res["triggered"])
            disk.assert_not_called()
            self.assertEqual(run.calls, [])
        self.assertEqual(self.lines, [])

    def test_only_an_explicit_zero_disables(self):
        for value in ("", "1", "no", "false"):
            os.environ[dg.KILL_ENV] = value
            self.assertTrue(dg.enabled(), value)

    def test_unreadable_disk_is_one_line_and_no_clean(self):
        # (i) a disk_usage error
        self.clone("idle")
        run = FakeRun(FakeDisk(1 * G))
        res = self.guard(mock.Mock(side_effect=OSError(5, "EIO")), run)
        self.assertEqual(res["error"], "free space unreadable")
        self.assertEqual(run.calls, [])
        self.assertEqual(len(self.lines), 1)
        self.assertIn("free space unreadable", self.lines[0])

    def test_unexpected_error_from_run_is_a_skip(self):
        # (i) an exception type the probes do not expect
        self.clone("a", mtime=1_000_000)
        self.clone("b", mtime=2_000_000)
        disk = FakeDisk(1 * G)
        run = FakeRun(disk, git_raise={("a", "branch"): RuntimeError("boom")})
        res = self.guard(disk, run)
        self.assertEqual(res["skipped"], {"a": "error: RuntimeError"})
        self.assertEqual(res["cleaned"], ["b"])

    def test_an_error_outside_the_sweep_is_reported_not_raised(self):
        self.clone("idle")
        disk = FakeDisk(1 * G)
        with mock.patch.object(dg, "candidates", side_effect=RuntimeError("scan died")):
            res = self.guard(disk, FakeRun(disk))
        self.assertTrue(res["error"].startswith("RuntimeError: scan died"))
        self.assertEqual(len(self.lines), 1)
        self.assertIn("disk guard: error (RuntimeError", self.lines[0])

    def test_a_progress_sink_that_raises_is_ignored(self):
        self.clone("idle")
        disk = FakeDisk(1 * G, gain=5 * G)
        res = dg.ensure_free_space(progress=mock.Mock(side_effect=OSError("mailbox full")),
                                   worker_root=self.root, assignments_dir=self.ledger,
                                   jobs_dir=self.jobs, disk_usage=disk, run=FakeRun(disk),
                                   proc_cwds=lambda: [], clock=self.clock)
        self.assertEqual(res["cleaned"], ["idle"])


class CleanCommandTest(_GuardCase):
    def test_clean_is_exactly_gradlew_clean_no_daemon_in_the_clone(self):
        # (j)
        c = self.clone("idle")
        disk = FakeDisk(1 * G, gain=5 * G)
        run = FakeRun(disk)
        self.guard(disk, run)
        cleans = [(argv, kw) for argv, kw in run.calls if argv[0] != "git"]
        self.assertEqual(len(cleans), 1)
        argv, kw = cleans[0]
        self.assertEqual(argv, ["./gradlew", "clean", "--no-daemon"])
        self.assertEqual(kw["cwd"], str(c))
        self.assertEqual(kw["timeout"], dg.CLEAN_TIMEOUT_S)
        self.assertIs(kw["stdin"], subprocess.DEVNULL)

    def test_the_module_deletes_nothing_by_itself(self):
        # (k) the task's safety rule, pinned on the source: no rm/rmtree/unlink/
        # rmdir call, and the only command literal naming `clean` is CLEAN_ARGV
        tree = ast.parse(Path(dg.__file__).read_text(encoding="utf-8"))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                called.add(f.attr if isinstance(f, ast.Attribute)
                           else f.id if isinstance(f, ast.Name) else None)
        self.assertEqual(called & {"rmtree", "remove", "unlink", "rmdir", "removedirs"}, set())
        strings = {n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertEqual(strings & {"rm", "-rf", "-fd", "-fdx", "rmdir"}, set())
        with_clean = [n for n in ast.walk(tree) if isinstance(n, (ast.Tuple, ast.List))
                      and any(isinstance(e, ast.Constant) and e.value == "clean" for e in n.elts)]
        self.assertEqual([ast.literal_eval(n) for n in with_clean],
                         [("./gradlew", "clean", "--no-daemon")])


class HelpersTest(_GuardCase):
    def test_measure_walks_up_to_the_filesystem_it_would_live_on(self):
        disk = FakeDisk(7 * G)
        self.assertEqual(dg.measure(self.tmp / "not" / "yet", disk_usage=disk), (7 * G, 48 * G))
        self.assertEqual(disk.calls, [str(self.tmp)])
        self.assertIsNone(dg.measure(self.tmp, disk_usage=mock.Mock(side_effect=OSError)))

    def test_build_output_depth_and_what_is_not_walked(self):
        c = self.tmp / "c"
        (c / "a" / "b" / "c" / "build").mkdir(parents=True)          # depth 3
        (c / ".gradle" / "build").mkdir(parents=True)                # hidden
        (c / "node_modules" / "x" / "build").mkdir(parents=True)     # not Gradle's
        self.assertFalse(dg.has_build_output(c))
        (c / "feature" / "login" / "build").mkdir(parents=True)      # */*/build
        self.assertTrue(dg.has_build_output(c))

    def test_candidates_need_a_git_entry(self):
        self.clone("real")
        (self.root / "stash").mkdir()          # e.g. a long_job stash: not a checkout
        (self.root / "file.txt").write_text("x", encoding="utf-8")
        self.assertEqual([c.name for c in dg.candidates(self.root)], ["real"])
        self.assertEqual(dg.candidates(self.tmp / "nowhere"), [])

    def test_live_process_cwds_reads_proc_links(self):
        proc = self.tmp / "proc"
        (proc / "123").mkdir(parents=True)
        os.symlink(self.root, proc / "123" / "cwd")
        (proc / "self").mkdir()                          # not a pid
        (proc / str(os.getpid())).mkdir()
        os.symlink(self.root, proc / str(os.getpid()) / "cwd")   # this process: not a holder
        (proc / "456").mkdir()                           # exited mid-scan: no cwd link
        self.assertEqual(dg.live_process_cwds(proc), [(123, str(self.root))])
        self.assertIsNone(dg.live_process_cwds(self.tmp / "no-proc"))

    def test_progress_line_caps_the_skip_list(self):
        res = {"threshold": 3 * G, "target": 5 * G, "free_before": 2 * G, "free_after": 2 * G,
               "cleaned": [], "skipped": {f"clone{i:02d}": "held by a-long-task-slug (in_progress)"
                                          for i in range(30)}}
        line = dg.summarize(res)
        self.assertIn(", …(+", line)
        self.assertLess(len(line), dg.SKIP_TEXT_LIMIT + 200)
        self.assertTrue(line.endswith("— STILL below 3.0G"), line)


class DryRunTest(_GuardCase):
    def _main(self, argv, run, disk):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(dg, "WORKER_ROOT", self.root), \
             mock.patch.object(dg, "ASSIGNMENTS_DIR", self.ledger), \
             mock.patch.object(dg, "JOBS_DIR", self.jobs), \
             mock.patch.object(dg, "_run", run), \
             mock.patch.object(dg, "_disk_usage", disk), \
             mock.patch.object(dg, "_proc_cwds", lambda: []), \
             redirect_stdout(out), redirect_stderr(err):
            rc = dg.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_dry_run_prints_a_verdict_per_clone_and_cleans_nothing(self):
        # (l) through the MODULE seams, as the real CLI resolves them
        self.clone("idle")
        self.clone("nogradlew", gradlew=False)
        self.clone("wt-x", linked=True)
        disk = FakeDisk(1 * G, gain=5 * G)
        run = FakeRun(disk)
        rc, out, _ = self._main(["--dry-run"], run, disk)
        self.assertEqual(rc, 0)
        self.assertIn("  idle: would clean", out)
        self.assertIn("  nogradlew: skip: no gradlew", out)
        self.assertIn("  wt-x: skip: linked worktree", out)
        self.assertIn("below the trigger", out)
        self.assertIn(f"ledger: {self.ledger}", out)
        self.assertEqual(run.cleans, [])
        self.assertEqual(run.probed, {"idle"})        # the read-only probes did run
        self.assertEqual(disk.free, 1 * G)

    def test_without_dry_run_it_refuses(self):
        self.clone("idle")
        disk = FakeDisk(1 * G)
        run = FakeRun(disk)
        rc, out, err = self._main([], run, disk)
        self.assertEqual(rc, 2)
        self.assertIn("only --dry-run", err)
        self.assertEqual(run.calls, [])


class ImportWeightTest(unittest.TestCase):
    def test_import_pulls_in_no_third_party_module(self):
        # (m) scripts/health_report.py imports this under a bare /usr/bin/python3
        # (no venv, so no yaml): the guard must stay stdlib + stdlib-only siblings.
        code = ("import sys, todo_system.orchestrator.disk_guard; "
                "print('yaml' in sys.modules)")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(VAULT),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "False")


class SuiteIsolationTest(unittest.TestCase):
    """(n) todo_system/conftest.py's `_disk_guard_isolated` is live. Without it,
    any _execute test on a host below the trigger would walk the real ~/Worker
    (the HQ host had 2.2G free the day this was written). No env pop here."""

    def test_the_kill_switch_is_set_for_every_test(self):
        self.assertEqual(os.environ.get(dg.KILL_ENV), "0")
        self.assertFalse(dg.enabled())

    def test_every_path_seam_points_into_a_tmp_dir(self):
        real = {"WORKER_ROOT": paths.worker_root(),
                "ASSIGNMENTS_DIR": jobs.REPO_ROOT / "Worker" / "assignments",
                "JOBS_DIR": jobs.REPO_ROOT / "Worker" / "jobs"}
        for attr, path in real.items():
            value = Path(getattr(dg, attr))
            self.assertNotEqual(value, path, attr)
            self.assertIn("disk-guard", str(value), attr)

    def test_an_uninjected_call_does_nothing_at_all(self):
        disk = mock.Mock(side_effect=AssertionError("measured"))
        lines: list[str] = []
        res = dg.ensure_free_space(progress=lines.append, disk_usage=disk)
        self.assertTrue(res["disabled"])
        disk.assert_not_called()
        self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
