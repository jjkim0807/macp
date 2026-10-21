"""Tests for planner-fed project discovery (discovery.py).

The load-bearing properties: a discovery task is created only when the planner
is exhausted, one at a time, once per repo per interval, and the file it writes
is a task the worker contract accepts; a report the planner handles is retired
and one it ignores expires instead of costing context forever. Paths are
monkeypatched onto a tempdir — the real ones are live state.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from todo_system import task_validate
from todo_system.orchestrator import discovery, stages

URL_A = "https://github.com/example-org/DripCoffee.git"
URL_B = "https://github.com/example-org/KnowScroll.git"
NOW = dt.datetime(2026, 9, 10, 12, 0, 0).timestamp()
DAY = 86400.0


def _task(items: Path, slug: str, *, status="open", repo=None, mode="auto",
          extra: str = "") -> Path:
    fm = [f"status: {status}", f"worker_mode: {mode}", "priority: 50"]
    if repo:
        fm.append(f'repo: "{repo}"')
    if extra:
        fm.append(extra)
    p = items / f"{slug}.md"
    p.write_text("---\ntags:\n  - todo\n" + "\n".join(fm) + f"\n---\n\n# {slug}\n",
                 encoding="utf-8")
    return p


def _report(episodes: Path, stem: str, repo: str, candidates: list[str]) -> Path:
    body = "\n".join(f"- {c}" for c in candidates) or "- 없음"
    p = episodes / f"{stem}.md"
    p.write_text(f"---\ntype: discovery-report\nrepo: {repo}\nhead: abc\n---\n\n"
                 f"# Discovery\n\n## 실행 결과\n\n- tests: pass\n\n"
                 f"## 후보 태스크\n\n{body}\n  - nested elaboration ignored\n\n"
                 f"## 관찰\n\n- x\n", encoding="utf-8")
    return p


class DiscoveryBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.items = self.tmp / "items"
        self.items.mkdir()
        self.episodes = self.tmp / "episodes"
        self.episodes.mkdir()
        self.state = self.tmp / "state.json"
        self.rej = self.tmp / "rejected.jsonl"
        self.planner = self.tmp / "planner_state.json"
        self._orig = (discovery.STATE_PATH, discovery.REJECTIONS_PATH,
                      discovery.PLANNER_STATE_PATH, discovery.ITEMS_DIR,
                      discovery.EPISODES_DIR)
        discovery.STATE_PATH = self.state
        discovery.REJECTIONS_PATH = self.rej
        discovery.PLANNER_STATE_PATH = self.planner
        discovery.ITEMS_DIR = self.items
        discovery.EPISODES_DIR = self.episodes
        self.env = {"MAESTRO_DISCOVERY": "1"}

    def tearDown(self):
        (discovery.STATE_PATH, discovery.REJECTIONS_PATH, discovery.PLANNER_STATE_PATH,
         discovery.ITEMS_DIR, discovery.EPISODES_DIR) = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _verdict(self, v: str) -> None:
        self.planner.write_text(json.dumps({"verdict": v}), encoding="utf-8")


class ActiveReposTest(DiscoveryBase):
    def test_only_alive_tasks_count_and_discovery_tasks_are_flagged(self):
        _task(self.items, "a-1", repo=URL_A)
        _task(self.items, "a-2", repo=URL_A, status="blocked", mode="blocked")
        _task(self.items, "b-old", repo=URL_B, status="done")
        _task(self.items, "b-cancel", repo=URL_B, status="cancelled")
        _task(self.items, "vault-only")                       # no repo
        rm = discovery.active_repos()
        self.assertEqual(set(rm), {URL_A})
        self.assertEqual(rm[URL_A]["tasks"], ["a-1", "a-2"])
        self.assertEqual(rm[URL_A]["name"], "dripcoffee")
        self.assertIsNone(rm[URL_A]["open_discovery"])
        _task(self.items, "dripcoffee-discovery-20260901", mode="discover",
              extra=f'{discovery.REPO_FIELD}: "{URL_A}"')
        self.assertEqual(discovery.active_repos()[URL_A]["open_discovery"],
                         "dripcoffee-discovery-20260901")

    def test_a_repo_named_only_by_its_own_discovery_task_is_not_active(self):
        _task(self.items, "b-discovery-20260901", mode="discover",
              extra=f'{discovery.REPO_FIELD}: "{URL_B}"')
        self.assertEqual(discovery.active_repos(), {})


class PickTest(unittest.TestCase):
    def test_never_explored_first_then_oldest_and_interval_respected(self):
        rm = {URL_A: {"tasks": ["x"], "open_discovery": None},
              URL_B: {"tasks": ["y"], "open_discovery": None}}
        def ts(days_ago):
            return dt.datetime.fromtimestamp(NOW - days_ago * DAY).isoformat()
        # B never explored, A explored 20 days ago -> B first
        st = {"repos": {URL_A: {"last_ts": ts(20)}}}
        self.assertEqual(discovery.pick(rm, st, now=NOW), URL_B)
        # both explored: A 20d ago, B 3d ago -> A (B inside the 14d interval)
        st = {"repos": {URL_A: {"last_ts": ts(20)}, URL_B: {"last_ts": ts(3)}}}
        self.assertEqual(discovery.pick(rm, st, now=NOW), URL_A)
        # both fresh -> nothing
        st = {"repos": {URL_A: {"last_ts": ts(2)}, URL_B: {"last_ts": ts(3)}}}
        self.assertIsNone(discovery.pick(rm, st, now=NOW))
        # a shorter interval re-opens them
        self.assertEqual(discovery.pick(rm, st, now=NOW, interval_days=1), URL_B)

    def test_one_open_discovery_blocks_every_repo(self):
        rm = {URL_A: {"tasks": ["x"], "open_discovery": "a-discovery-1"},
              URL_B: {"tasks": ["y"], "open_discovery": None}}
        self.assertIsNone(discovery.pick(rm, {"repos": {}}, now=NOW))


class CreateTaskTest(DiscoveryBase):
    def test_written_task_passes_the_worker_contract(self):
        _task(self.items, "a-1", repo=URL_A)
        path = discovery.create_task(URL_A, discovery.active_repos(),
                                     today=dt.date(2026, 9, 10), now=NOW)
        self.assertIsNotNone(path)
        self.assertEqual(path.name, "dripcoffee-discovery-20260910.md")
        issues = task_validate.validate_file(path)
        errors = [i for i in issues if i.level == task_validate.ERROR]
        self.assertEqual(errors, [], [i.format("x") for i in issues])
        warns = [i for i in issues if i.level == task_validate.WARN]
        self.assertEqual(warns, [], [i.format("x") for i in issues])
        import yaml as _yaml
        fm = _yaml.safe_load(path.read_text(encoding="utf-8").split("---")[1])
        for field in ("outcome", "next_action", "if_then", "estimate_p50", "estimate_p90"):
            self.assertTrue(fm.get(field), f"audit requires {field} to be non-empty")
        text = path.read_text(encoding="utf-8")
        self.assertIn("worker_mode: discover", text)
        self.assertIn(f'{discovery.REPO_FIELD}: "{URL_A}"', text)
        self.assertNotIn("\nrepo:", text, "repo: would route it to the external pipeline")
        self.assertIn("[[a-1]]", text)
        # the repo is stamped so the cadence gate holds
        st = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(st["repos"][URL_A]["last_task"], "dripcoffee-discovery-20260910")
        # same day again -> no second file
        self.assertIsNone(discovery.create_task(URL_A, today=dt.date(2026, 9, 10), now=NOW))

    def test_it_is_dispatchable_and_routes_to_the_discover_pipeline(self):
        from todo_system.orchestrator import tasks
        from todo_system.orchestrator.runner import StagedExecutor
        self.assertIn("discover", tasks.DISPATCHABLE_MODES)
        ex = StagedExecutor()
        self.assertEqual(ex._pipeline_for({"worker_mode": "discover"}), stages.STAGES_DISCOVER)
        self.assertEqual(stages.STAGES_DISCOVER, ("discover",))
        for s in stages.STAGES_DISCOVER:
            self.assertIn(s, stages.STAGE_TOOLS)
            self.assertIn(s, stages.STAGE_MAX_TURNS)
            self.assertIn(s, stages.STAGE_ARTIFACT)
            stages._safe_tools(s)            # raises on a forbidden tool
            self.assertTrue((stages.COMMANDS_DIR / f"worker-{s}.md").exists())
        self.assertNotIn("Edit", stages.STAGE_TOOLS["discover"].split(),
                         "discover is read-only towards the tree by construction")
        # macOS ships no `timeout`; the prompt must teach the portable bound and
        # the whitelist must let it run.
        prompt = (stages.COMMANDS_DIR / "worker-discover.md").read_text(encoding="utf-8")
        self.assertIn("perl -e 'alarm shift; exec @ARGV'", prompt)
        self.assertIn("Bash(perl:*)", stages.STAGE_TOOLS["discover"])


class MaybeCreateTest(DiscoveryBase):
    def setUp(self):
        super().setUp()
        _task(self.items, "a-1", repo=URL_A)

    def test_only_when_the_planner_is_exhausted(self):
        self._verdict("tasks_created")
        path, reason = discovery.maybe_create(now=NOW, env=self.env)
        self.assertIsNone(path)
        self.assertIn("not exhausted", reason)
        self._verdict("exhausted")
        path, reason = discovery.maybe_create(now=NOW, env=self.env)
        self.assertIsNotNone(path)
        self.assertIn("created", reason)
        # the open discovery task now blocks a second one
        path2, reason2 = discovery.maybe_create(now=NOW + DAY, env=self.env)
        self.assertIsNone(path2)
        self.assertIn("already open", reason2)

    def test_force_overrides_the_verdict_but_not_the_cadence(self):
        self._verdict("tasks_created")
        path, _ = discovery.maybe_create(force=True, now=NOW, env=self.env)
        self.assertIsNotNone(path)
        path.unlink()                                   # task finished & gone
        path2, reason = discovery.maybe_create(force=True, now=NOW + 2 * DAY, env=self.env)
        self.assertIsNone(path2)
        self.assertIn("no repo due", reason)
        path3, _ = discovery.maybe_create(force=True, now=NOW + 15 * DAY, env=self.env)
        self.assertIsNotNone(path3)

    def test_kill_switch_and_interval_env(self):
        self._verdict("exhausted")
        self.assertIsNone(discovery.maybe_create(now=NOW, env={"MAESTRO_DISCOVERY": "0"})[0])
        self.assertEqual(discovery.interval_days({"MAESTRO_DISCOVERY_INTERVAL_DAYS": "3"}), 3.0)
        self.assertEqual(discovery.interval_days({"MAESTRO_DISCOVERY_INTERVAL_DAYS": "x"}),
                         discovery.DEFAULT_INTERVAL_DAYS)

    def test_no_repos_is_a_clean_skip(self):
        for p in self.items.glob("*.md"):
            p.unlink()
        self._verdict("exhausted")
        self.assertIn("no active", discovery.maybe_create(now=NOW, env=self.env)[1])

    def test_candidate_is_the_same_gate_without_the_write(self):
        """worker.check_work_starvation asks this to learn whether discovery
        could still find work; asking must never itself create a task."""
        self._verdict("tasks_created")
        url, reason = discovery.candidate(now=NOW, env=self.env)
        self.assertIsNone(url)
        self.assertIn("not exhausted", reason)
        self._verdict("exhausted")
        url, reason = discovery.candidate(now=NOW, env=self.env)
        self.assertEqual(url, URL_A)
        self.assertIn("due", reason)
        self.assertEqual(sorted(p.name for p in self.items.glob("*.md")), ["a-1.md"],
                         "candidate() wrote a task")
        self.assertFalse(self.state.exists(), "candidate() touched discovery state")
        # and once the real create happens, candidate reports the same block
        self.assertIsNotNone(discovery.maybe_create(now=NOW, env=self.env)[0])
        url, reason = discovery.candidate(now=NOW + DAY, env=self.env)
        self.assertIsNone(url)
        self.assertIn("already open", reason)


class ReportsTest(DiscoveryBase):
    def test_context_is_empty_without_reports_and_lists_candidates_with_them(self):
        self.assertEqual(discovery.render_context([]), "")
        self.assertEqual(discovery.render_context(discovery.open_reports(discovery._load_state())), "")
        _report(self.episodes, "2026-09-10-1200-discovery-dripcoffee", URL_A,
                ["**A** — 근거: x", "**B** — 근거: y"])
        ctx = discovery.render_context(discovery.open_reports(discovery._load_state()))
        self.assertIn("[2026-09-10-1200-discovery-dripcoffee]", ctx)
        self.assertIn("후보 2개", ctx)
        self.assertIn("  - **A** — 근거: x", ctx)
        self.assertNotIn("nested elaboration", ctx)
        self.assertIn("from_discovery", ctx)

    def test_created_task_retires_the_report(self):
        _report(self.episodes, "r1-discovery-a", URL_A, ["c"])
        _task(self.items, "dripcoffee-fix-1", repo=URL_A, extra="from_discovery: r1-discovery-a")
        self.assertEqual(discovery.reconcile(), {"r1-discovery-a": "created: dripcoffee-fix-1"})
        self.assertEqual(discovery.open_reports(discovery._load_state()), [])
        self.assertEqual(discovery.reconcile(), {})           # idempotent

    def test_rejection_retires_the_report(self):
        _report(self.episodes, "r2-discovery-a", URL_A, ["c"])
        self.rej.write_text(json.dumps({"id": "r2-discovery-a", "reason": "project retiring"}) + "\n",
                            encoding="utf-8")
        self.assertEqual(discovery.reconcile(), {"r2-discovery-a": "rejected: project retiring"})

    def test_an_ignored_report_expires_after_max_injections(self):
        _report(self.episodes, "r3-discovery-a", URL_A, ["c"])
        for i in range(discovery.MAX_INJECTIONS - 1):
            self.assertEqual(discovery.reconcile(), {}, f"tick {i}")
            self.assertEqual(len(discovery.open_reports(discovery._load_state())), 1)
        out = discovery.reconcile()
        self.assertTrue(out["r3-discovery-a"].startswith("expired"))
        self.assertEqual(discovery.open_reports(discovery._load_state()), [])

    def test_a_report_not_shown_this_tick_is_not_charged(self):
        _report(self.episodes, "r4-discovery-a", URL_A, ["c"])
        for _ in range(discovery.MAX_INJECTIONS + 1):
            discovery.reconcile(injected=[])
        self.assertEqual(len(discovery.open_reports(discovery._load_state())), 1)

    def test_parse_candidates_skips_the_empty_marker(self):
        _report(self.episodes, "r5-discovery-a", URL_A, [])
        text = (self.episodes / "r5-discovery-a.md").read_text(encoding="utf-8")
        self.assertEqual(discovery.parse_candidates(text), [])


class ParseCandidatesAliasTest(DiscoveryBase):
    def test_korean_candidates_pinned(self):
        p = _report(self.episodes, "k1-discovery-a", URL_A, ["후보 A", "후보 B"])
        self.assertEqual(discovery.parse_candidates(p.read_text(encoding="utf-8")),
                         ["후보 A", "후보 B"])
        e = _report(self.episodes, "k2-discovery-a", URL_A, [])
        self.assertEqual(discovery.parse_candidates(e.read_text(encoding="utf-8")), [])
        self.assertEqual(discovery.CANDIDATE_SECTION, "후보 태스크")


class StateAtomicWriteTest(DiscoveryBase):
    def test_save_state_round_trips_valid_json(self):
        ok = discovery._save_state({"repos": {"a": 1}, "reports": {}}, self.state)
        self.assertTrue(ok)
        self.assertEqual(discovery._load_state(self.state),
                          {"repos": {"a": 1}, "reports": {}})

    def test_save_state_never_leaves_a_tmp_file_behind(self):
        discovery._save_state({"repos": {}, "reports": {}}, self.state)
        leftovers = list(self.state.parent.glob(f"{self.state.name}.tmp.*"))
        self.assertEqual(leftovers, [])

    def test_save_state_goes_through_temp_then_rename(self):
        seen_tmp_names = []
        real_replace = discovery.os.replace

        def spy_replace(src, dst):
            seen_tmp_names.append(Path(src).name)
            return real_replace(src, dst)

        discovery.os.replace = spy_replace
        try:
            discovery._save_state({"repos": {}, "reports": {}}, self.state)
        finally:
            discovery.os.replace = real_replace
        self.assertEqual(len(seen_tmp_names), 1)
        self.assertTrue(seen_tmp_names[0].startswith(f"{self.state.name}.tmp."))

    def test_torn_old_file_still_falls_back_on_read(self):
        self.state.write_text('{"repos": {"a": 1}, ', encoding="utf-8")
        self.assertEqual(discovery._load_state(self.state),
                          {"repos": {}, "reports": {}})


class CliTest(DiscoveryBase):
    def test_subcommands(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        _task(self.items, "a-1", repo=URL_A)
        self._verdict("exhausted")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(discovery.main(["emit-context"]), 0)
        self.assertEqual(buf.getvalue(), "")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(discovery.main(["status"]), 0)
        self.assertIn("dripcoffee", buf.getvalue())
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(discovery.main(["reconcile"]), 0)
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(discovery.main(["bogus"]), 2)
        self.assertIn("usage", err.getvalue())


class ConsumedTest(DiscoveryBase):
    """Every task already created from a report — open AND done, resolved report
    or not — so the planner stops re-creating a finished candidate under a new
    slug (10-18: a done task's candidate came back as a fresh task)."""

    STEM = "2026-09-10-1200-discovery-dripcoffee"

    def test_tasks_by_discovery_lists_every_task_with_status_and_title(self):
        _task(self.items, "b-fix", repo=URL_A, extra=f"from_discovery: {self.STEM}")
        _task(self.items, "a-fix", status="done", repo=URL_A,
              extra=f'from_discovery: "{self.STEM}"')
        _task(self.items, "other", extra="from_discovery: 2026-09-01-1200-discovery-x")
        _task(self.items, "nulled", extra="from_discovery: null")
        _task(self.items, "plain")
        got = discovery.tasks_by_discovery()
        self.assertEqual(set(got), {self.STEM, "2026-09-01-1200-discovery-x"})
        self.assertEqual(got[self.STEM], [
            {"slug": "a-fix", "status": "done", "title": "a-fix"},
            {"slug": "b-fix", "status": "open", "title": "b-fix"},
        ])

    def test_reconcile_still_names_the_first_slug(self):
        _task(self.items, "b-fix", repo=URL_A, extra=f"from_discovery: {self.STEM}")
        _task(self.items, "a-fix", status="done", repo=URL_A,
              extra=f"from_discovery: {self.STEM}")
        _report(self.episodes, self.STEM, URL_A, ["c"])
        self.assertEqual(discovery.reconcile(), {self.STEM: "created: a-fix"})
        rec = json.loads(self.state.read_text(encoding="utf-8"))["reports"][self.STEM]
        self.assertEqual(set(rec), {"injections", "resolved"})

    def test_consumed_is_empty_without_reports_or_tasks(self):
        self.assertEqual(discovery.render_consumed([], now=NOW), "")
        _report(self.episodes, self.STEM, URL_A, ["c"])
        _task(self.items, "unrelated", repo=URL_A)
        self.assertEqual(discovery.render_consumed(discovery.reports(), now=NOW), "")

    def test_report_with_only_done_tasks_is_listed(self):
        stem = "2026-09-08-1200-discovery-dripcoffee"
        _report(self.episodes, stem, URL_A, ["**X** — 근거: x"])
        _task(self.items, "x-fix", status="done", repo=URL_A, extra=f"from_discovery: {stem}")
        discovery.reconcile()
        self.assertEqual(discovery.open_reports(discovery._load_state()), [],
                         "the report is resolved, so the unconsumed block no longer shows it")
        ctx = discovery.render_consumed(discovery.reports(), now=NOW)
        self.assertIn(f"[{stem}] repo={URL_A} · 생성 1개", ctx)
        self.assertIn("  - x-fix (done) x-fix", ctx)
        self.assertIn("open 이든 done 이든", ctx)
        self.assertIn("from_discovery", ctx)
        self.assertIn("제목", ctx)
        self.assertIn("근거", ctx)

    def test_every_slug_per_report_is_listed(self):
        _report(self.episodes, self.STEM, URL_A, ["a", "b", "c"])
        for slug, status in (("t-one", "open"), ("t-two", "done"), ("t-three", "done")):
            _task(self.items, slug, status=status, repo=URL_A,
                  extra=f"from_discovery: {self.STEM}")
        ctx = discovery.render_consumed(discovery.reports(), now=NOW)
        self.assertIn("생성 3개", ctx)
        for line in ("  - t-one (open) t-one", "  - t-two (done) t-two",
                     "  - t-three (done) t-three"):
            self.assertIn(line, ctx)

    def test_reports_outside_window_are_excluded(self):
        stems = {
            "old": "2026-08-20-1200-discovery-dripcoffee",   # 21 days before NOW
            "edge": "2026-08-27-1200-discovery-dripcoffee",  # exactly 14 days
            "gen": "r9-discovery-a",                         # dated by generated:
            "undated": "r8-discovery-a",                     # no date at all
        }
        for key, stem in stems.items():
            _report(self.episodes, stem, URL_A, ["c"])
            _task(self.items, f"{key}-fix", repo=URL_A, extra=f"from_discovery: {stem}")
        gen = self.episodes / f"{stems['gen']}.md"
        gen.write_text(gen.read_text(encoding="utf-8").replace(
            "head: abc", "head: abc\ngenerated: 2026-09-09T10:00:00+09:00"), encoding="utf-8")
        ctx = discovery.render_consumed(discovery.reports(), now=NOW)
        self.assertNotIn(stems["old"], ctx)
        self.assertIn(f"[{stems['edge']}]", ctx)
        self.assertIn(f"[{stems['gen']}]", ctx)
        self.assertNotIn(stems["undated"], ctx)
        self.assertIn(f"최근 {discovery.CONSUMED_WINDOW_DAYS}일", ctx)

    def test_emit_consumed_cli(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(discovery.main(["emit-consumed"]), 0)
        self.assertEqual(buf.getvalue(), "")
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(discovery.main(["bogus"]), 2)
        self.assertIn("emit-consumed", err.getvalue())


if __name__ == "__main__":
    unittest.main()
