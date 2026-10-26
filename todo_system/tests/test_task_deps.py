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
