"""Engine log/CLI, alert and dashboard text in the configured UI language (``paths.ui_language()``).

``t(key, **fmt)`` is the one entry point for messages — log/CLI lines and the
Slack/Kakao alert texts alike; the dashboard page's fragments live
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
        "alert.close_failed": (
            "🔒 {slug} 닫기 실패(mark_done=False) — md 없음 또는 CAS 재시도 소진; "
            "원장과 md가 어긋났을 수 있어 확인이 필요합니다"),
        "alert.timeout_park": (
            "⏱️ {n}회 연속 stage timeout으로 대기 전환 — "
            "더 잘게 쪼개거나 long_job으로 돌릴지 판단이 필요합니다"),
        "alert.timeout_checkpointed": "작업은 origin worker 브랜치에 checkpoint됨(유실 아님)",
        "alert.propagate": (
            "🔗 상류 '{proj}' 변경으로 하류 통합검증 {n}개를 만들었어요 — "
            "검증 결과를 확인해주세요"),
        "alert.parent_complete": (
            "🧭 하위 작업이 모두 끝난 상위 태스크 {n}건이 아직 open이에요 — "
            "닫을지 후속 작업을 추가할지 확인해주세요"),
        "alert.human_ready": (
            "🙋 사람이 할 차례가 됐어요 — 기다리던 선행 작업이 모두 끝났습니다"),
        "alert.human_ready_dep": "마지막 선행: {dep}",
        "alert.human_ready_next": "다음 행동: {next_action}",
        "alert.human_ready_ifthen": "If-Then: {if_then}",
        "alert.closed_unmerged": (
            "⚠️ delegate PR이 머지 없이 닫혔어요 — "
            "다시 열지·재작업할지·접을지 판단해주세요"),
        # orchestrator trace view (orchestrator/trace.py render())
        "trace.title": "# 🔎 trace — `{slug}`",
        "trace.summary": "> 런 {n}개 · runs_dir `{runs_dir}` · 읽기 전용 (상태를 바꾸지 않음)",
        "trace.latest.heading": "## 최신 런",
        "trace.latest.no_runs": "- 런 없음 (`*-{slug}` 디렉터리가 하나도 없음)",
        "trace.latest.position": "- 런 {n}개 중 마지막 ({n}/{n})",
        "trace.latest.none": "(없음)",
        "trace.latest.provenance_heading": (
            "### 산출물 출처 (판정 근거는 stages.json이 아니라 파일 자체의 스탬프)"),
        "trace.origin.own": "이번 런",
        "trace.origin.missing": "없음",
        "trace.origin.inherited": "- `{name}` ({stage}) — **상속본** ← `{detail}`",
        "trace.origin.foreign_author": (
            "- `{name}` ({stage}) — ⚠ **출처 불명**: 파일의 run_id는 `{detail}` "
            "(스탬프 도입 이전의 상속본이거나 잘못 쓰인 run_id)"),
        "trace.origin.presumed_own": (
            "- `{name}` ({stage}) — 출처 표시 없음: **이번 런 생성으로 추정** "
            "(상속이면 스탬프가 남으므로 — 단 스탬프 도입 이전 런이면 확증 불가)"),
        "trace.verify.heading": (
            "## verify 판정 (⚠ verify는 상속되지 않음 — 판정은 그 런의 디렉터리에만 남는다)"),
        "trace.verify.no_verdict": "판정 없음",
        "trace.verify.stale": " — ⚠ 이 판정은 이후 {after}번의 런 **이전** 상태",
        "trace.verify.latest": " — 최신 런의 판정",
        "trace.verify.line": "- 런 {i}/{n} `{run}`: **{verdict}**{stale}",
        "trace.verify.none": (
            "- verify.md 를 남긴 런이 하나도 없음 (런 {n}개 전부) "
            "— 이 태스크는 아직 검증된 적이 없다"),
        "trace.ledger.heading": "## 원장 (assignment ledger)",
        "trace.ledger.not_injected": "- 원장 조회 안 함 (ledger 미주입)",
        "trace.ledger.no_assignment": "- `{slug}` 에 대한 assignment 없음 (미할당이거나 이미 정리됨)",
        "trace.ledger.stored": " (원장 저장값: {state} — 실패 아님, 자동 재시도)",
        "trace.ledger.parked": (
            "- ⛔ 원장이 **파킹** 상태 — todo.db/`load_dispatchable`가 이 태스크를 "
            "후보로 올려도 `dispatch()`가 라우팅 전에 즉시 None을 반환한다"),
        "trace.ledger.merged_exit": (
            "  - 종결: PR은 이미 머지됨 — 남은 사람 단계를 마친 뒤 태스크 md를 "
            "`status: done`으로 닫으면 다음 틱에 `reconcile_merged()`가 "
            "\"todo already closed\" 경로로 자동 종결한다 — 재큐 불필요 "
            "(원장 수기 편집도 불필요)"),
        "trace.ledger.unpark": (
            "  - 해제: 블로커를 해소하고 태스크 md 수정을 커밋한 뒤 "
            "`python -m todo_system.cli resume <slug> --after-commit <sha>`로 재개를 "
            "예약하면 다음 틱에 `reconcile_resumes()`가 재큐한다 — 원장 수기 편집 불필요"),
        "trace.ledger.long_job": "- long_job 실행 중",
        "trace.ledger.checkout_busy": "- 체크아웃 대기 (다른 런이 같은 체크아웃 사용 중 — 끝나면 자동 재시도)",
        "trace.ledger.heavy_routed": (
            "- 무거운 태스크로 원격 `{agent}`에 보냄 — 응답이 없거나 오프라인이면 "
            "`{fallback}`로 되돌림"),
        "trace.ledger.rerouted_from": (
            "- 되돌림: `{agent}`에서 옮겨 옴 (사유 {reason}, {ts}) — 실패가 아니라 "
            "refail_count는 그대로"),
        "trace.ledger.run_dir": "- 원장이 가리키는 run_dir: `{run_dir}`",
        "trace.branch.heading": "## 브랜치 · PR",
        "trace.branch.origin_unknown": "- origin에 존재: unknown (git 조회 실패 — 실패가 아님)",
        "trace.branch.origin_yes": "- origin에 존재: 예",
        "trace.branch.origin_no": "- origin에 존재: 아니오 (미push, 또는 머지 후 삭제됨)",
        "trace.branch.pr_merged": "- PR: 머지됨",
        "trace.branch.pr_open": "- PR: 존재하지만 아직 머지 안 됨 (open/closed)",
        "trace.branch.pr_unknown": "- PR: unknown (gh 없음/미인증/PR 없음 — 실패가 아님)",
        # attention triage cards (orchestrator/triage.py): why a parked/failed task
        # stopped and what one click can do. Data values (notes, URLs) stay raw.
        "triage.action.merge": "머지",
        "triage.action.resume": "재개",
        "triage.action.cancel": "취소",
        "triage.action.instruct": "지시 추가",
        "triage.action.answer": "답하기",
        "triage.merged_blocked.reason": "PR은 이미 머지됐고 사람이 할 단계만 남았어요",
        "triage.merged_blocked.advice": (
            "남은 단계를 마친 뒤 태스크 md를 status: done으로 닫으면 다음 틱에 자동 종결돼요 "
            "— 재개하면 머지된 작업이 다시 돌아요"),
        "triage.orphan_redelivery.reason": (
            "러너가 재시작되거나 꺼지면서 실행 중이던 런이 끊겼어요 — 사람이 판단할 것은 없어요"),
        "triage.orphan_redelivery.advice": (
            "[재개]하면 다음 틱에 다시 디스패치되고 푸시된 브랜치를 이어받아요"),
        "triage.workflow_scope.reason": (
            "gh 토큰에 workflow 권한이 없어 .github/workflows를 바꾼 push가 거부됐어요"),
        "triage.workflow_scope.advice": (
            "본부(HQ)에서 그 브랜치를 push·머지하거나, 그 기기에서 gh auth refresh -s workflow를 "
            "승인한 뒤 [재개]하세요"),
        "triage.nonff_push.reason": (
            "원격 worker 브랜치에 이전 런 커밋이 남아 push가 거부됐어요(non-fast-forward)"),
        "triage.nonff_push.advice": (
            "옛 원격 브랜치는 그대로 두고 worker/{slug}-r2 같은 새 브랜치로 push해 PR을 여세요"
            "(force push 금지) — [지시 추가]로 그렇게 지시하고 재개할 수 있어요"),
        "triage.remote_no_pr.reason": (
            "원격 러너 {agent}에서 끝났지만 PR이 없어요 — 산출물은 그 기기에만 있어요"),
        "triage.remote_no_pr.advice": (
            "원격 러너는 gh workflow 권한이 없어 .github/workflows를 바꾼 push가 거부되곤 해요 "
            "— 본부(HQ)에서 브랜치를 push·머지하거나 [재개]로 다시 돌리세요"),
        "triage.human_misroute.reason": "워커가 할 태스크인데 사람 담당 패턴에 걸려 사람에게 배정됐어요",
        "triage.human_misroute.advice": (
            "[재개]하면 다시 라우팅돼 워커에게 가요 — 또 사람에게 가면 registry.yaml 워커 "
            "projects에 이 slug를 정확히 넣으세요"),
        "triage.reaped_off.reason": (
            "일시정지(worker_mode: {mode})된 태스크라 러너가 건너뛰었고 오래돼 정리됐어요 "
            "— 실행된 적 없음"),
        "triage.reaped_off.advice": (
            "할 일은 없어요 — 다시 돌리려면 md worker_mode를 auto로 바꾸고, 접으려면 [취소]하세요"),
        "triage.ui_merge_hold.reason": (
            "UI 미확인 머지 보류는 폐지된 규칙이에요 — 기기 확인은 머지 조건이 아니에요"),
        "triage.ui_merge_hold.advice": "검증이 통과했으니 [머지]하세요",
        "triage.ui_merge_hold.advice_nopr": "머지할 PR을 찾지 못했어요 — [재개]로 다시 돌리세요",
        "triage.device_unverified.reason": (
            "기기·에뮬레이터 확인만 남아 멈췄어요 — 기기 확인은 머지 조건이 아니에요"),
        "triage.device_unverified.advice": "[머지]하고 기기 확인은 「확인 못 함」으로 기록하면 돼요",
        "triage.device_unverified.advice_nopr": (
            "[지시 추가]로 '기기 확인은 확인 못 함으로 기록하고 진행'을 적고 재개하세요"),
        "triage.review_pr.reason": "PR이 열려 있고 머지만 남았어요",
        "triage.review_pr.advice": "검토하고 [머지]하세요",
        "triage.review_pr.advice_blocked": (
            "검증이 통과하지 않았거나 충돌이 있어 머지 버튼을 두지 않았어요 — [지시 추가]로 "
            "고칠 방향을 적고 재개하세요"),
        "triage.review_pr.advice_nopr": (
            "PR 주소를 찾지 못해 머지 버튼을 두지 않았어요 — 상세에서 PR을 확인하거나 [재개]로 "
            "다시 돌리세요"),
        "triage.rescope_pending.reason": "한 스테이지를 넘는 일이라 분할 계획이 준비됐어요",
        "triage.rescope_pending.advice": "다음 틱에 자동으로 나눠져요 — 기다리면 돼요",
        "triage.timeout_park.reason": (
            "스테이지가 {n}번 연속 시간 초과로 멈췄어요 — 작업은 원격 브랜치에 체크포인트됐어요"),
        "triage.timeout_park.advice": (
            "더 잘게 나누거나 long_job으로 돌리세요 — [지시 추가]로 방향을 적고 재개할 수 있어요"),
        "triage.blocked.reason": "선행 조건이 막혀 멈췄어요",
        "triage.blocked.advice": "막힌 것을 해결한 뒤 [재개]하세요 — 방향을 바꾸려면 [지시 추가]",
        "triage.result_no_pr.reason": "정상 끝났지만 머지할 PR이 없어요(0커밋)",
        "triage.result_no_pr.advice": (
            "결과를 보고 남은 일이 있으면 [지시 추가]로 다시 시키고, 끝난 일이면 [취소]로 접으세요"),
        "triage.fallback.reason": "멈춘 이유: {note}",
        "triage.fallback.reason_none": "멈춘 이유가 기록되지 않았어요",
        "triage.fallback.advice": "상세에서 최신 실행을 확인하세요 — 다시 돌리려면 [재개]",
        "triage.gate.advice": (
            "되돌릴 수 없는 단계가 남은 태스크(human_gate)라 버튼으로 진행하지 않아요 — 직접 처리하세요"),
        "triage.ev.note": "워커 메모: {note}",
        "triage.ev.blocked_on": "막힌 것: {blocked_on}",
        "triage.ev.pr": "PR: {pr}",
        "triage.ev.pr_merged": "PR 머지됨",
        "triage.ev.verify": "검증: {verdict} ({run})",
        "triage.ev.agent": "실행 기기: {agent}",
        "triage.ev.redelivered": "재전달 판정: {verdict}",
        "triage.ev.workflow": ".github/workflows를 바꾼 push에는 workflow 권한이 필요해요",
        "triage.ev.nonff": "원격 브랜치가 로컬 HEAD의 조상이 아님",
        "triage.ev.remote_artifacts": "산출물은 원격 기기의 Worker/runs에만 있어요",
        "triage.ev.agent_human": "배정: {agent}(사람)",
        "triage.ev.pattern": "걸린 패턴: {pattern}",
        "triage.ev.worker_mode": "md worker_mode: {mode}",
        "triage.ev.reaped": "러너가 응답 없이 건너뛰어 정리됨(reaped-stale)",
        "triage.ev.auto_merge_off": "auto_merge: false — 사람 머지를 기다리는 설정",
        "triage.ev.review_hold": "Report가 사람 검토를 권했어요(review)",
        "triage.ev.rebase_conflict": "동시 실행 PR과 리베이스 충돌",
        "triage.ev.timeouts": "연속 시간 초과 {n}회",
        "triage.ev.refail": "재실패 {n}회",
        "triage.ev.alert": "마지막 알림: {kind}",
        "triage.ev.not_dispatchable": "md worker_mode가 {mode}라 재개해도 디스패치되지 않아요",
        "triage.ev.resume_reserved": "재개 예약됨 — 다음 틱에 다시 디스패치돼요",
        "triage.ev.human_gate": "human_gate: 되돌릴 수 없는 단계가 남은 태스크",
        "triage.ev.md_missing": "태스크 md가 없어요",
        # what a card's button did (orchestrator/triage_actions.py, POST /api/action)
        "triage.result.merged": "머지했어요 — 다음 틱(5분 안)에 태스크가 닫혀요",
        "triage.result.already_merged": "이미 머지된 PR이에요 — 다음 틱에 태스크가 정리돼요",
        "triage.result.pr_closed": "PR이 닫혀 있어 머지하지 않았어요 — 다시 열거나 재개하세요",
        "triage.result.pr_not_task": "이 태스크의 PR이 아니라서 머지하지 않았어요",
        "triage.result.pr_lookup_failed": "PR 상태를 확인하지 못했어요 — gh 로그인이나 네트워크를 확인하세요",
        "triage.result.merge_conflict": (
            "충돌이 있어 머지하지 못했어요 — [지시 추가]로 리베이스를 지시하고 재개하세요"),
        "triage.result.merge_failed": "머지하지 못했어요 — 상세를 열어 PR 상태를 확인하세요",
        "triage.result.resumed": "재개를 예약했어요 — 다음 틱(5분 안)에 다시 디스패치돼요",
        "triage.result.resume_failed": "재개 예약을 만들지 못했어요 — 태스크 파일이나 원장 행이 없어요",
        "triage.result.cancelled": "취소했어요 — 태스크를 cancelled로 닫고 커밋했어요",
        "triage.result.instructed": "지시를 남기고 재개를 예약했어요 — 다음 틱에 반영돼요",
        "triage.result.instructed_no_resume": (
            "지시는 커밋했지만 재개 예약은 못 했어요 — [재개]를 다시 눌러 주세요"),
        "triage.result.vault_busy": "볼트가 다른 git 작업 중이에요 — 잠시 뒤 다시 눌러 주세요",
        "triage.result.vault_not_main": (
            "볼트 체크아웃이 main이 아니라 지금은 바꿀 수 없어요 — 잠시 뒤 다시 눌러 주세요"),
        "triage.result.md_missing": "태스크 파일이 없어 바꾸지 못했어요",
        "triage.result.md_untracked": "태스크 파일이 아직 커밋되지 않았어요 — 잠시 뒤 다시 눌러 주세요",
        "triage.result.md_changed": (
            "태스크 파일이 계속 바뀌는 중이라 쓰지 않았어요 — 잠시 뒤 다시 눌러 주세요"),
        "triage.result.commit_failed": "커밋하지 못해 변경을 되돌렸어요 — 볼트 상태를 확인하세요",
        "triage.result.not_offered": "지금 상태에서는 할 수 없는 처리예요 — 화면을 새로고침하세요",
        "triage.result.bad_request": "요청 형식이 잘못됐어요",
        "triage.result.bad_text": "지시는 한 줄, 500자 이하로 적어 주세요",
        "triage.result.unknown_task": "그런 태스크가 없어요",
        "triage.result.internal": "처리 중 오류가 났어요 — 대시보드 로그를 확인하세요",
        # what answering a decision card did (the answer only records)
        "triage.result.answered": (
            "결정을 기록했어요: {choice} — 태스크는 열려 있고 다음 행동에 반영했어요"),
        "triage.result.answered_no_reply": (
            "결정은 커밋했지만 오케스트레이터 회신은 보내지 못했어요 — 대시보드 로그를 확인하세요"),
        "triage.result.bad_option": "없는 선택지예요 — 화면을 새로고침하세요",
        "triage.result.bad_answer": "답은 한 줄, 500자 이하로 적어 주세요",
        # Progress Log lines a click writes into the task md
        "triage.md.cancel": "- {today}: 대시보드에서 취소(사용자)",
        "triage.md.instruction": "- {today}: 사용자 지시(대시보드): {text}",
        "triage.md.answer": "- {today}: 결정(대시보드): {text}",
        # the next_action an answered decision task is left with
        "triage.md.answer_next": "결정 반영 — {text}",
        # usage vs results (usage_value.py): the dashboard card and the brief table
        "usage_value.title": "사용량 대비 결과",
        "usage_value.basis": (
            "본부 run dir 기준 — 원격 러너 런과 정리된 run dir는 빠져 있어 출력 토큰은 하한이에요"),
        "usage_value.period.week": "지난 7일",
        "usage_value.period.month": "이번 달",
        "usage_value.kind.discovery": "발굴",
        "usage_value.kind.self": "macp 자기개선",
        "usage_value.kind.feature": "기능",
        "usage_value.kind.bug": "버그",
        "usage_value.kind.security": "보안",
        "usage_value.kind.research": "연구",
        "usage_value.kind.ops": "운영",
        "usage_value.kind.other": "기타",
        "usage_value.cell": (
            "런 {runs} · 머지 PR {merged} · 완료 {done} · 취소 {cancelled} · 실패 {failed}"
            " · 머지당 {per_merge}"),
        "usage_value.cell_unmetered": " · 미계측 런 {n}",
        "usage_value.total": "합계",
        "usage_value.col.row": "프로젝트 · 종류",
        "usage_value.col.out": "출력 토큰",
        "usage_value.col.runs": "런",
        "usage_value.col.merged": "머지 PR",
        "usage_value.col.status": "완료/취소/실패",
        "usage_value.col.per_merge": "머지당 토큰",
        "usage_value.more_rows": "외 {n}행은 대시보드에 있어요",
        "usage_value.no_runs": "런 없음",
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
        "digest.empty_entry": "(empty entry)",
        "digest.merged": "+{n} more merged",
        "human.head.assign": "🙋 New assignment arrived",
        "human.head.query": "❓ A question needs your answer",
        "human.head.cancel": "🚫 Assignment cancelled",
        "human.head.wake": "🔔 Reminder to check in",
        "human.head.ping": "📡 Ping",
        "human.head.other": "✉️ A message arrived",
        "human.headline": "{head} — please take a look",
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
        "alert.close_failed": (
            "🔒 Closing {slug} failed (mark_done=False) — md missing or CAS retries "
            "exhausted; the ledger and md may disagree, so check it"),
        "alert.timeout_park": (
            "⏱️ Parked after {n} consecutive stage timeouts — "
            "decide whether to split it further or run it as a long_job"),
        "alert.timeout_checkpointed": "work is checkpointed on the origin worker branch (not lost)",
        "alert.propagate": (
            "🔗 Upstream '{proj}' changed, so {n} downstream integration check(s) "
            "were created — check their results"),
        "alert.parent_complete": (
            "🧭 {n} parent task(s) have all subtasks finished but are still open — "
            "close them or add follow-up work"),
        "alert.human_ready": (
            "🙋 Your turn — every upstream task this one waited on is done"),
        "alert.human_ready_dep": "last upstream: {dep}",
        "alert.human_ready_next": "next: {next_action}",
        "alert.human_ready_ifthen": "If-Then: {if_then}",
        "alert.closed_unmerged": (
            "⚠️ delegate PR was closed without merging — "
            "decide whether to reopen, redo or drop it"),
        # orchestrator trace view (orchestrator/trace.py render())
        "trace.title": "# 🔎 trace — `{slug}`",
        "trace.summary": "> {n} run(s) · runs_dir `{runs_dir}` · read-only (changes no state)",
        "trace.latest.heading": "## Latest run",
        "trace.latest.no_runs": "- no runs (no `*-{slug}` directory at all)",
        "trace.latest.position": "- last of {n} run(s) ({n}/{n})",
        "trace.latest.none": "(none)",
        "trace.latest.provenance_heading": (
            "### Artifact provenance (judged from each file's own stamp, not stages.json)"),
        "trace.origin.own": "this run",
        "trace.origin.missing": "missing",
        "trace.origin.inherited": "- `{name}` ({stage}) — **inherited** ← `{detail}`",
        "trace.origin.foreign_author": (
            "- `{name}` ({stage}) — ⚠ **unknown origin**: the file's run_id is `{detail}` "
            "(a copy from before stamping existed, or a wrongly written run_id)"),
        "trace.origin.presumed_own": (
            "- `{name}` ({stage}) — no origin mark: **presumed written by this run** "
            "(an inheritance leaves a stamp — but for a run from before stamping "
            "this cannot be confirmed)"),
        "trace.verify.heading": (
            "## verify verdicts (⚠ verify is not inherited — a verdict stays only in "
            "its own run's directory)"),
        "trace.verify.no_verdict": "no verdict",
        "trace.verify.stale": " — ⚠ this verdict is from **before** the {after} later run(s)",
        "trace.verify.latest": " — verdict of the latest run",
        "trace.verify.line": "- run {i}/{n} `{run}`: **{verdict}**{stale}",
        "trace.verify.none": (
            "- no run left a verify.md (all {n} run(s)) "
            "— this task has never been verified"),
        "trace.ledger.heading": "## Assignment ledger",
        "trace.ledger.not_injected": "- ledger not consulted (no ledger injected)",
        "trace.ledger.no_assignment": (
            "- no assignment for `{slug}` (never assigned, or already cleaned up)"),
        "trace.ledger.stored": " (stored in the ledger as {state} — not a failure, retries automatically)",
        "trace.ledger.parked": (
            "- ⛔ the ledger row is **parked** — even when todo.db/`load_dispatchable` "
            "lists this task as a candidate, `dispatch()` returns None right away, "
            "before routing"),
        "trace.ledger.merged_exit": (
            "  - exit: the PR already merged — finish the remaining human step, then "
            "close the task md with `status: done`; on the next tick "
            "`reconcile_merged()` closes the row via the \"todo already closed\" path "
            "— no requeue needed (no hand edit of the ledger either)"),
        "trace.ledger.unpark": (
            "  - release: once the blocker is resolved, commit the task md change and "
            "reserve a resume with `python -m todo_system.cli resume <slug> --after-commit "
            "<sha>`; on the next tick `reconcile_resumes()` requeues it — no hand edit "
            "of the ledger needed"),
        "trace.ledger.long_job": "- long_job running",
        "trace.ledger.checkout_busy": (
            "- waiting for the checkout (another run is using the same checkout "
            "— retries automatically when it finishes)"),
        "trace.ledger.heavy_routed": (
            "- sent to heavy remote `{agent}` — comes back to `{fallback}` if it "
            "goes silent or offline"),
        "trace.ledger.rerouted_from": (
            "- rerouted: moved here from `{agent}` (reason {reason}, {ts}) — not a "
            "failure, refail_count unchanged"),
        "trace.ledger.run_dir": "- run_dir the ledger points at: `{run_dir}`",
        "trace.branch.heading": "## Branch · PR",
        "trace.branch.origin_unknown": "- exists on origin: unknown (git lookup failed — not a failure)",
        "trace.branch.origin_yes": "- exists on origin: yes",
        "trace.branch.origin_no": "- exists on origin: no (not pushed, or deleted after merge)",
        "trace.branch.pr_merged": "- PR: merged",
        "trace.branch.pr_open": "- PR: exists but not merged yet (open/closed)",
        "trace.branch.pr_unknown": "- PR: unknown (no gh / not authenticated / no PR — not a failure)",
        "triage.action.merge": "Merge",
        "triage.action.resume": "Resume",
        "triage.action.cancel": "Cancel",
        "triage.action.instruct": "Add instruction",
        "triage.action.answer": "Answer",
        "triage.merged_blocked.reason": "The PR is already merged; only a human step is left",
        "triage.merged_blocked.advice": (
            "Finish that step, then close the task md with status: done and the next tick "
            "ends it — resuming would rerun work that already merged"),
        "triage.orphan_redelivery.reason": (
            "The runner restarted or went away mid-run, so the run was cut off — nothing for "
            "a person to decide"),
        "triage.orphan_redelivery.advice": (
            "[Resume] dispatches it again on the next tick and it picks up the pushed branch"),
        "triage.workflow_scope.reason": (
            "The push was refused: the gh token has no workflow scope for changes under "
            ".github/workflows"),
        "triage.workflow_scope.advice": (
            "Push and merge that branch from HQ, or approve gh auth refresh -s workflow on "
            "that machine, then [Resume]"),
        "triage.nonff_push.reason": (
            "The push was refused because the remote worker branch still holds an earlier "
            "run's commits (non-fast-forward)"),
        "triage.nonff_push.advice": (
            "Leave the old remote branch alone and push to a new branch such as "
            "worker/{slug}-r2, then open the PR (no force push) — [Add instruction] can say "
            "so and resume"),
        "triage.remote_no_pr.reason": (
            "It finished on remote runner {agent} without a PR — its artifacts are on that "
            "machine only"),
        "triage.remote_no_pr.advice": (
            "Remote runners lack the gh workflow scope, so pushes that touch "
            ".github/workflows get refused — push and merge the branch from HQ, or [Resume] "
            "to run it again"),
        "triage.human_misroute.reason": (
            "A worker task matched a human-owned pattern and went to a person"),
        "triage.human_misroute.advice": (
            "[Resume] routes it again, to a worker — if it lands on a person again, add the "
            "exact slug to the worker projects in registry.yaml"),
        "triage.reaped_off.reason": (
            "Paused (worker_mode: {mode}), so the runner skipped it and it was cleaned up as "
            "stale — it never ran"),
        "triage.reaped_off.advice": (
            "Nothing to do — set the md worker_mode to auto to run it, or [Cancel] to fold it"),
        "triage.ui_merge_hold.reason": (
            "The unverified-UI merge hold is abolished — a device check is not a merge "
            "condition"),
        "triage.ui_merge_hold.advice": "Verification passed, so [Merge]",
        "triage.ui_merge_hold.advice_nopr": "No mergeable PR was found — [Resume] to run it again",
        "triage.device_unverified.reason": (
            "It stopped with only a device or emulator check left — a device check is not a "
            "merge condition"),
        "triage.device_unverified.advice": "[Merge] and record the device check as not verified",
        "triage.device_unverified.advice_nopr": (
            "[Add instruction] saying to record the device check as not verified and go on, "
            "then it resumes"),
        "triage.review_pr.reason": "The PR is open and only the merge is left",
        "triage.review_pr.advice": "Review it and [Merge]",
        "triage.review_pr.advice_blocked": (
            "No merge button: verification did not pass or there is a conflict — [Add "
            "instruction] with how to fix it, then it resumes"),
        "triage.review_pr.advice_nopr": (
            "No merge button: the PR address was not found — check the PR in the detail view "
            "or [Resume] to run it again"),
        "triage.rescope_pending.reason": "Too big for one stage, so a split plan is ready",
        "triage.rescope_pending.advice": "The next tick splits it automatically — just wait",
        "triage.timeout_park.reason": (
            "A stage timed out {n} times in a row — the work is checkpointed on the remote "
            "branch"),
        "triage.timeout_park.advice": (
            "Split it smaller or run it as a long_job — [Add instruction] can set the "
            "direction and resume"),
        "triage.blocked.reason": "Stopped on an unmet precondition",
        "triage.blocked.advice": (
            "Clear the blocker, then [Resume] — to change direction, [Add instruction]"),
        "triage.result_no_pr.reason": "Finished normally, but there is no PR to merge (0 commits)",
        "triage.result_no_pr.advice": (
            "Read the result: if work is left, [Add instruction] to run it again; if it is "
            "done, [Cancel] to fold it"),
        "triage.fallback.reason": "Why it stopped: {note}",
        "triage.fallback.reason_none": "No reason was recorded for the stop",
        "triage.fallback.advice": (
            "Check the latest run in the detail view — [Resume] to run it again"),
        "triage.gate.advice": (
            "An irreversible step is left (human_gate), so no button moves it on — do it by "
            "hand"),
        "triage.ev.note": "worker note: {note}",
        "triage.ev.blocked_on": "blocked on: {blocked_on}",
        "triage.ev.pr": "PR: {pr}",
        "triage.ev.pr_merged": "PR merged",
        "triage.ev.verify": "verify: {verdict} ({run})",
        "triage.ev.agent": "ran on: {agent}",
        "triage.ev.redelivered": "redelivery verdict: {verdict}",
        "triage.ev.workflow": "a push that changes .github/workflows needs the workflow scope",
        "triage.ev.nonff": "the remote branch is not an ancestor of the local HEAD",
        "triage.ev.remote_artifacts": "the artifacts are only in Worker/runs on the remote machine",
        "triage.ev.agent_human": "assigned to: {agent} (a person)",
        "triage.ev.pattern": "matched pattern: {pattern}",
        "triage.ev.worker_mode": "md worker_mode: {mode}",
        "triage.ev.reaped": "the runner skipped it without a reply; reaped as stale (reaped-stale)",
        "triage.ev.auto_merge_off": "auto_merge: false — set to wait for a human merge",
        "triage.ev.review_hold": "Report recommended a human review (review)",
        "triage.ev.rebase_conflict": "rebase conflict with a concurrent PR",
        "triage.ev.timeouts": "{n} stage timeouts in a row",
        "triage.ev.refail": "failed again {n} times",
        "triage.ev.alert": "last alert: {kind}",
        "triage.ev.not_dispatchable": "md worker_mode is {mode}, so a resume would not dispatch it",
        "triage.ev.resume_reserved": "resume reserved — it dispatches again on the next tick",
        "triage.ev.human_gate": "human_gate: an irreversible step is left",
        "triage.ev.md_missing": "the task md is missing",
        "triage.result.merged": "Merged — the task closes on the next tick (within 5 minutes)",
        "triage.result.already_merged": "That PR is already merged — the next tick tidies the task up",
        "triage.result.pr_closed": "The PR is closed, so nothing was merged — reopen it or resume",
        "triage.result.pr_not_task": "That PR is not this task's, so nothing was merged",
        "triage.result.pr_lookup_failed": "Could not check the PR — check the gh login or the network",
        "triage.result.merge_conflict": (
            "Could not merge because of a conflict — [Add instruction] to ask for a rebase, "
            "then it resumes"),
        "triage.result.merge_failed": "Could not merge — open the details and check the PR",
        "triage.result.resumed": (
            "Resume reserved — it dispatches again on the next tick (within 5 minutes)"),
        "triage.result.resume_failed": (
            "Could not reserve the resume — the task file or its ledger row is missing"),
        "triage.result.cancelled": "Cancelled — the task is closed as cancelled and committed",
        "triage.result.instructed": (
            "Instruction recorded and resume reserved — it applies on the next tick"),
        "triage.result.instructed_no_resume": (
            "The instruction is committed, but the resume could not be reserved — press "
            "[Resume]"),
        "triage.result.vault_busy": "The vault is busy with another git operation — try again in a moment",
        "triage.result.vault_not_main": (
            "The vault checkout is not on main, so nothing can change now — try again in a "
            "moment"),
        "triage.result.md_missing": "The task file is missing, so nothing changed",
        "triage.result.md_untracked": "The task file is not committed yet — try again in a moment",
        "triage.result.md_changed": (
            "The task file kept changing, so nothing was written — try again in a moment"),
        "triage.result.commit_failed": "Could not commit, so the change was rolled back — check the vault",
        "triage.result.not_offered": "That is not available in the current state — refresh the page",
        "triage.result.bad_request": "The request was malformed",
        "triage.result.bad_text": "Write the instruction as one line of at most 500 characters",
        "triage.result.unknown_task": "No such task",
        "triage.result.internal": "Something went wrong — check the dashboard log",
        "triage.result.answered": (
            "Decision recorded: {choice} — the task stays open and its next action now says so"),
        "triage.result.answered_no_reply": (
            "The decision is committed, but the reply to the orchestrator could not be sent — "
            "check the dashboard log"),
        "triage.result.bad_option": "That option does not exist — refresh the page",
        "triage.result.bad_answer": "Write the answer as one line of at most 500 characters",
        "triage.md.cancel": "- {today}: cancelled from the dashboard (user)",
        "triage.md.instruction": "- {today}: user instruction (dashboard): {text}",
        "triage.md.answer": "- {today}: decision (dashboard): {text}",
        "triage.md.answer_next": "Carry out the decision — {text}",
        "usage_value.title": "Usage vs results",
        "usage_value.basis": (
            "Counted from this host's run dirs — remote-runner runs and pruned run dirs are "
            "missing, so output tokens are a lower bound"),
        "usage_value.period.week": "Last 7 days",
        "usage_value.period.month": "This month",
        "usage_value.kind.discovery": "Discovery",
        "usage_value.kind.self": "macp self-improvement",
        "usage_value.kind.feature": "Feature",
        "usage_value.kind.bug": "Bug",
        "usage_value.kind.security": "Security",
        "usage_value.kind.research": "Research",
        "usage_value.kind.ops": "Ops",
        "usage_value.kind.other": "Other",
        "usage_value.cell": (
            "runs {runs} · merged PRs {merged} · done {done} · cancelled {cancelled} · "
            "failed {failed} · {per_merge} per merge"),
        "usage_value.cell_unmetered": " · {n} unmetered run(s)",
        "usage_value.total": "Total",
        "usage_value.col.row": "Project · kind",
        "usage_value.col.out": "Output tokens",
        "usage_value.col.runs": "Runs",
        "usage_value.col.merged": "Merged PRs",
        "usage_value.col.status": "Done/cancelled/failed",
        "usage_value.col.per_merge": "Tokens per merge",
        "usage_value.more_rows": "{n} more row(s) on the dashboard",
        "usage_value.no_runs": "no runs",
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
        "dash.js.agent_game": "게임 중",
        "dash.js.agent_game_since": "{since}부터",
        "dash.js.agent_btn_game": "게임 모드",
        "dash.js.agent_btn_work": "작업 재개",
        "dash.js.agent_mode_failed": "모드 변경 실패",
        # triage card (attention / parked rows): the card's own sentences come from the server
        "dash.js.tri_sending": "처리 중…",
        "dash.js.tri_net_fail": "요청을 보내지 못했어요 — 연결을 확인하세요",
        "dash.js.tri_send": "보내기",
        "dash.js.tri_placeholder": "지시 한 줄 (500자 이하)",
        "dash.js.tri_need_text": "지시를 한 줄 적어 주세요",
        # decision card (human rows): question and options come from the server
        "dash.js.dec_placeholder": "직접 답 한 줄 (500자 이하)",
        "dash.js.dec_need_text": "답을 한 줄 적어 주세요",
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
        "dash.html.graph_link": "그래프",
        "dash.graph.title": "그래프 메모리",
        "dash.graph.back": "← 대시보드",
        "dash.graph.search_ph": "제목·slug 검색",
        "dash.graph.as_of": "기준일",
        "dash.graph.now": "현재",
        "dash.graph.load_failed": "불러오지 못했어요",
        "dash.graph.unavailable": "Memory/relations.sqlite 가 없어요. `python -m todo_system.graph_memory scan` 을 먼저 실행하세요.",
        "dash.graph.empty": "이 시점에 유효한 관계가 없어요.",
        "dash.graph.nodes": "노드",
        "dash.graph.edges": "관계",
        "dash.graph.truncated": "개 생략(연결 적은 순)",
        "dash.graph.links": "개 연결",
        "dash.graph.none": "연결 없음",
        "dash.graph.kind_task": "할 일",
        "dash.graph.kind_decision": "결정 기록",
        "dash.graph.kind_entity": "엔티티",
        "dash.graph.kind_episode": "에피소드",
        "dash.graph.kind_note": "노트",
        "dash.graph.kind_ref": "참조(파일 없음)",
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
        "dash.js.agent_game": "in game",
        "dash.js.agent_game_since": "since {since}",
        "dash.js.agent_btn_game": "Game mode",
        "dash.js.agent_btn_work": "Resume work",
        "dash.js.agent_mode_failed": "Mode change failed",
        "dash.js.tri_sending": "Working…",
        "dash.js.tri_net_fail": "Could not send the request — check the connection",
        "dash.js.tri_send": "Send",
        "dash.js.tri_placeholder": "One-line instruction (max 500 chars)",
        "dash.js.tri_need_text": "Write a one-line instruction first",
        "dash.js.dec_placeholder": "One-line answer (max 500 chars)",
        "dash.js.dec_need_text": "Write a one-line answer first",
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
        "dash.html.graph_link": "Graph",
        "dash.graph.title": "Graph memory",
        "dash.graph.back": "← Dashboard",
        "dash.graph.search_ph": "Search title or slug",
        "dash.graph.as_of": "As of",
        "dash.graph.now": "Now",
        "dash.graph.load_failed": "Failed to load",
        "dash.graph.unavailable": "Memory/relations.sqlite not found. Run `python -m todo_system.graph_memory scan` first.",
        "dash.graph.empty": "No relations were live at this point in time.",
        "dash.graph.nodes": "nodes",
        "dash.graph.edges": "edges",
        "dash.graph.truncated": "hidden (least connected)",
        "dash.graph.links": "links",
        "dash.graph.none": "No links",
        "dash.graph.kind_task": "task",
        "dash.graph.kind_decision": "decision",
        "dash.graph.kind_entity": "entity",
        "dash.graph.kind_episode": "episode",
        "dash.graph.kind_note": "note",
        "dash.graph.kind_ref": "reference (no file)",
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
