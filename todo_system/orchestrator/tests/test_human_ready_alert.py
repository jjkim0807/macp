"""The "it is your turn" alert — a human task whose every dep just closed.

Before 2026-11-09 nothing announced this. `auto_unblock` only flips tasks into
*worker* modes, so a `manual` / `decision` leaf whose upstream closed became
quietly actionable and sat there until a person happened to look at the
dashboard. These tests pin the three properties that make the alert trustworthy:

* it fires EXACTLY once per ready event (gitignored fingerprint state),
* it re-arms when the chain genuinely reopens and closes again,
* it never floods — neither on first deployment nor on a deferred task.

No real notification is sent: the notifier is injected and the state file lives
in a tmp dir. Generic slugs only (`task-a`, `upstream-1`) — public repo.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system import task_deps as TD
from todo_system.orchestrator.assignment import Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import Orchestrator
from todo_system.orchestrator.registry import Agent, Registry


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # A throwaway vault: `_task_title` resolves Todo/items/<slug>.md through
        # MACP_VAULT_ROOT, so the alert body reads the same files the sweep scans.
        self.items = self.tmp / "Todo" / "items"
        self.items.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"MACP_VAULT_ROOT": str(self.tmp)})
        env.start()
        self.addCleanup(env.stop)
        self.db = self.tmp / "nodb.db"          # absent → status comes from md
        self.ledger = Ledger(self.tmp / "assignments")
        self.sent: list[tuple[str, str | None]] = []
        self.levels: list[tuple[str | None, str | None]] = []

        def route(text, *, level=None, event_key=None, cooldown_s=0.0):
            self.levels.append((level, event_key))
            return "send", text

        self.orch = Orchestrator(
            FileMailbox(self.tmp / "agents"),
            Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")]),
            self.ledger,
            notify=lambda text, link=None, **k: self.sent.append((text, link)),
            alert_route=route)

    def _w(self, slug, status="open", worker_mode="", depends_on=None,
           on_unblock=None, extra="", title="작업 제목"):
        lines = ["---", f"status: {status}"]
        if worker_mode:
            lines.append(f"worker_mode: {worker_mode}")
        if depends_on is not None:
            lines.append("depends_on:")
            lines += [f"  - {d}" for d in depends_on]
        if on_unblock:
            lines.append(f"on_unblock: {on_unblock}")
        if extra:
            lines += extra.strip("\n").split("\n")
        lines += ["---", "", f"# {title}", ""]
        (self.items / f"{slug}.md").write_text("\n".join(lines), encoding="utf-8")

    def _row(self, **kw):
        row = {"slug": "task-a", "depends_on": ["upstream-1"], "last_dep": "upstream-1",
               "last_dep_at": None, "fingerprint": "abc123", "deferred": False,
               "next_action": "", "if_then": ""}
        row.update(kw)
        return row

    def _state(self):
        p = self.orch._human_ready_seen_path()
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


class NotifyHumanReadyTest(_Base):
    """`_notify_human_ready` — the alert body and where the PR link rides."""

    def test_body_carries_title_next_action_if_then_and_last_dep(self):
        self._w("task-a", title="학자금 상환 유예 신청")
        self.orch._notify_human_ready(self._row(
            next_action="포털에서 신청서 제출", if_then="월요일 9시에 신청"),
            runs_dir=self.tmp / "runs")
        self.assertEqual(len(self.sent), 1)
        text, link = self.sent[0]
        self.assertIn("학자금 상환 유예 신청", text)       # md title, not the slug
        self.assertIn("포털에서 신청서 제출", text)
        self.assertIn("월요일 9시에 신청", text)
        self.assertIn("upstream-1", text)
        self.assertIsNone(link)

    def test_it_is_an_immediate_alert_keyed_on_the_slug(self):
        """Immediate, not digest: the person is the only thing left in the chain,
        so a daily summary would park "your turn" for up to a day."""
        self._w("task-a")
        self.orch._notify_human_ready(self._row(), runs_dir=self.tmp / "runs")
        self.assertEqual(self.levels, [("immediate", "human-ready:task-a")])

    def test_missing_md_falls_back_to_the_slug(self):
        self.orch._notify_human_ready(self._row(), runs_dir=self.tmp / "runs")
        self.assertIn("task-a", self.sent[0][0])

    def test_pr_url_rides_in_the_link_never_in_the_body(self):
        """A URL in the text can be clipped mid-token by the length budget."""
        run = self.tmp / "runs" / "20261109T120000-upstream-1"
        run.mkdir(parents=True)
        (run / "report.md").write_text(
            "pr_url: https://github.com/o/r/pull/42\n", encoding="utf-8")
        self._w("task-a")
        self.orch._notify_human_ready(self._row(), runs_dir=self.tmp / "runs")
        text, link = self.sent[0]
        self.assertEqual(link, "https://github.com/o/r/pull/42")
        self.assertNotIn("github.com", text)

    def test_newest_run_wins(self):
        for stamp, pr in (("20261101T000000", "1"), ("20261109T000000", "2")):
            run = self.tmp / "runs" / f"{stamp}-upstream-1"
            run.mkdir(parents=True)
            (run / "report.md").write_text(
                f"https://github.com/o/r/pull/{pr}\n", encoding="utf-8")
        self.assertEqual(self.orch._dep_run_link("upstream-1", self.tmp / "runs"),
                         "https://github.com/o/r/pull/2")

    def test_no_report_means_no_link(self):
        run = self.tmp / "runs" / "20261109T120000-upstream-1"
        run.mkdir(parents=True)
        self.assertIsNone(self.orch._dep_run_link("upstream-1", self.tmp / "runs"))
        self.assertIsNone(self.orch._dep_run_link("", self.tmp / "runs"))

    def test_a_broken_notifier_does_not_raise(self):
        self.orch.notify = lambda *_a, **_k: (_ for _ in ()).throw(OSError("net down"))
        self._w("task-a")
        self.orch._notify_human_ready(self._row(), runs_dir=self.tmp / "runs")   # no raise

    def test_state_file_lives_beside_the_ledger_and_is_not_tracked(self):
        self.assertEqual(self.orch._human_ready_seen_path(),
                         self.tmp / "_state" / "human_ready.json")
        self.assertEqual(self.orch._human_ready_seen(), {})      # unreadable → {}
        self.orch._write_human_ready({"task-a": {"fp": "x", "ts": "t"}})
        self.assertEqual(self.orch._human_ready_seen(), {"task-a": {"fp": "x", "ts": "t"}})

    def test_corrupt_state_file_reads_as_empty(self):
        p = self.orch._human_ready_seen_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.orch._human_ready_seen(), {})


class _SweepBase(_Base):
    def _sweep(self, **kw):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = self.orch.reconcile_human_ready(
                items_dir=self.items, db=self.db, runs_dir=self.tmp / "runs", **kw)
        return out, buf.getvalue()

    def _chain(self, up_status="done", **task_kw):
        self._w("upstream-1", status=up_status)
        kw = {"worker_mode": "manual", "depends_on": ["upstream-1"]}
        kw.update(task_kw)
        self._w("task-a", **kw)


class ReconcileHumanReadySweepTest(_SweepBase):
    """The tick sweep: once per ready event, re-arming, flips, deferral.

    The state file is seeded empty in setUp so these are *steady-state* sweeps.
    Without it every case here would be the first-ever sweep and the 24h backfill
    guard (its own class below) would silence them all.
    """

    def setUp(self):
        super().setUp()
        self.orch._write_human_ready({})

    # --- the core once-only behaviour --------------------------------------

    def test_open_dep_sends_nothing(self):
        self._chain(up_status="open")
        out, _ = self._sweep()
        self.assertEqual(out, [])
        self.assertEqual(self.sent, [])

    def test_all_deps_done_sends_exactly_one(self):
        self._chain()
        out, _ = self._sweep()
        self.assertEqual(out, ["task-a"])
        self.assertEqual(len(self.sent), 1)

    def test_a_second_sweep_in_the_same_state_is_silent(self):
        self._chain()
        self._sweep()
        out, _ = self._sweep()
        self.assertEqual(out, [])
        self.assertEqual(len(self.sent), 1)

    def test_reopening_the_dep_then_closing_it_again_alerts_twice(self):
        """The re-arm: ready → not ready prunes the entry, so the next close is a
        new event. A timestamp-only dedup could never do this."""
        self._chain()
        self._sweep()
        self._w("upstream-1", status="open")             # dep reopened
        self._sweep()
        self.assertEqual(len(self.sent), 1)              # nothing new while open
        self._w("upstream-1", status="done")             # ...and closed again
        out, _ = self._sweep()
        self.assertEqual(out, ["task-a"])
        self.assertEqual(len(self.sent), 2)

    def test_reopening_the_task_itself_alerts_again(self):
        self._chain()
        self._sweep()
        self._w("task-a", status="done", worker_mode="manual", depends_on=["upstream-1"])
        self._sweep()
        self._w("task-a", status="open", worker_mode="manual", depends_on=["upstream-1"])
        out, _ = self._sweep()
        self.assertEqual(out, ["task-a"])
        self.assertEqual(len(self.sent), 2)

    def test_adding_a_dep_re_arms_via_the_fingerprint(self):
        """Same slug, a genuinely different chain → a new fingerprint → a new ping,
        even though the entry was never pruned."""
        self._chain()
        self._sweep()
        self._w("upstream-2", status="done")
        self._w("task-a", worker_mode="manual", depends_on=["upstream-1", "upstream-2"])
        out, _ = self._sweep()
        self.assertEqual(out, ["task-a"])
        self.assertEqual(len(self.sent), 2)

    # --- unblock flips ------------------------------------------------------

    def test_a_flip_into_a_human_mode_alerts_once(self):
        self._w("upstream-1", status="done")
        self._w("task-a", worker_mode="manual", depends_on=["upstream-1"])
        out, _ = self._sweep(unblock_flips=[("task-a", "manual")])
        self.assertEqual(out, ["task-a"])
        self.assertEqual(len(self.sent), 1)

    def test_a_flip_into_an_auto_mode_is_silent(self):
        """auto/delegate/quick/discover → a worker picks it up; nobody must act."""
        self._w("upstream-1", status="done")
        self._w("task-b", worker_mode="auto", depends_on=["upstream-1"])
        for mode in TD.AUTO_WORKER_MODES:
            out, _ = self._sweep(unblock_flips=[("task-b", mode)])
            self.assertEqual(out, [], mode)
            self.assertEqual(self.sent, [], mode)

    def test_a_flip_and_the_sweep_finding_the_same_task_send_one_alert(self):
        """Both paths compute the fingerprint from the dep set, so the merged row
        is one row — the flip cannot ping and then the sweep ping again."""
        self._chain()
        out, _ = self._sweep(unblock_flips=[("task-a", "manual")])
        self.assertEqual(out, ["task-a"])
        self.assertEqual(len(self.sent), 1)

    def test_a_flip_for_a_task_with_no_md_is_skipped_quietly(self):
        out, printed = self._sweep(unblock_flips=[("ghost", "manual")])
        self.assertEqual(out, [])
        self.assertEqual(self.sent, [])
        self.assertNotIn("Traceback", printed)

    # --- deferral -----------------------------------------------------------

    def test_a_deferred_task_is_neither_alerted_nor_pruned(self):
        """Respecting a `not_before` must not cost the dedup state — pruning it
        would make the alert fire again the moment the deferral lapses twice."""
        self._chain(extra="not_before: 2099-01-01")
        out, _ = self._sweep()
        self.assertEqual(out, [])
        self.assertEqual(self.sent, [])
        self.assertEqual(self._state(), {})               # no entry written
        # and a previously-recorded entry survives a deferred row
        self.orch._write_human_ready({"task-a": {"fp": "keep", "ts": "t"}})
        self._sweep()
        self.assertIn("task-a", self._state())

    # --- robustness ---------------------------------------------------------

    def test_a_failing_scan_preserves_the_existing_state(self):
        """The flood guard that matters most: if the scan raises, the ready set is
        empty, and a naive prune would wipe every entry — so the next healthy tick
        would re-alert on everything at once."""
        self.orch._write_human_ready({"task-a": {"fp": "abc", "ts": "t"}})
        with mock.patch.object(TD, "scan_items", side_effect=OSError("items gone")):
            out, printed = self._sweep()
        self.assertEqual(out, [])
        self.assertEqual(self._state(), {"task-a": {"fp": "abc", "ts": "t"}})
        self.assertIn("reconcile_human_ready", printed)

    def test_an_entry_for_a_slug_the_scan_does_not_know_is_kept(self):
        """Same reason: only slugs the scan actually saw may be pruned."""
        self._chain(up_status="open")
        self.orch._write_human_ready({"gone-task": {"fp": "abc", "ts": "t"}})
        self._sweep()
        self.assertIn("gone-task", self._state())

    def test_a_notify_failure_is_swallowed_by_the_alert_gate(self):
        """`_notify_routed` already absorbs notifier failures, so the row counts as
        delivered. The sweep must not raise into the tick either way."""
        self._chain()
        self.orch.notify = lambda *_a, **_k: (_ for _ in ()).throw(OSError("net down"))
        out, printed = self._sweep()
        self.assertNotIn("Traceback", printed)
        self.assertEqual(out, ["task-a"])

    def test_unknown_dep_warning_is_printed_not_raised(self):
        self._w("task-a", worker_mode="manual", depends_on=["no-such-task"])
        out, printed = self._sweep()
        self.assertEqual(out, [])
        self.assertIn("no-such-task", printed)


class FirstSweepBackfillTest(_SweepBase):
    """The first-ever sweep (no state file) must not page a month of history.

    Deploying this onto a live vault means meeting every already-closed chain at
    once. Only the ones that closed inside HUMAN_READY_BACKFILL_WINDOW_S are
    announced; the rest are recorded silently, so their NEXT genuine ready event
    still alerts normally.
    """

    def test_a_chain_that_closed_long_ago_is_recorded_silently(self):
        self._chain()
        out, _ = self._sweep(dep_done_at=lambda _d: "2020-01-01T00:00:00")
        self.assertEqual(out, [])
        self.assertEqual(self.sent, [])
        self.assertIn("task-a", self._state())           # recorded, not forgotten
        self.assertTrue(self._state()["task-a"].get("backfilled"))

    def test_a_chain_that_just_closed_still_alerts(self):
        self._chain()
        recent = (dt.datetime.now() - dt.timedelta(hours=2)).isoformat()
        out, _ = self._sweep(dep_done_at=lambda _d: recent)
        self.assertEqual(out, ["task-a"])
        self.assertEqual(len(self.sent), 1)

    def test_an_unknown_close_time_is_treated_as_old(self):
        """Conservative by choice: guessing "recent" is what would flood."""
        self._chain()
        out, _ = self._sweep()
        self.assertEqual(out, [])
        self.assertIn("task-a", self._state())

    def test_a_future_timestamp_is_not_within_the_window(self):
        """Clock skew must fail closed, not open."""
        ahead = (dt.datetime.now() + dt.timedelta(days=2)).isoformat()
        self.assertFalse(self.orch._within_backfill_window(ahead))
        self.assertFalse(self.orch._within_backfill_window("not-a-date"))
        self.assertFalse(self.orch._within_backfill_window(None))

    def test_a_date_only_stamp_is_accepted(self):
        today = dt.date.today().isoformat()
        self.assertTrue(self.orch._within_backfill_window(today))

    def test_the_guard_applies_only_to_the_very_first_sweep(self):
        self._chain()
        self._sweep()                                     # creates the state file
        self._w("upstream-2", status="done")
        self._w("task-a", worker_mode="manual", depends_on=["upstream-1", "upstream-2"])
        out, _ = self._sweep(dep_done_at=lambda _d: "2020-01-01T00:00:00")
        self.assertEqual(out, ["task-a"])                 # old timestamp, still sent

    def test_a_silenced_row_alerts_on_its_next_ready_event(self):
        """The whole point of recording instead of ignoring: the backfilled entry
        is pruned when the chain reopens, so the next close is a real event."""
        self._chain()
        self._sweep(dep_done_at=lambda _d: "2020-01-01T00:00:00")
        self._w("upstream-1", status="open")
        self._sweep()
        self._w("upstream-1", status="done")
        out, _ = self._sweep()
        self.assertEqual(out, ["task-a"])


class DepDoneAtTest(_Base):
    """`_dep_done_at`: ledger row first, then the task md's `updated:`."""

    def test_the_ledger_timestamp_wins(self):
        from todo_system.orchestrator.assignment import Assignment
        self.ledger.save(Assignment(task_id="upstream-1", agent_id="w", corr_id="c1",
                                    updated_ts="2026-11-09T17:00:00"))
        self._w("upstream-1", status="done", extra="updated: 2026-11-01")
        self.assertEqual(self.orch._dep_done_at("upstream-1", self.items),
                         "2026-11-09T17:00:00")

    def test_falls_back_to_the_md_updated_field(self):
        self._w("upstream-1", status="done", extra="updated: 2026-11-01")
        self.assertEqual(self.orch._dep_done_at("upstream-1", self.items), "2026-11-01")

    def test_unknown_dep_is_none(self):
        self.assertIsNone(self.orch._dep_done_at("no-such-task", self.items))

    def test_the_sweep_reads_the_md_it_scanned(self):
        """The md fallback must follow the sweep's own items_dir — a stamp taken
        from a different directory would be a lie about this chain."""
        self._w("upstream-1", status="done",
                extra=f"updated: {dt.date.today().isoformat()}")
        self._w("task-a", worker_mode="manual", depends_on=["upstream-1"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            out = self.orch.reconcile_human_ready(
                items_dir=self.items, db=self.db, runs_dir=self.tmp / "runs")
        self.assertEqual(out, ["task-a"])      # today's stamp → inside the window


class IsHumanOwnedTest(_Base):
    def test_a_human_agent_owner_counts(self):
        self.orch.reg = Registry([Agent(id="me", kind="human", projects=["*"])])
        self.assertTrue(self.orch._is_human_owned("task-a"))

    def test_an_ai_owner_does_not(self):
        self.assertFalse(self.orch._is_human_owned("task-a"))

    def test_a_broken_registry_does_not_invent_an_owner(self):
        self.orch.route = lambda _s: (_ for _ in ()).throw(RuntimeError("registry"))
        self.assertFalse(self.orch._is_human_owned("task-a"))


class TickWiringTest(_Base):
    """The sweep runs from `tick()` and carries that tick's unblock flips.

    Every other machine-state read is injected (same reasoning as
    test_orchestrator._hermetic_tick): without that, tick() would reach for the
    live todo.db, `gh`, the queue lock and the real vault.
    """

    _STUBS = ("collect", "reap_stale", "renudge_due", "reconcile_merged",
              "reconcile_unblocked", "reconcile_unparked", "reconcile_closed",
              "reconcile_queue", "reconcile_parents", "reconcile_resumes",
              "reconcile_rescope", "reconcile_split_parents",
              "_dispatch_serial_per_checkout", "_advote_bridge_tick",
              "_beta_sweep_tick", "_project_stall_tick")

    def _tick(self, **kw):
        with contextlib.ExitStack() as st:
            for name in self._STUBS:
                st.enter_context(mock.patch.object(self.orch, name, return_value=[]))
            for name, value in (("paused", False), ("budget_exceeded", False),
                                ("usage_hold", None)):
                st.enter_context(mock.patch.object(self.orch, name, return_value=value))
            sweep = st.enter_context(mock.patch.object(
                self.orch, "reconcile_human_ready", return_value=["task-a"]))
            st.enter_context(contextlib.redirect_stdout(io.StringIO()))
            return self.orch.tick([], **kw), sweep

    def test_flips_are_handed_to_the_sweep_and_the_result_is_reported(self):
        result, sweep = self._tick(unblock_flips=[("task-a", "manual")])
        self.assertEqual(result["human_ready"], ["task-a"])
        self.assertEqual(sweep.call_args.args[0], [("task-a", "manual")])

    def test_tick_without_flips_still_sweeps(self):
        _result, sweep = self._tick()
        self.assertIsNone(sweep.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
