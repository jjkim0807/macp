"""The tracked vault state has to actually reach the remote.

Worker/assignments is kept out of .gitignore on purpose — "durable ledger a
cloud takeover inherits". Between 2026-07-30 and 2026-08-05 nothing committed
it: the v2 cutover moved scan_queue()'s upkeep into cmd_maintenance and left the
sync behind. Six days of ledger and run episodes existed only on one Mac while
everything looked healthy, because nothing local depends on the push.

These drive a real git repo rather than mocking subprocess: the failure being
guarded against is about what ends up in a commit, which mocks cannot show.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from todo_system import daemonctl, worker
from todo_system.orchestrator.runner import Runner


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)


class SyncVaultStateTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "t@t")
        _git(self.repo, "config", "user.name", "t")
        # a bare "remote" so push has somewhere to go
        self.remote = Path(tempfile.mkdtemp())
        _git(self.remote, "init", "-q", "--bare", "-b", "main")
        _git(self.repo, "remote", "add", "origin", str(self.remote))
        (self.repo / "seed").write_text("x", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "seed")
        _git(self.repo, "push", "-q", "origin", "main")
        self._orig_root = worker.ROOT
        worker.ROOT = self.repo

    def tearDown(self):
        worker.ROOT = self._orig_root
        shutil.rmtree(self.repo, ignore_errors=True)
        shutil.rmtree(self.remote, ignore_errors=True)

    def _write(self, rel, text="{}"):
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def test_commits_and_pushes_the_ledger(self):
        self._write("Worker/assignments/t.json")
        self.assertEqual(worker.sync_vault_state(), 1)
        # the point of the whole exercise: it is on the REMOTE, not just local
        remote_files = _git(self.remote, "ls-tree", "-r", "--name-only", "main").stdout
        self.assertIn("Worker/assignments/t.json", remote_files)

    def test_syncs_episodes_and_mailbox_too(self):
        self._write("Memory/episodes/e.md", "run record")
        self._write("Worker/agents/orchestrator/inbox/m.json")
        self.assertEqual(worker.sync_vault_state(), 2)

    def test_syncs_the_remote_host_transport(self):
        # Worker/remote is not a backup — it IS how a task reaches another
        # machine. Omitting it makes remote dispatch silently one-way.
        self._write("Worker/remote/windows/inbox/t.md", "# task")
        self.assertEqual(worker.sync_vault_state(), 1)
        self.assertIn("Worker/remote/windows/inbox/t.md",
                      _git(self.remote, "ls-tree", "-r", "--name-only", "main").stdout)

    def test_syncs_worker_files_that_are_not_in_a_known_subdir(self):
        # planner_policy.yaml is live config sitting directly under Worker/. An
        # enumeration of subdirectories misses it, and the Worker/ prefix is
        # exempt from the dirty-tree check, so the divergence would be silent.
        self._write("Worker/planner_policy.yaml", "cap: 2h")
        self.assertEqual(worker.sync_vault_state(), 1)
        self.assertIn("Worker/planner_policy.yaml",
                      _git(self.remote, "ls-tree", "-r", "--name-only", "main").stdout)

    def test_every_declared_path_is_actually_synced(self):
        # guards the list itself: a path added to VAULT_SYNC_PATHS but not
        # reachable by the add is the bug this test exists to catch
        for i, rel in enumerate(worker.VAULT_SYNC_PATHS):
            self._write(f"{rel}/probe{i}.txt", "x")
        self.assertEqual(worker.sync_vault_state(), len(worker.VAULT_SYNC_PATHS))

    def test_deletions_are_synced(self):
        # consumed mailbox messages are deletions; they piled up unpushed
        p = self._write("Worker/agents/orchestrator/inbox/m.json")
        worker.sync_vault_state()
        p.unlink()
        self.assertEqual(worker.sync_vault_state(), 1)
        self.assertNotIn("inbox/m.json",
                         _git(self.remote, "ls-tree", "-r", "--name-only", "main").stdout)

    def test_defers_when_a_stage_left_the_vault_on_a_branch(self):
        # The vault is the working tree stages check out into. Committing here
        # would put the ledger on a branch that may never merge, while
        # `push origin main` pushes a main without it.
        self._write("Worker/assignments/t.json")
        _git(self.repo, "checkout", "-q", "-b", "worker/some-task")
        self.assertEqual(worker.sync_vault_state(), 0)
        self.assertNotIn("sync vault state",
                         _git(self.repo, "log", "--oneline").stdout)

    def test_the_deferred_state_syncs_once_main_is_restored(self):
        self._write("Worker/assignments/t.json")
        _git(self.repo, "checkout", "-q", "-b", "worker/some-task")
        worker.sync_vault_state()                       # deferred
        _git(self.repo, "checkout", "-q", "main")       # restore_main equivalent
        self.assertEqual(worker.sync_vault_state(), 1)  # nothing was lost
        self.assertIn("Worker/assignments/t.json",
                      _git(self.remote, "ls-tree", "-r", "--name-only", "main").stdout)

    def test_defers_when_main_moved_but_the_index_is_stale(self):
        # 2026-09-25: main was advanced by a ref-only move while the index and
        # working tree stayed on the previous tree. `git add -A` happily staged
        # the old content and `git commit` wrote the WHOLE index, so
        # `chore: sync vault state (16 files)` (e056a1e35) landed as a reverse
        # diff that deleted the DanceMirror beta wiring of 7ff78bb55. Nothing
        # alarmed; a person found it and restored it by hand (05b1c4053).
        self._write("Worker/assignments/t.json")
        _git(self.repo, "checkout", "-q", "-b", "tmp")
        # the change lives OUTSIDE the synced paths, exactly as the lost commit
        # did — a path-limited comparison would wave the accident through
        (self.repo / "seed").write_text("wired", encoding="utf-8")
        _git(self.repo, "commit", "-qam", "feat: wire something")
        c2 = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        _git(self.repo, "checkout", "-q", "main")        # index/worktree back at C1
        _git(self.repo, "update-ref", "refs/heads/main", c2)   # ref only — the accident
        self.assertEqual(worker.sync_vault_state(), 0)
        self.assertNotIn("sync vault state",
                         _git(self.repo, "log", "--oneline").stdout)
        self.assertEqual(_git(self.repo, "show", "HEAD:seed").stdout, "wired")

    def test_the_stale_index_syncs_once_the_index_is_reconciled(self):
        # Deferring is a delay, not a loss — the mirror of
        # test_the_deferred_state_syncs_once_main_is_restored. Whoever puts the
        # index back (a person, or the next stage's checkout) gets the pending
        # state synced on the following tick, with the recovered commit intact.
        self._write("Worker/assignments/t.json")
        _git(self.repo, "checkout", "-q", "-b", "tmp")
        (self.repo / "seed").write_text("wired", encoding="utf-8")
        _git(self.repo, "commit", "-qam", "feat: wire something")
        c2 = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        _git(self.repo, "checkout", "-q", "main")
        _git(self.repo, "update-ref", "refs/heads/main", c2)
        self.assertEqual(worker.sync_vault_state(), 0)   # deferred
        _git(self.repo, "reset", "-q", "--mixed", "HEAD")  # index reconciled by hand
        self.assertEqual(worker.sync_vault_state(), 1)   # nothing was lost
        self.assertIn("Worker/assignments/t.json",
                      _git(self.remote, "ls-tree", "-r", "--name-only", "main").stdout)
        self.assertEqual(_git(self.repo, "show", "HEAD:seed").stdout, "wired")

    def test_ordinary_unstaged_dirt_does_not_defer(self):
        # Todo/items/ and Memory/index.md are dirty on nearly every tick
        # (ALLOWED_DIRTY_PREFIXES / _EXACT exist for exactly that reason). The
        # guard compares the INDEX to HEAD, so working-tree-only changes must
        # not trip it — over-firing here would stop the daemon syncing forever,
        # a quieter-but-longer outage than the one the guard fixes.
        self._write("Worker/assignments/t.json")
        self._write("Todo/items/x.md", "- [ ] thing")
        self._write("Memory/index.md", "last_synced: t0")
        self.assertEqual(worker.sync_vault_state(), 3)
        # and again with those files now TRACKED and merely modified on disk:
        # a check written against `git diff --quiet HEAD` would defer here
        self._write("Memory/index.md", "last_synced: t1")
        self.assertEqual(worker.sync_vault_state(), 1)
        self.assertIn("Todo/items/x.md",
                      _git(self.remote, "ls-tree", "-r", "--name-only", "main").stdout)

    def _pending_merge_with_a_clean_index(self):
        """Leave MERGE_HEAD present while the index still matches HEAD.

        This is the shape the incident had, and the reason the merge guard is not
        redundant with the index guard above: a plain CONFLICTED merge leaves the
        index dirty, so `diff --cached` already defers on that one. Only a merge
        whose index was put back to HEAD — what `--autostash` re-application and
        a failed `merge --abort` can leave behind — slips through to the commit.
        Both preconditions are asserted, so this fixture can never quietly stop
        reproducing the state and let the tests below pass on the other guard.
        """
        _git(self.repo, "branch", "-q", "side", "HEAD")
        _git(self.repo, "checkout", "-q", "side")
        (self.repo / "seed").write_text("incoming\n", encoding="utf-8")
        _git(self.repo, "commit", "-qam", "incoming change")
        _git(self.repo, "checkout", "-q", "main")
        (self.repo / "ours").write_text("ours\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-qm", "our change")   # a real divergence
        _git(self.repo, "merge", "--no-commit", "--no-ff", "side")
        # put the index back at HEAD; MERGE_HEAD survives a path checkout
        _git(self.repo, "checkout", "HEAD", "--", ".")
        self.assertEqual(
            _git(self.repo, "rev-parse", "--verify", "--quiet", "MERGE_HEAD").returncode,
            0, "fixture left no pending merge — nothing is being tested")
        self.assertEqual(
            _git(self.repo, "diff", "--cached", "--quiet").returncode, 0,
            "fixture left a DIRTY index — the index guard would defer and these "
            "tests would pass without the merge guard existing")

    def test_defers_while_a_merge_is_in_progress(self):
        # `git commit` with MERGE_HEAD present inherits that second parent and
        # publishes the WORKING TREE as the merge's resolution. That is how
        # `chore: sync vault state (13 files)` (36655ee53, 2026-11-09) became a
        # two-parent commit which dropped both task md files a just-merged PR had
        # brought in: the orchestrator's close had left pre-merge bytes on disk
        # and this commit recorded them as the merge result. The reflog action is
        # `commit (merge)`, which git writes only for a commit finalising
        # MERGE_HEAD, and `git show --stat` hides such a loss because a merge
        # prints the combined diff.
        self._pending_merge_with_a_clean_index()
        self._write("Worker/assignments/t.json")

        self.assertEqual(worker.sync_vault_state(), 0)
        self.assertNotIn("sync vault state",
                         _git(self.repo, "log", "--oneline").stdout)
        # specifically: no merge commit was manufactured
        parents = _git(self.repo, "log", "--format=%P", "-5").stdout.splitlines()
        self.assertFalse([p for p in parents if len(p.split()) > 1],
                         "a two-parent commit was created while merging")

    def test_the_deferred_state_syncs_once_the_merge_is_resolved(self):
        # Deferring is a delay, not a loss — the mirror of the other two deferral
        # pairs. Over-firing here would stop the vault syncing forever, which is
        # a quieter and longer outage than the one being fixed.
        self._pending_merge_with_a_clean_index()
        self._write("Worker/assignments/t.json")
        self.assertEqual(worker.sync_vault_state(), 0)      # deferred
        _git(self.repo, "merge", "--abort")                 # a person resolves it
        self.assertEqual(worker.sync_vault_state(), 1)      # nothing was lost
        self.assertIn("Worker/assignments/t.json",
                      _git(self.remote, "ls-tree", "-r", "--name-only", "main").stdout)

    def test_nothing_to_do_is_a_clean_noop(self):
        self.assertEqual(worker.sync_vault_state(), 0)
        before = _git(self.repo, "rev-parse", "HEAD").stdout
        self.assertEqual(worker.sync_vault_state(), 0)
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD").stdout, before)

    def test_leaves_untracked_work_outside_the_synced_paths_alone(self):
        # a stage's half-written file in the working tree must not be swept in
        self._write("Worker/assignments/t.json")
        self._write("scratch.py", "print(1)")
        worker.sync_vault_state()
        self.assertNotIn("scratch.py",
                         _git(self.repo, "ls-tree", "-r", "--name-only", "main").stdout)

    def test_survives_a_missing_path(self):
        self._write("Worker/assignments/t.json")
        self.assertEqual(worker.sync_vault_state(("Worker/assignments", "Nope")), 1)

    def test_push_failure_still_leaves_the_commit_local(self):
        self._write("Worker/assignments/t.json")
        _git(self.repo, "remote", "set-url", "origin", "/nonexistent/repo.git")
        self.assertEqual(worker.sync_vault_state(), 1)   # reported as synced
        log = _git(self.repo, "log", "--oneline", "-1").stdout
        self.assertIn("sync vault state", log)           # and committed locally


class SyncVaultRePushTest(unittest.TestCase):
    """When the first push is rejected (another host pushed first), sync rebases
    and pushes again — and the SECOND push's result must actually be checked, not
    assumed. Faked through `_run` so the push/rebase/re-push race is driven
    directly; the only real thing is the ROOT dir whose presence gates the add.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "Worker").mkdir()
        self._orig_root = worker.ROOT
        worker.ROOT = self.tmp

    def tearDown(self):
        worker.ROOT = self._orig_root
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake(self, push_results, pull_rc=0, head="main", staged="Worker/a.json",
              diff_rc=0, in_progress=()):
        """`in_progress` names the merge/rebase refs that EXIST in this fake repo
        (e.g. ("MERGE_HEAD",)); by default none do, which is the steady state."""
        pushes = list(push_results)
        calls = []

        def run(argv, **kw):
            calls.append(list(argv[1:]))
            sub = argv[1]
            if sub == "rev-parse" and "--verify" in argv:
                # the merge/rebase-in-progress probe: rc 0 = the ref exists
                ref = argv[-1]
                return subprocess.CompletedProcess(
                    argv, 0 if ref in in_progress else 1, "", "")
            if sub == "rev-parse":
                return subprocess.CompletedProcess(argv, 0, head + "\n", "")
            if sub == "diff":
                # rc 0 = index matches HEAD (and, for --name-only, the staged
                # list). A non-zero rc is git failing to answer at all.
                return subprocess.CompletedProcess(
                    argv, diff_rc, staged + "\n" if diff_rc == 0 else "", "")
            if sub == "push":
                rc = pushes.pop(0) if pushes else 0
                return subprocess.CompletedProcess(
                    argv, rc, "", "rejected" if rc else "")
            if sub == "rebase" and argv[2:3] != ["--abort"]:
                return subprocess.CompletedProcess(
                    argv, pull_rc, "", "conflict" if pull_rc else "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        return run, calls

    def _sync(self, run):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            n = worker.sync_vault_state(_run=run)
        return n, buf.getvalue()

    def _pushes(self, calls):
        return [c for c in calls if c[0] == "push"]

    def test_a_rejected_first_push_rebases_and_re_pushes(self):
        run, calls = self._fake(push_results=[1, 0])
        n, out = self._sync(run)
        self.assertEqual(n, 1)
        self.assertEqual(len(self._pushes(calls)), 2)    # first + the re-push
        self.assertNotIn("still rejected", out)          # the re-push landed

    def test_a_still_rejected_second_push_is_logged_not_swallowed(self):
        run, calls = self._fake(push_results=[1, 1])
        n, out = self._sync(run)
        self.assertEqual(n, 1)                            # commit stays local
        self.assertIn("push still rejected", out)
        self.assertEqual(len(self._pushes(calls)), 2)

    def test_a_failed_rebase_skips_the_second_push_and_says_so(self):
        run, calls = self._fake(push_results=[1], pull_rc=1)
        n, out = self._sync(run)
        self.assertEqual(n, 1)
        self.assertEqual(len(self._pushes(calls)), 1)    # 2nd push skipped
        self.assertIn("rebase before re-push failed", out)
        self.assertIn(["rebase", "--abort"], calls)       # no half-done rebase left

    def test_re_push_rebases_onto_the_tracking_ref_not_fetch_head(self):
        run, calls = self._fake(push_results=[1, 0])
        self._sync(run)
        self.assertNotIn("pull", [c[0] for c in calls])
        self.assertIn(["fetch", "origin", "main"], calls)
        self.assertIn(["rebase", "origin/main"], calls)

    def test_a_clean_first_push_neither_pulls_nor_re_pushes(self):
        run, calls = self._fake(push_results=[0])
        n, out = self._sync(run)
        self.assertEqual(n, 1)
        self.assertEqual(len(self._pushes(calls)), 1)
        self.assertNotIn("rebase", [c[0] for c in calls])
        self.assertEqual(out, "")

    def test_an_unreadable_index_comparison_defers(self):
        # Fail-safe: rc 1 means "the index differs", anything else means git
        # could not tell us, and neither may authorise a commit. "I cannot
        # tell" must never end in a `git commit` of the whole index.
        run, calls = self._fake(push_results=[0], diff_rc=128)
        n, out = self._sync(run)
        self.assertEqual(n, 0)
        self.assertEqual([c for c in calls if c[0] in ("commit", "push")], [])
        self.assertIn("index is not at HEAD", out)

    def test_every_half_done_operation_defers_before_anything_is_staged(self):
        # All four refs mean "git is mid-operation and `git commit` would finish
        # it with whatever is on disk". The guard must run before add/commit/push
        # so nothing is staged, and must name the ref so the log says which
        # operation is stuck rather than the generic index diagnosis.
        for ref in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD"):
            with self.subTest(ref=ref):
                run, calls = self._fake(push_results=[0], in_progress=(ref,))
                n, out = self._sync(run)
                self.assertEqual(n, 0)
                self.assertEqual(
                    [c for c in calls if c[0] in ("add", "commit", "push")], [])
                self.assertIn(ref, out)

    def test_the_steady_state_does_not_trip_the_in_progress_guard(self):
        # None of those refs exist on an ordinary tick, and a completed merge or
        # rebase clears them — over-firing here would stop the vault syncing
        # indefinitely, a quieter and longer outage than the loss it prevents.
        run, calls = self._fake(push_results=[0])
        n, out = self._sync(run)
        self.assertEqual(n, 1)
        self.assertIn(["commit", "-m", "chore: sync vault state (1 files)"], calls)
        self.assertEqual(out, "")


class RotateLogsTest(unittest.TestCase):
    """Nothing bounded the daemon logs; todo-sync.out.log reached 35 MB."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _log(self, name, size):
        p = self.d / name
        p.write_bytes(b"line\n" * (size // 5))
        return p

    def test_small_logs_are_left_alone(self):
        p = self._log("small.log", 1000)
        self.assertEqual(worker.rotate_logs(self.d, max_bytes=5000), [])
        self.assertEqual(p.stat().st_size, 1000)

    def test_oversized_log_is_truncated_in_place(self):
        p = self._log("big.log", 50_000)
        ino = p.stat().st_ino
        self.assertEqual(worker.rotate_logs(self.d, max_bytes=10_000, keep_bytes=2_000),
                         ["big.log"])
        self.assertEqual(p.stat().st_size, 0)
        # same inode: launchd holds this file open and must keep writing to it
        self.assertEqual(p.stat().st_ino, ino)

    def test_tail_is_preserved_in_the_archive(self):
        p = self.d / "big.log"
        p.write_text("".join(f"line{i}\n" for i in range(20_000)), encoding="utf-8")
        worker.rotate_logs(self.d, max_bytes=1000, keep_bytes=2000)
        archived = (self.d / "big.log.1").read_text(encoding="utf-8")
        self.assertIn("line19999", archived)          # the tail, which is the useful part
        self.assertNotIn("line0\n", archived)         # not the whole history
        self.assertTrue(archived.startswith("line"))  # no partial first line


class RestartStaleDaemonsTest(unittest.TestCase):
    """macp edits its own source, and the executor of those edits is resident.

    On 2026-08-06 the runner was 7 commits behind its own repo — including a
    security fix — because nothing restarted it. Its verify stage tested the new
    code in the working tree while the daemon dispatching it ran the old.
    """

    LABEL = worker.RUNNER_LABEL

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.paused = self.d / "PAUSED"       # absent unless a test creates it
        self.state = self.d / "daemon-restart.json"
        self.addCleanup(shutil.rmtree, self.d, True)

    def _entry(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def _run(self, pid, started, code_ts, rc=0, labels=None, idle=True,
             now=10_000_000.0):
        calls = []

        def fake(argv, **kw):
            calls.append(argv)
            # health_report.gather_code_time() shells `git -C <vault> log ... %cI`
            if argv[0] == "git" and "log" in argv:
                iso = dt.datetime.fromtimestamp(code_ts, dt.timezone.utc).isoformat()
                return subprocess.CompletedProcess(argv, 0, iso, "")
            if argv[0] == "launchctl" and argv[1] == "kickstart":
                return subprocess.CompletedProcess(argv, rc, "", "boom" if rc else "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with mock.patch("subprocess.run", side_effect=fake), \
             mock.patch.object(daemonctl, "IS_DARWIN", True), \
             mock.patch.object(worker, "_daemon_pid", return_value=pid), \
             mock.patch.object(worker, "_process_start_ts", return_value=started), \
             mock.patch.object(worker, "_daemon_idle", return_value=idle), \
             mock.patch.object(worker, "PAUSED_FILE", self.paused), \
             mock.patch.object(worker, "RESTART_STATE_FILE", self.state):
            out = worker.restart_stale_daemons(labels or (self.LABEL,), now=now)
        return out, calls

    @staticmethod
    def _kickstarts(calls):
        return [c for c in calls if c[0] == "launchctl" and c[1] == "kickstart"]

    def test_restarts_a_daemon_older_than_the_code(self):
        out, calls = self._run(pid=123, started=1000, code_ts=2000)
        self.assertEqual(out, [self.LABEL])
        self.assertTrue(any(c[0] == "launchctl" and c[1] == "kickstart" for c in calls))

    def test_leaves_a_daemon_newer_than_the_code_alone(self):
        out, calls = self._run(pid=123, started=3000, code_ts=2000)
        self.assertEqual(out, [])
        self.assertFalse(any(c[0] == "launchctl" for c in calls if len(c) > 1))

    def test_skips_a_daemon_that_is_not_resident(self):
        # periodic jobs respawn with fresh code; restarting them is meaningless
        out, _ = self._run(pid=None, started=1000, code_ts=2000)
        self.assertEqual(out, [])

    def test_a_failed_kickstart_is_reported_not_claimed(self):
        out, _ = self._run(pid=123, started=1000, code_ts=2000, rc=1)
        self.assertEqual(out, [])

    def test_prompt_edits_do_not_trigger_restarts(self):
        # Stage prompts are read fresh at each stage spawn, so editing them — or
        # docs, or tasks — must not count as a code change. Asserted against the
        # ONE definition of that set: worker restarts on health_report's tuple and
        # no longer keeps a second copy that could be fixed on one side only.
        hr = worker._health_report()
        self.assertIsNotNone(hr, "health_report must load; restarts depend on it")
        self.assertEqual(hr.CODE_TIME_PATHS, ("todo_system", "scripts"))
        for untracked in (".claude", "Todo", "Worker", "Memory"):
            self.assertNotIn(untracked, hr.CODE_TIME_PATHS)
        self.assertFalse(hasattr(worker, "RESTART_TRIGGER_PREFIXES"),
                         "the duplicated path constant must stay deleted")

    def test_paused_blocks_every_restart(self):
        # A pause is a person saying stop. It outranks staleness.
        self.paused.write_text("", encoding="utf-8")
        out, calls = self._run(pid=123, started=1000, code_ts=2000)
        self.assertEqual(out, [])
        self.assertEqual(self._kickstarts(calls), [])

    def test_a_busy_daemon_is_deferred_not_restarted(self):
        # kickstart -k is a SIGTERM; stale-but-working outranks stale-and-killed
        out, calls = self._run(pid=123, started=1000, code_ts=2000, idle=False)
        self.assertEqual(out, [])
        self.assertEqual(self._kickstarts(calls), [])

    def test_restart_target_set_is_exactly_the_three_resident_daemons(self):
        # Interval jobs (orchestrator/brief/health/macp-monitor) re-exec per tick
        # and already run current code — restarting them is meaningless, and the
        # gate table is what keeps them out. The dashboard, by contrast, is a
        # resident server that holds whatever code was on disk when it started,
        # so it belongs with the runner and todo-sync.
        self.assertEqual(worker.LONG_LIVED_LABELS,
                         (worker.RUNNER_LABEL, worker.TODO_SYNC_LABEL,
                          worker.DASHBOARD_LABEL))
        # The invariant that keeps a label from being added without its gate:
        # a LONG_LIVED_LABELS entry with no IDLE_GATES entry defers forever.
        self.assertEqual(set(worker.IDLE_GATES), set(worker.LONG_LIVED_LABELS))
        # Interval labels are built on the SAME prefix the resident labels use
        # (whatever this host resolved), so the NotIn checks cannot pass merely
        # because a hard-coded org prefix differs from the resolved one.
        sb = worker.RUNNER_LABEL[: -len("runner.worker-default")]
        self.assertTrue(sb.endswith("secondbrain."), sb)
        for interval in (sb + "orchestrator",
                         sb + "brief",
                         sb + "health",
                         sb + "macp-monitor",
                         sb + "maintenance"):
            self.assertNotIn(interval, worker.LONG_LIVED_LABELS)

    def test_dashboard_stale_is_restarted(self):
        # A resident dashboard older than the code on disk is a restart target,
        # exactly like the runner and todo-sync. (This flows through the _run
        # fixture, which stubs _daemon_idle=True — so it proves the label is a
        # target, NOT that its real gate is wired; that is the next test's job.)
        out, calls = self._run(pid=123, started=1000, code_ts=2000,
                               labels=(worker.DASHBOARD_LABEL,))
        self.assertEqual(out, [worker.DASHBOARD_LABEL])
        self.assertTrue(any(c[0] == "launchctl" and c[1] == "kickstart"
                            for c in calls))

    def test_dashboard_gate_is_always_idle(self):
        # Deliberately does NOT use _run: that fixture mocks worker._daemon_idle,
        # so it cannot tell a registered gate from a missing one. Call the real
        # functions. Both must hold: the gate itself returns True (a stateless
        # read-only server is always idle), AND it is reachable through
        # _daemon_idle — i.e. actually registered in IDLE_GATES. A label added
        # without its gate would make _daemon_idle fall through to False here.
        self.assertTrue(worker._dashboard_idle(123))
        self.assertTrue(worker._daemon_idle(worker.DASHBOARD_LABEL, 123))

    def test_a_label_with_no_idle_gate_is_never_restarted(self):
        # fail-closed: not knowing what a daemon does cannot authorise a SIGTERM
        self.assertFalse(worker._daemon_idle("com.unknown.daemon", 123))

    def test_restart_budget_is_capped_and_escalates_exactly_once(self):
        # Repeatedly kickstarting the daemon that dispatches work stops the whole
        # system. The budget is hard, and the page fires once — an alert that
        # repeats every tick only teaches you to ignore it.
        pages = []
        step = worker.RESTART_COOLDOWN_S
        with mock.patch.object(worker, "kakao_notify", side_effect=pages.append):
            for i in range(worker.RESTART_MAX_ATTEMPTS):
                out, _ = self._run(pid=123, started=1000, code_ts=2000,
                                   now=10_000_000.0 + i * step)
                self.assertEqual(out, [self.LABEL], f"attempt {i + 1} should fire")
            for i in (3, 4, 5):
                out, calls = self._run(pid=123, started=1000, code_ts=2000,
                                       now=10_000_000.0 + i * step)
                self.assertEqual(out, [])
                self.assertEqual(self._kickstarts(calls), [])
        self.assertEqual(len(pages), 1, "escalation must page once, not every tick")
        self.assertTrue(self._entry()[self.LABEL]["escalated"])

    def test_a_retry_inside_the_cooldown_is_blocked(self):
        out, _ = self._run(pid=123, started=1000, code_ts=2000, now=10_000_000.0)
        self.assertEqual(out, [self.LABEL])
        out, calls = self._run(pid=123, started=1000, code_ts=2000,
                               now=10_000_000.0 + worker.RESTART_COOLDOWN_S - 1)
        self.assertEqual(out, [])
        self.assertEqual(self._kickstarts(calls), [])

    def test_deferring_a_busy_daemon_does_not_spend_the_budget(self):
        # otherwise a daemon that is merely busy burns all three attempts without
        # a single restart ever being tried
        step = worker.RESTART_COOLDOWN_S
        for i in range(5):
            self._run(pid=123, started=1000, code_ts=2000, idle=False,
                      now=10_000_000.0 + i * step)
        self.assertEqual(self._entry()[self.LABEL]["attempts"], 0)
        out, _ = self._run(pid=123, started=1000, code_ts=2000,
                           now=10_000_000.0 + 5 * step)
        self.assertEqual(out, [self.LABEL], "full budget survives the deferrals")

    # --- deferral streak surfacing -------------------------------------------
    # A deferral must not spend the restart budget, which is why a permanently
    # busy idle gate is invisible: attempts stays 0, the cap never engages, the
    # one-shot escalation never fires. Observed 2026-08-18 — todo-sync deferred
    # 169 consecutive times over four days with attempts still 0, because the
    # then-pgrep-based _todo_sync_idle could not return idle at all. That gate is
    # heartbeat-based since 2026-08-21 (IdleGateTest.test_uv_wrapped_daemon_never_reads_idle
    # pins the fix), but the alert stays: it is the only surfacing for ANY
    # long-lived deferral streak, whatever its cause. These pin that alert.

    def _defer_for(self, hours, pages, start=10_000_000.0, step=300.0):
        """Defer every `step` seconds for `hours`, collecting pages."""
        with mock.patch.object(worker, "kakao_notify", side_effect=pages.append):
            t = start
            while t < start + hours * 3600:
                self._run(pid=123, started=1000, code_ts=2000, idle=False, now=t)
                t += step
        return t

    def test_consecutive_deferrals_page_after_the_time_threshold(self):
        pages = []
        # just under the threshold: still silent
        self._defer_for(5, pages)
        self.assertEqual(pages, [], "must not page before the threshold")
        self.assertGreater(self._entry()[self.LABEL]["deferrals"], 0)
        # crossing it pages exactly once, not once per tick
        self._defer_for(2, pages, start=10_000_000.0 + 5 * 3600)
        self.assertEqual(len(pages), 1, "one page for the streak, not one per tick")
        self.assertIn(self.LABEL, pages[0])

    def test_deferral_page_re_nags_daily_not_every_tick(self):
        # a single alert that gets missed leaves the system quietly stale for
        # days — which is exactly what happened. So it must repeat, but slowly.
        pages = []
        self._defer_for(7, pages)
        self.assertEqual(len(pages), 1)
        self._defer_for(20, pages, start=10_000_000.0 + 7 * 3600)
        self.assertEqual(len(pages), 1, "no second page inside the re-nag window")
        self._defer_for(6, pages, start=10_000_000.0 + 27 * 3600)
        self.assertEqual(len(pages), 2, "re-nags once a day until someone acts")

    def test_deferrals_never_spend_the_restart_budget(self):
        # criterion 5: a deferral is "not now", a failure is "broken". Sharing one
        # counter would let a merely-busy daemon exhaust its attempts without a
        # single restart ever being tried.
        pages = []
        end = self._defer_for(30, pages)
        self.assertGreaterEqual(len(pages), 1, "the streak must have been surfaced")
        self.assertEqual(self._entry()[self.LABEL]["attempts"], 0,
                         "deferrals must not consume attempts")
        out, _ = self._run(pid=123, started=1000, code_ts=2000, now=end)
        self.assertEqual(out, [self.LABEL], "full budget survives 30h of deferrals")

    def test_a_restart_attempt_clears_the_deferral_streak(self):
        pages = []
        end = self._defer_for(7, pages)
        self.assertGreater(self._entry()[self.LABEL]["deferrals"], 0)
        out, _ = self._run(pid=123, started=1000, code_ts=2000, now=end)
        self.assertEqual(out, [self.LABEL])
        entry = self._entry()[self.LABEL]
        self.assertEqual(entry["deferrals"], 0, "an attempt breaks the streak")
        self.assertEqual(entry["first_defer_ts"], 0.0)
        self.assertEqual(entry["defer_notified_ts"], 0.0)

    def test_a_legacy_state_entry_without_the_new_fields_still_works(self):
        # the live daemon-restart.json predates these keys; reads must default,
        # not KeyError, or the maintenance tick dies on the first upgrade run
        self.state.write_text(json.dumps(
            {self.LABEL: {"attempts": 0, "last_ts": 0.0, "code_ts": 2000,
                          "escalated": False}}), encoding="utf-8")
        with mock.patch.object(worker, "kakao_notify"):
            out, calls = self._run(pid=123, started=1000, code_ts=2000, idle=False)
        self.assertEqual(out, [])
        self.assertEqual(self._kickstarts(calls), [])
        self.assertEqual(self._entry()[self.LABEL]["deferrals"], 1)

    def test_unreadable_deferral_counters_do_not_crash_or_fake_a_page(self):
        # same fail-safe posture as the attempts/last_ts parse above
        self.state.write_text(json.dumps(
            {self.LABEL: {"attempts": 0, "last_ts": 0.0, "code_ts": 2000,
                          "escalated": False, "deferrals": "many",
                          "first_defer_ts": None, "defer_notified_ts": "x"}}),
            encoding="utf-8")
        pages = []
        with mock.patch.object(worker, "kakao_notify", side_effect=pages.append):
            out, _ = self._run(pid=123, started=1000, code_ts=2000, idle=False)
        self.assertEqual(out, [])
        self.assertEqual(pages, [], "a corrupt counter must not manufacture an alert")
        self.assertEqual(self._entry()[self.LABEL]["deferrals"], 1, "streak restarts")

    def test_cooldown_ticks_are_not_counted_as_deferrals(self):
        # cooling down is not "the daemon is busy" — conflating them would inflate
        # the streak and page for a restart that is simply waiting its turn
        out, _ = self._run(pid=123, started=1000, code_ts=2000, now=10_000_000.0)
        self.assertEqual(out, [self.LABEL])
        self._run(pid=123, started=1000, code_ts=2000, idle=False,
                  now=10_000_000.0 + 60)
        self.assertEqual(self._entry()[self.LABEL]["deferrals"], 0)

    def test_a_corrupt_state_file_blocks_restarts_instead_of_resetting_the_count(self):
        # fail-closed: a counter that reads 0 because the file is broken would
        # re-SIGTERM the dispatcher every tick forever
        self.state.write_text("{not json", encoding="utf-8")
        out, calls = self._run(pid=123, started=1000, code_ts=2000)
        self.assertEqual(out, [])
        self.assertEqual(self._kickstarts(calls), [])

    def test_a_newer_commit_opens_a_fresh_budget(self):
        step = worker.RESTART_COOLDOWN_S
        with mock.patch.object(worker, "kakao_notify"):
            for i in range(worker.RESTART_MAX_ATTEMPTS + 1):
                self._run(pid=123, started=1000, code_ts=2000,
                          now=10_000_000.0 + i * step)
        out, _ = self._run(pid=123, started=1000, code_ts=2000,
                           now=10_000_000.0 + 9 * step)
        self.assertEqual(out, [], "budget for that commit is spent")
        out, _ = self._run(pid=123, started=1000, code_ts=5000,
                           now=10_000_000.0 + 10 * step)
        self.assertEqual(out, [self.LABEL], "a new commit is a new window")

    def test_a_daemon_that_comes_back_fresh_clears_its_history(self):
        self._run(pid=123, started=1000, code_ts=2000, now=10_000_000.0)
        self.assertIn(self.LABEL, self._entry())
        # restarted process now reports a start time after the code: not stale
        self._run(pid=123, started=9000, code_ts=2000,
                  now=10_000_000.0 + worker.RESTART_COOLDOWN_S)
        self.assertEqual(self._entry(), {})

    def test_no_test_path_ever_shells_a_real_launchctl(self):
        # the suite must never restart the machine's actual daemons
        with mock.patch("subprocess.run", side_effect=AssertionError("real subprocess")):
            with mock.patch.object(worker, "PAUSED_FILE", self.d / "nope"):
                self.assertFalse(worker._daemon_idle("com.unknown.daemon", 1))


class IdleGateTest(unittest.TestCase):
    """A wrong "idle" does not delay a run — it SIGTERMs one mid-stage.

    So both runner signals must agree, and anything unreadable counts as busy.
    """

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        (self.d / "assignments").mkdir()
        self.addCleanup(shutil.rmtree, self.d, True)

    def _row(self, name, **over):
        row = {"task_id": name, "agent_id": "ai-worker", "corr_id": "c1",
               "state": "done"}
        row.update(over)
        (self.d / "assignments" / f"{name}.json").write_text(
            json.dumps(row), encoding="utf-8")

    def _rhb(self):
        return self.d / "runner_heartbeat"

    def _runner_idle(self, pgrep_out="", pgrep_rc=1, heartbeat=None):
        """`heartbeat=None` means no file at all — the runner is between ticks.

        That is the default because it is the steady state, and because it is
        exactly the reading the ledger's row state used to override: every case
        below that expects idle is one where nothing is executing, whatever the
        rows happen to say. Pass an mtime to stamp a heartbeat instead.
        """
        def fake(argv, **kw):
            if argv[0] == "pgrep":
                return subprocess.CompletedProcess(argv, pgrep_rc, pgrep_out, "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        hb = self._rhb()
        if heartbeat is not None:
            hb.write_text("tick", encoding="utf-8")
            os.utime(hb, (heartbeat, heartbeat))
        with mock.patch("subprocess.run", side_effect=fake), \
             mock.patch.object(worker, "WORKER_DIR", self.d), \
             mock.patch.object(worker, "RUNNER_HEARTBEAT_PATH", hb):
            return worker._runner_idle(999)

    def test_idle_when_ledger_quiet_and_no_claude_process(self):
        self._row("finished")
        self.assertTrue(self._runner_idle())

    def test_dispatched_rows_without_a_live_tick_are_idle(self):
        # 2026-08-21 moksori-2, and the reason this gate was rewritten: while
        # the usage guard holds, run_forever polls with only_bypass=True and
        # leaves every non-bypass envelope queued and un-acked, so its ledger
        # row sits at `dispatched` for DAYS with no process behind it. Reading
        # that as busy deferred the runner restart 73 times in 6 hours. A row
        # records what the orchestrator DECIDED; only the tick heartbeat knows
        # what is executing. (This assertion is the inverse of the one that
        # stood here until 2026-08-23 — that test encoded the defect.)
        for state in ("dispatched", "accepted", "in_progress"):
            with self.subTest(state=state):
                self._row("live", state=state)
                self.assertTrue(self._runner_idle())

    def test_busy_while_a_stage_tick_is_live(self):
        # The signal that replaces the row state. The runner touches this before
        # handle() and again at the top of every stage, so a fresh file means a
        # stage is mid-flight and a SIGTERM would kill it. Note pgrep finds
        # nothing here: the heartbeat has to stand on its own, because it also
        # covers the seams between stages where no `claude -p` is alive.
        self._row("live", state="dispatched")
        self.assertFalse(self._runner_idle(heartbeat=time.time()))

    def test_stale_runner_heartbeat_is_a_corpse_not_work(self):
        # A pipeline re-touches this every stage, so a file older than the
        # cutoff was left behind by a tick that died mid-run. Reading a corpse
        # as busy would freeze the gate at "busy" forever — the very
        # never-restarts defect this replaced, rebuilt from another road.
        self._row("finished")
        self.assertTrue(self._runner_idle(
            heartbeat=time.time() - worker.RUNNER_HEARTBEAT_STALE_S - 1))

    def test_unreadable_runner_heartbeat_counts_as_busy(self):
        # "I cannot tell" must not authorise a SIGTERM. _pgrep is booby-trapped
        # to prove the answer comes from the heartbeat branch and short-circuits
        # there, rather than falling through to a quiet process table.
        self._row("finished")
        bad = mock.Mock()
        bad.stat.side_effect = OSError("denied")
        with mock.patch.object(worker, "WORKER_DIR", self.d), \
             mock.patch.object(worker, "RUNNER_HEARTBEAT_PATH", bad), \
             mock.patch.object(worker, "_pgrep",
                               side_effect=AssertionError("unreached")):
            self.assertFalse(worker._runner_idle(999))

    def test_runner_stale_cutoff_outlasts_a_single_stage(self):
        # The cutoff is not a guess about pipeline length — it is 1.5x the hard
        # bound between two consecutive touches, which is one stage. If anyone
        # raises stage_timeout past the cutoff, a legitimately-running stage
        # starts reading as a corpse and this gate resumes killing runs
        # mid-stage, so the relationship is pinned rather than commented.
        from todo_system.orchestrator.runner import StagedExecutor
        self.assertGreater(
            worker.RUNNER_HEARTBEAT_STALE_S, StagedExecutor.stage_timeout,
            "a heartbeat re-touched once per stage must not be able to go "
            "stale while that stage is still legitimately running")

    def test_busy_when_a_long_job_is_still_running(self):
        # The one ledger signal that survived the cut: a detached long_job
        # OUTLIVES the tick that spawned it, so it has neither a heartbeat nor
        # a `claude -p` and the row is its only witness.
        self._row("bg", state="failed", job_running=True)
        self.assertFalse(self._runner_idle())

    def test_idle_despite_stale_job_running_on_done_row(self):
        # reconcile_merged closed a FAILED+job_running row to DONE without a
        # runner reply, so the flag was never reset and the runner restart was
        # deferred 63x in 6h (2026-09-26). A DONE row fronts no live job.
        self._row("bg", state="done", job_running=True)
        self.assertTrue(self._runner_idle())

    def test_busy_when_a_claude_stage_is_running(self):
        # this very pipeline's shape: ledger row dispatched + a live `claude -p`
        self._row("me", state="dispatched")
        self.assertFalse(self._runner_idle(pgrep_out="900 claude -p\n", pgrep_rc=0))

    def test_a_live_claude_alone_is_enough_to_be_busy(self):
        # ledger can look quiet while a stage is mid-flight; the AND catches it
        self._row("finished")
        self.assertFalse(self._runner_idle(pgrep_out="900 claude -p\n", pgrep_rc=0))

    def test_unreadable_pgrep_counts_as_busy(self):
        self._row("finished")
        self.assertFalse(self._runner_idle(pgrep_rc=2))

    def test_unreadable_ledger_counts_as_busy(self):
        (self.d / "assignments" / "broken.json").write_text("{{{", encoding="utf-8")
        self._row("finished")
        # a corrupt row must not read as "nothing in flight". The heartbeat is
        # pointed at the (absent) tempdir file so this stays hermetic: the
        # answer must come from the ledger, not from whatever this machine's
        # real Worker/_state happens to hold while the suite runs.
        with mock.patch.object(worker, "WORKER_DIR", self.d), \
             mock.patch.object(worker, "RUNNER_HEARTBEAT_PATH", self._rhb()), \
             mock.patch.object(worker, "_pgrep", side_effect=AssertionError("unreached")):
            self.assertFalse(worker._runner_idle(999))

    def _hb(self):
        return self.d / "todo_sync_heartbeat"

    def test_todo_sync_idle_without_heartbeat(self):
        # Between ticks the daemon holds no heartbeat file — that is the safe
        # kickstart window, and the process tree is deliberately not consulted.
        with mock.patch.object(worker, "TODO_SYNC_HEARTBEAT_PATH", self._hb()):
            self.assertTrue(worker._todo_sync_idle(4242))

    def test_todo_sync_busy_with_a_fresh_heartbeat(self):
        self._hb().write_text("now", encoding="utf-8")
        with mock.patch.object(worker, "TODO_SYNC_HEARTBEAT_PATH", self._hb()):
            self.assertFalse(worker._todo_sync_idle(4242))

    def test_todo_sync_stale_heartbeat_is_a_corpse_not_work(self):
        # A tick is seconds; a file older than the stale window was left by a
        # tick that died mid-write. Reading it as busy would rebuild the
        # constant-False gate this heartbeat replaced (see gate docstring).
        hb = self._hb()
        hb.write_text("old", encoding="utf-8")
        old = time.time() - worker.TODO_SYNC_HEARTBEAT_STALE_S - 1
        os.utime(hb, (old, old))
        with mock.patch.object(worker, "TODO_SYNC_HEARTBEAT_PATH", hb):
            self.assertTrue(worker._todo_sync_idle(4242))

    def test_todo_sync_unreadable_heartbeat_counts_as_busy(self):
        # "I cannot tell" must not authorise a SIGTERM — same fail-closed
        # convention as _runner_idle's unreadable-pgrep case.
        hb = self._hb()
        with mock.patch.object(worker, "TODO_SYNC_HEARTBEAT_PATH", hb), \
             mock.patch.object(Path, "stat", side_effect=OSError("denied")):
            self.assertFalse(worker._todo_sync_idle(4242))

    def test_uv_wrapped_daemon_never_reads_idle(self):
        # 2026-08-18 defect, now FIXED (flipped to assertTrue as the old
        # known-defect note prescribed): launchd's pid is the `uv` WRAPPER
        # whose python child (86003) is permanently resident, so the old
        # pgrep-based gate was the constant False — todo-sync was deferred
        # 169x/4d, then 359x/30h, and could never restart automatically.
        # The gate now reads the tick heartbeat and must call this exact
        # process shape idle whenever no heartbeat file exists, no matter
        # what the process tree says.
        with mock.patch.object(worker, "_pgrep", return_value="86003\n"), \
             mock.patch.object(worker, "TODO_SYNC_HEARTBEAT_PATH", self._hb()):
            for tick in range(10):
                with self.subTest(tick=tick):
                    self.assertTrue(
                        worker._todo_sync_idle(85998),
                        "no heartbeat -> idle, regardless of the uv wrapper's "
                        "resident python child")

    def test_both_heartbeat_gates_share_the_four_branch_convention(self):
        """The two heartbeat gates differ in cutoff VALUE and must not differ in
        SHAPE.

        A todo-sync tick is seconds; a runner tick is a multi-hour pipeline, so
        10 minutes vs 90 minutes is correct, not an inconsistency. What has to
        match is the branch table, because each branch encodes a lesson paid for
        in production: absent → idle (or the daemon can never be restarted),
        fresh → busy (or a SIGTERM lands mid-work), corpse → idle (or one crash
        freezes the gate at busy forever), unreadable → busy (an absence of
        information must never authorise a kill). Both gates are driven through
        all four here so neither can drift out from under the other.
        """
        gates = (
            ("todo_sync", "TODO_SYNC_HEARTBEAT_PATH",
             worker.TODO_SYNC_HEARTBEAT_STALE_S, worker._todo_sync_idle),
            ("runner", "RUNNER_HEARTBEAT_PATH",
             worker.RUNNER_HEARTBEAT_STALE_S, worker._runner_idle),
        )
        for label, path_attr, stale_s, gate in gates:
            hb = self.d / f"{label}_convention_hb"

            def call(path):
                # every non-heartbeat signal quiet, so the branch under test is
                # the only thing that can decide the answer
                with mock.patch.object(worker, path_attr, path), \
                     mock.patch.object(worker, "WORKER_DIR", self.d), \
                     mock.patch.object(worker, "_pgrep", return_value=""):
                    return gate(999)

            with self.subTest(gate=label, branch="absent"):
                hb.unlink(missing_ok=True)
                self.assertTrue(call(hb), "no heartbeat -> idle")
            with self.subTest(gate=label, branch="fresh"):
                hb.write_text("tick", encoding="utf-8")
                self.assertFalse(call(hb), "a live tick -> busy")
            with self.subTest(gate=label, branch="corpse"):
                old = time.time() - stale_s - 1
                os.utime(hb, (old, old))
                self.assertTrue(call(hb), "a stale heartbeat is a corpse, not work")
            with self.subTest(gate=label, branch="unreadable"):
                # a Mock rather than patching Path.stat globally: _runner_idle
                # stats the ledger dir too, and patching that out would let this
                # pass via the ledger's fail-closed branch instead of the
                # heartbeat's — the same assertion for the wrong reason.
                bad = mock.Mock()
                bad.stat.side_effect = OSError("denied")
                self.assertFalse(call(bad), "cannot tell -> busy")

    def test_pgrep_no_match_is_an_answer_but_an_error_is_not(self):
        with mock.patch("subprocess.run",
                        return_value=subprocess.CompletedProcess([], 1, "", "")):
            self.assertEqual(worker._pgrep("-P", "1"), "")     # rc=1 = no match
        with mock.patch("subprocess.run",
                        return_value=subprocess.CompletedProcess([], 2, "", "")):
            self.assertIsNone(worker._pgrep("-P", "1"))        # rc=2 = unknown
        with mock.patch("subprocess.run", side_effect=OSError("no pgrep")):
            self.assertIsNone(worker._pgrep("-P", "1"))


class RunnerHeartbeatWiringTest(unittest.TestCase):
    """The gate is only as good as whatever actually writes the heartbeat.

    IdleGateTest drives worker._runner_idle against files it places by hand, so
    every case there would still pass if the runner never wrote one at all.
    These drive the real Runner.poll_once and assert on the file, because the
    2026-08-21 defect was never in the gate's logic — it was that nothing told
    the gate what was executing.
    """

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.hb = self.d / "runner_heartbeat"

    def _runner(self, payload=None, prior_claim=None):
        from todo_system.orchestrator import message as m
        from todo_system.orchestrator.registry import Agent

        msg = m.Envelope(to="ai-worker", frm="orchestrator", type=m.TASK_ASSIGN,
                         task_id="t1", payload=payload or {})
        mailbox = mock.Mock()
        mailbox.receive.return_value = [msg]
        claims = mock.Mock()
        claims.read.return_value = prior_claim
        return Runner(mailbox=mailbox, agent=Agent(id="ai-worker", kind="ai"),
                      executor=mock.Mock(mode="shadow"), log=lambda s: None,
                      claims=claims), msg

    def test_heartbeat_is_held_across_handle_and_released_after(self):
        r, _ = self._runner()
        hb, seen = self.hb, []
        with mock.patch.object(worker, "RUNNER_HEARTBEAT_PATH", hb), \
             mock.patch.object(Runner, "handle",
                               lambda _s, _m: seen.append(hb.exists())):
            self.assertEqual(r.poll_once(), 1)
        self.assertEqual(seen, [True], "a stage must run under a live heartbeat")
        self.assertFalse(hb.exists(), "and the tick must release it when done")

    def test_heartbeat_is_released_even_if_the_ack_throws(self):
        # clear() is first in the finally precisely so a failing ack cannot
        # strand a heartbeat — that would leave the 90-minute stale cutoff as
        # the only thing releasing the restart gate.
        r, _ = self._runner()
        r.mailbox.ack.side_effect = OSError("mailbox gone")
        hb = self.hb
        with mock.patch.object(worker, "RUNNER_HEARTBEAT_PATH", hb), \
             mock.patch.object(Runner, "handle", lambda _s, _m: None), \
             self.assertRaises(OSError):
            r.poll_once()
        self.assertFalse(hb.exists())

    def test_the_redelivery_bypass_takes_no_heartbeat(self):
        # A redelivered envelope is diverted, never executed, so it must not
        # hold the heartbeat either.
        r, _ = self._runner(prior_claim={"msg_id": "x"})
        hb = self.hb
        with mock.patch.object(worker, "RUNNER_HEARTBEAT_PATH", hb), \
             mock.patch.object(Runner, "_classify_redelivery", return_value="dead"), \
             mock.patch.object(Runner, "handle",
                               side_effect=AssertionError("must not run")):
            self.assertEqual(r.poll_once(), 1)
        self.assertFalse(hb.exists())

    def test_guard_deferred_dispatch_reads_idle_end_to_end(self):
        """The 2026-08-21 condition, reproduced through the real code path.

        Guard blocked -> run_forever calls poll_once(only_bypass=True) -> a
        non-bypass envelope stays queued and un-acked and nothing executes, yet
        its ledger row still says `dispatched`. The restart gate must call that
        idle. Placing the heartbeat by hand would prove nothing here; the point
        is that poll_once declines to take one on the path that does no work.
        """
        (self.d / "assignments").mkdir()
        (self.d / "assignments" / "t1.json").write_text(
            json.dumps({"task_id": "t1", "agent_id": "ai-worker",
                        "corr_id": "c1", "state": "dispatched"}),
            encoding="utf-8")
        r, _ = self._runner(payload={})           # no bypass_guard
        hb = self.hb
        with mock.patch.object(worker, "RUNNER_HEARTBEAT_PATH", hb), \
             mock.patch.object(Runner, "handle",
                               side_effect=AssertionError("must not run")):
            self.assertEqual(r.poll_once(only_bypass=True), 0)
        r.mailbox.ack.assert_not_called()         # still queued for later
        self.assertFalse(hb.exists())
        with mock.patch.object(worker, "RUNNER_HEARTBEAT_PATH", hb), \
             mock.patch.object(worker, "WORKER_DIR", self.d), \
             mock.patch.object(worker, "_pgrep", return_value=""):
            self.assertTrue(worker._runner_idle(999),
                            "a dispatched row the guard never ran must not "
                            "defer the runner restart")


class DispatchSilenceTest(unittest.TestCase):
    """The only detector for the task pipeline going quiet.

    A false alarm here is expensive: it trains the reader to ignore the one
    channel that reports absence. So the conditions are deliberately narrow.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = worker.SILENCE_STATE_FILE
        worker.SILENCE_STATE_FILE = self.tmp / "silence.json"

    def tearDown(self):
        worker.SILENCE_STATE_FILE = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _check(self, pending=1, idle_h=10.0, allowed=True, now=1_000_000.0):
        from todo_system.orchestrator import tasks as t
        from todo_system.orchestrator.assignment import Assignment
        import datetime as dt
        stamp = dt.datetime.fromtimestamp(now - idle_h * 3600).isoformat()
        sent = []
        pend = [type("T", (), {"slug": f"q{i}"})() for i in range(pending)]
        with mock.patch.object(t, "load_dispatchable", return_value=pend), \
             mock.patch("todo_system.usage_guard.check",
                        return_value={"allowed": allowed}), \
             mock.patch("todo_system.orchestrator.assignment.Ledger.all",
                        return_value=[Assignment(task_id="t", agent_id="w",
                                                 corr_id="c", updated_ts=stamp)]), \
             mock.patch.object(worker, "kakao_notify", side_effect=sent.append):
            out = worker.check_dispatch_silence(now=now)
        return out, sent

    def test_pages_when_work_is_queued_and_nothing_moves(self):
        out, sent = self._check(pending=3, idle_h=10)
        self.assertIsNotNone(out)
        self.assertEqual(len(sent), 1)
        self.assertIn("3", sent[0])

    def test_silent_with_an_empty_queue_is_correct(self):
        out, sent = self._check(pending=0, idle_h=48)
        self.assertIsNone(out)
        self.assertEqual(sent, [])

    def test_usage_guard_pacing_is_not_a_fault(self):
        # the guard legitimately holds for hours; paging would be crying wolf
        out, sent = self._check(pending=3, idle_h=48, allowed=False)
        self.assertIsNone(out)
        self.assertEqual(sent, [])

    def test_recent_dispatch_does_not_page(self):
        out, _ = self._check(pending=3, idle_h=1)
        self.assertIsNone(out)

    def test_does_not_repage_within_the_cooldown(self):
        first, sent = self._check(pending=3, idle_h=10)
        self.assertIsNotNone(first)
        again, sent2 = self._check(pending=3, idle_h=11, now=1_000_000.0 + 3600)
        self.assertIsNone(again)
        self.assertEqual(sent2, [])

    def test_parked_tasks_do_not_count_as_queued(self):
        """The first real firing (2026-08-07) was a false positive: both queued
        tasks were awaiting_human on PRs, dispatch() correctly refused to re-send
        them, and nothing was stalled. load_dispatchable() is a TODO-level view
        and does not know about ledger parking."""
        from todo_system.orchestrator import tasks as t
        from todo_system.orchestrator.assignment import Assignment
        import datetime as dt
        now = 1_000_000.0
        stale = dt.datetime.fromtimestamp(now - 10 * 3600).isoformat()
        pend = [type("T", (), {"slug": "parked-one"})()]
        sent = []
        with mock.patch.object(t, "load_dispatchable", return_value=pend), \
             mock.patch("todo_system.usage_guard.check",
                        return_value={"allowed": True}), \
             mock.patch("todo_system.orchestrator.assignment.Ledger.all",
                        return_value=[Assignment(task_id="parked-one", agent_id="w",
                                                 corr_id="c", state="awaiting_human",
                                                 updated_ts=stale)]), \
             mock.patch.object(worker, "kakao_notify", side_effect=sent.append):
            out = worker.check_dispatch_silence(now=now)
        self.assertIsNone(out)
        self.assertEqual(sent, [])

    def test_a_genuinely_stuck_task_still_pages(self):
        """The escape hatch is parking, not idleness — a task queued and NOT
        parked, with nothing moving, is still a real stall."""
        from todo_system.orchestrator import tasks as t
        from todo_system.orchestrator.assignment import Assignment
        import datetime as dt
        now = 1_000_000.0
        stale = dt.datetime.fromtimestamp(now - 10 * 3600).isoformat()
        pend = [type("T", (), {"slug": "stuck"})()]
        sent = []
        with mock.patch.object(t, "load_dispatchable", return_value=pend), \
             mock.patch("todo_system.usage_guard.check",
                        return_value={"allowed": True}), \
             mock.patch("todo_system.orchestrator.assignment.Ledger.all",
                        return_value=[Assignment(task_id="other", agent_id="w",
                                                 corr_id="c", state="failed",
                                                 updated_ts=stale)]), \
             mock.patch.object(worker, "kakao_notify", side_effect=sent.append):
            out = worker.check_dispatch_silence(now=now)
        self.assertIsNotNone(out)
        self.assertEqual(len(sent), 1)



class WorkStarvationTest(unittest.TestCase):
    """The mirror of DispatchSilenceTest: nothing queued AND nothing left that
    could queue something. Same rule — a false alarm here is a page the reader
    learns to ignore, so every gate is exercised on its own.
    """

    NOW = 1_000_000.0

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = (worker.STARVATION_STATE_FILE, worker.PLANNER_STATE_FILE, worker.ROOT)
        worker.STARVATION_STATE_FILE = self.tmp / "starvation.json"
        worker.PLANNER_STATE_FILE = self.tmp / "planner_state.json"
        # PAUSED lives under ROOT/Worker/_state — point ROOT at the tempdir so
        # the real control plane's switch cannot leak into the test.
        worker.ROOT = self.tmp
        (self.tmp / "Worker" / "_state").mkdir(parents=True)

    def tearDown(self):
        worker.STARVATION_STATE_FILE, worker.PLANNER_STATE_FILE, worker.ROOT = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _planner(self, verdict="exhausted", ran_h_ago=1.0):
        worker.PLANNER_STATE_FILE.write_text(json.dumps({
            "verdict": verdict,
            "last_run": dt.datetime.fromtimestamp(self.NOW - ran_h_ago * 3600).isoformat(),
        }), encoding="utf-8")

    def _check(self, *, runnable=0, ledger=None, discovery_url=None, now=None):
        from todo_system.orchestrator import discovery, tasks as t
        now = self.NOW if now is None else now
        sent = []
        pend = [type("T", (), {"slug": f"q{i}"})() for i in range(runnable)]
        # parked rows are ALSO dispatchable at the todo level (open + auto)
        pend += [type("T", (), {"slug": a.task_id})()
                 for a in (ledger or []) if a.state == "awaiting_human"]
        with mock.patch.object(t, "load_dispatchable", return_value=pend), \
             mock.patch.object(discovery, "candidate",
                               return_value=(discovery_url,
                                             "due" if discovery_url else "no repo due")), \
             mock.patch("todo_system.orchestrator.assignment.Ledger.all",
                        return_value=list(ledger or [])), \
             mock.patch.object(worker, "kakao_notify", side_effect=sent.append):
            out = worker.check_work_starvation(now=now)
        return out, sent

    def _row(self, task_id, state, h_ago):
        from todo_system.orchestrator.assignment import Assignment
        return Assignment(task_id=task_id, agent_id="w", corr_id="c", state=state,
                          updated_ts=dt.datetime.fromtimestamp(self.NOW - h_ago * 3600).isoformat())

    def test_pages_when_idle_and_nothing_can_feed_the_queue(self):
        self._planner("exhausted", ran_h_ago=1)
        out, sent = self._check(ledger=[self._row("old", "done", h_ago=5)])
        self.assertIsNotNone(out)
        self.assertEqual(len(sent), 1)
        self.assertIn("exhausted", sent[0])
        self.assertIn("no repo due", sent[0])
        self.assertLessEqual(len(sent[0]), 180, "kakao memo limit")

    def test_queued_work_means_not_starved(self):
        self._planner("exhausted", ran_h_ago=1)
        out, sent = self._check(runnable=1, ledger=[self._row("old", "done", h_ago=5)])
        self.assertIsNone(out)
        self.assertEqual(sent, [])

    def test_in_flight_work_means_wait(self):
        self._planner("exhausted", ran_h_ago=1)
        out, _ = self._check(ledger=[self._row("running", "in_progress", h_ago=0.1)])
        self.assertIsNone(out)

    def test_planner_that_may_still_create_work_holds_the_page(self):
        self._planner("tasks_created", ran_h_ago=1)
        out, _ = self._check(ledger=[self._row("old", "done", h_ago=5)])
        self.assertIsNone(out)
        worker.PLANNER_STATE_FILE.unlink()               # no state at all
        out, _ = self._check(ledger=[self._row("old", "done", h_ago=5)])
        self.assertIsNone(out)

    def test_a_verdict_older_than_the_last_change_is_stale(self):
        """A run finished AFTER the planner last looked: the planner tick will
        re-plan on that completion within 30 min. Paging first is a false alarm."""
        self._planner("exhausted", ran_h_ago=3)
        out, _ = self._check(ledger=[self._row("just-finished", "done", h_ago=1)])
        self.assertIsNone(out)
        self._planner("exhausted", ran_h_ago=0.5)        # planner re-ran: still nothing
        out, _ = self._check(ledger=[self._row("just-finished", "done", h_ago=1)])
        self.assertIsNotNone(out)

    def test_discovery_with_a_repo_to_explore_holds_the_page(self):
        self._planner("exhausted", ran_h_ago=1)
        out, _ = self._check(ledger=[self._row("old", "done", h_ago=5)],
                             discovery_url="https://github.com/x/y.git")
        self.assertIsNone(out)

    def test_paused_is_idle_on_purpose(self):
        self._planner("exhausted", ran_h_ago=1)
        (self.tmp / "Worker" / "_state" / "PAUSED").touch()
        out, sent = self._check(ledger=[self._row("old", "done", h_ago=5)])
        self.assertIsNone(out)
        self.assertEqual(sent, [])

    def test_parked_rows_are_the_humans_queue_and_are_reported_not_counted(self):
        self._planner("exhausted", ran_h_ago=1)
        out, sent = self._check(ledger=[self._row("pr-review", "awaiting_human", h_ago=5)])
        self.assertIsNotNone(out)
        self.assertIn("사람 대기 1건", sent[0])

    def test_one_page_per_day(self):
        self._planner("exhausted", ran_h_ago=1)
        first, sent = self._check(ledger=[self._row("old", "done", h_ago=5)])
        self.assertIsNotNone(first)
        again, sent2 = self._check(ledger=[self._row("old", "done", h_ago=5)],
                                   now=self.NOW + 6 * 3600)
        self.assertIsNone(again)
        self.assertEqual(sent2, [])
        later, sent3 = self._check(ledger=[self._row("old", "done", h_ago=5)],
                                   now=self.NOW + 25 * 3600)
        self.assertIsNotNone(later)
        self.assertEqual(len(sent3), 1)

    def test_an_empty_ledger_with_an_exhausted_planner_still_pages(self):
        """Fresh profile / everything archived: no rows at all is the purest
        form of "nothing to do"."""
        self._planner("exhausted", ran_h_ago=1)
        out, _ = self._check(ledger=[])
        self.assertIsNotNone(out)
