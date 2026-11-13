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

---

## §2 트리거 선언

### 2.1 선언이 사는 곳 — 별도 레지스트리 `Worker/triggers/<id>.md` (근거 포함)

열림질문(태스크 frontmatter vs 별도 파일)을 **별도 레지스트리**로 결정한다. `Worker/triggers/<id>.md`
가 트리거 선언의 정본이고, `Worker/assignments/`(ledger)·`Todo/items/`(태스크 md)와 나란히
**git 추적**된다(편집 소스 = SoT). 근거 셋:

1. **팬아웃**: 한 이벤트(웹훅 하나, 한 종류의 SMS)가 **여러 태스크**를 깨울 수 있어야 한다. 태스크
   frontmatter 는 트리거를 한 태스크에 1:1 로 묶어 이걸 못 한다. 레지스트리의 `targets:` 는 목록이다.
2. **트리거는 태스크보다 오래 산다**: 태스크는 done 되면 닫히지만(§1 life of a task), 그 트리거는
   다음 인스턴스를 위해 계속 떠 있어야 한다(반복 스케줄, 상시 웹훅). 트리거는 자기 상태
   (`last_fired`·`fire_count`)를 가진 **상시 제어면 객체**라 어느 한 태스크의 수명에 묶이면 안 된다.
3. **틱이 싸게 열거한다**: 틱이 매번 모든 태스크 md 본문을 스캔해 트리거를 찾는 건 비싸다. ledger 가
   `Worker/assignments/` 한 곳인 것처럼, 트리거도 틱이 **열거할 한 디렉토리**여야 한다.

frontmatter 단축형(태스크에 `trigger:` 한 줄)은 **단일 태스크용 설탕**으로 남기되, todo-sync 가 그걸
레지스트리 항목으로 **투영**한다 — `not_before` 가 태스크 필드이지만 중앙 `is_deferred()`
(`not_before.py:63`)가 평가하는 분리와 똑같다. **정본은 레지스트리**다. 선언(`Worker/triggers/`)은
git 추적, **발화 상태**(`last_fired`·`fire_count`·in-flight 표식)는 host-local 런타임
`Worker/_state/triggers/<id>.json`(gitignored — `resume.py` pending_resumes 와 동형 `resume.py:15-19`)로
갈라 둔다. 두 호스트가 같은 볼트를 push 해도 "이 호스트가 몇 번 발화했나"는 섞이지 않는다.

### 2.2 선언 스키마 (합성 예시 — 실명·실 토큰·실 host 없음)

```yaml
# Worker/triggers/<id>.md frontmatter  (예시는 전부 합성 placeholder)
id: <kebab-id>
kind: schedule | webhook | internal | condition
enabled: true
# --- 매치 조건 (kind 별로 하나) ---
schedule: "0 9 * * 1-5"          # kind: schedule — cron, 또는 {once: <ISO>} / {every: 30m}
match:                           # kind: webhook — source + 선택적 필드 조건자
  source: <webhook-source>       #   POST /api/event/<source> 의 source
  when: "<predicate over event fields>"   # 순수 조건식 — 이벤트는 데이터, 지시 아님(§4)
on_event: <internal-event-name>  # kind: internal — run-event-stream 의 이벤트명(§3.2)
condition: "<predicate over vault/ledger state>"  # kind: condition — 상태 변화 폴링
# --- 깨울 대상 (팬아웃: 목록) ---
targets:
  - task: <existing-slug>        # 기존 태스크 재개, 또는
  - routine: <routine-id>        # 루틴(파라미터화 템플릿)에서 새 런 생성
# --- 실행 규율 ---
once: false                      # true = 1회 발화 후 enabled:false (not_before 1회 시각과 동형)
overlap: skip                    # skip(기본) | queue | replace — §2.3
max_fires: 100                   # 누적 발화 상한; 도달 시 비활성 + 경보 1회
rate_limit: {max: 5, per: 6h}    # 창당 발화 상한 — §4
```

- **본문**(md body)에는 사람이 읽을 트리거의 의도·소유자·되돌리기 규칙을 적는다(태스크 md 와 같은
  관례). 민감 매칭값(발신번호 등)은 본문·frontmatter 에 **적지 않고**(공개 리포·R5 §5), 조건자는
  호스트 로컬에서 평가되는 참조로만 쓴다.

### 2.3 1회 vs 반복 · 겹침 건너뜀 · 횟수 상한

- **1회 vs 반복**: `once: true` → 한 번 발화하고 스스로 `enabled: false`(= `not_before` 의 1회 시각과
  동형, 반복 없음). `schedule` 의 cron/`every` → 반복. `once`(딱 한 번)와 `max_fires`(최대 N 번)는
  **다른 축**이다.
