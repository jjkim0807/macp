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
  - docs/design/chat-channels.md         # R2 바닥·CANCEL 한계·§4~§6 (머지됨)
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
> [chat-channels.md](chat-channels.md) 의 §4~§6 은 머지되어 있고 이 노트를 인터페이스로 쓴다.
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

### 3.1 대기 방식 — 공식 문서 확인과 결정

**확인표.** 이 런의 implement 스테이지에는 WebFetch 도구가 없어서, 공개 문서 페이지를 로그인 없는 일회용
브라우저 인스턴스로 열어 본문 텍스트를 읽었다. 최종 URL 은 브라우저가 보고한 주소이고 조회일은 모두 2026-11-17
이다. 커뮤니티 글·블로그는 쓰지 않았다.

| # | 출처 | 최종 URL (조회 2026-11-17) | 이 노트가 기대는 확인 사실 | 미확인 |
|---|---|---|---|---|
| V1 | CLI reference | `https://code.claude.com/docs/en/cli-reference` | `--permission-prompt-tool` 은 비대화 모드의 권한 프롬프트를 MCP 도구(`mcp__<server>__<tool>`)로 넘기고, 그 서버 연결을 `MCP_TIMEOUT`(기본 30 s)까지 기다린다. `--resume` 은 세션 ID·이름·**.jsonl 절대 경로**를 받고 ID 는 이 머신의 모든 프로젝트에서 찾는다. `--settings` 는 파일 또는 인라인 JSON 이고 세션 한정이다 | 권한 위임 도구가 돌려줄 JSON 의 정확한 모양(문서가 가리키는 절을 이 런에서 찾지 못함) |
| V2 | Run Claude Code programmatically | `https://code.claude.com/docs/en/headless` | 권한 모드를 정하지 않은 `-p` 런은 Manual(v2.1.302 이상)이고, 승인이 필요한 호출은 PermissionRequest hook 이나 권한 위임 도구가 승인하지 않으면 거부된다. 권한 호스트가 있으면 런이 그 답을 **기다린다**(`--permission-prompts none` 이면 호스트를 건너뛰고 AskUserQuestion 을 없앤다). 권한 프롬프트 대기 중 SIGTERM 이면 답하지 않은 채 끝나고(exit 143), 재개 때 `CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1` 이면 끊긴 턴을 잇는다 | — |
| V3 | Hooks reference | `https://code.claude.com/docs/en/hooks` | PreToolUse 결정은 allow·deny·ask·**defer**. defer 는 `-p` 에서 한 턴에 도구 호출이 **하나일 때만** 받아들여지고, 턴이 `stop_reason: "tool_deferred"` 로 끝나며 대기 호출이 transcript 에 남는다. `claude -p --resume <id>` 로 같은 호출이 PreToolUse 를 다시 탄다. hook `timeout` 기본 600 s | 아래 사실 목록 |
| V4 | Settings | `https://code.claude.com/docs/en/settings` | `--settings` 는 user·project·local 위, managed 아래에 놓이고 "user 설정 파일이 둘 수 있는 키는 모두" 둘 수 있다. user 설정 파일은 hook 위치다(V3) | 실제 발화는 A3 캡처 테스트가 못박는다 |
| V5 | MCP | `https://code.claude.com/docs/en/mcp` | 서버 항목의 `timeout`(ms)이 그 서버 도구 호출의 벽시계 상한이고 idle 상한의 하한도 된다(stdio idle 기본 30분). 2분 넘는 호출의 자동 백그라운드는 비대화 모드와 `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` 에서 일어나지 않는다 | 인라인 `--mcp-config` 항목도 같은 `timeout` 필드를 받는가(A3) |
| V6 | Permissions | `https://code.claude.com/docs/en/permissions` | 막는 hook 은 allow 규칙보다 먼저 평가된다. dontAsk 모드는 AskUserQuestion 을 거부한다 | — |
| V7 | Agent SDK — Configure permissions | `https://code.claude.com/docs/en/agent-sdk/permissions` | 열었으나 권한 위임 도구의 반환 모양이 이 페이지에 없다 | V1 과 같다 |
| U1~U5 | 형제 확인(조회 2026-11-13) | `run-event-stream.md:258-266` | `result.permission_denials[{tool_name, tool_use_id, tool_input}]`(U5), hooks 가 `-p` 에서 돈다(U3) | — |

V3 에서 결정에 쓰인 사실을 더 적는다. ⓐ 한 턴에 호출이 여럿이면 defer 는 무시되고 그 호출은 **일반 권한
흐름**으로 간다. ⓑ defer 에는 "no timeout or retry limit" 이고, 세션은 `cleanupPeriodDays`(기본 30일)까지
디스크에 남는다. ⓒ 재개 때 권한 모드는 복원되지 않는다. ⓓ PermissionRequest 의 결정은 `decision.behavior`
(allow·deny, `updatedInput`)이고, 권한 위임 도구와 함께 돌면 먼저 결정한 쪽이 이긴다. ⓔ PreToolUse hook 이 시간
초과면 호출을 막지 **않는다** — `onFailure: "block"`(v2.1.295 이상)이어야 막는다. ⓕ AskUserQuestion 은 `-p` 에서
권한 호스트가 있을 때만 제공되고, defer → resume → `allow` + `updatedInput.answers` 가 문서화된 왕복이다.

**방식 비교.**

| 방식 | 재시작 생존 | 스테이지 예산 | 러너 슬롯·체크아웃 | 세션 문맥 | 주입·argv | 공식 확인 |
|---|---|---|---|---|---|---|
| 붙잡기(inline) — hook·위임 도구 안에서 블록 | ✗ 프로세스가 죽으면 대기도 죽는다(카드는 디스크에 남는다) | 쓴다(R-T2) | 잡는다 — in-flight 행만 체크아웃을 잡는다(`orchestrator.py:831-837`) | ✓ 같은 프로세스 | `--permission-prompt-tool`·`--mcp-config` 병합·`--settings` | V1·V2·V3·V5 |
| 끝내고 세션 재개(suspend) | ✓ 디스크의 세션·카드만 남는다 | 쓰지 않는다 | 놓는다 — AWAITING_HUMAN 은 체크아웃을 잡지 않는다 | ✓ 같은 세션(transcript), 프로세스만 새로 | 재개 argv `--resume <session_id>` | V3 의 defer 왕복·V1·V2 |
| **혼합(채택)** — 짧게 붙잡고 넘으면 suspend | ✓ 붙잡는 동안 죽어도 카드·세션이 남아 재개로 잇는다(3.6) | 짧은 대기만 | 짧은 대기 동안만 | ✓ | 위 둘의 합 | 위 둘의 합 |

**결정 규칙.** 계획 단계의 확인 항목 C1~C7 을 위 확인표로 판정했다.

- **C1 ✓ (V1·V2) — 백스톱 = 권한 위임 MCP 도구.** `--permission-prompt-tool mcp__macp-approval__permission`.
  승인 서버는 브라우저 서버와 함께 **하나의 인라인 `--mcp-config`** 에 합치고 `--strict-mcp-config` 를 유지한다
  (`stages.py:465-477`, `:691-695`). 이 도구는 기다릴 수만 있고 defer 할 수 없다(defer 는 PreToolUse 전용).
- **C2 ✓ ∧ C3 ✓ (V3·V4) — 1차 게이트 = PreToolUse command hook.** `--settings` 인라인 JSON 으로 그 세션에만
  싣는다. 볼트 `.claude/settings.json` 은 헤드리스 하드 가드에 걸리고 대화형 세션까지 오염시키므로 쓰지 않는다.
  matcher 없이 모든 도구에 걸고, `timeout` = 인라인 상한 + 60 s, `onFailure: "block"`(ⓔ)을 준다. 호출마다 뜨는
  프로세스라 stdlib 만 쓰고, 엔진 venv 의 python 을 직접 부르며, 결정할 것이 없으면 즉시 아무 결정 없이 끝난다.
