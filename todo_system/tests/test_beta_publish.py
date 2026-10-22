"""Unit tests for the deterministic post-merge beta publisher.

Every external effect (gradle build, firebase-tools group:list/distribute, the
git diff migration probe, the watermark commit) is driven through a fake `run`,
so nothing shells out and no real Firebase/gradle is touched. All canon / lock /
notes / task paths are pointed at tmp_path — the suite never writes the real
vault.
"""

from __future__ import annotations

import importlib
import json
import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from todo_system.orchestrator import beta_publish as bp

# The shipped canon now lives in the VAULT (.macp/), not in the engine package, so
# an argument-less bp.load_config() follows MACP_VAULT_ROOT. The shipped-config
# tests below must assert about THIS checkout's canon regardless of the runtime
# env, so they anchor the path explicitly instead of taking the module default.
_SHIPPED_CONFIG = Path(bp.__file__).resolve().parents[2] / ".macp" / "beta_publish_config.json"

REPO = "if-then-habits"
APP_ID = "1:493678352690:android:a8d8b840897be693e8df2b"
PROJECT = "ifthenhabits-app"
APK = "app/build/outputs/apk/beta/app-beta.apk"
SHA1 = "a" * 40
SHA2 = "b" * 40

_MIGRATION_DIFF = (
    "diff --git a/app/AppDatabase.kt b/app/AppDatabase.kt\n"
    "--- a/app/AppDatabase.kt\n"
    "+++ b/app/AppDatabase.kt\n"
    "@@ -3 +3 @@\n"
    "-    version = 20,\n"
    "+    version = 21,\n"
)


def _group_list_table(count) -> str:
    """A realistic `appdistribution:group:list` box table with `count` testers."""
    return (
        "┌──────────────┬──────────────┬──────────────┬───────────────┐\n"
        "│ Group name   │ Group alias  │ Tester count │ Release count │\n"
        "├──────────────┼──────────────┼──────────────┼───────────────┤\n"
        f"│ beta-testers │ beta-testers │ {count}            │ 3             │\n"
        "└──────────────┴──────────────┴──────────────┴───────────────┘\n"
    )


class FakeRun:
    """Records every call and dispatches canned output by argv. Signature matches
    beta_publish's default runner: (argv, *, cwd, env, timeout)."""

    def __init__(self, *, tester_count=1, build_rc=0, distribute_rc=0,
                 migration_diff="", release_id="4chcvn0lf5h5g"):
        self.calls: list[dict] = []
        self.tester_count = tester_count
        self.build_rc = build_rc
        self.distribute_rc = distribute_rc
        self.migration_diff = migration_diff
        self.release_id = release_id

    def __call__(self, argv, *, cwd=None, env=None, timeout=None):
        self.calls.append({"argv": list(argv), "cwd": cwd,
                           "env": dict(env) if env else None})

        def cp(rc=0, out="", err=""):
            return subprocess.CompletedProcess(argv, rc, out, err)

        if argv[:1] == ["git"] and "diff" in argv:
            return cp(0, self.migration_diff)
        if argv[:1] == ["git"] and "commit" in argv:
            return cp(0, "committed")
        if argv[:1] == ["git"] and "rev-parse" in argv:
            return cp(0, "d" * 40)
        if argv[:1] == ["gh"]:
            return cp(0, "c" * 40)
        if "appdistribution:group:list" in argv:
            return cp(0, _group_list_table(self.tester_count))
        if "appdistribution:distribute" in argv:
            return cp(self.distribute_rc, f"i View release: /releases/{self.release_id}")
        return cp(self.build_rc, "BUILD SUCCESSFUL" if self.build_rc == 0 else "BUILD FAILED")

    def find(self, needle: str) -> dict | None:
        for c in self.calls:
            if needle in c["argv"]:
                return c
        return None

    def has(self, needle: str) -> bool:
        return self.find(needle) is not None


@pytest.fixture
def env(tmp_path):
    """config.json + watermarks.json + lock/notes dirs, all under tmp_path."""
    cfg = tmp_path / "beta_publish_config.json"
    cfg.write_text(json.dumps({"repos": {REPO: {
        "app_id": APP_ID,
        "project": PROJECT,
        "group": "beta-testers",
        "build_cmd": ["./gradlew", ":app:assembleBeta"],
        "apk_path": APK,
        "migration_globs": ["*Database*.kt", "*Migration*.kt", "*/schemas/*"],
    }}}), encoding="utf-8")
    wm = tmp_path / "beta_watermarks.json"
    wm.write_text(json.dumps({"repos": {
        REPO: {"version_code": 75, "last_published_sha": None, "published_shas": []},
    }}), encoding="utf-8")

    class E:
        config_path = cfg
        watermark_path = wm
        lock_dir = tmp_path / "locks"
        notes_dir = tmp_path / "notes"
        clone = tmp_path / "clone"
        smoke_alert = tmp_path / "smoke_alerts.json"

        @staticmethod
        def load_wm():
            return json.loads(wm.read_text(encoding="utf-8"))["repos"][REPO]

    return E()


def _publish(env, run, sha=SHA1, *, repo=REPO, payload=None, notify=None, task_path=None, progress=None):
    return bp.maybe_publish(
        repo, env.clone, sha, payload if payload is not None else {"outcome": "루틴 발견 기능"},
        progress=progress or (lambda _m: None),
        config_path=env.config_path, watermark_path=env.watermark_path,
        lock_dir=env.lock_dir, notes_dir=env.notes_dir, task_path=task_path,
        run=run, notify=notify or (lambda _m: None),
    )


# --- no config / regression -------------------------------------------------

def test_no_config_repo_skips_with_progress_note(env):
    """A repo absent from the config publishes nothing, does not touch `run`,
    but does say why in the progress log (matching the other skip branches)."""
    run = FakeRun()
    logged = []
    note = _publish(env, run, repo="SomeUnconfiguredRepo", progress=logged.append)
    assert note is None
    assert run.calls == []
    assert logged == ["beta-publish: SomeUnconfiguredRepo has no config entry — skip"]


def test_dancemirror_regression_no_publish_no_error(env):
    """DanceMirror (versionCode hardcoded, no config entry) stays out cleanly."""
    run = FakeRun()
    assert _publish(env, run, repo="DanceMirror") is None
    assert run.calls == []
    # watermark canon untouched
    assert env.load_wm()["version_code"] == 75


# --- dedup ------------------------------------------------------------------

def test_already_published_sha_skips(env):
    wm = json.loads(env.watermark_path.read_text(encoding="utf-8"))
    wm["repos"][REPO]["published_shas"] = [SHA1]
    env.watermark_path.write_text(json.dumps(wm), encoding="utf-8")
    run = FakeRun()
    assert _publish(env, run, SHA1) is None
    assert not run.has("appdistribution:distribute")
    assert env.load_wm()["version_code"] == 75


# --- tester gate ------------------------------------------------------------

def test_tester_zero_aborts_without_distributing(env):
    run = FakeRun(tester_count=0)
    notified = []
    note = _publish(env, run, notify=notified.append)
    assert "[베타 중단]" in note
    assert not run.has("appdistribution:distribute")
    assert notified and REPO in notified[0]
    assert env.load_wm()["version_code"] == 75      # no bump on abort


def test_unparseable_tester_count_aborts(env):
    """A group:list we cannot parse is treated as abort, never a silent success."""
    run = FakeRun()
    run.tester_count = 1  # value ignored — override the table entirely:
    run._orig = run.__call__

    lists = []

    def broken(argv, **kw):
        if "appdistribution:group:list" in argv:
            lists.append(argv)
            return subprocess.CompletedProcess(argv, 0, "no table here", "")
        return run._orig(argv, **kw)

    note = bp.maybe_publish(
        REPO, env.clone, SHA1, {"outcome": "x"}, config_path=env.config_path,
        watermark_path=env.watermark_path, lock_dir=env.lock_dir, notes_dir=env.notes_dir,
        run=broken, notify=lambda _m: None,
    )
    assert "[베타 보류]" in note and "조회 실패" in note
    assert "테스터 추가" not in note                   # not the empty-group instruction
    assert len(lists) == 2                             # retried once before giving up
    assert not run.has("appdistribution:distribute")


