"""Tests for the orchestrator dashboard renderer (P5)."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from todo_system.orchestrator import assignment as A
from todo_system.orchestrator import dashboard
from todo_system.orchestrator.assignment import Assignment, Ledger
from todo_system.orchestrator.registry import Agent, Registry


class DashboardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.led = Ledger(self.tmp / "assignments")
        self.led.save(Assignment(task_id="core-loop", agent_id="armchair-commander",
                                 corr_id="c", state=A.IN_PROGRESS, kind="ai-local",
                                 payload={"attach": "claude attach abc123"}))
        self.led.save(Assignment(task_id="withdrawal-a2", agent_id="alice",
                                 corr_id="c", state=A.AWAITING_HUMAN, kind="human",
                                 nudge_index=2))
        self.led.save(Assignment(task_id="old-thing", agent_id="armchair-commander",
                                 corr_id="c", state=A.DONE, kind="ai-local"))
        self.reg = Registry([
            Agent(id="armchair-commander", kind="ai-local", projects=["armchair-commander"]),
            Agent(id="alice", kind="human", projects=["withdrawal-*"]),
        ])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_render_sections(self):
        md = dashboard.render(self.led, self.reg)
        self.assertIn("# 🛰 Orchestrator", md)
        self.assertIn("active 1", md)
        self.assertIn("awaiting-human 1", md)
        self.assertIn("core-loop", md)
        self.assertIn("claude attach abc123", md)          # live watch pointer
        self.assertIn("nudge #2", md)                      # parked human
        self.assertIn("armchair-commander", md)            # agent roster

    def test_agent_section_excludes_humans(self):
        # Humans are not heartbeat subjects; a `⚪ 사람` line in the runner roster
        # read as "did a person die?". People belong to the parked / 방치 sections.
        md = dashboard.render(self.led, self.reg)
        section = md.split("## 🤖 에이전트 (liveness)")[1]
        self.assertIn("**armchair-commander**", section)   # runner line untouched
        self.assertNotIn("alice", section)                 # no human line
        self.assertNotIn("⚪ 사람", md)                     # the branch itself is gone
        self.assertIn("nudge #2", md)                      # parked section unchanged

    def test_write_creates_file(self):
        out = self.tmp / "Orchestrator.md"
        dashboard.write(self.led, self.reg, str(out))
        self.assertTrue(out.exists())
        self.assertIn("Orchestrator", out.read_text())


class ConcurrentRunDashboardTest(unittest.TestCase):
    """AC2 engine half: two same-repo runs in flight render as two separate
    진행-중 rows (render iterates every active row, so concurrency surfaces for
    free), and a deferred row's serial-hold reason (from _hold_reason) rides its
    last_note into the deferred section via _deferred_line."""

    REPO = "https://github.com/example-org/some-app.git"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.led = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"])])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _section(self, md: str, header: str) -> str:
        """Text from `header` up to the next '## ' heading."""
        return md.split(header, 1)[1].split("\n## ", 1)[0]

    def test_two_same_repo_runs_render_as_two_in_flight_rows(self):
        self.led.save(Assignment(task_id="leaf-a", agent_id="w", corr_id="c",
                                 state=A.IN_PROGRESS, kind="ai-local",
                                 payload={"repo": self.REPO, "touches": ["a/**"]}))
        self.led.save(Assignment(task_id="leaf-b", agent_id="w", corr_id="c",
                                 state=A.IN_PROGRESS, kind="ai-local",
                                 payload={"repo": self.REPO, "touches": ["b/**"]}))
        md = dashboard.render(self.led, self.reg)
        running = self._section(md, "## 🏃 진행 중")
        self.assertIn("leaf-a", running)
        self.assertIn("leaf-b", running)
        self.assertIn("active 2", md)

    def test_deferred_hold_reason_renders_in_the_deferred_section(self):
        self.led.save(Assignment(
            task_id="leaf-c", agent_id="w", corr_id="c", state=A.FAILED,
            kind="ai-local", checkout_busy=True,
            last_note=("대기 — 같은 체크아웃(some-app)에서 `leaf-a` 실행 중; "
                       "사유: 겹침 있음")))
        md = dashboard.render(self.led, self.reg)
        deferred = self._section(md, "## 💤 대기 (deferred")
        self.assertIn("leaf-c", deferred)
        self.assertIn("사유: 겹침 있음", deferred)         # the classified reason flows


class ParkedUnparkHintTest(unittest.TestCase):
    """The parked section has to say what would release the task.

    "nudge #3" records that a human was pinged, never what they are supposed to
    do — so a task parked on something already fixed (an API key that got set)
    just sat there. For a row parked on an external blocker the release is
    self-service: naming `blocked_on` and the `cli resume` reservation
    reconcile_resumes() acts on turns the section into an instruction.
    Except when the PR already merged (reconcile_merged parked it): there the
    exit is closing the md, and the re-queue recipe would retry merged work.
    """

    # the stamp reconcile_merged leaves on a merged row it parks instead of
    # closing; MergedButBlockedTest renders a row the real sweep parked
    MERGED_PARK = ({"ts": "2026-09-18T07:00:00+09:00", "type": dashboard._MERGED_BUT_BLOCKED,
                    "from": "orchestrator"},)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.led = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"])])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _parked(self, task_id, blocker="", history=()):
        a = Assignment(task_id=task_id, agent_id="w", corr_id="c",
                       state=A.AWAITING_HUMAN, kind="ai-local", nudge_index=3)
        a.blocked_on = blocker
        a.history = list(history)
        self.led.save(a)

    def _block(self, md, task_id):
        """One parked row: its head line plus the `↳` lines under it."""
        self.assertIn(f"- `{task_id}` ", md)
        lines = md.splitlines()
        i = next(n for n, ln in enumerate(lines) if ln.startswith(f"- `{task_id}` "))
        block = [lines[i]]
        for ln in lines[i + 1:]:
            if not ln.startswith("    ↳"):
                break
            block.append(ln)
        return "\n".join(block)

    def test_blocker_and_release_recipe_are_rendered(self):
        self._parked("worldgraph-26-live-eval", "ANTHROPIC_API_KEY 미설정")
        md = dashboard.render(self.led, self.reg)
        self.assertIn("ANTHROPIC_API_KEY 미설정", md)
        # the actionable half
        self.assertIn("resume worldgraph-26-live-eval --after-commit", md)

    def test_parking_without_a_blocker_is_unchanged(self):
        # PR review / NEEDS_INPUT have no self-service exit; inventing one lies
        self._parked("awaiting-review")
        md = dashboard.render(self.led, self.reg)
        self.assertIn("nudge #3", md)
        self.assertNotIn("--after-commit", md)

    def test_render_survives_a_row_missing_the_blocker_field(self):
        # a row written by an older build has no blocked_on at all; an exception
        # in the renderer takes the whole 5-minute tick's dashboard down
        self.led.save(Assignment(task_id="legacy", agent_id="w", corr_id="c",
                                 state=A.AWAITING_HUMAN, kind="ai-local"))
        (self.led.root / "legacy.json").write_text(
            '{"task_id": "legacy", "agent_id": "w", "corr_id": "c",'
            ' "state": "awaiting_human"}', encoding="utf-8")
        self.assertIn("legacy", dashboard.render(self.led, self.reg))

    def test_merged_park_says_close_the_md_not_requeue(self):
        # The resume recipe on a merged row re-queues it FAILED with the blocker
        # wiped — a retry of work that already merged.
        self._parked("dash", "AC4/AC5 need a human on acme-hq", self.MERGED_PARK)
        block = self._block(dashboard.render(self.led, self.reg), "dash")
        self.assertIn("⛔ blocked_on: AC4/AC5", block)
        self.assertIn("status: done", block)
        self.assertNotIn("--after-commit", block)

    def test_merged_and_unmerged_blocked_rows_get_their_own_exit(self):
        self._parked("dash", "AC4/AC5 need a human on acme-hq", self.MERGED_PARK)
        self._parked("eval", "ANTHROPIC_API_KEY 미설정")
        md = dashboard.render(self.led, self.reg)
        dash, ev = self._block(md, "dash"), self._block(md, "eval")
        self.assertIn("status: done", dash)
        self.assertNotIn("--after-commit", dash)
        self.assertIn("--after-commit", ev)
        self.assertNotIn("status: done", ev)

    def test_render_survives_malformed_history(self):
        # history is read only for the merged stamp, and an older build or a hand
        # edit can leave anything there — the renderer runs inside the tick
        self._parked("strings", "X 미설정", ["legacy-string"])
        (self.led.root / "nulled.json").write_text(
            '{"task_id": "nulled", "agent_id": "w", "corr_id": "c",'
            ' "state": "awaiting_human", "blocked_on": "Y 미설정", "history": null}',
            encoding="utf-8")
        md = dashboard.render(self.led, self.reg)
        self.assertIn("--after-commit", self._block(md, "strings"))
        self.assertIn("--after-commit", self._block(md, "nulled"))


class DivergenceSectionTest(unittest.TestCase):
    """origin/main drift renders from the host-local state file _check_divergence
    writes — fail-soft (missing = 없음, corrupt = loud) and ⚠ once a streak has
    crossed its threshold. This section runs inside the 5-minute tick, so a bad
    file must never raise."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.led = Ledger(self.tmp / "assignments")
        self.reg = Registry([Agent(id="w", kind="ai-local", projects=["*"])])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_state(self, **over):
        import json
        d = {"ahead": 0, "behind": 0, "consecutive_behind": 0,
             "threshold": 3, "alert": False}
        d.update(over)
        p = self.tmp / "_state" / "divergence.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d), encoding="utf-8")

    def test_absent_state_reads_as_none(self):
        md = dashboard.render(self.led, self.reg)
        self.assertIn("## 🔀 Divergence", md)
        section = md.split("## 🔀 Divergence")[1].split("##")[0]
        self.assertIn("없음", section)

    def test_behind_without_alert_shows_the_counts(self):
        self._write_state(behind=2, ahead=1, consecutive_behind=1)
        md = dashboard.render(self.led, self.reg)
        self.assertIn("ahead 1 · behind 2", md)
        self.assertNotIn("⚠ origin/main", md)

    def test_a_crossed_threshold_is_promoted_to_a_warning(self):
        self._write_state(behind=7, ahead=188, consecutive_behind=3, alert=True)
        md = dashboard.render(self.led, self.reg)
        self.assertIn("⚠ origin/main과 갈라짐", md)
        self.assertIn("behind 7", md)
        self.assertIn("연속 3틱", md)

    def test_a_corrupt_state_file_is_loud_but_not_fatal(self):
        p = self.tmp / "_state" / "divergence.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{not json", encoding="utf-8")
        md = dashboard.render(self.led, self.reg)          # must not raise
        self.assertIn("스윕 실패", md)


if __name__ == "__main__":
    unittest.main()
