---
title: 실행 중 승인 카드 — 설계 노트 (사람 판단이 필요할 때 스테이지를 멈춘 채 대시보드 카드로 묻고, 답이 오면 같은 런을 이어간다)
status: draft
created: 2026-11-17
related:
  - Todo/items/macp-inflight-approval-cards-design.md                   # 이 태스크
  - Todo/items/macp-run-event-stream-design.md                         # 선행 — approval.* kind 이름·필드를 예약했다
  - Todo/items/macp-chat-channel-entry-design-b-reply-safety-split.md  # 자매 — chat-channels §4·§5 가 이 노트를 인터페이스로 쓴다
  - Todo/items/macp-event-triggers-design.md                           # 자매 — 새 상시 인프라 없이 본부 대시보드를 확장한다
  - docs/design/run-event-stream.md      # 봉투·단일 writer·events.in/ 드롭박스·approval.* 예약·마스킹(§2.1·§2.2·§4)
  - docs/design/chat-channels.md         # R2 바닥·CANCEL 한계·§4 자리표시
  - docs/design/event-triggers.md        # 대시보드 프로세스 확장 원칙
  - docs/design/state-store.md           # leaf ⑦ — 본부 HTTP 상태 API(원격 러너 경로)
  - docs/design/agent-backends.md        # AgentBackend capability(`approval_broker` 를 서술할 자리)
  - Decisions/2026-09-11-no-github-actions-local-release-lane.md  # 완료 조건은 로컬 명령("CI green" 금지)
---

# 실행 중 승인 카드 — 설계 노트

> **이 노트의 성격**: 분석·설계 전용. 코드를 한 줄도 바꾸지 않고 `docs/design/inflight-approvals.md` 한 파일만
> 추가한다. 실제 구현은 §6 분할안의 leaf 가, 설계 머지 뒤 **사람이 게이트를 풀 때** 맡는다. 본문의 `파일:줄`
> 앵커는 전부 이 리포 HEAD `76ccee6` 트리를 **읽어** 확인한 것이다. 볼트 쪽 파일은 `(볼트)` 로 표시하고, 이
> 체크아웃 밖(대시보드 Android 앱, 브라우저 MCP 서버 리포, 원격 조종 스택, systemd 유닛의 실제 설정)은
> '미확인'으로 남겼다. 외부 문서는 이 런에서 **열어 확인한 것만** '확인'(최종 도달 URL, 조회 2026-11-17)이고,
> 못 연 것은 '미확인'이다(열람 수단은 §3.1 확인표 머리말). 자매 `run-event-stream.md` 가 확인한 출처(U1~U5,
> 조회 2026-11-13)는 "형제 확인"으로 다시 인용한다. 자매 `run-event-stream.md` 는 머지되어 § 번호로 인용하고,
> `chat-channels.md` 의 §4~§6 은 아직 자리표시(`chat-channels.md:368-387`)라 이 노트를 인터페이스로 가정한다.
> 공개 리포라 예시·스키마는 합성 placeholder(`example.invalid`·`<token>`·`<ULID>`·
> `00000000-0000-4000-8000-000000000000`)이고 실명·토큰·호스트·절대 홈경로·SMS/메일 본문을 넣지 않는다.

## 왜 이 문서인가

사용자 의도(2026-11-13, 태스크 Spec): *"OpenMausBot은 권한 브로커가 셸 명령·파일 수정·에이전트 질문을 인라인
허용/거부 카드로 바꾼다. Grok Bot은 액션별 승인 설정과 2FA·캡차 넘김 흐름을 둔다."* *"지금 macp는 사람 판단이
필요하면 태스크가 파킹되고, 재개하면 파이프라인을 처음부터 다시 돈다. 권한에 막힌 스테이지는 결과 없이
끝난다."* (두 선행 봇의 서술은 태스크 Spec 요약을 그대로 옮긴 것이며, 이 노트는 외부 URL 로 재인용하지 않는다.)