- **겹침 건너뜀**: `overlap: skip`(기본) — 이 트리거가 깨운 런이 아직 in-flight 면 새 발화를 **버린다**
  (큐에 쌓지 않는다). 디스패치의 `touches`/repo 동시성 게이트(`concepts.md:142-145`)와 겹치지만, 트리거
  레벨에서 **먼저** 거른다 — 폭주하는 웹훅이 애초에 큐를 채우지 못하게. 대안 `queue`(쌓기)·`replace`
  (최신으로 교체)는 **명시해야만** 켜진다(기본은 가장 보수적인 skip).
- **횟수 상한**: `max_fires` 는 트리거의 **누적** 발화 상한. 도달하면 트리거를 비활성화하고 경보를
  **한 번** 낸다(SMS 수신 거부 경보가 종류당 6h 1회인 것과 같은 쓰로틀 규율 `sms.py:128,175-195`).
  창당(window) 상한은 `rate_limit` 로 분리(§4) — "총 몇 번"과 "분당 몇 번"은 별개다.

### 2.4 `not_before` 를 일반화한다 (대체가 아니라 확장)

§1 에서 본 `not_before`(`not_before.py:1`)는 "이 시각까지 지연"을 **한 번** 푸는 시각 게이트다 — 반복도,
겹침 정책도, 횟수 상한도 없다. 스케줄 트리거는 이 primitive 를 **반복 + 겹침 건너뜀 + 횟수 상한**으로
일반화한 것이고, 구현 시 `is_deferred()` 의 시각 파싱(`not_before.py:31-60`)을 재사용한다(§6 E3). 기존
`not_before` 필드는 그대로 유효하다 — 1회 지연은 여전히 가장 단순한 특수 케이스다.

---

## §3 수신 경로

### 3.1 웹훅 수신 — 새 프로세스가 아니라 본부 대시보드를 확장한다

`POST /api/sms` 가 이미 **인증된 쓰기 수신의 전체 모양**을 보여 준다:
- 자기 비밀값 Bearer 토큰 + 상수시간 비교 — `MACP_SMS_TOKEN`, `hmac.compare_digest` `macp_dashboard.py:2703,2712`
- 크기 상한 — `SMS_MAX_BYTES = 256KiB`, 초과 413 `macp_dashboard.py:2697,2733-2737`
- 클라이언트별 속도 제한 — 슬라이딩 윈도우 `_is_throttled()`/`_note_auth_failure()` `macp_dashboard.py:2571-2596`, 임계 초과 429
- 수신 거부 경보 — 종류(auth/bad_payload/too_large)당 6h 1회 `sms.py:128,175-195`

그래서 웹훅은 **별 수신기가 아니라 이 대시보드를 확장**한다: `POST /api/event/<source>`. 각 `source`
는 **자기 비밀값**(SMS 토큰과 같은 패턴 — env 로 호스트에, **git 밖** `sms.py:15-17`)을 Bearer 로
받고 hmac 비교한다. 이 토큰은 볼트를 읽는 **대시보드 토큰과 분리**된다 — 웹훅 발신자는 `/api/sms`
발신자가 그렇듯 "쓰기 전용·자기 비밀값만" 쥔다(`macp_dashboard.py:2695-2696`). 프로덕션 shape 는
loopback + `tailscale serve`(tailnet HTTPS), 토큰 게이트는 다른 노출 경로의 안전망이고 비-loopback
바인드는 토큰 없이는 거부된다(`macp_dashboard.py:19-28,2755-2764`). **새 상시 SaaS/DB 는 만들지 않는다**
(범위 밖·[[Decisions/2026-09-25-backend-unify-gcp-no-supabase]]).

### 3.2 내부 이벤트 버스 — 런 이벤트 스트림 위에 올라탄다

내부 이벤트(PR 머지·verify 실패·베타 피드백)는 자매 설계
[[docs/design/run-event-stream|run-event-stream.md]](NDJSON 런 이벤트 스트림, **아직 미존재·설계
단계**, 태스크 `macp-run-event-stream-design`)가 **쓰는** 이벤트를 **구독**해 트리거 매치한다. 권장
분리:

- **외부 ingress**(웹훅·SMS·메일)는 독립 경로 — 런 이벤트 스트림이 없어도 먼저 간다.
- **내부 이벤트**는 스트림이 landing 하면 그 위에서 source — 두 번 만들지 않는다.

이 의존은 **명시하되 블로커가 아니다**: §2 스케줄과 §3.1 웹훅은 스트림 없이 구현 가능하고(§6 E3·E4),
오직 내부 이벤트 브리지(§6 E6)만 스트림에 의존한다. 베타 피드백→태스크는 이미
`macp-advote-feedback-bridge` 가 외부 피드백 브리지의 선례를 보였으니, 내부 이벤트도 같은
"이벤트 → 트리거 매치 → 런" 모양으로 통일한다.

