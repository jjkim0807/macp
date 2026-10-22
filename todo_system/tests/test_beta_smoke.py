"""Unit tests for the opt-in pre-distribute emulator smoke gate.

Phase 1 covers the PURE pieces — uiautomator dump parsing, selector resolution,
the tap/wait/expect scenario DSL, the logcat crash scan, the smoke-skip
classifier, the cap/alert-throttle helpers — pinned by the 2026-10-22 물꼬
regression (after ⏭ the task title must change; a title that stays put, even
with the timer reset, or that only changes after the >10 s window, is a FAIL).

Phase 2 covers :func:`run_smoke`'s node round-trip with every external effect
driven through a fake ``run`` (always-stop on every exit path, never raises).
Nothing here shells out to gcloud / gradle / an emulator; all paths are tmp.
"""

from __future__ import annotations

import subprocess

from todo_system.orchestrator import beta_smoke as bs


# ---------------------------------------------------------------------------
# uiautomator dump fixtures (a realistic 물꼬 timer screen)
# ---------------------------------------------------------------------------

def _dump(title: str, timer: str, *, extra: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<hierarchy rotation="0">\n'
        '  <node index="0" class="android.widget.FrameLayout" bounds="[0,0][1080,2400]">\n'
        f'    <node index="0" resource-id="com.mulkko:id/task_title" class="android.widget.TextView"'
        f' text="{title}" content-desc="" bounds="[48,300][1032,420]"/>\n'
        f'    <node index="1" resource-id="com.mulkko:id/timer_text" class="android.widget.TextView"'
        f' text="{timer}" content-desc="타이머" bounds="[300,600][780,900]"/>\n'
        '    <node index="2" resource-id="com.mulkko:id/skip_button" class="android.widget.Button"'
        ' text="⏭" content-desc="건너뛰기" bounds="[840,2100][1032,2292]"/>\n'
        f'    {extra}\n'
        '  </node>\n'
        '</hierarchy>\n'
    )


def _nodes(title: str, timer: str):
    return bs.parse_ui_dump(_dump(title, timer))


TITLE = {"id": "task_title"}
TIMER = {"id": "timer_text"}


# ---------------------------------------------------------------------------
# parse_ui_dump
# ---------------------------------------------------------------------------