§1 이 보여 주듯 사람 판단이 필요한 순간은 넷이다 — 권한 거부, 되돌리기 어려운 조작, 방향 선택, 로그인·2FA.
넷 다 지금은 같은 길로 처리된다: 스테이지가 끝나고, 태스크가 파킹되고, 사람이 터미널에서 답하고, **새 run_id
로** 처음부터(또는 상속분만 빼고) 다시 돈다. 이 노트는 그 사이에 **승인 카드**를 둔다. 스테이지를 멈춘 채
대시보드(웹·앱 동일)에 허용·거부·답변 카드를 띄우고, 답이 오면 **같은 run_id 의 같은 스테이지 세션**이 이어서
돈다. 답이 오지 않아 만료되면 오늘의 파킹(`FAIL`+`blocked_on` → `awaiting_human`)으로 내려간다. 여섯 축:
(§1) 현황 전수표, (§2) 승인 대상 분류와 설정, (§3) 실행 의미(대기·예산·만료·이벤트·재시작), (§4) 대시보드,
(§5) 안전, (§6) 단계별 구현 분할안과 라우트·화면 수준 완료 조건.

**핵심 전제 셋.** ① **새 상시 인프라를 신설하지 않는다.** 브로커는 세 곳에만 산다 — 스테이지 프로세스 트리
안(PreToolUse hook 과 권한 위임 MCP 도구), 이미 있는 본부 대시보드 프로세스(카드 API), 오케스트레이터 틱(재개·
만료 sweep). 근거는 자매 원칙 `event-triggers.md:41-45` 다. ② **끄면 오늘과 바이트 단위로 같다.** kill switch
`MACP_APPROVALS` 는 기본 꺼짐(명시적 `1` 만 켬)이고 켜는 것은 사람 게이트다 — "호출 시점에 읽는 env" 라는
`usage_enabled()` 패턴(`stages.py:539-544`)을 기본값만 뒤집어 쓴다. claude argv 골든 테스트
(`test_agent_backend_golden.py:14-17` 가 골격과 순서를 핀)는 꺼진 모양을 고치지 않고, 켜진 모양만 의식적으로
더한다. ③ **만료·실패·불확실은 모두 오늘의 파킹으로 떨어진다.** 카드는 파킹보다 나쁜 결과를 만들지 않는다 —
"불확실하면 WAIT, 만료는 먼저 본다"는 `resume.py:21-34` 의 fail-safe 규율이다. 결정의 원천은 승인
저장소(§3.8)이고, 이벤트 스트림은 감사용 거울이다(§3.5).

---

## §1 현황 전수표 — 사람 입력이 필요한 상황 · 지금의 처리 · 재개 때 버리는 것

### 1.1 상황별 전수표

범례 — **지금 일어나는 일**: 스테이지 안과 밖에서 실제로 도는 코드(`파일:줄`). **알림**: 사람에게 무엇이 언제
가나. **재개 경로**: 사람이 답한 뒤 런이 다시 도는 길. **버리는 것**: 그 길이 다시 사야 하는 작업. 시점·경로는
전부 이 HEAD(`76ccee6`) 확인. "V<n>" 은 §3.1 확인표의 공식 문서 행이다.

