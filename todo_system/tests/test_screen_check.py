"""Unit tests for the opt-in post-verify app screen check.

Every external effect — the gradle debug build and the ``scripts/app-test-node``
start/shot/stop — is driven through a fake ``run``, so nothing shells out and no
gcloud / gradle / emulator is ever touched. The repo, run dir, lock file and node
script are all under ``tmp_path``; the node script file is created but never
executed (the injected ``run`` intercepts every call).

The heart of the suite is AC #2: on EVERY exit path (success, APK build failure,
shot failure/preemption, cap-breach timeout, and an arbitrary exception) the node
is stopped — ``"stop"`` appears in the recorded subcommands — and ``run_check``
never raises.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from todo_system.orchestrator import lease
from todo_system.orchestrator import screen_check
from todo_system.orchestrator import stages
from todo_system.orchestrator.runner import StagedExecutor


class FakeRun:
    """Records every call and dispatches canned results by argv. Signature
    matches ``screen_check``'s default runner: ``(argv, *, cwd, env, timeout)``.

    ``build_rc`` / ``shot_rc`` set the gradle / shot return codes. ``raise_on`` is
    a token (``"gradle"`` | ``"start"`` | ``"shot"`` | ``"stop"``) that, when hit,
    raises ``raise_exc`` instead of returning — used to drive the timeout and
    arbitrary-exception paths. A successful ``shot`` writes a PNG under the call's
    ``APP_TEST_OUT`` and echoes its path on the last stdout line, mimicking the
    real node script."""

    def __init__(self, *, build_rc: int = 0, shot_rc: int = 0,
                 raise_on: str | None = None, raise_exc: BaseException | None = None):
        self.calls: list[dict] = []
        self.build_rc = build_rc
        self.shot_rc = shot_rc
        self.raise_on = raise_on
        self.raise_exc = raise_exc
        self._shot_n = 0

    @staticmethod
    def _token(argv: list[str]) -> str:
        if argv[:1] == ["./gradlew"]:
            return "gradle"
        if len(argv) >= 3 and argv[0] == "bash":
            return argv[2]            # start / shot / stop
        return argv[0] if argv else ""

    def __call__(self, argv, *, cwd=None, env=None, timeout=None):
        argv = list(argv)
        self.calls.append({"argv": argv, "cwd": cwd,
                           "env": dict(env) if env else None, "timeout": timeout})
        token = self._token(argv)
        if self.raise_on is not None and token == self.raise_on:
            assert self.raise_exc is not None
            raise self.raise_exc

        def cp(rc=0, out=""):
            return subprocess.CompletedProcess(argv, rc, out, "")

        if token == "gradle":
            return cp(self.build_rc,
                      "BUILD SUCCESSFUL" if self.build_rc == 0 else "BUILD FAILED")
        if token == "shot":
            self._shot_n += 1
            if self.shot_rc != 0:
                return cp(self.shot_rc, "")
            out_dir = Path(env["APP_TEST_OUT"])
            out_dir.mkdir(parents=True, exist_ok=True)
            dst = out_dir / f"com.example.app-2026T{self._shot_n:06d}.png"
            dst.write_bytes(b"\x89PNG\r\n")
            return cp(0, f"install: Success\n{dst}")
        return cp(0, "")              # start / stop / fallthrough

    def subcmds(self) -> list[str]:
        return [self._token(c["argv"]) for c in self.calls]

    def stop_calls(self) -> list[dict]:
        return [c for c in self.calls if self._token(c["argv"]) == "stop"]


def _mk_env(tmp_path, *, with_apk: bool = True):
    """A fake app repo (gradlew [+ a prebuilt debug APK]), a run dir, a lock path
    and a (never-executed) node script — all under ``tmp_path``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    if with_apk:
        apk = repo / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
        apk.parent.mkdir(parents=True)
        apk.write_bytes(b"PK\x03\x04")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock_path = tmp_path / "node.lock"
    node_script = tmp_path / "app-test-node"
    node_script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    return repo, run_dir, lock_path, node_script


def _run_check(repo, run_dir, lock_path, node_script, fake, *, cap_s=600):
    return screen_check.run_check(
        repo, run_dir, cap_s=cap_s, lock_path=lock_path,
        node_script=node_script, run=fake)


# ---------------------------------------------------------------------------
# AC #2 — every exit path calls stop
# ---------------------------------------------------------------------------

