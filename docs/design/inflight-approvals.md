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

### 2.1 네 상황 × 세 층

네 상황은 이벤트에 **새로 더하는 필드 `category`** 로 구분한다. 형제가 예약한 payload `kind`
(`permission`·`question`·`confirm` — `run-event-stream.md:192`)는 그 세 값 안에서만 쓴다(§3.5).

| `category` | 판정 신호 (누가 탐지하나) | 기본 층 | 카드 `kind` | 범위 선택지 | 예 |
|---|---|---|---|---|---|
| `permission_denied` | 허용 목록 밖 호출. 1차는 PreToolUse hook 이 런 정책 스냅샷의 허용 목록과 대조한다(확실히 밖일 때만). 백스톱은 CLI 가 권한 프롬프트로 보낸 호출을 받는 `--permission-prompt-tool`(§3.1) | 쓰기 스테이지는 카드, 읽기 전용 스테이지는 거부(오늘과 같다). verify 는 Bash 러너만 예외(2.2) | `permission` | 한 번 · 이 런 동안 | verify 의 `pnpm --version`(`stages.py:263-266`) |
| `irreversible` | 바닥(2.3): 태스크 태그, 명령·도구 패턴표, 에이전트 선언 `confirm(category=irreversible)` | **카드(바닥)** — 허용 목록 안이어도, 본문이 허용해도 | `permission`(가로챈 호출) · `confirm`(선언) | 한 번만 | 패키지 공개, 실계좌 주문, 사용자 명의 제출 |
| `direction` | 에이전트가 내장 질문 도구 AskUserQuestion 을 부른다. 권한 호스트가 있으면 `-p` 에서도 제공된다(V3) | 카드 | `question` | 한 번(질문마다) | 두 설계안 중 선택, 빠진 정보 |
| `human_step` | 에이전트가 `confirm(category=human_step)` 을 부른다 — 사람만 할 수 있는 단계 | 카드 | `confirm` | 한 번 | 로그아웃된 콘솔의 재로그인 요청 |

### 2.2 세 층의 정의와 스테이지별 규칙

- **항상 자동 허용** — 스테이지의 허용 목록에 든 호출: `STAGE_TOOLS`(`stages.py:375-403`), 태스크
  `allowed_tools`, 상시 규칙 `_STANDING_RULES`(`:368-373`). 의미는 오늘과 같고 카드가 생기지 않는다. 인증번호
  읽기(`macp-sms`·`macp-mail`)도 여기다 — 사람이 필요 없는 2FA 는 카드가 되지 않는다. **예외 하나**: 바닥(2.3)에
  걸리는 호출은 허용 목록 안이어도 카드로 올라간다. PreToolUse hook 의 차단은 allow 규칙보다 먼저 평가된다(V6).
- **카드로 묻기** — (a) 허용 목록 밖 호출 중 2.4 정책이 묻기로 두는 것(기본: 쓰기 스테이지의 `permission_denied`
  전부), (b) 바닥 범주, (c) 에이전트의 질문(AskUserQuestion), (d) 에이전트의 확인 요청(`confirm`).
- **항상 거부(카드도 없다)**:
  - `FORBIDDEN_TOOL_SUBSTRINGS`(`stages.py:419-426`). 오늘 허용 목록 검증이 ValueError 로 막는 것(`:509-511`)을
    카드가 풀면 "rm 을 허용해 주세요" 식 사회공학 표면이 생긴다.
  - `.claude/**` 쓰기 — 헤드리스 하드 가드(형제 `run-event-stream.md` §6.3).
  - 비밀·승인 장치 경로: `.secrets/`, 승인 저장소(§3.8), 정책 파일, 원장 `Worker/assignments/*.json`
    (사람에게도 "손으로 고치지 말라"는 파일 — `concepts.md:80-83`).
  - 에이전트의 로그인 폼 제출 — 로그인은 사람 몫이다(§1 ④).
  - 읽기 전용 스테이지의 허용 목록 밖 호출 전부.