- **C4 ✓ (V1·V2·V3) — 오래 기다리면 일시정지했다가 `claude -p --resume <session_id>`.**
- **C5 ✓ (V3 ⓕ) — 질문은 내장 AskUserQuestion.** 별도 `ask` 도구를 만들지 않는다. 확인(human_step·바닥
  선언)만 승인 서버의 `confirm` 도구로 둔다. MCP elicitation 은 비대화 세션에서 취소되므로 쓰지 않는다(V5).
- **C6 ✓ (V5) — 시간 한도는 env 가 아니라 주입 JSON 에 명시한다.** 승인 서버 항목 `timeout` = 인라인 상한 +
  60 s(ms). 자동 백그라운드는 `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`(`stages.py:3137-3141`)과 비대화 모드라
  일어나지 않는다.
- **C7 ✓ (V2·V6) — 권한 모드는 지금처럼 정하지 않는다(Manual).** `--permission-prompts none`·dontAsk 는 호스트를
  건너뛰거나 AskUserQuestion 을 없애므로 켜진 모양에서 쓰지 않는다.
- **C3 이 A3 캡처에서 깨지면**: hook 없이 위임 도구만 쓴다. defer 가 없으니 일시정지는 아래 ② 하나뿐이고,
  허용 목록 **안**의 바닥 호출은 선언(2.3 c)과 태그(a)로만 잡는다. 그때 바닥 태그 태스크의 `allowed_tools` 에서
  외부 쓰기 명령을 뺄지는 A8 이 정한다. **C1 이 깨지면**: 권한 거부는 오늘처럼 즉시 거부하고 "다음 런에
  허용할까요" 사후 카드로 degrade 한다.
- **자매 경계.** run-event-stream 은 hooks 를 **관측** 소스로 쓰지 않았다(`run-event-stream.md:238-240`). 이
  노트는 hook 을 **통제** 수단으로 쓴다.

**일시정지는 두 길이다.**

- ① **defer** — AskUserQuestion, `confirm`, hook 이 잡은 `permission_denied`. 한 턴에 호출이 하나면 턴이
  `tool_deferred` 로 끝나 프로세스가 종료된다. 여럿이면 defer 가 무시되어 일반 권한 흐름으로 가고(ⓐ), 허용 목록
  밖이므로 위임 도구가 받는다. 위임 도구는 같은 `input_sha256` 의 열린 카드를 보면 기다리지 않고 "승인 대기 중
  — 이 호출만 단독으로 다시 하라"로 거부하고, 단독 재시도가 defer 된다. 그래서 `mcp__macp-approval__*` 는
  `--allowed-tools` 에 넣지 않는다. PreToolUse 의 allow 는 권한 프롬프트를 건너뛰므로(V3) 답이 붙은 호출은 그대로
  돌고, 답 없이 실행된 `confirm` 은 성공이 아니라 "답 없음" 오류를 돌려준다.
- ② **거부 후 턴 종료** — **바닥 호출에는 defer 를 쓰지 않는다.** 묶인 호출이면 defer 가 무시되고, 허용 목록
  안의 바닥 호출이 일반 흐름에서 그대로 실행돼 버린다(ⓐ). 그래서 바닥은 인라인 대기가 끝나면 `deny` 와 사유
  "승인 카드 <id> 대기 — 턴을 끝내라"를 돌려준다.
- **러너 규칙 "열린 카드 = 일시정지".** 스테이지 프로세스가 끝났을 때 승인 저장소에 그 런·스테이지의 열린
  카드(pending·suspended)가 있으면, rc·아티팩트와 상관없이 그 스테이지를 **완료도 실패도 아닌 일시정지**로 본다.
  카드를 남기고 턴을 끝냈다면 그 단계를 하지 않은 것이다. FAIL 로 보면 `_retry` 가 새 run_id 를 만들고, 완료로
  보면 카드가 버려진다. 판정은 저장소로 하고 stdout 의 `stop_reason` 은 읽지 않는다 — 텍스트 출력 모드를 지킨
  이유(`stages.py:523-528`)가 그대로다.

### 3.2 상태 기계

```
 hook·위임 도구가 카드 생성
          │
   requested(inline) ──답(≤ 인라인 상한)──► answered ──► 같은 프로세스가 계속          (resumed_via: inline)
          │
   인라인 상한 초과 · 예산 문턱 → defer, 또는 바닥이면 deny + 턴 종료
          ▼
     suspended ── 스테이지 종료 → 러너: NEEDS_INPUT{approval_pending} → collect: AWAITING_HUMAN
          │
    ┌─────┴───────────────────────────────┐
  답(만료 전)                          만료(expires_at)
    │                                     │
  reconcile_approvals(틱, dispatch 전)    approval.resolved{expired}
  → TASK_ASSIGN{approval_resume}          blocked_on = "approval expired: <category>"
    │                                     ⛔ blocked 알림 = 오늘의 파킹
  러너: 같은 run_id · 완료 스테이지 건너뜀
  → 그 스테이지를 --resume <session_id> 로   (resumed_via: session_resume) → consumed
```

- **원장.** NEEDS_INPUT 봉투(`message.py:36`) payload 에 `approval_pending{approval_id, run_id, stage, session_id,
  expires_at}` 를 싣는다. collect 가 AWAITING_HUMAN 으로 바꾸고(`orchestrator.py:1185-1186`), `apply_reply` 가
  `blocked_on`(`assignment.py:239`)처럼 이 필드를 원장에 옮긴다. 새 필드라 옛 빌드는 모르는 키로 버린다(`:249-257`).
  AWAITING_HUMAN 은 dedup 상 busy 라 재디스패치되지 않고(`orchestrator.py:776-784`), reaper 대상이 아니며(`:1919`),
  체크아웃을 잡지 않는다(`:831-837`).
- **재개 송신.** `reconcile_approvals` 는 `dispatch()` 를 거치지 않는다. AWAITING_HUMAN 은 거기서 busy 이고, FAILED
  분기를 타면 `refail_count += 1`(`:734-736`)이 된다. `_retry` 의 송신 모양(`:1755-1767` — 새 corr_id, 에이전트 자기
  전송로)을 쓰되 `attempts`·`refail_count` 는 바꾸지 않는다. `approval_resume{run_id, stage, approval_id,
  session_id}` 는 봉투에만 싣는다 — `envelope_extra` 가 원장 payload 를 오염시키지 않게 한 선례(`:658-660`)와 같다.
- **러너 재개.** run_id 를 새로 만들지 않는다(`runner.py:1091-1093` 대신 기존 run dir 을 검증해 재사용 — 그
  태스크의 dir 인가, 저장소의 카드가 answered 인가). 브랜치 준비는 그대로다(외부 리포는 origin/<branch> 를 이어 감
  `:1042`, 볼트 `:1070`). `stages.json` 의 완료 스테이지는 상속과 같은 모양으로 건너뛰고(`:1235-1238`), 멈췄던
  스테이지만 재개 argv 로 돈다.
- **재개 argv.** `claude -p --resume <session_id>` 에 같은 `--mcp-config`·`--permission-prompt-tool`·`--settings`·
  `--allowed-tools`·`--add-dir` 를 붙인다. defer 였으면 프롬프트 없이 재개한다(V3: hook 이 allow·deny 를 낼
  때까지 프롬프트 없이). 바닥 거부였으면 한 줄 프롬프트 "승인 카드 <id> 의 답: <decision> — 같은 호출을 다시
  하라"를 준다. transcript 는 계정별 config dir 에 있으므로(`transcript_path` `stages.py:2334-2345`) 같은 계정으로
  재개하고, 계정이 바뀌었으면 .jsonl 절대 경로를 넘긴다(V1). 다른 계정으로 이어지는지는 미확인이고, 안 되면
  3.6 의 "같은 run_id 스테이지 재시작"으로 간다.