def test_transient_group_list_failure_is_retried_then_distributes(env):
    """One failed listing (npx / network blip) must not abort the release or page
    "add testers" — DanceMirror 2026-10-06 shipped that false alarm with 2 testers."""
    run = FakeRun(tester_count=2)
    orig = run.__call__
    lists = []

    def flaky(argv, **kw):
        if "appdistribution:group:list" in argv:
            lists.append(argv)
            if len(lists) == 1:
                return subprocess.CompletedProcess(argv, 1, "", "npm ERR! network")
        return orig(argv, **kw)

    notified = []
    bp.maybe_publish(
        REPO, env.clone, SHA1, {"outcome": "x"}, config_path=env.config_path,
        watermark_path=env.watermark_path, lock_dir=env.lock_dir, notes_dir=env.notes_dir,
        run=flaky, notify=notified.append,
    )
    assert len(lists) == 2
    assert run.has("appdistribution:distribute")
    assert not any("베타 중단" in m or "베타 보류" in m for m in notified)


# --- watermark + notes ------------------------------------------------------

def test_success_bumps_watermark_and_records_sha(env):
    run = FakeRun()
    note = _publish(env, run, SHA1)
    entry = env.load_wm()
    assert entry["version_code"] == 76
    assert entry["last_published_sha"] == SHA1
    assert entry["published_shas"] == [SHA1]
    assert "베타 76 발행" in note and "4chcvn0lf5h5g" in note
    # watermark canon was committed (queue-stall mitigation)
    commit = run.find("commit")
    assert commit and "beta_watermarks.json" in commit["argv"][-1]


def test_serialize_two_merges_no_lost_update(env):
    """Two different merges of one app compound the watermark: base 75 -> 77."""
    run = FakeRun()
    _publish(env, run, SHA1)
    _publish(env, run, SHA2)
    entry = env.load_wm()
    assert entry["version_code"] == 77
    assert entry["published_shas"] == [SHA1, SHA2]
    assert entry["last_published_sha"] == SHA2


# --- concurrency: per-run worktrees + concurrent runs stay serialized / deduped --
# AC (c): confirm (do NOT rebuild) that app_lock (per-repo flock) + published_shas
# dedup keep two concurrent same-app publishes serialized and single-published.

def test_app_lock_serializes_two_holders(tmp_path):
    """Two threads contending on ONE per-app lock run their critical sections
    strictly one-after-another, never interleaved — the second flock(LOCK_EX)
    blocks until the first releases. Ordering is asserted on the recorded order,
    not on wall-clock timing."""
    lock_dir = tmp_path / "locks"
    order: list[str] = []
    a_inside = threading.Event()
    b_contending = threading.Event()
    release_a = threading.Event()

    def hold_a():
        with bp.app_lock(lock_dir, REPO):
            order.append("A-enter")
            a_inside.set()                     # A now holds the lock
            b_contending.wait(timeout=5)       # keep holding until B is blocked on it
            release_a.wait(timeout=5)
            order.append("A-exit")             # appended BEFORE the lock releases

    def grab_b():
        a_inside.wait(timeout=5)               # don't attempt until A holds it
        b_contending.set()
        with bp.app_lock(lock_dir, REPO):      # must block until A exits
            order.append("B-enter")
            order.append("B-exit")

    ta = threading.Thread(target=hold_a)
    tb = threading.Thread(target=grab_b)
    ta.start()
    tb.start()
    assert a_inside.wait(timeout=5)
    assert b_contending.wait(timeout=5)
    release_a.set()                            # let A finish; B may now proceed
    ta.join(timeout=5)
    tb.join(timeout=5)
    assert not ta.is_alive() and not tb.is_alive()
    assert order == ["A-enter", "A-exit", "B-enter", "B-exit"]


def test_app_lock_different_apps_do_not_block_each_other(tmp_path):
    """Different apps take DIFFERENT lock files and can be held at once — two
    different apps publish in parallel."""
    lock_dir = tmp_path / "locks"
    acquired_b = threading.Event()
    with bp.app_lock(lock_dir, "RepoA"):
        def grab_b():
            with bp.app_lock(lock_dir, "RepoB"):   # a different app → different file
                acquired_b.set()

        t = threading.Thread(target=grab_b)
        t.start()
        assert acquired_b.wait(timeout=5), "RepoB blocked on RepoA's lock — not per-repo isolated"
        t.join(timeout=5)
    assert (lock_dir / "RepoA.lock").exists()
    assert (lock_dir / "RepoB.lock").exists()


