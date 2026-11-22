"""Stage command budget (2026-10-18): one Bash call may run an hour, a stage two
(three for heavy), the stale gates stay clear of both, and the Gradle cap is a
real profile field. Pins the relationships, not just the numbers.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system import worker
from todo_system.macp import config
from todo_system.orchestrator import stages
from todo_system.orchestrator.runner import StagedExecutor


class BashBudgetTest(unittest.TestCase):
    def test_stage_env_carries_bash_timeouts_as_ms_strings(self):
        env = stages.stage_env({})
        self.assertEqual(env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "1")
        self.assertEqual(int(env["BASH_MAX_TIMEOUT_MS"]), 100 * 60 * 1000)
        self.assertEqual(int(env["BASH_DEFAULT_TIMEOUT_MS"]), 20 * 60 * 1000)
        self.assertEqual(stages.stage_env({"KEEP": "x"})["KEEP"], "x")

    def test_one_command_fits_inside_every_non_light_stage(self):
        # A single build may take the whole cap; the stage must still have room
        # to write run.log / verify.md after it. light keeps its historical hour
        # on purpose (a one-liner needing more is mis-tiered, not slow) — its own
        # stage timeout ends a longer command, so it is not held to the cap.
        cap_s = stages.BASH_MAX_TIMEOUT_MS // 1000
        for tier in ("standard", "heavy"):
            self.assertGreater(stages.STAGE_TIMEOUTS[tier], cap_s, tier)
        self.assertLess(stages.BASH_DEFAULT_TIMEOUT_MS, stages.BASH_MAX_TIMEOUT_MS)

    def test_tier_ladder(self):
        self.assertEqual(stages.STAGE_TIMEOUTS,
                         {"heavy": 10800, "standard": 7200, "light": 3600})
        self.assertEqual(stages.MAX_STAGE_TIMEOUT, 10800)
        self.assertLessEqual(StagedExecutor.stage_timeout, min(stages.STAGE_TIMEOUTS.values()))


class StaleInvariantTest(unittest.TestCase):
    def test_heartbeat_cutoff_clears_longest_stage_plus_reap_margin(self):
        # worker.py mirrors the constant instead of importing stages (cycle), so
        # the relationship is pinned here: raising a stage timeout without the
        # heartbeat cutoff would make the idle gate kickstart a runner mid-stage.
        self.assertLess(stages.MAX_STAGE_TIMEOUT + stages.REAP_MARGIN,
                        worker.RUNNER_HEARTBEAT_STALE_S)
        self.assertEqual(worker.RUNNER_HEARTBEAT_STALE_S, 270 * 60)

    def test_stage_timeout_for_follows_the_ladder(self):
        self.assertEqual(stages.stage_timeout_for({"model_tier": "standard"}), 7200)
        self.assertEqual(stages.stage_timeout_for({"model_tier": "heavy"}), 10800)
        self.assertEqual(stages.stage_timeout_for({"model_tier": "light"}), 3600)
        self.assertEqual(stages.stage_timeout_for({"stage_timeout_min": "15"}), 900)


class GradleCapProfileFieldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _roundtrip(self, prof: config.Profile):
        with mock.patch.object(config, "PROFILES", self.tmp):
            config.write_profile(prof)
            text = (self.tmp / prof.name / "config.toml").read_text(encoding="utf-8")
            loaded = config.load_profile(prof.name)
        return loaded, text

    def test_field_round_trips_through_toml(self):
        loaded, text = self._roundtrip(config.Profile(name="p", max_parallel_gradle_builds=2))
        self.assertEqual(loaded.max_parallel_gradle_builds, 2)
        self.assertIn("max_parallel_gradle_builds = 2", text)

    def test_unset_field_is_not_written_and_loads_as_zero(self):
        loaded, text = self._roundtrip(config.Profile(name="q"))
        self.assertEqual(loaded.max_parallel_gradle_builds, 0)
        self.assertNotIn("max_parallel_gradle_builds", text)


if __name__ == "__main__":
    unittest.main()