- **usage.** 같은 transcript 에 이어 쓰므로 `transcript_usage`(`stages.py:2348-2403`)가 앞선 턴을 다시 센다.
  A6 이 재개 전후 차분으로 기록한다.

### 3.3 시간 예산 규칙

- **R-T1 — 인라인 대기 상한** = min(`inline_hold`, 스테이지 남은 예산 − 마무리 여유 10분). 러너가 스테이지 시작
  시각과 `stage_timeout` 으로 마감 시각을 계산해 브로커에 넘긴다. 남은 예산이 문턱보다 작으면 붙잡지 않고 바로
  일시정지한다. 스테이지 벽시계는 `communicate(timeout)` 하나다(`stages.py:2965`).
- **R-T2 — 대기는 `stage_timeout` 을 늘리지 않는다**(시계를 멈추지 않는다). 그래서 세 불변식이 그대로다:
  `STAGE_TIMEOUTS` + `REAP_MARGIN`(`stages.py:119-132`), `_stale_bound_for`(`orchestrator.py:1873-1890`),
  `MAX_STAGE_TIMEOUT + REAP_MARGIN < RUNNER_HEARTBEAT_STALE_S`(10800 + 900 < 16200, `worker.py:155-174`).
- **R-T3 — 대기는 Bash 호출이 아니다.** `BASH_MAX_TIMEOUT_MS`(`stages.py:3135-3141`)의 지배를 받지 않는다. 대신
  hook `timeout`(기본 600 s, V3)과 승인 서버 `timeout`(V5)이 인라인 상한 + 60 s 이상이어야 하고, 둘 다 주입 JSON 에
  명시한다. 기본 제안 540 s 는 hook 기본값 600 s 아래다.
- **R-T4 — 일시정지 중에는** 스테이지 예산을 쓰지 않고 `_IN_FLIGHT` 도 아니다. reaper·하트비트와 무관하다
  (하트비트는 스테이지마다 다시 찍는다 `runner.py:1240-1244`). 재개 호출은 새 `stage_timeout` 을 받는다. 무한 연장은
  상한으로 막는다: 스테이지당 일시정지 ≤ 2회, 런당 카드 ≤ 5장, 전역 열린 카드 ≤ 10장(숫자는 A8). 넘으면 새 카드를
  만들지 않고 거부하며 사유에 "카드 상한"을 적는다.
- **R-T5 — 일시정지할 때 작업을 보존한다.** 타임아웃 arm 의 순서를 그대로 쓴다: implement 면 WIP rescue →
  `checkpoint_push`(`runner.py:1360-1378`), report 는 checkpoint 를 건너뛴다(`:1353-1359`). 결과는 `stages.json` 에
  `suspended.<stage>` 로 남긴다(`record_timeout` 선례).
- **R-T6 — 카드가 생기면** 러너의 watcher 스레드가 PROGRESS "승인 대기: <category>" 를 보낸다. 대시보드 active 행
  (`last_note`)에 바로 보이게 하기 위해서다. `updated_ts` 가 갱신돼 reaper 기한이 늘지만 스테이지 자체 타임아웃이
  그대로라 해가 없다.

### 3.4 만료

- 판정은 순수 함수이고 **만료를 먼저** 본다(`resume.py:122-152` 선례). `expires_at` 이 없거나 깨졌으면 만료다
  (`:108-119`). 기본 24 h 는 `DEFAULT_EXPIRE_HOURS`(`:51`) 선례이고 숫자는 사람 게이트다. 세션 보존(30일, V3)
  안이다.
- 일시정지 중 만료되면 `approval.resolved{decision: expired, by: system}` 를 남기고, 행은 AWAITING_HUMAN 에 둔 채
  `blocked_on = "approval expired: <category>"` 를 달아 `_notify_review` 의 blocked 문구(`orchestrator.py:1399-1403`)로
  알린다. **오늘의 파킹과 같은 모양**이다. 복구도 오늘처럼 md 수정 + `cli resume`(`concepts.md:190-193`)이고, 그때는
  새 run_id 다.
- 인라인 대기 중 만료되면(짧은 `expire`) 브로커가 deny 를 돌려주고 스테이지는 계속 간다. 그 호출만 거부되는
  것이라 오늘의 즉시 거부와 같다.
- 만료 뒤에 온 답은 410 으로 거절한다(§4.1).

### 3.5 이벤트 형식 — 예약 어휘는 그대로, 필드만 더한다

봉투는 `run-event-stream.md` §2.1 과 같다. 합성 예시 — 인라인으로 답한 권한 카드 하나와, 일시정지됐다가 만료된
질문 카드 하나:

```json
{"v":1,"run_id":"20260101T000000-demo-task","seq":57,"ts":"2026-01-01T00:12:00.000Z","stage":"verify","kind":"approval.requested","src":"hook","payload":{"approval_id":"<ULID>","kind":"permission","tool":"Bash","summary":"Bash: pnpm --version","options":["allow_once","allow_run","deny"],"expires_at":"2026-01-02T00:12:00.000Z","category":"permission_denied","scope_options":["once","run"],"suggested_rule":"Bash(pnpm:*)","tool_use_id":"toolu_<id>","input_sha256":"<hex>","floor":false,"hold":"inline","session_id":"00000000-0000-4000-8000-000000000000","policy_sha256":"<hex>"}}
{"v":1,"run_id":"20260101T000000-demo-task","seq":58,"ts":"2026-01-01T00:13:30.000Z","stage":"verify","kind":"approval.resolved","src":"orchestrator","payload":{"approval_id":"<ULID>","decision":"allow","by":"dashboard:web","note":null,"scope":"once","channel":"dashboard","latency_ms":90000,"resumed_via":"inline","answer_sha256":null}}
{"v":1,"run_id":"20260101T000000-demo-task","seq":71,"ts":"2026-01-01T00:30:00.000Z","stage":"verify","kind":"approval.requested","src":"hook","payload":{"approval_id":"<ULID>","kind":"question","tool":"AskUserQuestion","summary":"설정 키를 A 와 B 중 어느 쪽으로 둘까요","options":["A","B"],"expires_at":"2026-01-02T00:30:00.000Z","category":"direction","scope_options":["once"],"suggested_rule":null,"tool_use_id":"toolu_<id>","input_sha256":"<hex>","floor":false,"hold":"inline","session_id":"00000000-0000-4000-8000-000000000000","policy_sha256":"<hex>"}}
{"v":1,"run_id":"20260101T000000-demo-task","seq":72,"ts":"2026-01-01T00:39:00.000Z","stage":"verify","kind":"state.transition","src":"orchestrator","payload":{"from":"in_progress","to":"awaiting_human","reason":"approval:<ULID>","via":"NEEDS_INPUT"}}
{"v":1,"run_id":"20260101T000000-demo-task","seq":73,"ts":"2026-01-02T00:30:00.000Z","stage":"verify","kind":"approval.resolved","src":"orchestrator","payload":{"approval_id":"<ULID>","decision":"expired","by":"system","note":null,"scope":null,"channel":null,"latency_ms":86400000,"resumed_via":null,"answer_sha256":null}}
```

| kind | 예약 필드(`run-event-stream.md:192-193` — 불변) | 이 노트가 더하는 필드(같은 `v`) |
|---|---|---|
| `approval.requested` | `approval_id` · `kind` · `tool` · `summary` · `options[]` · `expires_at` | `category` · `scope_options[]` · `suggested_rule` · `tool_use_id` · `input_sha256` · `floor` · `hold`(`inline`·`suspended`) · `session_id` · `policy_sha256` |
| `approval.resolved` | `approval_id` · `decision` · `by` · `note` | `scope` · `channel` · `latency_ms` · `resumed_via`(`inline`·`session_resume`·`stage_restart`) · `answer_sha256` |