| # | 상황 | 지금 일어나는 일 (`파일:줄`) | 알림 | 재개 경로 | 재개 때 버리는 것 |
|---|---|---|---|---|---|
| ① | **권한 거부** — 허용 목록 밖 호출 | 스테이지 argv 는 `-p` + `--allowed-tools`(`stages.py:701`)뿐이고 `--permission-mode`·`--permission-prompt-tool`·`--settings` 가 없다(`:688-705`). 권한 모드를 정하지 않은 `-p` 런에서 승인이 필요한 호출은 승인 주체가 없으면 그 자리에서 거부된다(V2). 실사례 둘이 "This command requires approval" 로 FAIL(unverified) 파킹됐다 — pnpm(`stages.py:263-266`), flutter(`:268-274`) | verify 가 `blocked_on` 을 쓰면 ⛔ 1회(`orchestrator.py:1207-1232`, 문구 `:1399-1403`). 아니면 알림 없이 `_retry`(`:1245-1247`) | 사람이 md 의 `allowed_tools` 를 넓혀 커밋하고 `cli resume`(`concepts.md:190-193`). 단 이 필드는 implement 에만 먹는다(`stages.py:441-446`) — verify 에서 막힌 두 사례는 엔진 코드 수정(`FALLBACK_RUNNER_TOOLS` `:275-278`)으로만 풀렸다 | 새 run_id(`runner.py:1091-1093`). 직전 verify 가 FAIL 이면 상속 0(`stages.py:2535-2537`) — research 부터 다시. flutter 사례는 "one human intervention and two worker runs"(`:268-274`) |
| ② | **되돌리기 어려운 조작** — 실계좌·결제·사용자 명의 제출·배포·공개 | 스테이지 **안**에는 엔진 게이트가 없다. 막는 것은 프롬프트 규칙 (볼트) CLAUDE.md §12 "태스크 본문이 명시적으로 허용할 때만" 뿐이고, 허용 목록 안의 명령(예: `Bash(npm:*)` 아래의 publish)은 그대로 실행된다. 엔진 게이트는 **경계**에만 있다 — `auto_merge` 와 UI merge hold(`concepts.md:69-75`), 실계좌 태스크의 자동 분할 거부(`rescope.py:226-227`, 태그 `:70`), 채널 R2 신호 → `manual`(`chat-channels.md:258`, `:268-270`) | 에이전트가 스스로 멈추고 `blocked_on` 을 쓸 때만 ⛔ | 사람이 손으로 처리한 뒤 md 를 고치고 `cli resume` | 파이프라인 전체(①과 같음) |
| ③ | **방향 선택** — 설계 갈림길, 정보 부족 | 스테이지에서 사람에게 묻는 채널이 없다. 질문 도구 AskUserQuestion 은 `-p` 에서 **권한 호스트가 있을 때만** 제공되는데(V3) 지금 argv 엔 호스트가 없다. 백그라운드를 켰던 시절의 헤드리스는 "Awaiting user input." 으로 끝났다(`stages.py:3113-3116`). 그래서 에이전트는 추측하거나 `blocked_on` 으로 파킹된다 | ⛔ 1회(`blocked_on` 인 경우), 그 뒤 AWAITING_HUMAN 재알림 백오프(기본 1d/3d/7d — `orchestrator.py:1770-1772`, `:1823-1843`) | `/decide`(reply 는 `kind: human` 일 때만 — `decide.md:25-35`), `human reply` CLI(`concepts.md:182-190`), md 수정 + resume. 셋 다 터미널이 필요하다 | 파이프라인 전체 |
| ④ | **로그인·2FA·캡차** | 2FA **인증번호**는 사람이 필요 없다 — 상시 규칙 `macp-sms`·`macp-mail` 로 읽는다(`stages.py:280-298`; (볼트) CLAUDE.md §11·§11b). **로그아웃**이면 에이전트는 로그인하지 않고 멈춰 `blocked_on` 을 쓴다(메타데이터 `stages.py:619-621`). 사람은 (볼트) CLAUDE.md §12 의 재로그인 절차를 밟는다. 캡차·폰 앱 간편인증은 같은 모양으로 보이나 엔진 경로로 확인하지 못했다 — 미확인 | browser_watch 경보(볼트 운영 — 미확인) + ⛔ | 재로그인 뒤 `cli resume` | 파이프라인 전체 |
| ⑤ | (부수) **스테이지 타임아웃** — 사람 입력은 아니지만 "기다림 = 프로세스 종료"라는 구조가 같다 | 러너가 `TimeoutExpired` 를 잡아 implement 면 WIP rescue → `checkpoint_push`(`runner.py:1324-1387`). 연속 2회면 파킹(`orchestrator.py:1233-1244`) | 2회째 파킹 때 경보(`:1244`) | 1회째는 `_retry` 가 origin/<branch> 를 이어받는다(`runner.py:1130-1142` "CONTINUE it") | 끊긴 스테이지의 세션 문맥과 그 스테이지의 미커밋 작업(implement 외) |
| ⑥ | (부수) **러너 재시작 뒤 재전달** | 지금 NEEDS_INPUT 을 보내는 유일한 곳(`runner.py:2309-2332`). FAIL 이 아닌 이유가 주석에 있다 — FAIL 이면 `_retry` 로 가서 막으려던 재실행을 일으킨다(`:2317-2321`). collect 가 AWAITING_HUMAN 으로 바꾼다(`orchestrator.py:1185-1186`) | **없음** — collect 의 NEEDS_INPUT 분기는 상태만 바꾸고, 첫 재알림은 백오프가 지난 뒤다(`:1823-1843`) | 사람이 판단해 resume | 사람이 resume 하면 ①과 같다 |

