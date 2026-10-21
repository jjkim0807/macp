"""Tests for per-task repo resolution (general runner, P-converge)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import repos


class RepoNameTest(unittest.TestCase):
    def test_names(self):
        self.assertEqual(
            repos.repo_name("https://github.com/example-org/agent-commander.git"),
            "agent-commander")
        self.assertEqual(repos.repo_name("git@github.com:x/DripCoffee.git"), "DripCoffee")
        self.assertEqual(repos.repo_name("https://h/x/worldgraph"), "worldgraph")


class ReadTaskRepoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _md(self, body: str) -> Path:
        p = self.tmp / "t.md"
        p.write_text(body)
        return p

    def test_reads_repo_and_working_dir(self):
        md = self._md('---\nstatus: open\nrepo: "https://h/x/foo.git"\n'
                      'working_dir: sub/dir\n---\n# t\n')
        self.assertEqual(repos.read_task_repo(md),
                         ("https://h/x/foo.git", "sub/dir"))

    def test_vault_task_has_no_repo(self):
        md = self._md('---\nstatus: open\nworker_mode: auto\n---\n# t\n')
        self.assertEqual(repos.read_task_repo(md), (None, None))

    def test_placeholder_dash_is_none(self):
        md = self._md('---\nrepo: "-"\n---\n# t\n')
        self.assertEqual(repos.read_task_repo(md), (None, None))

    def test_missing_file(self):
        self.assertEqual(repos.read_task_repo(self.tmp / "nope.md"), (None, None))

    def test_model_tier_is_lowercased_and_placeholder_is_none(self):
        self.assertEqual(repos.read_task_model_tier(self._md('---\nmodel_tier: Heavy\n---\n')), "heavy")
        self.assertIsNone(repos.read_task_model_tier(self._md('---\nmodel_tier: "-"\n---\n')))
        self.assertIsNone(repos.read_task_model_tier(self._md('---\nstatus: open\n---\n')))
        # an unknown value passes through: the classifier ignores it, validate names it
        self.assertEqual(repos.read_task_model_tier(self._md('---\nmodel_tier: ultra\n---\n')), "ultra")

    def test_estimate_p90_is_read_verbatim(self):
        self.assertEqual(repos.read_task_estimate_p90(self._md('---\nestimate_p90: 1h30m\n---\n')), "1h30m")
        self.assertIsNone(repos.read_task_estimate_p90(self._md('---\nestimate_p90: "-"\n---\n')))


class ReadTaskWorkerModeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _md(self, body: str) -> Path:
        p = self.tmp / "t.md"
        p.write_text(body)
        return p

    def _mode(self, raw: str) -> str | None:
        return repos.read_task_worker_mode(
            self._md(f'---\nstatus: open\nworker_mode: {raw}\n---\n# t\n'))

    def test_bare_off_md_reads_off(self):
        # YAML 1.1 loads an unquoted off as boolean False; it must not fail open.
        for raw in ("off", "Off", "OFF"):
            with self.subTest(raw=raw):
                self.assertEqual(self._mode(raw), "off")

    def test_yaml_false_is_off(self):
        self.assertEqual(repos.task_worker_mode_from_fm({"worker_mode": False}), "off")
        for raw in ("no", "false"):
            with self.subTest(raw=raw):
                self.assertEqual(self._mode(raw), "off")

    def test_string_modes_unchanged(self):
        for raw, want in (("auto", "auto"), ("Auto", "auto"),
                          ('"off"', "off"), ('"OFF"', "off")):
            with self.subTest(raw=raw):
                self.assertEqual(self._mode(raw), want)

    def test_absent_blank_placeholder_none(self):
        self.assertIsNone(repos.read_task_worker_mode(self._md('---\nstatus: open\n---\n# t\n')))
        self.assertIsNone(self._mode('"-"'))
        self.assertIsNone(self._mode('""'))
        self.assertIsNone(repos.read_task_worker_mode(self.tmp / "nope.md"))

    def test_non_false_non_strings_do_not_block(self):
        # `0 == False` in Python: a numeric 0 must not read as off.
        for v in (True, 0, 1, 0.0, None, []):
            with self.subTest(v=v):
                self.assertIsNone(repos.task_worker_mode_from_fm({"worker_mode": v}))
        for raw in ("on", "0"):
            with self.subTest(raw=raw):
                self.assertIsNone(self._mode(raw))


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_checkout(self, name: str) -> Path:
        d = self.tmp / name
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        return d

    def test_task_repo_reuses_existing_checkout(self):
        self._fake_checkout("foo")
        got = repos.ensure_local("https://h/x/foo.git", worker_root=self.tmp, clone=False)
        self.assertEqual(got, self.tmp / "foo")

    def test_absent_without_clone_is_none(self):
        self.assertIsNone(
            repos.ensure_local("https://h/x/bar.git", worker_root=self.tmp, clone=False))

    def test_resolve_prefers_task_repo(self):
        self._fake_checkout("foo")
        got = repos.resolve({"repo": "https://h/x/foo.git"}, agent_repo=None,
                            worker_root=self.tmp, clone=False)
        self.assertEqual(got, self.tmp / "foo")

    def test_resolve_falls_back_to_agent_repo(self):
        agent_co = self._fake_checkout("agentco")
        got = repos.resolve({}, agent_repo=str(agent_co), worker_root=self.tmp)
        self.assertEqual(got, agent_co)

    def test_resolve_none_when_nothing(self):
        self.assertIsNone(repos.resolve({}, agent_repo=None, worker_root=self.tmp))


class NonGitDestRecoveryTest(unittest.TestCase):
    """clone=True 복구 동작: 체크아웃 자리에 `.git` 없는 디렉터리가 있으면
    비어있으면 그 자리에 클론, 아니면 `<dest>-stale-<ts>`로 치우고(삭제 금지) 재클론.
    url은 로컬 bare 리포 경로만 쓴다(github owner URL 리터럴 금지 — 공개 식별자 게이트)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.wr = self.tmp / "wr"            # worker_root
        self.wr.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bare(self, name: str) -> str:
        """Create a clonable bare repo `<tmp>/src/<name>.git` with one commit.
        Returns its path as the clone url; repo_name(url) == name so the
        checkout lands at worker_root/<name>."""
        _git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
        seed = self.tmp / "seed" / name
        seed.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(seed)], check=True)
        (seed / "README.md").write_text("seed\n", encoding="utf-8")
        subprocess.run(_git + ["-C", str(seed), "add", "."], check=True)
        subprocess.run(_git + ["-C", str(seed), "commit", "-q", "-m", "seed"], check=True)
        bare = self.tmp / "src" / f"{name}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
        return str(bare)

    def test_nonempty_nongit_dest_is_renamed_then_cloned(self):
        url = self._bare("foo")
        dest = repos.local_path(url, self.wr)
        dest.mkdir(parents=True)
        (dest / "keep.txt").write_text("human left this\n", encoding="utf-8")
        log: list[str] = []

        got = repos.ensure_local(url, worker_root=self.wr, clone=True, log=log)

        self.assertEqual(got, dest)
        self.assertTrue((dest / ".git").exists())
        self.assertTrue((dest / "README.md").exists())      # clone populated it
        stale = sorted(self.wr.glob("foo-stale-*"))
        self.assertEqual(len(stale), 1, "non-git dir should be moved aside, not deleted")
        self.assertEqual((stale[0] / "keep.txt").read_text(encoding="utf-8"),
                         "human left this\n")
        self.assertTrue(any("moved to" in line for line in log))
        self.assertTrue(any("cloned" in line for line in log))

    def test_empty_nongit_dest_is_cloned_in_place(self):
        url = self._bare("foo")
        dest = repos.local_path(url, self.wr)
        dest.mkdir(parents=True)                              # empty dir
        log: list[str] = []

        got = repos.ensure_local(url, worker_root=self.wr, clone=True, log=log)

        self.assertEqual(got, dest)
        self.assertTrue((dest / ".git").exists())
        self.assertEqual(list(self.wr.glob("foo-stale-*")), [],
                         "empty dir needs no rename")
        self.assertTrue(any("empty non-git dir" in line for line in log))

    def test_existing_git_checkout_is_untouched(self):
        url = self._bare("foo")
        dest = repos.local_path(url, self.wr)
        subprocess.run(["git", "init", "-q", str(dest)], check=True)   # real checkout
        marker = dest / "marker.txt"
        marker.write_text("mine\n", encoding="utf-8")
        log: list[str] = []

        got = repos.ensure_local(url, worker_root=self.wr, clone=True, log=log)

        self.assertEqual(got, dest)
        self.assertTrue(marker.exists() and marker.read_text(encoding="utf-8") == "mine\n")
        self.assertEqual(list(self.wr.glob("foo-stale-*")), [])
        self.assertEqual(log, [], "reuse branch never clones, so nothing is logged")

    def test_clone_false_leaves_nongit_dest_untouched_and_none(self):
        url = self._bare("foo")
        dest = repos.local_path(url, self.wr)
        dest.mkdir(parents=True)
        (dest / "keep.txt").write_text("human left this\n", encoding="utf-8")
        log: list[str] = []

        got = repos.ensure_local(url, worker_root=self.wr, clone=False, log=log)

        self.assertIsNone(got)
        self.assertFalse((dest / ".git").exists())
        self.assertTrue((dest / "keep.txt").exists())        # unmoved
        self.assertEqual(list(self.wr.glob("foo-stale-*")), [])
        self.assertEqual(log, [])

    def test_clone_failure_reason_is_captured(self):
        url = str(self.tmp / "does-not-exist" / "nope.git")   # bad local url
        log: list[str] = []

        got = repos.ensure_local(url, worker_root=self.wr, clone=True, log=log)

        self.assertIsNone(got)
        self.assertTrue(log and log[-1].startswith("clone failed:"))
        self.assertGreater(len(log[-1]), len("clone failed: "),
                           "git stderr tail should be carried, not an empty reason")