def test_concurrent_same_sha_publishes_exactly_once(env):
    """Two runs race to publish the SAME main sha of one app: the flock serializes
    them, the first publishes, the second reads the bumped watermark and dedups via
    already_published → None. No double publish, no lost update."""
    results: dict[str, object] = {}
    start = threading.Barrier(2)

    def publish(tag):
        start.wait(timeout=5)                  # both enter maybe_publish together
        results[tag] = _publish(env, FakeRun(), SHA1)

    t1 = threading.Thread(target=publish, args=("one",))
    t2 = threading.Thread(target=publish, args=("two",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert not t1.is_alive() and not t2.is_alive()

    entry = env.load_wm()
    assert entry["version_code"] == 76         # bumped EXACTLY once (75 -> 76)
    assert entry["published_shas"] == [SHA1]   # recorded once — no duplicate
    assert entry["last_published_sha"] == SHA1
    # exactly one call returned a publish note; the other deduped to None
    notes = [results.get("one"), results.get("two")]
    assert notes.count(None) == 1, notes
    assert sum(1 for n in notes if isinstance(n, str) and "베타 76 발행" in n) == 1


def test_release_notes_reflect_outcome(env):
    run = FakeRun()
    _publish(env, run, SHA1, payload={"outcome": "길로 저장·발견된 루틴"})
    notes = (env.notes_dir / f"{REPO}-release-notes.txt").read_text(encoding="utf-8")
    assert "길로 저장·발견된 루틴" in notes
    assert bp.BACKUP_WARNING not in notes                 # no migration => no warning


def test_migration_puts_backup_warning_first(env):
    run = FakeRun(migration_diff=_MIGRATION_DIFF)
    _publish(env, run, SHA1, payload={"outcome": "DB v20->v21"})
    notes = (env.notes_dir / f"{REPO}-release-notes.txt").read_text(encoding="utf-8")
    assert notes.splitlines()[0] == bp.BACKUP_WARNING
    assert "DB v20->v21" in notes


# --- migration probe vs a clone that lacks the squash sha -------------------

_V14_DIFF = (
    "diff --git a/app/AppDatabase.kt b/app/AppDatabase.kt\n"
    "--- a/app/AppDatabase.kt\n"
    "+++ b/app/AppDatabase.kt\n"
    "@@ -3 +3 @@\n"
    "-    version = 13,\n"
    "+    version = 14,\n"
)


class _SquashCloneRun(FakeRun):
    """A clone that does not have the squash merge commit gh just reported:
    `cat-file -e` and `diff` answer bad object until a fetch succeeds, after
    which the diff carries the `version = 14` bump (DripCoffee 131's shape)."""

    def __init__(self, *, fetch_rc=0, fetch_raises=False, **kw):
        super().__init__(**kw)
        self.fetch_rc = fetch_rc
        self.fetch_raises = fetch_raises
        self.fetched = False

    def __call__(self, argv, *, cwd=None, env=None, timeout=None):
        def cp(rc=0, out="", err=""):
            return subprocess.CompletedProcess(argv, rc, out, err)

        if argv[:1] != ["git"] or not {"cat-file", "fetch", "symbolic-ref", "diff"} & set(argv):
            return super().__call__(argv, cwd=cwd, env=env, timeout=timeout)
        self.calls.append({"argv": list(argv), "cwd": cwd,
                           "env": dict(env) if env else None})
        if "cat-file" in argv:
            return cp(0) if self.fetched else cp(128, "", "fatal: bad object")
        if "fetch" in argv:
            if self.fetch_raises:
                raise subprocess.TimeoutExpired(argv, 300)
            if self.fetch_rc == 0:
                self.fetched = True
            return cp(self.fetch_rc, "", "" if self.fetch_rc == 0 else "fatal: couldn't find remote ref")
        if "symbolic-ref" in argv:
            return cp(0, "refs/remotes/origin/main\n")
        return cp(0, _V14_DIFF) if self.fetched else cp(128, "", "fatal: bad object")

    def argvs(self, needle: str) -> list[list[str]]:
        return [c["argv"] for c in self.calls if needle in c["argv"]]


def test_migration_probe_fetches_missing_merge_sha(env):
    """RED control: the squash sha is not in the clone yet. Without a fetch the
    diff is a bad object and the backup warning silently vanishes."""
    run = _SquashCloneRun()
    _publish(env, run, SHA1, payload={"outcome": "DB v13->v14"})
    notes = (env.notes_dir / f"{REPO}-release-notes.txt").read_text(encoding="utf-8")
    assert notes.splitlines()[0] == bp.BACKUP_WARNING
    assert ["git", "-C", str(env.clone), "fetch", "origin", SHA1] in run.argvs("fetch")


@pytest.mark.parametrize("kw", [{"fetch_rc": 128}, {"fetch_raises": True}],
                         ids=["rc128", "timeout"])
def test_migration_probe_fetch_failure_warns_and_still_publishes(env, kw):
    run = _SquashCloneRun(**kw)
    logged = []
    note = _publish(env, run, SHA1, progress=logged.append)
    assert "베타 76 발행" in note
    assert run.has("appdistribution:distribute")
    assert env.load_wm()["version_code"] == 76
    assert [m for m in logged if "migration probe skipped" in m] == [
        f"beta-publish: migration probe skipped — {SHA1[:9]} not in clone"]
    clone = str(env.clone)
    assert run.argvs("fetch") == [["git", "-C", clone, "fetch", "origin", SHA1],
                                  ["git", "-C", clone, "fetch", "origin", "main"]]
    notes = (env.notes_dir / f"{REPO}-release-notes.txt").read_text(encoding="utf-8")
    assert bp.BACKUP_WARNING not in notes
    assert not run.has("diff")                            # probe skipped, not guessed


def test_migration_probe_present_sha_does_not_fetch(env):
    run = FakeRun(migration_diff=_MIGRATION_DIFF)
    _publish(env, run, SHA1)
    notes = (env.notes_dir / f"{REPO}-release-notes.txt").read_text(encoding="utf-8")
    assert notes.splitlines()[0] == bp.BACKUP_WARNING
    assert not run.has("fetch")
    assert not run.has("symbolic-ref")


def test_migration_probe_cumulative_checks_diff_base(env):
    wm = json.loads(env.watermark_path.read_text(encoding="utf-8"))
    wm["repos"][REPO]["last_published_sha"] = SHA2
    env.watermark_path.write_text(json.dumps(wm), encoding="utf-8")
    run = FakeRun()
    bp.maybe_publish(
        REPO, env.clone, SHA1, {"outcome": "x"}, config_path=env.config_path,
        watermark_path=env.watermark_path, lock_dir=env.lock_dir, notes_dir=env.notes_dir,
        run=run, notify=lambda _m: None, cumulative_notes=True,
    )
    assert any(f"{SHA2}^{{commit}}" in c["argv"] for c in run.calls)
    assert not run.has("fetch")


def test_progress_log_line_appended(env, tmp_path):
    task = tmp_path / "task.md"
    task.write_text("---\nstatus: open\n---\n\n# T\n\n## Progress Log\n\n- 2026-09-16: 생성\n",
                    encoding="utf-8")
    run = FakeRun()
    _publish(env, run, SHA1, task_path=task)
    body = task.read_text(encoding="utf-8")
    assert "베타 76 발행" in body and "## Progress Log" in body


# --- build/distribute failure surfaces (does not revert the merge) ----------

def test_build_failure_raises_beta_publish_error(env):
    run = FakeRun(build_rc=1)
    with pytest.raises(bp.BetaPublishError):
        _publish(env, run, SHA1)
    assert not run.has("appdistribution:distribute")
    assert env.load_wm()["version_code"] == 75


def test_distribute_failure_raises_and_leaves_watermark(env):
    run = FakeRun(distribute_rc=1)
    with pytest.raises(bp.BetaPublishError):
        _publish(env, run, SHA1)
    assert env.load_wm()["version_code"] == 75            # not bumped on failure


# --- Phase 5: if-then-habits dry-run argv assembly --------------------------

def test_argv_assembly_for_if_then_habits(env):
    run = FakeRun()
    _publish(env, run, SHA1)

    build = run.find(":app:assembleBeta")
    assert build["argv"] == ["./gradlew", ":app:assembleBeta", "--no-daemon"]
    assert build["env"]["GITHUB_RUN_NUMBER"] == "76"      # watermark 75 + 1

    glist = run.find("appdistribution:group:list")
    assert glist["argv"] == ["npx", "-y", "firebase-tools",
                             "appdistribution:group:list", "--project", PROJECT]

    dist = run.find("appdistribution:distribute")["argv"]
    assert dist[:4] == ["npx", "-y", "firebase-tools", "appdistribution:distribute"]
    assert dist[4].endswith(APK)                          # the built apk path
    assert dist[dist.index("--app") + 1] == APP_ID
    assert dist[dist.index("--groups") + 1] == "beta-testers"
    notes_file = dist[dist.index("--release-notes-file") + 1]
    assert notes_file.endswith(f"{REPO}-release-notes.txt")


# --- parser + notes units ---------------------------------------------------

def test_parse_tester_count_reads_the_beta_testers_row():
    assert bp.parse_tester_count(_group_list_table(3), "beta-testers") == 3
    assert bp.parse_tester_count(_group_list_table(0), "beta-testers") == 0


def test_parse_tester_count_none_when_no_table():
    assert bp.parse_tester_count("nothing tabular here", "beta-testers") is None


def test_parse_release_id_from_console_url():
    assert bp.parse_release_id("View: /releases/4chcvn0lf5h5g") == "4chcvn0lf5h5g"
    assert bp.parse_release_id("no id here") is None


def test_build_release_notes_prefers_pr_title_then_falls_back():
    assert bp.build_release_notes({"pr_title": "새 기능"}, migration=False).startswith("새 기능")
    assert bp.build_release_notes({"task_id": "slug-x"}, migration=False).startswith("slug-x")
    assert bp.build_release_notes({}, migration=False).strip() == "베타 업데이트"


def test_detect_migration_true_on_version_bump():
    run = FakeRun(migration_diff=_MIGRATION_DIFF)
    assert bp.detect_migration("/repo", SHA1, ["*Database*.kt"], run=run) is True


def test_detect_migration_false_on_plain_diff():
    run = FakeRun(migration_diff="+    val x = 1\n")
    assert bp.detect_migration("/repo", SHA1, ["*Database*.kt"], run=run) is False


def test_resolve_merge_sha_prefers_gh_then_falls_back():
    run = FakeRun()
    assert bp.resolve_merge_sha("/repo", "worker/x", run=run) == "c" * 40   # gh oid

    def gh_fails(argv, **kw):
        if argv[:1] == ["gh"]:
            return subprocess.CompletedProcess(argv, 1, "", "no pr")
        return run(argv, **kw)

    assert bp.resolve_merge_sha("/repo", "worker/x", run=gh_fails) == "d" * 40  # git fallback


# --- copy_files: out-of-git build inputs (물꼬 .secrets/mulkko.json) ------------

def _with_copy_files(env, tmp_path, mapping):
    cfg = json.loads(env.config_path.read_text(encoding="utf-8"))
    cfg["repos"][REPO]["copy_files"] = mapping
    env.config_path.write_text(json.dumps(cfg), encoding="utf-8")
    vault = tmp_path / "vault"
    (vault / ".secrets").mkdir(parents=True)
    (vault / ".secrets" / "mulkko.json").write_text('{"url": "x"}', encoding="utf-8")
    env.clone.mkdir(exist_ok=True)
    return vault


def _publish_in(env, run, vault, sha=SHA1):
    return bp.maybe_publish(
        REPO, env.clone, sha, {"outcome": "세션 살려두기"},
        progress=lambda _m: None,
        config_path=env.config_path, watermark_path=env.watermark_path,
        lock_dir=env.lock_dir, notes_dir=env.notes_dir,
        run=run, notify=lambda _m: None, vault_root=vault,
    )


def test_copy_files_present_during_build_and_removed_after(env, tmp_path):
    """The worker clone has no .secrets/: without staging, the auto beta ships with
    물꼬's backup section hidden. Present for the build, gone afterwards."""
    vault = _with_copy_files(env, tmp_path, {".secrets/mulkko.json": ".secrets/mulkko.json"})
    dest = env.clone / ".secrets" / "mulkko.json"
    seen = {}
    fake = FakeRun()

    def run(argv, **kw):
        if argv[:1] == ["./gradlew"]:
            seen["during_build"] = dest.read_text(encoding="utf-8") if dest.exists() else None
        return fake(argv, **kw)

    note = _publish_in(env, run, vault)
    assert seen["during_build"] == '{"url": "x"}'
    assert not dest.exists()
    assert "베타 76 발행" in note


def test_copy_files_missing_source_aborts_before_build(env, tmp_path):
    vault = _with_copy_files(env, tmp_path, {".secrets/other.json": ".secrets/other.json"})
    run = FakeRun()
    with pytest.raises(bp.BetaPublishError, match="missing"):
        _publish_in(env, run, vault)
    assert not run.has(":app:assembleBeta")
    assert not run.has("appdistribution:distribute")
    assert env.load_wm()["version_code"] == 75


def test_copy_files_keeps_an_identical_preexisting_file(env, tmp_path):
    """A clone that already carries the file (e.g. ~/Projects/if-then-habits) keeps it."""
    vault = _with_copy_files(env, tmp_path, {".secrets/mulkko.json": ".secrets/mulkko.json"})
    dest = env.clone / ".secrets" / "mulkko.json"
    dest.parent.mkdir(parents=True)
    dest.write_text('{"url": "x"}', encoding="utf-8")
    _publish_in(env, FakeRun(), vault)
    assert dest.read_text(encoding="utf-8") == '{"url": "x"}'


def test_copy_files_removed_even_when_build_fails(env, tmp_path):
    vault = _with_copy_files(env, tmp_path, {".secrets/mulkko.json": ".secrets/mulkko.json"})
    with pytest.raises(bp.BetaPublishError):
        _publish_in(env, FakeRun(build_rc=1), vault)
    assert not (env.clone / ".secrets" / "mulkko.json").exists()


def test_copy_files_entry_escaping_the_clone_is_rejected(env, tmp_path):
    vault = _with_copy_files(env, tmp_path, {"../outside.json": ".secrets/mulkko.json"})
    run = FakeRun()
    with pytest.raises(bp.BetaPublishError, match="escapes"):
        _publish_in(env, run, vault)
    assert not run.has(":app:assembleBeta")


@pytest.mark.skipif(
    not _SHIPPED_CONFIG.is_file(),
    reason="vault-only: the beta_publish canon (.macp/beta_publish_config.json) is vault "
           "data (moved out of the engine package in split-8), not in the engine code "
           "export set — absent in an engine-only tree (public-split-10 AC2).",
)
def test_shipped_config_stages_mulkko_backup_settings():
    cfg = bp.load_config(_SHIPPED_CONFIG)
    assert cfg["if-then-habits"]["copy_files"] == {".secrets/mulkko.json": ".secrets/mulkko.json"}


# --- build_props: gitignored gradle-property secrets (advote endpoint/key) -----

def _with_build_props(env, tmp_path, *, props_from=".secrets/advote.json",
                      build_props=None, secret=None):
    """Point REPO at a build_props secret and (optionally) write it into a tmp vault.
    `secret` is the raw file content (str) or None to leave the file absent."""
    cfg = json.loads(env.config_path.read_text(encoding="utf-8"))
    cfg["repos"][REPO]["build_props_from"] = props_from
    cfg["repos"][REPO]["build_props"] = (
        build_props if build_props is not None
        else {"advote.endpoint": "endpoint", "advote.appKey": "ingest_key"})
    env.config_path.write_text(json.dumps(cfg), encoding="utf-8")
    vault = tmp_path / "vault"
    (vault / ".secrets").mkdir(parents=True, exist_ok=True)
    if secret is not None:
        (vault / ".secrets" / "advote.json").write_text(secret, encoding="utf-8")
    env.clone.mkdir(exist_ok=True)
    return vault


def test_build_props_appended_after_build_cmd(env, tmp_path):
    """A wired repo gets `-P<prop>=<value>` appended AFTER build_cmd, in config order."""
    vault = _with_build_props(env, tmp_path, secret=json.dumps(
        {"endpoint": "https://advote.example/ingest", "ingest_key": "sk_live_abc"}))
    run = FakeRun()
    note = _publish_in(env, run, vault)
    build = run.find(":app:assembleBeta")
    assert build["argv"] == [
        "./gradlew", ":app:assembleBeta", "--no-daemon",
        "-Padvote.endpoint=https://advote.example/ingest",
        "-Padvote.appKey=sk_live_abc",
    ]
    assert "베타 76 발행" in note              # build → distribute → watermark bump all ran


def test_no_build_props_leaves_build_argv_unchanged(env):
    """Regression: a repo without build_props_from gets a byte-identical build argv."""
    run = FakeRun()
    _publish(env, run, SHA1)
    build = run.find(":app:assembleBeta")
    assert build["argv"] == ["./gradlew", ":app:assembleBeta", "--no-daemon"]      # no -P appended


def test_build_props_missing_file_aborts_before_build(env, tmp_path):
    """No secret file → abort before the build; the message names the file + keys."""
    vault = _with_build_props(env, tmp_path, secret=None)           # file never written
    run = FakeRun()
    with pytest.raises(bp.BetaPublishError) as ei:
        _publish_in(env, run, vault)
    msg = str(ei.value)
    assert ".secrets/advote.json" in msg                           # the file to create
    assert "endpoint" in msg and "ingest_key" in msg               # the keys it needs
    assert not run.has(":app:assembleBeta")
    assert not run.has("appdistribution:distribute")
    assert env.load_wm()["version_code"] == 75                     # no bump


def test_build_props_missing_key_aborts_before_build(env, tmp_path):
    """A mapped key absent from the secret → abort naming the key + the -P it feeds."""
    vault = _with_build_props(env, tmp_path,
                              secret=json.dumps({"endpoint": "https://x"}))  # ingest_key absent
    run = FakeRun()
    with pytest.raises(bp.BetaPublishError) as ei:
        _publish_in(env, run, vault)
    msg = str(ei.value)
    assert "ingest_key" in msg and "-Padvote.appKey" in msg
    assert ".secrets/advote.json" in msg
    assert not run.has(":app:assembleBeta")
    assert env.load_wm()["version_code"] == 75


def test_build_props_blank_value_aborts_before_build(env, tmp_path):
    """A blank / whitespace value is treated exactly like an absent key."""
    vault = _with_build_props(env, tmp_path, secret=json.dumps(
        {"endpoint": "https://x", "ingest_key": "   "}))
    run = FakeRun()
    with pytest.raises(bp.BetaPublishError, match="empty or absent"):
        _publish_in(env, run, vault)
    assert not run.has(":app:assembleBeta")
    assert env.load_wm()["version_code"] == 75


def test_build_props_unparseable_json_aborts_before_build(env, tmp_path):
    vault = _with_build_props(env, tmp_path, secret="{not valid json")
    run = FakeRun()
    with pytest.raises(bp.BetaPublishError, match="JSON"):
        _publish_in(env, run, vault)
    assert not run.has(":app:assembleBeta")
    assert env.load_wm()["version_code"] == 75


def test_build_props_non_object_json_aborts_before_build(env, tmp_path):
    """Valid JSON that is not an object (e.g. a list) is rejected, not iterated."""
    vault = _with_build_props(env, tmp_path, secret="[1, 2, 3]")
    run = FakeRun()
    with pytest.raises(bp.BetaPublishError, match="JSON object"):
        _publish_in(env, run, vault)
    assert not run.has(":app:assembleBeta")


def test_build_props_path_escaping_the_vault_is_rejected(env, tmp_path):
    vault = _with_build_props(env, tmp_path, props_from="../outside.json", secret=None)
    run = FakeRun()
    with pytest.raises(bp.BetaPublishError, match="escapes"):
        _publish_in(env, run, vault)
    assert not run.has(":app:assembleBeta")


def test_build_props_failure_does_not_stage_copy_files_secret(env, tmp_path):
    """build_prop_args runs BEFORE stage_build_files: a missing gradle-prop secret
    aborts before any copy_files secret is copied into the worker clone."""
    vault = _with_build_props(env, tmp_path, secret=None)          # build_props file absent → abort
    cfg = json.loads(env.config_path.read_text(encoding="utf-8"))
    cfg["repos"][REPO]["copy_files"] = {".secrets/mulkko.json": ".secrets/mulkko.json"}
    env.config_path.write_text(json.dumps(cfg), encoding="utf-8")
    (vault / ".secrets" / "mulkko.json").write_text('{"url": "x"}', encoding="utf-8")
    dest = env.clone / ".secrets" / "mulkko.json"
    run = FakeRun()
    with pytest.raises(bp.BetaPublishError, match="missing"):
        _publish_in(env, run, vault)
    assert not dest.exists()                                       # copy_files secret never staged
    assert not run.has(":app:assembleBeta")


@pytest.mark.skipif(
    not _SHIPPED_CONFIG.is_file(),
    reason="vault-only: the beta_publish canon (.macp/beta_publish_config.json) is vault "
           "data (moved out of the engine package in split-8), not in the engine code "
           "export set — absent in an engine-only tree (public-split-10 AC2).",
)
def test_shipped_config_wires_dripcoffee_advote_build_props():
    """The real shipped config wires DripCoffee to .secrets/advote.json; if-then-habits
    stays unwired so its build argv cannot change."""
    cfg = bp.load_config(_SHIPPED_CONFIG)
    dc = cfg["DripCoffee"]
    assert dc["build_props_from"] == ".secrets/advote.json"
    assert dc["build_props"] == {"advote.endpoint": "endpoint", "advote.appKey": "ingest_key"}
    assert "build_props_from" not in cfg["if-then-habits"]


@pytest.mark.skipif(
    not _SHIPPED_CONFIG.is_file(),
    reason="vault-only: the beta_publish canon (.macp/beta_publish_config.json) is vault "
           "data (moved out of the engine package in split-8), not in the engine code "
           "export set — absent in an engine-only tree (public-split-10 AC2).",
)
def test_shipped_config_wires_dancemirror_advote_build_props():
    """The real shipped config wires DanceMirror to .secrets/advote.json with its OWN
    ingest key (its own advote tenant since 2026-10-13 — sharing DripCoffee's key filed
    DanceMirror feedback/usage under the dripcoffee app); if-then-habits stays unwired
    so its build argv cannot change."""
    cfg = bp.load_config(_SHIPPED_CONFIG)
    dm = cfg["DanceMirror"]
    assert dm["build_props_from"] == ".secrets/advote.json"
    assert dm["build_props"] == {"advote.endpoint": "endpoint",
                                 "advote.appKey": "dancemirror_ingest_key"}
    assert "build_props_from" not in cfg["if-then-habits"]


# --- canon location: vault .macp/, redirectable via MACP_VAULT_ROOT -----------

def test_config_path_follows_macp_vault_root_and_is_read(monkeypatch, tmp_path):
    """The canon is vault data: a redirected vault publishes from ITS OWN config.
    Asserts the file is actually READ, not merely that the constant points at it —
    a path that resolves but is never opened fails open to {} (silent skip)."""
    (tmp_path / ".macp").mkdir()
    (tmp_path / ".macp" / "beta_publish_config.json").write_text(
        json.dumps({"repos": {"RedirectApp": {"project": "p"}}}), encoding="utf-8")
    with monkeypatch.context() as m:
        m.setenv("MACP_VAULT_ROOT", str(tmp_path))
        mod = importlib.reload(bp)
        assert mod.CONFIG_PATH == tmp_path / ".macp" / "beta_publish_config.json"
        assert mod.WATERMARK_PATH == tmp_path / ".macp" / "beta_watermarks.json"
        assert sorted(mod.load_config()) == ["RedirectApp"]
        assert mod.repo_config("RedirectApp") == {"project": "p"}
    importlib.reload(bp)                      # restore the real root for later tests


def test_watermark_commit_runs_in_caller_named_vault_root(env, tmp_path):
    """The watermark commit runs `git -C <vault_root the caller passed>`, never a
    root back-derived from watermark_path. The old wm.resolve().parents[1] made
    this tmp_path.parent — right by luck for the real .macp/ layout, wrong for any
    injected canon, which is exactly what this asserts against."""
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    run = FakeRun()
    _publish_in(env, run, vault)
    commit = run.find("commit")
    assert commit and commit["argv"][:3] == ["git", "-C", str(vault)]
    assert "beta_watermarks.json" in commit["argv"][-1]


# --- no-checkout ls-remote: owner comes from the vault config, never a literal --

class _LsRemoteRun:
    """Records argv and answers every ls-remote with SHA1."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, cwd=None, env=None, timeout=None):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, f"{SHA1}\trefs/heads/main\n", "")


def test_ls_remote_no_checkout_uses_configured_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_REPO_SLUG", "acme/MaestroACP")
    run = _LsRemoteRun()
    assert bp._ls_remote_sha(REPO, worker_root=tmp_path, run=run) == SHA1
    assert run.calls == [["git", "ls-remote",
                          f"https://github.com/acme/{REPO}.git", "refs/heads/main"]]


def test_ls_remote_no_checkout_reads_owner_from_vault_config(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_REPO_SLUG", raising=False)
    vault = tmp_path / "vault"
    (vault / ".macp").mkdir(parents=True)
    (vault / ".macp" / "config.toml").write_text('repo_slug = "acme/X"\n', encoding="utf-8")
    monkeypatch.setattr(bp, "_VAULT_DATA_ROOT", vault)
    run = _LsRemoteRun()
    assert bp._ls_remote_sha(REPO, worker_root=tmp_path / "workers", run=run) == SHA1
    assert run.calls[0][2] == f"https://github.com/acme/{REPO}.git"


def test_ls_remote_no_checkout_no_owner_returns_none(monkeypatch, tmp_path):
    """RED control: with neither env nor config the neutral default resolves, and
    the fallback must fail closed — no URL guessed, no ls-remote issued."""
    monkeypatch.delenv("MACP_REPO_SLUG", raising=False)
    monkeypatch.setattr(bp, "_VAULT_DATA_ROOT", tmp_path / "vault")
    run = _LsRemoteRun()
    assert bp._ls_remote_sha(REPO, worker_root=tmp_path / "workers", run=run) is None
    assert run.calls == []



def test_resolve_merge_sha_falls_back_when_gh_times_out():
    """A slow GitHub API must not raise out of the resolver (2026-10-04 page)."""
    run = FakeRun()

    def gh_hangs(argv, **kw):
        if argv[:1] == ["gh"]:
            raise subprocess.TimeoutExpired(argv, 60)
        return run(argv, **kw)

    assert bp.resolve_merge_sha("/repo", "worker/x", run=gh_hangs) == "d" * 40


def test_publish_beta_skips_unconfigured_repo_without_calling_gh(monkeypatch, tmp_path):
    """An unconfigured repo returns before any merge-sha lookup, so a gh timeout
    there can never page a human about a repo that does not publish."""
    from todo_system.orchestrator import runner as runner_mod

    monkeypatch.setattr(runner_mod.beta_publish, "repo_config", lambda name, *a, **k: None)

    def boom(*a, **k):
        raise AssertionError("resolve_merge_sha must not run for an unconfigured repo")

    monkeypatch.setattr(runner_mod.beta_publish, "resolve_merge_sha", boom)
    pages = []
    monkeypatch.setattr(runner_mod._worker, "kakao_notify", pages.append)

    note = runner_mod.StagedExecutor._publish_beta(
        None, tmp_path, "https://github.com/o/ai-stock-trading.git", "worker/x",
        tmp_path, {}, "t", lambda _m: None)

    assert note == ""
    assert pages == []


# --- catch-up sweep: detached spawn + child logging ---------------------------

_SWEEP_ARGV = [sys.executable, "-u", "-m", "todo_system.orchestrator.beta_publish", "sweep"]
_SCOPE = ["/usr/bin/systemd-run", "--user", "--scope", "--quiet", "--collect"]


class FakePopen:
    """Records every launch; never starts a process. The pid is fixed and
    positive and is never handed to os.kill."""

    def __init__(self, calls, argv, kwargs, *, wait_rc=None, pid=424242):
        self.argv, self.kwargs, self.pid, self._rc = argv, kwargs, pid, wait_rc
        calls.append((argv, kwargs))

    def wait(self, timeout=None):
        if self._rc is None:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        return self._rc


def _under_systemd(monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "x")
    monkeypatch.setattr(bp.shutil, "which",
                        lambda n: "/usr/bin/systemd-run" if n == "systemd-run" else None)


def test_spawn_detached_runs_in_own_scope_under_systemd(monkeypatch, tmp_path):
    """A new session is not a new cgroup: under KillMode=control-group a runner
    restart killed the sweep with a 0-byte log. Under systemd it gets a scope."""
    _under_systemd(monkeypatch)
    calls: list = []
    monkeypatch.setattr(bp.subprocess, "Popen",
                        lambda argv, **kw: FakePopen(calls, list(argv), kw))
    log = tmp_path / "sweep.log"

    assert bp._spawn_detached(log_path=log) == 424242
    assert len(calls) == 1
    argv, kw = calls[0]
    assert argv[:5] == _SCOPE
    assert argv[5:] == _SWEEP_ARGV
    assert kw["env"]["PYTHONUNBUFFERED"] == "1"
    assert kw["start_new_session"] is True
    assert kw["stderr"] is subprocess.STDOUT
    assert "spawning (scope=yes)" in log.read_text(encoding="utf-8")


def test_spawn_detached_plain_outside_systemd(monkeypatch, tmp_path):
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    calls: list = []
    monkeypatch.setattr(bp.subprocess, "Popen",
                        lambda argv, **kw: FakePopen(calls, list(argv), kw))
    log = tmp_path / "sweep.log"

    assert bp._spawn_detached(log_path=log) == 424242
    assert len(calls) == 1
    argv, kw = calls[0]
    assert argv == _SWEEP_ARGV and argv[0] == sys.executable and "-u" in argv
    assert kw["env"]["PYTHONUNBUFFERED"] == "1"
    assert "spawning (scope=no)" in log.read_text(encoding="utf-8")


def test_spawn_detached_falls_back_when_scope_launch_fails(monkeypatch, tmp_path):
    _under_systemd(monkeypatch)
    calls: list = []

    def factory(argv, **kw):
        if argv[0].endswith("systemd-run"):
            return FakePopen(calls, list(argv), kw, wait_rc=1, pid=111)
        return FakePopen(calls, list(argv), kw, pid=222)

    monkeypatch.setattr(bp.subprocess, "Popen", factory)
    log = tmp_path / "sweep.log"

    assert bp._spawn_detached(log_path=log) == 222
    assert len(calls) == 2
    assert calls[1][0] == _SWEEP_ARGV
    assert not any("systemd-run" in a for a in calls[1][0])
    text = log.read_text(encoding="utf-8")
    assert "rc=1" in text and "falling back" in text


def test_spawn_detached_falls_back_on_oserror(monkeypatch, tmp_path):
    _under_systemd(monkeypatch)
    calls: list = []

    def factory(argv, **kw):
        if argv[0].endswith("systemd-run"):
            raise OSError("no user bus")
        return FakePopen(calls, list(argv), kw, pid=333)

    monkeypatch.setattr(bp.subprocess, "Popen", factory)
    log = tmp_path / "sweep.log"

    assert bp._spawn_detached(log_path=log) == 333
    assert [c[0] for c in calls] == [_SWEEP_ARGV]
    assert "OSError no user bus" in log.read_text(encoding="utf-8")


def test_main_logs_start_notes_and_end(monkeypatch, capsys):
    def fake_sweep(*, progress):
        progress("p1")
        return ["note1"]

    monkeypatch.setattr(bp, "sweep_unpublished", fake_sweep)
    assert bp.main(["sweep"]) == 0
    out = capsys.readouterr().out
    idx = [out.index(s) for s in ("start pid=", "p1", "note1", "end pid=")]
    assert idx == sorted(idx)
    assert "rc=0" in out[idx[-1]:]


def test_main_logs_traceback_when_sweep_raises(monkeypatch, capsys):
    def fake_sweep(**_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(bp, "sweep_unpublished", fake_sweep)
    assert bp.main(["sweep"]) == 1
    out = capsys.readouterr().out
    for s in ("start pid=", "Traceback", "RuntimeError: boom", "rc=1"):
        assert s in out, s


def test_main_logs_end_line_on_systemexit(monkeypatch, capsys):
    def fake_sweep(**_kw):
        raise SystemExit(143)

    monkeypatch.setattr(bp, "sweep_unpublished", fake_sweep)
    with pytest.raises(SystemExit):
        bp.main(["sweep"])
    out = capsys.readouterr().out
    assert "interrupted (SystemExit)" in out
    assert "end pid=" in out and "rc=143" in out


def test_sigterm_handler_logs_and_exits(capsys):
    with pytest.raises(SystemExit) as ei:
        bp._on_sigterm(signal.SIGTERM, None)
    assert ei.value.code == 128 + signal.SIGTERM
    assert "received signal" in capsys.readouterr().out


def test_main_usage_unchanged(capsys):
    assert bp.main([]) == 2
    assert bp.main(["sweep", "x"]) == 2
    assert "usage:" in capsys.readouterr().out


# --- watermark mirror to main when vault HEAD is on a worker branch ---

_OLD, _BLOB, _TREE, _NEW = "1" * 40, "2" * 40, "3" * 40, "4" * 40
_MIRROR_STEPS = ["rev-parse", "hash-object", "read-tree", "update-index",
                 "write-tree", "commit-tree", "update-ref"]
_FORBIDDEN = {"push", "checkout", "stash", "reset", "switch"}


class _MirrorRun:
    """Fake runner answering the watermark commit + mirror plumbing by git
    subcommand (``argv[3]`` of ``git -C <vault> <sub> ...``)."""

    def __init__(self, *, head="main", head_rc=0, main_rc=0, fail_step=None,
                 raise_step=None):
        self.head, self.head_rc, self.main_rc = head, head_rc, main_rc
        self.fail_step, self.raise_step = fail_step, raise_step
        self.calls: list[dict] = []

    def __call__(self, argv, *, cwd=None, env=None, timeout=None):
        self.calls.append({"argv": list(argv), "env": env})
        sub = argv[3]
        if sub == self.raise_step:
            raise subprocess.TimeoutExpired(argv, timeout or 0)
        if sub == self.fail_step:
            return subprocess.CompletedProcess(argv, 1, "", "boom")
        out = {"symbolic-ref": self.head, "rev-parse": _OLD, "hash-object": _BLOB,
               "write-tree": _TREE, "commit-tree": _NEW}.get(sub, "")
        rc = {"symbolic-ref": self.head_rc, "rev-parse": self.main_rc}.get(sub, 0)
        return subprocess.CompletedProcess(argv, rc, out + "\n" if out else "", "")

    def subs(self) -> list[str]:
        return [c["argv"][3] for c in self.calls]

    def call(self, sub) -> dict:
        return next(c for c in self.calls if c["argv"][3] == sub)


def _vault_wm(tmp_path):
    vault = tmp_path / "vault"
    wm = vault / ".macp" / "beta_watermarks.json"
    wm.parent.mkdir(parents=True)
    wm.write_text(json.dumps({REPO: {"last_version_code": 76}}), encoding="utf-8")
    return vault, wm


def test_watermark_mirror_head_on_main_is_single_commit(tmp_path):
    vault, wm = _vault_wm(tmp_path)
    run, logged = _MirrorRun(head="main"), []
    bp._commit_watermark(wm, REPO, 76, vault_root=vault, run=run, progress=logged.append)
    assert run.subs() == ["commit", "symbolic-ref"]
    assert sum(s in ("commit", "commit-tree") for s in run.subs()) == 1
    assert not set(run.subs()) & set(_MIRROR_STEPS)
    assert logged == []


def test_watermark_mirror_head_on_worker_branch_plumbing_sequence(tmp_path):
    vault, wm = _vault_wm(tmp_path)
    run, logged = _MirrorRun(head="worker/x"), []
    bp._commit_watermark(wm, REPO, 76, vault_root=vault, run=run, progress=logged.append)
    assert run.subs() == ["commit", "symbolic-ref", *_MIRROR_STEPS]
    assert all(c["argv"][:3] == ["git", "-C", str(vault)] for c in run.calls)
    assert run.call("read-tree")["argv"][-1] == _OLD
    assert f"100644,{_BLOB},.macp/beta_watermarks.json" in run.call("update-index")["argv"]
    ct = run.call("commit-tree")["argv"]
    assert ct[4] == _TREE and ct[ct.index("-p") + 1] == _OLD
    assert ct[ct.index("-m") + 1] == f"chore: beta watermark {REPO} -> 76"
    assert run.call("update-ref")["argv"][-3:] == ["refs/heads/main", _NEW, _OLD]
    idx = {run.call(s)["env"]["GIT_INDEX_FILE"]
           for s in ("read-tree", "update-index", "write-tree")}
    assert len(idx) == 1
    idx_path = Path(idx.pop())
    assert vault.resolve() not in idx_path.resolve().parents
    assert not idx_path.parent.exists()                  # temp index dir cleaned up
    for s in ("symbolic-ref", "rev-parse", "hash-object", "commit-tree", "update-ref"):
        assert not (run.call(s)["env"] or {}).get("GIT_INDEX_FILE")
    assert not any(_FORBIDDEN & set(c["argv"]) for c in run.calls)
    assert logged == [f"beta-publish: watermark also committed to main {_NEW[:9]} "
                      f"(HEAD on worker/x)"]


def test_watermark_mirror_detached_head_still_mirrors(tmp_path):
    vault, wm = _vault_wm(tmp_path)
    run, logged = _MirrorRun(head="", head_rc=1), []
    bp._commit_watermark(wm, REPO, 76, vault_root=vault, run=run, progress=logged.append)
    assert run.subs() == ["commit", "symbolic-ref", *_MIRROR_STEPS]
    assert logged == [f"beta-publish: watermark also committed to main {_NEW[:9]} "
                      f"(HEAD on detached)"]


def test_watermark_mirror_skips_silently_without_main(tmp_path):
    vault, wm = _vault_wm(tmp_path)
    run, logged = _MirrorRun(head="worker/x", main_rc=1), []
    bp._commit_watermark(wm, REPO, 76, vault_root=vault, run=run, progress=logged.append)
    assert run.subs() == ["commit", "symbolic-ref", "rev-parse"]
    assert logged == []


def test_watermark_mirror_skips_canon_outside_vault(tmp_path):
    # The existing publish tests keep the canon at tmp_path/, outside vault_root —
    # this pins that they see no git call beyond the in-place commit.
    vault, _ = _vault_wm(tmp_path)
    wm = tmp_path / "beta_watermarks.json"
    wm.write_text("{}", encoding="utf-8")
    run, logged = _MirrorRun(head="worker/x"), []
    bp._commit_watermark(wm, REPO, 76, vault_root=vault, run=run, progress=logged.append)
    assert run.subs() == ["commit"]
    assert logged == []


def test_watermark_mirror_not_attempted_when_inplace_commit_fails(tmp_path):
    vault, wm = _vault_wm(tmp_path)
    run, logged = _MirrorRun(head="worker/x", fail_step="commit"), []
    bp._commit_watermark(wm, REPO, 76, vault_root=vault, run=run, progress=logged.append)
    assert run.subs() == ["commit"]
    assert len(logged) == 1 and "watermark commit failed (rc=1)" in logged[0]


@pytest.mark.parametrize("step", ["hash-object", "read-tree", "update-index",
                                  "write-tree", "commit-tree", "update-ref"])
def test_watermark_mirror_failure_is_nonfatal(tmp_path, step):
    vault, wm = _vault_wm(tmp_path)
    run, logged = _MirrorRun(head="worker/x", fail_step=step), []
    bp._commit_watermark(wm, REPO, 76, vault_root=vault, run=run, progress=logged.append)
    assert len(logged) == 1
    assert logged[0].startswith("beta-publish: watermark mirror to main failed")
    assert step in logged[0] and "(rc=1)" in logged[0]
    assert run.subs()[-1] == step
    if step != "update-ref":
        assert "update-ref" not in run.subs()
    assert not any(_FORBIDDEN & set(c["argv"]) for c in run.calls)


def test_watermark_mirror_failure_is_nonfatal_on_timeout(tmp_path):
    vault, wm = _vault_wm(tmp_path)
    run, logged = _MirrorRun(head="worker/x", raise_step="write-tree"), []
    bp._commit_watermark(wm, REPO, 76, vault_root=vault, run=run, progress=logged.append)
    assert logged == ["beta-publish: watermark mirror to main failed at write-tree "
                      "(rc=exc:TimeoutExpired) — in-place commit kept"]
    assert "commit-tree" not in run.subs()
    assert not Path(run.call("write-tree")["env"]["GIT_INDEX_FILE"]).parent.exists()


def _git(repo, *args) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, check=True).stdout.strip()


def test_watermark_mirror_real_git_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    vault, wm = _vault_wm(tmp_path)
    wm.write_text(json.dumps({REPO: {"last_version_code": 75}}), encoding="utf-8")
    (vault / "notes.md").write_text("original\n", encoding="utf-8")
    _git(tmp_path, "init", "-q", "-b", "main", str(vault))
    _git(vault, "config", "user.email", "test@example.invalid")
    _git(vault, "config", "user.name", "test")
    _git(vault, "config", "commit.gpgsign", "false")
    _git(vault, "add", "-A")
    _git(vault, "commit", "-q", "-m", "init")
    main0 = _git(vault, "rev-parse", "main")
    _git(vault, "checkout", "-q", "-b", "worker/x")
    (vault / "worker_only.txt").write_text("branch\n", encoding="utf-8")
    _git(vault, "add", "worker_only.txt")
    _git(vault, "commit", "-q", "-m", "worker change")
    (vault / "notes.md").write_text("staged edit\n", encoding="utf-8")
    _git(vault, "add", "notes.md")
    (vault / "scratch.txt").write_text("untracked\n", encoding="utf-8")
    wm.write_text(json.dumps({REPO: {"last_version_code": 76}}), encoding="utf-8")

    head_before = _git(vault, "symbolic-ref", "HEAD")
    status_before = _git(vault, "status", "--porcelain").splitlines()
    assert any(".macp/beta_watermarks.json" in ln for ln in status_before)

    logged: list[str] = []
    bp._commit_watermark(wm, REPO, 76, vault_root=vault, run=bp._run,
                         progress=logged.append)

    assert _git(vault, "symbolic-ref", "HEAD") == head_before == "refs/heads/worker/x"
    assert _git(vault, "status", "--porcelain").splitlines() == [
        ln for ln in status_before if ".macp/beta_watermarks.json" not in ln]
    assert _git(vault, "rev-parse", "main^") == main0
    assert (_git(vault, "rev-parse", "main:.macp/beta_watermarks.json")
            == _git(vault, "rev-parse", "worker/x:.macp/beta_watermarks.json"))
    subject = f"chore: beta watermark {REPO} -> 76"
    assert _git(vault, "log", "-1", "--format=%s", "main") == subject
    assert _git(vault, "log", "-1", "--format=%s", "worker/x") == subject
    assert subprocess.run(["git", "-C", str(vault), "cat-file", "-e", "main:worker_only.txt"],
                          capture_output=True).returncode != 0
    assert _git(vault, "show", "main:notes.md") == "original"
    assert len(logged) == 1 and "watermark also committed to main" in logged[0]


def test_build_argv_no_daemon_only_for_gradlew_and_once():
    """Regression (2026-10-16): beta builds must not attach to a shared daemon
    living in another unit's cgroup — --no-daemon is added once, gradlew only."""
    from todo_system.orchestrator.beta_publish import build_argv
    assert build_argv({"build_cmd": ["./gradlew", ":app:assembleBeta"]}, ["-Pa=1"]) == [
        "./gradlew", ":app:assembleBeta", "--no-daemon", "-Pa=1"]
    assert build_argv({"build_cmd": ["./gradlew", "--no-daemon", "x"]}, []) == [
        "./gradlew", "--no-daemon", "x"]
    assert build_argv({"build_cmd": ["make", "beta"]}, []) == ["make", "beta"]


# ===========================================================================
# Smoke gate wiring (opt-in config["smoke"]) — AC2
# ===========================================================================

from todo_system.orchestrator import beta_smoke  # noqa: E402 — grouped with the smoke block


def _add_smoke(env, smoke=None):
    """Add a `smoke` block to REPO's config (so the gate turns on)."""
    cfg = json.loads(env.config_path.read_text(encoding="utf-8"))
    cfg["repos"][REPO]["smoke"] = smoke if smoke is not None else {"scenario": []}
    env.config_path.write_text(json.dumps(cfg), encoding="utf-8")


def _fake_smoke(result):
    """A fake smoke_runner matching maybe_publish's call shape; records its calls
    so a test can assert it was (not) invoked."""
    calls: list[dict] = []

    def runner(repo_name, repo_dir, sha, cfg, *, run=None, notify=None, progress=None):
        calls.append({"repo": repo_name, "sha": sha, "cfg": cfg})
        return result

    runner.calls = calls                         # type: ignore[attr-defined]
    return runner


def _publish_smoke(env, run, smoke_runner, *, sha=SHA1, notify=None, task_path=None,
                   progress=None):
    return bp.maybe_publish(
        REPO, env.clone, sha, {"outcome": "x"},
        progress=progress or (lambda _m: None),
        config_path=env.config_path, watermark_path=env.watermark_path,
        lock_dir=env.lock_dir, notes_dir=env.notes_dir, task_path=task_path,
        run=run, notify=notify or (lambda _m: None),
        smoke_runner=smoke_runner, smoke_alert_path=env.smoke_alert)


def test_no_smoke_config_never_calls_runner_and_distributes(env, monkeypatch):
    """Byte-identical regression guard (AC2): a repo with no `smoke` block never
    touches the smoke_runner and distributes + bumps exactly as before."""
    # skippable must never even be consulted when there is no smoke block
    monkeypatch.setattr(beta_smoke, "smoke_skippable",
                        lambda *a, **k: pytest.fail("smoke_skippable called without a smoke block"))
    run = FakeRun()
    runner = _fake_smoke({"ok": True})
    note = _publish_smoke(env, run, runner)
    assert runner.calls == []                    # gate fully behind config.get("smoke")
    assert run.has("appdistribution:distribute")
    assert env.load_wm()["version_code"] == 76
    assert "발행" in note


def test_smoke_pass_distributes_and_bumps(env, monkeypatch):
    _add_smoke(env)
    monkeypatch.setattr(beta_smoke, "smoke_skippable", lambda *a, **k: False)
    run = FakeRun()
    runner = _fake_smoke({"ok": True, "kind": None, "detail": "smoke PASS"})
    note = _publish_smoke(env, run, runner)
    assert len(runner.calls) == 1
    assert run.has("appdistribution:distribute")
    assert env.load_wm()["version_code"] == 76
    assert "발행" in note


def test_smoke_fail_withholds_distribute_and_watermark(env, monkeypatch):
    _add_smoke(env)
    monkeypatch.setattr(beta_smoke, "smoke_skippable", lambda *a, **k: False)
    run = FakeRun()
    runner = _fake_smoke({"ok": False, "kind": "scenario", "detail": "skip: '운동' → '운동' (변화 없음)"})
    notified: list[str] = []
    task = env.clone.parent / "task.md"
    task.parent.mkdir(parents=True, exist_ok=True)
    task.write_text("# task\n\n## Progress Log\n", encoding="utf-8")

    note = _publish_smoke(env, run, runner, notify=notified.append, task_path=task)

    assert note is not None and "[베타 보류]" in note and "스모크 실패(scenario)" in note
    assert not run.has("appdistribution:distribute")      # distribute WITHHELD
    assert env.load_wm()["version_code"] == 75            # watermark UNCHANGED
    assert len(notified) == 1                             # exactly one (throttled) alert
    assert "베타 보류 — 스모크 실패(scenario)" in task.read_text(encoding="utf-8")


def test_smoke_fail_does_not_raise(env, monkeypatch):
    """The failure path returns a handled note (like the 0-tester abort), never
    raises — so the runner never surfaces the merge as retryable."""
    _add_smoke(env)
    monkeypatch.setattr(beta_smoke, "smoke_skippable", lambda *a, **k: False)
    runner = _fake_smoke({"ok": False, "kind": "crash", "detail": "FATAL=1 ANR=0"})
    note = _publish_smoke(env, FakeRun(), runner)         # must not raise
    assert "스모크 실패(crash)" in note


def test_smoke_alert_throttled_second_failure_silent(env, monkeypatch):
    """Two failures of the same (repo, kind) alert the human only once."""
    _add_smoke(env)
    monkeypatch.setattr(beta_smoke, "smoke_skippable", lambda *a, **k: False)
    runner = _fake_smoke({"ok": False, "kind": "scenario", "detail": "x"})
    notified: list[str] = []
    _publish_smoke(env, FakeRun(), runner, sha=SHA1, notify=notified.append)
    _publish_smoke(env, FakeRun(), runner, sha=SHA2, notify=notified.append)
    assert len(notified) == 1                             # second (within 6h) suppressed


def test_smoke_budget_trip_still_distributes(env, monkeypatch):
    """A monthly-budget trip (kind budget) skips the gate but STILL distributes —
    a cost ceiling must not block a month of betas."""
    _add_smoke(env)
    monkeypatch.setattr(beta_smoke, "smoke_skippable", lambda *a, **k: False)
    run = FakeRun()
    runner = _fake_smoke({"ok": False, "kind": "budget", "detail": "over cap"})
    notified: list[str] = []
    note = _publish_smoke(env, run, runner, notify=notified.append)
    assert run.has("appdistribution:distribute")          # distribute proceeds
    assert env.load_wm()["version_code"] == 76
    assert notified == []                                 # run_smoke owns the budget alert
    assert "발행" in note


def test_smoke_skippable_trivial_diff_skips_gate_and_distributes(env, monkeypatch):
    """A trivial diff since the last publish (prose/test/doc/resource) skips the
    node round-trip and still distributes, logging the reason."""
    _add_smoke(env)
    monkeypatch.setattr(beta_smoke, "smoke_skippable", lambda *a, **k: True)
    run = FakeRun()
    runner = _fake_smoke({"ok": True})
    task = env.clone.parent / "task2.md"
    task.parent.mkdir(parents=True, exist_ok=True)
    task.write_text("# task\n", encoding="utf-8")

    _publish_smoke(env, run, runner, task_path=task)

    assert runner.calls == []                             # gate skipped (no node round-trip)
    assert run.has("appdistribution:distribute")
    assert env.load_wm()["version_code"] == 76
    assert "스모크 생략" in task.read_text(encoding="utf-8")
