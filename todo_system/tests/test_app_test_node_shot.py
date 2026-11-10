"""``scripts/app-test-node shot`` — cold-boot survives a dropped ssh channel.

The `shot` subcommand used to boot the emulator, wait for it, install, launch
and screencap inside **one** ``gcloud compute ssh --command`` call. A cold
boot takes 40s+; if the ssh channel drops mid-wait (observed as rc 255), the
whole call failed even though the emulator kept running on the node. The fix
splits the boot into a fire-and-forget launch (its own short ssh call that
returns immediately) plus a host-side polling loop of short ssh calls, and
wraps every short call in a one-shot retry (`_ssh_retry`/`_scp_retry`) so a
transient rc 255 on any single call does not fail the run.

Both cases here drive the real script through a fake ``gcloud`` (the
``GCLOUD`` env override) that never touches the network or a real node —
``test_success_survives_transient_rc255`` injects rc 255 on the first boot
poll *and* the first install/launch/screencap call, and asserts `shot` still
exits 0 and produces the screenshot; ``test_boot_timeout_exits_nonzero``
starves the poll so ``sys.boot_completed`` never reports 1 and asserts the
run exits non-zero with the emulator log tail on stderr, using the
``APP_TEST_BOOT_TIMEOUT``/``APP_TEST_POLL_INTERVAL`` overrides so the case
finishes in well under a second instead of the real 400s cap.

No literal from the identifier gate's pattern may appear in this file; it
sits inside the scanned tree.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "app-test-node"
_TIMEOUT = 30

_FAKE_GCLOUD = '''#!/usr/bin/env python3
import json
import os
import shutil
import sys
from pathlib import Path

home = Path(os.environ["FAKE_REMOTE_HOME"])
home.mkdir(parents=True, exist_ok=True)
state_path = Path(os.environ["FAKE_STATE_FILE"])
state = json.loads(state_path.read_text()) if state_path.exists() else {}


def save():
    state_path.write_text(json.dumps(state))


def next_rc(key, env_var):
    seq = [v for v in os.environ.get(env_var, "").split(",") if v]
    idx = state.get(key, 0)
    state[key] = idx + 1
    if not seq:
        return "0"
    return seq[idx] if idx < len(seq) else seq[-1]


def resolve(p):
    if ":" in p:
        _node, _, rel = p.partition(":")
        rel = rel[2:] if rel.startswith("~/") else rel.lstrip("~")
        return home / rel
    return Path(p)


args = sys.argv[1:]
if args[:2] == ["compute", "ssh"]:
    cmd = ""
    for i, a in enumerate(args):
        if a == "--command":
            cmd = args[i + 1]
            break
    if "getprop sys.boot_completed" in cmd:
        rc = next_rc("poll", "FAKE_POLL_RC_SEQUENCE")
        save()
        if rc == "255":
            sys.exit(255)
        print(rc)
        sys.exit(0)
    elif "setsid sg kvm" in cmd:
        (home / "emu.log").write_text("fake emulator log tail\\n")
        save()
        sys.exit(0)
    elif "aapt2 dump packagename" in cmd:
        print("com.example.fake")
        sys.exit(0)
    elif "install -r" in cmd:
        rc = next_rc("install", "FAKE_INSTALL_RC_SEQUENCE")
        # Record the install/launch script verbatim so a test can assert WHICH
        # launch line the script built (monkey vs am start).
        (home / "launch.cmd").write_text(cmd)
        save()
        if rc == "255":
            sys.exit(255)
        (home / "shot.png").write_bytes(b"\\x89PNG\\r\\n\\x1a\\nFAKE")
        (home / "shot.pkg").write_text("com.example.fake\\n")
        sys.exit(0)
    elif "cat ~/shot.pkg" in cmd:
        pkg_file = home / "shot.pkg"
        print(pkg_file.read_text().strip() if pkg_file.exists() else "")
        sys.exit(0)
    elif "tail -20 ~/emu.log" in cmd:
        log_file = home / "emu.log"
        sys.stderr.write(log_file.read_text() if log_file.exists() else "")
        sys.exit(0)
    else:
        sys.stderr.write(f"fake gcloud: unrecognized ssh command: {cmd}\\n")
        sys.exit(1)
elif args[:2] == ["compute", "scp"]:
    positional = []
    for a in args[2:]:
        if a.startswith("--"):
            break
        positional.append(a)
    src, dst = positional[0], positional[1]
    src_path, dst_path = resolve(src), resolve(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src_path, dst_path)
    sys.exit(0)
else:
    sys.stderr.write(f"fake gcloud: unrecognized args: {args}\\n")
    sys.exit(1)
'''


def _write_fake_gcloud(bin_path: Path) -> None:
    bin_path.write_text(_FAKE_GCLOUD, encoding="utf-8")
    os.chmod(bin_path, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)


def _run_shot(tmp_path: Path, apk: Path, env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    fake_gcloud = tmp_path / "fake-gcloud"
    _write_fake_gcloud(fake_gcloud)
    out_dir = tmp_path / "shots"
    env = {
        **os.environ,
        "GCLOUD": str(fake_gcloud),
        "APP_TEST_OUT": str(out_dir),
        "FAKE_REMOTE_HOME": str(tmp_path / "remote_home"),
        "FAKE_STATE_FILE": str(tmp_path / "state.json"),
        # Pinned empty so a stray value in the caller's environment cannot flip a
        # case between the monkey and the am-start launch.
        "APP_TEST_LAUNCH_EXTRAS": "",
        "APP_TEST_ACTIVITY": "",
        **env_overrides,
    }
    return subprocess.run(
        [str(SCRIPT), "shot", str(apk)],
        env=env,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT,
    )


def test_success_survives_transient_rc255(tmp_path):
    """Boot poll fails once (rc 255) before the emulator is up, and the
    install/launch/screencap call fails once (rc 255) before succeeding.
    `_ssh_retry` must absorb both and `shot` must still exit 0."""
    apk = tmp_path / "under-test.apk"
    apk.write_bytes(b"fake apk bytes")

    result = _run_shot(
        tmp_path,
        apk,
        {
            # initial check: 255 (retried) -> 0 (not booted); first poll
            # inside the wait loop: 255 (retried) -> 1 (booted).
            "FAKE_POLL_RC_SEQUENCE": "255,0,255,1",
            "FAKE_INSTALL_RC_SEQUENCE": "255,0",
            "APP_TEST_POLL_INTERVAL": "0.1",
            "APP_TEST_BOOT_TIMEOUT": "30",
        },
    )

    assert result.returncode == 0, result.stderr
    dst = result.stdout.strip()
    assert dst, result.stderr
    out_file = Path(dst)
    assert out_file.exists()
    assert out_file.read_bytes() == b"\x89PNG\r\n\x1a\nFAKE"
    assert "com.example.fake" in out_file.name


def _launched_with(tmp_path, env_overrides: dict[str, str]) -> str:
    """Run `shot` to completion and return the install/launch script the node
    was actually asked to run."""
    apk = tmp_path / "under-test.apk"
    apk.write_bytes(b"fake apk bytes")
    result = _run_shot(tmp_path, apk, {"FAKE_POLL_RC_SEQUENCE": "1", **env_overrides})
    assert result.returncode == 0, result.stderr
    return (tmp_path / "remote_home" / "launch.cmd").read_text()


def test_shot_launches_with_monkey_when_no_extras_are_set(tmp_path):
    """Regression gate for the whole feature: with no launch env the script must
    still build the ORIGINAL monkey launcher intent, untouched."""
    cmd = _launched_with(tmp_path, {})

    assert '$A shell monkey -p "com.example.fake" -c android.intent.category.LAUNCHER 1' in cmd
    assert "am start" not in cmd


def test_shot_launches_with_am_start_when_extras_are_set(tmp_path):
    cmd = _launched_with(tmp_path, {
        "APP_TEST_LAUNCH_EXTRAS": "--es session_token tok --ez skip_onboarding true",
        "APP_TEST_ACTIVITY": ".MainActivity",
    })

    assert '$A shell am start -n "com.example.fake/.MainActivity" ' in cmd
    assert "--es session_token tok --ez skip_onboarding true" in cmd
    assert "monkey" not in cmd


def test_shot_keeps_monkey_when_only_one_of_the_two_vars_is_set(tmp_path):
    """The engine sets both or neither; a half-set environment must not produce
    an `am start` with a missing component."""
    only_extras = _launched_with(tmp_path, {"APP_TEST_LAUNCH_EXTRAS": "--ez k true"})
    assert "monkey" in only_extras and "am start" not in only_extras


def test_shot_keeps_monkey_when_only_the_activity_is_set(tmp_path):
    only_activity = _launched_with(tmp_path, {"APP_TEST_ACTIVITY": ".MainActivity"})
    assert "monkey" in only_activity and "am start" not in only_activity


def test_boot_timeout_exits_nonzero(tmp_path):
    """sys.boot_completed never reports 1: the poll loop must exit non-zero
    once elapsed time passes the (test-shortened) boot timeout, and show the
    emulator log tail on stderr — not hang or spin forever."""
    apk = tmp_path / "under-test.apk"
    apk.write_bytes(b"fake apk bytes")

    result = _run_shot(
        tmp_path,
        apk,
        {
            "FAKE_POLL_RC_SEQUENCE": "0",
            "APP_TEST_POLL_INTERVAL": "0.2",
            "APP_TEST_BOOT_TIMEOUT": "1",
        },
    )

    assert result.returncode != 0
    assert "boot timeout" in result.stderr
    assert "fake emulator log tail" in result.stderr
