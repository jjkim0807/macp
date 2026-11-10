"""Tests for worker model resolution — notably that an exhausted quota trips
the fallback even though the CLI exits 0."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import model_select as ms


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr)


class ProbeTest(unittest.TestCase):
    def test_working_model_probes_true(self):
        with mock.patch.object(ms.subprocess, "run",
                               return_value=_completed("What would you like me to help with?")):
            self.assertTrue(ms._probe("claude-fable-5"))

    def test_out_of_credits_probes_false_despite_exit_zero(self):
        # Regression (2026-07-31): the CLI prints this and still exits 0, so an
        # exit-code-only probe kept the exhausted model selected and the
        # fallback was never reached.
        msg = ("You're out of usage credits. Run /usage-credits to keep using "
               "Fable 5 or /model to switch models.")
        with mock.patch.object(ms.subprocess, "run", return_value=_completed(msg)):
            self.assertFalse(ms._probe("claude-fable-5"))

    def test_quota_notice_on_stderr_also_probes_false(self):
        with mock.patch.object(ms.subprocess, "run",
                               return_value=_completed("", stderr="usage limit reached")):
            self.assertFalse(ms._probe("claude-fable-5"))

    def test_nonzero_exit_probes_false(self):
        with mock.patch.object(ms.subprocess, "run", return_value=_completed("", returncode=1)):
            self.assertFalse(ms._probe("claude-fable-5"))

    def test_probe_never_raises(self):
        with mock.patch.object(ms.subprocess, "run", side_effect=OSError("boom")):
            self.assertFalse(ms._probe("claude-fable-5"))


class ResolveTest(unittest.TestCase):
    def setUp(self):
        # resolve_model() writes its choice even when use_cache=False — that flag
        # skips the read, not the write. Without this redirect the suite rewrote
        # the LIVE Worker/_state/model_pref.json on every run, parking whichever
        # id the last test happened to mock and refreshing the 24h TTL. That
        # silently undoes a real invalidation, which is the whole point of
        # invalidate_cache() below.
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.object(ms, "_CACHE", self.tmp / "model_pref.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_available_preferred_wins(self):
        with mock.patch.object(ms, "_probe", return_value=True):
            self.assertEqual(ms.resolve_model(use_cache=False), ms.PREFERRED)

    def test_exhausted_preferred_walks_to_first_working_fallback(self):
        # Fable out of credits, first Opus fallback fine -> it, without reaching the floor
        with mock.patch.object(ms, "_probe", side_effect=lambda m, e="max": m != ms.PREFERRED):
            self.assertEqual(ms.resolve_model(use_cache=False), ms.FALLBACKS[0])

    def test_everything_exhausted_returns_the_floor_unprobed(self):
        probed: list[str] = []

        def _probe(model, effort="max"):
            probed.append(model)
            return False

        with mock.patch.object(ms, "_probe", side_effect=_probe):
            self.assertEqual(ms.resolve_model(use_cache=False), ms.FALLBACKS[-1])
        # the floor is never probed — there is nothing better to fall back to
        self.assertNotIn(ms.FALLBACKS[-1], probed)

    def test_env_override_skips_probing(self):
        with mock.patch.dict(ms.os.environ, {"WORKER_MODEL": "claude-sonnet-5"}), \
             mock.patch.object(ms, "_probe", side_effect=AssertionError("must not probe")):
            self.assertEqual(ms.resolve_model(), "claude-sonnet-5")

    def test_probe_model_is_a_thin_public_alias(self):
        # scripts/claude_model_autoswitch.py calls this instead of reimplementing
        # the rc=0-plus-quota-notice rule; a copy would drift silently.
        with mock.patch.object(ms, "_probe", return_value=True) as probe:
            self.assertTrue(ms.probe_model("claude-opus-5"))
        probe.assert_called_once_with("claude-opus-5")


class TierLadderTest(unittest.TestCase):
    """Each tier walks its own ladder and keeps its own cache entry."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = self.tmp / "model_pref.json"
        patcher = mock.patch.object(ms, "_CACHE", self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(ms.os.environ, {"WORKER_MODEL": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_default_tier_is_the_heavy_ladder(self):
        # planner_tick.sh and every pre-tier caller call resolve_model() bare
        # and mean "Fable, else the fallbacks" — that contract is unchanged.
        self.assertEqual(ms.LADDERS["heavy"], (ms.PREFERRED, *ms.FALLBACKS))
        with mock.patch.object(ms, "_probe", return_value=True):
            self.assertEqual(ms.resolve_model(use_cache=False), ms.PREFERRED)

    def test_light_tier_starts_at_sonnet_not_fable(self):
        probed: list[str] = []
        with mock.patch.object(ms, "_probe", side_effect=lambda m, e="max": probed.append((m, e)) or True):
            self.assertEqual(ms.resolve_model(use_cache=False, tier="light"), ms.LADDERS["light"][0])
        # probed at the tier's own effort, not the heavy "max"
        self.assertEqual(probed, [(ms.LADDERS["light"][0], ms.TIER_EFFORT["light"])])
        self.assertNotEqual(ms.LADDERS["light"][0], ms.PREFERRED)

    def test_standard_tier_never_touches_fable(self):
        self.assertNotIn(ms.PREFERRED, ms.LADDERS["standard"])
        with mock.patch.object(ms, "_probe", return_value=True):
            self.assertEqual(ms.resolve_model(use_cache=False, tier="standard"), ms.LADDERS["standard"][0])

    def test_exhausted_light_rung_walks_down_its_own_ladder(self):
        with mock.patch.object(ms, "_probe", side_effect=lambda m, e="max": m != "claude-sonnet-5"):
            self.assertEqual(ms.resolve_model(use_cache=False, tier="light"), ms.LADDERS["light"][1])

    def test_tier_caches_do_not_leak_into_each_other(self):
        with mock.patch.object(ms, "_probe", return_value=True):
            ms.resolve_model(tier="light")
        # the light choice is cached; a heavy resolve must still probe its own ladder
        with mock.patch.object(ms, "_probe", return_value=True) as probe:
            self.assertEqual(ms.resolve_model(tier="heavy"), ms.PREFERRED)
            probe.assert_called_once()
        with mock.patch.object(ms, "_probe", side_effect=AssertionError("cached")):
            self.assertEqual(ms.resolve_model(tier="light"), "claude-sonnet-5")
            self.assertEqual(ms.resolve_model(tier="heavy"), ms.PREFERRED)

    def test_legacy_flat_cache_is_read_as_the_heavy_tier(self):
        # The unblock playbook writes {"model", "ts"} by hand; that must keep
        # steering the heavy ladder, and only the heavy ladder.
        self.cache.write_text(json.dumps({"model": "claude-opus-4-8", "ts": time.time()}))
        with mock.patch.object(ms, "_probe", side_effect=AssertionError("cached")):
            self.assertEqual(ms.resolve_model(tier="heavy"), "claude-opus-4-8")
        with mock.patch.object(ms, "_probe", return_value=True) as probe:
            self.assertEqual(ms.resolve_model(tier="light"), "claude-sonnet-5")
            probe.assert_called_once()

    def test_heavy_write_mirrors_legacy_keys_and_keeps_siblings(self):
        with mock.patch.object(ms, "_probe", return_value=True):
            ms.resolve_model(tier="light")
            ms.resolve_model(tier="heavy")
        c = json.loads(self.cache.read_text())
        self.assertEqual(c["model"], ms.PREFERRED)            # legacy mirror
        self.assertEqual(c["tiers"]["heavy"]["model"], ms.PREFERRED)
        self.assertEqual(c["tiers"]["light"]["model"], "claude-sonnet-5")

    def test_unknown_tier_falls_back_to_heavy(self):
        with mock.patch.object(ms, "_probe", return_value=True):
            self.assertEqual(ms.resolve_model(use_cache=False, tier="ultra"), ms.PREFERRED)

    def test_env_override_applies_to_every_tier(self):
        with mock.patch.dict(ms.os.environ, {"WORKER_MODEL": "claude-haiku-4-5-20251001"}), \
             mock.patch.object(ms, "_probe", side_effect=AssertionError("must not probe")):
            for tier in ms.TIERS:
                self.assertEqual(ms.resolve_model(tier=tier), "claude-haiku-4-5-20251001")


class EstimateMinutesTest(unittest.TestCase):
    def test_vault_forms(self):
        for text, want in (("30m", 30), ("1h30m", 90), ("2h", 120), ("8h", 480),
                           ("1d", 1440), ("1.5h", 90), ("45 m", 45), ("1h 30m", 90)):
            self.assertEqual(ms.estimate_minutes(text), want, text)

    def test_unparsable_is_none_not_a_guess(self):
        for text in (None, "", "-", "90", "soon", "2 weeks", "1h30"):
            self.assertIsNone(ms.estimate_minutes(text), text)


class ClassifyTierTest(unittest.TestCase):
    def test_explicit_model_tier_wins_over_everything(self):
        p = {"model_tier": "Heavy", "tags": ["trivial"], "worker_mode": "quick",
             "estimate_p90": "10m"}
        self.assertEqual(ms.classify_tier(p), ("heavy", "model_tier: heavy"))

    def test_unrecognised_explicit_value_is_ignored_not_honoured(self):
        tier, reason = ms.classify_tier({"model_tier": "ultra", "estimate_p90": "8h"})
        self.assertEqual(tier, "heavy")
        self.assertIn("estimate_p90", reason)

    def test_tags_decide_before_worker_mode_and_estimate(self):
        self.assertEqual(ms.classify_tier({"tags": ["todo", "hard"], "estimate_p90": "10m"})[0], "heavy")
        self.assertEqual(ms.classify_tier({"tags": ["Trivial"], "estimate_p90": "8h"})[0], "light")

    def test_worker_mode_quick_is_light_and_delegate_is_standard(self):
        self.assertEqual(ms.classify_tier({"worker_mode": "quick", "estimate_p90": "8h"})[0], "light")
        self.assertEqual(ms.classify_tier({"worker_mode": "delegate", "estimate_p90": "8h"})[0], "standard")

    def test_estimate_thresholds(self):
        self.assertEqual(ms.classify_tier({"estimate_p90": "45m"})[0], "light")
        self.assertEqual(ms.classify_tier({"estimate_p90": "46m"})[0], "standard")
        self.assertEqual(ms.classify_tier({"estimate_p90": "90m"})[0], "standard")   # template default
        self.assertEqual(ms.classify_tier({"estimate_p90": "3h"})[0], "standard")
        self.assertEqual(ms.classify_tier({"estimate_p90": "4h"})[0], "heavy")

    def test_no_signal_is_the_default_tier(self):
        self.assertEqual(ms.classify_tier({}), (ms.DEFAULT_TIER, "default"))
        self.assertEqual(ms.classify_tier(None), (ms.DEFAULT_TIER, "default"))
        self.assertEqual(ms.classify_tier({"estimate_p90": "-"}), (ms.DEFAULT_TIER, "default"))

    def test_every_answer_is_a_real_tier(self):
        for p in ({}, {"tags": ["x"]}, {"worker_mode": "auto"}, {"estimate_p90": "junk"},
                  {"model_tier": None}, {"tags": None}):
            self.assertIn(ms.classify_tier(p)[0], ms.LADDERS)


class ShiftTierTest(unittest.TestCase):
    def test_moves_toward_light_and_clamps(self):
        self.assertEqual(ms.shift_tier("heavy", 1), "standard")
        self.assertEqual(ms.shift_tier("standard", 1), "light")
        self.assertEqual(ms.shift_tier("light", 1), "light")
        self.assertEqual(ms.shift_tier("heavy", -1), "heavy")
        self.assertEqual(ms.shift_tier("light", 0), "light")

    def test_unknown_tier_is_treated_as_default(self):
        self.assertEqual(ms.shift_tier("bogus", 0), ms.DEFAULT_TIER)


class ModelUnavailableTest(unittest.TestCase):
    def test_every_marker_matches_case_insensitively(self):
        for mark in ms._UNAVAILABLE_MARKERS:
            self.assertTrue(ms.model_unavailable(f"Error: {mark.upper()} · resets 6pm"), mark)

    def test_ordinary_stage_failures_do_not_match(self):
        # A stage can fail for a hundred reasons that say nothing about the model.
        for s in ("2 tests failed", "AssertionError: expected 3, got 4",
                  "Bash(chmod:*) requires approval", "verify overall=fail: TS18046",
                  "", None):
            self.assertFalse(ms.model_unavailable(s), s)

    def test_detects_fable5_rebrand_message(self):
        # 2026-09-01 — see mulkko-5-ladder-editor incident: this exact message
        # matched none of the pre-existing markers.
        self.assertTrue(ms.model_unavailable(
            "You've reached your Fable 5 limit. Run /usage-credits to "
            "continue or switch models with /model."))

    def test_capacity_only_signals_are_excluded(self):
        # These make stages.is_capacity_limit() True, but picking a different
        # model fixes none of them — lost auth is account-wide and overload is
        # transient, so re-probing would burn three calls against the same wall.
        for s in ("Not logged in · Please run /login", "Error: overloaded_error",
                  "You've hit your weekly limit · resets 8am"):
            self.assertFalse(ms.model_unavailable(s), s)


class InvalidateCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = self.tmp / "model_pref.json"
        patcher = mock.patch.object(ms, "_CACHE", self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fresh_cache(self, model="claude-opus-4-8"):
        self.cache.write_text(json.dumps({"model": model, "ts": time.time()}),
                              encoding="utf-8")

    def test_removes_an_existing_cache(self):
        self._fresh_cache()
        self.assertTrue(ms.invalidate_cache())
        self.assertFalse(self.cache.exists())

    def test_absent_cache_reports_false_without_raising(self):
        self.assertFalse(ms.invalidate_cache())

    def test_never_raises_even_on_an_unusable_path(self):
        with mock.patch.object(ms, "_CACHE", self.tmp / "no" / "such" / "dir" / "c.json"):
            self.assertFalse(ms.invalidate_cache())

    def test_unavailable_output_makes_the_next_resolve_reprobe(self):
        """The 24h TTL is bypassed, not waited out (acceptance 1)."""
        self._fresh_cache("claude-opus-4-8")
        with mock.patch.dict(ms.os.environ, {"WORKER_MODEL": ""}), \
             mock.patch.object(ms, "_probe", return_value=True) as probe:
            # a fresh cache is served without probing at all
            self.assertEqual(ms.resolve_model(), "claude-opus-4-8")
            self.assertEqual(probe.call_count, 0)

            self.assertTrue(ms.invalidate_cache_if_unavailable(
                "You're out of usage credits. Run /usage-credits to keep using Fable 5."))

            # still inside the same TTL window, but the cache is gone, so the
            # next dispatch actually re-probes instead of re-serving the dead id
            self.assertEqual(ms.resolve_model(), ms.PREFERRED)
            self.assertEqual(probe.call_count, 1)

    def test_ordinary_failure_leaves_the_cache_byte_identical(self):
        """Only a model-unavailable signal may touch the cache (acceptance 2)."""
        self._fresh_cache("claude-opus-4-8")
        before = self.cache.read_bytes()
        self.assertFalse(ms.invalidate_cache_if_unavailable(
            "2 tests failed\nAssertionError: expected 3, got 4"))
        self.assertEqual(self.cache.read_bytes(), before)


class FloorTTLTest(unittest.TestCase):
    """A first-rung cache keeps 24h; any fallback/floor choice expires at 1h so
    the next stage re-probes from the top (acceptance 1, injected clock)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = self.tmp / "model_pref.json"
        patcher = mock.patch.object(ms, "_CACHE", self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(ms.os.environ, {"WORKER_MODEL": ""})
        env.start()
        self.addCleanup(env.stop)

    def _write(self, tier, model, ts):
        self.cache.write_text(json.dumps({"tiers": {tier: {"model": model, "ts": ts}}}))

    def test_first_rung_cache_kept_for_24h_then_reprobed(self):
        base = 1_000_000.0
        first = ms.LADDERS["standard"][0]
        self._write("standard", first, base)
        # served without probing right up to 24h
        with mock.patch.object(ms, "_probe", side_effect=AssertionError("must not probe")):
            self.assertEqual(
                ms.resolve_model(tier="standard", now=base + ms._TTL - 1), first)
        # just past 24h it re-probes from the top of the ladder
        with mock.patch.object(ms, "_probe", return_value=True) as probe:
            self.assertEqual(
                ms.resolve_model(tier="standard", now=base + ms._TTL + 1), first)
            probe.assert_called_once()

    def test_non_first_choice_expires_at_1h_and_reprobes_from_top(self):
        base = 1_000_000.0
        floor = ms.LADDERS["standard"][-1]
        first = ms.LADDERS["standard"][0]
        self._write("standard", floor, base)
        # still served inside the 1h floor window, no probe
        with mock.patch.object(ms, "_probe", side_effect=AssertionError("must not probe")):
            self.assertEqual(
                ms.resolve_model(tier="standard", now=base + ms._FLOOR_TTL - 1), floor)
        # at ts + 3601s the floor cache is stale -> re-probe returns the top rung
        with mock.patch.object(ms, "_probe", return_value=True) as probe:
            self.assertEqual(
                ms.resolve_model(tier="standard", now=base + ms._FLOOR_TTL + 1), first)
            probe.assert_called_once()

    def test_legacy_flat_floor_cache_read_as_heavy_expires_at_1h(self):
        base = 1_000_000.0
        floor = ms.LADDERS["heavy"][-1]            # not PREFERRED -> floor TTL
        self.cache.write_text(json.dumps({"model": floor, "ts": base}))
        with mock.patch.object(ms, "_probe", side_effect=AssertionError("must not probe")):
            self.assertEqual(
                ms.resolve_model(tier="heavy", now=base + ms._FLOOR_TTL - 1), floor)
        with mock.patch.object(ms, "_probe", return_value=True) as probe:
            self.assertEqual(
                ms.resolve_model(tier="heavy", now=base + ms._FLOOR_TTL + 1), ms.PREFERRED)
            probe.assert_called_once()

    def test_legacy_flat_first_rung_cache_read_as_heavy_keeps_24h(self):
        base = 1_000_000.0
        self.cache.write_text(json.dumps({"model": ms.PREFERRED, "ts": base}))
        with mock.patch.object(ms, "_probe", side_effect=AssertionError("must not probe")):
            self.assertEqual(
                ms.resolve_model(tier="heavy", now=base + ms._TTL - 1), ms.PREFERRED)

    def test_light_tier_floor_expires_but_first_rung_kept(self):
        base = 1_000_000.0
        # a cached floor id for light expires at 1h
        self._write("light", ms.LADDERS["light"][-1], base)
        with mock.patch.object(ms, "_probe", return_value=True) as probe:
            self.assertEqual(
                ms.resolve_model(tier="light", now=base + ms._FLOOR_TTL + 1),
                ms.LADDERS["light"][0])
            probe.assert_called_once()
        # a cached first-rung id for light keeps 24h
        self._write("light", ms.LADDERS["light"][0], base)
        with mock.patch.object(ms, "_probe", side_effect=AssertionError("must not probe")):
            self.assertEqual(
                ms.resolve_model(tier="light", now=base + ms._TTL - 1),
                ms.LADDERS["light"][0])


class DowngradeAlertTest(unittest.TestCase):
    """A fresh probe resolution to a non-first model emits exactly one alert per
    tier per 24h through the injected notify path (acceptance 2, fake notifier)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = self.tmp / "model_pref.json"
        patcher = mock.patch.object(ms, "_CACHE", self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(ms.os.environ, {"WORKER_MODEL": ""})
        env.start()
        self.addCleanup(env.stop)
        self.sent: list[str] = []

    def _notify(self, message):
        self.sent.append(message)

    def test_fresh_fallback_resolution_alerts_once_and_names_the_tier(self):
        base = 1_000_000.0
        # standard's first rung is exhausted; the next fallback wins
        first = ms.LADDERS["standard"][0]
        with mock.patch.object(ms, "_probe", side_effect=lambda m, e="max": m != first):
            model = ms.resolve_model(tier="standard", now=base,
                                     use_cache=False, notify=self._notify)
        self.assertEqual(model, ms.LADDERS["standard"][1])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("standard", self.sent[0])

    def test_throttled_to_once_per_tier_per_24h(self):
        base = 1_000_000.0
        first = ms.LADDERS["standard"][0]
        with mock.patch.object(ms, "_probe", side_effect=lambda m, e="max": m != first):
            ms.resolve_model(tier="standard", now=base,
                             use_cache=False, notify=self._notify)
            # a second fresh resolution <24h later: floor re-probed (1h TTL) but
            # the alert is throttled by the cached alerted_ts
            ms.resolve_model(tier="standard", now=base + ms._FLOOR_TTL + 1,
                             use_cache=True, notify=self._notify)
        self.assertEqual(len(self.sent), 1)

    def test_alert_fires_again_after_24h(self):
        base = 1_000_000.0
        first = ms.LADDERS["standard"][0]
        with mock.patch.object(ms, "_probe", side_effect=lambda m, e="max": m != first):
            ms.resolve_model(tier="standard", now=base,
                             use_cache=False, notify=self._notify)
            ms.resolve_model(tier="standard", now=base + ms._TTL + 1,
                             use_cache=True, notify=self._notify)
        self.assertEqual(len(self.sent), 2)

    def test_resolving_to_first_rung_never_alerts(self):
        with mock.patch.object(ms, "_probe", return_value=True):
            ms.resolve_model(tier="standard", now=1_000_000.0,
                             use_cache=False, notify=self._notify)
        self.assertEqual(self.sent, [])

    def test_cache_hit_never_alerts(self):
        base = 1_000_000.0
        floor = ms.LADDERS["standard"][-1]
        self.cache.write_text(json.dumps({"tiers": {"standard": {"model": floor, "ts": base}}}))
        with mock.patch.object(ms, "_probe", side_effect=AssertionError("must not probe")):
            self.assertEqual(
                ms.resolve_model(tier="standard", now=base + 1,
                                 use_cache=True, notify=self._notify), floor)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
