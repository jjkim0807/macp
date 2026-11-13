---
title: 이벤트 트리거 — 설계 노트 (외부·내부 이벤트가 주기 틱을 기다리지 않고 런을 깨운다)
status: draft
created: 2026-11-13
related:
  - Todo/items/macp-event-triggers-design.md            # 이 태스크
  - Todo/items/macp-run-event-stream-design.md          # 자매 — 내부 이벤트 버스가 올라탈 런 이벤트 스트림(NDJSON)
  - Todo/items/macp-skill-promotion-ladder-design.md    # 자매 (같은 날 ①~⑤ 설계 묶음의 형제)
  - docs/design/graph-memory.md                         # 자매 설계 노트 — 정본 형식(현황표·분할표·공개 규율)
  - docs/design/run-event-stream.md                     # 자매 leaf(아직 설계 단계, 미존재) — §3 내부 이벤트 버스의 소스
  - Decisions/2026-09-11-no-github-actions-local-release-lane.md  # 완료 조건은 로컬 명령(“CI green” 금지)
---

# 이벤트 트리거 — 설계 노트

> **이 노트의 성격**: 분석·설계 전용. 이 태스크는 코드를 한 줄도 바꾸지 않는다 —
> `docs/design/event-triggers.md` 한 파일만 추가한다. 실제 구현은 §6 분할안의 후속 leaf 가,
> 설계 머지 뒤 **사람이 게이트를 풀 때** 맡는다. 본문의 `파일:줄` 앵커는 전부 이 리포
> (`jjkim0807/macp`) HEAD 트리를 **읽어** 확인한 것이고, 확인하지 못한 지점(이 체크아웃 밖의
> 리포 — mailroom 등)은 '미확인'으로 남겼다. 자매 설계 `docs/design/run-event-stream.md` 는 아직
> 파일이 없는 **설계 단계**라 '미존재'로 표기한다. 외부 문서 URL 인용은 WebFetch 로 **열어 확인**한
> 것만 쓰는 규율이나, 이 설계는 결론을 전부 **리포 내부 선례**(SMS 수신·usage guard·resume)로
> 못박아 외부 URL 인용이 필요없었다. 공개 리포라 예시·스키마에 실명·토큰·엔드포인트 host·절대
> 홈경로·개인 데이터를 넣지 않는다(자매 `graph-memory.md` 와 동일 규율).

## 왜 이 문서인가

사용자 의도(2026-11-13, 태스크 Spec): *"지금 macp 는 주기 틱과 플래너가 런을 시작시킨다.
이벤트가 들어와도 다음 틱까지 기다리고, '이 메일이 오면 이 일을 해라'를 선언할 방법이 없다."*
배경으로 둔 두 선행 봇 — **OpenMausBot** 은 스케줄(한 번·평일·N분 간격, 겹치면 건너뜀, 횟수 상한),
웹훅(로컬 수신기 + 비밀값), 앱 이벤트(큐에 쌓아 폴링) **세 종류**의 트리거를 두고, **Grok Bot** 은
스케줄에 묶인 루틴만 밝혔다. macp 에는 그중 **아무것도** 없다 — 런을 시작시키는 건 ~5분 주기 틱과
플래너·discovery 타이머, 그리고 사람의 수동 재개뿐이다. SMS·메일은 이미 본부로 들어오지만 어느
것도 런을 **깨우지** 않는다(스테이지가 pull 로 읽을 뿐).

이 노트는 그 공백을 메운다 — 외부 이벤트(웹훅·SMS·메일)와 내부 이벤트(PR 머지·verify 실패·베타
피드백)가 **틱·플래너를 기다리지 않고 정해진 태스크·루틴을 바로 깨우는** 설계를 정리한다. 여섯
축: (§1) 지금 런을 시작시키는 것과 이벤트→런 지연 전수표, (§2) 트리거 선언 형식, (§3) 수신 경로,
(§4) 폭주·비용 방지, (§5) 개인정보 경계, (§6) 단계별 구현 분할안과 라우트 수준 완료 조건.

