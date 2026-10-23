"""UI language (ko|en) for engine log/CLI text.

The ko golden tests pin today's output byte for byte: the author's vault sets
``language = "ko"``, so routing these strings through ``i18n.t()`` must not
change a single character there. Each test sets ``MACP_LANGUAGE`` itself rather
than relying on the conftest pin.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import string
from pathlib import Path
from unittest import mock

from todo_system import cli, i18n
from todo_system import kakao_format
from todo_system.orchestrator import followups, human, stages, tasks
from todo_system.orchestrator import message as m
from todo_system.orchestrator import resume
from todo_system.orchestrator.assignment import AWAITING_HUMAN, FAILED, Assignment, Ledger
from todo_system.orchestrator.mailbox import FileMailbox
from todo_system.orchestrator.orchestrator import Orchestrator, Task
from todo_system.orchestrator.registry import Agent, Registry

REPO = "https://github.com/x/repo.git"


def _vault(root: Path, slug: str) -> Path:
    """Todo/items/<slug>.md + an awaiting_human assignment — what `resume` needs."""
    (root / "Todo" / "items").mkdir(parents=True)
    (root / "Todo" / "items" / f"{slug}.md").write_text(
        f"---\nstatus: open\n---\n\n# {slug}\n", encoding="utf-8")
    Ledger(root / "Worker" / "assignments").save(
        Assignment(task_id=slug, agent_id="ai-1", corr_id="c1", state=AWAITING_HUMAN))
    return root


def _resume_out(root: Path, slug: str, after_commit=None) -> tuple[int, str]:
    args = argparse.Namespace(slug=slug, after_commit=after_commit,
                              expire_hours=24, note=None)
    buf = io.StringIO()
    with mock.patch.object(cli, "ROOT", root), contextlib.redirect_stdout(buf):
        rc = cli.cmd_resume(args)
    return rc, buf.getvalue()


def _orch(tmp: Path) -> Orchestrator:
    reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
    return Orchestrator(FileMailbox(tmp / "agents"), reg, Ledger(tmp / "assignments"),
                        notify=lambda *a, **k: None)


def _hold_note(orch: Orchestrator, owner: str, *reason: str) -> str:
    """Seed a deferred row, let `_note_checkout_hold` rewrite it, return the note."""
    orch.ledger.save(Assignment(
        task_id="t", agent_id="w", corr_id="c", kind="ai-local",
        state=FAILED, checkout_busy=True, payload={"repo": REPO}, last_note="old"))
    orch._note_checkout_hold(Task("t", {"repo": REPO}), REPO, owner, *reason)
    return orch.ledger.load("t").last_note


# --- ko: byte-identical to the pre-i18n hard-coded strings ---------------------


def test_ko_resume_output_is_byte_identical_with_sha(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    root = _vault(tmp_path, "s")
    rc, out = _resume_out(root, "s", after_commit="a1b2c3d")
    assert rc == 0
    assert out == (
        f"resume 예약: {resume.record_path(root, 's')}\n"
        "  조건: 라이브 체크아웃 HEAD가 main + a1b2c3d가 HEAD의 조상 + state가 awaiting_human/failed\n"
        "  만료: 24h 뒤 (만료되면 예약만 지워지고 경보 1회)\n"
        "  다음 오케스트레이터 틱이 조건을 판정합니다 — assignment json은 건드리지 마세요.\n")


def test_ko_resume_output_is_byte_identical_without_sha(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    root = _vault(tmp_path, "s")
    rc, out = _resume_out(root, "s")
    assert rc == 0
    assert out == (
        f"resume 예약: {resume.record_path(root, 's')}\n"
        "  조건: 라이브 체크아웃 HEAD가 main + 커밋 조건 없음 + state가 awaiting_human/failed\n"
        "  만료: 24h 뒤 (만료되면 예약만 지워지고 경보 1회)\n"
        "  다음 오케스트레이터 틱이 조건을 판정합니다 — assignment json은 건드리지 마세요.\n")


def test_ko_resume_refused_prefix(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    root = _vault(tmp_path, "s")
    rc, out = _resume_out(root, "__no_such_slug__")
    assert rc == 2
    assert out.startswith("resume 예약 거부: ")


def test_ko_hold_note_is_byte_identical(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    orch = _orch(tmp_path)
    assert _hold_note(orch, "other-slug", "겹침 있음") == (
        "대기 — 같은 체크아웃(repo)에서 `other-slug` 실행 중; 사유: 겹침 있음")
    assert _hold_note(orch, "") == (
        "대기 — 같은 체크아웃(repo)에서 다른 태스크 실행 중; 사유: 직렬 대기")


def test_ko_hold_reasons_are_byte_identical(monkeypatch):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")

    def held(slug, **payload):
        return Assignment(task_id=slug, agent_id="w", corr_id="c", payload=dict(payload))

    r = Orchestrator._hold_reason
    assert r("t", {"touches": ["c/**"]},
             [held("a", touches=["a/**"]), held("b", touches=["b/**"])], 2) == (
        "상한 도달 — repo당 동시 실행 최대 2개")
    assert r("t", {}, [held("a", touches=["a/**"])], 2) == "겹침 미판정 (touches 없음)"
    assert r("t", {"touches": ["b/**"], "depends_on": ["a"]},
             [held("a", touches=["a/**"])], 2) == "depends_on 직렬"
    assert r("t", {"touches": ["a/x.py"]}, [held("a", touches=["a/**"])], 2) == "겹침 있음"
    assert r("t", {"touches": ["a/**"]}, [], 2) == "직렬 대기"


# --- en: the same points in English ---------------------------------------------


def test_en_hold_reasons(monkeypatch):
    monkeypatch.setenv("MACP_LANGUAGE", "en")

    def held(slug, **payload):
        return Assignment(task_id=slug, agent_id="w", corr_id="c", payload=dict(payload))

    r = Orchestrator._hold_reason
    assert r("t", {"touches": ["c/**"]},
             [held("a", touches=["a/**"]), held("b", touches=["b/**"])], 2) == (
        "cap reached — at most 2 concurrent runs per repo")
    assert r("t", {}, [held("a", touches=["a/**"])], 2) == "overlap undetermined (no touches)"
    assert r("t", {"touches": ["b/**"], "depends_on": ["a"]},
             [held("a", touches=["a/**"])], 2) == "serial via depends_on"
    assert r("t", {"touches": ["a/x.py"]}, [held("a", touches=["a/**"])], 2) == "touches overlap"
    assert r("t", {"touches": ["a/**"]}, [], 2) == "waiting in series"


def test_en_hold_note(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    orch = _orch(tmp_path)
    assert _hold_note(orch, "other-slug", "touches overlap") == (
        "waiting — `other-slug` is running in the same checkout (repo); reason: touches overlap")
    assert _hold_note(orch, "") == (
        "waiting — another task is running in the same checkout (repo); reason: waiting in series")


def test_en_resume_output(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    root = _vault(tmp_path, "s")
    tail = ("  expires: in 24h (on expiry only the reservation is removed, with one alert)\n"
            "  the next orchestrator tick checks the conditions — do not edit the assignment json.\n")
    rc, out = _resume_out(root, "s", after_commit="a1b2c3d")
    assert rc == 0
    assert out == (
        f"resume reserved: {resume.record_path(root, 's')}\n"
        "  condition: live checkout HEAD is main + a1b2c3d is an ancestor of HEAD"
        " + state is awaiting_human/failed\n" + tail)
    rc, out = _resume_out(root, "s")
    assert rc == 0
    assert out == (
        f"resume reserved: {resume.record_path(root, 's')}\n"
        "  condition: live checkout HEAD is main + no commit condition"
        " + state is awaiting_human/failed\n" + tail)
    # The ResumeError text comes from resume.py (out of scope, still Korean).
    rc, out = _resume_out(root, "__no_such_slug__")
    assert rc == 2
    assert out.startswith("resume reservation refused: ")


def test_note_truncation_is_kept(monkeypatch, tmp_path):
    orch = _orch(tmp_path)
    for lang in ("ko", "en"):
        monkeypatch.setenv("MACP_LANGUAGE", lang)
        assert len(_hold_note(orch, "other-slug", "x" * 300)) == 200


# --- the table ------------------------------------------------------------------


def _fields(template: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


def test_ko_and_en_have_identical_key_sets():
    assert set(i18n.MESSAGES) == {"ko", "en"}
    assert set(i18n.MESSAGES["ko"]) == set(i18n.MESSAGES["en"])
    for lang, table in i18n.MESSAGES.items():
        assert all(v.strip() for v in table.values()), lang


def test_placeholders_match_across_languages():
    for key, ko in i18n.MESSAGES["ko"].items():
        assert _fields(ko) == _fields(i18n.MESSAGES["en"][key]), key


def test_t_follows_ui_language(monkeypatch):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    assert i18n.t("hold.overlap") == "겹침 있음"
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    assert i18n.t("hold.overlap") == "touches overlap"


def test_missing_en_key_does_not_fall_back_to_ko(monkeypatch):
    monkeypatch.setitem(i18n.MESSAGES["ko"], "only.ko", "한국어")
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    assert i18n.t("only.ko") == "only.ko"
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    assert i18n.t("only.ko") == "한국어"


def test_t_never_raises_on_bad_format_args(monkeypatch):
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    assert i18n.t("hold.cap", wrong=1) == i18n.MESSAGES["en"]["hold.cap"]
    assert i18n.t("hold.cap", cap=3) == "cap reached — at most 3 concurrent runs per repo"


# --- the dashboard fragment table ------------------------------------------------

_HANGUL = re.compile("[가-힣]")
# Characters that carry syntax where a fragment sits (markup, JS literals, comments).
_SYNTAX = "'\"`\\<>{}$&@\n"


def test_dashboard_ko_and_en_have_identical_key_sets():
    assert set(i18n.DASHBOARD) == {"ko", "en"}
    assert set(i18n.DASHBOARD["ko"]) == set(i18n.DASHBOARD["en"])
    assert all(i18n.DASHBOARD["ko"].values())          # ko is the marker source text


def test_dashboard_en_has_no_hangul():
    for key, v in i18n.DASHBOARD["en"].items():
        assert not _HANGUL.search(v), key


def test_dashboard_ko_fragments_are_distinct():
    ko = list(i18n.DASHBOARD["ko"].values())
    assert len(ko) == len(set(ko))


def test_dashboard_en_adds_no_syntax_characters():
    # en may only carry the quote/bracket/brace characters its ko counterpart has,
    # so swapping a fragment can't break the markup or script around it.
    for key, ko in i18n.DASHBOARD["ko"].items():
        en = i18n.DASHBOARD["en"][key]
        assert sorted(c for c in en if c in _SYNTAX) == sorted(c for c in ko if c in _SYNTAX), key


def test_dashboard_phrases_follows_ui_language(monkeypatch):
    assert i18n.dashboard_phrases("ko") is i18n.DASHBOARD["ko"]
    assert i18n.dashboard_phrases("en") is i18n.DASHBOARD["en"]
    assert i18n.dashboard_phrases("xx") is i18n.DASHBOARD["en"]
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    assert i18n.dashboard_phrases() is i18n.DASHBOARD["ko"]
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    assert i18n.dashboard_phrases() is i18n.DASHBOARD["en"]


# --- alerts: ko golden ------------------------------------------------------------
# Slack/Kakao alert bodies, pinned byte for byte as they read before they went
# through i18n.t(). Fixtures are ASCII so the en tests below can reuse them: task
# titles, slugs and blockers are data, not translated text.

SLUG = "demo-task"
FID = "abcdef012345"


def _alert_orch(tmp: Path, monkeypatch) -> tuple[Orchestrator, list]:
    """An Orchestrator whose alerts land in `sent` as (text, link): the router
    always says send, and `_task_title` never reads a vault md."""
    sent: list = []
    reg = Registry([Agent(id="w", kind="ai-local", projects=["*"], repo="~/x")])
    orch = Orchestrator(FileMailbox(tmp / "agents"), reg, Ledger(tmp / "assignments"),
                        notify=lambda text, link=None: sent.append((text, link)),
                        alert_route=lambda text, **k: ("send", text))
    monkeypatch.setattr(orch, "_task_title", lambda slug: slug)
    return orch, sent


def _row(**kw) -> Assignment:
    return Assignment(task_id=SLUG, agent_id="w", corr_id="c", **kw)


def _env(type_: str, **payload) -> m.Envelope:
    return m.Envelope(to=m.ORCHESTRATOR, type=type_, frm="w", task_id=SLUG,
                      payload=dict(payload))


def _review_texts(orch, sent) -> dict[str, tuple]:
    """The four `_notify_review` framings, keyed by branch."""
    out = {}
    orch._notify_review(_row(blocked_on="needs upstream merge"), _env(m.FAIL))
    out["blocked"] = sent.pop()
    orch._notify_review(_row(), _env(m.RESULT, pr_url="https://example.com/pr/1"))
    out["pr"] = sent.pop()
    orch._notify_review(_row(), _env(m.RESULT, run_dir="Worker/runs/r1", stages=["delegate"]))
    out["result_run_dir"] = sent.pop()
    orch._notify_review(_row(), _env(m.RESULT, stages=["delegate"]))
    out["result_no_run_dir"] = sent.pop()
    orch._notify_review(_row(), _env(m.FAIL))
    out["failure"] = sent.pop()
    return out


def _account_texts(orch, sent, monkeypatch) -> tuple[str, str]:
    from todo_system.macp import accounts
    monkeypatch.setattr(accounts, "claim_paused_alert", lambda *a, **k: True)
    orch._notify_account_switch(_row(), _env(m.FAIL, account_switch={
        "from": "dev", "to": "alt", "reason": "usage"}))
    switched = sent.pop()[0]
    orch._notify_account_switch(_row(), _env(m.FAIL, account_switch={
        "from": "dev", "to": None, "reason": "usage"}))
    return switched, sent.pop()[0]


def _misc_alert_texts(orch, sent) -> dict[str, str]:
    out = {}
    orch._notify_orphaned_commits(_row(), _env(m.FAIL, run_dir="Worker/runs/r1", orphaned_commits={
        "shas": ["a" * 40, "b" * 40], "count": 2, "tip": "c" * 40, "branch": f"worker/{SLUG}"}))
    out["orphaned"] = sent.pop()[0]
    orch._notify_poison(_row(refail_count=3, last_note="boom on retry"))
    out["poison"] = sent.pop()[0]
    orch._notify_poison(_row(refail_count=3))
    out["poison_no_note"] = sent.pop()[0]
    orch._notify_timeout_park(_row(timeout_streak=2))
    out["timeout_park"] = sent.pop()[0]
    orch._notify_blocker_cancelled(_row(), "up-task")
    out["blocker_cancelled"] = sent.pop()[0]
    out["propagate"] = orch._compose_propagate_alert("core", ["a-check", "b-check"])
    orch._notify_parent_complete([{"slug": "p1", "title": "Parent one"}, {"slug": "p2"}])
    out["parent_complete"] = sent.pop()[0]
    return out


def _closed_unmerged_text(orch, sent, monkeypatch) -> tuple:
    orch.ledger.save(_row(state=AWAITING_HUMAN, payload={"branch": f"worker/{SLUG}", "repo": "o/r"}))
    monkeypatch.setattr(stages, "gh_pr_merged", lambda *a, **k: None)
    monkeypatch.setattr(stages, "gh_pr_state", lambda *a, **k: "CLOSED")
    monkeypatch.setattr(tasks, "task_status", lambda *a, **k: "open")
    with contextlib.redirect_stdout(io.StringIO()):
        orch.reconcile_merged()
    return sent.pop()


def _waiting_rec(**kw) -> dict:
    rec = {"id": FID, "source_task": SLUG, "section": "다음 단계", "text": "do the thing",
           "status": "awaiting_human", "human_reason": "needs a person"}
    rec.update(kw)
    return rec


APPROVED_REC = {"id": "0123456789ab", "source_task": "other-task", "section": "다음 단계",
                "text": "approved thing", "status": "open", "approved": True,
                "approved_ts": "2026-10-23T10:00:00"}


def _followup_files(tmp: Path, monkeypatch, *recs: dict) -> tuple[Path, Path]:
    import json
    fpath = tmp / "_state" / "followups.jsonl"
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs),
                     encoding="utf-8")
    monkeypatch.setattr(followups, "DIGEST_PATH", tmp / "logs" / "kakao_digest.jsonl")
    monkeypatch.setattr(followups, "ROUTE_LOG_PATH", tmp / "logs" / "kakao_route.log")
    return fpath, tmp / "_state" / "followups_rejected.jsonl"


def _cli_replies(tmp: Path, monkeypatch) -> tuple[str, str, str]:
    fpath, rpath = _followup_files(
        tmp, monkeypatch, _waiting_rec(), _waiting_rec(id="111111111111"),
        _waiting_rec(id="222222222222", status="done", resolution="rejected: old"))
    rc1, approved = followups.approve(FID, path=fpath, rejections_path=rpath)
    rc2, dismissed = followups.dismiss("111111111111", "not needed", path=fpath)
    rc3, rerouted = followups.reroute("222222222222", path=fpath, rejections_path=rpath)
    assert (rc1, rc2, rc3) == (0, 0, 0)
    return approved, dismissed, rerouted


def test_ko_review_alerts_are_byte_identical(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    orch, sent = _alert_orch(tmp_path, monkeypatch)
    got = _review_texts(orch, sent)
    assert got["blocked"] == (
        "⛔ 파이프라인이 선행 조건 미충족으로 멈췄어요 — 조건을 갖추거나 이 태스크를 접을지 판단해주세요\n"
        f"📋 {SLUG}\n사유: needs upstream merge", None)
    assert got["pr"] == (
        f"✅ 파이프라인 완료 · PR 열림(미머지) — 리뷰 후 머지해주세요\n📋 {SLUG}",
        "https://example.com/pr/1")
    assert got["result_run_dir"] == (
        "✅ 정상 완료 · 머지할 PR 없음(0-commit) — 결과 확인 후 태스크를 닫아주세요\n"
        f"📋 {SLUG}\n결과: Worker/runs/r1/delegate.md", None)
    assert got["result_no_run_dir"] == (
        "✅ 정상 완료 · 머지할 PR 없음(0-commit) — 결과 확인 후 태스크를 닫아주세요\n"
        f"📋 {SLUG}\n결과: Worker/runs/ 아래 {SLUG} 최신 run 의 delegate.md", None)
    assert got["failure"] == (
        "❌ PR 없음 · 머지할 것이 없어요 — 실패 원인 확인이 필요합니다\n"
        f"📋 {SLUG}\nrun: Worker/runs/ 아래 {SLUG} 최신 run", None)


def test_ko_account_alerts_are_byte_identical(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    orch, sent = _alert_orch(tmp_path, monkeypatch)
    switched, paused = _account_texts(orch, sent, monkeypatch)
    assert switched == (
        "🔁 Claude 계정 전환 dev → alt (usage) — 다음 틱부터 새 계정으로 재디스패치됩니다\n"
        f"📋 {SLUG}")
    assert paused == (
        "⏸ 모든 Claude 계정 사용 불가 (dev: usage) — 디스패치 PAUSED. "
        f"윈도 리셋·재로그인·macp account use 중 하나로 자동 해제\n📋 {SLUG}")


def test_ko_other_orchestrator_alerts_are_byte_identical(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    orch, sent = _alert_orch(tmp_path, monkeypatch)
    got = _misc_alert_texts(orch, sent)
    assert got["orphaned"] == (
        "🧨 이전 시도의 미푸시 커밋 2개가 되감겼어요 — 되살릴지 무시할지 판단해주세요\n"
        f"📋 {SLUG}\ntip cccccccc\ngit cherry-pick aaaaaaaa bbbbbbbb\n"
        f"branch worker/{SLUG}\nrun Worker/runs/r1/stages.json")
    assert got["poison"] == (
        "🚧 3회 재실패로 자동 재시도를 멈췄어요 — 원인 확인 후 수동 처리가 필요합니다\n"
        f"📋 {SLUG}\n최근 사유: boom on retry")
    assert got["poison_no_note"] == (
        f"🚧 3회 재실패로 자동 재시도를 멈췄어요 — 원인 확인 후 수동 처리가 필요합니다\n📋 {SLUG}")
    assert got["timeout_park"] == (
        "⏱️ 2회 연속 stage timeout으로 대기 전환 — 더 잘게 쪼개거나 long_job으로 돌릴지 판단이 필요합니다\n"
        f"📋 {SLUG}\n작업은 origin worker 브랜치에 checkpoint됨(유실 아님)")
    assert got["blocker_cancelled"] == (
        "🔗 대기하던 상류 태스크가 취소돼 진행 불가 — 이 태스크가 아직 필요한지, 함께 취소할지 판단이 필요합니다\n"
        f"📋 {SLUG}\n상류: up-task")
    assert got["propagate"] == (
        "🔗 상류 'core' 변경으로 하류 통합검증 2개를 만들었어요 — 검증 결과를 확인해주세요\n"
        "a-check, b-check")
    assert got["parent_complete"] == (
        "🧭 하위 작업이 모두 끝난 상위 태스크 2건이 아직 open이에요 — 닫을지 후속 작업을 추가할지 확인해주세요\n"
        "- p1 (Parent one)\n- p2 (-)")


def test_ko_closed_unmerged_alert_is_byte_identical(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    orch, sent = _alert_orch(tmp_path, monkeypatch)
    assert _closed_unmerged_text(orch, sent, monkeypatch) == (
        f"⚠️ delegate PR이 머지 없이 닫혔어요 — 다시 열지·재작업할지·접을지 판단해주세요\n📋 {SLUG}",
        None)


def test_ko_followup_digest_notice_is_byte_identical(monkeypatch):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    assert followups.HUMAN_DIGEST_HEAD == "🙋 사람 결정 대기(followups human)"
    assert kakao_format._HUMAN_DECISION_HEAD == followups.HUMAN_DIGEST_HEAD
    assert followups._digest_message(_waiting_rec()) == (
        f"🙋 사람 결정 대기(followups human): [{FID}] {SLUG}\n"
        "→ python -m todo_system.orchestrator.followups human\n"
        "사유: needs a person")


def test_ko_render_human_is_byte_identical(monkeypatch):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    cli_ = followups.CLI
    assert followups.render_human([]) == "사람 결정 대기 없음\n"
    assert followups.render_human([_waiting_rec(), APPROVED_REC]) == (
        "사람 결정 대기 1건\n"
        "\n"
        f"- [{FID}] from {SLUG} (다음 단계)\n"
        "  본문: do the thing\n"
        "  사유: needs a person\n"
        "\n"
        "승인됨 — planner 태스크화 대기 1건\n"
        "- [0123456789ab] from other-task (승인 2026-10-23T10:00:00)\n"
        "\n"
        f"승인:   {cli_} approve <id>\n"
        f"기각:   {cli_} dismiss <id> --reason <사유>\n"
        f"재분류: {cli_} reroute <id>   (rejected → 사람 결정 대기)\n")
    assert followups.render_human([APPROVED_REC]).startswith(
        "사람 결정 대기 없음\n\n승인됨 — planner 태스크화 대기 1건\n")


def test_ko_followup_cli_replies_are_byte_identical(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    approved, dismissed, rerouted = _cli_replies(tmp_path, monkeypatch)
    assert approved == (
        f"승인: {FID} → open+approved. 다음 planner 실행 때 '사람 승인됨 — "
        "태스크화 대상'으로 주입된다. planner는 run 완료·태스크 변경·24h "
        "하트비트 때만 돌므로, 조용한 날엔 최대 약 24시간 걸린다.")
    assert dismissed == "기각: 111111111111 → done (dismissed: not needed)"
    assert rerouted == "재분류: 222222222222 → awaiting_human"


HUMAN_KO_HEADS = {
    m.TASK_ASSIGN: "🙋 새 할당이 도착했어요",
    m.QUERY: "❓ 확인이 필요한 질문이 있어요",
    m.CANCEL: "🚫 할당이 취소됐어요",
    m.WAKE: "🔔 확인 요청 알림이에요",
    m.PING: "📡 핑",
    m.RESULT: "✉️ 메시지가 도착했어요",          # any other type: the default head
}


def _human_agent() -> Agent:
    return Agent(id="human-1", kind="human", projects=["*"])


def test_ko_human_nudges_are_byte_identical(monkeypatch):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    for type_, head in HUMAN_KO_HEADS.items():
        env = m.Envelope(to="human-1", type=type_, task_id=SLUG,
                         payload={"summary": "please review"})
        assert human.compose_kakao(env, _human_agent()) == (
            f"{head} — 확인해주세요\n📋 {SLUG}\nplease review"), type_
    bare = m.Envelope(to="human-1", type=m.RESULT)
    assert human.compose_kakao(bare, _human_agent()) == "✉️ 메시지가 도착했어요 — 확인해주세요"


# --- alerts: en -------------------------------------------------------------------
# The same alerts with MACP_LANGUAGE=en. Every fixture above is ASCII, so any
# Hangul left in the output came from the engine's own text.


def _no_hangul(text: str, what: str = "") -> None:
    assert not _HANGUL.search(text), f"{what}: {text!r}"


def test_messages_en_has_no_hangul():
    for key, v in i18n.MESSAGES["en"].items():
        assert not _HANGUL.search(v), key


# The three representative alerts (acceptance criterion: "대표 알림 3종").

def test_en_review_blocked_alert_has_no_hangul(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    orch, sent = _alert_orch(tmp_path, monkeypatch)
    text, link = _review_texts(orch, sent)["blocked"]
    assert text == (
        "⛔ Pipeline stopped on an unmet precondition — meet it or decide whether to drop this task\n"
        f"📋 {SLUG}\nreason: needs upstream merge")
    assert link is None
    _no_hangul(text)


def test_en_poison_alert_has_no_hangul(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    orch, sent = _alert_orch(tmp_path, monkeypatch)
    text = _misc_alert_texts(orch, sent)["poison"]
    assert text == (
        "🚧 Auto-retry stopped after 3 repeated failures — find the cause and handle it by hand\n"
        f"📋 {SLUG}\nlast reason: boom on retry")
    _no_hangul(text)


def test_en_followup_digest_notice_has_no_hangul(monkeypatch):
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    notice = followups._digest_message(_waiting_rec())
    assert notice == (
        f"🙋 Decision pending (followups human): [{FID}] {SLUG}\n"
        "→ python -m todo_system.orchestrator.followups human\n"
        "reason: needs a person")
    digest = kakao_format.compose_digest([{"msg": notice}])
    assert digest.splitlines()[0] == "📮 macp digest — 1 alert(s), 1 decision(s) pending"
    assert f"[{FID}]" in digest
    _no_hangul(notice)
    _no_hangul(digest)


# The rest of the in-scope alerts.

def test_en_other_review_framings_have_no_hangul(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    orch, sent = _alert_orch(tmp_path, monkeypatch)
    got = _review_texts(orch, sent)
    assert got["pr"] == (
        f"✅ Pipeline done · PR open (not merged) — review and merge it\n📋 {SLUG}",
        "https://example.com/pr/1")
    assert got["result_run_dir"][0].endswith("\nresult: Worker/runs/r1/delegate.md")
    assert got["result_no_run_dir"][0].endswith(
        f"\nresult: delegate.md of the latest run of {SLUG} under Worker/runs/")
    assert got["failure"][0] == (
        "❌ No PR · nothing to merge — the failure cause needs a look\n"
        f"📋 {SLUG}\nrun: latest run of {SLUG} under Worker/runs/")
    for branch, (text, _link) in got.items():
        _no_hangul(text, branch)


def test_en_account_alerts_have_no_hangul(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    orch, sent = _alert_orch(tmp_path, monkeypatch)
    switched, paused = _account_texts(orch, sent, monkeypatch)
    assert switched.startswith("🔁 Claude account switched dev → alt (usage) — ")
    assert paused.startswith("⏸ No Claude account usable (dev: usage) — dispatch PAUSED.")
    _no_hangul(switched, "switch")
    _no_hangul(paused, "paused")


def test_en_other_orchestrator_alerts_have_no_hangul(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    orch, sent = _alert_orch(tmp_path, monkeypatch)
    got = _misc_alert_texts(orch, sent)
    assert got["orphaned"].startswith("🧨 2 unpushed commit(s) from an earlier attempt")
    assert got["timeout_park"].splitlines()[-1] == (
        "work is checkpointed on the origin worker branch (not lost)")
    assert got["blocker_cancelled"].splitlines()[-1] == "upstream: up-task"
    assert got["propagate"].startswith("🔗 Upstream 'core' changed, so 2 downstream")
    assert got["parent_complete"].startswith("🧭 2 parent task(s) have all subtasks finished")
    for kind, text in got.items():
        _no_hangul(text, kind)


def test_en_closed_unmerged_alert_has_no_hangul(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    orch, sent = _alert_orch(tmp_path, monkeypatch)
    text, _link = _closed_unmerged_text(orch, sent, monkeypatch)
    assert text == (
        "⚠️ delegate PR was closed without merging — decide whether to reopen, redo or drop it\n"
        f"📋 {SLUG}")
    _no_hangul(text)


def test_en_render_human_has_no_hangul(monkeypatch):
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    assert followups.render_human([]) == "no human decisions pending\n"
    out = followups.render_human([_waiting_rec(), APPROVED_REC])
    assert out.splitlines()[:6] == [
        "1 human decision(s) pending",
        "",
        f"- [{FID}] from {SLUG} (Next steps)",
        "  text: do the thing",
        "  reason: needs a person",
        "",
    ]
    assert "- [0123456789ab] from other-task (approved 2026-10-23T10:00:00)" in out
    _no_hangul(out)
    _no_hangul(followups.render_human([APPROVED_REC]), "approved only")
    _no_hangul(followups.render_human([_waiting_rec(section="사람이 검토해야 할 부분")]),
               "review section")


def test_en_followup_cli_replies_have_no_hangul(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    approved, dismissed, rerouted = _cli_replies(tmp_path, monkeypatch)
    assert approved.startswith(f"approved: {FID} → open+approved. ")
    assert dismissed == "dismissed: 111111111111 → done (dismissed: not needed)"
    assert rerouted == "rerouted: 222222222222 → awaiting_human"
    for text in (approved, dismissed, rerouted):
        _no_hangul(text)


def test_en_human_nudges_have_no_hangul(monkeypatch):
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    for type_ in HUMAN_KO_HEADS:
        env = m.Envelope(to="human-1", type=type_, task_id=SLUG,
                         payload={"summary": "please review"})
        text = human.compose_kakao(env, _human_agent())
        assert text.splitlines()[0].endswith(" — please take a look"), type_
        _no_hangul(text, type_)
    bare = m.Envelope(to="human-1", type=m.RESULT)
    assert human.compose_kakao(bare, _human_agent()) == "✉️ A message arrived — please take a look"


def test_digest_counts_a_decision_queued_in_the_other_language(monkeypatch):
    for queued, drained, pending in (("ko", "en", "1 decision(s) pending"),
                                     ("en", "ko", "결정 1건 대기")):
        monkeypatch.setenv("MACP_LANGUAGE", queued)
        notice = followups._digest_message(_waiting_rec())
        monkeypatch.setenv("MACP_LANGUAGE", drained)
        digest = kakao_format.compose_digest([{"msg": notice}, {"msg": "⚠️ beta: built"}])
        assert digest.splitlines()[0].endswith(pending), (queued, drained)


def test_alert_text_resolves_language_at_call_time(monkeypatch):
    rec = _waiting_rec()
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    assert followups._digest_message(rec).startswith(followups.HUMAN_DIGEST_HEAD)
    monkeypatch.setenv("MACP_LANGUAGE", "en")
    assert followups._digest_message(rec).startswith(i18n.MESSAGES["en"]["followup.digest_head"])
    assert followups.human_digest_head() == "🙋 Decision pending (followups human)"
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    assert followups.human_digest_head() == followups.HUMAN_DIGEST_HEAD