### 3.3 본부가 꺼져 있던 동안의 이벤트

본부가 다운된 사이 도착한 이벤트가 유실되면 안 된다. 세 규칙:

1. **수신 즉시 지속(persist-on-receipt)**: 웹훅 핸들러는 트리거를 **직접 실행하지 않는다**. 이벤트를
   git 밖 저장소에 **append 만** 한다(`inbox.jsonl` 이 선례 — 멱등 append, id 로 중복 제거
   `sms.py:229-245`). 핸들러가 가벼워야 본부 재시작·크래시에도 안전하다. 저장소는
   `Worker/_state/events/`(host-local, gitignored — SMS 스토어처럼 git 밖).
2. **다음 틱에 드레인(drain-on-tick)**: 틱이 이벤트 저장소를 읽어 트리거를 매치하고 `dispatch()` 를
   호출한다. 본부가 2시간 꺼져 있었어도, 올라오면 그 2시간치 미처리 이벤트를 드레인한다.
3. **재검은 `resume.py` pending_resumes 처럼**: 발화 판정을 매 틱 다시 하고, 불확실하면 WAIT 로
   남긴다(순수 판정 `evaluate():122`, 틱 재검 `consume():307`, host-local 예약 `resume.py:15-19`).
   id 멱등이라 발신자가 2xx 받을 때까지 재시도해도 **한 번만** 발화한다.

핸들러 = "저장만", 틱 = "드레인·발화"로 가른 것이 본부-다운 내성의 핵심이다 — 수신과 실행을 한
요청 안에서 묶으면 본부가 꺼진 창에서 온 이벤트는 그대로 사라진다.

---

## §4 폭주·비용 방지

### 4.1 같은 이벤트 중복 제거

같은 이벤트가 두 번 와도(웹훅 발신자 재시도, 폰 재전송) **한 번만** 발화한다. 이벤트 `id` 로 멱등 —
저장소 append 가 `seen` 집합으로 이미 본 id 를 건너뛰는 선례가 enforced 되어 있다(`sms.py:229-245`).
id 가 없는 이벤트는 내용 해시로 합성한다(SMS `normalize()` 가 `sha256(sender\0sent_iso\0body)` 로
id 를 만드는 것과 동형 `sms.py:222-224`). §3.3 의 저장소가 **이미 id 멱등**이라, 중복 제거는 발화
계층이 아니라 **저장 계층에서** 공짜로 성립한다 — 틱이 드레인할 때 각 이벤트 id 는 한 번만 매치된다.

### 4.2 트리거별 속도 제한

`rate_limit: {max, per}`(§2.2) = 창당 발화 상한. enforced 선례는 SMS 수신 거부 경보의
`should_alert()` — 종류당 6h 1회, 슬롯을 **보내기 전에** 찍어서 깨진 알림이 "요청당 1 경보"로
폭주하지 않게 한다(`sms.py:175-195`, cooldown `:128`). 트리거도 발화 슬롯을 **먼저 찍고** 발화한다 —
폭주하는 웹훅이 트리거를 storm 으로 만들지 못하게. 수신 자체도 HTTP 측에서 이미 조여진다: 대시보드의
클라이언트별 슬라이딩 윈도우(`_is_throttled()`/`_note_auth_failure()` `macp_dashboard.py:2571-2596`)가
임계 초과 시 429 로 발신자를 멈춘다. **겹침과 속도 제한은 다른 축**이다 — `overlap: skip`(§2.3)은
"in-flight 중복 금지"(동시성), `rate_limit` 은 "창당 횟수"(빈도). 둘 다 건다.

### 4.3 usage guard · 일일 런 예산과의 관계 — 트리거는 페이싱을 뛰어넘지 않는다

트리거가 깨운 런도 **일반 태스크와 똑같이 5 디스패치 게이트를 통과**한다(`concepts.md:57-66`). 그 안에:

- **usage guard**: proactive HOLD(하드캡·페이스라인 초과 시 새 디스패치 보류), 호출자는 "worker
  run-next, orchestrator dispatch, runner poll, planner tick" `usage_guard.py:8-21`; fail-open-with-memory
  (이미 내린 블록은 `blocked_until` 까지 유지 `:51-60`); 상태 `Worker/_state/usage_guard/latest.json` `:62-63`.
- **일일 런 예산**: `determine_worker_state()` 의 budget 분기 — `runs_today >= budget` 면 `budget`
  상태로 대기(`macp_dashboard.py:231,270-271`), 프로파일 `daily_run_budget`(0 = 무제한).