**핵심 전제 둘.** ① **새 상시 인프라를 신설하지 않는다** — 웹훅 수신은 이미 `POST /api/sms` 로
인증된 수신을 보여 주는 **본부 대시보드 프로세스를 확장**하고, 비밀값은 SMS 토큰과 같은 패턴으로
git 밖에 둔다. ② **내부 이벤트는 자매 설계 [[docs/design/run-event-stream|run-event-stream.md]] 의
런 이벤트 스트림 위에 올라탄다**(그게 생기면). 외부 ingress 는 독립, 내부 이벤트는 스트림에서
source — 의존은 명시하되 **블로커로 두지 않는다**.

---

## §1 현황 전수표 — 무엇이 런을 시작시키나 · 이벤트는 어디까지 오나 · 지연

### 1.1 지금 런을 시작시키는 것

범례 — **구동**: 주기 타이머인가(periodic), 외부 이벤트가 깨우는가(event-driven), 사람이
누르는가(manual). **이벤트→런 지연**: 어떤 신호가 생긴 순간부터 그에 해당하는 런이 시작되기까지.
`파일:줄` 앵커는 전부 이 리포 HEAD(074a68f) 확인.

| 런 시작 경로 | 메커니즘 (`파일:줄`) | 구동 | 이벤트→런 지연 |
|---|---|---|---|
| **주기 오케스트레이터 틱 (~5분)** | 매 ~5분 `orchestrator auto` 가 dispatchable 태스크를 로드해 **5 게이트**(`not_before`·`depends_on`·usage guard·repo 동시성·`touches`)를 통과시킨다 `concepts.md:57-66`; 진입 `dispatch()` 는 **중복 제거**(이미 active 면 no-op) `orchestrator.py:491-508`; repo map `concepts.md:213` | periodic (~5분) | **≤ 1 틱(~5분)** — 단 "이미 큐에 있는" 태스크에 한함 |
| **플래너 틱** | 토큰이 놀고 큐가 비면 더 공격적으로 새 태스크를 파일 `planner_mode.py:1-9`; 틱 셸 `scripts/planner_tick.sh`, 프롬프트 `.claude/commands/worker-planner.md` `concepts.md:159-162` | periodic (별도 타이머) | (태스크 **생성** → 다음 틱이 디스패치) |
| **discovery** | 프로젝트를 클론해 테스트·빌드·스모크를 돌리고 트리를 훑어 후보 태스크를 만든다 `concepts.md:163-165`; `discovery.py:1`, 선택 `pick():161`, 기본 간격 14일 `discovery.py:25` | periodic (기본 14일) | (태스크 생성 → 틱) |
| **follow-ups** (report "next steps") | report 스테이지가 남긴 다음 단계를 **읽는 즉시** 내구적으로 픽업 `concepts.md:167-168`; `followups.py:1-14`, 저장 `Worker/_state/followups.jsonl` `followups.py:77`, 파서 `parse_report_followups():150` | 내부(런 종료) 구동 | ≤ 1 틱 |
| **수동·조건부 재개 (resume-when)** | 사람이 `cli resume <slug> --after-commit <sha>` → 볼트가 그 커밋을 담으면 재디스패치 `concepts.md:190-193`; 예약 `Worker/_state/pending_resumes/<slug>.json` (host-local, gitignored) `resume.py:15-19`, 순수 판정 `evaluate():122`(불확실→WAIT `:131-135`), 틱 재검 `consume():307` | manual + 조건(커밋) | 사람 조작 + ≤ 1 틱 |
| **`not_before` 게이트 (예약된 1회 시각)** | 태스크가 미래 시각까지 **지연**되었다가 그 시각 이후 틱에 통과. 유일한 "지연 판정 지점" `not_before.py:1`; 허용 형식 `YYYY-MM-DD[THH:MM]` `not_before.py:3-11`; `is_deferred()` 를 모든 dispatch/NOW/stale/대시보드가 호출 `not_before.py:13,63-76`; 게이트 자리 `concepts.md:60` | 시각 구동(1회) | ≤ 1 틱(시각 도달 후) |
| **project_stall → 결정 태스크** | 조용히 멈춘 umbrella 프로젝트에 침묵 대신 **결정 태스크 하나** `concepts.md:179-180`; `project_stall.py:1`, 판정 `classify_project():198` | periodic 판정 | (결정 태스크 생성; 사람 몫) |