def test_success_captures_screen_and_always_stops(tmp_path):
    repo, run_dir, lock_path, node_script = _mk_env(tmp_path)
    fake = FakeRun()
    note = _run_check(repo, run_dir, lock_path, node_script, fake)

    assert note.startswith("화면 확인됨")
    subs = fake.subcmds()
    assert "start" in subs and "shot" in subs and "stop" in subs
    shots = sorted((run_dir / "screens").glob("*.png"))
    assert len(shots) == 1
    # the note points at the pulled screenshot, relative to the run dir
    assert "screens/" in note


def test_apk_build_failure_skips_and_stops(tmp_path):
    repo, run_dir, lock_path, node_script = _mk_env(tmp_path)
    fake = FakeRun(build_rc=1)
    note = _run_check(repo, run_dir, lock_path, node_script, fake)

    assert "APK 빌드 실패" in note
    assert "stop" in fake.subcmds()
    # a build failure never reached the node, but stop is still called (idempotent)
    assert "shot" not in fake.subcmds()


def test_missing_apk_artifact_skips_and_stops(tmp_path):
    repo, run_dir, lock_path, node_script = _mk_env(tmp_path, with_apk=False)
    fake = FakeRun(build_rc=0)          # build "succeeds" but produces no APK
    note = _run_check(repo, run_dir, lock_path, node_script, fake)

    assert "APK 산출물 없음" in note
    assert "stop" in fake.subcmds()


def test_shot_failure_retries_once_then_skips_and_stops(tmp_path):
    repo, run_dir, lock_path, node_script = _mk_env(tmp_path)
    fake = FakeRun(shot_rc=1)           # every shot fails → preemption retry path
    note = _run_check(repo, run_dir, lock_path, node_script, fake)

    assert "1회 재시도" in note
    subs = fake.subcmds()
    assert subs.count("start") == 2     # initial start + one restart on retry
    assert subs.count("shot") == 2      # exactly one retry
    assert "stop" in subs


def test_cap_breach_timeout_forces_stop(tmp_path):
    repo, run_dir, lock_path, node_script = _mk_env(tmp_path)
    fake = FakeRun(raise_on="gradle",
                   raise_exc=subprocess.TimeoutExpired(cmd=["./gradlew"], timeout=1))
    note = _run_check(repo, run_dir, lock_path, node_script, fake, cap_s=600)

    assert "상한" in note and "강제 stop" in note
    assert "10분" in note               # cap_s=600 → 10분
    stops = fake.stop_calls()
    assert stops, "a cap breach must still stop the node"
    # the stop call uses its OWN fixed timeout, not the (expired) cap deadline
    assert stops[0]["timeout"] == screen_check._STOP_TIMEOUT_S


def test_arbitrary_exception_is_absorbed_and_stops(tmp_path):
    repo, run_dir, lock_path, node_script = _mk_env(tmp_path)
    fake = FakeRun(raise_on="gradle", raise_exc=RuntimeError("boom"))
    # run_check must NOT propagate the exception — it can only ever SKIP
    note = _run_check(repo, run_dir, lock_path, node_script, fake)

    assert "노드 오류: RuntimeError" in note
    assert "stop" in fake.subcmds()


# ---------------------------------------------------------------------------
# AC #3 — single-node mutual exclusion via the file lock
# ---------------------------------------------------------------------------

def test_lock_contention_skips_with_zero_node_calls(tmp_path):
    repo, run_dir, lock_path, node_script = _mk_env(tmp_path)
    # a different holder squats the node lock with a long TTL (not reclaimable)
    squatter = lease.Lease(str(lock_path), holder="otherhost:999999", ttl=100_000)
    assert squatter.acquire()
    try:
        fake = FakeRun()
        note = _run_check(repo, run_dir, lock_path, node_script, fake)
        assert "락 획득 실패" in note
        assert fake.calls == []          # ZERO node calls when the lock is held
    finally:
        squatter.release()


def test_lock_is_released_after_a_run(tmp_path):
    repo, run_dir, lock_path, node_script = _mk_env(tmp_path)
    _run_check(repo, run_dir, lock_path, node_script, FakeRun())
    # a second run (and any later run) can still take the lock
    note = _run_check(repo, run_dir, lock_path, node_script, FakeRun())
    assert note.startswith("화면 확인됨")


# ---------------------------------------------------------------------------
# AC #1 / #5 — opt-in gating + cap parsing (pure)
# ---------------------------------------------------------------------------

