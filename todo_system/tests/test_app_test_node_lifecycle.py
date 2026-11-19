"""``scripts/app-test-node start|stop`` go through the engine's node-up/node-down.

The script no longer calls ``gcloud instances start|stop`` itself: it runs
``remote_gate``'s ``node-up`` / ``node-down`` (on ``APP_TEST_PYTHON``), which
follows the vault config's ``node_mode`` (start-stop, or create-from-image /
delete). Only when that helper cannot run at all (rc 3 / 127) does it fall back
to the legacy direct gcloud call. ``APP_TEST_NODE_MODE=local`` never reaches the
helper.

Both binaries are fakes: ``APP_TEST_PYTHON`` is a shell script that records its
argv + the node env it saw and exits ``FAKE_PY_RC``; ``GCLOUD`` records its argv
and exits 0 (so the post-start ssh wait succeeds at once). Nothing touches the
network, a VM or the vault. No literal from the identifier gate's pattern may
appear here — this file sits inside the scanned tree.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "app-test-node"

_FAKE_PY = """#!/usr/bin/env bash
a="$*"; a=${a//$'\\n'/ }      # the inline -c launcher spans lines; log one
printf '%s\\n' "ARGS $a" "NODE=$APP_TEST_NODE ZONE=$APP_TEST_ZONE" >>"$FAKE_LOG"
exit "${FAKE_PY_RC:-0}"
"""

_FAKE_GCLOUD = """#!/usr/bin/env bash
printf '%s\\n' "GCLOUD $*" >>"$FAKE_LOG"
exit 0
"""


def _exe(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _run(tmp_path: Path, subcmd: str, *, py_rc: int = 0, extra: dict | None = None):
    log = tmp_path / "calls.log"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("APP_TEST_") and k != "GCLOUD"}
    env.update({
        "APP_TEST_PYTHON": str(_exe(tmp_path / "fake-python", _FAKE_PY)),
        "GCLOUD": str(_exe(tmp_path / "fake-gcloud", _FAKE_GCLOUD)),
        "FAKE_LOG": str(log),
        "FAKE_PY_RC": str(py_rc),
        "APP_TEST_NODE": "demo-node",
        "APP_TEST_ZONE": "demo-zone",
        "APP_TEST_LOCAL_HOME": str(tmp_path / "local-home"),
        "ANDROID_HOME": str(tmp_path / "sdk"),
    })
    env.update(extra or {})
    cp = subprocess.run(["bash", str(SCRIPT), subcmd], env=env, capture_output=True,
                        text=True, timeout=30, check=False)
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return cp, lines


def _helper_calls(lines):
    return [ln for ln in lines if ln.startswith("ARGS ")]


def _gcloud_instances(lines):
    return [ln for ln in lines if ln.startswith("GCLOUD compute instances")]


def test_stop_runs_node_down_and_never_gcloud_directly(tmp_path):
    cp, lines = _run(tmp_path, "stop")
    assert cp.returncode == 0, cp.stderr
    calls = _helper_calls(lines)
    assert len(calls) == 1
    assert "remote_gate" in calls[0] and calls[0].endswith(" node-down")
    assert "NODE=demo-node ZONE=demo-zone" in lines      # same instance as ssh/scp
    assert _gcloud_instances(lines) == []


def test_start_runs_node_up_then_waits_for_ssh(tmp_path):
    cp, lines = _run(tmp_path, "start")
    assert cp.returncode == 0, cp.stderr
    assert "ssh ready" in cp.stdout
    calls = _helper_calls(lines)
    assert len(calls) == 1 and calls[0].endswith(" node-up")
    assert _gcloud_instances(lines) == []
    assert any(ln.startswith("GCLOUD compute ssh demo-node") for ln in lines)


def test_helper_unavailable_falls_back_to_legacy_gcloud(tmp_path):
    cp, lines = _run(tmp_path, "stop", py_rc=3)
    assert cp.returncode == 0, cp.stderr
    assert "engine helper unavailable" in cp.stderr
    assert _gcloud_instances(lines) == [
        "GCLOUD compute instances stop demo-node --zone demo-zone --quiet"]

    (tmp_path / "s").mkdir()
    cp, lines = _run(tmp_path / "s", "start", py_rc=127)
    assert cp.returncode == 0, cp.stderr
    assert _gcloud_instances(lines) == [
        "GCLOUD compute instances start demo-node --zone demo-zone --quiet"]


def test_helper_gcloud_failure_is_the_verdict_no_fallback(tmp_path):
    cp, lines = _run(tmp_path, "stop", py_rc=1)
    assert cp.returncode == 1
    assert _gcloud_instances(lines) == []                 # no second, legacy attempt
    (tmp_path / "s").mkdir()
    cp, lines = _run(tmp_path / "s", "start", py_rc=1)
    assert cp.returncode == 1
    assert not any(ln.startswith("GCLOUD compute ssh") for ln in lines)


def test_local_mode_stop_never_calls_the_helper(tmp_path):
    (tmp_path / "sdk").mkdir()
    cp, lines = _run(tmp_path, "stop", extra={"APP_TEST_NODE_MODE": "local"})
    assert cp.returncode == 0, cp.stderr
    assert _helper_calls(lines) == [] and _gcloud_instances(lines) == []


def test_real_helper_import_failure_maps_to_rc3(tmp_path):
    """The script's inline launcher turns an engine import failure into rc 3
    (→ legacy fallback), not a bare traceback rc 1 that would read as a gcloud
    verdict. Drive it with the real interpreter but an unresolvable vault."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("APP_TEST_") and k not in ("GCLOUD", "MACP_VAULT_ROOT")}
    log = tmp_path / "calls.log"
    env.update({"GCLOUD": str(_exe(tmp_path / "fake-gcloud", _FAKE_GCLOUD)),
                "FAKE_LOG": str(log), "APP_TEST_NODE": "demo-node",
                "APP_TEST_ZONE": "demo-zone",
                # a python whose engine import cannot succeed: -I drops PYTHONPATH
                # and the cwd, -S drops site-packages (an editable install)
                "APP_TEST_PYTHON": str(_exe(tmp_path / "py-noengine", (
                    "#!/usr/bin/env bash\n"
                    'exec python3 -I -S "$@"\n')))})
    cp = subprocess.run(["bash", str(SCRIPT), "stop"], env=env, capture_output=True,
                        text=True, timeout=30, check=False)
    assert "engine helper unavailable" in cp.stderr
    assert cp.returncode == 0
    assert log.read_text(encoding="utf-8").splitlines() == [
        "GCLOUD compute instances stop demo-node --zone demo-zone --quiet"]
