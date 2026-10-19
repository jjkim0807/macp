"""Tests for the systemd unit generator (scripts/systemd/gen_units.py).

``scripts/systemd`` is not a package (no ``__init__.py``), so load the module by
path instead of relying on pytest's sys.path insertion — that keeps the test
working under ``uv run pytest scripts/systemd/test_gen_units.py`` and a bare
``uv run pytest`` alike.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "gen_units", Path(__file__).with_name("gen_units.py"))
gen_units = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gen_units)


class RunnerOOMPolicyTest(unittest.TestCase):
    """A build daemon dying to the kernel OOM killer must not drop the runner
    unit (and the run it was executing) — the default ``OOMPolicy=stop`` kills
    every process in the cgroup. ``OOMPolicy=continue`` on the runner isolates
    that; it is deliberately NOT applied to the control-plane daemons, whose OOM
    should still trigger their normal restart."""

    def setUp(self):
        self.units = gen_units.build()

    def test_runner_unit_has_oom_policy_continue_in_service_section(self):
        runner = self.units["macp-runner-worker-default.service"]
        self.assertIn("OOMPolicy=continue", runner)
        # It must land in [Service], not leak into [Install].
        _, after_service = runner.split("[Service]", 1)
        service_section = after_service.split("[Install]", 1)[0]
        self.assertIn("OOMPolicy=continue", service_section)

    def test_control_plane_units_do_not_get_oom_policy(self):
        # A blanket OOMPolicy would silence real OOMs on these; isolating it to
        # the runner (the only unit that spawns builds) is the whole point.
        for name in ("macp-orchestrator.service", "macp-todo-sync.service",
                     "macp-dashboard.service"):
            self.assertNotIn("OOMPolicy", self.units[name], name)


class OneshotTimeoutStartSecTest(unittest.TestCase):
    """Type=oneshot's TimeoutStartSec defaults to `infinity`; a hung internal git
    fetch/clone could then occupy the unit forever and block the next timer tick.
    Every oneshot unit must carry a finite TimeoutStartSec in [Service]; simple
    (long-running) daemons must not, since a start timeout is meaningless there."""

    def setUp(self):
        self.units = gen_units.build()

    def test_oneshot_units_carry_timeout_start_sec(self):
        for name, text in self.units.items():
            if not name.endswith(".service") or "Type=oneshot" not in text:
                continue
            _, after_service = text.split("[Service]", 1)
            service_section = after_service.split("[Install]", 1)[0]
            self.assertIn("TimeoutStartSec=1800", service_section, name)

    def test_simple_units_do_not_get_timeout_start_sec(self):
        for name, text in self.units.items():
            if not name.endswith(".service") or "Type=simple" not in text:
                continue
            self.assertNotIn("TimeoutStartSec", text, name)


class EnableTargetsCoreTest(unittest.TestCase):
    """--enable targets are exactly the engine-core timers + timerless daemons. With
    the author-only install-only units (kis-trading-real, macp-kakao-refresh) gone,
    there is no stem for enable_targets() to suppress; the `--only` filter narrows to
    named stems."""

    def setUp(self):
        self.units = gen_units.build()

    def test_targets_are_the_core_timers_and_simple_daemons(self):
        self.assertEqual(sorted(gen_units.enable_targets(self.units, None)), sorted([
            "macp-orchestrator.timer", "macp-planner.timer", "macp-maintenance.timer",
            "macp-brief.timer", "macp-health.timer",
            "macp-runner-worker-default.service", "macp-todo-sync.service",
            "macp-dashboard.service"]))

    def test_only_filter_narrows_to_named_stems(self):
        # A service with a timer is enabled via its timer, so --only on a timer stem
        # yields the timer; --only on a timerless daemon yields its service.
        self.assertEqual(gen_units.enable_targets(self.units, ["macp-dashboard"]),
                         ["macp-dashboard.service"])
        self.assertEqual(gen_units.enable_targets(self.units, ["macp-orchestrator"]),
                         ["macp-orchestrator.timer"])
        self.assertEqual(gen_units.enable_targets(self.units, ["nope"]), [])


class ParameterizationTest(unittest.TestCase):
    """Host-identity knobs (MACP_HOST_ID / MACP_CLAUDE_RC_NAME / MACP_CALENDAR_ID)
    are env-overridable so a third party can render their own units. The host-id
    DEFAULTS are neutral: the live host's identity comes from its env file, and
    HOST_ID only feeds ENV_TEMPLATE (which write_env writes only when the file is
    missing), so rendered units are unaffected by the default (equivalence guard,
    Todo/items/macp-public-split-4-daemon-params.md if_then)."""

    @staticmethod
    def _reload(**env):
        # Load a fresh copy of the module under a patched environment; patch.dict
        # snapshots os.environ on enter and fully restores it on exit, so the
        # manual pops/sets below never leak into other tests. Keys mapped to None
        # are removed (exercising the hardcoded default), the rest are set.
        with mock.patch.dict(os.environ, {}, clear=False):
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            spec = importlib.util.spec_from_file_location(
                "gen_units_reloaded", Path(__file__).with_name("gen_units.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

    def test_defaults_render_neutral_host_id_in_env_template(self):
        mod = self._reload(MACP_HOST_ID=None, MACP_CLAUDE_RC_NAME=None,
                           MACP_CALENDAR_ID=None)
        self.assertIn("\nMACP_HOST_ID=macp-host\n", mod.ENV_TEMPLATE)
        self.assertIn("\nCLAUDE_RC_NAME=macp-host\n", mod.ENV_TEMPLATE)

    def test_env_overrides_take_effect(self):
        # Host-identity ENV_TEMPLATE knobs still honour their env overrides; the
        # calendar id resolution has its own hermetic class (CalendarIdResolutionTest).
        mod = self._reload(MACP_HOST_ID="acme-hq",
                           MACP_CLAUDE_RC_NAME="acme-rc")
        self.assertIn("\nMACP_HOST_ID=acme-hq\n", mod.ENV_TEMPLATE)
        self.assertIn("\nCLAUDE_RC_NAME=acme-rc\n", mod.ENV_TEMPLATE)

    def test_claude_rc_name_defaults_to_host_id(self):
        # MACP_CLAUDE_RC_NAME unset → CLAUDE_RC_NAME mirrors MACP_HOST_ID, so a
        # third party setting only MACP_HOST_ID still gets a matching RC name.
        mod = self._reload(MACP_HOST_ID="acme-hq", MACP_CLAUDE_RC_NAME=None,
                           MACP_CALENDAR_ID=None)
        self.assertIn("\nMACP_HOST_ID=acme-hq\n", mod.ENV_TEMPLATE)
        self.assertIn("\nCLAUDE_RC_NAME=acme-hq\n", mod.ENV_TEMPLATE)


class CalendarIdResolutionTest(unittest.TestCase):
    """CALENDAR_ID / LABEL_PREFIX resolve env → <vault>/.macp/config.toml → neutral
    (empty). An empty CALENDAR_ID makes the todo-sync unit OMIT --calendar-id (the
    CLI's own default then applies) rather than syncing to a blank target, and both
    values are injected into ENV_TEMPLATE so the daemon resolves them regardless of
    its WorkingDirectory. Every case uses a throwaway MAESTRO_VAULT_DIR + neutral
    values, so no personal identifier is referenced (public-split leaf 7)."""

    @staticmethod
    def _reload_with_vault(vault, **env):
        # patch.dict snapshots os.environ on enter and restores it on exit, so the
        # MACP_VAULT_ROOT + knob writes below never leak into other tests. Keys
        # mapped to None are removed (exercising env-absent), the rest are set.
        # MACP_VAULT_ROOT is now primary; pop the alias so the ambient gate value
        # (MACP_VAULT_ROOT=vault-template) can't win over this throwaway vault.
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ["MACP_VAULT_ROOT"] = str(vault)
            os.environ.pop("MAESTRO_VAULT_DIR", None)
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            spec = importlib.util.spec_from_file_location(
                "gen_units_cal", Path(__file__).with_name("gen_units.py"))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod

    @staticmethod
    def _write_config(vault, body):
        (vault / ".macp").mkdir(parents=True, exist_ok=True)
        (vault / ".macp" / "config.toml").write_text(body, encoding="utf-8")

    def test_calendar_id_from_config_when_no_env(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            self._write_config(vault, "calendar_id = 'cal@example.com'\n")
            mod = self._reload_with_vault(vault, MACP_CALENDAR_ID=None)
            self.assertIn("--calendar-id cal@example.com\n",
                          mod.build()["macp-todo-sync.service"])

    def test_calendar_id_env_overrides_config(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            self._write_config(vault, "calendar_id = 'cal@example.com'\n")
            mod = self._reload_with_vault(vault, MACP_CALENDAR_ID="ops@acme.example")
            todo_sync = mod.build()["macp-todo-sync.service"]
            self.assertIn("--calendar-id ops@acme.example\n", todo_sync)
            self.assertNotIn("cal@example.com", todo_sync)

    def test_calendar_id_omitted_when_config_and_env_absent(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            self._write_config(vault, "profile = 'test-host'\n")  # no calendar_id key
            mod = self._reload_with_vault(vault, MACP_CALENDAR_ID=None)
            todo_sync = mod.build()["macp-todo-sync.service"]
            self.assertIn("--calendar-sync", todo_sync)
            self.assertNotIn("--calendar-id", todo_sync)

    def test_env_template_injects_label_and_calendar_from_config(self):
        with tempfile.TemporaryDirectory() as d:
            vault = Path(d)
            self._write_config(
                vault, "label_prefix = 'com.acme.'\ncalendar_id = 'cal@example.com'\n")
            mod = self._reload_with_vault(vault, MACP_LABEL_PREFIX=None,
                                          MACP_CALENDAR_ID=None)
            self.assertIn("\nMACP_LABEL_PREFIX=com.acme.\n", mod.ENV_TEMPLATE)
            self.assertIn("\nMACP_CALENDAR_ID=cal@example.com\n", mod.ENV_TEMPLATE)


if __name__ == "__main__":
    unittest.main()


class EnvBackfillTest(unittest.TestCase):
    """write_env() keeps an existing env file (it holds hand-filled secrets), so
    identity keys added after install must be appended, not skipped — a host
    without MACP_LABEL_PREFIX resolved com.macp. from $HOME and daemonctl could
    no longer restart remote-control (2026-09-26)."""

    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "env"

    def _backfill(self, text):
        self.path.write_text(text)
        with mock.patch.object(gen_units, "LABEL_PREFIX", "com.example."), \
             mock.patch.object(gen_units, "CALENDAR_ID", "cal@example.com"):
            gen_units._backfill_identity(self.path)
        return self.path.read_text()

    def test_appends_missing_keys_and_keeps_existing_lines(self):
        out = self._backfill("SECRET=keep\n")
        self.assertIn("SECRET=keep\n", out)
        self.assertIn("MACP_LABEL_PREFIX=com.example.\n", out)
        self.assertIn("MACP_CALENDAR_ID=cal@example.com\n", out)

    def test_never_overrides_a_key_already_present(self):
        out = self._backfill("MACP_LABEL_PREFIX=com.pinned.\n")
        self.assertEqual(out.count("MACP_LABEL_PREFIX="), 1)
        self.assertIn("MACP_LABEL_PREFIX=com.pinned.", out)

    def test_commented_key_counts_as_missing(self):
        out = self._backfill("# MACP_LABEL_PREFIX=com.old.\n")
        self.assertIn("\nMACP_LABEL_PREFIX=com.example.\n", out)


# --- 13c: default-mode golden + installed mode -------------------------------------

GOLDEN = Path(__file__).with_name("testdata") / "default_units.golden"
# Neutral host: no real home or vault path ends up in the golden (it sits inside the
# identifier gate's scanned tree). The vault has no .macp/config.toml, so the
# calendar id and label prefix resolve to "". None = removed from the env.
NEUTRAL_ENV = {
    "HOME": "/srv/neutral-home",
    "MACP_VAULT_ROOT": "/srv/neutral-vault",
    "MAESTRO_VAULT_DIR": None,
    "MACP_ENGINE_ROOT": "/srv/neutral-engine",
    "MACP_HOST_ID": None,
    "MACP_CLAUDE_RC_NAME": None,
    "MACP_CALENDAR_ID": None,
    "MACP_LABEL_PREFIX": None,
    "MACP_ENGINE_PYTHON": None,
}


@contextlib.contextmanager
def _env(**env):
    """Same patch.dict pattern as ParameterizationTest._reload: keys mapped to None
    are removed, the rest set, and os.environ is fully restored on exit."""
    with mock.patch.dict(os.environ, {}, clear=False):
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield


def _neutral_gen_units(**extra):
    """A fresh gen_units loaded under NEUTRAL_ENV (plus ``extra``); ``Path.home()``
    reads the patched HOME at load. MACP_ENGINE_PYTHON is read by build() at call
    time, so callers patch it around build() (see _default_build)."""
    with _env(**(NEUTRAL_ENV | extra)):
        spec = importlib.util.spec_from_file_location(
            "gen_units_neutral", Path(__file__).with_name("gen_units.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


def _default_build(mod):
    """build() with the knob absent from the env, whatever the host has set."""
    with _env(MACP_ENGINE_PYTHON=None):
        return mod.build()


def _render(mod, units=None):
    """Every unit (``--print`` framing), then the env template, as one string."""
    units = _default_build(mod) if units is None else units
    return ("".join(f"##### {n}\n{t}" for n, t in units.items())
            + "##### ENV_TEMPLATE\n" + mod.ENV_TEMPLATE)


class DefaultModeGoldenTest(unittest.TestCase):
    """Invariance pin, not a RED→GREEN test: the golden was rendered from the
    generator BEFORE installed mode existed (13c). With no engine-python knob every
    unit, timer and env template must stay byte-identical, so a host that reruns
    gen_units.py without opting in gets exactly the units it already runs."""

    def test_default_mode_matches_golden_byte_for_byte(self):
        self.assertEqual(_render(_neutral_gen_units()),
                         GOLDEN.read_text(encoding="utf-8"))


REPO = Path(__file__).resolve().parents[2]
ENGINE_PY = "/opt/macp-engine/bin/python"
# Units whose script ships with the engine (unit stem → script name). Default mode
# resolves them under ENGINE_ROOT/scripts/; installed mode through engine_script.
CONVERTED = {
    "macp-orchestrator": "orchestrator_tick.sh",
    "macp-planner": "planner_tick.sh",
    "macp-brief": "brief.sh",
    "macp-health": "health_report.py",
}
# Units that ran ``uv run python -m ...`` (public split 14): installed mode runs the
# same module/arguments from the installed interpreter with -P.
MODULE_UNITS = {
    "macp-runner-worker-default": "-m todo_system.orchestrator.runner --agent worker-default "
                                  "--mode live-staged --uri file://Worker/agents --daemon",
    "macp-maintenance": "-m todo_system.worker maintenance",
    "macp-todo-sync": "-u -m todo_system.cli daemon --interval 3 --calendar-sync",
    "macp-dashboard": "-u -m todo_system.engine_script macp_dashboard.py "
                      "--host 127.0.0.1 --port 8787 --no-open",
}
# No vault-only units survive in the engine-core set (the Remote Control stack and
# usage monitor were author-only and are dropped).
VAULT_ONLY: tuple[str, ...] = ()


def _service_section(text):
    return text.split("[Service]", 1)[1].split("[Install]", 1)[0]


def _exec_start(text):
    (line,) = [ln for ln in text.splitlines() if ln.startswith("ExecStart=")]
    return line[len("ExecStart="):]


def _export_excluded():
    lines = (REPO / "scripts" / "public_export_exclude.txt").read_text(encoding="utf-8").splitlines()
    return {ln.split("#", 1)[0].strip() for ln in lines} - {""}


class InstalledModeTest(unittest.TestCase):
    """--engine-python / MACP_ENGINE_PYTHON (13c): engine-script units run through
    ``todo_system.engine_script`` from the installed interpreter with -P (the vault
    cwd may still hold a checkout's todo_system/) and MACP_VAULT_ROOT; nothing else
    in any unit moves."""

    def setUp(self):
        self.mod = _neutral_gen_units()
        self.default = _default_build(self.mod)
        self.inst = self.mod.build(ENGINE_PY)

    def test_engine_script_units_exec_through_engine_script(self):
        for stem, name in CONVERTED.items():
            self.assertEqual(_exec_start(self.inst[f"{stem}.service"]),
                             f"{ENGINE_PY} -P -m todo_system.engine_script {name}", stem)

    def test_engine_script_units_pass_vault_root_in_service_section(self):
        env_line = f"Environment=MACP_VAULT_ROOT={self.mod.VAULT}"
        for stem in CONVERTED:
            text = self.inst[f"{stem}.service"]
            self.assertIn(env_line, _service_section(text), stem)
            self.assertEqual(text.count(env_line), 1, stem)
            # engine_script setdefaults it to its own interpreter.
            self.assertNotIn("MACP_PYTHON", text, stem)

    def test_converted_units_keep_working_directory_and_lifecycle(self):
        env_line = f"Environment=MACP_VAULT_ROOT={self.mod.VAULT}"

        def strip(text):
            return [ln for ln in text.splitlines()
                    if not ln.startswith("ExecStart=") and ln != env_line]

        for stem in CONVERTED:
            name = f"{stem}.service"
            self.assertEqual(strip(self.inst[name]), strip(self.default[name]), stem)

    def test_module_units_run_from_installed_interpreter(self):
        env_line = f"Environment=MACP_VAULT_ROOT={self.mod.VAULT}"
        for stem, args in MODULE_UNITS.items():
            text = self.inst[f"{stem}.service"]
            self.assertEqual(_exec_start(text), f"{ENGINE_PY} -P {args}", stem)
            self.assertNotIn(str(self.mod.UV), text, stem)
            self.assertEqual(_service_section(text).count(env_line), 1, stem)

    def test_module_units_keep_everything_but_exec_and_vault_root(self):
        env_line = f"Environment=MACP_VAULT_ROOT={self.mod.VAULT}"

        def strip(text):
            return [ln for ln in text.splitlines()
                    if not ln.startswith("ExecStart=") and ln != env_line]

        for stem in MODULE_UNITS:
            name = f"{stem}.service"
            self.assertEqual(strip(self.inst[name]), strip(self.default[name]), stem)
        # The runner keeps its OOM policy (order inside [Service] is irrelevant).
        self.assertIn("OOMPolicy=continue", self.inst["macp-runner-worker-default.service"])

    def test_no_vault_engine_reference_left_in_macp_units(self):
        # After the switch nothing under macp-* may still import the vault's own
        # todo_system/ (uv run in the vault) or exec an engine script by vault path.
        for name, text in self.inst.items():
            if not name.startswith("macp-") or not name.endswith(".service"):
                continue
            exec_start = _exec_start(text)
            self.assertNotIn(f"{self.mod.UV} run", exec_start, name)
            self.assertNotIn(f"{self.mod.VAULT}/scripts/", exec_start, name)

    def test_everything_else_is_untouched(self):
        self.assertEqual(list(self.inst), list(self.default))
        converted = {f"{stem}.service" for stem in [*CONVERTED, *MODULE_UNITS]}
        for name in self.default:
            if name not in converted:
                self.assertEqual(self.inst[name], self.default[name], name)

    def test_env_knob_equals_cli_arg(self):
        with _env(MACP_ENGINE_PYTHON=ENGINE_PY):
            via_env = self.mod.build()
        self.assertEqual(via_env, self.inst)

    def test_empty_knob_is_default_mode(self):
        self.assertEqual(self.mod.build(""), self.default)
        with _env(MACP_ENGINE_PYTHON=""):
            via_empty_env = self.mod.build()
        self.assertEqual(_render(self.mod, via_empty_env), GOLDEN.read_text(encoding="utf-8"))

    def test_relative_engine_python_rejected(self):
        with self.assertRaises(ValueError):
            self.mod.build("venv/bin/python")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            self.mod.main(["--print", "--engine-python", "python"])
        self.assertEqual(cm.exception.code, 2)

    def test_print_cli_renders_installed_mode(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = self.mod.main(["--print", "--engine-python", ENGINE_PY])
        self.assertEqual(rc, 0)
        self.assertIn(f"ExecStart={ENGINE_PY} -P -m todo_system.engine_script orchestrator_tick.sh\n",
                      out.getvalue())

    def test_macp_worker_unit_not_invented(self):
        # The AC names a macp-worker unit, but systemd replaced worker_run_next.sh
        # with the runner + maintenance units; inventing one would break the
        # default-mode golden.
        self.assertNotIn("macp-worker.service", self.inst)
        self.assertNotIn("macp-worker.service", self.default)


class ClassificationMatchesExportExcludeTest(unittest.TestCase):
    """scripts/public_export_exclude.txt decides what is vault-only: those files
    leave the engine tree, everything else ships in the wheel and leaves the vault.
    A unit left on a vault path for an exported script would break once the vault
    copy is gone; one routed through engine_script for a vault-only file would not
    find it in the wheel's export set."""

    def setUp(self):
        self.mod = _neutral_gen_units()
        self.default = _default_build(self.mod)
        self.excluded = _export_excluded()

    def test_converted_scripts_are_exported_engine_files(self):
        for stem, name in CONVERTED.items():
            self.assertNotIn(f"scripts/{name}", self.excluded, stem)
            self.assertTrue((REPO / "scripts" / name).is_file(), name)
            # Default mode now resolves the engine script from ENGINE_ROOT, not VAULT.
            self.assertIn(f"{self.mod.ENGINE_ROOT}/scripts/{name}",
                          _exec_start(self.default[f"{stem}.service"]), stem)

    def test_every_engine_script_unit_is_classified(self):
        # Any default-mode unit that execs a script under ENGINE_ROOT/scripts/ must be
        # a known engine-script (CONVERTED) or module (dashboard) unit — a guard that
        # no new unused engine-script unit sneaks in unclassified.
        classified = set(CONVERTED) | set(MODULE_UNITS)
        for name, text in self.default.items():
            if name.endswith(".service") and f"{self.mod.ENGINE_ROOT}/scripts/" in _exec_start(text):
                self.assertIn(name.rsplit(".", 1)[0], classified, name)


# --- outsider-safe criteria (볼트 경로·코어 유닛만) ------------------------------

AUTHOR_ONLY_STEMS = (
    "kis-trading", "kis-trading-real", "kis-trading-livesync", "kis-trading-watchdog",
    "mailroom-tick", "claude-remote-control", "claude-remote-control-watchdog",
    "claude-upgrade", "claude-auth-watch", "macp-kakao-refresh", "macp-kakao-digest",
    "macp-browser", "macp-monitor",
)
CORE_STEMS = frozenset({
    "macp-runner-worker-default", "macp-orchestrator", "macp-planner",
    "macp-maintenance", "macp-todo-sync", "macp-dashboard", "macp-brief", "macp-health",
})


def _reload_vault(**env):
    """A fresh gen_units loaded under ``env`` (keys mapped to None are removed, the
    rest set; os.environ restored on exit) — for exercising path resolution."""
    with _env(**env):
        spec = importlib.util.spec_from_file_location(
            "gen_units_vault", Path(__file__).with_name("gen_units.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod


class VaultRootResolutionTest(unittest.TestCase):
    """AC: ``MACP_VAULT_ROOT=/tmp/v`` relocates every vault path — WorkingDirectory,
    the log append targets, and the installed-mode ``Environment=MACP_VAULT_ROOT=``
    line — under ``/tmp/v``. ``MAESTRO_VAULT_DIR`` stays a working backward-compat
    alias when the primary var is absent."""

    @staticmethod
    def _vault_tokens(units):
        for name, text in units.items():
            for line in text.splitlines():
                for prefix in ("WorkingDirectory=", "StandardOutput=append:",
                               "StandardError=append:", "Environment=MACP_VAULT_ROOT="):
                    if line.startswith(prefix):
                        yield name, line[len(prefix):]

    def test_every_vault_path_sits_under_macp_vault_root(self):
        mod = _reload_vault(MACP_VAULT_ROOT="/tmp/v", MAESTRO_VAULT_DIR=None,
                            MACP_ENGINE_ROOT="/srv/neutral-engine")
        self.assertEqual(mod.VAULT, Path("/tmp/v"))
        both = {**mod.build(), **{f"inst-{k}": v for k, v in mod.build(ENGINE_PY).items()}}
        checked = 0
        for name, tok in self._vault_tokens(both):
            self.assertTrue(tok.startswith("/tmp/v"), f"{name}: {tok}")
            checked += 1
        self.assertGreater(checked, 0)  # guard: the extraction isn't a no-op

    def test_maestro_vault_dir_alias_resolves_when_primary_absent(self):
        mod = _reload_vault(MACP_VAULT_ROOT=None, MAESTRO_VAULT_DIR="/tmp/alias",
                            MACP_ENGINE_ROOT="/srv/neutral-engine")
        self.assertEqual(mod.VAULT, Path("/tmp/alias"))


class ExecStartPathsExistTest(unittest.TestCase):
    """AC: every rendered ExecStart script path must exist (engine repo or vault).
    Built with the module's real ENGINE_ROOT / VAULT (unpatched), so a dropped or
    moved script surfaces as a missing file here instead of a crash-looping unit on
    someone else's host. ``uv``/module invocations with no script path are skipped."""

    def test_every_exec_start_script_file_exists(self):
        units = gen_units.build()
        checked = 0
        for name, text in units.items():
            if not name.endswith(".service"):
                continue
            for tok in _exec_start(text).split():
                if "/" in tok and (tok.endswith(".sh") or tok.endswith(".py")):
                    self.assertTrue(Path(tok).is_file(), f"{name}: {tok}")
                    checked += 1
        self.assertGreater(checked, 0)  # guard: the extraction isn't a no-op


class AuthorOnlyUnitsAbsentTest(unittest.TestCase):
    """AC: author-only unit names (trading, mailroom, Remote Control, Kakao, browser,
    usage monitor) must be absent from ``build()`` in both default and installed mode
    — a third party running ``--enable`` would otherwise start crash-looping units
    whose scripts shipped out to the author's vault."""

    def test_no_author_only_stem_in_default_or_installed_build(self):
        for units in (gen_units.build(), gen_units.build(ENGINE_PY)):
            keys = set(units)
            for stem in AUTHOR_ONLY_STEMS:
                self.assertNotIn(f"{stem}.service", keys, stem)
                self.assertNotIn(f"{stem}.timer", keys, stem)

    def test_exactly_the_eight_core_stems_render(self):
        stems = {name.rsplit(".", 1)[0] for name in gen_units.build()}
        self.assertEqual(stems, set(CORE_STEMS))