| 스테이지 | 카드 | 이유 |
|---|---|---|
| research · plan · discover | 없음 — 오늘처럼 거부 | 읽기 전용 보장(`_READONLY_GUARANTEE_STAGES` `stages.py:236`)이 그 아티팩트를 믿는 근거다. 질문(`direction`)은 쓰기가 아니라 무해하지만 1차 범위에서 뺀다(A8 결정 항목) |
| verify | `permission_denied` 중 **Bash 러너류만**(`FALLBACK_RUNNER_TOOLS` 모양 `:275-278`), `direction`·`human_step` | Edit·Write·NotebookEdit 는 항상 거부 — verify 는 "test-only BY DESIGN"(`:441-445`). 동기 사례 둘이 verify 에서 났다 |
| implement · quick · delegate · report | 네 범주 전부 | 세상에 닿는 스테이지다(`:444-445`). report 의 PR 생성·머지는 `auto_merge` 가 이미 게이트라 바닥 패턴에서 뺀다(2.3) |

### 2.3 바닥 — 실계좌·결제·사용자 명의 제출은 본문이 허용해도 카드

**정의.** 되돌리기 어렵고 사용자의 돈·명의·공개 평판에 닿는 조작이다: 실계좌 주문·이체, 결제·구매, 사용자 명의
제출(지원·신청·발송·게시), 공개 배포(패키지 publish, 스토어 제출). 이 범주는 **태스크 본문이 허용해도** 카드를
거치고, 카드의 선택지는 **"한 번 허용"과 "거부"뿐**이다("이 런 동안"이 없다). 허용은 2단계 확인이다(§5.1).

**탐지는 3중이다.**

- (a) **태스크 신호** — tags `live`·`real-trade`·`real-account`(엔진 선례 `_LIVE_TAGS` `rescope.py:70`, 판정
  `:154-158`)가 있으면 그 태스크의 **외부 쓰기성 호출 전부**(밖으로 나가는 Bash, 브라우저 `click`·`type`,
  `gh` 쓰기)가 바닥 대상이다.
- (b) **명령·도구·호스트 패턴표** — publish·deploy·release·주문·결제류 명령과 결제·주문 호스트. 구체 정규식은
  A1 이 테스트와 함께 정한다(형제 §4.2 가 마스킹 정규식을 S2 로 미룬 것과 같은 방식). 파이프라인 자신의 PR
  생성·머지는 `auto_merge`·UI merge hold 가 이미 게이트라 뺀다(`concepts.md:69-75`).
- (c) **에이전트 선언** — 되돌리기 어려운 조작 **직전에** `confirm(category=irreversible, summary=…)` 을 부르라는
  규칙. 스테이지 프롬프트는 `.claude/**` 라 헤드리스가 고칠 수 없으므로 메타데이터 줄로 싣는다(브라우저 lease
  규칙 선례 `stages.py:588-597`).

**정직한 한계.** 범용 도구 `browser_click` 은 패턴만으로 "이 클릭이 결제 버튼인가"를 가를 수 없다. (a) 태그가
없고 (c) 선언이 빠지면 구멍이 남는다. 그래서 브라우저 MCP 서버 쪽 클릭 가드(결제·제출 화면의 버튼 문구·URL
패턴)가 필요하다. 그것은 다른 리포라 사람 게이트(A8)다.

| | 지금 — (볼트) CLAUDE.md §12 | 이 설계 |
|---|---|---|
| 결제·삭제·사용자 명의 제출 | "태스크 본문이 명시적으로 허용할 때만 한다" — 본문 허용이 곧 실행 | **본문이 허용해도 카드.** 본문 허용은 "카드를 띄워도 된다"는 뜻일 뿐이다 |
| 반영 | — | 볼트 문서 반영은 사람 게이트 leaf A8(이 리포의 PR 은 볼트를 고칠 수 없다) |

**chat-channels R2 와 어휘를 맞춘다**(`chat-channels.md:258`, 바닥 `:268-270`):

