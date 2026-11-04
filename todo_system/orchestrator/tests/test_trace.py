"""Tests for the per-task trace view.

The fixture is the real misdiagnosis it was built for: five runs of
ai-stock-trading-23-accuracy-alpha-grid, verify.md present only in the OLDEST
and the NEWEST, and the middle runs carrying inherited artifacts.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import dashboard, orchestrator, stages, trace
from todo_system.orchestrator.assignment import Assignment, Ledger

SLUG = "ai-stock-trading-23-accuracy-alpha-grid"
RUN_IDS = [f"20260817T{t}-{SLUG}" for t in
           ("165905", "175419", "185435", "193012", "201522")]


class TraceTest(unittest.TestCase):
    def setUp(self):
        # These asserts pin the ko text byte for byte; the en default (and its
        # no-Hangul guarantee) is covered in todo_system/tests/test_trace.py.
        patcher = mock.patch.dict(os.environ, {"MACP_LANGUAGE": "ko"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.runs = self.tmp / "runs"
        self.runs.mkdir()
        for rid in RUN_IDS:
            (self.runs / rid).mkdir()
        # run 1 ran everything and verified FAIL
        self._research(RUN_IDS[0], author=RUN_IDS[0])
        self._verify(RUN_IDS[0], "fail")
        # runs 2-4 inherited run 1's research; none of them verified
        for rid in RUN_IDS[1:4]:
            self._research(rid, author=RUN_IDS[0], inherited_from=RUN_IDS[0])
        # run 5 inherited too, and verified PASS
        self._research(RUN_IDS[4], author=RUN_IDS[0], inherited_from=RUN_IDS[0])
        self._verify(RUN_IDS[4], "pass")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _research(self, run_id, *, author, inherited_from=None):
        p = self.runs / run_id / stages.STAGE_ARTIFACT["research"]
        p.write_text(f"---\nstage: research\nrun_id: {author}\n---\n\n# R\n본문\n",
                     encoding="utf-8")
        if inherited_from:
            stages.stamp_inherited(p, inherited_from)
        stages.record_stage(self.runs / run_id, "research")

    def _verify(self, run_id, verdict):
        # shape copied from a real verify.md — it DOES carry run_id, which is what
        # lets an unstamped artifact be told apart from a pre-stamp copy
        (self.runs / run_id / stages.STAGE_ARTIFACT["verify"]).write_text(
            f"---\nstage: verify\nrun_id: {run_id}\noverall_status: {verdict}\n---\n",
            encoding="utf-8")
        stages.record_stage(self.runs / run_id, "verify")

    def _render(self, slug=SLUG, ledger=None):
        return trace.render(slug, ledger, runs_dir=self.runs, repo_dir=self.tmp,
                            pr_lookup=lambda *a, **k: None,
                            branch_lookup=lambda *a, **k: False)

    # --- the incident ----------------------------------------------------

    def test_stale_verify_verdict_is_labelled_with_how_stale(self):
        md = self._render()
        self.assertIn("런 1/5", md)
        self.assertIn("FAIL", md)
        self.assertIn("이후 4번의 런", md)          # the fact the misdiagnosis missed
        self.assertIn("런 5/5", md)
        self.assertIn("PASS", md)
        self.assertIn("최신 런의 판정", md)
        self.assertIn("verify는 상속되지 않음", md)      # the trap, named in the heading

    def test_latest_run_is_the_newest_and_shows_inherited_source(self):
        md = self._render()
        self.assertIn(RUN_IDS[4], md)                       # latest run dir
        self.assertIn(f"**상속본** ← `{RUN_IDS[0]}`", md)     # provenance, from the file
        self.assertIn("verify.md", md)

    def test_own_artifact_is_not_called_inherited(self):
        md = trace.render(SLUG, None, runs_dir=self.runs, repo_dir=self.tmp,
                          pr_lookup=lambda *a, **k: None,
                          branch_lookup=lambda *a, **k: False)
        # run 5's verify.md was written by run 5 — no stamp, and its frontmatter
        # carries no foreign run_id, so it must not be flagged
        self.assertNotIn("`verify.md` (verify) — ⚠", md)
        self.assertIn("`verify.md` (verify) — 이번 런", md)

    def test_artifact_with_no_author_claim_is_presumed_own(self):
        # run.log carries no frontmatter, so 'own' can only ever be a presumption —
        # it must be stated as one, and must not read as inherited
        p = self.runs / RUN_IDS[4] / stages.STAGE_ARTIFACT["implement"]
        p.write_text("\n=== implement (rc=0) ===\nout\n", encoding="utf-8")
        stages.record_stage(self.runs / RUN_IDS[4], "implement")
        md = self._render()
        self.assertIn("`run.log` (implement) — 출처 표시 없음: **이번 런 생성으로 추정**", md)
        self.assertNotIn("`run.log` (implement) — **상속본**", md)

    def test_unstamped_copy_is_flagged_not_silently_trusted(self):
        # a pre-stamp inheritance: the file says another run wrote it, no stamp
        p = self.runs / RUN_IDS[4] / stages.STAGE_ARTIFACT["plan"]
        p.write_text(f"---\nstage: plan\nrun_id: {RUN_IDS[0]}\n---\n\n# P\n",
                     encoding="utf-8")
        stages.record_stage(self.runs / RUN_IDS[4], "plan")
        md = self._render()
        self.assertIn("출처 불명", md)
        self.assertIn(RUN_IDS[0], md)

    # --- read-only -------------------------------------------------------

    def test_render_has_no_side_effects(self):
        def snapshot():
            return {p: p.stat().st_mtime_ns for p in sorted(self.tmp.rglob("*"))}

        before = snapshot()
        self._render()
        self.assertEqual(snapshot(), before)

    def test_module_has_no_write_entry_point(self):
        # dashboard.py pairs render() with write(); this one deliberately does
        # not — a diagnostic that can write causes incidents during incidents
        self.assertFalse(hasattr(trace, "write"))
        src = Path(trace.__file__).read_text(encoding="utf-8")
        for forbidden in ("write_text", "write_bytes", "mkdir", "subprocess",
                          ".save(", "os.remove", "shutil"):
            self.assertNotIn(forbidden, src, f"trace.py must not use {forbidden}")

    # --- unknown is not failure -------------------------------------------

    def test_missing_gh_renders_unknown_not_failure(self):
        md = self._render()
        self.assertIn("PR: unknown", md)
        # the contract gh_pr_merged() documents: None must NOT collapse into the
        # "a PR exists and has not merged" answer — that false negative is what
        # parks already-merged PRs and sends humans hunting for a break
        self.assertNotIn("아직 머지 안 됨", md)
        self.assertNotIn("머지됨", md)

    def test_merged_pr_is_reported(self):
        md = trace.render(SLUG, None, runs_dir=self.runs, repo_dir=self.tmp,
                          pr_lookup=lambda *a, **k: True,
                          branch_lookup=lambda *a, **k: True)
        self.assertIn("머지됨", md)
        self.assertIn("origin에 존재: 예", md)

    def test_lookup_that_raises_is_unknown_not_a_crash(self):
        def boom(*a, **k):
            raise OSError("gh: command not found")

        md = trace.render(SLUG, None, runs_dir=self.runs, repo_dir=self.tmp,
                          pr_lookup=boom, branch_lookup=boom)
        self.assertIn("unknown", md)

    # --- selection --------------------------------------------------------

    def test_runs_for_matches_the_whole_slug_not_a_suffix(self):
        # the fullmatch now shared with resumable_run(); the glob(f"*-{slug}")
        # it used to use would have matched all five
        self.assertEqual(trace.runs_for("grid", self.runs), [])
        self.assertEqual(trace.runs_for("alpha-grid", self.runs), [])
        self.assertEqual([p.name for p in trace.runs_for(SLUG, self.runs)], RUN_IDS)

    # --- degenerate states -------------------------------------------------

    def test_no_runs_at_all(self):
        md = self._render(slug="never-ran")
        self.assertIn("런 없음", md)
        self.assertIn("verify.md 를 남긴 런이 하나도 없음", md)

    def test_no_runs_but_a_ledger_entry(self):
        led = Ledger(self.tmp / "assignments")
        led.save(Assignment(task_id="never-ran", agent_id="armchair-commander",
                            corr_id="c", state="awaiting_human", refail_count=2,
                            blocked_on="upstream PR #13", last_note="parked"))
        md = self._render(slug="never-ran", ledger=led)
        self.assertIn("런 없음", md)
        self.assertIn("awaiting_human", md)
        self.assertIn("refail_count: 2", md)
        self.assertIn("upstream PR #13", md)

    def test_runs_but_no_ledger_entry(self):
        led = Ledger(self.tmp / "assignments")
        md = self._render(ledger=led)
        self.assertIn("assignment 없음", md)
        self.assertIn("런 5/5", md)

    def test_runs_with_no_verify_anywhere(self):
        for rid in (RUN_IDS[0], RUN_IDS[4]):
            (self.runs / rid / stages.STAGE_ARTIFACT["verify"]).unlink()
        md = self._render()
        self.assertIn("아직 검증된 적이 없다", md)

    # --- "why is this not running?" --------------------------------------

    # the stamp reconcile_merged leaves on a merged row it parks instead of
    # closing (test_dashboard's fixture); MergedButBlockedTest renders the trace
    # of a row the real sweep parked
    MERGED_PARK = ({"ts": "2026-09-18T07:00:00+09:00", "type": dashboard._MERGED_BUT_BLOCKED,
                    "from": "orchestrator"},)
    # the unmerged park's release line, byte for byte
    REQUEUE_RECIPE = ("  - 해제: 블로커 해소 후 태스크 md의 `worker_mode`를 비-디스패치 "
                      "값(`blocked` 등)으로 바꿨다가 `auto`로 되돌리면 "
                      "`reconcile_unparked()`가 그 전이를 해제 신호로 읽어 자동 재큐한다 "
                      "— 원장 수기 편집 불필요")

    def _led(self, state, blocked_on="", history=()):
        led = Ledger(self.tmp / "assignments")
        led.save(Assignment(task_id="never-ran", agent_id="w", corr_id="c",
                            state=state, blocked_on=blocked_on, history=list(history)))
        return led

    @staticmethod
    def _ledger_part(md):
        """The `## 원장` section alone, so no other section can satisfy an assert."""
        return md.split("## 원장")[1].split("## 브랜치")[0]

    def test_parked_ledger_row_explains_why_dispatch_never_fires(self):
        # the question the trace exists to answer, and the one thing todo.db and
        # CI cannot tell you: a non-terminal row short-circuits dispatch() before
        # routing, so the task can be a candidate every tick and still never run
        md = self._render(slug="never-ran", ledger=self._led("awaiting_human"))
        self.assertIn("파킹", md)
        self.assertIn("dispatch()", md)

    def test_parked_on_a_blocker_also_shows_how_to_unpark(self):
        md = self._render(slug="never-ran",
                          ledger=self._led("awaiting_human", "ANTHROPIC_API_KEY 미설정"))
        self.assertIn("ANTHROPIC_API_KEY 미설정", md)
        self.assertIn("worker_mode", md)
        self.assertIn("reconcile_unparked", md)

    def test_an_unparked_row_gets_no_parking_note(self):
        md = self._render(slug="never-ran", ledger=self._led("failed"))
        self.assertNotIn("파킹", md)
        self.assertNotIn("worker_mode", md)

    def test_merged_park_says_close_the_md_not_requeue(self):
        # reconcile_merged parked this row after its PR merged, on a step a human
        # still owes. The worker_mode recipe would make reconcile_unparked re-queue
        # it FAILED with the blocker wiped — a retry of work that already merged.
        part = self._ledger_part(self._render(
            slug="never-ran", ledger=self._led("awaiting_human",
                                               "AC4/AC5 need a human on acme-hq",
                                               self.MERGED_PARK)))
        self.assertIn("파킹", part)                 # still parked: dispatch() stays None
        self.assertIn("dispatch()", part)
        self.assertIn("- blocked_on: AC4/AC5", part)
        self.assertIn("status: done", part)        # the exit is closing the md
        self.assertIn("reconcile_merged()", part)
        self.assertNotIn("worker_mode", part)
        self.assertNotIn("reconcile_unparked", part)

    def test_unmerged_park_keeps_the_requeue_recipe_verbatim(self):
        part = self._ledger_part(self._render(
            slug="never-ran", ledger=self._led("awaiting_human", "ANTHROPIC_API_KEY 미설정")))
        self.assertIn(self.REQUEUE_RECIPE, part.splitlines())
        self.assertIn("- blocked_on: ANTHROPIC_API_KEY 미설정", part)
        self.assertNotIn("status: done", part)

    def test_malformed_history_falls_back_to_the_recipe(self):
        # history is read only for the merged stamp, and an older build or a hand
        # edit can leave anything there — a non-dict entry, or null for the list
        led = Ledger(self.tmp / "assignments")
        led.save(Assignment(task_id="strings", agent_id="w", corr_id="c",
                            state="awaiting_human", blocked_on="X 미설정",
                            history=["legacy-string"]))
        (led.root / "nulled.json").write_text(
            '{"task_id": "nulled", "agent_id": "w", "corr_id": "c",'
            ' "state": "awaiting_human", "blocked_on": "Y 미설정", "history": null}',
            encoding="utf-8")
        for slug in ("strings", "nulled"):
            part = self._ledger_part(self._render(slug=slug, ledger=led))
            self.assertIn(self.REQUEUE_RECIPE, part.splitlines(), slug)

    def test_closed_merged_row_gets_no_exit_note(self):
        # once the human closes the md, reconcile_merged ends the row DONE with its
        # stamp and blocker still on it; the exit note belongs to the park alone
        part = self._ledger_part(self._render(
            slug="never-ran", ledger=self._led("done", "AC4/AC5 need a human on acme-hq",
                                               self.MERGED_PARK)))
        self.assertIn("- blocked_on: AC4/AC5", part)
        self.assertNotIn("파킹", part)
        self.assertNotIn("status: done", part)
        self.assertNotIn("worker_mode", part)


class TraceCliTest(unittest.TestCase):
    """The subcommand is registered, required-arg'd, and wired to render()."""

    def test_trace_is_a_registered_subcommand(self):
        buf = io.StringIO()
        with self.assertRaises(SystemExit), redirect_stdout(buf):
            orchestrator._main(["--help"])
        self.assertIn("trace", buf.getvalue())

    def test_task_is_required(self):
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
            orchestrator._main(["trace"])

    def test_cli_prints_the_view(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        buf = io.StringIO()
        with mock.patch.object(orchestrator, "open_mailbox"), \
             mock.patch.object(orchestrator, "load_registry"), \
             mock.patch.object(stages, "RUNS_DIR", tmp / "runs"), \
             mock.patch.object(stages, "gh_pr_merged", lambda *a, **k: None), \
             mock.patch.object(stages, "remote_branch_exists", lambda *a, **k: False), \
             redirect_stdout(buf):
            rc = orchestrator._main(["--ledger", str(tmp / "assignments"),
                                     "trace", "--task", SLUG])
        self.assertEqual(rc, 0)
        self.assertIn(f"trace — `{SLUG}`", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