- `decision` 값은 `allow`·`deny`·`answer`·`expired`·`superseded`. `by` 값은 `dashboard:web`·`dashboard:app`·
  `channel:<channel-id>`·`cli`·`system` 이고 사람 이름은 들어가지 않는다.
- 한 approval_id 에 `requested`·`resolved` 는 한 번씩이다. 일시정지는 `requested` 를 다시 쓰지 않고
  `state.transition{via: NEEDS_INPUT, reason: "approval:<id>"}` 로 남긴다.
- **src 매핑.** `requested` 는 `hook` — 스테이지 프로세스 트리 안의 가로채기 생산자(PreToolUse hook 또는 승인 MCP
  서버). `resolved` 는 `orchestrator` — 본부 제어면의 응답 핸들러와 만료 sweep. 새 src 값은 없다. 둘 다 `events.in/`
  드롭박스로 쓰고 writer 는 러너 하나다(`run-event-stream.md:157-166`).
- **마스킹.** 형제 §4.1 의 `approval.*` 행(`:443`)과 §4.2 패턴을 따른다. `summary` 는 마스킹 뒤 ≤ 200자이고, 자유
  답 원문은 이벤트에 넣지 않는다(`answer_sha256` 만 — §5.3).
- **이벤트는 감사 거울이지 결정의 원천이 아니다.** `events.in/` 은 `--add-dir`(`stages.py:702-704`) 안이라 에이전트가
  쓸 수 있다. 그래서 러너는 접을 때 `approval.*` 드롭을 승인 저장소와 대조해 맞지 않으면 버리고 로그를 남긴다.
  결정은 언제나 저장소의 `answer.json` 에서 읽는다.

### 3.6 본부 재시작 생존표

| 상태 \ 사건 | 러너 재시작 | 대시보드 재시작 | 오케스트레이터 재시작 | 호스트 재부팅 |
|---|---|---|---|---|
| **인라인 대기 중** | 스테이지 프로세스 트리가 죽는다 — 러너 유닛은 KillMode=control-group 으로 서술돼 있다(`jobs.py:111-116`, 실제 유닛 설정은 미확인). 대기 중이던 권한 프롬프트는 답 없이 끝난다(V2). 카드는 디스크에 남고 재전달 NEEDS_INPUT(`runner.py:2309-2332`)이 AWAITING_HUMAN 으로 만든다. sweep 이 그 런의 열린 카드를 입양하고, 답이 오면 `--resume` + `CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1`(V2)로 잇는다 | 대기와 무관하다(저장소는 파일). 진행 중이던 POST 는 실패할 수 있고, 재전송은 409 와 함께 기존 결정을 받는다 | 무관(틱은 원래 일회성) | 러너 재시작과 대시보드 재시작을 합친 것과 같다 |
| **일시정지 중** | 무관 — 도는 프로세스가 없다. 세션은 30일 보존(V3), 카드는 디스크 | 무관 | 무관 | 무관 |
| **답은 왔지만 sweep 전** | 무관 — `answer.json` 이 있고 `consumed` 가 없으니 다음 sweep 이 한 번 소비한다 | 무관 | 다음 틱이 소비한다 | 무관 |

- 멱등 키는 `approval_id` 다. sweep 은 `resume_sent{corr_id}` 를 남긴 뒤 보내고, 러너는 같은 approval_id 의 재개를
  두 번째로 받으면 클레임으로 SKIP 한다(재전달 분류 `claims.py:77` 선례).
- 응답 없는 tool_use 로 끝난 세션을 `--resume` 했을 때의 거동은 V2 의 두 문장(끊긴 턴은 그대로 두고 다음
  프롬프트가 대화를 이끈다, env 로 끊긴 턴을 잇는다) 이상은 미확인이다. A6 이 캡처 픽스처로 확정한다. 안 되면 같은
  run_id 로 그 스테이지를 새 세션으로 재시작하고 답을 메타데이터 블록으로 넘긴다(`resumed_via: stage_restart`).
  "같은 런"은 지키고 세션 문맥만 잃는다.

### 3.7 백엔드 중립

- 처음에는 claude 만 지원한다. Solar 루프(엔진 안 HTTP 도구 루프)는 나중에 브로커 라이브러리를 직접 부를 수 있다.
  Gemini 는 승인을 끈다 — 오늘과 같다.
- capability `approval_broker` 는 서술만 하고 `required_capabilities`(`stages.py:239-249`)에 넣지 않는다. 형제가
  `event_stream` 을 다룬 방식(`run-event-stream.md:293-305`)과 같다. 없는 백엔드는 오늘처럼 즉시 거부한다.

### 3.8 승인 저장소 위치

| 후보 | 에이전트의 Write 가 닿나 | 판정 |
|---|---|---|
| run dir `Worker/runs/<run-id>/` | 닿는다 — `--add-dir` 가 `Worker/` 전체다(`stages.py:702-704`) | 기각 |
| `Worker/_state/` | 닿는다(같은 이유) | 기각 |
| **XDG `~/.local/share/macp/approvals/`**(0700/0600) | 작업 디렉토리·`--add-dir` 밖이다. 그 밖으로의 Write 가 `-p` 에서 실제로 막히는지는 미확인이고, 프롬프트가 되더라도 그 경로는 "항상 거부"라 카드가 되지 않는다(2.2) | **채택** — SMS 저장소 선례(`sms.py:16-19`, 0700 `:85-88`, 0600 `:119-123`). override 는 `MACP_APPROVALS_DIR` → config `approvals_dir` |

구조는 `<run_id>/<approval_id>/request.json` · `answer.json`(O_EXCL — 첫 답이 이긴다) · `consumed`,
`<run_id>/grants.json`("이 런 동안" 규칙 — 응답 핸들러만 쓴다), 최상위 `audit.jsonl`(§5.3)이다. `Bash(uv:*)` 같은
임의 코드는 여전히 닿으므로 위치는 가드레일일 뿐이다(§5.2).

---

## §4 대시보드 — 카드 API · 화면(웹·앱 동일) · 푸시 · 범위 · 원격 러너 경로

### 4.1 카드 API 계약

```
GET  /api/approvals[?state=pending|recent][&run=<run-id>]
→ 200 {"now": "...", "approvals": [{
        "approval_id": "<ULID>", "run_id": "...", "task_id": "...", "task_title": "...",
        "stage": "verify", "kind": "permission", "category": "permission_denied",
        "summary": "Bash: pnpm --version", "options": ["allow_once", "allow_run", "deny"],
        "created_at": "...", "expires_at": "...",
        "expired": false, "answerable": true, "allowed_decisions": ["allow", "deny"],
        "allowed_scopes": ["once", "run"], "floor": false, "remaining_s": 431,
        "hold": "inline", "remote_host": null }]}

POST /api/approvals        (Content-Type: application/json, 본문 ≤ 4 KiB)
     {"approval_id": "<ULID>", "decision": "allow|deny|answer", "scope": "once|run",
      "answer": "<선택지 또는 자유 답>", "note": "<선택>", "confirm": "<바닥 카드 id 끝 4자>"}
→ 200 {"approval_id": "...", "decision": "...", "scope": "...", "resumes": "inline|next_tick"}
→ 400 형식 · 401 인증 · 403 서버에 토큰 없음 · 404 없는 카드 · 409 이미 답함(기존 결정을 돌려준다)
  · 410 만료 · 413 크기 · 415 JSON 아님 · 422 민감값처럼 보이는 답 · 429 스로틀
```

- `expired`·`answerable`·`allowed_decisions`·`allowed_scopes`·`floor`·`remaining_s`·`hold`·`remote_host` 는 **서버가
  판정**한다(볼트 `docs/dashboard-parity.md` 의 공통 규칙). 클라이언트는 그리기만 한다.