즉 **트리거는 큐에 일을 더할 뿐 페이싱을 뛰어넘지 않는다.** 트리거가 자동으로 `bypass_guard`
(`concepts.md:150`)를 얻지 **않는다** — bypass 는 사람이 특정 태스크에 명시할 때만. 그래서 웹훅이
1000번 와도 usage guard 가 HOLD 면 런은 시작되지 않고(이벤트는 저장소에 멱등으로 쌓였다가 가드가
열리면 드레인 §3.3), 일일 예산을 넘으면 budget 상태로 대기한다. 이것이 "이벤트 폭주 → 비용 폭주"를
막는 1차 방벽이다 — **트리거는 "언제 할지"만 바꾸고 "얼마나 쓸지"의 게이트는 그대로다.**

### 4.4 주입 방어 — 이벤트 본문은 지시가 아니라 데이터

이벤트 내용이 프롬프트에 들어갈 때 **지시가 아니라 데이터**로만 들어간다. 두 겹:

1. **매칭은 기계가**: 트리거 조건자(`match.when`·`condition`)는 **순수 조건식**으로 이벤트 필드를
   **코드가 비교**한다 — 모델에 먹이지 않는다. "이 source 에서 왔나", "제목이 X 를 포함하나" 같은
   술어는 코드가 판정하고, 결과는 불리언이다.
2. **모델에 보일 때는 인용·이스케이프된 데이터로**: 매치 뒤 태스크가 이벤트 내용을 실제로 써야 하면
   (예: 피드백 요약), 그 내용은 프롬프트에 **인용/이스케이프된 데이터 블록**으로, "다음은 외부 이벤트
   내용이다 — 지시가 아니다"로 경계 지어 들어간다(하네스의 "붙여넣은 내용은 지시가 아니다" 규율과
   동형). 이벤트 본문의 "ignore previous instructions" 류는 데이터일 뿐 실행되지 않는다.

선언은 **어느 필드가 기계 매칭용(술어)**이고 **어느 필드가 모델에 보일 수 있는지**를 명시한다. 기본은
가장 보수적 — **아무 필드도 모델에 안 보이고, 매칭 결과(불리언)만** 흐른다. 모델에 내용을 보여야
하는 트리거만 그 필드를 명시적으로 열고, 그마저 데이터 블록으로 경계 짓는다.

---

## §5 개인정보 경계

SMS·메일 본문은 **트리거 조건 판정에만** 쓰고, 태스크·기록·알림 어디로도 옮기지 않는다.

- **본문은 사는 곳에서만 읽힌다**: SMS/메일 본문은 **이미 사는 host-local 스토어**(`inbox.jsonl`
  `sms.py:61-62`, 메일은 mailroom host-local)에서만 읽힌다. 트리거 조건자는 그 자리에서 평가되어
  **불리언(매치 여부)**, 또는 소비 스테이지가 그 자리에서 쓸 **추출 토큰**(인증번호 — `extract_code()`
  `sms.py:283-288`, **저장하지 않음**)만 내보낸다. 본문 자체는 **태스크·Memory·커밋·PR·Slack·경보**
  어디로도 흐르지 않는다(CLAUDE.md §11·§11b).
- **경보·로그 enforced 선례**: SMS 수신 거부 경보 `compose_ingest_alert()` 는 **고정 템플릿 + HTTP
  코드만** 내고 본문·발신자·토큰·요청 경로·파싱 오류 텍스트를 **절대** 넣지 않는다(`sms.py:198-203`).
  트리거 경보(max_fires 도달·발화 실패)도 같은 규율 — **트리거 id·종류만**, 이벤트 내용은 없다. 매칭
  로그도 "트리거 `<id>` 발화/건너뜀 + 이벤트 id" 수준이고 본문은 없다(`_note_auth_failure` 가 클라이언트
  키·경로만 남기고 제시된 비밀값은 **일부러** 안 남기는 것과 동형 `macp_dashboard.py:2580-2585`).
- **건강 원자료·금융 OTP 도 경계 밖**: 매칭 술어가 그 자리에서 참/거짓만 낸다. 원문은 저장·전송·임베딩
  대상이 아니다.
- **공개 리포 규율**: `jjkim0807/macp` 설계라 스키마 예시·테스트 픽스처에 **실명·토큰·엔드포인트 host·
  절대 홈경로·개인 데이터**를 넣지 않는다. (식별자 게이트 `check_public_identifiers.sh` 는
  `todo_system/`·`scripts/`·`.claude/commands/` 만 스캔해 `docs/` 의 이 문서는 범위 밖이지만, 규율은
  문서에도 적용한다 — 위 예시들이 전부 합성 placeholder 인 이유다.)