| R2 신호 | 이 노트의 범주 | 처리 |
|---|---|---|
| 결정·승인 | `direction` · `permission_denied` | 카드 |
| 결제·구매 · 실계좌·주문 · 사용자 명의 제출 | `irreversible`(바닥) | 카드 — 한 번 허용만 |
| 로그인·2FA | 에이전트에게는 항상 거부, 사람에게는 `human_step` | 카드는 "사람이 해 주세요"라는 요청이지 자격 증명 입력창이 아니다(§5.1) |

**실계좌 auto_merge 게이트는 그대로다.** 카드는 스테이지 안의 조작 **하나**를 허락할 뿐 머지 게이트를 풀지
않는다. 실계좌 태스크의 자동 분할 거부(`rescope.py:226-227`)와 `auto_merge`·UI merge hold(`concepts.md:69-75`)는
손대지 않는다.

### 2.4 설정 방식 — 태스크 frontmatter · 프로젝트 config · env

태스크 frontmatter 에는 맵 하나를 둔다(합성 예시). 값 뒤에 주석을 달지 않는다 — 볼트 규칙이다.

```yaml
approvals:
  ask: ["Bash(pnpm:*)", "Bash(flutter:*)"]
  deny: ["mcp__macp-browser__browser_type"]
  expire: "6h"
  inline_hold: "10m"
```

| 키 | 뜻 | 기본값 |
|---|---|---|
| `ask` | 허용 목록 밖이지만 카드로 물어도 되는 규칙. 허용 목록 **안**의 규칙을 적으면 그 호출도 매번 묻는다(좁히기) | 쓰기 스테이지의 `permission_denied` 전부 |
| `deny` | 카드 없이 거부할 규칙. 허용 목록 안이어도 거부한다(좁히기) | 없음 |
| `expire` | 카드 만료(§3.4) | 24h — 숫자는 사람 게이트(A8) |
| `inline_hold` | 스테이지를 붙잡고 기다리는 상한(§3.3 R-T1) | 9m(540 s) — 숫자는 사람 게이트(A8) |

- **파싱.** 태스크 frontmatter 는 `yaml.safe_load` 로 읽혀(`repos.py:77-82`) 중첩 맵이 dict 로 온다.
- **프로젝트별.** 볼트 `.macp/config.toml` 의 `[approvals]`(이 호스트의 기본값)와 `[approvals.repo."<repo-url>"]`
  (그 리포의 태스크 전부)에 같은 키를 둔다. env 는 `MACP_APPROVALS`(kill switch)와 `MACP_APPROVALS_DIR`(§3.8)이고,
  리졸버는 env → config → 기본값의 기존 순서를 따른다(`docs/configuration.md:26-42`, XDG 기본값 선례 `:57`).
- **우선순위.** 엔진 바닥(내릴 수 없음) > 엔진의 항상 거부 > `deny`(태스크 ∪ 프로젝트) > `ask`(태스크 ∪
  프로젝트) > 허용 목록(자동 허용) > 기본값.
- **검증 — 넓히는 키가 없다.** 설정으로 할 수 있는 일은 "카드로 물을 범위를 정하기"와 "좁히기"뿐이다. 바닥을
  자동 허용으로 내리는 `allow:` 같은 키는 **만들지 않고**, 모르는 키·FORBIDDEN 에 걸리는 `ask` 규칙·`*` 전체
  규칙은 디스패치를 거절한다. `_safe_tools` 가 금지 규칙을 ValueError 로 막는 선례(`stages.py:509-511`)와 같다.
  검증 자리는 `task_validate.validate_frontmatter`(`task_validate.py:334`)다.
- **배선.** 새 frontmatter 필드는 payload 에 실리기 전까지 아무 효과가 없다 — "A frontmatter field is dead unless
  it reaches the payload"(`tasks.py:217-220`; `allowed_tools` 배선 `:206-208`; 그것을 잊었던 선례 `stages.py:484-489`).
  그래서 A2 가 `tasks.py` 배선을 따로 맡는다. `allowed_tools` 는 implement 에만 먹지만(`stages.py:446`),
  `approvals` 는 브로커가 도는 모든 스테이지(2.2 표)에 적용된다.

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