- 기존 라우트에는 **필드 추가만** 한다. `/api/status` 에 `approvals_pending`·`approvals_next_expiry_s`, active 행에
  `approval_pending`. `/api/run?id=` 에 `approvals[]`. 기존 키는 그대로라 옛 앱이 깨지지 않는다.
- SSE 는 쓰지 않는다. 웹은 이미 3초 폴링이고, 증분이 필요하면 형제의 cursor GET 선례(`run-event-stream.md:368-415`)를
  따른다.
- POST 핸들러가 하는 일은 순서대로 다섯이다: `answer.json` O_EXCL 쓰기 → "이 런 동안"이면 `grants.json` → audit 한 줄 →
  `events.in/` 에 `approval.resolved` 드롭 → 상태 캐시 무효화(`/api/agent-mode` 의 선례 `macp_dashboard.py:3154`).
- 앵커: `_dashboard_gate`(`:2992-3021`), JSON 전용과 크기 검사(`:3114-3126`), `AGENT_MODE_MAX_BYTES = 4096`(`:3093`).
  지금 `do_GET` 은 7 라우트(`:3023-3063`), `do_POST` 는 둘(`:3157-3213`)이다.

### 4.2 화면 (웹·앱 동일)

- 맨 위에 "승인 대기" 구역을 두고, 비어 있으면 숨긴다. 접힘 키는 `mdash.sec.approvals`.
- 카드: 태스크 제목 · 스테이지 · 범주 배지 · 바닥 배지(빨강) · 마스킹된 요약 · 남은 시간(`remaining_s`).
- 버튼은 서버가 준 `allowed_decisions`·`allowed_scopes` 대로만 그린다.
  - `permission`: [한 번 허용] [이 런 동안 허용] [거부]
  - 바닥: [한 번 허용] → 카드 id 끝 4자 입력 확인 → 전송, 그리고 [거부]
  - `question`: 선택지 버튼 + 자유 답(≤ 500자, "비밀번호·인증번호를 적지 마세요" 안내)
  - `confirm`(human_step): [완료했어요] [못 함]
- 답한 뒤에는 응답의 `resumes` 로 "바로 이어 갑니다"(inline) 또는 "다음 틱(약 5분 안)에 재개합니다"를 보여 준다.
- run 상세에 승인 타임라인(요청 · 답 · 재개)을 둔다. payload 는 텍스트로 escape 한다(XSS — `run-event-stream.md:399-403`).

### 4.3 한 번 허용 vs 이 런 동안 허용

| 범위 | 무엇에 적용 | 수명 | 바닥 카드 | verify | 기록 |
|---|---|---|---|---|---|
| **once** | 그 호출 하나(`tool_use_id` + `input_sha256`) | 그 호출 | 유일한 허용 선택지 | 가능 | `answer.json`, audit |
| **run** | `suggested_rule` 하나 × 그 run_id | 그 런 끝까지. 재개된 세션에는 이어지고, 새 run_id(재시도)에는 이어지지 않는다 | 없음 | Bash 러너 규칙만 | `grants.json`, audit |

- **규칙 도출.** Bash 는 첫 토큰으로 `Bash(<cmd>:*)` 를 만든다 — 허용 목록은 공백으로 쪼개지므로 공백 든 규칙은
  아무것도 허용하지 못한다(`stages.py:281-284`). MCP 는 `mcp__<server>__<tool>`. `*` 전체 규칙과 FORBIDDEN 에 걸리는
  규칙은 만들지 않는다.
- **영구화**는 사람이 md 의 `allowed_tools` 나 `approvals.ask` 를 고쳐서 한다. 카드가 그 안내를 보여 준다.

### 4.4 푸시 알림

- **1단계는 기존 카톡·Slack 이다.** `_notify_routed`(`orchestrator.py:1309-1329`)로 immediate, `event_key =
  approval:<approval_id>`. 보내는 주체는 오케스트레이터 틱의 `reconcile_approvals` 이고, 새 카드를 처음 본 틱에 한
  번 보낸다(약 5분 안 — 인라인 상한 9분 안쪽이다). 카드 딥링크는 버튼으로만 보내고 본문에 넣지 않는다(`:1392-1396`).
  본문은 태스크 제목 · 범주 · 마스킹된 한 줄 요약이다.
- 리마인더는 만료 시간의 50% 지점에 한 번. 만료되면 기존 ⛔ blocked 알림이 간다. 채널 opt-in 은
  `scripts/NOTIFICATIONS.md:15-28` 을 따른다.
- 앱 badge 는 `/api/status` 의 `approvals_pending` 으로 띄운다 — 앱은 이미 이 라우트를 폴링한다. 네이티브 푸시는 앱
  리포 몫이라 미확인이고 사람 게이트(A8)다.
- **chat-channels §4 와의 경계.** 채널은 같은 API 를 보여 주는 또 하나의 화면이다. 알림은 approval_id 마다 한 번
  (`event_key` 로 병합)이고 채널 표시는 따로 알림을 내지 않는다. 채널에서 답하면 `by: channel:<channel-id>` 다.

### 4.5 원격 러너 경로

- 원격 러너의 브로커는 본부 상태 API(`state_store_remote` — `docs/configuration.md:138-171`)에 라우트 둘을 더해 쓴다:
  `POST /approvals/request`(카드 생성)와 `GET /approvals/get?id=`(답 폴링). 기존 라우트 표(`state_store_remote.py:214-233`)
  에 더하고, 인증은 같은 bearer 다(`:256-263`; `MACP_STATE_REMOTE_TOKEN` 은 env 전용·로그 금지 `configuration.md:59`).
- 본부는 카드를 본부 저장소에 쓰므로 대시보드 경로가 로컬 카드와 같다. 화면에는 `remote_host`(합성 라벨)를 보인다.
  이벤트는 형제 S4 경로(`run-event-stream.md:337-366`)로 온다.
- 재개 봉투는 원장 행의 `agent_id` 로, 그 에이전트 자기 전송로(`orchestrator.py:1760-1767`)로 보낸다. 세션
  transcript 가 원격 호스트에 있기 때문이다.
- **본부가 다운이면** 카드를 만들 수 없다. 즉시 거부하고 오늘의 파킹으로 간다(fail-safe). 상태 API 의 outbox
  (`configuration.md:165-168`)에 카드를 쌓지 않는다 — 답할 수 없는 카드는 의미가 없다.
- 상태 API 의 네트워크 노출은 사람 단계(`configuration.md:173-176`)이고 S4 도 아직 없다. 그 둘이 생길 때까지 원격
  러너의 승인은 꺼 둔다 — 원격에서 `MACP_APPROVALS=1` 이어도 브로커는 "remote unsupported" 로 즉시 거부한다.

### 4.6 Parity 계약 초안

머지 뒤 볼트 `docs/dashboard-parity.md` 에 사람이 옮긴다(이 리포는 볼트 문서를 고칠 수 없다). 앱 짝 태스크는 앱 리포
몫이고 사람 게이트다.

> **Dashboard parity contract — 승인 카드** (웹 먼저, 앱이 따른다. 판정은 전부 서버, 클라이언트는 렌더만.)
> **1. 데이터 계약(서버, 추가만)** — 새 라우트 `GET`·`POST /api/approvals`(4.1). `/api/status` 에 `approvals_pending`·
> `approvals_next_expiry_s`, active 행에 `approval_pending`. `/api/run` 에 `approvals[]`. `expired`·`answerable`·
> `allowed_decisions`·`allowed_scopes`·`floor`·`remaining_s`·`hold`·`remote_host` 는 서버 판정. **2. 렌더** — 맨 위
> "승인 대기" 구역(비면 숨김). 카드 = 제목·스테이지·범주·바닥 배지·요약·남은 시간. 버튼은 `allowed_*` 그대로이고,
> 바닥은 끝 4자 확인. 답 뒤 `resumes` 문구. 모르는 `kind`·`category` 는 회색 배지와 [거부]만. **3. 상태** — 접힘 키
> 웹 `mdash.sec.approvals`, 앱 FoldStore 같은 이름. **4. 제약** — 외부 네트워크 의존 없음, payload 는 escape 된 텍스트,
> 390px 가로 스크롤 0, 자격 증명 입력란 없음. **5. 테스트** — `todo_system/tests/test_macp_dashboard.py` 의
> `ApprovalsRoute`(바인딩된 서버에 실제 GET·POST — 기존 `_LiveServer` `:655` 모양) · `ApprovalsShell`(셸 계약).

