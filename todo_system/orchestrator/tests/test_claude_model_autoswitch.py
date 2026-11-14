"""Tests for scripts/claude_model_autoswitch.py — the interactive-session half
of model recovery.

Two safety properties are pinned by construction rather than by convention: no
test may touch the real legacy-dir settings.json (every run injects --settings
and --state under a temp dir), and no test may invoke the real `claude` CLI (the
probe is substituted, and subprocess.run is booby-trapped to prove it).
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_system.orchestrator import model_select as ms

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "claude_model_autoswitch.py"


def _load_script():
    """scripts/ is not a package, so load the file directly."""
    spec = importlib.util.spec_from_file_location("claude_model_autoswitch", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_script()


@contextlib.contextmanager
def _probes(alive):
    """Force probe results. Yields the patched invalidate_cache so a test can
    assert the worker's cache is dropped on a real switch and only then — the
    patch also keeps the real Worker/_state/model_pref.json out of reach.

    On exit this asserts subprocess.run was never reached, which is how every
    test that uses it also proves acceptance 6. It is checked with
    assert_not_called() rather than a raising side_effect on purpose: both
    _probe() and the runner hook swallow every Exception, so a sentinel raise
    would be caught by the code under test and prove nothing.
    """
    with mock.patch.object(mod, "probe_model", side_effect=lambda m: m in alive), \
         mock.patch.object(mod, "invalidate_cache") as inv, \
         mock.patch.object(ms.subprocess, "run") as spawned:
        yield inv
        spawned.assert_not_called()


class SettingsWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.settings = self.tmp / "settings.json"
        self.state = self.tmp / "state.json"
        self.bak = self.tmp / "settings.json.bak"
        self.tmpfile = self.tmp / "settings.json.tmp"

    def _write(self, payload) -> str:
        raw = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
        self.settings.write_text(raw, encoding="utf-8")
        return raw

    def _run(self, *extra) -> int:
        return mod.main(["--settings", str(self.settings),
                         "--state", str(self.state), *extra])

    def test_switch_replaces_only_the_model_key(self):
        self._write({"model": ms.PREFERRED, "theme": "dark", "env": {"FOO": "1"},
                     "permissions": {"allow": ["Bash(git:*)"]}, "unknownFutureKey": [1, 2]})
        with _probes({ms.FALLBACKS[0]}) as inv:
            self.assertEqual(self._run(), 0)

        after = json.loads(self.settings.read_text(encoding="utf-8"))
        self.assertEqual(after["model"], ms.FALLBACKS[0])
        # everything else survives verbatim, including keys this script has
        # never heard of
        self.assertEqual(after["theme"], "dark")
        self.assertEqual(after["env"], {"FOO": "1"})
        self.assertEqual(after["permissions"], {"allow": ["Bash(git:*)"]})
        self.assertEqual(after["unknownFutureKey"], [1, 2])
        inv.assert_called_once()            # the worker's 24h cache is dropped too

    def test_backup_holds_the_previous_version_and_no_tmp_is_left(self):
        self._write({"model": ms.PREFERRED, "theme": "dark"})
        with _probes({ms.FALLBACKS[0]}):
            self.assertEqual(self._run(), 0)
        self.assertEqual(json.loads(self.bak.read_text(encoding="utf-8"))["model"],
                         ms.PREFERRED)
        self.assertFalse(self.tmpfile.exists())

    def test_unchanged_model_is_not_rewritten_at_all(self):
        """Rewriting identical bytes would churn the mtime and imply a change
        nobody made."""
        self._write({"model": ms.PREFERRED, "theme": "dark"})
        before_bytes = self.settings.read_bytes()
        before_mtime = self.settings.stat().st_mtime_ns
        with _probes({ms.PREFERRED}) as inv:
            self.assertEqual(self._run(), 0)
        self.assertEqual(self.settings.read_bytes(), before_bytes)
        self.assertEqual(self.settings.stat().st_mtime_ns, before_mtime)
        self.assertFalse(self.bak.exists())
        inv.assert_not_called()

    def test_unparseable_settings_is_left_strictly_untouched(self):
        raw = self._write('{"model": "claude-opus-5",\n  "theme": oops\n}')
        with _probes({ms.PREFERRED}):
            self.assertEqual(self._run(), 1)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), raw)
        self.assertFalse(self.bak.exists())
        self.assertFalse(self.tmpfile.exists())

    def test_non_object_top_level_is_rejected(self):
        raw = self._write('["not", "an", "object"]')
        with _probes({ms.PREFERRED}):
            self.assertEqual(self._run(), 1)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), raw)

    def test_missing_settings_file_is_not_created(self):
        with _probes({ms.PREFERRED}):
            self.assertEqual(self._run(), 1)
        self.assertFalse(self.settings.exists())

    def test_every_model_dead_writes_nothing(self):
        """Unlike resolve_model(), never park a known-dead id in real settings."""
        raw = self._write({"model": ms.PREFERRED, "theme": "dark"})
        with _probes(set()) as inv:
            self.assertEqual(self._run(), 1)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), raw)
        self.assertFalse(self.bak.exists())
        inv.assert_not_called()

    def test_dry_run_writes_nothing_anywhere(self):
        raw = self._write({"model": ms.PREFERRED})
        with _probes({ms.FALLBACKS[0]}) as inv:
            self.assertEqual(self._run("--dry-run"), 0)
        self.assertEqual(self.settings.read_text(encoding="utf-8"), raw)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.bak.exists())
        inv.assert_not_called()

    def test_failed_upgrade_is_stamped_so_the_next_tick_skips_it(self):
        """Quota discipline: one PREFERRED probe per UPGRADE_TTL, not per tick."""
        self._write({"model": ms.FALLBACKS[0]})
        with _probes({ms.FALLBACKS[0]}):
            self.assertEqual(self._run(), 0)        # tick 1: tries PREFERRED, keeps current
        self.assertGreater(
            json.loads(self.state.read_text(encoding="utf-8"))["last_upgrade_ts"], 0)

        with mock.patch.object(mod, "probe_model",
                               side_effect=lambda m: m == ms.FALLBACKS[0]) as probe, \
             mock.patch.object(mod, "invalidate_cache"):
            self.assertEqual(self._run(), 0)        # tick 2, well inside the TTL
        self.assertNotIn(ms.PREFERRED, [c.args[0] for c in probe.call_args_list])


class ChooseModelTest(unittest.TestCase):
    NOW = 1_000_000.0

    def _choose(self, current, state, alive):
        with mock.patch.object(mod, "probe_model", side_effect=lambda m: m in alive):
            return mod.choose_model(current, state, self.NOW)

    def test_healthy_current_stops_after_one_probe(self):
        decision = self._choose(ms.FALLBACKS[0], {"last_upgrade_ts": self.NOW},
                                {ms.PREFERRED, ms.FALLBACKS[0]})
        self.assertEqual(decision["model"], ms.FALLBACKS[0])
        # PREFERRED is alive but deliberately not probed — inside the upgrade TTL
        self.assertEqual(list(decision["probed"]), [ms.FALLBACKS[0]])
        self.assertFalse(decision["upgrade_attempted"])

    def test_dead_current_walks_down_the_ladder(self):
        decision = self._choose(ms.PREFERRED, {"last_upgrade_ts": self.NOW},
                                {ms.FALLBACKS[-1]})
        self.assertEqual(decision["model"], ms.FALLBACKS[-1])

    def test_the_floor_is_probed_not_assumed(self):
        decision = self._choose(None, {}, set())
        self.assertIsNone(decision["model"])
        # resolve_model() returns this one unprobed; writing it here would put a
        # dead id into the user's settings, so it must be verified
        self.assertIn(ms.FALLBACKS[-1], decision["probed"])

    def test_upgrade_back_to_preferred_once_the_ttl_has_passed(self):
        stale = {"last_upgrade_ts": self.NOW - mod.UPGRADE_TTL - 1}
        decision = self._choose(ms.FALLBACKS[0], stale, {ms.PREFERRED, ms.FALLBACKS[0]})
        self.assertEqual(decision["model"], ms.PREFERRED)
        self.assertTrue(decision["upgrade_attempted"])

    def test_failed_upgrade_keeps_the_working_fallback(self):
        stale = {"last_upgrade_ts": self.NOW - mod.UPGRADE_TTL - 1}
        decision = self._choose(ms.FALLBACKS[0], stale, {ms.FALLBACKS[0]})
        self.assertEqual(decision["model"], ms.FALLBACKS[0])
        self.assertTrue(decision["upgrade_attempted"])

    def test_no_upgrade_probe_inside_the_ttl(self):
        recent = {"last_upgrade_ts": self.NOW - 60}
        decision = self._choose(ms.FALLBACKS[0], recent, {ms.PREFERRED, ms.FALLBACKS[0]})
        self.assertEqual(decision["model"], ms.FALLBACKS[0])
        self.assertNotIn(ms.PREFERRED, decision["probed"])

    def test_preferred_alias_with_context_suffix_is_not_rewritten(self):
        # settings.json often says "fable[1m]"; an upgrade to bare "fable" would
        # silently drop the 1M-context suffix.
        stale = {"last_upgrade_ts": self.NOW - mod.UPGRADE_TTL - 1}
        current = f"{ms.PREFERRED}[1m]"
        decision = self._choose(current, stale, {ms.PREFERRED, current})
        self.assertEqual(decision["model"], current)
        self.assertFalse(decision["upgrade_attempted"])


if __name__ == "__main__":
    unittest.main()
