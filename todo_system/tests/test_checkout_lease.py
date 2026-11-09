"""Checkout lease + branch pin: serialize the planner tick against the vault
branch-prep so the planner's self-commit can never land on (and be orphaned
from) a worker branch.

The race (observed `ed395e1b`): `scripts/planner_tick.sh` runs `claude -p
/worker-planner` in the LIVE vault checkout with no lock. The planner, following
CLAUDE.md §10.1 ("uncommitted vault files stall the queue"), commits what it
wrote. Meanwhile the orchestrator's `prepare_vault_branch` does `checkout -B
worker/<slug>`, moving HEAD off `main`. If those interleave, the planner's
commit lands on the worker branch and the next sibling's branch prep rewinds it
into a dangling commit.

Three layers, each pinned here:
  * the checkout lease (`Worker/checkout.lock`, reusing `lease.Lease`) closes the
    TOCTOU — the orchestrator holds it across its HEAD-moving branch prep, the
    planner holds it across its claude call, so the two serialize;
  * the branch pin (a `.claude/commands/worker-planner.md` hard rule, shipped as
    a proposal because `.claude/**` is hard-gated for headless workers) covers
    the static case where HEAD is already on a worker branch when the planner
    starts;
  * the post-verify in `planner_tick.sh` alerts (reusing `scripts/kakao_send.sh`)
    if a worker/* commit appears anyway.

These are RED before the fix: the strings are absent, `lease` has no dead-PID
reclaim and no CLI, and the runner has no checkout-lease guard.
"""

from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pytest

from todo_system.orchestrator import lease as lease_mod
from todo_system.orchestrator import message as m
from todo_system.orchestrator import runner as runner_mod
from todo_system.orchestrator import stages
from todo_system.orchestrator.registry import Agent
from todo_system.orchestrator.runner import StagedExecutor

REPO_ROOT = Path(__file__).resolve().parents[2]
HOST = socket.gethostname()