---

## §5 안전 — 응답 인증 · 자기 승인 차단(강제와 가드레일) · 감사 기록

### 5.1 카드 응답의 인증

- `_dashboard_gate`(`macp_dashboard.py:2992-3021`)를 지난다 — 토큰·스로틀·쿠키.
- **토큰 필수.** 서버에 토큰이 없으면 POST 는 403 이다. 오늘 토큰 없는 loopback 대시보드는 열려 있고 스로틀도 하지
  않으며(`:2998-3000`), 바인드 거부는 비-loopback 이나 공개 노출일 때만이다(`:3224-3229`). 승인은 그 열린 모양을
  상속하지 않는다.
- JSON 전용이다(`:3114-3119`). 교차 사이트 form POST 는 이 서버가 답하지 않는 CORS preflight 없이 보낼 수 없다.
  쿠키는 HttpOnly·SameSite=Strict(`:3018-3020`)이고 본문은 ≤ 4 KiB 다.
- approval_id 는 엔진의 ULID(`message.py:47-51`)다. 시간순이라 추측이 불가능하지는 않지만 토큰 뒤에 있다.
- **첫 답이 이긴다**(`answer.json` O_EXCL). 두 화면이 경합하면 두 번째는 409 와 기존 결정을 받는다.
- 바닥 카드는 `confirm` 으로 id 끝 4자를 받는다. 보안 장치가 아니라 실수 탭을 막는 확인 장치다.
- 인증 실패 스로틀(`_note_auth_failure`)을 그대로 쓴다.
- 자유 답에 민감값 패턴(형제 §4.2 의 토큰·키=값·인증번호·이메일·전화)이 보이면 422 다. 카드는 비밀을 건네는 통로가
  아니다. 로그인·인증번호는 카드로 받지 않는다 — human_step 카드는 "사람이 직접 하고 [완료했어요]"뿐이고, 인증번호는
  에이전트가 `macp-sms`·`macp-mail` 로 읽는다((볼트) CLAUDE.md §11·§11b).

### 5.2 자기 승인과 설정 변경 차단 — 강제와 가드레일을 나눈다

| # | 장치 | 막는 것 | 강도 |
|---|---|---|---|
| 1 | 브로커 도구(hook·승인 MCP 서버)에 "답을 쓰는" 연산이 없다 — 요청·대기·읽기만 | 에이전트가 도구로 스스로 승인 | 구조적 강제 |
| 2 | 저장소는 `--add-dir`·볼트 밖, 0700/0600(§3.8) | Write·Edit 로 `answer.json` 쓰기 | 도구 수준 강제 — 위치에 대해서만 |
| 3 | MCP 서버 env 는 config 의 `env` 블록뿐이다(`stages.py:331-336`). 거기에 토큰을 넣지 않는다 | 브로커가 대시보드에 스스로 POST | 구조적 강제 |
| 4 | 정책은 런 시작 때 스냅샷으로 뜨고 `policy_sha256` 을 남긴다. 런 중간의 md·config 변경은 무시한다 | 볼트 md 를 고쳐 `ask` 를 넓히기 | 그 런에 대해 강제. 다음 런에 쓰이려면 볼트 커밋이 필요하고 git 에 남는다 |
| 5 | 바닥을 내리는 설정 키가 없다(2.4) | 설정으로 바닥 해제 | 구조적 강제 |
| 6 | FORBIDDEN 은 카드가 되지 않는다(2.2) | 사회공학 | 구조적 강제 |
| 7 | 이벤트는 결정의 원천이 아니다(3.5) | `events.in/` 위조 | 구조적 강제 |
| 8 | 스테이지 env 에서 대시보드·상태 API 토큰 env 를 뺀다. 오늘 `stage_env` 는 `os.environ` 을 통째로 복사한다(`stages.py:3103`) | env 의 토큰으로 POST | 부분적 — 토큰 파일과 서비스 env 파일은 같은 uid 가 여전히 읽는다 |

**가드레일의 한계를 그대로 적는다.** 워커는 사용자와 같은 uid 로 돌고, 허용 목록은 샌드박스가 아니다(`SECURITY.md:3-23`
— `Bash(uv:*)` 같은 접두 규칙은 임의 코드 실행이다). 그런 코드는 토큰 파일을 읽어 POST 하거나 `answer.json` 을 직접 쓸
수 있다. 그래서 위 1~8 은 **실수하는 에이전트와 평범한 프롬프트 주입**에 대한 보장이지, **작정한 코드 실행**에 대한
보장이 아니다. "에이전트가 절대 스스로 승인할 수 없다"고 쓰지 않는다.

- **탐지.** audit 의 출처(client key 해시)와 시각 이상 — 요청 직후 ms 단위의 답, 스테이지와 같은 호스트 loopback 에서
  온 답 — 을 경보로 잡는다.
- **진짜 강제를 위한 사람 게이트 선택지.** (a) 대시보드와 승인 저장소를 별도 OS 사용자로 돌린다. `SECURITY.md:25-28`
  의 전용 OS 사용자 권고이고, 유일한 실제 경계다. (b) HMAC 서명 — 검증자가 키를 들어야 하므로 (a) 가 있을 때만 의미가
  있다. (c) 토큰 env 제거(8) — 값싸지만 부분적이다. 권장: (c) 는 A3 에 넣고, (a) 는 A8 에서 사람이 정한다.

### 5.3 감사 기록 — 세 곳

- **승인 저장소 `audit.jsonl`** — append-only, 줄마다 `prev_sha256` 해시 체인을 단다(위조를 막지는 못하지만 드러낸다).
  필드: `ts`·`approval_id`·`run_id`·`event`(requested·answered·expired·consumed·rejected_auth)·`decision`·`scope`·
  `channel`·`client_key_hash`.
- **런 이벤트** `approval.*`(3.5).
- **원장 `history`** — `{ts, type: "approval", id}` 만 남긴다. git 추적 원장(`assignment.py:1-5`)이라 요약을 넣지 않는다.
- 보존 기간은 사람 게이트(A8 — 형제 이벤트의 30일과 맞추는 안)다.
- **남기지 않는 것.** 자유 답 원문은 에이전트에 건네기 위해 저장소의 `answer.json` 에만 두고 보존 기간이 지나면
  지운다. 이벤트에는 마스킹본과 `answer_sha256` 만 넣는다. SMS·메일·브라우저 본문과 스크린샷은 어디에도 넣지 않는다
  ((볼트) CLAUDE.md §11·§11b·§12).

---

## §6 단계별 구현 분할안 · 라우트·화면 수준 완료 조건

> **분할 원칙**: expand → migrate → contract(parallel-change)로 **각 leaf 가 단독으로 컴파일·테스트 green**. 첫 leaf 는
> 소비자 없는 additive 저장소·정책이다(가장 안전하고 독립적). 완료 조건은 전부 **로컬 명령**이다 — "CI green" 금지
> (Actions 미사용, [[Decisions/2026-09-11-no-github-actions-local-release-lane]]). 각 leaf 는 이 리포의 태스크이고
> `touches` 는 아래 열거한다.