_APP_DIFF = ["app/src/main/java/Main.kt", "README.md"]
_NON_APP_DIFF = ["docs/x.md", "todo_system/orchestrator/runner.py"]


def test_requested_defaults_on_for_an_app_diff():
    """The new contract: no flag + a diff under app/ → ON (the opt-in flag was
    never set by any task, so the check had never run — root cause 1)."""
    assert screen_check.requested({}, _APP_DIFF) is True
    assert screen_check.requested(None, _APP_DIFF) is True


def test_requested_defaults_off_without_an_app_diff():
    assert screen_check.requested({}, _NON_APP_DIFF) is False
    assert screen_check.requested({}, []) is False


def test_requested_is_off_when_the_diff_could_not_be_read():
    """``diff_paths=None`` = "no diff available". That resolves OFF: guessing ON
    would wake the billed node on every run of every repo. A bare one-argument
    call (the old signature) therefore behaves exactly as it used to."""
    assert screen_check.requested({}, None) is False
    assert screen_check.requested({}) is False
    assert screen_check.requested(None) is False


def test_an_explicit_flag_always_beats_the_diff():
    # true forces it on even with a diff that never touches the app module
    assert screen_check.requested({"screen_check": True}, _NON_APP_DIFF) is True
    assert screen_check.requested({"screen_check": "true"}, None) is True
    # false forces it off even with an app diff
    assert screen_check.requested({"screen_check": False}, _APP_DIFF) is False
    assert screen_check.requested({"screen_check": "false"}, _APP_DIFF) is False
    assert screen_check.requested({"screen_check": "off"}, _APP_DIFF) is False
    # an empty frontmatter value is "unset", not "false"
    assert screen_check.requested({"screen_check": "  "}, _APP_DIFF) is True


def test_explicitly_disabled_distinguishes_unset_from_false():
    assert screen_check.explicitly_disabled({"screen_check": False}) is True
    assert screen_check.explicitly_disabled({"screen_check": "false"}) is True
    assert screen_check.explicitly_disabled({}) is False          # unset ≠ opted out
    assert screen_check.explicitly_disabled(None) is False
    assert screen_check.explicitly_disabled({"screen_check": True}) is False


def test_touches_app_paths_prefix_matching():
    t = screen_check.touches_app_paths
    assert t(["app/src/Main.kt"]) is True
    assert t(["app/build.gradle.kts"]) is True
    # a sibling directory that merely starts with the same letters is NOT a match
    assert t(["application-notes/x.md"]) is False
    assert t(["appcompat.md"]) is False
    assert t([], ("app/",)) is False
    assert t(None) is False
    # a config prefix a human wrote loosely still matches
    assert t(["composeApp/src/Main.kt"], ("./composeApp",)) is True
    assert t(["x"], ("",)) is False
    assert t([None, 7, "", "app/a.kt"]) is True       # junk entries are skipped


def test_app_paths_default_and_repo_override():
    assert screen_check.app_paths(None) == ("app/",)
    assert screen_check.app_paths({}) == ("app/",)
    assert screen_check.app_paths({"screen_check": {"app_paths": ["composeApp/"]}}) == ("composeApp/",)
    assert screen_check.app_paths({"smoke": {"app_paths": ["lib/", "app/"]}}) == ("lib/", "app/")
    # junk falls back to the default rather than matching nothing
    assert screen_check.app_paths({"screen_check": {"app_paths": []}}) == ("app/",)
    assert screen_check.app_paths({"screen_check": {"app_paths": "app/"}}) == ("app/",)
    assert screen_check.app_paths({"screen_check": {"app_paths": [None, "  "]}}) == ("app/",)


def test_requested_honors_a_repos_configured_app_paths():
    paths = screen_check.app_paths({"screen_check": {"app_paths": ["composeApp/"]}})
    assert screen_check.requested({}, ["composeApp/src/Main.kt"], paths) is True
    # …and the default set no longer matches that repo's app module
    assert screen_check.requested({}, ["composeApp/src/Main.kt"]) is False


