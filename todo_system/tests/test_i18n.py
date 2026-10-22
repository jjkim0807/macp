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
