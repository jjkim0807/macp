"""Engine log/CLI text in the configured UI language (``paths.ui_language()``).

``t(key, **fmt)`` is the one entry point. Scope is human-facing log and CLI
text only — strings a parser reads back (stage-artifact headings, the ``없음``
sentinel) stay out of this table.

There is no ko fallback: a key missing from the active language comes back as
the key itself, so a gap shows up instead of silently printing Korean.
``tests/test_i18n.py`` checks that both languages carry the same keys and
placeholders.

Stdlib-only and never raises, like ``paths``: bare-``python3`` daemons import it.
"""

from __future__ import annotations

from . import paths

MESSAGES: dict[str, dict[str, str]] = {
    "ko": {
        "hold.cap": "상한 도달 — repo당 동시 실행 최대 {cap}개",
        "hold.no_touches": "겹침 미판정 (touches 없음)",
        "hold.depends_on": "depends_on 직렬",
        "hold.overlap": "겹침 있음",
        "hold.serial": "직렬 대기",
        "hold.other_task": "다른 태스크",
        "hold.note": "대기 — 같은 체크아웃({label})에서 {other} 실행 중; 사유: {reason}",
        "resume.refused": "resume 예약 거부: {error}",
        "resume.reserved": "resume 예약: {path}",
        "resume.ancestry_sha": "{sha}가 HEAD의 조상",
        "resume.ancestry_none": "커밋 조건 없음",
        "resume.condition": "  조건: 라이브 체크아웃 HEAD가 main + {ancestry} + state가 awaiting_human/failed",
        "resume.expiry": "  만료: {hours}h 뒤 (만료되면 예약만 지워지고 경보 1회)",
        "resume.next_tick": "  다음 오케스트레이터 틱이 조건을 판정합니다 — assignment json은 건드리지 마세요.",
    },
    "en": {
        "hold.cap": "cap reached — at most {cap} concurrent runs per repo",
        "hold.no_touches": "overlap undetermined (no touches)",
        "hold.depends_on": "serial via depends_on",
        "hold.overlap": "touches overlap",
        "hold.serial": "waiting in series",
        "hold.other_task": "another task",
        "hold.note": "waiting — {other} is running in the same checkout ({label}); reason: {reason}",
        "resume.refused": "resume reservation refused: {error}",
        "resume.reserved": "resume reserved: {path}",
        "resume.ancestry_sha": "{sha} is an ancestor of HEAD",
        "resume.ancestry_none": "no commit condition",
        "resume.condition": "  condition: live checkout HEAD is main + {ancestry} + state is awaiting_human/failed",
        "resume.expiry": "  expires: in {hours}h (on expiry only the reservation is removed, with one alert)",
        "resume.next_tick": "  the next orchestrator tick checks the conditions — do not edit the assignment json.",
    },
}


def t(key: str, **fmt) -> str:
    """Message ``key`` in :func:`paths.ui_language`, formatted with ``fmt``.

    A key missing from the active language returns ``key`` itself (never the
    ko text); a template whose fields ``fmt`` does not fill comes back raw.
    """
    template = MESSAGES.get(paths.ui_language(), {}).get(key)
    if template is None:
        return key
    try:
        return template.format(**fmt) if fmt else template
    except (KeyError, IndexError, ValueError):
        return template
