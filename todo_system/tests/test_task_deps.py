"""Tests for task-level dependencies + auto-unblock (issue #21)."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from todo_system import task_deps as TD


def _md(status, worker_mode, depends_on=None, on_unblock=None, auto_activate=None,
        human_gate=None):
    lines = ["---", f"status: {status}", f"worker_mode: {worker_mode}"]
    if depends_on is not None:                       # YAML block list (vault convention)
        lines.append("depends_on:")
        lines += [f"  - {d}" for d in depends_on]
    if on_unblock:
        lines.append(f"on_unblock: {on_unblock}")
    if auto_activate is not None:
        lines.append(f"auto_activate: {str(auto_activate).lower()}")
    if human_gate is not None:
        lines.append(f"human_gate: {str(human_gate).lower()}")
    lines += ["---", "", "# task", "", "## Progress Log", ""]
    return "\n".join(lines)


class TaskDepsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "nodb.db"          # absent → status comes from md only

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _w(self, slug, **kw):
        (self.tmp / f"{slug}.md").write_text(_md(**kw), encoding="utf-8")

    def _mode(self, slug):
        return TD.scan_items(self.tmp)[slug]["worker_mode"]

    def _status(self, slug):
        return TD.scan_items(self.tmp)[slug]["status"]

    def test_all_deps_done_flips_to_auto(self):
        self._w("up", status="done", worker_mode="auto")
        self._w("down", status="open", worker_mode="blocked", depends_on=["up"])
        flips, cyclic = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [("down", "auto")])
        self.assertEqual(self._mode("down"), "auto")
        self.assertIn("auto-unblocked", (self.tmp / "down.md").read_text(encoding="utf-8"))

    def test_partial_deps_no_flip(self):
        self._w("up1", status="done", worker_mode="auto")
        self._w("up2", status="open", worker_mode="auto")
        self._w("down", status="open", worker_mode="blocked", depends_on=["up1", "up2"])
        flips, _ = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [])
        self.assertEqual(self._mode("down"), "blocked")

    def test_auto_activate_false_opts_out(self):
        self._w("up", status="done", worker_mode="auto")
        self._w("down", status="open", worker_mode="blocked",
                depends_on=["up"], auto_activate=False)
        flips, _ = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [])
        self.assertEqual(self._mode("down"), "blocked")

    def test_on_unblock_target_mode(self):
        self._w("up", status="done", worker_mode="auto")
        self._w("down", status="open", worker_mode="blocked",
                depends_on=["up"], on_unblock="delegate")
        flips, _ = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [("down", "delegate")])
        self.assertEqual(self._mode("down"), "delegate")

    def test_cycle_is_skipped_not_infinite(self):
        self._w("a", status="done", worker_mode="blocked", depends_on=["b"])
        self._w("b", status="done", worker_mode="blocked", depends_on=["a"])
        flips, cyclic = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [])
        self.assertEqual(set(cyclic), {"a", "b"})

    def test_on_unblock_ignores_inline_comment(self):
        # a stray '# comment' must not silently downgrade the safety-critical mode
        self._w("up", status="done", worker_mode="auto")
        (self.tmp / "down.md").write_text(
            "---\nstatus: open\nworker_mode: blocked\ndepends_on:\n  - up\n"
            "on_unblock: manual  # 운영성 → 사람에게\n---\n\n# t\n\n## Progress Log\n",
            encoding="utf-8")
        flips, _ = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [("down", "manual")])
        self.assertEqual(self._mode("down"), "manual")

    def test_chain_activates_one_hop_per_pass(self):
        # up(done) → mid(blocked, dep up) → down(blocked, dep mid). One pass flips mid;
        # only after mid is done does down become eligible (correct, conservative).
        self._w("up", status="done", worker_mode="auto")
        self._w("mid", status="open", worker_mode="blocked", depends_on=["up"])
        self._w("down", status="open", worker_mode="blocked", depends_on=["mid"])
        flips, _ = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [("mid", "auto")])          # down waits (mid not done)

    # --- the other shape: the block lives in `status`, not in `worker_mode` ---

    def test_status_blocked_with_on_unblock_flips(self):
        # how the vault actually writes a gated task (ai-stock-trading-8b): status
        # carries the block and worker_mode is already the real post-unblock mode.
        self._w("up", status="done", worker_mode="auto")
        self._w("down", status="blocked", worker_mode="auto",
                depends_on=["up"], on_unblock="auto")
        flips, _ = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [("down", "auto")])
        self.assertEqual(self._status("down"), "open")      # the flip must clear this
        self.assertEqual(self._mode("down"), "auto")

    def test_status_blocked_flip_is_idempotent(self):
        # worker_mode is `auto` before *and* after, so rewriting status is the only
        # thing that stops the resolver re-reporting this task on every pass.
        self._w("up", status="done", worker_mode="auto")
        self._w("down", status="blocked", worker_mode="auto",
                depends_on=["up"], on_unblock="auto")
        self.assertEqual(TD.apply_unblocks(self.tmp, self.db)[0], [("down", "auto")])
        self.assertEqual(TD.apply_unblocks(self.tmp, self.db)[0], [])

    def test_status_blocked_without_on_unblock_stays_blocked(self):
        # no on_unblock → we cannot tell a dep-gated task from operational work a
        # human parked, so it must not be flipped to the `auto` default.
        self._w("up", status="done", worker_mode="auto")
        self._w("down", status="blocked", worker_mode="manual", depends_on=["up"])
        flips, _ = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [])
        self.assertEqual(self._status("down"), "blocked")
        self.assertEqual(self._mode("down"), "manual")

    def test_worker_mode_blocked_leaves_status_untouched(self):
        # the original shape keeps flipping, and its already-open status is not
        # rewritten (only a status-blocked task needs that).
        self._w("up", status="done", worker_mode="auto")
        self._w("down", status="open", worker_mode="blocked", depends_on=["up"])
        flips, _ = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [("down", "auto")])
        self.assertEqual(self._status("down"), "open")
        self.assertEqual(self._mode("down"), "auto")

    def test_status_blocked_without_worker_mode_line_gets_one(self):
        # no worker_mode line at all: opening status alone would leave the mode
        # reading as `off`, voiding the flip — so the line has to be written in.
        self._w("up", status="done", worker_mode="auto")
        (self.tmp / "down.md").write_text(
            "---\nstatus: blocked\ndepends_on:\n  - up\non_unblock: delegate\n"
            "---\n\n# t\n\n## Progress Log\n", encoding="utf-8")
        flips, _ = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [("down", "delegate")])
        self.assertEqual(self._status("down"), "open")
        self.assertEqual(self._mode("down"), "delegate")

    # --- depends_on written as a wikilink, the `[[slug]]` convention `parent:`
    # already uses in the vault (mulkko-77-walked-edge-decay) ---

    def test_depends_on_block_list_wikilink_resolves(self):
        self._w("up", status="done", worker_mode="auto")
        self._w("down", status="open", worker_mode="blocked", depends_on=["[[up]]"])
        flips, _ = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [("down", "auto")])
        self.assertEqual(self._mode("down"), "auto")

    def test_depends_on_inline_wikilink_resolves(self):
        self._w("up1", status="done", worker_mode="auto")
        self._w("up2", status="done", worker_mode="auto")
        (self.tmp / "down.md").write_text(
            "---\nstatus: open\nworker_mode: blocked\n"
            "depends_on: [[up1]], [[up2]]\n---\n\n# t\n\n## Progress Log\n",
            encoding="utf-8")
        flips, _ = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [("down", "auto")])
        self.assertEqual(self._mode("down"), "auto")

    def test_depends_on_wikilink_with_alias_resolves(self):
        self._w("up", status="done", worker_mode="auto")
        self._w("down", status="open", worker_mode="blocked",
                depends_on=["[[up|Upstream Task]]"])
        flips, _ = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [("down", "auto")])
        self.assertEqual(self._mode("down"), "auto")

    # --- human_gate: a person must look before this runs, deps-done or not ---
    # The 2026-10-12 incident: a delegate PR closed `done` out from under a
    # reviewer let the deps-done auto-flip activate a leaf gated on a human.

    def test_human_gate_blocks_flip_even_with_all_deps_done(self):
        self._w("up", status="done", worker_mode="auto")
        self._w("down", status="open", worker_mode="blocked",
                depends_on=["up"], human_gate=True)
        flips, _ = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [])                         # NOT flipped
        self.assertEqual(self._mode("down"), "blocked")     # stays visible as blocked

    def test_human_gate_default_false_does_not_change_the_baseline(self):
        # No human_gate field → the ordinary deps-done flip still fires, so the
        # new field is strictly opt-in and no existing task regresses.
        self._w("up", status="done", worker_mode="auto")
        self._w("down", status="open", worker_mode="blocked", depends_on=["up"])
        flips, _ = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [("down", "auto")])
        self.assertEqual(self._mode("down"), "auto")

    def test_human_gate_honours_inline_comment(self):
        # The frontmatter parser leaves `# ...` on the value; a stray comment must
        # not flip the gate off (same trap on_unblock guards against).
        self._w("up", status="done", worker_mode="auto")
        (self.tmp / "down.md").write_text(
            "---\nstatus: open\nworker_mode: blocked\ndepends_on:\n  - up\n"
            "human_gate: true  # 사람이 먼저 확인\n---\n\n# t\n\n## Progress Log\n",
            encoding="utf-8")
        flips, _ = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [])
        self.assertEqual(self._mode("down"), "blocked")

    def test_human_gate_false_is_not_a_gate(self):
        # Explicit human_gate: false reads as "not gated" → flips normally.
        self._w("up", status="done", worker_mode="auto")
        self._w("down", status="open", worker_mode="blocked",
                depends_on=["up"], human_gate=False)
        flips, _ = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [("down", "auto")])


if __name__ == "__main__":
    unittest.main()

    def test_on_unblock_review_is_honoured_not_downgraded_to_auto(self):
        self._w("up", status="done", worker_mode="auto")
        self._w("down", status="open", worker_mode="blocked", depends_on=["up"],
                on_unblock="review")
        flips, _ = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [("down", "review")])
        self.assertEqual(self._mode("down"), "review")

    def test_unknown_on_unblock_falls_to_manual_not_auto(self):
        self._w("up", status="done", worker_mode="auto")
        self._w("down", status="open", worker_mode="blocked", depends_on=["up"],
                on_unblock="humann")   # a typo must not grant autonomy
        flips, _ = TD.apply_unblocks(self.tmp, self.db)
        self.assertEqual(flips, [("down", "manual")])
        self.assertEqual(self._mode("down"), "manual")


class HumanDepsReadyTest(unittest.TestCase):
    """`human_deps_ready` — "it is a person's turn" as a pure function.

    A human-owned task that declares `depends_on` used to go completely
    unannounced when its upstream closed: `auto_unblock` only flips tasks into
    *worker* modes, so a `manual` leaf just silently became actionable and waited
    for someone to notice (2026-11-09). This is the predicate the tick sweeps.

    Generic slugs only (`task-a`, `upstream-1`) — this is a public repo.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "nodb.db"          # absent → status comes from md only

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _w(self, slug, status="open", worker_mode="", depends_on=None,
           on_unblock=None, extra=""):
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
        lines += ["---", "", "# task", ""]
        (self.tmp / f"{slug}.md").write_text("\n".join(lines), encoding="utf-8")

    def _ready(self, **kw):
        return TD.human_deps_ready(TD.scan_items(self.tmp), self.db, **kw)

    def _slugs(self, **kw):
        rows, _ = self._ready(**kw)
        return [r["slug"] for r in rows]

    # --- the core judgement -------------------------------------------------

    def test_all_deps_done_is_ready(self):
        self._w("upstream-1", status="done")
        self._w("task-a", worker_mode="manual", depends_on=["upstream-1"],
                extra="next_action: 서류 제출\nif_then: 월요일 9시에 포털에서 제출")
        rows, warnings = self._ready()
        self.assertEqual([r["slug"] for r in rows], ["task-a"])
        self.assertEqual(warnings, [])
        self.assertEqual(rows[0]["depends_on"], ["upstream-1"])
        self.assertEqual(rows[0]["last_dep"], "upstream-1")
        self.assertEqual(rows[0]["next_action"], "서류 제출")
        self.assertEqual(rows[0]["if_then"], "월요일 9시에 포털에서 제출")
        self.assertFalse(rows[0]["deferred"])

    def test_one_open_dep_is_not_ready(self):
        self._w("upstream-1", status="done")
        self._w("upstream-2", status="open")
        self._w("task-a", worker_mode="manual", depends_on=["upstream-1", "upstream-2"])
        self.assertEqual(self._slugs(), [])

    def test_unknown_dep_is_not_ready_and_warns(self):
        """A typo'd slug can never be done; treating it as satisfied would page
        someone for work that is still genuinely blocked."""
        self._w("task-a", worker_mode="manual", depends_on=["no-such-task"])
        rows, warnings = self._ready()
        self.assertEqual(rows, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("task-a", warnings[0])
        self.assertIn("no-such-task", warnings[0])

    def test_no_depends_on_is_never_ready(self):
        """This alert is strictly about a chain closing — a plain human to-do has
        its own due date and is not news."""
        self._w("task-a", worker_mode="manual")
        self.assertEqual(self._slugs(), [])

    def test_db_done_counts_even_when_md_says_open(self):
        """Same done test as resolve_unblocks: md OR db (_done_test)."""
        import sqlite3
        db = self.tmp / "todo.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE tasks (slug TEXT, status TEXT)")
        con.execute("INSERT INTO tasks VALUES ('upstream-1', 'done')")
        con.commit()
        con.close()
        self._w("upstream-1", status="open")
        self._w("task-a", worker_mode="manual", depends_on=["upstream-1"])
        rows, _ = TD.human_deps_ready(TD.scan_items(self.tmp), db)
        self.assertEqual([r["slug"] for r in rows], ["task-a"])

    def test_dep_known_only_to_the_db_is_not_an_unknown_dep(self):
        import sqlite3
        db = self.tmp / "todo.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE tasks (slug TEXT, status TEXT)")
        con.execute("INSERT INTO tasks VALUES ('archived-1', 'done')")
        con.commit()
        con.close()
        self._w("task-a", worker_mode="manual", depends_on=["archived-1"])
        rows, warnings = TD.human_deps_ready(TD.scan_items(self.tmp), db)
        self.assertEqual([r["slug"] for r in rows], ["task-a"])
        self.assertEqual(warnings, [])

    # --- whose job is it? ---------------------------------------------------

    def test_auto_modes_are_excluded(self):
        self._w("upstream-1", status="done")
        for mode in TD.AUTO_WORKER_MODES:
            self._w("task-a", worker_mode=mode, depends_on=["upstream-1"])
            self.assertEqual(self._slugs(), [], mode)

    def test_blocked_with_auto_on_unblock_is_the_workers_job(self):
        """resolve_unblocks is about to hand it to a worker — nobody must act."""
        self._w("upstream-1", status="done")
        self._w("task-a", worker_mode="blocked", depends_on=["upstream-1"],
                on_unblock="auto")
        self.assertEqual(self._slugs(), [])

    def test_blocked_with_manual_on_unblock_is_a_persons_job(self):
        self._w("upstream-1", status="done")
        self._w("task-a", worker_mode="blocked", depends_on=["upstream-1"],
                on_unblock="manual")
        self.assertEqual(self._slugs(), ["task-a"])

    def test_blocked_with_a_decision_marker_is_a_persons_job(self):
        self._w("upstream-1", status="done")
        self._w("task-a", worker_mode="blocked", depends_on=["upstream-1"],
                on_unblock="auto", extra="needs_decision: true")
        self.assertEqual(self._slugs(), ["task-a"])

    def test_review_mode_is_a_persons_job(self):
        self._w("upstream-1", status="done")
        self._w("task-a", worker_mode="review", depends_on=["upstream-1"])
        self.assertEqual(self._slugs(), ["task-a"])

    def test_decision_tag_without_a_worker_mode_is_a_persons_job(self):
        self._w("upstream-1", status="done")
        self._w("task-a", depends_on=["upstream-1"],
                extra="tags:\n  - todo\n  - decision")
        self.assertEqual(self._slugs(), ["task-a"])

    def test_needs_decision_with_an_inline_comment_still_reads_true(self):
        self._w("upstream-1", status="done")
        self._w("task-a", depends_on=["upstream-1"],
                extra="needs_decision: true  # 방향 결정")
        self.assertEqual(self._slugs(), ["task-a"])

    def test_unrouted_backlog_is_silent(self):
        """No worker_mode, no owner, no decision marker → unrouted backlog. Paging
        on it would make every closed chain noise."""
        self._w("upstream-1", status="done")
        self._w("task-a", depends_on=["upstream-1"])
        self.assertEqual(self._slugs(), [])

    def test_registry_human_owner_makes_it_a_persons_job(self):
        self._w("upstream-1", status="done")
        self._w("task-a", depends_on=["upstream-1"])
        self.assertEqual(self._slugs(is_human_owned=lambda s: s == "task-a"),
                         ["task-a"])

    def test_a_broken_owner_resolver_does_not_raise(self):
        self._w("upstream-1", status="done")
        self._w("task-a", worker_mode="manual", depends_on=["upstream-1"])

        def boom(_slug):
            raise RuntimeError("registry gone")
        self.assertEqual(self._slugs(is_human_owned=boom), ["task-a"])

    # --- skips --------------------------------------------------------------

    def test_done_task_is_excluded(self):
        self._w("upstream-1", status="done")
        self._w("task-a", status="done", worker_mode="manual", depends_on=["upstream-1"])
        self.assertEqual(self._slugs(), [])

    def test_parent_is_excluded(self):
        """A parent carries no work of its own; reconcile_parents announces it."""
        self._w("upstream-1", status="done")
        self._w("task-a", worker_mode="manual", depends_on=["upstream-1"],
                extra="is_parent: true")
        self.assertEqual(self._slugs(), [])

    def test_cycle_is_excluded_and_warns(self):
        self._w("task-a", worker_mode="manual", depends_on=["task-b"])
        self._w("task-b", status="done", worker_mode="manual", depends_on=["task-a"])
        rows, warnings = self._ready()
        self.assertEqual([r["slug"] for r in rows], [])
        self.assertTrue(any("cycle" in w for w in warnings), warnings)

    # --- deferral, fingerprint, last dep ------------------------------------

    def test_future_not_before_is_flagged_deferred_not_dropped(self):
        """Returned so the caller can skip the alert WITHOUT pruning its dedup
        state — a human's deferral must survive, and must not re-fire later."""
        self._w("upstream-1", status="done")
        self._w("task-a", worker_mode="manual", depends_on=["upstream-1"],
                extra="not_before: 2099-01-01")
        rows, _ = self._ready()
        self.assertEqual([r["slug"] for r in rows], ["task-a"])
        self.assertTrue(rows[0]["deferred"])

    def test_past_not_before_is_not_deferred(self):
        self._w("upstream-1", status="done")
        self._w("task-a", worker_mode="manual", depends_on=["upstream-1"],
                extra="not_before: 2000-01-01")
        rows, _ = self._ready()
        self.assertFalse(rows[0]["deferred"])

    def test_fingerprint_depends_only_on_the_dep_set(self):
        self._w("upstream-1", status="done")
        self._w("upstream-2", status="done")
        self._w("task-a", worker_mode="manual", depends_on=["upstream-1", "upstream-2"])
        first = self._ready()[0][0]["fingerprint"]
        # same set, written in the other order → same fingerprint
        self._w("task-a", worker_mode="manual", depends_on=["upstream-2", "upstream-1"])
        self.assertEqual(self._ready()[0][0]["fingerprint"], first)
        # a dep ADDED → different fingerprint (that is what re-arms the alert)
        self._w("upstream-3", status="done")
        self._w("task-a", worker_mode="manual",
                depends_on=["upstream-1", "upstream-2", "upstream-3"])
        self.assertNotEqual(self._ready()[0][0]["fingerprint"], first)

    def test_last_dep_comes_from_the_injected_resolver(self):
        self._w("upstream-1", status="done")
        self._w("upstream-2", status="done")
        self._w("task-a", worker_mode="manual", depends_on=["upstream-1", "upstream-2"])
        at = {"upstream-1": "2026-11-09T10:00:00", "upstream-2": "2026-11-09T17:30:00"}
        rows, _ = self._ready(dep_done_at=at.get)
        self.assertEqual(rows[0]["last_dep"], "upstream-2")
        self.assertEqual(rows[0]["last_dep_at"], "2026-11-09T17:30:00")

    def test_no_resolver_leaves_the_timestamp_unknown(self):
        self._w("upstream-1", status="done")
        self._w("upstream-2", status="done")
        self._w("task-a", worker_mode="manual", depends_on=["upstream-1", "upstream-2"])
        rows, _ = self._ready()
        self.assertIsNone(rows[0]["last_dep_at"])
        self.assertEqual(rows[0]["last_dep"], "upstream-2")   # alphabetical stand-in

    def test_a_broken_dep_done_at_does_not_raise(self):
        self._w("upstream-1", status="done")
        self._w("task-a", worker_mode="manual", depends_on=["upstream-1"])

        def boom(_dep):
            raise OSError("ledger gone")
        rows, _ = self._ready(dep_done_at=boom)
        self.assertEqual([r["slug"] for r in rows], ["task-a"])
        self.assertIsNone(rows[0]["last_dep_at"])

    # --- the additive scan_items fields ------------------------------------

    def test_scan_items_keeps_every_pre_existing_key(self):
        """The new fields are additive — apply_unblocks/task_export read the old
        ones unchanged."""
        self._w("task-a", worker_mode="blocked", depends_on=["upstream-1"],
                on_unblock="manual")
        t = TD.scan_items(self.tmp)["task-a"]
        for k in ("path", "status", "worker_mode", "depends_on", "on_unblock",
                  "on_unblock_explicit", "auto_activate", "human_gate"):
            self.assertIn(k, t)
        for k in ("tags", "needs_decision", "is_parent", "not_before",
                  "next_action", "if_then"):
            self.assertIn(k, t)
        self.assertEqual(t["on_unblock"], "manual")

    def test_tags_are_lowercased(self):
        self._w("task-a", extra="tags:\n  - Todo\n  - Decision")
        self.assertEqual(TD.scan_items(self.tmp)["task-a"]["tags"], ["todo", "decision"])

    def test_prose_fields_keep_a_hash(self):
        """`next_action`/`if_then` are free text where `#` is legitimate; only the
        structured fields get the inline-comment strip."""
        self._w("task-a", extra="next_action: PR #390 리뷰")
        self.assertEqual(TD.scan_items(self.tmp)["task-a"]["next_action"], "PR #390 리뷰")