### 6.1 leaf 목록 · 로컬 검증

| leaf | 단계 | 범위(touches) | depends_on | 로컬 검증 명령 |
|---|---|---|---|---|
| **A1** 승인 저장소 · 정책 해석기 · 감사 로그 | expand — 소비자 없음, `MACP_APPROVALS` 기본 0 | `todo_system/orchestrator/approvals.py`(신규: 저장소 리졸버, request·answer·consumed, 만료 순수 판정, 정책 우선순위와 바닥 패턴, 해시 체인 audit) · `todo_system/orchestrator/tests/test_approvals.py`(신규) · `docs/configuration.md` | **없음** — 안전한 첫 leaf | `uv run pytest todo_system/orchestrator/tests/test_approvals.py` · `uvx ruff check .` · `bash scripts/check_engine_isolation.sh`(stdlib only) |
| **A2** `approvals:` → payload 배선과 검증 | expand | `todo_system/orchestrator/tasks.py`(`load_dispatchable`) · `todo_system/orchestrator/repos.py`(`task_approvals_from_fm`) · `todo_system/task_validate.py` · 테스트 · 태스크 필드 문서 | A1 | `uv run pytest todo_system/orchestrator/tests/test_tasks.py todo_system/tests/test_task_validate.py -k approvals` |
| **A3** 스테이지 측 브로커와 argv 배선(꺼짐 기본) | expand | `todo_system/orchestrator/approval_hook.py`(신규: PreToolUse 게이트 — 분류·인라인 대기·defer/deny) · `todo_system/orchestrator/approval_broker.py`(신규: stdlib stdio MCP 서버 — `permission`·`confirm`) · `stages.py`(`build_stage_argv` 의 `--mcp-config` 병합·`--permission-prompt-tool`·`--settings`·메타데이터 줄, `stage_env` 의 토큰 env 제거) · `todo_system/tests/test_agent_backend_golden.py`(**의식적 갱신** — 꺼짐 무수정, 켜짐 새로 핀) · `todo_system/orchestrator/tests/test_approval_broker.py`(신규 — 위임 도구 반환 모양과 hook 발화의 캡처 픽스처) | A1, A2, run-event-stream S1 | `uv run pytest todo_system/tests/test_agent_backend_golden.py todo_system/tests/test_agent_backend.py todo_system/orchestrator/tests/test_approval_broker.py` |
| **A4** 대시보드 카드 API 와 웹 화면 | expand → migrate | `scripts/macp_dashboard.py` · `todo_system/tests/test_macp_dashboard.py` | A1 | `uv run pytest todo_system/tests/test_macp_dashboard.py -k Approvals` · `uvx ruff check .` |
| **A5** 인라인 왕복 배선 | migrate | `todo_system/orchestrator/runner.py`(스테이지 시작 때 정책 스냅샷·마감 시각 전달, watcher 스레드의 PROGRESS 미러, `events.in/` 의 `approval.*` 를 저장소와 대조) · `todo_system/orchestrator/tests/test_approvals_e2e.py`(신규) | A3, A4 | `uv run pytest todo_system/orchestrator/tests/test_approvals_e2e.py todo_system/orchestrator/tests/test_runner.py` |
| **A6** 일시정지 · 세션 재개 · 만료 파킹 | migrate | `runner.py`("열린 카드 = 일시정지" → NEEDS_INPUT, rescue·checkpoint 재사용, `approval_resume` — run_id 재사용·완료 스테이지 건너뛰기·`--resume`·usage 차분) · `stages.py`(재개 argv) · `assignment.py`(`approval_pending`·`apply_reply`) · `orchestrator.py`(`reconcile_approvals` — 재개 송신·만료 파킹) · `docs/concepts.md`("When a person is needed") · 골든(재개 모양) · 테스트 | A5 | `uv run pytest todo_system/orchestrator/tests/test_approvals_resume.py todo_system/orchestrator/tests/test_orchestrator.py todo_system/orchestrator/tests/test_assignment.py todo_system/tests/test_agent_backend_golden.py` · 전체 `uv run pytest -q` |
| **A7** 알림과 원격 러너 경로 | migrate | `orchestrator.py`(카드 immediate 알림·리마인더) · `todo_system/orchestrator/state_store_remote.py`(`POST /approvals/request`·`GET /approvals/get`) · `approval_broker.py`(원격 모드) · 테스트 | A6, 상태 API 의 네트워크 노출(사람 단계), run-event-stream S4 | `uv run pytest todo_system/orchestrator/tests/test_state_store_remote.py -k approvals` · 가짜 notifier 를 쓰는 알림 테스트 |
| **A8** 사람 게이트 묶음 | human_gate (contract 포함) | 숫자(인라인 540 s · 만료 24 h · 런당 카드 5 · 스테이지당 일시정지 2 · 전역 10 · 보존 기간), kill switch 기본 0 → 1, 별도 OS 사용자(§5.2 a), 볼트 반영((볼트) CLAUDE.md §12 바닥 · `docs/dashboard-parity.md` 카드 계약 — 저장소가 XDG 라 볼트 `.gitignore` 변경은 필요 없다), Android 짝 태스크, chat-channels §4 연결(leaf b 몫), 브라우저 MCP 서버 쪽 클릭 가드(다른 리포), 읽기 전용 스테이지의 질문 허용(2.2), C3 이 깨질 때의 바닥 태그 태스크 처리(3.1) | A4, A6, A7 | 사람 결정이라 로컬 명령이 없다. 문서 leaf 는 `uvx ruff check .` 와 식별자 스캔 |

### 6.2 depends_on 체인

`A1 → {A2, A4}` · `A2 → A3` · `{A3, A4} → A5 → A6 → A7` · `{A4, A6, A7} → A8`. 바깥 의존은 run-event-stream S1 → A3,
상태 API 노출과 S4 → A7 이다. 오케스트레이터 자동 분할이면 **첫 서브태스크는 A1** 이다 — 소비자 없는 저장소·정책이고
`MACP_APPROVALS` 가 기본 꺼짐이라 어떤 argv·화면도 바꾸지 않는다. 나머지는 위 체인대로 `blocked` + `on_unblock: auto`
+ `depends_on` 으로 줄 세운다. A4 는 A2·A3 과 병렬로 갈 수 있다.

### 6.3 라우트·화면 수준 완료 조건 (§6 AC 의 이빨)

부품(저장소·hook·서버)이 아니라 **배선**(스테이지가 묻고 → 화면이 보이고 → 사람이 답하고 → 같은 런이 끝난다)을 완료
조건으로 삼는다. 각 배선 leaf 는 새 프로덕션 호출 위치를 verify.md `## Wiring` 에 적는다. **부품 단위 테스트만
녹색이면 어느 leaf 도 PASS 가 아니다**(형제 `run-event-stream.md:572-601` 과 같은 규율).

- **A4**: 바인딩된 서버에 **실제 HTTP GET** 하면 대기 카드가 서버 판정 필드와 함께 온다. **실제 HTTP POST** 는 첫 답
  200, 두 번째 409, 만료 카드 410, 토큰 없는 서버 403 이다 — 양성 대조군으로 토큰 있는 서버의 정상 답 200 을 같은
  테스트에 둔다. 웹 셸에 카드 구역이 그려진다(셸 계약). Wiring = `do_GET`·`do_POST` 분기 줄과 셸 JS 의 fetch.
- **A5** — AC 의 문장을 그대로 이빨로 쓴다: 가짜 에이전트 seam(`test_runner.py` 의 agent seam)이 **실제 승인 서버
  프로세스**의 `permission` 을 부른다 → `GET /api/approvals` 에 카드가 보인다 → **실제 HTTP POST 로 카드에 답한다** →
  **대기 중이던 스테이지가 이어진다** → **같은 run_id 의 `events.ndjson` 에** `approval.requested` →
  `approval.resolved{decision: allow}` → `stage.finished` → `run.finished{outcome: ok}` 가 이 순서로 남는다. 즉 **런이
  끝난다.** 거부 경로도 같은 모양으로 짝을 이룬다. Wiring = 러너의 브로커 전달 줄, `build_stage_argv` 의
  `--permission-prompt-tool` 줄, 대시보드 POST 분기 줄.
