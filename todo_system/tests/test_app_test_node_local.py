"""``APP_TEST_NODE_MODE=local`` — ``scripts/app-test-node`` against an emulator on
the same host (a remote runner with capability ``device``).

The real script runs against a fake SDK: ``emulator`` records its argv and flips a
boot flag, ``adb`` answers ``getprop sys.boot_completed`` from that flag and fakes
install / launch / ``exec-out screencap``, ``aapt2`` prints a package name. No
gcloud is on PATH, so any GCE path taken by mistake fails the test.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "app-test-node"
_TIMEOUT = 30

_EMULATOR = """#!/usr/bin/env bash
echo "$@" > "$FAKE_STATE/emulator.argv"
echo "$HOME" > "$FAKE_STATE/emulator.home"
touch "$FAKE_STATE/booted"
sleep 30
"""

_ADB = """#!/usr/bin/env bash
case "$*" in
  "shell getprop sys.boot_completed") [ -e "$FAKE_STATE/booted" ] && echo 1 || echo "" ;;
  "install -r "*) echo "Success" ;;
  "exec-out screencap -p") printf 'PNG' ;;
  "emu kill") rm -f "$FAKE_STATE/booted"; pkill -f "$FAKE_SDK/emulator/emulator" ; echo OK ;;
  shell\\ monkey*|"shell am start"*) echo "$*" >> "$FAKE_STATE/launch.log" ;;
  *) echo "$*" >> "$FAKE_STATE/adb.other" ;;
esac
"""

_AAPT2 = """#!/usr/bin/env bash
echo com.example.demo
"""


def _exe(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _setup(tmp: Path) -> tuple[dict, Path, Path]:
    sdk = tmp / "sdk"
    state = tmp / "state"
    state.mkdir()
    _exe(sdk / "emulator" / "emulator", _EMULATOR)
    _exe(sdk / "platform-tools" / "adb", _ADB)
    _exe(sdk / "build-tools" / "35.0.0" / "aapt2", _AAPT2)
    node_home = tmp / "nodehome"
    real_home = tmp / "home"
    real_home.mkdir()
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(real_home),
        "APP_TEST_NODE_MODE": "local",
        "APP_TEST_LOCAL_HOME": str(node_home),
        "ANDROID_HOME": str(sdk),
        "APP_TEST_OUT": str(tmp / "shots"),
        "APP_TEST_POLL_INTERVAL": "0.1",
        "APP_TEST_BOOT_TIMEOUT": "20",
        "GCLOUD": str(tmp / "no-gcloud"),
        "FAKE_STATE": str(state),
        "FAKE_SDK": str(sdk),
    }
    return env, node_home, state


def _run(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True,
                          text=True, timeout=_TIMEOUT)


def test_start_checks_the_local_sdk(tmp_path):
    env, _, _ = _setup(tmp_path)
    r = _run(env, "start")
    assert r.returncode == 0, r.stderr
    assert "local node ready" in r.stdout
    os.remove(Path(env["ANDROID_HOME"]) / "emulator" / "emulator")
    r = _run(env, "start")
    assert r.returncode == 1
    assert "missing" in r.stderr


def test_shot_boots_installs_and_captures_locally(tmp_path):
    env, node_home, state = _setup(tmp_path)
    apk = tmp_path / "app-debug.apk"
    apk.write_bytes(b"apk")
    try:
        r = _run(env, "shot", str(apk), "0")
        assert r.returncode == 0, r.stderr
        shot = Path(r.stdout.strip().splitlines()[-1])
        assert shot.parent == tmp_path / "shots"
        assert shot.name.startswith("com.example.demo-") and shot.suffix == ".png"
        assert shot.read_bytes() == b"PNG"
        assert (node_home / "under-test.apk").read_bytes() == b"apk"
        argv = (state / "emulator.argv").read_text()
        assert "-avd api30" in argv and "-no-window" in argv
        # node-side `~` is the node home, so ~/android resolves to the SDK
        assert (state / "emulator.home").read_text().strip() == str(node_home)
        assert "monkey -p com.example.demo" in (state / "launch.log").read_text()
    finally:
        r = _run(env, "stop")
    assert r.returncode == 0, r.stderr
    assert not (state / "booted").exists()


def test_ssh_push_pull_use_the_node_home(tmp_path):
    env, node_home, _ = _setup(tmp_path)
    src = tmp_path / "in.txt"
    src.write_text("hello", encoding="utf-8")
    assert _run(env, "push", str(src), "~/sub/in.txt").returncode == 0
    assert (node_home / "sub" / "in.txt").read_text() == "hello"
    r = _run(env, "ssh", 'echo "$HOME"; ls ~/android/platform-tools; cat ~/sub/in.txt')
    assert r.returncode == 0, r.stderr
    lines = r.stdout.split()
    assert lines[0] == str(node_home)
    assert "adb" in lines and "hello" in lines
    out = tmp_path / "out.txt"
    assert _run(env, "pull", "~/sub/in.txt", str(out)).returncode == 0
    assert out.read_text() == "hello"
