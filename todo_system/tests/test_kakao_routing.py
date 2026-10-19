"""Noise-tier routing tests (macp-kakao-alert-2-noise-tiers).

Four AC behaviours, each deterministic (clock + every state path injected, no
real send — [[tests-reach-real-alert-sender]] / todo_system/conftest.py):

* digest  → zero individual sends, the entry IS in the composed daily summary;
* silent  → zero sends, a log line remains;
* level unnamed → immediate send, byte-identical message (backward compat);
* same event key N times inside the window → merged into ONE delivery, the
  window's next send carrying "외 N건".

Worker-side plumbing (`kakao_notify`) is exercised against a recorder script so
"reached the send script" vs "queued" is observed at the real subprocess seam.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from todo_system import worker
from todo_system.kakao_format import (
    KAKAO_TEXT_LIMIT,
    LEVEL_DIGEST,
    LEVEL_SILENT,
    compose_digest,
    read_digest_entries,
    route,
)


class RoutePaths:
    """Explicit state paths + injected clock for deterministic route() calls."""

    def __init__(self, root: Path) -> None:
        self.digest = root / "kakao_digest.jsonl"
        self.dedup = root / "kakao_dedup.json"
        self.log = root / "kakao_route.log"

    def route(self, message: str, *, now: float, **kw):
        return route(message, now=now,
                     digest_path=self.digest, dedup_path=self.dedup,
                     log_path=self.log, **kw)

    def queued(self) -> list[dict]:
        if not self.digest.exists():
            return []
        return read_digest_entries(self.digest.read_text(encoding="utf-8"))

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""


# ---------------------------------------------------------------- route() core


def test_unnamed_level_is_an_untouched_immediate_send(tmp_path):
    p = RoutePaths(tmp_path)
    assert p.route("그대로 나가야 함", now=0.0) == ("send", "그대로 나가야 함")
    # backward compat means no routing state is even created
    assert not p.digest.exists() and not p.dedup.exists()


def test_unknown_level_string_fails_open_to_send(tmp_path):
    p = RoutePaths(tmp_path)
    verdict, final = p.route("오타 레벨", now=0.0, level="digset")
    assert (verdict, final) == ("send", "오타 레벨")


def test_digest_queues_without_sending_and_survives_into_the_summary(tmp_path):
    p = RoutePaths(tmp_path)
    verdict, final = p.route("🤖 Worker 시작: some-task — 조치 불필요",
                             now=100.0, level=LEVEL_DIGEST, event_key="worker-start:some-task")
    assert (verdict, final) == ("digest", None)
    [entry] = p.queued()
    assert entry["key"] == "worker-start:some-task"
    assert "some-task" in entry["msg"]
    assert "DIGEST" in p.log_text()
    # ...and the queued item is present in the composed daily summary
    assert "Worker 시작" in compose_digest(p.queued())


def test_silent_sends_nothing_but_leaves_a_log_line(tmp_path):
    p = RoutePaths(tmp_path)
    verdict, final = p.route("synced_no_restart — 기대된 동작",
                             now=100.0, level=LEVEL_SILENT, event_key="kis:noop")
    assert (verdict, final) == ("silent", None)
    assert p.queued() == []                       # not queued anywhere
    assert "SILENT" in p.log_text()
    assert "kis:noop" in p.log_text()


def test_same_event_key_merges_into_one_send_with_the_count(tmp_path):
    p = RoutePaths(tmp_path)
    assert p.route("첫 알림", now=0.0, event_key="k") == ("send", "첫 알림")
    assert p.route("둘째", now=100.0, event_key="k") == ("suppressed", None)
    assert p.route("셋째", now=200.0, event_key="k") == ("suppressed", None)
    # inside the 10-min window: exactly ONE delivery happened
    assert "SUPPRESSED" in p.log_text()
    # the window's next send surfaces what was merged
    verdict, final = p.route("넷째 (창 이후)", now=700.0, event_key="k")
    assert verdict == "send"
    assert final == "넷째 (창 이후)\n외 2건 합침"
    # and the counter reset — the send after that is clean
    verdict, final = p.route("다섯째", now=1400.0, event_key="k")
    assert (verdict, final) == ("send", "다섯째")


def test_different_event_keys_do_not_merge(tmp_path):
    p = RoutePaths(tmp_path)
    assert p.route("a", now=0.0, event_key="k1")[0] == "send"
    assert p.route("b", now=1.0, event_key="k2")[0] == "send"


def test_cooldown_extends_the_merge_window(tmp_path):
    p = RoutePaths(tmp_path)
    assert p.route("재알림 1", now=0.0, event_key="review:t", cooldown_s=3600)[0] == "send"
    # past the default 600s window but inside the cooldown → still suppressed
    assert p.route("재알림 2", now=1800.0, event_key="review:t", cooldown_s=3600) == \
        ("suppressed", None)
    verdict, final = p.route("재알림 3", now=3700.0, event_key="review:t", cooldown_s=3600)
    assert verdict == "send" and final.endswith("외 1건 합침")


def test_merge_suffix_is_dropped_rather_than_overflowing_the_budget(tmp_path):
    p = RoutePaths(tmp_path)
    long_msg = "가" * KAKAO_TEXT_LIMIT
    assert p.route(long_msg, now=0.0, event_key="k")[0] == "send"
    p.route(long_msg, now=1.0, event_key="k")
    verdict, final = p.route(long_msg, now=700.0, event_key="k")
    assert verdict == "send"
    assert final == long_msg                      # suffix would overflow → dropped whole


def test_corrupt_dedup_state_fails_open_to_send(tmp_path):
    p = RoutePaths(tmp_path)
    p.dedup.write_text("{not json", encoding="utf-8")
    assert p.route("깨진 상태", now=0.0, event_key="k")[0] == "send"


def test_stale_dedup_entries_are_pruned(tmp_path):
    p = RoutePaths(tmp_path)
    p.route("old", now=0.0, event_key="ancient")
    p.route("new", now=8 * 24 * 3600.0, event_key="fresh")
    state = json.loads(p.dedup.read_text(encoding="utf-8"))
    assert "fresh" in state and "ancient" not in state


def test_default_state_paths_honour_the_vault_dir_seam(tmp_path, monkeypatch):
    # the seam the autouse fixture and kakao_send.sh both rely on
    monkeypatch.setenv("KAKAO_VAULT_DIR", str(tmp_path / "v"))
    assert route("fyi", level=LEVEL_DIGEST) == ("digest", None)
    queue = tmp_path / "v" / "logs" / "kakao_digest.jsonl"
    assert queue.exists() and "fyi" in queue.read_text(encoding="utf-8")


# ---------------------------------------------------------------- digest memo


def test_compose_digest_groups_repeats_and_stays_within_budget():
    entries = (
        [{"msg": f"🤖 Worker 시작: task-{i} (mode=auto, 5 stage) — 조치 불필요"} for i in range(7)]
        + [{"msg": "[Planner] 태스크 1개 적재: some-new-task-slug"}]
    )
    text = compose_digest(entries)
    assert len(text) <= KAKAO_TEXT_LIMIT
    assert "8건" in text.splitlines()[0]          # headline always carries the count
    assert "×7" in text                           # repeats collapse, not 7 lines
    assert "[Planner]" in text                    # the singleton keeps its first line


def test_compose_digest_headline_flags_pending_human_decisions():
    entries = (
        [{"msg": f"🤖 Worker 시작: task-{i} (mode=auto, 5 stage) — 조치 불필요"} for i in range(3)]
        + [{"msg": "🙋 사람 결정 대기(followups human): [abc123] some-slug"},
           {"msg": "🙋 사람 결정 대기(followups human): [def456] other-slug"}]
    )
    headline = compose_digest(entries).splitlines()[0]
    assert "결정 2건 대기" in headline
    assert "조치 불필요" not in headline


def test_compose_digest_headline_keeps_no_action_wording_without_human_entries():
    entries = [{"msg": f"🤖 Worker 시작: task-{i} (mode=auto, 5 stage) — 조치 불필요"} for i in range(3)]
    headline = compose_digest(entries).splitlines()[0]
    assert "조치 불필요" in headline
    assert "결정" not in headline


def test_read_digest_entries_never_drops_a_corrupt_line():
    raw = json.dumps({"msg": "정상 항목"}, ensure_ascii=False) + "\n{torn line\n"
    entries = read_digest_entries(raw)
    assert [e["msg"] for e in entries] == ["정상 항목", "{torn line"]


# ------------------------------------------------------- worker.kakao_notify


def _recorder_script(tmp_path) -> tuple[Path, Path]:
    rec = tmp_path / "sent.log"
    script = tmp_path / "kakao_send_recorder.sh"
    script.write_text(f'#!/bin/sh\nprintf \'%s\\n\' "$1" >> "{rec}"\n', encoding="utf-8")
    script.chmod(0o755)
    return script, rec


def test_kakao_notify_without_level_reaches_the_send_script(tmp_path, monkeypatch):
    script, rec = _recorder_script(tmp_path)
    monkeypatch.setattr(worker, "KAKAO_SEND_SCRIPT", script)
    worker.kakao_notify("기존 호출 그대로")
    assert rec.read_text(encoding="utf-8").strip() == "기존 호출 그대로"


def test_kakao_notify_digest_never_spawns_and_lands_in_the_queue(tmp_path, monkeypatch):
    script, rec = _recorder_script(tmp_path)
    monkeypatch.setattr(worker, "KAKAO_SEND_SCRIPT", script)
    # KAKAO_VAULT_DIR is already tmp-scoped by the autouse fixture; re-pin for clarity
    monkeypatch.setenv("KAKAO_VAULT_DIR", str(tmp_path / "vault"))
    worker.kakao_notify("✅ Worker 완료: t — FYI", level=LEVEL_DIGEST, event_key="worker-done:t")
    assert not rec.exists()                       # zero individual sends
    queue = tmp_path / "vault" / "logs" / "kakao_digest.jsonl"
    [entry] = read_digest_entries(queue.read_text(encoding="utf-8"))
    assert entry["key"] == "worker-done:t"


def test_kakao_notify_silent_never_spawns_but_logs(tmp_path, monkeypatch):
    script, rec = _recorder_script(tmp_path)
    monkeypatch.setattr(worker, "KAKAO_SEND_SCRIPT", script)
    monkeypatch.setenv("KAKAO_VAULT_DIR", str(tmp_path / "vault"))
    worker.kakao_notify("기대된 동작", level=LEVEL_SILENT)
    assert not rec.exists()
    log = tmp_path / "vault" / "logs" / "kakao_route.log"
    assert "SILENT" in log.read_text(encoding="utf-8")


def test_red_control_dispatch_silence_alert_stays_immediate(tmp_path, monkeypatch):
    """RED control (AC 5, 4th kind — 정체): the pipeline-went-quiet page must
    reach kakao_notify WITHOUT a digest/silent level. Fires the real
    check_dispatch_silence against mocked state (the DispatchSilenceTest
    pattern) and records the exact kwargs the alert was routed with — flipping
    the call site to level=digest turns this red."""
    import datetime as dt
    from unittest import mock

    from todo_system.orchestrator import tasks as t
    from todo_system.orchestrator.assignment import Assignment

    monkeypatch.setattr(worker, "SILENCE_STATE_FILE", tmp_path / "silence.json")
    now = 1_000_000.0
    stamp = dt.datetime.fromtimestamp(now - 10 * 3600).isoformat()
    calls: list[tuple[str, dict]] = []

    def recorder(msg, link=None, **kw):
        calls.append((msg, kw))

    pend = [type("T", (), {"slug": "q0"})()]
    with mock.patch.object(t, "load_dispatchable", return_value=pend), \
         mock.patch("todo_system.usage_guard.check", return_value={"allowed": True}), \
         mock.patch("todo_system.orchestrator.assignment.Ledger.all",
                    return_value=[Assignment(task_id="t", agent_id="w", corr_id="c",
                                             updated_ts=stamp)]), \
         mock.patch.object(worker, "kakao_notify", new=recorder):
        out = worker.check_dispatch_silence(now=now)

    assert out is not None
    [(msg, kw)] = calls
    assert "정체" in msg
    assert kw.get("level") in (None, "immediate"), \
        "macp 정체 must ring the phone — digest/silent here loses the one stall detector"


# --------------------------------------------------- daily digest delivery


_REPO = Path(__file__).resolve().parents[2]


def _load_script(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, _REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


digest_send = _load_script("kakao_digest_send", "scripts/kakao_digest_send.py")


def _queue(tmp_path, entries) -> Path:
    q = tmp_path / "kakao_digest.jsonl"
    q.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
                 encoding="utf-8")
    return q


def test_flush_sends_one_memo_then_archives_and_empties_the_queue(tmp_path):
    q = _queue(tmp_path, [{"ts": "t1", "key": "a", "msg": "🤖 Worker 시작: x — FYI"},
                          {"ts": "t2", "key": "b", "msg": "[Planner] 태스크 1개 적재: y"}])
    sent_archive = tmp_path / "sent.jsonl"
    sends: list[str] = []

    rc = digest_send.flush(q, sent_archive, send=lambda t: sends.append(t) or True,
                           log=lambda *_: None)

    assert rc == 0
    assert len(sends) == 1                                  # ONE memo for the whole queue
    assert "2건" in sends[0] and "Worker 시작" in sends[0]
    assert q.read_bytes() == b""                            # drained...
    archived = sent_archive.read_text(encoding="utf-8")
    assert "Worker 시작" in archived and "[Planner]" in archived   # ...not lost


def test_flush_failure_preserves_the_queue_byte_for_byte(tmp_path):
    q = _queue(tmp_path, [{"ts": "t", "key": "a", "msg": "잃으면 안 되는 항목"}])
    before = q.read_bytes()
    sent_archive = tmp_path / "sent.jsonl"

    rc = digest_send.flush(q, sent_archive, send=lambda t: False, log=lambda *_: None)

    assert rc == 1
    assert q.read_bytes() == before                         # retry next timer run
    assert not sent_archive.exists()


def test_flush_empty_queue_sends_nothing(tmp_path):
    sends: list[str] = []
    q = tmp_path / "kakao_digest.jsonl"
    # missing file and empty file are both "nothing to send", exit 0
    assert digest_send.flush(q, tmp_path / "s", send=sends.append, log=lambda *_: None) == 0
    q.write_text("", encoding="utf-8")
    assert digest_send.flush(q, tmp_path / "s", send=sends.append, log=lambda *_: None) == 0
    assert sends == []


def test_flush_keeps_entries_that_arrive_during_the_send(tmp_path):
    q = _queue(tmp_path, [{"ts": "t1", "key": "a", "msg": "발송분"}])
    late_line = json.dumps({"ts": "t2", "key": "b", "msg": "발송 중 도착"},
                           ensure_ascii=False) + "\n"

    def send_and_append(_text: str) -> bool:
        with open(q, "a", encoding="utf-8") as fh:      # a route() append mid-send
            fh.write(late_line)
        return True

    rc = digest_send.flush(q, tmp_path / "sent.jsonl", send=send_and_append,
                           log=lambda *_: None)

    assert rc == 0
    assert q.read_text(encoding="utf-8") == late_line       # kept for tomorrow
    assert "발송분" in (tmp_path / "sent.jsonl").read_text(encoding="utf-8")

# NB: the gen_units macp-kakao-digest *unit* wiring test was removed when gen_units
# dropped to engine-core units only (the digest is an author-only vault unit). The
# kakao_digest_send.py *script* and its flush() behavior above are unaffected.
