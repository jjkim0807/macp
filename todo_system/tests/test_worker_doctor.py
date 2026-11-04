"""`doctor_recover_queue` reconciles stale *pending* rows against their source task.

The bug: doctor only ever looked at `in_progress` and `failed` rows. A `pending`
row whose source markdown had since gone `done` stayed pending forever, and
`count_auto_pending()` kept counting it — so `auto_queue_depth_limit` (5) was
being measured against a queue that was partly dead. One real row
(`dripcoffee-bean-dialing-explorer`) sat pending from 2026-08-21 with a `done`
source.

The asymmetry these tests pin down: the new `pending` branch closes a row only
on an *explicitly observed* `done`/`cancelled` source. Unlike the `failed`
branch, a source it cannot find (`None`) is left alone. `Todo/items/` is
gitignored, so in any other checkout `slug_to_status` comes back empty — and
treating "not found" as "dead" there would silently drain the whole queue.

`iter_worker_tasks` is patched in every case on purpose. It reads the module-global
`ITEMS_DIR` of the real vault, so an unpatched "open source is preserved" test
would pass vacuously in a clean clone (everything resolves to `None`, so
everything is preserved regardless of the branch being correct).

`reclaim_stale_worktrees` is patched for the same reason and with a sharper
failure mode: it shells out against the module-global `ROOT` and now *removes*
worktrees + branches, so leaving it live here would not merely flip the
`actions == []` assertions with the host's worktree state — it would delete real
worktrees as a test side effect. Silencing it makes the hook invisible to every
test in this file, so `StaleWorktreeReportingHookTest` exists to pin the wiring —
without it, deleting the `actions.extend(...)` call would leave the suite green.
The parked `.wt/<slug>` sweep is patched the same way (it removes worktrees
and registry dirs too), and `ParkedWorktreeSweepHookTest` pins its wiring.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from todo_system import worker


def _task(slug: str, status: str) -> worker.WorkerTask:
    """A source task with just enough shape for `WorkerTask.status`."""
    return worker.WorkerTask(
        slug=slug,
        path=Path(f"Todo/items/{slug}.md"),
        rel_path=f"Todo/items/{slug}.md",
        frontmatter={"status": status},
    )


def _pending_row(slug: str, run_id: str, mode: str = "auto") -> dict:
    return {
        "run_id": run_id,
        "slug": slug,
        "status": "pending",
        "mode": mode,
        "created_at": "2026-08-21T15:06:12",
    }


def _doctor(
    runs: list[dict],
    tasks: list[worker.WorkerTask],
    worktree_actions: list[str] | None = None,
    parked: list[str] | Exception | None = None,
) -> tuple[dict, list[str]]:
    """Run doctor over `runs` with source statuses injected via `tasks`.

    `worktree_actions` defaults to `[]` so the queue assertions stay hermetic;
    pass a sentinel to observe the reclaim hook instead. `parked` does the same
    for the parked `.wt/<slug>` sweep (an exception instance is raised by it).
    """
    queue = {"runs": runs}
    parked_kw = ({"side_effect": parked} if isinstance(parked, Exception)
                 else {"return_value": list(parked or [])})
    with mock.patch("todo_system.worker.iter_worker_tasks", return_value=list(tasks)), \
         mock.patch("todo_system.worker.reclaim_stale_worktrees",
                    return_value=list(worktree_actions or [])), \
         mock.patch.object(worker._worktree_sweep, "reclaim_parked_worktrees", **parked_kw):
        actions = worker.doctor_recover_queue(queue)
    return queue, actions


class StalePendingRowTest(unittest.TestCase):
    def test_done_source_closes_pending_row(self):
        queue, actions = _doctor(
            [_pending_row("finished-task", "r1")],
            [_task("finished-task", "done")],
        )
        self.assertEqual(queue["runs"][0]["status"], "skipped")
        self.assertEqual(len(actions), 1)
        self.assertIn("finished-task", actions[0])

    def test_cancelled_source_closes_pending_row(self):
        queue, actions = _doctor(
            [_pending_row("dropped-task", "r1")],
            [_task("dropped-task", "cancelled")],
        )
        self.assertEqual(queue["runs"][0]["status"], "skipped")
        self.assertEqual(len(actions), 1)

    def test_canceled_one_l_source_closes_pending_row(self):
        """`canceled` (one l) is closed too, as are case variants."""
        for spelling in ("canceled", "Canceled"):
            with self.subTest(spelling=spelling):
                queue, actions = _doctor(
                    [_pending_row("dropped-task", "r1")],
                    [_task("dropped-task", spelling)],
                )
                self.assertEqual(queue["runs"][0]["status"], "skipped")
                self.assertEqual(len(actions), 1)

    def test_open_source_pending_row_is_untouched(self):
        """AC2: killing a live waiting row silently empties the queue."""
        queue, actions = _doctor(
            [_pending_row("still-open", "r1")],
            [_task("still-open", "open")],
        )
        self.assertEqual(queue["runs"][0]["status"], "pending")
        self.assertEqual(actions, [])

    def test_unknown_source_pending_row_is_untouched(self):
        """AC3 fail-open: 'not found' is not 'dead'. No task is injected at all."""
        queue, actions = _doctor([_pending_row("no-such-slug", "r1")], [])
        self.assertEqual(queue["runs"][0]["status"], "pending")
        self.assertEqual(actions, [])

    def test_every_pending_row_of_a_done_slug_closes(self):
        """One slug commonly has several rows (re-run / rr2 / rr3)."""
        queue, actions = _doctor(
            [
                _pending_row("dupe-task", "r1"),
                _pending_row("dupe-task", "r2"),
            ],
            [_task("dupe-task", "done")],
        )
        self.assertEqual(
            [r["status"] for r in queue["runs"]], ["skipped", "skipped"]
        )
        self.assertEqual(len(actions), 2)

    def test_non_auto_pending_row_of_a_done_slug_also_closes(self):
        """A dead row is dead regardless of mode — same rule as the failed branch."""
        queue, _ = _doctor(
            [_pending_row("manual-task", "r1", mode="manual")],
            [_task("manual-task", "done")],
        )
        self.assertEqual(queue["runs"][0]["status"], "skipped")

    def test_in_progress_reset_is_not_closed_in_the_same_pass(self):
        """The branch chain must not re-process a row it just reset to pending.

        A stuck `in_progress` row with a `done` source resets to `pending`; if the
        new branch were a bare `if` reading the *mutated* status it would close
        that row immediately, and the auto-retry path would lose the run.
        """
        queue, _ = _doctor(
            [
                {
                    "run_id": "no-such-run-dir-for-this-test",
                    "slug": "stuck-task",
                    "status": "in_progress",
                    "mode": "auto",
                    "started_at": "2020-01-01T00:00:00",
                }
            ],
            [_task("stuck-task", "done")],
        )
        self.assertEqual(queue["runs"][0]["status"], "pending")


def _failed_row(slug: str, run_id: str, retries: int = 0) -> dict:
    return {
        "run_id": run_id,
        "slug": slug,
        "status": "failed",
        "mode": "auto",
        "created_at": "2026-08-21T15:06:12",
        "auto_retry_count": retries,
    }


class FailedRowClosedSourceTest(unittest.TestCase):
    """A failed entry whose source task is closed is skipped, never auto-retried.

    Regression: the check used to know only `cancelled`/`done`, so a task closed
    as `canceled` (one l) came back `pending` with auto_retry_count+1."""

    def test_closed_source_failed_row_is_skipped_not_retried(self):
        for spelling in ("canceled", "cancelled", "done", "Canceled"):
            with self.subTest(spelling=spelling):
                queue, actions = _doctor(
                    [_failed_row("closed-task", "r1")],
                    [_task("closed-task", spelling)],
                )
                row = queue["runs"][0]
                self.assertEqual(row["status"], "skipped")
                self.assertEqual(row["auto_retry_count"], 0)
                self.assertEqual(len(actions), 1)
                self.assertIn("closed-task", actions[0])

    def test_open_source_failed_row_is_auto_retried(self):
        queue, actions = _doctor(
            [_failed_row("live-task", "r1")],
            [_task("live-task", "open")],
        )
        row = queue["runs"][0]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["auto_retry_count"], 1)
        self.assertEqual(len(actions), 1)
        self.assertIn("auto-retry", actions[0])

    def test_unknown_source_failed_row_is_skipped(self):
        queue, _ = _doctor([_failed_row("no-such-slug", "r1")], [])
        self.assertEqual(queue["runs"][0]["status"], "skipped")


class StaleWorktreeReportingHookTest(unittest.TestCase):
    """Pins the `actions.extend(reclaim_stale_worktrees())` call itself.

    Every other test here patches the helper to `[]`, which makes the hook
    invisible to them — deleting the call would leave the whole file green.
    """

    def test_candidates_are_appended_to_doctor_actions(self):
        _, actions = _doctor(
            [_pending_row("still-open", "r1")],
            [_task("still-open", "open")],
            worktree_actions=["SENTINEL"],
        )
        self.assertEqual(actions, ["SENTINEL"])

    def test_candidates_come_after_queue_actions(self):
        _, actions = _doctor(
            [_pending_row("finished-task", "r1")],
            [_task("finished-task", "done")],
            worktree_actions=["SENTINEL"],
        )
        self.assertIn("finished-task", actions[0])
        self.assertEqual(actions[-1], "SENTINEL")

    def test_reporting_never_mutates_the_queue(self):
        """Read-only: a live row stays pending however many candidates there are."""
        runs = [_pending_row("still-open", "r1")]
        queue, actions = _doctor(
            runs,
            [_task("still-open", "open")],
            worktree_actions=["SENTINEL-1", "SENTINEL-2"],
        )
        self.assertEqual(queue, {"runs": [_pending_row("still-open", "r1")]})
        self.assertEqual(actions, ["SENTINEL-1", "SENTINEL-2"])


class ParkedWorktreeSweepHookTest(unittest.TestCase):
    """Pins the `reclaim_parked_worktrees()` call: after the `wt-*` reclaim,
    filesystem only, and a raising sweep cannot take doctor down."""

    def test_parked_lines_follow_queue_and_wt_actions(self):
        _, actions = _doctor(
            [_pending_row("finished-task", "r1")],
            [_task("finished-task", "done")],
            worktree_actions=["WT"],
            parked=["정리 완료: x"],
        )
        self.assertIn("finished-task", actions[0])
        self.assertEqual(actions[1:], ["WT", "정리 완료: x"])

    def test_raising_sweep_still_returns_doctor_actions(self):
        queue, actions = _doctor(
            [_pending_row("finished-task", "r1")],
            [_task("finished-task", "done")],
            worktree_actions=["WT"],
            parked=RuntimeError("boom"),
        )
        self.assertEqual(queue["runs"][0]["status"], "skipped")
        self.assertEqual(actions[1:], ["WT"])

    def test_sweep_never_mutates_the_queue(self):
        queue, actions = _doctor(
            [_pending_row("still-open", "r1")],
            [_task("still-open", "open")],
            parked=["정리 보류: y (in-use)"],
        )
        self.assertEqual(queue, {"runs": [_pending_row("still-open", "r1")]})
        self.assertEqual(actions, ["정리 보류: y (in-use)"])


class DoctorPersistenceGateTest(unittest.TestCase):
    """`run_doctor_and_persist` writes queue.json only when the queue changed.

    The gate used to be `if doctor_actions:` — "doctor logged something" stood
    in for "doctor changed something". Stale-worktree reclaim breaks that
    equivalence: a candidate that stays *held* (in-use / unpushed) keeps the
    list non-empty on every 5-minute tick while touching no queue state, so
    queue.json would be rewritten forever, for state nothing touched, widening
    the window for clobbering a concurrent orchestrator write.
    """

    def _persist(
        self, runs: list[dict], tasks: list[worker.WorkerTask],
        worktree_actions: list[str] | None = None,
    ) -> tuple[dict, list[str], mock.Mock]:
        queue = {"runs": runs}
        with mock.patch("todo_system.worker.iter_worker_tasks", return_value=list(tasks)), \
             mock.patch("todo_system.worker.reclaim_stale_worktrees",
                        return_value=list(worktree_actions or [])), \
             mock.patch.object(worker._worktree_sweep, "reclaim_parked_worktrees",
                               return_value=[]), \
             mock.patch("todo_system.worker.save_queue") as save:
            actions = worker.run_doctor_and_persist(queue)
        return queue, actions, save

    def test_report_only_actions_do_not_trigger_a_save(self):
        """The regression this gate exists for."""
        queue, actions, save = self._persist(
            [_pending_row("still-open", "r1")],
            [_task("still-open", "open")],
            worktree_actions=["정리 완료: /w/wt-x (worktree+branch 제거)"],
        )
        self.assertEqual(len(actions), 1, "precondition: doctor did report something")
        self.assertEqual(queue["runs"][0]["status"], "pending")
        save.assert_not_called()

    def test_a_real_queue_change_still_saves(self):
        """The control: the gate must not have silenced legitimate saves."""
        _, actions, save = self._persist(
            [_pending_row("finished-task", "r1")],
            [_task("finished-task", "done")],
        )
        self.assertEqual(len(actions), 1)
        save.assert_called_once()

    def test_a_queue_change_saves_even_alongside_report_only_actions(self):
        _, _, save = self._persist(
            [_pending_row("finished-task", "r1")],
            [_task("finished-task", "done")],
            worktree_actions=["정리 완료: /w/wt-x (worktree+branch 제거)"],
        )
        save.assert_called_once()

    def test_a_quiet_tick_does_not_save(self):
        _, actions, save = self._persist(
            [_pending_row("still-open", "r1")], [_task("still-open", "open")]
        )
        self.assertEqual(actions, [])
        save.assert_not_called()

    def test_fingerprint_ignores_key_order_but_not_values(self):
        """Ordering churn must not read as a change; a status flip must."""
        self.assertEqual(
            worker._queue_fingerprint({"runs": [], "version": 1}),
            worker._queue_fingerprint({"version": 1, "runs": []}),
        )
        self.assertNotEqual(
            worker._queue_fingerprint({"runs": [_pending_row("a", "r1")]}),
            worker._queue_fingerprint({"runs": [dict(_pending_row("a", "r1"),
                                                     status="skipped")]}),
        )


class CountAutoPendingAfterDoctorTest(unittest.TestCase):
    def test_only_live_rows_are_counted(self):
        """AC4: the queue-depth number doctor leaves behind is the live one."""
        runs = [
            _pending_row("finished-task", "r1"),
            _pending_row("still-open", "r2"),
            _pending_row("no-such-slug", "r3"),
        ]
        tasks = [_task("finished-task", "done"), _task("still-open", "open")]

        self.assertEqual(worker.count_auto_pending({"runs": runs}), 3)

        queue, _ = _doctor(runs, tasks)

        # 2, not 1: the unresolvable row is preserved by fail-open and still counts.
        self.assertEqual(worker.count_auto_pending(queue), 2)
        self.assertEqual(
            {r["slug"] for r in queue["runs"] if r["status"] == "pending"},
            {"still-open", "no-such-slug"},
        )


if __name__ == "__main__":
    unittest.main()