**읽는 법.** ①~④ 는 처리 모양이 하나다 — **스테이지가 끝나야** 사람에게 닿고, 답은 **터미널**로만 돌아오며,
돌아온 답은 **새 run_id** 에서만 효과가 난다. 엔진이 "사람이 필요하다"를 런 **중간에** 아는 경로는 하나도 없다.

### 1.2 재개 경로별로 버리는 작업량

| 재개 경로 | 상속하는 것 | 다시 사는 것 | 근거 (`파일:줄`) |
|---|---|---|---|
| **`_retry`** (verify FAIL, 일반 실패) | retry_context 로 넘기는 진단문뿐 | 새 run_id · 새 corr_id · `attempts+1`. 직전 verify 가 FAIL 이면 research·plan·implement·verify 전부 | `orchestrator.py:1728-1767`, `stages.py:2520-2537`, `runner.py:1126-1144` |
| **`cli resume --after-commit`** | 직전 run 의 verify 가 FAIL 이 아니면 completed 스테이지 — implement 는 origin/<branch> 가 있을 때만(`runner.py:1145-1158`) | 새 run_id. `refail_count=0 → state=FAILED → blocked_on=''` 순서로 바꾼 뒤 일반 디스패치(`resume.py:320-333`) | `stages.py:2511-2539`, `resume.py:382-394` |
| **타임아웃·용량 재시도** | origin/<branch> 의 커밋(rescue + checkpoint) | 끊긴 스테이지를 새 세션으로 처음부터 | `runner.py:1360-1378`, `:1449-1466`, `:1130-1142` |
| **재전달 NEEDS_INPUT** | 런을 다시 돌리지 않는다(그것이 목적) | 사람이 resume 하면 위 둘째 행 | `runner.py:2309-2332` |

**언제나 버리는 것** 셋이 있다. ⓐ **스테이지 세션 문맥** — 호출마다 `--session-id` 를 새로 받는다
(`stages.py:696-700`). 같은 세션을 다시 여는 코드는 엔진 어디에도 없다. ⓑ **이미 쓴 턴** — implement 상한 200턴
(`stages.py:405`), 스테이지 상한 1/2/3 h(`:125`). 형제 런 implement 가 46턴·약 16분이었다(`run-event-stream.md:106-109`).
ⓒ **사람의 주의** — 알림은 폰으로 오고 답은 터미널에서 한다. flutter 사례의 비용 서술
"one human intervention and two worker runs"(`stages.py:268-274`)가 이 셋의 합이다.

### 1.3 한계 — 이 설계가 닫는 세 공백

1. **스테이지 안에서 사람에게 묻는 채널이 없다.** 권한 프롬프트는 받을 주체가 없어 즉시 거부되고(①), 질문
   도구는 아예 제공되지 않으며(③), 되돌리기 어려운 조작은 프롬프트 규칙 하나만 막는다(②).
2. **웹·앱에 답하는 면이 없다.** 대시보드 `do_POST` 는 `/api/agent-mode`·`/api/sms` 둘뿐이고
   (`macp_dashboard.py:3157-3213`), 사람의 답은 `/decide`·`human reply`·md 수정 셋 다 터미널이다.
3. **답한 뒤 이어 가지 못한다.** 답의 효과는 새 run_id 에서만 나고, 세션 문맥을 잃으며, verify FAIL 뒤면
   상속이 0 이다. "같은 런을 이어간다"를 표현할 상태도, 봉투도, argv 도 없다.

---

## §2 승인 대상 분류 — 세 층 · 바닥 · 태스크·프로젝트별 설정

> (이 런의 다음 페이즈에서 채움)

---

## §3 실행 의미 — 대기 방식 · 시간 예산 · 만료 · 이벤트 · 재시작 생존

> (이 런의 다음 페이즈에서 채움)

---

## §4 대시보드 — 카드 API · 화면(웹·앱 동일) · 푸시 · 범위 · 원격 러너 경로

> (이 런의 다음 페이즈에서 채움)

---

## §5 안전 — 응답 인증 · 자기 승인 차단(강제와 가드레일) · 감사 기록

> (이 런의 다음 페이즈에서 채움)

---

## §6 단계별 구현 분할안 · 라우트·화면 수준 완료 조건

> (이 런의 다음 페이즈에서 채움)

---

## 부록: 이 노트가 확정한 설계 결정 요약

> (이 런의 다음 페이즈에서 채움)