class OrchestratorAutoUnblockTest(unittest.TestCase):
    """orchestrator.auto_unblock: the every-tick dependency release."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "nodb.db"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_flips_ready_task_and_syncs_db_once(self):
        from todo_system.orchestrator import orchestrator as O
        (self.tmp / "up.md").write_text(_md("done", "auto"), encoding="utf-8")
        (self.tmp / "down.md").write_text(
            _md("open", "blocked", depends_on=["up"], on_unblock="auto"), encoding="utf-8")
        calls = []
        flips = O.auto_unblock(self.db, self.tmp, _import=lambda: calls.append(1))
        self.assertEqual(flips, [("down", "auto")])
        self.assertEqual(TD.scan_items(self.tmp)["down"]["worker_mode"], "auto")
        self.assertEqual(calls, [1])
        # idempotent: second tick finds nothing and does not re-import
        self.assertEqual(O.auto_unblock(self.db, self.tmp, _import=lambda: calls.append(1)), [])
        self.assertEqual(calls, [1])

    def test_nothing_ready_skips_import(self):
        from todo_system.orchestrator import orchestrator as O
        (self.tmp / "up.md").write_text(_md("open", "auto"), encoding="utf-8")
        (self.tmp / "down.md").write_text(
            _md("open", "blocked", depends_on=["up"], on_unblock="auto"), encoding="utf-8")
        calls = []
        self.assertEqual(O.auto_unblock(self.db, self.tmp, _import=lambda: calls.append(1)), [])
        self.assertEqual(calls, [])

    def test_fail_open_on_apply_error(self):
        from todo_system.orchestrator import orchestrator as O

        def boom(**_):
            raise RuntimeError("disk gone")
        self.assertEqual(O.auto_unblock(self.db, self.tmp, _apply=boom), [])