class WorktreeTest(unittest.TestCase):
    """실행별 worktree plumbing (`worktree_path` / `ensure_worktree`): 공유 클론에서
    `--detach` linked worktree를 `<worker_root>/<repo>.wt/<slug>`에 만들고 그 git
    common-dir이 공유 클론을 가리킴(AC1), 서로 다른 slug → 서로 다른 worktree + 재호출
    멱등(AC2), 경로 모양이 `wt-` sweep을 비껴감(AC3), 공유 클론 부재 + clone=False → None.
    url은 로컬 bare 리포 경로만 쓴다(github owner URL 리터럴 금지 — 공개 식별자 게이트)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.wr = self.tmp / "wr"            # worker_root
        self.wr.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bare(self, name: str) -> str:
        """Clonable bare repo `<tmp>/src/<name>.git` with one commit (worktree add
        needs a HEAD). repo_name(url) == name so the shared clone lands at
        worker_root/<name>. Mirrors NonGitDestRecoveryTest._bare."""
        _git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
        seed = self.tmp / "seed" / name
        seed.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(seed)], check=True)
        (seed / "README.md").write_text("seed\n", encoding="utf-8")
        subprocess.run(_git + ["-C", str(seed), "add", "."], check=True)
        subprocess.run(_git + ["-C", str(seed), "commit", "-q", "-m", "seed"], check=True)
        bare = self.tmp / "src" / f"{name}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(bare)], check=True)
        return str(bare)

    def _common_dir(self, wt: Path) -> Path:
        """`git -C <wt> rev-parse --git-common-dir`, resolved. git may answer with
        a path relative to the worktree, so resolve against it when not absolute."""
        out = subprocess.run(
            ["git", "-C", str(wt), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, check=True).stdout.strip()
        p = Path(out)
        return (p if p.is_absolute() else (wt / p)).resolve()

    def test_ensure_worktree_points_git_at_shared_clone(self):  # AC1
        url = self._bare("foo")
        wt = repos.ensure_worktree(url, "task-a", worker_root=self.wr, clone=True)
        base = repos.local_path(url, self.wr)
        self.assertEqual(wt, repos.worktree_path(url, "task-a", self.wr))
        self.assertTrue((wt / ".git").exists())
        self.assertEqual(self._common_dir(wt), (base / ".git").resolve(),
                         "worktree .git must point back at the shared clone")

    def test_distinct_slugs_make_distinct_worktrees(self):  # AC2 + 멱등
        url = self._bare("foo")
        base = repos.local_path(url, self.wr)
        wt_a = repos.ensure_worktree(url, "task-a", worker_root=self.wr, clone=True)
        wt_b = repos.ensure_worktree(url, "task-b", worker_root=self.wr, clone=True)
        self.assertNotEqual(wt_a, wt_b)
        self.assertTrue((wt_a / ".git").exists())
        self.assertTrue((wt_b / ".git").exists())
        self.assertEqual(self._common_dir(wt_a), (base / ".git").resolve())
        self.assertEqual(self._common_dir(wt_b), (base / ".git").resolve())
        # 멱등: 같은 slug 재호출 → 재-add 없이 같은 경로를 에러 없이 반환
        again = repos.ensure_worktree(url, "task-a", worker_root=self.wr, clone=True)
        self.assertEqual(again, wt_a)
        self.assertTrue((again / ".git").exists())

    def test_worktree_path_shape_no_wt_prefix(self):  # AC3
        url = str(self.tmp / "src" / "foo.git")   # pure: worktree_path touches no disk
        wt = repos.worktree_path(url, "task-a", self.wr)
        self.assertEqual(wt.name, "task-a")
        self.assertFalse(wt.name.startswith("wt-"))
        self.assertEqual(wt.parent.name, "foo.wt")
        # sweep은 parent==worker_root AND name startswith("wt-")를 둘 다 요구한다
        # (worker.py). 우리 parent는 worker_root보다 한 단계 깊어 첫 조건부터 불성립.
        self.assertEqual(wt.parent.parent.resolve(), self.wr.resolve())

    def test_absent_shared_clone_without_clone_is_none(self):
        url = self._bare("foo")   # bare exists, but no shared clone at worker_root/foo
        got = repos.ensure_worktree(url, "task-a", worker_root=self.wr, clone=False)
        self.assertIsNone(got)
        self.assertFalse(repos.worktree_path(url, "task-a", self.wr).exists(),
                         "no shared clone → no worktree dir is created")


class GitTimeoutTest(unittest.TestCase):
    """A wedged clone/prune/add must return None with a logged reason, never
    raise `subprocess.TimeoutExpired` into the caller (mirrors
    test_worker_git_timeout.py's pattern)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.wr = self.tmp / "wr"
        self.wr.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clone_timeout_returns_none_and_logs(self):
        exc = subprocess.TimeoutExpired(cmd=["git", "clone"], timeout=1)
        log: list[str] = []
        with mock.patch.object(repos.subprocess, "run", side_effect=exc):
            got = repos.ensure_local("https://h/x/foo.git", worker_root=self.wr,
                                     clone=True, log=log)
        self.assertIsNone(got)
        self.assertTrue(log and "timed out" in log[-1], log)

    def test_worktree_prune_timeout_returns_none_and_logs(self):
        base = self.wr / "foo"
        subprocess.run(["git", "init", "-q", str(base)], check=True)
        exc = subprocess.TimeoutExpired(cmd=["git", "worktree", "prune"], timeout=1)
        log: list[str] = []
        with mock.patch.object(repos.subprocess, "run", side_effect=exc):
            got = repos.ensure_worktree("https://h/x/foo.git", "task-a",
                                        worker_root=self.wr, clone=False, log=log)
        self.assertIsNone(got)
        self.assertTrue(log and "timed out" in log[-1], log)

    def test_worktree_add_timeout_returns_none_and_logs(self):
        base = self.wr / "foo"
        subprocess.run(["git", "init", "-q", str(base)], check=True)

        real_run = subprocess.run

        def fake_run(args, **kwargs):
            if "add" in args:
                raise subprocess.TimeoutExpired(cmd=args, timeout=1)
            return real_run(args, **kwargs)

        log: list[str] = []
        with mock.patch.object(repos.subprocess, "run", side_effect=fake_run):
            got = repos.ensure_worktree("https://h/x/foo.git", "task-a",
                                        worker_root=self.wr, clone=False, log=log)
        self.assertIsNone(got)
        self.assertTrue(log and "timed out" in log[-1], log)


class WorkerRootResolutionTest(unittest.TestCase):
    """AC3: the clone root comes from MACP_WORKER_ROOT / the `worker_root`
    config key, defaulting to ~/Worker.

    It used to be `DEFAULT_WORKER_ROOT`, a module constant frozen at import and
    baked into five function *defaults* — so no env var set after this module
    was first imported could ever reach them. Nothing here clones or touches
    the real clone root: the derivation functions are pure, and `ensure_local` /
    `ensure_worktree` are called with `clone=False`.
    """

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"MACP_WORKER_ROOT": "/tmp/w"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_local_path_follows_the_env(self):
        self.assertEqual(repos.local_path("https://h/x/foo.git"),
                         Path("/tmp/w/foo"))

    def test_worktree_path_follows_the_env(self):
        self.assertEqual(repos.worktree_path("https://h/x/foo.git", "task-a"),
                         Path("/tmp/w/foo.wt/task-a"))

    def test_ensure_local_looks_under_the_env_root(self):
        # No checkout at /tmp/w/foo and clone=False -> None, having looked
        # there and not under ~/Worker.
        with mock.patch.object(repos, "local_path",
                               wraps=repos.local_path) as lp:
            self.assertIsNone(repos.ensure_local("https://h/x/foo.git"))
        self.assertEqual(lp.call_args.args[1], Path("/tmp/w"))

    def test_ensure_worktree_resolves_the_root_once(self):
        """One call must not read the config twice — `ensure_worktree` resolves
        and passes the value down, so a mid-call change cannot split the paths."""
        calls: list[Path] = []
        with mock.patch.object(repos.paths, "worker_root",
                               side_effect=lambda *a, **k: calls.append(Path("/tmp/w"))
                               or Path("/tmp/w")):
            self.assertIsNone(repos.ensure_worktree("https://h/x/foo.git", "s"))
        self.assertEqual(len(calls), 1, calls)

    def test_explicit_argument_beats_the_env(self):
        self.assertEqual(repos.local_path("https://h/x/foo.git", "/explicit"),
                         Path("/explicit/foo"))

    def test_tilde_in_the_env_is_expanded(self):
        with mock.patch.dict(os.environ, {"MACP_WORKER_ROOT": "~/clones"}):
            self.assertEqual(repos.local_path("https://h/x/foo.git"),
                             Path.home() / "clones" / "foo")

    def test_default_without_the_env_is_still_home_worker(self):
        """Behaviour invariance: an unconfigured host keeps today's layout."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MACP_WORKER_ROOT", None)
            os.environ.pop("MACP_VAULT_ROOT", None)
            self.assertEqual(repos.local_path("https://h/x/foo.git"),
                             Path.home() / "Worker" / "foo")

    def test_resolve_uses_the_env_root_for_a_task_repo(self):
        with mock.patch.object(repos, "ensure_local",
                               return_value=None) as ensure:
            repos.resolve({"repo": "https://h/x/foo.git"}, None)
        self.assertEqual(ensure.call_args.args[1], Path("/tmp/w"))


if __name__ == "__main__":
    unittest.main()
