"""The suite must never reach the ``auto`` tick's real side effects.

Pins ``_auto_side_effects_isolated`` in todo_system/conftest.py: a real usage
fetch raises, the usage_guard state path is redirected, ``_pull_main`` /
``_check_divergence`` are no-ops without an injected ``_run`` and the real
functions with one, and the two marks lift each guard. The real values are
reached through the ``real_auto_side_effects`` fixture only — importing the
conftest module from a test risks loading a second copy of it.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from todo_system import usage_guard
from todo_system.orchestrator import orchestrator as orch_mod

_DIVERGENCE_KEYS = {"ahead", "behind", "consecutive_behind", "threshold", "alert",
                    "updated", "last_detail"}


def _no_subprocess(monkeypatch):
    calls = []

    def recorder(*args, **kwargs):
        calls.append(args)
        raise AssertionError(f"guarded call spawned a subprocess: {args}")

    monkeypatch.setattr(orch_mod.subprocess, "run", recorder)
    return calls


def test_fetch_usage_raises_under_guard():
    with pytest.raises(RuntimeError) as exc:
        usage_guard._fetch_usage()
    assert type(exc.value).__name__ == "RealUsageFetchBlocked"
    with pytest.raises(RuntimeError) as exc:
        usage_guard._fetch_usage_for(object())
    assert type(exc.value).__name__ == "RealUsageFetchBlocked"


def test_guarded_fetch_is_not_the_real_one(real_auto_side_effects):
    assert usage_guard._fetch_usage is not real_auto_side_effects["fetch_usage"]
    assert usage_guard._fetch_usage_for is not real_auto_side_effects["fetch_usage_for"]


def test_get_snapshot_records_block_not_network(monkeypatch, real_auto_side_effects):
    # What test_usage_guard.py's _clean_usage_guard_env does: the env redirect is
    # gone, and the _state_path wrapper alone keeps the state off the vault.
    monkeypatch.delenv("USAGE_GUARD_STATE_DIR")
    state = usage_guard.get_snapshot(force=True)
    assert "RealUsageFetchBlocked" in state["error"]
    path = usage_guard._state_path()
    real_dir = real_auto_side_effects["ug_state_dir"]
    assert not (os.path.realpath(path) + os.sep).startswith(real_dir + os.sep)
    assert path.is_file()
    assert (path.parent / "fetch.lock").exists()


def test_state_path_passthrough_when_root_patched(monkeypatch, tmp_path):
    monkeypatch.setattr(usage_guard, "ROOT", tmp_path)
    monkeypatch.delenv("USAGE_GUARD_STATE_DIR")
    assert usage_guard._state_path() == tmp_path / "Worker" / "_state" / "usage_guard" / "latest.json"


def test_state_path_passthrough_with_own_env(monkeypatch, tmp_path):
    monkeypatch.setenv("USAGE_GUARD_STATE_DIR", str(tmp_path))
    assert usage_guard._state_path() == tmp_path / "latest.json"


def test_pull_main_noop_without_run(monkeypatch, tmp_path):
    calls = _no_subprocess(monkeypatch)
    assert orch_mod._pull_main(tmp_path) is None
    assert calls == []


def test_check_divergence_noop_without_run(monkeypatch, tmp_path):
    calls = _no_subprocess(monkeypatch)
    state = orch_mod._check_divergence(tmp_path, threshold=5)
    assert set(state) == _DIVERGENCE_KEYS
    assert state["alert"] is False and state["threshold"] == 5
    assert calls == []
    assert not (tmp_path / "Worker" / "_state" / "divergence.json").exists()


def test_injected_run_passes_through(tmp_path):
    seen = []

    def head_is_feature(args, **_kwargs):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, "feature\n", "")

    orch_mod._pull_main(tmp_path, _run=head_is_feature)
    assert seen == [["git", "rev-parse", "--abbrev-ref", "HEAD"]]

    def two_behind(args, **_kwargs):
        return subprocess.CompletedProcess(args, 0, "2\t1\n", "")

    state = orch_mod._check_divergence(tmp_path, _run=two_behind)
    assert state["behind"] == 2 and state["ahead"] == 1
    assert (tmp_path / "Worker" / "_state" / "divergence.json").is_file()


@pytest.mark.real_usage_fetch
def test_real_usage_fetch_mark_lifts_the_block(monkeypatch, real_auto_side_effects):
    assert usage_guard._fetch_usage is real_auto_side_effects["fetch_usage"]
    tokens = []
    monkeypatch.setattr(usage_guard, "_read_token", lambda: "tok")
    monkeypatch.setattr(usage_guard, "_request_usage", lambda token: tokens.append(token) or {})
    assert usage_guard._fetch_usage() == {}
    assert tokens == ["tok"]


@pytest.mark.real_vault_git
def test_real_vault_git_mark_lifts_the_noop(monkeypatch, tmp_path, real_auto_side_effects):
    assert orch_mod._pull_main is real_auto_side_effects["pull_main"]
    calls = []

    def recorder(args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "", "")

    monkeypatch.setattr(orch_mod.subprocess, "run", recorder)
    orch_mod._pull_main(tmp_path)
    assert calls == [["git", "rev-parse", "--abbrev-ref", "HEAD"]]


class GuardOnTestCase(unittest.TestCase):
    """The suites are mostly unittest.TestCase classes: the guard and the marks
    must reach them too, with no fixture argument."""

    def test_guard_applies(self):
        with self.assertRaises(RuntimeError):
            usage_guard._fetch_usage()
        # A refusing subprocess.run and a tmp root: a regressed guard fails here
        # instead of fetching in the checkout.
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(orch_mod.subprocess, "run",
                                  side_effect=AssertionError("guard regressed")) as run:
            state = orch_mod._check_divergence(Path(tmp))
            self.assertEqual(set(state), _DIVERGENCE_KEYS)
            self.assertIn("conftest", state["last_detail"])
            run.assert_not_called()

    @pytest.mark.real_usage_fetch
    def test_method_mark_lifts_the_block(self):
        self.assertEqual(usage_guard._fetch_usage.__name__, "_fetch_usage")


@pytest.mark.real_vault_git
class GuardLiftedOnTestCase(unittest.TestCase):
    def test_class_mark_lifts_the_noop(self):
        self.assertEqual(orch_mod._check_divergence.__name__, "_check_divergence")
        self.assertEqual(orch_mod._pull_main.__name__, "_pull_main")