def test_parse_ui_dump_extracts_text_id_desc_bounds_center():
    nodes = _nodes("운동하기", "0:30")
    by_id = {n.resource_id: n for n in nodes}
    title = by_id["com.mulkko:id/task_title"]
    assert title.text == "운동하기"
    assert title.bounds == (48, 300, 1032, 420)
    assert title.center == ((48 + 1032) // 2, (300 + 420) // 2)
    skip = by_id["com.mulkko:id/skip_button"]
    assert skip.content_desc == "건너뛰기"
    assert skip.center == (936, 2196)


def test_parse_ui_dump_malformed_returns_empty_not_raise():
    assert bs.parse_ui_dump("<hierarchy><node bounds=") == []
    assert bs.parse_ui_dump("") == []
    assert bs.parse_ui_dump(None) == []  # type: ignore[arg-type]


def test_parse_ui_dump_missing_bounds_defaults_to_origin():
    nodes = bs.parse_ui_dump('<hierarchy><node text="x"/></hierarchy>')
    assert nodes[0].bounds == (0, 0, 0, 0)
    assert nodes[0].center == (0, 0)


# ---------------------------------------------------------------------------
# resolve_field / resolve_tap
# ---------------------------------------------------------------------------

def test_resolve_field_by_short_id_and_full_id():
    nodes = _nodes("운동하기", "0:30")
    assert bs.resolve_field(nodes, {"id": "task_title"}) == "운동하기"
    assert bs.resolve_field(nodes, {"id": "com.mulkko:id/task_title"}) == "운동하기"
    assert bs.resolve_field(nodes, {"id": "nope"}) is None
    assert bs.resolve_field(nodes, {}) is None


def test_resolve_tap_by_id_returns_center():
    nodes = _nodes("운동하기", "0:30")
    assert bs.resolve_tap(nodes, {"id": "skip_button"}) == (936, 2196)


def test_resolve_tap_by_desc_and_explicit_xy():
    nodes = _nodes("운동하기", "0:30")
    assert bs.resolve_tap(nodes, {"desc": "건너뛰기"}) == (936, 2196)
    assert bs.resolve_tap(nodes, {"xy": [10, 20]}) == (10, 20)
    assert bs.resolve_tap(nodes, {"id": "missing"}) is None


# ---------------------------------------------------------------------------
# evaluate_expect — the 물꼬 regression (AC3)
# ---------------------------------------------------------------------------

def test_text_changes_same_title_with_timer_reset_fails():
    """물꼬 regression (a): ⏭ leaves the SAME task title and only resets the
    timer → the title did not advance → text_changes FAIL."""
    series = [(0.0, _nodes("운동하기", "0:30")), (1.0, _nodes("운동하기", "0:05"))]
    ok, detail = bs.evaluate_expect("text_changes", {"field": TITLE, "within_s": 10}, series)
    assert ok is False
    assert "변화 없음" in detail


def test_text_changes_only_after_window_fails():
    """물꼬 regression (b): the title does change, but only after the 10 s
    window → within the window it is unchanged → FAIL (window timeout)."""
    series = [(0.0, _nodes("운동하기", "0:30")),
              (5.0, _nodes("운동하기", "0:20")),
              (12.0, _nodes("독서하기", "0:30"))]
    ok, _ = bs.evaluate_expect("text_changes", {"field": TITLE, "within_s": 10}, series)
    assert ok is False


def test_text_changes_within_window_passes():
    """물꼬 regression (c): the title advances within 10 s → PASS."""
    series = [(0.0, _nodes("운동하기", "0:30")), (3.0, _nodes("독서하기", "0:30"))]
    ok, detail = bs.evaluate_expect("text_changes", {"field": TITLE, "within_s": 10}, series)
    assert ok is True
    assert "독서하기" in detail


def test_text_changes_empty_window_fails():
    series = [(20.0, _nodes("운동하기", "0:30"))]      # only a sample past the window
    ok, detail = bs.evaluate_expect("text_changes", {"field": TITLE, "within_s": 10}, series)
    assert ok is False
    assert "창 안에 스냅샷 없음" in detail


def test_text_matches_pattern_and_contains():
    series = [(0.0, _nodes("운동하기", "0:30")), (2.0, _nodes("독서하기 완료!", "0:00"))]
    ok, _ = bs.evaluate_expect("text_matches", {"field": TITLE, "pattern": r"완료"}, series)
    assert ok is True
    ok2, _ = bs.evaluate_expect("text_matches", {"field": TITLE, "contains": "독서"}, series)
    assert ok2 is True
    ok3, _ = bs.evaluate_expect("text_matches", {"field": TITLE, "contains": "없음"}, series)
    assert ok3 is False


def test_text_matches_at_end_only():
    series = [(0.0, _nodes("완료!", "0:00")), (2.0, _nodes("진행중", "0:30"))]
    # "at: end" looks only at the last snapshot, where the title is 진행중
    ok, _ = bs.evaluate_expect("text_matches", {"field": TITLE, "pattern": "완료", "at": "end"}, series)
    assert ok is False


def test_timer_reset_detects_near_zero():
    series = [(0.0, _nodes("운동하기", "0:28")), (1.0, _nodes("운동하기", "0:02"))]
    ok, detail = bs.evaluate_expect("timer_reset", {"field": TIMER}, series)
    assert ok is True
    assert "0:02" in detail
    series2 = [(0.0, _nodes("운동하기", "0:28")), (1.0, _nodes("운동하기", "0:25"))]
    ok2, _ = bs.evaluate_expect("timer_reset", {"field": TIMER}, series2)
    assert ok2 is False


def test_unknown_expect_kind_fails_closed():
    ok, detail = bs.evaluate_expect("teleport", {"field": TITLE}, [(0.0, _nodes("x", "0:00"))])
    assert ok is False
    assert "알 수 없는" in detail


# ---------------------------------------------------------------------------
# scan_logcat
# ---------------------------------------------------------------------------

def test_scan_logcat_clean_passes():
    ok, detail = bs.scan_logcat("I/App: started\nD/App: tick\nI/App: ok\n")
    assert ok is True
    assert detail == "no FATAL/ANR"


def test_scan_logcat_fatal_fails_with_aggregate():
    text = ("I/App: started\n"
            "E/AndroidRuntime: FATAL EXCEPTION: main\n"
            "E/AndroidRuntime: java.lang.NullPointerException\n")
    ok, detail = bs.scan_logcat(text)
    assert ok is False
    assert "FATAL=1" in detail and "ANR=0" in detail


def test_scan_logcat_anr_fails():
    ok, detail = bs.scan_logcat("I/App: x\nE/ActivityManager: ANR in com.mulkko (com.mulkko/.Main)\n")
    assert ok is False
    assert "ANR=1" in detail


# ---------------------------------------------------------------------------
# smoke_skippable (AC6)
# ---------------------------------------------------------------------------

class FakeGit:
    """A minimal git seam for smoke_skippable: cat-file -e (rev presence) and
    git diff --name-only. ``cat_rc=0`` means every probed rev resolves, so
    ensure_probe_range returns True without a fetch."""

    def __init__(self, diff_out: str, *, cat_rc: int = 0, diff_rc: int = 0):
        self.diff_out = diff_out
        self.cat_rc = cat_rc
        self.diff_rc = diff_rc
        self.calls: list[list[str]] = []

    def __call__(self, argv, *, cwd=None, env=None, timeout=None):
        self.calls.append(list(argv))

        def cp(rc=0, out=""):
            return subprocess.CompletedProcess(argv, rc, out, "")

        if "cat-file" in argv:
            return cp(self.cat_rc)
        if "diff" in argv and "--name-only" in argv:
            return cp(self.diff_rc, self.diff_out)
        if "fetch" in argv:
            return cp(0)
        return cp(0)


BASE = "a" * 40
SHA = "b" * 40


def test_smoke_skippable_prose_tests_docs_strings_only_true():
    diff = ("README.md\n"
            "docs/guide.md\n"
            "app/src/test/java/com/mulkko/TimerTest.kt\n"
            "app/src/main/res/values-ko/strings.xml\n"
            "app/src/main/java/com/mulkko/ui/MulkkoCareCopy.kt\n")
    run = FakeGit(diff)
    assert bs.smoke_skippable("/tmp/clone", BASE, SHA, run=run) is True


def test_smoke_skippable_one_real_source_file_false():
    diff = "README.md\napp/src/main/java/com/mulkko/TimerViewModel.kt\n"
    run = FakeGit(diff)
    assert bs.smoke_skippable("/tmp/clone", BASE, SHA, run=run) is False


def test_smoke_skippable_no_base_is_false_without_git():
    run = FakeGit("README.md\n")
    assert bs.smoke_skippable("/tmp/clone", None, SHA, run=run) is False
    assert run.calls == []                       # first publish → no git, run the gate


def test_smoke_skippable_git_failure_is_false():
    run = FakeGit("README.md\n", diff_rc=1)
    assert bs.smoke_skippable("/tmp/clone", BASE, SHA, run=run) is False


def test_smoke_skippable_empty_diff_is_false():
    run = FakeGit("")                            # nothing resolved → run the gate
    assert bs.smoke_skippable("/tmp/clone", BASE, SHA, run=run) is False


# ---------------------------------------------------------------------------
# cap_seconds + alert_throttled
# ---------------------------------------------------------------------------

def test_cap_seconds_default_cfg_env(monkeypatch):
    monkeypatch.delenv(bs._CAP_ENV, raising=False)
    assert bs.cap_seconds({}) == 1800
    assert bs.cap_seconds({"cap_min": 5}) == 300
    assert bs.cap_seconds({"cap_min": 0}) == 1800
    assert bs.cap_seconds({"cap_min": "x"}) == 1800
    monkeypatch.setenv(bs._CAP_ENV, "42")
    assert bs.cap_seconds({"cap_min": 5}) == 42


def test_alert_throttled_fires_once_per_window():
    should, state = bs.alert_throttled({}, "if-then-habits", "scenario", now=1000.0)
    assert should is True
    # within the 6h window → suppressed, state unchanged
    should2, state2 = bs.alert_throttled(state, "if-then-habits", "scenario", now=1000.0 + 3600)
    assert should2 is False
    assert state2 == state
    # a DIFFERENT kind is independent
    should3, state3 = bs.alert_throttled(state2, "if-then-habits", "crash", now=1000.0 + 3600)
    assert should3 is True
    # after the window the same key fires again
    should4, _ = bs.alert_throttled(state3, "if-then-habits", "scenario", now=1000.0 + bs._ALERT_WINDOW_S + 1)
    assert should4 is True


def test_alert_throttled_does_not_mutate_input():
    original: dict = {}
    _, new = bs.alert_throttled(original, "r", "k", now=5.0)
    assert original == {}
    assert new == {"r:k": 5.0}