def test_is_app_repo(tmp_path):
    assert screen_check.is_app_repo(tmp_path) is False
    (tmp_path / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    assert screen_check.is_app_repo(tmp_path) is True


def test_cap_seconds_default_frontmatter_and_env(monkeypatch):
    monkeypatch.delenv(screen_check._CAP_ENV, raising=False)
    assert screen_check.cap_seconds({}) == 1200
    assert screen_check.cap_seconds({"screen_check_cap_min": 5}) == 300
    assert screen_check.cap_seconds({"screen_check_cap_min": 0}) == 1200     # non-positive → default
    assert screen_check.cap_seconds({"screen_check_cap_min": "x"}) == 1200   # unparseable → default
    monkeypatch.setenv(screen_check._CAP_ENV, "42")
    assert screen_check.cap_seconds({"screen_check_cap_min": 5}) == 42       # env wins


def test_gradle_task_default_and_override():
    assert screen_check.gradle_task({}) == "assembleDebug"
    assert screen_check.gradle_task({"screen_check_gradle_task": ":app:assembleDebug"}) == ":app:assembleDebug"
    assert screen_check.gradle_task({"screen_check_gradle_task": "  "}) == "assembleDebug"


# ---------------------------------------------------------------------------
# AC #4 — verify.md note + verdict invariance
# ---------------------------------------------------------------------------

def test_append_note_does_not_change_verdict(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    verify_md = run_dir / "verify.md"
    verify_md.write_text("---\noverall_status: pass\n---\n\n# Verify\n\nlocal gates green\n",
                         encoding="utf-8")

    screen_check.append_note(run_dir, "화면 확인됨 — screens/com.example.app-x.png")

    text = verify_md.read_text(encoding="utf-8")
    assert "실기 화면 확인 (screen_check)" in text
    assert "화면 확인됨 — screens/com.example.app-x.png" in text
    # the appended note must NOT flip the first-match verdict in the top frontmatter
    assert stages.parse_verify_verdict(text) == "PASS"


def test_append_note_creates_verify_md_when_absent(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    screen_check.append_note(run_dir, "화면 확인 불가(앱 리포 아님 — gradlew 없음)")
    assert (run_dir / "verify.md").exists()
    assert "gradlew 없음" in (run_dir / "verify.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Phase 3 wiring — StagedExecutor._app_screen_check (fail-open, verdict-safe)
# ---------------------------------------------------------------------------

def _verify_md(run_dir: Path, body: str = "# Verify\n\nlocal gates green\n") -> Path:
    p = run_dir / "verify.md"
    p.write_text(f"---\noverall_status: pass\n---\n\n{body}", encoding="utf-8")
    return p


def test_wiring_appends_note_without_changing_verdict(tmp_path, monkeypatch):
    repo, run_dir, _, _ = _mk_env(tmp_path)
    verify_md = _verify_md(run_dir)
    monkeypatch.setattr(screen_check, "run_check",
                        lambda *a, **k: "화면 확인됨 — screens/com.example.app-x.png")

    StagedExecutor()._app_screen_check(repo, {"screen_check": True}, run_dir,
                                       lambda _m: None)

    text = verify_md.read_text(encoding="utf-8")
    assert "화면 확인됨 — screens/com.example.app-x.png" in text
    assert stages.parse_verify_verdict(text) == "PASS"


def test_wiring_is_fail_open_when_run_check_raises(tmp_path, monkeypatch):
    repo, run_dir, _, _ = _mk_env(tmp_path)
    verify_md = _verify_md(run_dir)

    def boom(*a, **k):
        raise RuntimeError("node exploded")

    monkeypatch.setattr(screen_check, "run_check", boom)
    msgs: list[str] = []

    # must NOT raise — a screen check can never flip the verify outcome
    StagedExecutor()._app_screen_check(repo, {"screen_check": True}, run_dir, msgs.append)

    assert any("screen check skipped" in m for m in msgs)
    # the verdict is still readable and unchanged
    assert stages.parse_verify_verdict(verify_md.read_text(encoding="utf-8")) == "PASS"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _real_repo(tmp_path) -> Path:
    """A real git repo whose ``worker/x`` branch changed one file under ``app/``,
    with ``refs/remotes/origin/main`` pointing at the branch point (no network —
    ``update-ref`` fabricates the tracking ref the diff base names)."""
    repo = tmp_path / "gitrepo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _git(repo, "checkout", "-q", "-b", "worker/x")
    src = repo / "app" / "src" / "Main.kt"
    src.parent.mkdir(parents=True)
    src.write_text("fun main() {}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    return repo


def test_changed_paths_reads_the_runs_own_diff(tmp_path):
    repo = _real_repo(tmp_path)
    assert StagedExecutor()._changed_paths(repo, "worker/x") == ["app/src/Main.kt"]


def test_changed_paths_ignores_commits_that_landed_on_the_base_meanwhile(tmp_path):
    """The three-dot diff must report only what THIS branch changed. With a
    two-dot ``origin/main..worker/x`` diff, a file changed on main after the
    branch point shows up here too — and an unrelated ``app/`` commit on main
    would then turn the check on for a docs-only run."""
    repo = _real_repo(tmp_path)
    _git(repo, "checkout", "-q", "main")
    (repo / "app" / "other.kt").parent.mkdir(parents=True, exist_ok=True)
    (repo / "app" / "other.kt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "someone else's app change")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "checkout", "-q", "worker/x")

    assert StagedExecutor()._changed_paths(repo, "worker/x") == ["app/src/Main.kt"]


def test_changed_paths_is_none_when_unreadable(tmp_path):
    repo = _real_repo(tmp_path)
    ex = StagedExecutor()
    assert ex._changed_paths(repo, "worker/does-not-exist") is None   # bad rc
    assert ex._changed_paths(repo, None) is None                      # no branch
    assert ex._changed_paths(tmp_path / "not-a-repo", "worker/x") is None


def test_changed_paths_logs_only_a_count(tmp_path):
    """The run log gets the number of changed paths, never the list."""
    repo = _real_repo(tmp_path)
    msgs: list[str] = []
    StagedExecutor()._changed_paths(repo, "worker/x", msgs.append)
    assert any("1 changed path" in m for m in msgs)
    assert not any("Main.kt" in m for m in msgs)


def _stub_gate(monkeypatch, *, diff: list[str] | None, config: dict | None = None):
    """Pin the two inputs the gate reads from git / the vault, and record every
    ``run_check`` call so "ZERO node calls" can be asserted directly."""
    calls: list[dict] = []
    monkeypatch.setattr(StagedExecutor, "_changed_paths",
                        lambda self, repo, branch, progress=None: diff)
    monkeypatch.setattr(StagedExecutor, "_screen_check_config",
                        lambda self, payload: config)

    def fake_run_check(*a, **k):
        calls.append(dict(k))
        return "화면 확인됨 — screens/com.example.app-x.png"

    monkeypatch.setattr(screen_check, "run_check", fake_run_check)
    return calls


def test_wiring_runs_by_default_when_the_diff_touches_the_app_module(tmp_path, monkeypatch):
    """The headline change: no flag at all, but an app repo whose diff touches
    ``app/`` → the check runs and leaves exactly one note."""
    repo, run_dir, _, _ = _mk_env(tmp_path)
    verify_md = _verify_md(run_dir)
    calls = _stub_gate(monkeypatch, diff=["app/src/main/java/Main.kt"])

    StagedExecutor()._app_screen_check(repo, {}, run_dir, lambda _m: None,
                                       branch="worker/x")

    assert len(calls) == 1
    text = verify_md.read_text(encoding="utf-8")
    assert text.count("실기 화면 확인 (screen_check)") == 1
    assert stages.parse_verify_verdict(text) == "PASS"


def test_wiring_noop_when_the_diff_misses_the_app_module(tmp_path, monkeypatch):
    """AC #1 regression guard: an unflagged task whose diff never touches the app
    module makes ZERO node calls and leaves verify.md byte-identical."""
    repo, run_dir, _, _ = _mk_env(tmp_path)
    verify_md = _verify_md(run_dir)
    original = verify_md.read_text(encoding="utf-8")
    calls = _stub_gate(monkeypatch, diff=["docs/x.md"])

    StagedExecutor()._app_screen_check(repo, {}, run_dir, lambda _m: None,
                                       branch="worker/x")

    assert calls == []
    assert verify_md.read_text(encoding="utf-8") == original


def test_wiring_noop_when_the_diff_cannot_be_read(tmp_path, monkeypatch):
    """An unreadable diff (``_changed_paths`` → None) must not run the check:
    guessing ON would wake the billed node on every run."""
    repo, run_dir, _, _ = _mk_env(tmp_path)
    verify_md = _verify_md(run_dir)
    original = verify_md.read_text(encoding="utf-8")
    calls = _stub_gate(monkeypatch, diff=None)

    StagedExecutor()._app_screen_check(repo, {}, run_dir, lambda _m: None,
                                       branch="worker/x")

    assert calls == []
    assert verify_md.read_text(encoding="utf-8") == original


def test_wiring_noop_on_a_non_app_repo_without_a_flag(tmp_path, monkeypatch):
    """An unflagged NON-app task is silent — not even the "앱 리포 아님" note, and
    the diff is never collected (the is_app_repo gate comes first)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    verify_md = _verify_md(run_dir)
    original = verify_md.read_text(encoding="utf-8")
    non_app = tmp_path / "vaultish"          # no gradlew → not an app repo
    non_app.mkdir()
    diffed: list[str] = []
    monkeypatch.setattr(StagedExecutor, "_changed_paths",
                        lambda self, repo, branch, progress=None: diffed.append("x") or None)

    StagedExecutor()._app_screen_check(non_app, {}, run_dir, lambda _m: None,
                                       branch="worker/x")

    assert verify_md.read_text(encoding="utf-8") == original
    assert diffed == []                      # the diff was never even collected


def test_wiring_opt_out_skips_before_any_work(tmp_path, monkeypatch):
    """``screen_check: false`` + an app diff → zero node calls, zero git calls,
    verify.md byte-identical."""
    repo, run_dir, _, _ = _mk_env(tmp_path)
    verify_md = _verify_md(run_dir)
    original = verify_md.read_text(encoding="utf-8")
    diffed: list[str] = []
    monkeypatch.setattr(StagedExecutor, "_changed_paths",
                        lambda self, repo, branch, progress=None: diffed.append("x") or None)
    calls = _stub_gate(monkeypatch, diff=["app/src/Main.kt"])

    StagedExecutor()._app_screen_check(repo, {"screen_check": False}, run_dir,
                                       lambda _m: None, branch="worker/x")

    assert calls == []
    assert diffed == []
    assert verify_md.read_text(encoding="utf-8") == original


def test_wiring_flagged_non_app_repo_notes_skip(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    verify_md = _verify_md(run_dir)
    non_app = tmp_path / "vaultish"          # no gradlew → not an app repo
    non_app.mkdir()

    StagedExecutor()._app_screen_check(non_app, {"screen_check": True}, run_dir,
                                       lambda _m: None)

    text = verify_md.read_text(encoding="utf-8")
    assert "앱 리포 아님" in text
    assert stages.parse_verify_verdict(text) == "PASS"


def test_wiring_explicit_true_runs_without_reading_the_diff(tmp_path, monkeypatch):
    """``screen_check: true`` preserves the old opt-in behaviour exactly, and
    skips the git call since an explicit flag wins anyway."""
    repo, run_dir, _, _ = _mk_env(tmp_path)
    _verify_md(run_dir)
    diffed: list[str] = []
    monkeypatch.setattr(StagedExecutor, "_changed_paths",
                        lambda self, repo, branch, progress=None: diffed.append("x") or None)
    calls = _stub_gate(monkeypatch, diff=None)

    StagedExecutor()._app_screen_check(repo, {"screen_check": True}, run_dir,
                                       lambda _m: None, branch="worker/x")

    assert len(calls) == 1
    assert diffed == []


def test_wiring_passes_the_repo_config_through_to_run_check(tmp_path, monkeypatch):
    """The repo's vault entry reaches ``run_check(config=…)`` — that is the only
    route by which launch extras can be applied."""
    repo, run_dir, _, _ = _mk_env(tmp_path)
    _verify_md(run_dir)
    entry = {"screen_check": {"activity": ".MainActivity",
                              "launch_extras": [{"type": "bool", "key": "demo", "value": True}]}}
    calls = _stub_gate(monkeypatch, diff=["app/src/Main.kt"], config=entry)

    StagedExecutor()._app_screen_check(repo, {}, run_dir, lambda _m: None,
                                       branch="worker/x")

    assert calls[0]["config"] == entry


def test_wiring_uses_the_repos_configured_app_paths(tmp_path, monkeypatch):
    """A repo whose app module is not ``app/`` still gets the default-ON check,
    via its own ``screen_check.app_paths``."""
    repo, run_dir, _, _ = _mk_env(tmp_path)
    _verify_md(run_dir)
    calls = _stub_gate(monkeypatch, diff=["composeApp/src/Main.kt"],
                       config={"screen_check": {"app_paths": ["composeApp/"]}})

    StagedExecutor()._app_screen_check(repo, {}, run_dir, lambda _m: None,
                                       branch="worker/x")

    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Launch extras on the screen_check path (node env) — no secret in any message
# ---------------------------------------------------------------------------

def _extras_cfg(tmp_path, *, activity=".MainActivity"):
    rel = "/".join([".secrets", "demo-app.json"])
    p = tmp_path / "vault" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"session_token": "s3cret-token-value"}', encoding="utf-8")
    cfg = {"screen_check": {
        "activity": activity,
        "launch_extras": [{"type": "string", "key": "session_token",
                           "value_from_secret": f"{rel}#session_token"}]}}
    return cfg, tmp_path / "vault"


def test_extras_reach_the_node_env_and_never_the_progress_log(tmp_path, monkeypatch):
    repo, run_dir, lock_path, node_script = _mk_env(tmp_path)
    cfg, vault = _extras_cfg(tmp_path)
    monkeypatch.setattr(screen_check, "_VAULT_DATA_ROOT", vault)
    fake = FakeRun()
    msgs: list[str] = []

    note = screen_check.run_check(
        repo, run_dir, cap_s=600, lock_path=lock_path, node_script=node_script,
        run=fake, config=cfg, progress=msgs.append)

    assert note.startswith("화면 확인됨")
    shot = [c for c in fake.calls if c["argv"][2:3] == ["shot"]][0]
    assert shot["env"]["APP_TEST_LAUNCH_EXTRAS"] == "--es session_token s3cret-token-value"
    assert shot["env"]["APP_TEST_ACTIVITY"] == ".MainActivity"
    # the secret value must not appear in the run log or the verify.md note
    assert "s3cret-token-value" not in "\n".join(msgs)
    assert "s3cret-token-value" not in note


def test_no_extras_declared_leaves_the_node_env_without_launch_vars(tmp_path, monkeypatch):
    """Regression guard: a repo declaring nothing gets no launch vars at all, so
    the node script keeps its original ``monkey`` line."""
    repo, run_dir, lock_path, node_script = _mk_env(tmp_path)
    monkeypatch.delenv("APP_TEST_LAUNCH_EXTRAS", raising=False)
    monkeypatch.delenv("APP_TEST_ACTIVITY", raising=False)
    fake = FakeRun()

    for config in (None, {}, {"screen_check": {}}):
        fake.calls.clear()
        screen_check.run_check(repo, run_dir, cap_s=600, lock_path=lock_path,
                               node_script=node_script, run=fake, config=config)
        shot = [c for c in fake.calls if c["argv"][2:3] == ["shot"]][0]
        assert "APP_TEST_LAUNCH_EXTRAS" not in shot["env"], config
        assert "APP_TEST_ACTIVITY" not in shot["env"], config


def test_missing_secret_notes_the_skip_without_the_value(tmp_path, monkeypatch):
    """A declared-but-unreadable secret is a note, not a crash, and the launch
    falls back to ``monkey`` (no launch vars in the node env)."""
    repo, run_dir, lock_path, node_script = _mk_env(tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()                              # the secret file is NEVER written
    monkeypatch.setattr(screen_check, "_VAULT_DATA_ROOT", vault)
    cfg = {"screen_check": {
        "activity": ".MainActivity",
        "launch_extras": [{"type": "string", "key": "session_token",
                           "value_from_secret": ".secrets/demo-app.json#session_token"}]}}
    fake = FakeRun()

    note = screen_check.run_check(
        repo, run_dir, cap_s=600, lock_path=lock_path, node_script=node_script,
        run=fake, config=cfg)

    assert note.startswith("화면 확인됨")          # the check itself still succeeded
    assert "launch_extras" in note and "session_token" in note
    shot = [c for c in fake.calls if c["argv"][2:3] == ["shot"]][0]
    assert "APP_TEST_LAUNCH_EXTRAS" not in shot["env"]


def test_extras_without_a_valid_activity_fall_back_to_monkey(tmp_path, monkeypatch):
    repo, run_dir, lock_path, node_script = _mk_env(tmp_path)
    cfg, vault = _extras_cfg(tmp_path, activity="")
    monkeypatch.setattr(screen_check, "_VAULT_DATA_ROOT", vault)
    fake = FakeRun()

    note = screen_check.run_check(
        repo, run_dir, cap_s=600, lock_path=lock_path, node_script=node_script,
        run=fake, config=cfg)

    assert "activity" in note
    shot = [c for c in fake.calls if c["argv"][2:3] == ["shot"]][0]
    assert "APP_TEST_LAUNCH_EXTRAS" not in shot["env"]
    assert "s3cret-token-value" not in note