- **A6**: 인라인 상한을 1 s 로 줄인 테스트에서 러너는 FAIL 이 아니라 NEEDS_INPUT 을 보내고, 원장은 `awaiting_human` +
  `approval_pending` 이 된다. 실제 HTTP POST 로 답하면 다음 `reconcile_approvals` 가 **같은 run_id** 로 재디스패치하고,
  스텁 러너가 같은 run dir 에서 `--resume <session_id>` argv 로 그 스테이지만 잇는다(완료 스테이지는 다시 돌지 않는다).
  `approval.resolved`·`run.finished` 가 같은 run_id 에 남고 `refail_count`·`attempts` 는 그대로다. 만료 경로는 답이
  없으면 `blocked_on` 파킹과 ⛔ 1회 — 양성 대조군으로 만료 전 답은 파킹하지 않는다. defer 왕복과 응답 없는
  tool_use 로 끝난 세션의 `--resume` 거동은 캡처 픽스처로 확정한다.
- **A7**: 원격 모드 브로커의 실제 POST 가 본부 GET 에 카드로 보이고 답이 폴링으로 돌아간다. 본부가 다운이면 즉시
  거부하고 파킹한다.
- **A1~A3**: 네거티브 단언은 양성 대조군과 함께 둔다(`run-event-stream.md:461-470`). 바닥 설정 거절 ↔ 정상 설정 통과,
  골든 꺼짐 모양 무수정 green ↔ 켜짐 모양 핀, 카드 요약에 합성 비밀 마커 0회 ↔ 카드 행은 있음. A2 는 `approvals` 가
  payload 에 실리는 것까지 보여야 PASS 다.

### 6.4 규약 · 범위 가드

- `.claude/**` 변경(예: 스테이지 프롬프트에 승인 규칙 추가)은 헤드리스가 쓸 수 없다. 완성본을 `Worker/proposals/` 에
  두고 사람이 `cp` 한다. 그 전까지는 규칙을 메타데이터 줄로 싣는다(2.3 c).
- leaf 태스크 md 는 만들지 않는다. 설계 머지 뒤 사람이 게이트를 풀 때 만든다. 이 런은 이 리포 체크아웃에서 돌아 볼트
  `Todo/` 를 쓰지도 못한다.
- 볼트 문서 반영은 A8 이다. 자매 노트는 고치지 않는다 — `run-event-stream.md` 의 예약표를 이 노트가 의미로 채웠다는
  역링크도, `chat-channels.md` §4·§5 도 각 담당 leaf 의 몫이다.
- "CI green" 금지 — 완료 조건은 위 표의 로컬 명령이다.

---

## 부록: 이 노트가 확정한 설계 결정 요약

1. **공백 셋**: 스테이지 안에서 사람에게 묻는 채널이 없고, 웹·앱에 답하는 면이 없으며, 답한 뒤 같은 런을 잇지 못한다.
   네 상황 모두 "스테이지 종료 → 파킹 → 터미널 답 → 새 run_id" 로 처리된다(§1).
2. **세 층과 바닥**: 자동 허용 = 허용 목록, 카드 = 목록 밖·바닥·질문·확인, 거부 = FORBIDDEN·`.claude/**`·비밀과 승인
   장치·로그인·읽기 전용 스테이지. 실계좌·결제·사용자 명의 제출은 **본문이 허용해도 카드**, 한 번 허용만 — 볼트
   CLAUDE.md §12 보다 엄격하고 그 반영은 A8 이다(§2.2, §2.3).
3. **설정**: frontmatter `approvals:`(`ask`·`deny`·`expire`·`inline_hold`)와 config `[approvals]`. 우선순위는 바닥 >
   거부 > `deny` > `ask` > 허용 목록 > 기본값. 넓히는 키가 없고, payload 배선은 A2 가 따로 맡는다(§2.4).
4. **대기 = 혼합**: PreToolUse hook(`--settings` 인라인)이 1차 게이트, 권한 위임 MCP 도구가 백스톱이다. 짧게 붙잡고,
   넘으면 PreToolUse `defer` 로 일시정지한 뒤 `claude -p --resume` 으로 잇는다. 공식 문서 7페이지를 최종 URL·조회
   2026-11-17 로 확인했고, 위임 도구의 반환 모양만 미확인이다(§3.1).
5. **바닥에는 defer 를 쓰지 않는다**: 묶인 호출이면 defer 가 무시되고 허용 목록이 그 호출을 통과시킨다. 그래서 거부 +
   턴 종료 + 러너 규칙 "열린 카드 = 일시정지"로 처리한다(§3.1).
6. **상태 기계**: NEEDS_INPUT → AWAITING_HUMAN + `approval_pending`. `reconcile_approvals` 가 `dispatch()` 를 거치지 않고
   봉투 전용 `approval_resume` 으로 재개해 `refail_count`·`attempts` 를 지킨다. 러너는 run_id 를 재사용하고 완료 스테이지를
   건너뛴다(§3.2).
7. **시간 예산 R-T1~R-T6**: 대기는 스테이지 시계를 멈추지 않고, 세 불변식(REAP_MARGIN·`_stale_bound_for`·하트비트)은
   그대로다. hook·서버 시간 한도는 주입 JSON 에 명시하고, 일시정지 전에 rescue·checkpoint 로 작업을 보존한다(§3.3).
8. **만료 = 오늘의 파킹**: 만료를 먼저 보고 깨진 값은 만료로 본다. `blocked_on` + ⛔, 복구는 `cli resume` 이다(§3.4).
9. **이벤트**: kind 이름과 예약 필드는 불변이고 필드만 더한다. `category` 로 네 상황을 가르고, src 는 hook·orchestrator,
   writer 는 러너 하나다. 이벤트는 감사 거울이고 결정의 원천은 저장소다(§3.5).
10. **재시작 생존**: 일시정지와 답-대기는 모든 재시작을 견딘다. 인라인 대기는 프로세스와 함께 죽지만 카드·세션이 남아
    재개로 잇는다. 안 되면 같은 run_id 스테이지 재시작으로 내려간다(§3.6).
11. **저장소**: XDG `~/.local/share/macp/approvals/`(0700/0600, SMS 선례) — run dir·`Worker/_state/` 는 에이전트 Write 가
    닿아서 기각했다(§3.8).
12. **대시보드**: `GET`·`POST /api/approvals`, 판정 필드는 서버, 기존 라우트는 필드 추가만, SSE 없음. 한 번 / 이 런
    동안, 1단계 푸시는 기존 카톡·Slack. 원격은 상태 API 라우트 둘을 더하고 그것이 노출될 때까지 꺼 둔다. parity 초안을
    포함한다(§4).
13. **안전**: 토큰 필수·JSON 전용·첫 답 우선·민감값 422. 자기 승인 차단 1~7 은 구조적 강제지만 같은 uid 에서는
    가드레일이고, 실제 경계는 별도 OS 사용자(A8)다. 감사는 해시 체인 audit·런 이벤트·원장 id 세 곳에 남긴다(§5).
14. **분할**: `A1 → {A2, A4}`, `A2 → A3`, `{A3, A4} → A5 → A6 → A7 → A8`. A1 이 안전한 첫 leaf 이고, A5·A6 이 "실제 HTTP POST 로 답하면 같은
    run_id 가 이어져 끝난다"를 라우트 수준에서 요구한다. 설계 문서만 바꾸고 leaf md·볼트 반영은 머지 뒤 사람 몫이다(§6).