# ---------------------------------------------------------------------------
# lease.py — dead-PID reclaim (criterion "lease는 반드시 만료된다")
# ---------------------------------------------------------------------------
class CheckoutLeaseReclaimTest(unittest.TestCase):
    """The lock must NOT wedge dispatch forever when its holder is gone. TTL
    expiry already did that; a same-host dead PID must reclaim ahead of the (long)
    checkout TTL too — but only a POSITIVE dead verdict, never a remote host or an
    unparseable holder (fail-safe: a wrong reclaim double-runs branch prep)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "Worker" / "checkout.lock"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_lock(self, holder: str, expires_at: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"holder": holder, "expires_at": expires_at}),
                             encoding="utf-8")

    def _mine(self) -> lease_mod.Lease:
        # a fresh, unambiguously-different holder on THIS host
        return lease_mod.Lease(self.path, holder=f"{HOST}:{os.getpid()}",
                               ttl=lease_mod.CHECKOUT_LEASE_TTL)

    def test_expired_lease_is_reclaimed(self):
        self._write_lock(f"{HOST}:424242", expires_at=time.time() - 1)
        self.assertTrue(self._mine().acquire())

    def test_live_same_host_pid_is_held(self):
        self._write_lock(f"{HOST}:424242", expires_at=time.time() + 9999)
        with mock.patch.object(lease_mod, "_pid_alive", return_value=True):
            self.assertFalse(self._mine().acquire())

    def test_dead_same_host_pid_is_reclaimed_before_ttl(self):
        self._write_lock(f"{HOST}:424242", expires_at=time.time() + 9999)
        with mock.patch.object(lease_mod, "_pid_alive", return_value=False):
            self.assertTrue(self._mine().acquire())

    def test_remote_host_is_never_reclaimed(self):
        # even a "dead" verdict must not reclaim another host's pid — it cannot be
        # probed from here, and stealing it would run two branch preps at once.
        self._write_lock("some-other-host:424242", expires_at=time.time() + 9999)
        with mock.patch.object(lease_mod, "_pid_alive", return_value=False):
            self.assertFalse(self._mine().acquire())

    def test_unparseable_holder_is_held(self):
        self._write_lock("not-a-host-pid", expires_at=time.time() + 9999)
        with mock.patch.object(lease_mod, "_pid_alive", return_value=False):
            self.assertFalse(self._mine().acquire())

    def test_renewing_my_own_lease_always_succeeds(self):
        me = self._mine()
        self.assertTrue(me.acquire())
        self.assertTrue(me.renew())        # own holder — never blocked by the pid logic

    def test_pid_alive_reports_the_current_process(self):
        self.assertTrue(lease_mod._pid_alive(os.getpid()))


# ---------------------------------------------------------------------------
# lease.py — bash-callable CLI (criterion "bash에서 호출할 진입점(CLI)")
# ---------------------------------------------------------------------------
class CheckoutLeaseCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "Worker" / "checkout.lock"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *argv) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = lease_mod._main(list(argv))
        return rc, buf.getvalue()

    def test_acquire_current_release_roundtrip(self):
        rc, _ = self._run("acquire", "--path", str(self.path), "--holder", "planner:1")
        self.assertEqual(rc, 0)
        self.assertTrue(self.path.exists())

        rc, out = self._run("current", "--path", str(self.path))
        self.assertEqual(rc, 0)
        self.assertIn("planner:1", out)

        rc, _ = self._run("release", "--path", str(self.path), "--holder", "planner:1")
        self.assertEqual(rc, 0)
        self.assertFalse(self.path.exists())

    def test_acquire_when_held_by_a_live_other_returns_1(self):
        lease_mod.Lease(self.path, holder="some-other-host:5",
                        ttl=lease_mod.CHECKOUT_LEASE_TTL).acquire()
        rc, out = self._run("acquire", "--path", str(self.path), "--holder", "planner:9")
        self.assertEqual(rc, 1)
        self.assertIn("some-other-host:5", out)   # names who holds it, for the log

    def test_current_on_a_free_path_is_rc0(self):
        rc, out = self._run("current", "--path", str(self.path))
        self.assertEqual(rc, 0)
        self.assertNotIn(":", out.strip().replace("checkout.lock", ""))  # no holder printed


# ---------------------------------------------------------------------------
# lease.py — fail-safe exception branches (each error keeps its direction)
# ---------------------------------------------------------------------------
class TestLeaseFailSafeBranches:
    """Every `except` in lease.py picks a side: alive, held, not-acquired, or
    swallowed. Each test injects ONE stdlib failure (narrow `mock.patch` scope,
    paths under `tmp_path` only) and is arranged so the uninjected run would give
    the opposite answer — a regression that flips a direction (e.g. `except
    Exception: return False`) fails here instead of reclaiming a live lock."""

    @staticmethod
    def _lease(tmp_path: Path) -> lease_mod.Lease:
        return lease_mod.Lease(tmp_path / "Worker" / "checkout.lock",
                               holder=f"{HOST}:{os.getpid()}",
                               ttl=lease_mod.CHECKOUT_LEASE_TTL)

    @staticmethod
    def _write_lock(lease: lease_mod.Lease, holder: str, expires_at: float) -> str:
        lease.path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps({"holder": holder, "expires_at": expires_at})
        lease.path.write_text(raw, encoding="utf-8")
        return raw

    # -- _pid_alive ---------------------------------------------------------
    def test_pid_alive_process_lookup_error_is_gone(self):
        # the ONLY "gone" verdict — everything else must stay alive
        with mock.patch.object(lease_mod.os, "kill",
                               side_effect=ProcessLookupError) as kill:
            assert lease_mod._pid_alive(424242) is False
        kill.assert_called_once_with(424242, 0)

    def test_pid_alive_permission_error_is_alive(self):
        # #1: exists, owned by another user
        with mock.patch.object(lease_mod.os, "kill",
                               side_effect=PermissionError(errno.EPERM, "not permitted")) as kill:
            assert lease_mod._pid_alive(424242) is True
        kill.assert_called_once_with(424242, 0)

    def test_pid_alive_undecidable_oserror_is_alive(self):
        # #2: cannot tell → assume alive
        with mock.patch.object(lease_mod.os, "kill",
                               side_effect=OSError(errno.EINVAL, "invalid")) as kill:
            assert lease_mod._pid_alive(424242) is True
        kill.assert_called_once_with(424242, 0)

    def test_pid_alive_overflow_pid_is_alive(self):
        # #2: an insane pid from a lock file; os.kill is mocked so no real signal
        with mock.patch.object(lease_mod.os, "kill", side_effect=OverflowError) as kill:
            assert lease_mod._pid_alive(2**70) is True
        kill.assert_called_once_with(2**70, 0)

    # -- _parse_holder ------------------------------------------------------
    def test_parse_holder_non_integer_pid_keeps_host(self):
        # #3: host kept, pid None → `_reclaimable` can never reclaim it
        assert lease_mod._parse_holder("hostA:notapid") == ("hostA", None)

    # -- _read_state / _unknown_is_stale -------------------------------------
    def test_read_state_oserror_is_unknown_not_absent(self, tmp_path):
        # #4: a valid lock (uninjected → _HELD) that cannot be read is UNKNOWN,
        # never ABSENT ("nobody holds it")
        lease = self._lease(tmp_path)
        self._write_lock(lease, f"{HOST}:424242", time.time() + 9999)
        with mock.patch.object(lease_mod.Path, "read_text",
                               side_effect=PermissionError(errno.EACCES, "denied")) as rt:
            state = lease._read_state()
        assert state == (lease_mod._UNKNOWN, None)
        assert state[0] != lease_mod._ABSENT
        rt.assert_called_once()

    def test_unreadable_fresh_lock_is_held_by_acquire(self, tmp_path):
        # #4 end-to-end: an expired foreign lock would be taken over if readable;
        # unreadable and fresh by mtime, it stays held and untouched
        lease = self._lease(tmp_path)
        raw = self._write_lock(lease, "some-other-host:424242", time.time() - 1)
        with mock.patch.object(lease_mod.Path, "read_text",
                               side_effect=PermissionError(errno.EACCES, "denied")) as rt:
            ok = lease.acquire(now=time.time())
        assert ok is False
        rt.assert_called()
        assert lease.path.read_text(encoding="utf-8") == raw

    def test_unknown_is_stale_stat_oserror_is_held(self, tmp_path):
        # #5: `now` is far past the TTL, so an uninjected stat says stale=True
        lease = self._lease(tmp_path)
        lease.path.parent.mkdir(parents=True, exist_ok=True)
        lease.path.write_bytes(b"")
        now = time.time() + 10**9
        with mock.patch.object(lease_mod.Path, "stat",
                               side_effect=OSError(errno.EIO, "io")) as st:
            stale = lease._unknown_is_stale(now)
        assert stale is False
        st.assert_called_once()

    # -- _create_exclusive --------------------------------------------------
    def test_create_exclusive_open_oserror_is_not_acquired(self, tmp_path):
        # #6: a non-FileExistsError create failure claims nothing, raises nothing
        lease = self._lease(tmp_path)
        with mock.patch.object(lease_mod.os, "open",
                               side_effect=PermissionError(errno.EACCES, "denied")) as op:
            ok = lease._create_exclusive(time.time())
        assert ok is False
        op.assert_called_once()
        assert not lease.path.exists()

    def test_create_exclusive_write_oserror_closes_fd_and_leaves_empty_lock(self, tmp_path):
        # #7: not acquired, the fd is still closed, and the 0-byte lock stays for
        # the mtime backstop
        lease = self._lease(tmp_path)
        with mock.patch.object(lease_mod.os, "write",
                               side_effect=OSError(errno.ENOSPC, "full")) as wr, \
                mock.patch.object(lease_mod.os, "close", wraps=os.close) as close:
            ok = lease._create_exclusive(time.time())
        assert ok is False
        wr.assert_called_once()
        close.assert_called_once()
        assert lease.path.exists()
        assert lease.path.stat().st_size == 0

    # -- _takeover ----------------------------------------------------------
    def test_takeover_rename_oserror_claims_nothing(self, tmp_path):
        # #8: another taker consumed the lock first → False and NO create attempt
        lease = self._lease(tmp_path)
        raw = self._write_lock(lease, "some-other-host:424242", time.time() - 1)
        with mock.patch.object(lease_mod.os, "rename",
                               side_effect=OSError(errno.ENOENT, "gone")) as rn, \
                mock.patch.object(lease_mod.Lease, "_create_exclusive",
                                  autospec=True, return_value=True) as create:
            ok = lease._takeover(time.time())
        assert ok is False
        rn.assert_called_once()
        assert create.call_count == 0
        assert lease.path.read_text(encoding="utf-8") == raw

    @pytest.mark.parametrize("created", [True, False])
    def test_takeover_stale_unlink_oserror_returns_create_result(self, tmp_path, created):
        # #9: debris cleanup failing is swallowed; the create result is returned
        lease = self._lease(tmp_path)
        self._write_lock(lease, "some-other-host:424242", time.time() - 1)
        with mock.patch.object(lease_mod.os, "unlink",
                               side_effect=OSError(errno.EBUSY, "busy")) as ul, \
                mock.patch.object(lease_mod.Lease, "_create_exclusive",
                                  autospec=True, return_value=created) as create:
            ok = lease._takeover(time.time())
        assert ok is created
        assert create.call_count == 1
        ul.assert_called_once()
        assert len(list(lease.path.parent.glob("checkout.lock.stale.*"))) == 1

    # -- acquire / release --------------------------------------------------
    @pytest.mark.parametrize("second", [True, False])
    def test_acquire_absent_after_failed_create_retries_once(self, tmp_path, second):
        # #10: lock vanished between create and read (_ABSENT) → exactly one
        # retry through the atomic primitive, its result returned as-is
        lease = self._lease(tmp_path)
        with mock.patch.object(lease_mod.Lease, "_create_exclusive",
                               autospec=True, side_effect=[False, second]) as create, \
                mock.patch.object(lease_mod.Lease, "_takeover", autospec=True) as take:
            ok = lease.acquire(now=time.time())
        assert ok is second
        assert create.call_count == 2
        take.assert_not_called()

    def test_release_unlink_oserror_is_swallowed(self, tmp_path):
        # #11: release never raises; the lock simply stays until TTL/reclaim
        lease = self._lease(tmp_path)
        assert lease.acquire() is True
        with mock.patch.object(lease_mod.Path, "unlink",
                               side_effect=PermissionError(errno.EACCES, "denied")) as ul:
            result = lease.release()
        assert result is None
        ul.assert_called_once()
        assert lease.path.exists()


# ---------------------------------------------------------------------------
# runner.py — the vault branch-prep guard (criterion 1 & 2, the behavioral RED)
# ---------------------------------------------------------------------------
class RunnerCheckoutLeaseGuardTest(unittest.TestCase):
    """A held checkout lease must DEFER a vault run before it moves HEAD — but a
    free one must proceed and RELEASE right after branch prep, so the planner is
    never starved for a whole pipeline. Drives `_execute` against a real temp
    vault (mocks would not show which ref HEAD ends up on)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "vault"
        self.repo.mkdir()
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        self.mbroot = self.tmp / "agents"
        self.lock = self.repo / "Worker" / "checkout.lock"
        self._git("init", "-b", "main")
        self._git("config", "user.email", "worker@example.com")
        self._git("config", "user.name", "worker")
        self._git("config", "commit.gpgsign", "false")
        (self.repo / "f.txt").write_text("base\n", encoding="utf-8")
        self._git("add", "f.txt")
        self._git("commit", "-m", "base")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _git(self, *args) -> subprocess.CompletedProcess:
        r = subprocess.run(["git", "-C", str(self.repo), *args],
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, f"git {args} failed: {r.stderr}")
        return r

    def _head(self) -> str:
        return self._git("branch", "--show-current").stdout.strip()

    def _hold_lease(self, holder: str = "some-other-host:1") -> None:
        # foreign host → held by fail-safe regardless of pid liveness.
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text(json.dumps({"holder": holder,
                                         "expires_at": time.time() + 9999}),
                             encoding="utf-8")

    @contextlib.contextmanager
    def _sandbox(self, mock_restore: bool = True):
        with contextlib.ExitStack() as st:
            # The runner's vault-checkout git ops (lease acquire, prepare_vault_
            # branch, restore_main) read stages._VAULT_DATA_ROOT; patch it so branch
            # prep can never reach the live vault.
            st.enter_context(mock.patch.object(stages, "_VAULT_DATA_ROOT", self.repo))
            st.enter_context(mock.patch.object(stages, "RUNS_DIR", self.runs))
            st.enter_context(mock.patch.object(stages, "install_push_guard"))
            st.enter_context(mock.patch.object(runner_mod, "DEFAULT_ROOT", self.mbroot))
            st.enter_context(mock.patch.object(runner_mod.disk_guard,
                                               "ensure_free_space", return_value={}))
            st.enter_context(mock.patch.object(StagedExecutor, "_account_env",
                                               return_value=(None, "")))
            st.enter_context(mock.patch.object(StagedExecutor, "_pipeline_for",
                                               return_value=()))
            restore = None
            if mock_restore:
                restore = st.enter_context(mock.patch.object(stages, "restore_main"))
            yield restore

    def _run(self):
        agent = Agent(id="armchair-commander", kind="ai-local",
                      capabilities=["git"], repo="~/Worker/armchair-commander")
        msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="mine",
                         payload={"auto_merge": False})
        return StagedExecutor().execute(msg, agent, progress=lambda _: None)

    def test_held_lease_defers_before_touching_head(self):
        self._hold_lease()
        with self._sandbox(mock_restore=True) as restore:
            out = self._run()
        self.assertEqual(restore.call_count, 0,
                         "restore_main ran while the checkout lease was held")
        self.assertFalse(out.ok)
        self.assertTrue(out.payload.get("checkout_busy"))
        self.assertEqual(self._head(), "main", "HEAD was moved despite a held lease")

    def test_free_lease_proceeds_releases_and_is_quiet(self):
        """No contention: proceed exactly as before, and leave no lock behind."""
        with self._sandbox(mock_restore=True) as restore:
            out = self._run()
        self.assertGreaterEqual(restore.call_count, 1, "the run did not proceed")
        self.assertNotIn("checkout_busy", out.payload)
        self.assertFalse(self.lock.exists(),
                         "checkout lease left held after branch prep — planner starves")
        self.assertEqual(self._head(), "worker/mine",
                         "branch prep should have moved HEAD (restore_main is mocked)")

    def test_external_repo_task_is_not_gated_by_the_vault_lease(self):
        """The lease guards the VAULT checkout only; an external clone is a
        different checkout the planner never touches, so a held vault lock must
        not defer an external-repo run."""
        self._hold_lease()
        with self._sandbox(mock_restore=True), \
                mock.patch.object(runner_mod.repos, "ensure_local",
                                  return_value=self.repo), \
                mock.patch.object(stages, "prepare_external_branch") as prep, \
                mock.patch.object(stages, "default_branch", return_value="main"):
            prep.return_value = subprocess.CompletedProcess([], 0, "", "")
            prep.return_value.orphaned_commits = None
            agent = Agent(id="a", kind="ai-local", capabilities=["git"], repo="r")
            msg = m.Envelope(to="w", type=m.TASK_ASSIGN, task_id="ext",
                             payload={"auto_merge": False,
                                      "repo": "https://example.com/x.git"})
            out = StagedExecutor().execute(msg, agent, progress=lambda _: None)
        self.assertNotIn("checkout_busy", out.payload)
        prep.assert_called()   # external branch prep ran despite the held vault lock


