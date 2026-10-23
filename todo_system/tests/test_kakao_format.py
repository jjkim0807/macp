"""Unit tests for the self-limiting kakao memo formatter (stdlib leaf module).

These are pure-function tests — no subprocess, no network, no real Kakao send —
so they can never reach the real alert channel ([[tests-reach-real-alert-sender]]).
The worst-input fixtures use the 2026-09-16 19:42 real example: a task whose
`blocked_on` is a few hundred chars of English, which is exactly what used to be
blind-cut mid-sentence at 179.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from todo_system import kakao_format, paths
from todo_system.kakao_format import (
    KAKAO_TEXT_LIMIT,
    clip_words,
    compose_alert,
)
from todo_system import i18n
from todo_system.kakao_format import compose_digest
from todo_system.orchestrator import followups

# The 2026-09-16 19:42 [검토요청] macp-dashboard-public-access blocker, verbatim.
BLOCKER_1942 = (
    "AC4/AC5 need a human on acme-hq: sudo-enable Tailscale Funnel (operator/sudo "
    "required — a forbidden hard rule, and tailscale is not in the verify whitelist), "
    "then from the phone with Tailscale OFF confirm the dashboard 401s without a token "
    "and 200s with one."
)
LONG_PR_URL = "https://github.com/example-org/MaestroACP/pull/123456"


# ---------------------------------------------------------------- clip_words


def test_clip_words_returns_short_text_unchanged():
    assert clip_words("짧은 메모", KAKAO_TEXT_LIMIT) == "짧은 메모"


def test_clip_words_collapses_newlines_and_runs_to_single_spaces():
    assert clip_words("a\n\nb   c", KAKAO_TEXT_LIMIT) == "a b c"


def test_clip_words_marks_the_cut_and_never_exceeds_budget():
    out = clip_words(BLOCKER_1942, 60)
    assert len(out) <= 60
    assert out.endswith("…")


def test_clip_words_cuts_on_a_word_boundary_not_mid_token():
    # Budget lands inside "whitelist"; the whole partial word must be dropped, so
    # the result is a prefix made of WHOLE words from the source plus the ellipsis.
    out = clip_words(BLOCKER_1942, 80)
    body = out[:-1]  # drop the trailing ellipsis
    source_words = BLOCKER_1942.split()
    kept = body.split()
    assert kept == source_words[: len(kept)]  # every kept word is intact & in order


def test_clip_words_hard_cuts_a_single_overlong_word_but_still_marks_it():
    out = clip_words("a" * 300, 40)
    assert len(out) == 40
    assert out == "a" * 39 + "…"


def test_clip_words_non_positive_budget_is_empty():
    assert clip_words("anything", 0) == ""
    assert clip_words("anything", -5) == ""


# ---------------------------------------------------------------- compose_alert


def test_compose_alert_headline_only_survives_whole():
    head = "🔴 검토 필요 — 리뷰 후 머지해주세요"
    assert compose_alert(head) == head


def test_compose_alert_drops_a_whole_line_that_would_overflow_rather_than_cut():
    head = "🔴 검토 필요 — 리뷰 후 머지해주세요"
    fits = "📋 macp-dashboard-public-access"
    overflow = "x" * KAKAO_TEXT_LIMIT  # cannot fit next to the headline
    out = compose_alert(head, [fits, overflow])
    assert fits in out
    assert overflow not in out          # dropped entirely — never bisected
    assert len(out) <= KAKAO_TEXT_LIMIT


def test_compose_alert_worst_input_stays_within_budget_and_keeps_the_action():
    head = "⛔ 파이프라인이 선행 조건 미충족으로 멈췄어요 — 조건을 갖추거나 태스크를 접을지 판단해주세요"
    out = compose_alert(head, ["📋 macp-dashboard-public-access"], tail=BLOCKER_1942)
    assert len(out) <= KAKAO_TEXT_LIMIT
    assert out.startswith(head)         # the actionable headline is intact
    assert out.splitlines()[0] == head


def test_compose_alert_tail_is_clipped_to_the_remaining_budget_not_dropped():
    head = "❗짧은 머리글"
    # BLOCKER_1942 (~300 chars) now fits whole inside the 4000-char budget, so to
    # still exercise the clip-not-drop behaviour the tail must exceed the budget.
    long_tail = " ".join([BLOCKER_1942] * 30)  # ~9k chars, well over KAKAO_TEXT_LIMIT
    out = compose_alert(head, tail=long_tail)
    assert len(out) <= KAKAO_TEXT_LIMIT
    # There was room, so some of the tail rode along (clipped, marked).
    assert out != head
    assert out.endswith("…")


def test_compose_alert_a_url_placed_whole_is_never_split():
    head = "✅ PR 열림(미머지) — 리뷰 후 머지해주세요"
    out = compose_alert(head, [f"🔗 {LONG_PR_URL}"])
    assert LONG_PR_URL in out           # present in one piece
    assert len(out) <= KAKAO_TEXT_LIMIT


# ---------------------------------------------------------------- _vault_root
# _vault_root() anchors route()'s state files. KAKAO_VAULT_DIR is the top-priority
# test seam kakao_send.sh also honours; the production fallback now flows through
# the shared paths.vault_root resolver (public-split follow-up) instead of a
# hard-coded Path(__file__).resolve().parents[1].
#
# conftest._kakao_isolated is AUTOUSE and sets KAKAO_VAULT_DIR for every test, so
# the fallback / MACP-redirect cases MUST delenv it first or the early return makes
# them pass vacuously (the `else` branch never runs).


# byte-identity to parents[1] holds only in the combined vault layout (a
# .macp/config.toml marker at the repo root); an engine-only export tree has no
# marker and the shared resolver raises by design, so this case skips there
# (public-split-10 AC2 vault-only / AC4). The live vault has the marker and runs it.
_vault_only_byte_identity = pytest.mark.skipif(
    not (Path(paths.__file__).resolve().parents[1] / ".macp" / "config.toml").is_file(),
    reason="vault-only: byte-identity holds only in the combined vault layout "
           "(.macp/config.toml at the repo root); the engine-only export tree has no marker.",
)


@_vault_only_byte_identity
def test_vault_root_falls_back_to_shared_resolver_when_all_env_unset(monkeypatch):
    monkeypatch.delenv("KAKAO_VAULT_DIR", raising=False)
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    install = Path(kakao_format.__file__).resolve().parents[1]
    # Byte-identical to the old parents[1], and proven to go through the shared
    # resolver rather than any private computation.
    assert kakao_format._vault_root() == install
    assert kakao_format._vault_root() == paths.vault_root(start=install)
    assert (install / "todo_system" / "kakao_format.py").is_file()  # a real root


def test_vault_root_fallback_honours_macp_vault_root(monkeypatch, tmp_path):
    monkeypatch.delenv("KAKAO_VAULT_DIR", raising=False)
    monkeypatch.setenv("MACP_VAULT_ROOT", str(tmp_path))
    # KAKAO_VAULT_DIR unset → the fallback consults the resolver, which honours
    # MACP_VAULT_ROOT (AC a).
    assert kakao_format._vault_root() == tmp_path


def test_kakao_vault_dir_wins_over_macp_vault_root(monkeypatch, tmp_path):
    kakao_dir = tmp_path / "kakao"
    macp_dir = tmp_path / "macp"
    monkeypatch.setenv("KAKAO_VAULT_DIR", str(kakao_dir))
    monkeypatch.setenv("MACP_VAULT_ROOT", str(macp_dir))
    # AC c: KAKAO_VAULT_DIR stays TOP priority — it wins outright and the shared
    # resolver (which would otherwise return macp_dir) is never consulted.
    assert kakao_format._vault_root() == kakao_dir


def test_kakao_vault_dir_honoured_even_when_macp_vault_root_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("MACP_VAULT_ROOT", raising=False)
    kakao_dir = tmp_path / "kakao-only"
    monkeypatch.setenv("KAKAO_VAULT_DIR", str(kakao_dir))
    # The top-priority early return does not depend on MACP_VAULT_ROOT being set.
    assert kakao_format._vault_root() == kakao_dir


# ---------------------------------------------------------------- ko golden
# The digest headline, empty-entry marker and route()'s merge suffix, pinned byte
# for byte as they read before they went through i18n.t().

_KO_HUMAN_HEAD = "🙋 사람 결정 대기(followups human)"


def _route_paths(tmp_path: Path) -> dict:
    return {"dedup_path": tmp_path / "dedup.json", "digest_path": tmp_path / "digest.jsonl",
            "log_path": tmp_path / "route.log"}


def test_ko_digest_headline_without_a_pending_decision(monkeypatch):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    assert kakao_format.compose_digest([{"msg": "⚠️ beta: built 12\nmore"}]) == (
        "📮 macp 알림 요약 1건 — 조치 불필요\n· ⚠️ beta: built 12")


def test_ko_digest_headline_with_a_pending_decision_and_an_empty_entry(monkeypatch):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    out = kakao_format.compose_digest(
        [{"msg": f"{_KO_HUMAN_HEAD}: [abcdef012345] demo"}, {"msg": ""}])
    assert out == (
        "📮 macp 알림 요약 2건 — 결정 1건 대기\n"
        "· (빈 항목)\n"
        f"· {_KO_HUMAN_HEAD}: [abcdef012345] demo")


def test_ko_route_merge_suffix(monkeypatch, tmp_path):
    monkeypatch.setenv("MACP_LANGUAGE", "ko")
    kw = dict(event_key="k", **_route_paths(tmp_path))
    assert kakao_format.route("alert", now=1000.0, **kw) == ("send", "alert")
    assert kakao_format.route("alert", now=1010.0, **kw) == ("suppressed", None)
    assert kakao_format.route("alert", now=1000.0 + kakao_format.DEDUP_WINDOW_S + 1, **kw) == (
        "send", "alert\n외 1건 합침")


# ---------------------------------------------------------------- clip parity ko/en
# The length budget is language-blind: the same ASCII inputs clip the same way
# whichever language the engine's own text is in.

_LANGS = pytest.mark.parametrize("lang", ["ko", "en"])
_LONG_SLUG = " ".join(f"part{i:02d}-of-a-very-long-slug" for i in range(12))   # ~300 chars
_LONG_REASON = " ".join([BLOCKER_1942] * 15)                                  # > 4000 chars
_FID = "abcdef012345"


def _parked(**kw) -> dict:
    rec = {"id": _FID, "source_task": _LONG_SLUG, "human_reason": _LONG_REASON,
           "status": "awaiting_human"}
    rec.update(kw)
    return rec


@_LANGS
def test_followup_digest_notice_clips_the_same_in_both_languages(monkeypatch, lang):
    monkeypatch.setenv("MACP_LANGUAGE", lang)
    msg = followups._digest_message(_parked())
    first = msg.splitlines()[0]
    assert len(msg) <= KAKAO_TEXT_LIMIT
    assert first.startswith(f"{followups.human_digest_head()}: [{_FID}] ")
    assert len(first.split(f"[{_FID}] ", 1)[1]) <= 60          # slug capped at 60
    assert msg.endswith("…")                                    # tail clipped, marked
    kept = msg.splitlines()[-1][:-1].split()
    assert kept[1:] == _LONG_REASON.split()[: len(kept) - 1]   # whole words only


@_LANGS
def test_digest_singleton_keeps_the_followup_id_in_both_languages(monkeypatch, lang):
    monkeypatch.setenv("MACP_LANGUAGE", lang)
    digest = compose_digest([{"msg": followups._digest_message(_parked())}])
    line = digest.splitlines()[1]
    assert len(line) <= 60
    assert f"[{_FID}]" in line


@_LANGS
def test_digest_overflow_drops_whole_lines_in_both_languages(monkeypatch, lang):
    monkeypatch.setenv("MACP_LANGUAGE", lang)
    entries = [{"msg": f"alert-{i:03d} " + "word " * 30} for i in range(400)]
    digest = compose_digest(entries)
    lines = digest.splitlines()
    assert len(digest) <= KAKAO_TEXT_LIMIT
    assert 1 < len(lines) < 401                                 # some dropped, never cut
    assert all(len(line) <= 60 for line in lines[1:])
    assert lines[0] == i18n.t("digest.headline_fyi", n=400)


@_LANGS
def test_route_merge_suffix_is_dropped_at_the_limit_in_both_languages(monkeypatch, tmp_path, lang):
    monkeypatch.setenv("MACP_LANGUAGE", lang)
    kw = dict(event_key="k", dedup_path=tmp_path / "d.json",
              digest_path=tmp_path / "q.jsonl", log_path=tmp_path / "r.log")
    later = 1000.0 + kakao_format.DEDUP_WINDOW_S + 1
    full = "x" * (KAKAO_TEXT_LIMIT - 3)
    kakao_format.route(full, now=1000.0, **kw)
    kakao_format.route(full, now=1010.0, **kw)
    assert kakao_format.route(full, now=later, **kw) == ("send", full)   # suffix would overflow
    kakao_format.route("short", now=later + 1, **kw)
    _, final = kakao_format.route("short", now=later + 2 * kakao_format.DEDUP_WINDOW_S, **kw)
    assert final == "short\n" + i18n.t("digest.merged", n=1)


@_LANGS
def test_alert_headline_survives_an_overlong_tail_in_both_languages(monkeypatch, lang):
    monkeypatch.setenv("MACP_LANGUAGE", lang)
    head = i18n.t("alert.review_blocked")
    out = compose_alert(head, ["📋 demo-task"],
                        tail=i18n.t("alert.reason", reason=" ".join([BLOCKER_1942] * 30)))
    assert len(out) <= KAKAO_TEXT_LIMIT
    assert out.splitlines()[0] == head
    assert out.endswith("…")
