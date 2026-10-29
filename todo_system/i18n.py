"""Engine log/CLI, alert and dashboard text in the configured UI language (``paths.ui_language()``).

``t(key, **fmt)`` is the one entry point for messages — log/CLI lines and the
Slack/Kakao alert and follow-up texts alike; the dashboard page's fragments live
in :data:`DASHBOARD` (see :func:`dashboard_phrases`). Scope is human-facing text
only — strings a parser reads back (stage-artifact headings, the ``없음``
sentinel, alert event keys, ledger ``last_note`` texts) stay out of these
tables. Callers resolve a message when they compose it, never at import, so a
long-lived daemon follows a language change.

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
        "dash.state.paused": "일시정지(PAUSED) — 새 작업 디스패치 중단",
        "dash.state.hold": "usage guard HOLD — 사용량 한도로 대기",
        "dash.state.budget": "일일 실행 예산 도달 ({runs_today}/{budget}) — 리셋까지 대기",
        "dash.state.running": "워커 실행 중 — claude 스테이지 {n}개",
        "dash.state.post_run_one": "{label} 후처리 중{tail}",
        "dash.state.post_run_many": "{n}건 후처리 중 ({labels})",
        "dash.state.contended_part": "{label} {n}건",
        "dash.state.contended": "직렬 대기 — {parts}가 한 체크아웃을 공유 (repo당 최대 {cap}개)",
        "dash.state.next_tick": "다음 틱에 디스패치 예정 ({n}건 대기)",
        "dash.state.no_work": "디스패치 가능한 태스크 없음",
        "dash.human.parked": "검토/승인 대기",
        "dash.sms.apply": "SMS 릴레이 앱에 설정 적용",
        "dash.sms.title": "SMS 릴레이 설정",
        # daily digest + route() merge suffix (kakao_format)
        "digest.headline_fyi": "📮 macp 알림 요약 {n}건 — 조치 불필요",
        "digest.headline_pending": "📮 macp 알림 요약 {n}건 — 결정 {k}건 대기",
        "digest.empty_entry": "(빈 항목)",
        "digest.merged": "외 {n}건 합침",
        # human inbox nudges (orchestrator/human.py)
        "human.head.assign": "🙋 새 할당이 도착했어요",
        "human.head.query": "❓ 확인이 필요한 질문이 있어요",
        "human.head.cancel": "🚫 할당이 취소됐어요",
        "human.head.wake": "🔔 확인 요청 알림이에요",
        "human.head.ping": "📡 핑",
        "human.head.other": "✉️ 메시지가 도착했어요",
        "human.headline": "{head} — 확인해주세요",
        # follow-up digest notice + `followups human|approve|dismiss|reroute`
        "followup.digest_head": "🙋 사람 결정 대기(followups human)",
        "followup.none": "사람 결정 대기 없음",
        "followup.waiting": "사람 결정 대기 {n}건",
        "followup.section_next_steps": "다음 단계",
        "followup.section_human_review": "사람이 검토해야 할 부분",
        "followup.body": "본문: {text}",
        "followup.approved_head": "승인됨 — planner 태스크화 대기 {n}건",
        "followup.approved_at": "(승인 {ts})",
        "followup.hint_approve": "승인:   {cli} approve <id>",
        "followup.hint_dismiss": "기각:   {cli} dismiss <id> --reason <사유>",
        "followup.hint_reroute": "재분류: {cli} reroute <id>   (rejected → 사람 결정 대기)",
        "followup.approved_msg": (
            "승인: {fid} → open+approved. 다음 planner 실행 때 '사람 승인됨 — "
            "태스크화 대상'으로 주입된다. planner는 run 완료·태스크 변경·24h "
            "하트비트 때만 돌므로, 조용한 날엔 최대 약 24시간 걸린다."),
        "followup.dismissed_msg": "기각: {fid} → done (dismissed: {reason})",
        "followup.rerouted_msg": "재분류: {fid} → {status}",
        # orchestrator alerts (Orchestrator._notify_* and reconcile_merged)
        "alert.reason": "사유: {reason}",
        "alert.account_switch": (
            "🔁 Claude 계정 전환 {frm} → {to} ({reason}) — "
            "다음 틱부터 새 계정으로 재디스패치됩니다"),
        "alert.account_paused": (
            "⏸ 모든 Claude 계정 사용 불가 ({frm}: {reason}) — 디스패치 PAUSED. "
            "윈도 리셋·재로그인·macp account use 중 하나로 자동 해제"),
        "alert.review_blocked": (
            "⛔ 파이프라인이 선행 조건 미충족으로 멈췄어요 — "
            "조건을 갖추거나 이 태스크를 접을지 판단해주세요"),
        "alert.review_pr": "✅ 파이프라인 완료 · PR 열림(미머지) — 리뷰 후 머지해주세요",
        "alert.review_result_no_pr": (
            "✅ 정상 완료 · 머지할 PR 없음(0-commit) — 결과 확인 후 태스크를 닫아주세요"),
        "alert.review_failure": "❌ PR 없음 · 머지할 것이 없어요 — 실패 원인 확인이 필요합니다",
        "alert.result": "결과: {hint}",
        "alert.no_pr_hint": "Worker/runs/ 아래 {task} 최신 run",
        "alert.no_pr_hint_art": "Worker/runs/ 아래 {task} 최신 run 의 {art}",
        "alert.orphaned": (
            "🧨 이전 시도의 미푸시 커밋 {count}개가 되감겼어요 — "
            "되살릴지 무시할지 판단해주세요"),
        "alert.poison": (
            "🚧 {n}회 재실패로 자동 재시도를 멈췄어요 — "
            "원인 확인 후 수동 처리가 필요합니다"),
        "alert.recent_reason": "최근 사유: {note}",
        "alert.timeout_park": (
            "⏱️ {n}회 연속 stage timeout으로 대기 전환 — "
            "더 잘게 쪼개거나 long_job으로 돌릴지 판단이 필요합니다"),
        "alert.timeout_checkpointed": "작업은 origin worker 브랜치에 checkpoint됨(유실 아님)",
        "alert.blocker_cancelled": (
            "🔗 대기하던 상류 태스크가 취소돼 진행 불가 — "
            "이 태스크가 아직 필요한지, 함께 취소할지 판단이 필요합니다"),
        "alert.upstream": "상류: {blocker}",
        "alert.propagate": (
            "🔗 상류 '{proj}' 변경으로 하류 통합검증 {n}개를 만들었어요 — "
            "검증 결과를 확인해주세요"),
        "alert.parent_complete": (
            "🧭 하위 작업이 모두 끝난 상위 태스크 {n}건이 아직 open이에요 — "
            "닫을지 후속 작업을 추가할지 확인해주세요"),
        "alert.closed_unmerged": (
            "⚠️ delegate PR이 머지 없이 닫혔어요 — "
            "다시 열지·재작업할지·접을지 판단해주세요"),
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
        "dash.state.paused": "paused (PAUSED) — no new dispatches",
        "dash.state.hold": "usage guard HOLD — waiting on the usage limit",
        "dash.state.budget": "daily run budget reached ({runs_today}/{budget}) — waiting for reset",
        "dash.state.running": "worker running — {n} claude stage(s)",
        "dash.state.post_run_one": "{label} post-processing{tail}",
        "dash.state.post_run_many": "{n} post-processing ({labels})",
        "dash.state.contended_part": "{label} ×{n}",
        "dash.state.contended": "waiting in series — {parts} share one checkout (max {cap} per repo)",
        "dash.state.next_tick": "dispatch on the next tick ({n} waiting)",
        "dash.state.no_work": "no dispatchable tasks",
        "dash.human.parked": "awaiting review/approval",
        "dash.sms.apply": "Apply settings in the SMS Relay app",
        "dash.sms.title": "SMS relay setup",
        "digest.headline_fyi": "📮 macp digest — {n} alert(s), no action needed",
        "digest.headline_pending": "📮 macp digest — {n} alert(s), {k} decision(s) pending",
        "digest.empty_entry": "(empty entry)",
        "digest.merged": "+{n} more merged",
        "human.head.assign": "🙋 New assignment arrived",
        "human.head.query": "❓ A question needs your answer",
        "human.head.cancel": "🚫 Assignment cancelled",
        "human.head.wake": "🔔 Reminder to check in",
        "human.head.ping": "📡 Ping",
        "human.head.other": "✉️ A message arrived",
        "human.headline": "{head} — please take a look",
        # No colon, ≤ 41 chars: compose_digest groups on the first ":" and clips a
        # singleton line to 60, which must still keep the "[<12-hex id>]" after it.
        "followup.digest_head": "🙋 Decision pending (followups human)",
        "followup.none": "no human decisions pending",
        "followup.waiting": "{n} human decision(s) pending",
        "followup.section_next_steps": "Next steps",
        "followup.section_human_review": "Needs human review",
        "followup.body": "text: {text}",
        "followup.approved_head": "approved — {n} waiting for the planner to file a task",
        "followup.approved_at": "(approved {ts})",
        "followup.hint_approve": "approve: {cli} approve <id>",
        "followup.hint_dismiss": "dismiss: {cli} dismiss <id> --reason <reason>",
        "followup.hint_reroute": "reroute: {cli} reroute <id>   (rejected → awaiting a human)",
        "followup.approved_msg": (
            "approved: {fid} → open+approved. The next planner run injects it in the "
            "human-approved block. The planner only runs on a run completion, a task "
            "change or the 24h heartbeat, so on a quiet day this can take up to about "
            "24 hours."),
        "followup.dismissed_msg": "dismissed: {fid} → done (dismissed: {reason})",
        "followup.rerouted_msg": "rerouted: {fid} → {status}",
        "alert.reason": "reason: {reason}",
        "alert.account_switch": (
            "🔁 Claude account switched {frm} → {to} ({reason}) — "
            "re-dispatching on the new account from the next tick"),
        "alert.account_paused": (
            "⏸ No Claude account usable ({frm}: {reason}) — dispatch PAUSED. "
            "Lifts on its own after a window reset, a re-login or macp account use"),
        "alert.review_blocked": (
            "⛔ Pipeline stopped on an unmet precondition — "
            "meet it or decide whether to drop this task"),
        "alert.review_pr": "✅ Pipeline done · PR open (not merged) — review and merge it",
        "alert.review_result_no_pr": (
            "✅ Finished cleanly · no PR to merge (0 commits) — check the result and close the task"),
        "alert.review_failure": "❌ No PR · nothing to merge — the failure cause needs a look",
        "alert.result": "result: {hint}",
        "alert.no_pr_hint": "latest run of {task} under Worker/runs/",
        "alert.no_pr_hint_art": "{art} of the latest run of {task} under Worker/runs/",
        "alert.orphaned": (
            "🧨 {count} unpushed commit(s) from an earlier attempt were rewound — "
            "decide whether to restore or ignore them"),
        "alert.poison": (
            "🚧 Auto-retry stopped after {n} repeated failures — "
            "find the cause and handle it by hand"),
        "alert.recent_reason": "last reason: {note}",
        "alert.timeout_park": (
            "⏱️ Parked after {n} consecutive stage timeouts — "
            "decide whether to split it further or run it as a long_job"),
        "alert.timeout_checkpointed": "work is checkpointed on the origin worker branch (not lost)",
        "alert.blocker_cancelled": (
            "🔗 An upstream task this one waited on was cancelled — "
            "decide whether this task is still needed or should be cancelled too"),
        "alert.upstream": "upstream: {blocker}",
        "alert.propagate": (
            "🔗 Upstream '{proj}' changed, so {n} downstream integration check(s) "
            "were created — check their results"),
        "alert.parent_complete": (
            "🧭 {n} parent task(s) have all subtasks finished but are still open — "
            "close them or add follow-up work"),
        "alert.closed_unmerged": (
            "⚠️ delegate PR was closed without merging — "
            "decide whether to reopen, redo or drop it"),
    },
}

# Dashboard page fragments (scripts/macp_dashboard.py): the page template carries
# an ``@@key@@`` marker wherever it used to carry Korean text, and
# ``render_html(lang)`` fills them from this table. Unlike ``MESSAGES`` these are
# raw fragments, not ``str.format`` templates; they sit inside markup, JS string
# literals and comments, so a value never adds a quote, bracket or brace its ko
# counterpart lacks (``tests/test_i18n.py`` checks this). An en value may be
# empty where English needs no word (the ``건`` counter suffix). No ko fallback:
# a key missing from a language renders as the key itself.
DASHBOARD: dict[str, dict[str, str]] = {
    "ko": {
        "dash.html.lang": "ko",
        "dash.js.locale": "ko-KR",
        # header + first screen
        "dash.html.subtitle": "실행 현황",
        "dash.html.theme_aria": "테마 전환 (자동/라이트/다크)",
        "dash.js.theme_auto": "◐ 자동",
        "dash.js.theme_light": "☀ 라이트",
        "dash.js.theme_dark": "🌙 다크",
        "dash.html.attention": "주의",
        "dash.html.decide": "결정",
        "dash.html.in_progress": "진행 중",
        "dash.html.projects": "프로젝트",
        "dash.html.more": "더보기",
        "dash.html.on_hold": "보류",
        "dash.html.to_decide": "결정할 것 · 🔥 급한 것",
        "dash.html.life": "현생 (여유 있음)",
        "dash.html.dev": "개발 프로젝트 (사람 몫)",
        "dash.html.blocked": "대기 중 (블록)",
        "dash.html.later": "시작일 전",
        "dash.html.broken": "깨진 것 · failed·사람대기",
        "dash.html.queue": "큐",
        "dash.html.queue_upcoming": "예약됨",
        "dash.html.queue_blocked": "선행 대기",
        "dash.html.system": "시스템 상세",
        "dash.html.history": "이력 (실행·태스크)",
        "dash.html.agents": "에이전트 (러너)",
        "dash.html.by_mode": "오픈 태스크 (worker_mode별)",
        "dash.html.deps": "크로스-프로젝트 의존성",
        "dash.html.footer": "자동 새로고침 3초",
        "dash.html.back": "뒤로 (Esc)",
        "dash.html.detail": "상세",
        "dash.html.reload": "새로고침",
        # script
        "dash.js.copy_id": "task id 복사:",
        "dash.js.project": "프로젝트:",
        "dash.js.now": "지금",
        "dash.js.hours": "시간",
        "dash.js.minutes": "분",
        "dash.js.why_parked": "검토대기",
        "dash.js.why_owned": "결정필요",
        "dash.js.why_todo": "직접 할 일",
        "dash.js.why_blocked": "블록",
        "dash.js.why_manual": "수동",
        "dash.js.why_backlog": "미설정",
        "dash.js.from": "부터",
        "dash.js.waiting": "대기",
        "dash.js.last_activity": "최근 활동",
        "dash.js.latest_of": "(run·ledger·md·session 중 최신)",
        "dash.js.ago": " 전",
        "dash.js.done": " 완료",
        "dash.js.qw_next_tick": "다음 틱",
        "dash.js.qw_checkout_busy": "리포 사용중",
        "dash.js.qw_gradle_cap": "빌드 상한",
        "dash.js.qw_budget": "예산",
        "dash.js.qw_retry": "재시도 대기",
        "dash.js.deps_done": "선행 완료 — 언블락 대기",
        "dash.js.count": "건",
        "dash.js.none": "없음",
        "dash.js.less": "접기",
        "dash.js.ws_budget": "예산 도달",
        "dash.js.ws_running": "워커 실행중",
        "dash.js.ws_running_hs": "실행중",
        "dash.js.ws_post_run": "후처리 중",
        "dash.js.ws_post_run_hs": "후처리",
        "dash.js.ws_serial": "직렬 대기",
        "dash.js.ws_serial_hs": "직렬대기",
        "dash.js.ws_idle": "유휴 (idle)",
        "dash.js.account": "계정:",
        "dash.js.slots": "슬롯",
        "dash.js.hold_release": "HOLD 해제",
        "dash.js.later": " 뒤",
        "dash.js.hold_next_guard": "다음 guard 틱에 해제 예정",
        "dash.js.tasks_running": "task 실행 중",
        "dash.js.hold_overdue": "예정 시각 지남 — 다음 guard 틱에 해제",
        "dash.js.hold_unknown": "시각 미상",
        "dash.js.usage_data": "usage 데이터",
        "dash.js.min_ago_stale": "분 전 (stale)",
        "dash.js.last_update": "마지막 갱신",
        "dash.js.nothing_urgent": "급하게 결정할 일 없음 ✓",
        "dash.js.no_projects": "진행 중인 프로젝트 없음",
        "dash.js.nothing_attention": "주의가 필요한 항목 없음 ✓",
        "dash.js.no_active": "지금 실행 중인 task 없음",
        "dash.js.queue_empty": "큐 비어 있음",
        "dash.js.no_deps": "선언된 크로스-프로젝트 의존성 없음",
        "dash.js.chars": "자",
        "dash.js.task": "태스크",
        "dash.js.outcome": "완료 조건",
        "dash.js.next_action": "다음 행동",
        "dash.js.worker_note": "워커 메모",
        "dash.js.task_detail_link": "태스크 상세 →",
        "dash.js.artifacts": "산출물",
        "dash.js.truncated": "서버 상한에서 잘림",
        "dash.js.no_artifacts": "산출물 없음",
        "dash.js.tail_only": "끝부분만",
        "dash.js.no_runlog": "run.log 없음",
        "dash.js.runs": "실행",
        "dash.js.no_runs": "실행 기록 없음",
        "dash.js.task_body": "태스크 본문",
        "dash.js.run_detail": "실행 상세",
        "dash.js.task_detail": "태스크 상세",
        "dash.js.loading": "불러오는 중…",
        "dash.js.run_not_found": "실행 기록을 찾을 수 없음",
        "dash.js.task_not_found": "태스크를 찾을 수 없음",
        "dash.js.load_failed": "불러오기 실패 — ↻로 다시 시도",
        "dash.js.copied": "복사됨",
        "dash.js.disconnected": "연결 끊김",
        # comments served inside the page
        "dash.cmt.status": "현재 상태 (idle/실행중/PAUSED/usage HOLD)",
        "dash.cmt.action_counts": "주의·결정·진행 중",
        "dash.cmt.projects": "진행 중인 프로젝트 (최상위 부모 태스크)",
        "dash.cmt.human": "내가 결정할 것 (사람 몫)",
        "dash.cmt.broken": "깨진 것 (failed · 사람 대기)",
        "dash.cmt.active": "지금 실행 중",
        "dash.cmt.queue": "큐: 디스패치 대기 순서·사유 (서버 판정, 읽기 전용). 기본 접힘, 그룹 폴딩 없음.",
        "dash.cmt.system_1": "이하 이력·참고·계측: 기본 접힘 '시스템 상세' 한 카드로 합침. 안의 기존",
        "dash.cmt.system_2": "는 중첩으로 보존(id·cnt-* 앵커 불변). runs 는 '이력'에",
        "dash.cmt.system_3": "흡수되므로 cnt-runs/#runs 는 숨긴 자리표시로만 남긴다.",
        "dash.cmt.runs_placeholder": "id 보존용 자리표시(runs 는 이력에 흡수됨)",
        "dash.cmt.hide_broken": "깨진 것 0건이면 카드 자체 숨김",
    },
    "en": {
        "dash.html.lang": "en",
        "dash.js.locale": "en-US",
        "dash.html.subtitle": "live status",
        "dash.html.theme_aria": "Toggle theme (auto/light/dark)",
        "dash.js.theme_auto": "◐ auto",
        "dash.js.theme_light": "☀ light",
        "dash.js.theme_dark": "🌙 dark",
        "dash.html.attention": "Attention",
        "dash.html.decide": "Decide",
        "dash.html.in_progress": "In progress",
        "dash.html.projects": "Projects",
        "dash.html.more": "More",
        "dash.html.on_hold": "On hold",
        "dash.html.to_decide": "To decide · 🔥 urgent",
        "dash.html.life": "Life (no rush)",
        "dash.html.dev": "Dev projects (human-owned)",
        "dash.html.blocked": "Waiting (blocked)",
        "dash.html.later": "Not started yet",
        "dash.html.broken": "Broken · failed·awaiting human",
        "dash.html.queue": "Queue",
        "dash.html.queue_upcoming": "Scheduled",
        "dash.html.queue_blocked": "Waiting on prerequisites",
        "dash.html.system": "System details",
        "dash.html.history": "History (runs·tasks)",
        "dash.html.agents": "Agents (runners)",
        "dash.html.by_mode": "Open tasks (by worker_mode)",
        "dash.html.deps": "Cross-project dependencies",
        "dash.html.footer": "Auto-refresh 3s",
        "dash.html.back": "Back (Esc)",
        "dash.html.detail": "Details",
        "dash.html.reload": "Reload",
        "dash.js.copy_id": "copy task id:",
        "dash.js.project": "project:",
        "dash.js.now": "now",
        "dash.js.hours": "h",
        "dash.js.minutes": "m",
        "dash.js.why_parked": "review",
        "dash.js.why_owned": "decide",
        "dash.js.why_todo": "to do",
        "dash.js.why_blocked": "blocked",
        "dash.js.why_manual": "manual",
        "dash.js.why_backlog": "unset",
        "dash.js.from": "~",
        "dash.js.waiting": "waiting",
        "dash.js.last_activity": "last activity",
        "dash.js.latest_of": "(latest of run·ledger·md·session)",
        "dash.js.ago": " ago",
        "dash.js.done": " done",
        "dash.js.qw_next_tick": "next tick",
        "dash.js.qw_checkout_busy": "repo busy",
        "dash.js.qw_gradle_cap": "build cap",
        "dash.js.qw_budget": "budget",
        "dash.js.qw_retry": "retry wait",
        "dash.js.deps_done": "prerequisites done — awaiting unblock",
        "dash.js.count": "",
        "dash.js.none": "none",
        "dash.js.less": "Less",
        "dash.js.ws_budget": "budget reached",
        "dash.js.ws_running": "worker running",
        "dash.js.ws_running_hs": "running",
        "dash.js.ws_post_run": "post-processing",
        "dash.js.ws_post_run_hs": "post-run",
        "dash.js.ws_serial": "waiting in series",
        "dash.js.ws_serial_hs": "serial",
        "dash.js.ws_idle": "idle",
        "dash.js.account": "account:",
        "dash.js.slots": "slots",
        "dash.js.hold_release": "HOLD release:",
        "dash.js.later": "",
        "dash.js.hold_next_guard": "releases on the next guard tick",
        "dash.js.tasks_running": "task(s) running",
        "dash.js.hold_overdue": "past due — releases on the next guard tick",
        "dash.js.hold_unknown": "time unknown",
        "dash.js.usage_data": "usage data",
        "dash.js.min_ago_stale": "m old (stale)",
        "dash.js.last_update": "last update",
        "dash.js.nothing_urgent": "nothing urgent to decide ✓",
        "dash.js.no_projects": "no projects in progress",
        "dash.js.nothing_attention": "nothing needs attention ✓",
        "dash.js.no_active": "no task running right now",
        "dash.js.queue_empty": "queue empty",
        "dash.js.no_deps": "no cross-project dependencies declared",
        "dash.js.chars": " chars",
        "dash.js.task": "Task",
        "dash.js.outcome": "Outcome",
        "dash.js.next_action": "Next action",
        "dash.js.worker_note": "Worker note",
        "dash.js.task_detail_link": "Task details →",
        "dash.js.artifacts": "Artifacts",
        "dash.js.truncated": "truncated at the server cap",
        "dash.js.no_artifacts": "no artifacts",
        "dash.js.tail_only": "tail only",
        "dash.js.no_runlog": "no run.log",
        "dash.js.runs": "Runs",
        "dash.js.no_runs": "no runs",
        "dash.js.task_body": "Task body",
        "dash.js.run_detail": "Run details",
        "dash.js.task_detail": "Task details",
        "dash.js.loading": "Loading…",
        "dash.js.run_not_found": "run not found",
        "dash.js.task_not_found": "task not found",
        "dash.js.load_failed": "Load failed — retry with ↻",
        "dash.js.copied": "copied",
        "dash.js.disconnected": "disconnected",
        "dash.cmt.status": "current state (idle/running/PAUSED/usage HOLD)",
        "dash.cmt.action_counts": "attention·decide·in progress",
        "dash.cmt.projects": "projects in progress (top-level parent tasks)",
        "dash.cmt.human": "mine to decide (human-owned)",
        "dash.cmt.broken": "broken (failed · awaiting human)",
        "dash.cmt.active": "running now",
        "dash.cmt.queue": "queue: dispatch wait order·reason (server-judged, read-only). Collapsed by default, no group folding.",
        "dash.cmt.system_1": "history·reference·metrics below: merged into one collapsed 'System details' card. The existing",
        "dash.cmt.system_2": "stay nested inside it (id·cnt-* anchors unchanged). runs are absorbed into 'History',",
        "dash.cmt.system_3": "so cnt-runs/#runs remain only as a hidden placeholder.",
        "dash.cmt.runs_placeholder": "placeholder kept for the ids (runs are absorbed into History)",
        "dash.cmt.hide_broken": "hide the whole card when nothing is broken",
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


def dashboard_phrases(lang: str | None = None) -> dict[str, str]:
    """The :data:`DASHBOARD` fragment table for ``lang`` (default
    :func:`paths.ui_language`); an unknown language gets the en table."""
    if lang is None:
        lang = paths.ui_language()
    return DASHBOARD.get(lang) or DASHBOARD["en"]
