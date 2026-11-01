"""Tests for the per-stage pipeline breakdown added to metrics.py.

Pins two things the todo-sync daemon depends on: (1) pipeline_breakdown()
aggregates the ledger (routing + rework) and the Worker/runs sample (per-stage
calls/bytes/verdicts, redispatch) correctly over a tmp fixture, and (2) render()
still emits its original four sections verbatim and merely APPENDS the breakdown
— the append must never rewrite or drop the sections the daemon regenerates.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import metrics


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ledger(dir_: Path, task_id: str, mode: str, state: str,
            attempts: int = 1, refail: int = 0) -> None:
    _write(dir_ / f"{task_id}.json", json.dumps({
        "task_id": task_id, "state": state, "attempts": attempts,
        "refail_count": refail, "payload": {"worker_mode": mode},
    }))


def _run(runs: Path, name: str, *, completed=None, arts: dict | None = None) -> None:
    d = runs / name
    d.mkdir(parents=True, exist_ok=True)
    if completed is not None:
        _write(d / "stages.json", json.dumps({"completed": completed}))
    for fname, content in (arts or {}).items():
        _write(d / fname, content)


class PipelineBreakdownTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.led = self.tmp / "assignments"
        self.runs = self.tmp / "runs"
        self.led.mkdir()
        self.runs.mkdir()

        # Ledger: 3 auto (one retried+refailed, one rejected + ci-fix id), 1 quick.
        _ledger(self.led, "alpha", "auto", "done")
        _ledger(self.led, "beta", "auto", "done", attempts=2, refail=1)
        _ledger(self.led, "ci-fix-delta", "auto", "rejected")
        _ledger(self.led, "gamma", "quick", "done")

        # Runs: A is a full 5-stage run of slug "alpha"; B is a SECOND run of the
        # same slug (redispatch) whose stages.json completed only research+plan —
        # even though verify.md exists on disk, it must NOT be counted as a verify
        # CALL, while its bytes and verdict still are. C has no stages.json, so it
        # falls back to artifact presence (research + implement).
        _run(self.runs, "20260101T010101-alpha",
             completed=["research", "plan", "implement", "verify", "report"],
             arts={"requirements.md": "R" * 10, "implementation_plan.md": "P" * 20,
                   "run.log": "I" * 30, "verify.md": "overall_status: pass\n",
                   "report.md": "T" * 5})
        _run(self.runs, "20260102T020202-alpha",
             completed=["research", "plan"],
             arts={"requirements.md": "r" * 7, "implementation_plan.md": "p" * 3,
                   "verify.md": "overall_status: fail\n"})
        _run(self.runs, "20260103T030303-mode-7-thing",
             completed=None,
             arts={"requirements.md": "x" * 4, "run.log": "y" * 6})

        self.bd = metrics.pipeline_breakdown(runs=self.runs, ledger=self.led)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_population_sizes_reported_separately(self):
        # The two Ns are different populations and both must be reported.
        self.assertEqual(self.bd["ledger_n"], 4)
        self.assertEqual(self.bd["run_dirs_n"], 3)

    def test_routing_worker_mode_by_state(self):
        self.assertEqual(self.bd["routing_totals"], {"auto": 3, "quick": 1})
        self.assertEqual(self.bd["routing"]["auto"], {"done": 2, "rejected": 1})
        self.assertEqual(self.bd["routing"]["quick"], {"done": 1})

    def test_rework_from_ledger(self):
        self.assertEqual(self.bd["rework"],
                         {"retried": 1, "refailed": 1, "refail_total": 1, "ci_fix": 1})
        self.assertEqual(self.bd["rework_by_mode"]["auto"],
                         {"n": 3, "done": 2, "retried": 1, "refailed": 1, "refail_total": 1})
        self.assertEqual(self.bd["rework_by_mode"]["quick"]["refail_total"], 0)

    def test_stage_calls_prefer_completed_over_artifacts(self):
        # A: 5 from completed. B: only research+plan from completed (verify.md on
        # disk is ignored for CALLS). C: research+implement from artifact fallback.
        self.assertEqual(self.bd["stage_calls"],
                         {"research": 3, "plan": 2, "implement": 2, "verify": 1, "report": 1})

    def test_stage_bytes_sum_every_artifact_on_disk(self):
        # Bytes count the artifact wherever it exists, independent of `completed`,
        # so verify sums BOTH verify.md files even though verify was 1 call.
        self.assertEqual(self.bd["stage_bytes"], {
            "research": 10 + 7 + 4, "plan": 20 + 3, "implement": 30 + 6,
            "verify": 21 + 21, "report": 5,
        })

    def test_verify_verdict_tally(self):
        self.assertEqual(self.bd["verify_verdicts"], {"PASS": 1, "FAIL": 1})

    def test_redispatch_groups_by_slug_without_mangling_digits(self):
        rd = self.bd["redispatch"]
        self.assertEqual(rd["run_dirs"], 3)
        self.assertEqual(rd["distinct_slugs"], 2)          # alpha, mode-7-thing
        self.assertEqual(rd["redispatched_slugs"], 1)      # alpha ran twice
        self.assertEqual(rd["multi"], {"alpha": 2})
        # the digit-bearing slug survived the timestamp strip intact
        slugs = {metrics._slug_of(p.name) for p in self.runs.iterdir()}
        self.assertIn("mode-7-thing", slugs)

    def test_slug_of_strips_only_timestamp_prefix(self):
        self.assertEqual(metrics._slug_of("20260101T010101-mulkko-32-dag"), "mulkko-32-dag")
        self.assertEqual(metrics._slug_of("no-prefix-2-slug"), "no-prefix-2-slug")

    def test_empty_dirs_do_not_raise(self):
        empty = Path(tempfile.mkdtemp())
        try:
            bd = metrics.pipeline_breakdown(runs=empty / "runs", ledger=empty / "led")
            self.assertEqual(bd["ledger_n"], 0)
            self.assertEqual(bd["run_dirs_n"], 0)
            self.assertEqual(bd["stage_calls"], {})
        finally:
            shutil.rmtree(empty, ignore_errors=True)


_FIXTURE = {
    "ledger_n": 42, "run_dirs_n": 5,
    "routing_totals": {"auto": 30, "quick": 12},
    "routing": {"auto": {"done": 28, "rejected": 2}, "quick": {"done": 12}},
    "rework_by_mode": {},
    "rework": {"retried": 3, "refailed": 2, "refail_total": 4, "ci_fix": 1},
    "stage_calls": {"research": 5, "plan": 5, "implement": 4, "verify": 4,
                    "report": 3, "delegate": 1},
    "stage_bytes": {"research": 1000, "plan": 2000, "implement": 3000,
                    "verify": 1500, "report": 800, "delegate": 50},
    "verify_verdicts": {"PASS": 3, "FAIL": 1},
    "redispatch": {"run_dirs": 5, "distinct_slugs": 4, "redispatched_slugs": 1,
                   "multi": {"alpha": 2}},
}


class BreakdownLinesTest(unittest.TestCase):
    def test_formats_every_field(self):
        text = "\n".join(metrics._breakdown_lines(_FIXTURE))
        self.assertIn("## 스테이지별 분해 (pipeline breakdown)", text)
        self.assertIn("**42건**", text)                       # ledger_n
        self.assertIn("**5개**", text)                        # run_dirs_n
        self.assertIn("- **auto**: 30 (done 28, rejected 2)", text)
        self.assertIn("- research: 콜 5 · 1,000 bytes", text)
        self.assertIn("- delegate: 콜 1 · 50 bytes", text)     # extra stage appended
        self.assertIn("FAIL 1", text)
        self.assertIn("PASS 3", text)
        self.assertIn("2회 이상 실행 태스크 1", text)
        self.assertIn("ci-fix 1", text)


class RenderContractTest(unittest.TestCase):
    """render() must KEEP its four original sections and only APPEND the breakdown."""

    ORIGINAL_HEADERS = (
        "# 📊 자율 시스템 메트릭",
        "## 처리량 (throughput)",
        "## 실행 신뢰성 (얼마나 매끄럽게 되나)",
        "## 비용 proxy",
        "## rework proxy (품질 신호 — 낮을수록 좋음)",
        "## 해석",
    )

    def test_render_keeps_sections_and_appends_breakdown(self):
        with mock.patch.object(metrics, "pipeline_breakdown", return_value=_FIXTURE):
            md = metrics.render()
        for header in self.ORIGINAL_HEADERS:
            self.assertIn(header, md)
        self.assertIn("## 스테이지별 분해 (pipeline breakdown)", md)
        # append lands AFTER the last original section, never before it.
        self.assertLess(md.index("## 해석"),
                        md.index("## 스테이지별 분해 (pipeline breakdown)"))

    def test_render_survives_breakdown_failure(self):
        # The append is best-effort: a breakdown blow-up must not break Metrics.md.
        with mock.patch.object(metrics, "pipeline_breakdown",
                               side_effect=RuntimeError("boom")):
            md = metrics.render()
        for header in self.ORIGINAL_HEADERS:
            self.assertIn(header, md)
        self.assertNotIn("## 스테이지별 분해", md)


def _usage(i, o, cc, cr, cost):
    return {"input_tokens": i, "output_tokens": o, "cache_creation_input_tokens": cc,
            "cache_read_input_tokens": cr, "num_turns": 1, "duration_ms": 5,
            "model": "m", "session_id": "s", "total_cost_usd": cost}


class StageUsageBreakdownTest(unittest.TestCase):
    """stage_usage: per-stage token N / Σ / median from stages.json `usage`. A
    run_dir from before instrumentation has none, so it leaves the sample and
    shows only in the 'k of N run_dirs' line."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runs = self.tmp / "runs"
        self.led = self.tmp / "led"
        self.runs.mkdir()
        self.led.mkdir()
        _run(self.runs, "20260919T010101-a", arts={"stages.json": json.dumps({
            "completed": ["research", "plan", "implement"],
            "usage": {"research": _usage(10, 20, 30, 37104, 0.5),
                      "plan": _usage(1, 2, 3, 4, None),
                      "implement": {"unavailable": "timeout"}}})})
        _run(self.runs, "20260919T020202-b", arts={"stages.json": json.dumps({
            "completed": ["research", "plan"],
            "usage": {"research": _usage(30, 41, 50, 40000, 0.25),
                      "plan": _usage(3, 4, 5, 6, 0.1),
                      "verify": {"input_tokens": "12"},        # damaged
                      "report": "not a dict",                 # damaged
                      "quick": _usage(5, 5, 5, 5, 0.004)}})})
        _run(self.runs, "20260101T000000-old", completed=["research", "plan"])
        _run(self.runs, "20260102T000000-odd", arts={"stages.json": json.dumps({
            "completed": ["research"], "usage": []})})         # not a map → no usage
        self.bd = metrics.pipeline_breakdown(runs=self.runs, ledger=self.led)
        self.su = self.bd["stage_usage"]["stages"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_runs_without_usage_leave_the_sample_but_are_counted(self):
        self.assertEqual(self.bd["run_dirs_n"], 4)
        self.assertEqual(self.bd["stage_usage"]["runs_with_usage"], 2)
        # the old run's research/plan calls still count as calls, not as usage
        self.assertEqual(self.bd["stage_calls"]["research"], 4)
        self.assertEqual(self.su["research"]["n"], 2)

    def test_sums_medians_and_quota(self):
        r = self.su["research"]
        self.assertEqual((r["n"], r["unavailable"]), (2, 0))
        self.assertEqual(r["sum"], {"input_tokens": 40, "output_tokens": 61,
                                    "cache_creation_input_tokens": 80,
                                    "cache_read_input_tokens": 77104,
                                    "quota_tokens": 181})       # (10+30+20)+(30+50+41)
        self.assertEqual(r["median"], {"input_tokens": 20, "output_tokens": 30.5,
                                       "cache_creation_input_tokens": 40,
                                       "cache_read_input_tokens": 38552,
                                       "quota_tokens": 90.5})
        self.assertEqual((r["cost_usd"], r["cost_n"]), (0.75, 2))

    def test_unavailable_and_damaged_records_are_counted_not_summed(self):
        for st in ("implement", "verify", "report"):
            self.assertEqual((self.su[st]["n"], self.su[st]["unavailable"]), (0, 1), st)
            self.assertEqual(self.su[st]["median"], {}, st)
            self.assertIsNone(self.su[st]["cost_usd"], st)

    def test_cost_counts_only_the_records_that_carry_one(self):
        self.assertEqual((self.su["plan"]["cost_usd"], self.su["plan"]["cost_n"],
                          self.su["plan"]["n"]), (0.1, 1, 2))

    def test_table_lines(self):
        text = "\n".join(metrics._breakdown_lines(self.bd))
        self.assertIn("### 스테이지별 토큰 usage (stages.json `usage`)", text)
        self.assertIn("run_dir 4개 중 usage 보유 2개", text)
        self.assertIn("| stage | N | unavailable | 쿼터 Σ (중앙값) | input Σ (중앙값) "
                      "| output Σ (중앙값) | cache_creation Σ (중앙값) "
                      "| cache_read Σ (중앙값) | 비용 추정 Σ |", text)
        self.assertIn("| research | 2 | 0 | 181 (90.5) | 40 (20) | 61 (30.5) "
                      "| 80 (40) | 77,104 (38,552) | $0.75 |", text)
        self.assertIn("| plan | 2 | 0 | 18 (9) | 4 (2) | 6 (3) | 8 (4) | 10 (5) "
                      "| $0.10 (1/2) |", text)
        self.assertIn("| implement | 0 | 1 | — | — | — | — | — | — |", text)
        self.assertIn("| quick | 1 | 0 | 15 (15) | 5 (5) | 5 (5) | 5 (5) | 5 (5) "
                      "| $0.0040 |", text)
        # STAGES_AUTO order first, anything else after
        self.assertLess(text.index("| report |"), text.index("| quick |"))
        self.assertLess(text.index("| research |"), text.index("| plan |"))

    def test_a_breakdown_without_stage_usage_still_renders(self):
        text = "\n".join(metrics._breakdown_lines(_FIXTURE))
        self.assertIn("- 아직 usage 가 기록된 run_dir 이 없다 (표본 run_dir 5개 전부 계측 이전)",
                      text)
        self.assertIn("## 스테이지별 분해 (pipeline breakdown)", text)

    def test_render_carries_the_table_inside_the_breakdown_section(self):
        with mock.patch.object(metrics, "pipeline_breakdown", return_value=self.bd):
            md = metrics.render()
        self.assertLess(md.index("## 스테이지별 분해 (pipeline breakdown)"),
                        md.index("### 스테이지별 토큰 usage"))
        self.assertIn("| research | 2 | 0 |", md)


_FULL = ["research", "plan", "implement", "verify", "report"]


def _run_usage_fixture(runs: Path, name: str, usage: dict, completed=None) -> None:
    _run(runs, name, arts={"stages.json": json.dumps({
        "completed": _FULL if completed is None else completed, "usage": usage})})


def _five(other, report) -> dict:
    """research..verify all get `other`, report gets `report`."""
    return {**{st: other for st in _FULL[:4]}, "report": report}


class ReportStageShareTest(unittest.TestCase):
    """report_stage_share: report's share of the 5-stage pipeline, over full
    runs only. A: report is 1/3 of io/quota/cost. B: report is 10/810. Pooled and
    per-run-median ratios therefore diverge; C–F are the four exclusion shapes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runs = self.tmp / "runs"
        self.runs.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _sample(self):
        _run_usage_fixture(self.runs, "20261001T000001-a", _five(
            _usage(10, 10, 5, 100, 0.1), _usage(20, 20, 10, 500, 0.2)))
        _run_usage_fixture(self.runs, "20261001T000002-b", _five(
            _usage(100, 100, 0, 0, 1.0), _usage(5, 5, 0, 0, 0.05)))
        _run_usage_fixture(self.runs, "20261001T000003-c", _five(
            _usage(1, 1, 1, 1, 0.01), {"unavailable": "timeout"}))
        _run_usage_fixture(self.runs, "20261001T000004-d",
                           {"quick": _usage(1, 1, 1, 1, 0.01)}, completed=["quick"])
        _run(self.runs, "20261001T000005-e", completed=_FULL)
        f = _five(_usage(1, 1, 1, 1, 0.01), _usage(1, 1, 1, 1, 0.01))
        del f["research"]
        _run_usage_fixture(self.runs, "20261001T000006-f", f)
        return metrics.report_stage_share(self.runs)

    def test_sample_and_exclusions(self):
        s = self._sample()
        self.assertEqual((s["run_dirs_n"], s["n"], s["cost_n"]), (6, 2, 2))
        self.assertEqual(s["excluded"],
                         {"no_usage": 1, "not_full_pipeline": 1, "partial_usage": 2})
        self.assertEqual(s["partial_by_stage"], {"report": 1, "research": 1})

    def test_pooled_vs_median_ratios_per_basis(self):
        s = self._sample()
        io = s["bases"]["io"]
        self.assertEqual((io["report_sum"], io["pipeline_sum"]), (50, 930))
        self.assertEqual(io["report_median"], 25)
        self.assertAlmostEqual(io["pooled_ratio"], 0.0538)
        self.assertAlmostEqual(io["median_run_ratio"], 0.1728)   # (1/3 + 10/810) / 2
        q = s["bases"]["quota"]
        self.assertEqual((q["report_sum"], q["pipeline_sum"]), (60, 960))
        self.assertAlmostEqual(q["pooled_ratio"], 0.0625)
        a = s["bases"]["all"]
        self.assertEqual((a["report_sum"], a["pipeline_sum"]), (560, 1860))
        self.assertAlmostEqual(a["pooled_ratio"], 0.3011)
        c = s["cost"]
        self.assertAlmostEqual(c["report_sum"], 0.25)
        self.assertAlmostEqual(c["pipeline_sum"], 4.65)
        self.assertAlmostEqual(c["report_median"], 0.125)
        self.assertAlmostEqual(c["pooled_ratio"], 0.0538)

    def test_verdict_keep_below_threshold(self):
        s = self._sample()
        self.assertEqual((s["primary_basis"], s["verdict"]), ("cost_usd", "keep"))
        self.assertAlmostEqual(s["primary_ratio"], 0.0538)
        self.assertEqual(s["threshold"], 0.10)

    def test_verdict_de_model_at_or_above_threshold(self):
        _run_usage_fixture(self.runs, "20261001T000001-a", _five(
            _usage(10, 10, 5, 100, 0.1), _usage(20, 20, 10, 500, 0.2)))
        s = metrics.report_stage_share(self.runs)
        self.assertEqual((s["n"], s["verdict"]), (1, "de-model"))
        self.assertAlmostEqual(s["primary_ratio"], 0.3333)

    def test_run_missing_one_cost_leaves_cost_but_stays_in_tokens(self):
        _run_usage_fixture(self.runs, "20261001T000001-a", _five(
            _usage(10, 10, 0, 0, 0.1), _usage(10, 10, 0, 0, None)))
        _run_usage_fixture(self.runs, "20261001T000002-b", _five(
            _usage(10, 10, 0, 0, 0.1), _usage(10, 10, 0, 0, 0.1)))
        s = metrics.report_stage_share(self.runs)
        self.assertEqual((s["n"], s["cost_n"]), (2, 1))
        self.assertEqual(s["bases"]["io"]["report_sum"], 40)
        self.assertAlmostEqual(s["cost"]["pooled_ratio"], 0.2)

    def test_no_cost_falls_back_to_quota(self):
        _run_usage_fixture(self.runs, "20261001T000001-a", _five(
            _usage(100, 100, 0, 0, None), _usage(5, 5, 0, 0, None)))
        s = metrics.report_stage_share(self.runs)
        self.assertEqual((s["cost_n"], s["primary_basis"], s["verdict"]),
                         (0, "quota", "keep"))
        self.assertAlmostEqual(s["primary_ratio"], 0.0123)

    def test_empty_runs_is_insufficient(self):
        s = metrics.report_stage_share(self.tmp / "nope")
        self.assertEqual((s["n"], s["verdict"]), (0, "insufficient"))
        self.assertIsNone(s["primary_ratio"])


_VFAIL = "---\noverall_status: fail\n---\n"
_VPASS = "---\noverall_status: pass\n---\n"


def _vrun(runs: Path, name: str, usage: dict | None, verify: str | None = None) -> None:
    """usage=None → stages.json without `usage` (a pre-instrumentation run)."""
    if usage is None:
        _run(runs, name, completed=_FULL)
    else:
        _run_usage_fixture(runs, name, usage)
    if verify is not None:
        _write(runs / name / "verify.md", verify)


_RETRY = {"research": _usage(10, 20, 30, 40, 0.3), "plan": _usage(1, 2, 3, 4, 0.2),
          "implement": _usage(100, 100, 0, 0, 0.5), "verify": {"unavailable": "timeout"}}


class VerifyFailRetryCostTest(unittest.TestCase):
    """verify_fail_retry_cost: (FAIL run, next run of the same slug) pairs and
    what the retry spent on research + plan. Every 'not a pair' case sits next to
    a positive control so a function that finds nothing cannot pass."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.runs = self.tmp / "runs"
        self.runs.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _f(self):
        return metrics.verify_fail_retry_cost(self.runs)

    def test_one_pair_sums_and_ratios(self):
        _vrun(self.runs, "20261001T000001-t",
              _five(_usage(1, 1, 1, 1, 0.1), _usage(1, 1, 1, 1, 0.1)), _VFAIL)
        _vrun(self.runs, "20261001T000002-t", _RETRY, _VPASS)
        s = self._f()
        self.assertEqual((s["run_dirs_n"], s["n"], s["plan_missing"]), (2, 1, 0))
        p = s["pairs"][0]
        self.assertEqual((p["slug"], p["fail_run"], p["retry_run"]),
                         ("t", "20261001T000001-t", "20261001T000002-t"))
        self.assertEqual((p["retry_verdict"], p["blocked_on"], p["cause_hint"]),
                         ("PASS", None, None))
        self.assertAlmostEqual(p["rp_cost"], 0.5)
        self.assertAlmostEqual(p["retry_total_cost"], 1.0)
        self.assertEqual((p["rp_io"], p["rp_quota"]), (33, 66))
        self.assertEqual((p["retry_io"], p["retry_quota"]), (233, 266))
        self.assertAlmostEqual(s["rp_cost_sum"], 0.5)
        self.assertAlmostEqual(s["retry_cost_sum"], 1.0)
        self.assertAlmostEqual(s["pooled_ratio"], 0.5)
        self.assertAlmostEqual(s["median_run_ratio"], 0.5)
        self.assertEqual(s["tokens"]["io"], {"rp_sum": 33, "retry_sum": 233,
                                             "pooled_ratio": 0.1416})
        self.assertEqual(s["tokens"]["quota"], {"rp_sum": 66, "retry_sum": 266,
                                                "pooled_ratio": 0.2481})
        self.assertAlmostEqual(s["sample_spend"], 1.5)    # F 0.5 + R 1.0
        self.assertAlmostEqual(s["spend_ratio"], 0.3333)

    def test_a_run_after_pass_is_not_a_pair(self):
        _vrun(self.runs, "20261001T000001-a", _RETRY, _VPASS)
        _vrun(self.runs, "20261001T000002-a", _RETRY, _VFAIL)
        _vrun(self.runs, "20261001T000003-a", _RETRY)
        s = self._f()
        self.assertEqual(s["n"], 1)
        self.assertEqual(s["pairs"][0]["fail_run"], "20261001T000002-a")
        self.assertIsNone(s["pairs"][0]["retry_verdict"])

    def test_fail_as_last_run_is_no_retry(self):
        _vrun(self.runs, "20261001T000001-t", _RETRY, _VFAIL)
        _vrun(self.runs, "20261001T000002-t", _RETRY, _VFAIL)
        s = self._f()
        self.assertEqual((s["n"], s["excluded"]["no_retry"]), (1, 1))
        self.assertEqual(s["excluded_pairs"], [{"slug": "t", "fail_run": "20261001T000002-t",
                                                "retry_run": None, "reason": "no_retry"}])

    def test_retry_without_usage_or_research_is_excluded(self):
        _vrun(self.runs, "20261001T000001-u", _RETRY, _VFAIL)
        _vrun(self.runs, "20261001T000002-u", None)
        _vrun(self.runs, "20261001T000001-v", _RETRY, _VFAIL)
        _vrun(self.runs, "20261001T000002-v", {**_RETRY, "research": {"unavailable": "x"}})
        _vrun(self.runs, "20261001T000001-w", _RETRY, _VFAIL)
        _vrun(self.runs, "20261001T000002-w", {**_RETRY, "research": _usage(1, 1, 1, 1, None)})
        _vrun(self.runs, "20261001T000001-x", _RETRY, _VFAIL)
        _vrun(self.runs, "20261001T000002-x", _RETRY)
        s = self._f()
        self.assertEqual(s["n"], 1)
        self.assertEqual(s["pairs"][0]["slug"], "x")
        self.assertEqual(s["excluded"],
                         {"no_retry": 0, "no_usage": 1, "no_research_usage": 2})
        self.assertEqual({e["slug"]: e["reason"] for e in s["excluded_pairs"]},
                         {"u": "no_usage", "v": "no_research_usage", "w": "no_research_usage"})

    def test_plan_unavailable_stays_in_and_counts_research_only(self):
        _vrun(self.runs, "20261001T000001-t", _RETRY, _VFAIL)
        _vrun(self.runs, "20261001T000002-t", {**_RETRY, "plan": {"unavailable": "timeout"}})
        s = self._f()
        self.assertEqual((s["n"], s["plan_missing"]), (1, 1))
        p = s["pairs"][0]
        self.assertIsNone(p["plan_cost"])
        self.assertAlmostEqual(p["rp_cost"], 0.3)
        self.assertAlmostEqual(p["retry_total_cost"], 0.8)
        self.assertEqual(p["rp_io"], 30)

    def test_blocked_on_gives_environment_hint(self):
        _vrun(self.runs, "20261001T000001-b", _RETRY,
              "---\noverall_status: fail\nblocked_on: upstream PR unmerged\n---\n")
        _vrun(self.runs, "20261001T000002-b", _RETRY)
        _vrun(self.runs, "20261001T000001-c", _RETRY, _VFAIL)
        _vrun(self.runs, "20261001T000002-c", _RETRY)
        hints = {p["slug"]: (p["blocked_on"], p["cause_hint"]) for p in self._f()["pairs"]}
        self.assertEqual(hints, {"b": ("upstream PR unmerged", "환경"), "c": (None, None)})

    def test_suffix_slug_never_pairs_with_the_shorter_slug(self):
        _vrun(self.runs, "20261001T000001-api-poc", _RETRY, _VFAIL)
        _vrun(self.runs, "20261001T000002-poc", _RETRY, _VFAIL)
        _vrun(self.runs, "20261001T000003-poc", _RETRY)
        s = self._f()
        self.assertEqual([(p["fail_run"], p["retry_run"]) for p in s["pairs"]],
                         [("20261001T000002-poc", "20261001T000003-poc")])
        self.assertEqual(s["excluded"]["no_retry"], 1)
        self.assertEqual(s["excluded_pairs"][0]["slug"], "api-poc")

    def test_third_attempt_is_two_pairs(self):
        _vrun(self.runs, "20261001T000001-t", _RETRY, _VFAIL)
        _vrun(self.runs, "20261001T000002-t", _RETRY, _VFAIL)
        _vrun(self.runs, "20261001T000003-t", _RETRY, _VPASS)
        s = self._f()
        self.assertEqual([(p["fail_run"][-8:], p["retry_verdict"]) for p in s["pairs"]],
                         [("000001-t", "FAIL"), ("000002-t", "PASS")])
        self.assertAlmostEqual(s["rp_cost_sum"], 1.0)
        self.assertAlmostEqual(s["retry_cost_sum"], 2.0)

    def test_sample_spend_covers_non_pair_runs_and_sufficiency(self):
        _vrun(self.runs, "20261001T000001-t", _RETRY, _VFAIL)
        _vrun(self.runs, "20261001T000002-t", _RETRY)
        _vrun(self.runs, "20261001T000001-z", {"quick": _usage(1, 1, 1, 1, 2.0)})
        _vrun(self.runs, "20261001T000001-old", None)
        s = self._f()
        self.assertAlmostEqual(s["sample_spend"], 4.0)   # 1.0 + 1.0 + quick 2.0
        self.assertAlmostEqual(s["spend_ratio"], 0.125)
        self.assertEqual((s["n"], s["min_n"], s["sufficient"]), (1, 5, False))
        for i in range(2, 6):
            _vrun(self.runs, f"20261001T00000{i}-t", _RETRY, _VFAIL)
        _vrun(self.runs, "20261001T000009-t", _RETRY)
        s = self._f()
        self.assertEqual((s["n"], s["sufficient"]), (5, True))

    def test_empty_runs_do_not_raise(self):
        s = metrics.verify_fail_retry_cost(self.tmp / "nope")
        self.assertEqual((s["run_dirs_n"], s["n"], s["sufficient"]), (0, 0, False))
        self.assertIsNone(s["pooled_ratio"])
        self.assertIsNone(s["spend_ratio"])
        self.assertEqual(s["tokens"]["io"]["pooled_ratio"], None)


class MetricsCliHelpTest(unittest.TestCase):
    """`--help` must be a pure usage print — it must never fall through to
    write(), which would rewrite Todo/Metrics.md as a surprise side effect."""

    def test_help_exits_zero_without_writing_metrics_md(self):
        with mock.patch.object(metrics, "write") as mocked_write:
            with self.assertRaises(SystemExit) as ctx:
                metrics.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        mocked_write.assert_not_called()

    def test_bare_invocation_still_writes(self):
        with mock.patch.object(metrics, "write", return_value="ok") as mocked_write:
            rc = metrics.main([])
        self.assertEqual(rc, 0)
        mocked_write.assert_called_once()


if __name__ == "__main__":
    unittest.main()