# ---------------------------------------------------------------------------
# planner_tick.sh — lease acquire + post-verify wiring (criterion 2 & 4)
# ---------------------------------------------------------------------------
class PlannerTickWiringTest(unittest.TestCase):
    def _tick(self) -> str:
        return (REPO_ROOT / "scripts" / "planner_tick.sh").read_text(encoding="utf-8")

    def test_acquires_the_dedicated_checkout_lease(self):
        t = self._tick()
        self.assertIn("checkout.lock", t)          # the dedicated lease, not orchestrator.lock
        self.assertIn("orchestrator.lease", t)     # via the reused lease CLI, not flock

    def test_does_not_widen_the_orchestrator_lock(self):
        # It must take the DEDICATED checkout.lock, never the shared
        # Worker/orchestrator.lock — locking that would starve collect/report/
        # dashboard for the whole (up to 10-min) call, the merge-blocking
        # regression. A comment may still name it; a code line must not.
        for line in self._tick().splitlines():
            if "orchestrator.lock" in line and not line.lstrip().startswith("#"):
                self.fail(f"planner_tick references orchestrator.lock in code: {line!r}")

    def test_post_verifies_worker_commits_via_existing_kakao(self):
        t = self._tick()
        self.assertIn("refs/heads/worker", t)      # snapshots worker/* tips
        self.assertIn("kakao_send.sh", t)          # reuses the existing alert plumbing


# ---------------------------------------------------------------------------
# worker-planner.md branch pin — rule lives directly in .claude/commands/
# ---------------------------------------------------------------------------
class BranchPinRuleTest(unittest.TestCase):
    PROMPT = (REPO_ROOT / ".claude" / "commands" / "worker-planner.md")

    def test_worker_planner_doc_pins_the_default_branch(self):
        self.assertTrue(self.PROMPT.exists(),
                        "worker-planner.md branch-pin rule is missing")
        txt = self.PROMPT.read_text(encoding="utf-8")
        self.assertIn("symbolic-ref", txt)         # the HEAD check the rule prescribes
        self.assertIn("## CRITICAL: Branch pin", txt)
        # the rule: do not commit when HEAD is not the default branch
        self.assertRegex(txt, r"(?s)not the default branch.*do not stage")


if __name__ == "__main__":
    unittest.main()