**읽는 법**: 위 어느 경로도 "외부에서 들어온 이벤트가 깨우는" 것이 아니다. 전부 **주기 타이머**
(틱·플래너·discovery·stall), **내부 런 종료**(follow-ups), 또는 **사람**(resume)이다. `not_before` 는
이벤트처럼 보이지만 실은 **시각 게이트** — "이 시각이 지날 때까지 기다려라"를 **한 번** 푸는 primitive
이고(반복·겹침·횟수 상한 없음), §2 의 스케줄 트리거가 이것을 일반화한다.

### 1.2 이미 본부로 들어오는 이벤트 (그러나 런을 깨우지 않는다)

| 들어오는 이벤트 | 수신·저장 (`파일:줄`) | 지금 읽는 방식 | 이벤트→런 지연(현재) |
|---|---|---|---|
| **SMS 수신** | 폰 → `POST /api/sms`(본부 대시보드, 자기 비밀값 `MACP_SMS_TOKEN`·hmac 비교) `macp_dashboard.py:2699-2752,2703,2712` → `sms.append()` 로 `inbox.jsonl` 에 적재(**git 밖**, 0600) `sms.py:15-17,61-62,229-245` | 스테이지가 **pull** 로만 — `wait-code`(실행 후 도착분만 폴링) `sms.py:305-321` | **런 트리거 아님** (pull 전용; 어떤 SMS 도 런을 시작시키지 않는다) |
| **메일 분류 틱** | mailroom(별도 리포 — **이 체크아웃 밖, 호스트·줄 미확인**)이 IMAP 을 틱으로 분류. 인증번호는 `macp-mail` CLI 로 pull(CLAUDE.md §11b) | 스테이지가 pull(`macp-mail wait-code`) / 틱이 분류 | **런 트리거 아님** (분류 틱이 태스크를 깨우지 않음) |
| **대시보드 요청** | `/api/status` 폴링 = **읽기 전용** 뷰; `do_GET` `macp_dashboard.py:2619,2644` | 브라우저가 몇 초마다 폴링 | **런 트리거 아님** (읽기 전용) |

### 1.3 공백 — 이 설계가 닫는 것

§1.1·§1.2 를 합치면 그림은 분명하다: **들어온 이벤트와 시작된 런 사이에 선이 없다.** SMS 가 12:00 에
도착해도 `inbox.jsonl` 에 조용히 쌓일 뿐, 어떤 런도 그 때문에 시작되지 않는다(마침 돌던 스테이지가
`wait-code` 로 읽어 가면 그건 그 스테이지가 **이미** 틱에 의해 시작됐기 때문이다). 선언 수단도 없다 —
"이 번호에서 이 내용의 SMS 가 오면 이 태스크를 깨워라"를 적을 자리가 없다. 그래서 이벤트 지연은
사실상 **다음 유관 틱까지(최악의 경우 영원히)**다.

이 설계는 세 조각을 더한다: **(a) 선언**(§2 — 무엇이 오면 무엇을 깨울지), **(b) 수신·지속**
(§3 — 웹훅 ingress 와 git 밖 이벤트 저장소), **(c) 배선**(§6 — 이벤트 매치 → `dispatch()` 호출,
단 5 게이트는 그대로 통과). 기존 primitive 를 **대체가 아니라 확장**한다: `not_before` →(스케줄
트리거), `inbox.jsonl` 멱등 적재 →(이벤트 저장소), `resume.py` pending_resumes 틱 재검 →(본부-다운
드레인), `/api/sms` 인증 수신 →(웹훅 ingress).
