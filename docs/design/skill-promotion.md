---
title: 스킬 승격 사다리 — 설계 노트 (반복 성공한 절차가 제안 → 스킬 → 루틴으로 올라간다)
status: draft
created: 2026-11-16
related:
  - Todo/items/macp-skill-promotion-ladder-design-a-census-ladder.md         # 이 leaf(§0~§3)
  - Todo/items/macp-skill-promotion-ladder-design-b-safety-metrics-split.md  # 형제 leaf(§4~§6·부록, depends_on 이 leaf)
  - Todo/items/macp-skill-promotion-ladder-design.md                         # 부모(손 분할)
  - docs/design/graph-memory.md       # 자매 — 정본 형식, §2 감지 신호(query_related)의 출처
  - docs/design/event-triggers.md     # 자매(2026-11-13 머지) — §3 루틴 단이 targets: routine 을 인용
  - docs/design/run-event-stream.md   # 자매(2026-11-13 머지) — §5.1 런 통계 블록이 §2 판정 소스를 넓힌다
  - Decisions/2026-09-11-no-github-actions-local-release-lane.md  # 완료 조건은 로컬 명령(“CI green” 금지)
---

# 스킬 승격 사다리 — 설계 노트

> **이 노트의 성격**: 분석·설계 전용. 이 태스크는 코드를 한 줄도 바꾸지 않는다 —
> `docs/design/skill-promotion.md` 한 파일만 추가한다. 이 파일은 두 leaf 가 나눠 쓴다: 이 leaf 가
> §0 머리말과 §1 현황 전수표 · §2 반복 감지 · §3 사다리 세 단을 채우고, §4 안전 · §5 효과 측정 ·
> §6 분할안 · 부록은 **절 제목만 둔 자리표시**다(형제 leaf 가 제자리에서 채운다). 실제 구현은 §6
> 분할안의 후속 leaf 가, 설계 머지 뒤 **사람이 게이트를 풀 때** 맡는다. 본문의 `파일:줄` 앵커는 전부
> 이 리포(`jjkim0807/macp`) HEAD(82781b2) 트리를 **읽어** 확인한 것이고, 확인하지 못한 지점은
> '미확인'으로 남겼다. 볼트 쪽 사실(원장 행 모양·`.gitignore`)은 볼트를 읽어 확인했고 경로만 적는다.
> 외부 문서 URL 인용은 없다 — 결론을 전부 **리포 내부 선례**(related_context 주입·proposals 프로토콜·
> follow-up 사람 승인·event-triggers 레지스트리)로 못박았다. 공개 리포라 예시·스키마에 실명·토큰·
> 엔드포인트 host·절대 홈경로·개인 데이터를 넣지 않는다(자매 `graph-memory.md`·`event-triggers.md` 와
> 동일 규율).

## 왜 이 문서인가

사용자 의도(2026-11-13, 태스크 Spec): *"macp 는 런마다 에피소드와 메모리를 남기지만, 반복되는
절차가 재사용 가능한 스킬로 굳는 경로가 없다. 프롬프트 제안은 사람이 손으로 옮긴다."* 배경으로 둔
두 선행 봇 — **Grok Bot** 은 사용자의 작업을 지켜본 뒤 스킬 초안을 만들고, 테스트를 거친 스킬을
스케줄에 묶인 **루틴**으로 올린다. **OpenMausBot** 은 스킬을 파일로 내보낼 때 비밀값·대화록·권한을
빼고, 가져온 쪽은 루틴을 **정지 상태로 시작**한다. macp 가 **가져오지 않는 것**은 화면 녹화로 배우는
방식이다 — macp 는 사람을 지켜보지 않고, **이미 끝난 런의 기록**(에피소드·원장·런 산출물)에서 뽑는다.

이 노트는 같은 날 태스크화된 macp 설계 묶음 ①~⑤ 의 다섯째다(자매 [[docs/design/graph-memory|graph-memory.md]]·
[[docs/design/event-triggers|event-triggers.md]]·[[docs/design/run-event-stream|run-event-stream.md]] 는
머지됨). 여섯 축: (§1) 교훈이 지금 어디에 남고 누가 읽나, (§2) 무엇을 같은 유형의 반복으로 보고
언제 승격 후보로 올리나, (§3) 제안 → 스킬 → 루틴 세 단의 파일 형식·저장 위치·기존 제안 프로토콜과의
관계, (§4) 안전, (§5) 효과 측정, (§6) 단계별 구현 분할안.

**핵심 전제 셋.** ① **스킬은 데이터다** — 엔진이 런에 주입하고, 어느 에이전트의 도구 권한도 바뀌지
않는다(`related.md` 주입이 이 불변식을 테스트로 고정한 선례 `related_context.py:16-21`). ② **사람이
승격시킨다** — 에이전트는 제안까지, 승인·설치·루틴 켜기는 사람. ③ **기존 기계를 재사용한다** — 감지는
그래프 메모리 v2 의 `query_related`, `.claude/**` 변경은 `Worker/proposals/` + `cp`, 루틴의 시각·이벤트는
event-triggers 의 레지스트리. 새 상시 데몬·SaaS 는 없다.

---

## §1 현황 전수표 — 교훈이 남는 곳 · 다음 런이 읽는 경로 · 한계

### 1.1 교훈이 남는 곳(싱크) 전수표

범례 — **git 추적?**: 볼트 리포의 `.gitignore` 기준(`Worker/runs/`·`Worker/_state/` 는 비추적).
**읽는 경로**: 다음 런(또는 사람)이 그 기록을 실제로 받는 길. `파일:줄` 은 엔진 HEAD 기준. ★ 는 태스크
acceptance 가 이름을 댄 셋(에피소드·프로젝트 메모리·제안 파일)과 그 곁가지(엔티티).

| 싱크 | 경로 | 누가 쓰나 (`파일:줄`) | 다음 런이 읽는 경로 (`파일:줄`) | git 추적? | 한계 |
|---|---|---|---|---|---|
| ★ **에피소드** | `Memory/episodes/<date>-worker-<slug>.md` | report 스테이지 — frontmatter `run_id`·`slug`·`status: pass\|fail` + `## 학습 / 메모` bullet `worker-report.md:123-154`; 같은 날 재실행은 같은 파일에 절을 덧붙인다 `:125` | `SCAN_ROOTS` 색인 `graph_memory.py:68-74` → `query_related` `:1632` → 러너가 Research 전에 `<run_dir>/related.md` 를 씀(`runner.py:1112` → `stages.py:2266` → `related_context.py:428`) → Research 가 Read `worker-research.md:17`; 플래너·brief 는 같은 목록을 `emit-context` 블록으로(`planner_tick.sh:298`·`worker-planner.md:354`·`brief.md:19`); brief 는 최근 7일을 직접 Read `brief.md:18` | 추적 | 교훈이 **산문 bullet**. 주입은 메타데이터 포인터뿐(R5). **delegate·quick 런은 에피소드를 안 쓴다**(두 프롬프트에 에피소드 지시 없음 — `run-event-stream.md:498-499` 와 같은 관찰) |
| ★ **엔티티 카드** | `Memory/entities/<name>.md` | report 규칙 "`[[name]]` 교훈이면 카드를 만들거나 사실 로그에 append" `worker-report.md:156-168`; 형식 = 정의·관계·사실 누적 로그·모순(볼트 `Memory/templates/entity-template.md`) | `SCAN_ROOTS` 포함 → 에피소드와 같은 `query_related` 경로; Verify 는 리포 모양 함정을 여기서 찾으라는 지시 `worker-verify.md:117-118` | 추적 | "158 dangling, 최다 59회" 감사 `worker-report.md:160-161` — 같은 교훈을 매 런 재도출. 카드 역시 **사실 누적 산문** |
| ★ **프로젝트 메모리** | 대상 리포의 `.macp/memory/*.md` + `index.md` | 대화형 `/macp` 스킬의 `macp memory add` `macp.md:43-45`(CLAUDE.md `@`-import·AGENTS.md 포인터 `:26-27`). **워커 스테이지 프롬프트는 쓰지 않는다**(`memory add` 언급은 `macp.md` 뿐) | 헤드리스 스테이지 cwd 는 **볼트 루트**라(`runner.py:884-888`·`:1309-1310`) 대상 리포 CLAUDE.md 의 `@`-import 는 로드되지 않는다. 대신 엔진이 `macp.read_memory(repo)`(`runner.py:956-959`)로 `index.md` 를 뺀 `*.md` 를 이어 붙여 **8000자에서 자르고**(`macp/memory.py:170-181`) 스테이지 프롬프트 메타데이터 "## Project memory" 절로 주입 `stages.py:676-678`; Verify 는 거기서 리포의 상설 게이트를 읽는다 `worker-verify.md:114` | 대상 리포에서 추적 | 8000자 절단(파일명 정렬 뒤쪽이 잘림), 태스크와 무관하게 통째로 실림, 사람이 대화형 세션에서만 갱신 |
| ★ **제안 파일** | `Worker/proposals/<slug>/{commands,.claude/commands}/<name>.md` + README | 스테이지가 `.claude/**` 하드 가드 때문에 완성본을 여기 커밋 `worker-implement.md:177-182`(선례: graph-memory G4 `graph-memory.md:298`, event-triggers §6.3 `event-triggers.md:340-344`) | 사람이 README 의 `cp` 한 줄로 `.claude/commands/` 에 반영 → 다음 런이 `load_stage_prompt` 로 읽음 `stages.py:429-430`(`COMMANDS_DIR` `:50`). 알림 = 대시보드 `why="todo"` 행 `proposals.py:188-208` ← `macp_dashboard.py:1220-1228`·`:1784` | 추적 | **사람이 손으로 옮긴다**. 알림 이전엔 "하나가 며칠씩 미반영" `proposals.py:6-7`. 대시보드가 스캔하는 `<root>` 문제는 §3.4 |
| **스테이지 프롬프트** (시스템의 실제 "스킬") | 엔진 `.claude/commands/worker-*.md` | **사람만**(헤드리스 하드 가드) | 매 스테이지가 `worker-<stage>.md` 를 통째로 로드 `stages.py:429-430` | 엔진 리포 추적 | 모든 태스크에 같은 텍스트 — 태스크 **유형별** 절차를 담을 자리가 아니다 |
| **런북 노트 + 회차 체인** (오늘의 손 스킬) | `Notes/<topic>/README.md` + `Todo/items/<topic>-<YYYYMMDD>.md` | 사람이 런북을 쓰고, delegate 회차가 끝날 때 다음 회차 md 를 `not_before` 로 스스로 쓴다 | delegate 가 태스크 본문 wikilink 로 런북을 읽음 `worker-delegate.md:11` | 추적 | 엔진 recurrence 없음 — `not_before` 는 1회 시각 게이트 `event-triggers.md:67-70`. 체인이 한 번 끊기면 아무도 다시 잇지 않는다 |
| 내구 follow-up | `Worker/_state/followups.jsonl` | 러너가 report 의 `## 다음 단계`·`## 사람이 검토해야 할 부분` 을 파싱해 append(와이어 포맷 `worker-report.md:69-81`; `followups.py:150,223`) | 플래너 `emit-context` `planner_tick.sh:248` → `from_followup` 태스크 `worker-planner.md:351` 또는 rejected jsonl; `route: human` → `awaiting_human` → `approve`/`dismiss`/`reroute` `followups.py:27-34,577-645` | 비추적 | **한 런**의 다음 할 일 — 교차 런 집계 없음 |
| discovery 리포트 | `Memory/episodes/<date>-discovery-<name>.md` | discover 스테이지 `discovery.py:16-21` | 플래너 `emit-context`·`emit-consumed` `planner_tick.sh:259,268` → `from_discovery` 태스크 `worker-planner.md:299` 또는 `discovery_rejected.jsonl`; `MAX_INJECTIONS = 3` 뒤 만료 `discovery.py:65,562-563` | 리포트 추적 / 상태 비추적 | 후보는 "할 일"이지 "절차"가 아니다 |
| 런 산출물 | `Worker/runs/<run-id>/{requirements,implementation_plan,verify,report,delegate}.md` + `run.log` | 각 스테이지 `stages.py:409-418` | 같은 런의 다음 스테이지(재시도는 상속) | **비추적**(호스트 로컬) | `run.log` 는 스테이지 stdout 마지막 2000자만 `run-event-stream.md:70` |
| 원장 | `Worker/assignments/<slug>.json` | 오케스트레이터 | 디스패치·대시보드 | 추적 | 슬러그당 **한 행**, 재디스패치가 행을 교체 `assignment.py:51-52` — 실물: 재디스패치 직후 행은 `history: []`·`attempts: 1` 로 돌아가고 `refail_count` 만 이월(이 태스크 자신의 2026-11-16 행) |
| 수동 세션 저널 | `Memory/sessions/<date>-<slug>.md` | `session_log` 체크포인트 | `Memory/index.md` 목록에만 — **`SCAN_ROOTS` 밖** `graph_memory.py:68-74`(`graph-memory.md:53`) | 추적 | 사람이 직접 한 절차는 `query_related` 가 못 본다 |
| 태스크 md Progress Log | `Todo/items/<slug>.md` | 스테이지·사람 | `SCAN_ROOTS` 포함; delegate 가 자기 태스크 md 를 읽고 고침 | 추적 | 런별 한 줄 — 절차가 아니라 경위 |

### 1.2 누가 읽나

| 주체 | 과거 기록을 어떻게 받나 | 근거 (`파일:줄`) |
|---|---|---|
| **brief** | `Memory/index.md`·최근 7일 에피소드 직접 Read + 엔진 `emit-context` 블록(메타데이터) | `brief.md:17-19` |
| **planner** | `Memory/index.md` + 엔진 블록 다섯(keep_alive·followups·discovery·planner_mode·related_context) | `worker-planner.md:63,346-354`; `planner_tick.sh:142,248,259,277,298` |
| **Research** | `Memory/index.md` + 엔진이 미리 쓴 `<run_dir>/related.md`(메타데이터만) | `worker-research.md:16-17`; `related_context.py:16-21,28-32` |
| **Plan · Implement · Report** | 과거 기록 질의 없음 — 같은 런의 앞 산출물 + 엔진이 붙인 프로젝트 메모리 절 | `stages.py:676-678` |
| **Verify** | 위와 같음 + 리포 모양 함정을 `Memory/entities/` 에서 찾으라는 지시 | `worker-verify.md:114-118` |
| **delegate** | 태스크 본문 wikilink 가 가리키는 노트·메모리 | `worker-delegate.md:11` |
| **사람(대시보드)** | 미반영 제안 행 | `macp_dashboard.py:1784` |

**읽는 법**: "이 태스크와 관련된 과거"를 골라 받는 스테이지는 지금 **Research 하나**이고, 그것도 엔진이
미리 써 둔 **메타데이터**(슬러그·제목·출처·점수)뿐이다(`graph-memory.md:74-81` 의 "reader 오분류 방지"와
같은 결론; `related_context.py:28-32` "metadata only"). 본문은 Research 가 `source` 를 직접 열어야 하고,
그 결론은 requirements.md 의 산문으로만 다음 스테이지에 전해진다. 즉 **"지난번에 같은 일을 이렇게
했다"는 절차는 어느 스테이지 프롬프트에도 기계적으로 실리지 않는다.**

### 1.3 지금의 한계 (사다리가 넘어서려는 기준선)

① **절차가 굳지 않는다**: 교훈은 에피소드의 `## 학습 / 메모` bullet 과 엔티티 카드의 사실 로그 —
둘 다 **산문**이다. "단계 · 각 단계의 확인 명령 · 함정"을 담는 필드나 형식이 없다. 절차 형태로 존재하는
것(스테이지 프롬프트, 런북 노트)은 전부 사람이 썼다(→ §3.1 제안 형식, §3.2 스킬).

② **사람이 손으로 옮긴다**: 스테이지가 프롬프트를 고치려면 `Worker/proposals/` 에 완성본을 두고 사람이
`cp` 한다. 알림은 대시보드 행 하나다. 절차가 바뀔 때마다 이 길을 타면 사람이 병목이 된다(→ §3.2 는 이
길을 **배선 한 번**에만 쓰고, 스킬 본문은 데이터로 주입한다).

③ **반복 성공을 세는 주체가 없다**: 원장 행은 재디스패치 때 교체되고(`history` 리셋), 런 산출물은
호스트 로컬이며, delegate·quick 런은 에피소드를 남기지 않는다. "같은 절차가 N 번 성공했다"를 아무도
집계하지 않는다(→ §2.2 데이터 소스, §2.4 감지 주체).

④ **반복 실행은 손 체인이다**: 정기 작업은 회차가 다음 회차 md 를 `not_before` 로 써 두는 delegate
체인이다. 엔진에 recurrence 가 없어 체인이 끊기면 그대로 멈춘다(event-triggers §2.4 가 `not_before` 를
반복 스케줄로 일반화한다 — `event-triggers.md:161-166`)(→ §3.3 루틴이 대체).

⑤ **읽기 경로가 좁다**: 과거 기록을 골라 받는 건 Research 뿐이고 메타데이터뿐이다. 실제로 손을 움직이는
Implement·delegate 는 "이 유형의 일은 이렇게 한다"를 받을 길이 없다(→ §3.2 주입 경로가 Research·Implement·
delegate 양쪽에 `skills.md` 를 둔다).

---

## §2 반복 감지 — 같은 유형 · 승격 후보 기준 · 오탐 규칙

### 2.1 무엇을 같은 유형으로 보나 — 절차 서명 + 두 신호의 캐스케이드

**셈의 단위는 런이다.** 문서(노트·결정·엔티티)는 절차를 **실행한 기록**이 아니므로 세지 않는다 — 센다는
것은 "끝난 런"(5스테이지 런은 에피소드 절, delegate·quick 런은 done 태스크)이고, 각 런은 자기 태스크의
frontmatter 로 **절차 서명**을 갖는다:

| 서명 성분 | 출처 필드 | 정규화 |
|---|---|---|
| `repo` | `repo:` (없으면 `vault`) | URL → `owner/name` |
| `tags` | `tags:` | 모든 태스크에 붙는 분류 태그(`todo`·`work`·`private`·`worker`) 제외 후 집합 |
| `touches_shape` | `touches:` | 경로 → 모양(디렉토리 + 확장자 패턴): `docs/design/<x>.md` → `docs/design/*.md`. 두 런의 "모양 일치" = 모양 문자열 집합의 자카드 ≥ 0.5 |
| `pipeline` | `worker_mode:` + 실제로 돈 스테이지 묶음 | `full`(5스테이지) · `delegate` · `quick` |
| `lineage` | 슬러그 접두 · `parent:` | `<topic>-<YYYYMMDD>` 의 `<topic>`(회차 체인), 손 분할 leaf 의 부모 |

"같은 유형"을 판정하는 신호는 둘이다 — (A) 그래프 메모리 v2 의 `query_related`(질의 입력 = slug·title·
touches·outcome·next_action `graph_memory.py:1466-1486`; 출력 = slug·source·`run_id`·score(vector/graph)·
edge·hops `:1455-1463`; 하이브리드 recall@10 0.901, 해싱 임베더 기본값 `graph-memory.md:361,409`), (B) 위
서명을 비교하는 규칙 레이어. 태스크 acceptance 가 요구하는 비교:

| 신호 | 잡는 것 | 오탐 | 미탐 | 비용 |
|---|---|---|---|---|
| **(A) `query_related` 단독** | 다른 단어로 쓴 같은 절차; 태스크 md 밖(에피소드·노트)까지 | **제목·어휘가 닮은 무관 작업**과 **그래프 edge 만 공유하는 무관 작업**. 실측 — 이 leaf 의 `related.md`(2026-11-16, hybrid, 8건): 다른 프로젝트의 설계 제안 노트가 vector 0.46 으로, wikilink 1 hop 으로만 이어진 엔진 기능 태스크가 vector 0.00 / graph 0.50 으로 걸렸다. 둘 다 이 leaf 와 절차가 다르다 | 임베딩이 없어 BFS-only 로 내려가면(`related_context.py:34-43` fail-open) 어휘가 다른 같은 절차를 놓친다. 문서 단위라 "몇 번 성공했나"를 모른다 | 이미 매 플래너 틱에 계산(`planner_tick.sh:298`); 질의당 수 ms(`graph-memory.md:361`) |
| **(B) 규칙 레이어 단독** | 서명이 같은 런 — 결정적·$0·설명 가능("같은 repo, 같은 touches 모양") | 서명이 거칠면 **리포만 같은 무관 작업**(한 리포의 코드 수정 태스크가 `repo` 하나로 뭉친다) → 그래서 단독으로는 **전체 서명 일치**를 요구해야 한다 | 그 엄격함의 대가: 태그·touches 를 다르게 적은 같은 절차, touches 를 생략한 태스크 | frontmatter 파싱뿐 |
| **★ (C) A → B 캐스케이드** | A 가 recall 로 넓게 모은 이웃 중 서명이 **부분 일치**하는 런 — `repo` 일치 **그리고** (`touches_shape` 일치 **또는** `tags` 교집합 ≥ 2) **그리고** 같은 `pipeline` | 가장 낮다 — A 의 제목 유사·그래프 인접 오탐을 B 의 `repo`·모양 일치가 떨어낸다(위 실측 두 건 모두 탈락: 하나는 런이 아닌 노트, 하나는 touches 모양이 겹치지 않고 분류 태그를 뺀 교집합이 1개다) | B 단독보다 적다 — A 가 앞단에서 의미 유사를 보증하므로 B 를 전체 일치에서 **부분 일치로 낮출 수 있다** | A + B, 둘 다 기존 계산 재사용 |

**★ 결론: (C) A → B 캐스케이드를 쓴다.** 오탐을 줄이는 쪽은 C 다 — A 의 오탐(제목·어휘 유사, 그래프
인접)은 B 의 `repo`·`touches_shape` 일치가 떨어내고, B 를 단독으로 쓸 때 필요한 엄격한 전체 서명 일치는
A 가 앞단에 있어 부분 일치로 낮출 수 있어 B 단독의 미탐을 되찾는다. 한 번뿐인 결정형 태스크와 리포만
같은 무관 작업은 §2.3 규칙이 마저 거른다. `lineage` 가 같으면(회차 체인·같은 부모의 leaf) A 를 건너뛰고
B 만으로 같은 유형이다 — 사람이 이미 이어 둔 관계가 유사도보다 강한 증거다.

양성 사례 둘(일반형):

- **회차 체인** — 같은 `<topic>` 접두 슬러그, 같은 런북 노트 wikilink, `not_before` 로 이어진 delegate
  회차들. 사람이 손으로 만든 "스킬 + 루틴"이라 가장 선명한 양성이고, `lineage` 만으로 확정된다.
- **설계 문서 묶음** — 이 리포의 `docs/design/*.md` 를 같은 §-구조(현황 전수표 → … → 분할안, leaf a/b 손
  분할)로 쓰는 태스크들. 실측 — 같은 `related.md` 에서 자매 설계(채팅 채널 진입)의 leaf 둘과 그 부모가
  vector 0.47~0.49 로, 자매 설계 런의 에피소드 1건이 vector 0.46 으로 걸렸고, 서명(같은 repo·touches 모양
  `docs/design/*.md`·같은 `pipeline`)도 일치해 C 를 통과한다.

### 2.2 승격 후보가 되는 기준 + 데이터 소스

**기본 임계**(구현 leaf 가 설정값으로 노출; 아래는 기본값만 정한다):

- 한 서명 클러스터에서 **연속 성공 런 ≥ 3**, 그중 **서로 다른 태스크 slug ≥ 2**(한 slug 의 재시도·재디스패치는
  1로 센다 — 같은 태스크를 세 번 돌린 것은 반복 절차가 아니다).
- 창은 **최근 90일**. 실패 런이 끼면 연속 카운터가 0으로 돌아간다 — 마지막 성공 뒤에 실패가 있으면 후보가 아니다.
- "성공" = 아래 표의 **검증 통과 · 재시도 없음 · 사람 수정 없음 · 머지(또는 done)** 를 **모두** 만족한 런.

| 판정 | 필드 | 소스 | git 추적? | 한계 |
|---|---|---|---|---|
| **검증 통과** (5스테이지 런) | 에피소드 절의 `- run_id:`·`- overall: pass`. frontmatter `status:` 는 그날 **첫 런** 값이다(같은 날 재실행은 절을 덧붙인다 `worker-report.md:125,137,142-145`) | `Memory/episodes/<date>-worker-<slug>.md` | 추적·영속 → **1차 소스** | 산문 템플릿이라 줄 모양이 흔들릴 수 있다 → 자매 run-event-stream 의 엔진 생성 `## 런 통계` 블록(verdict·`review_required`·`merged`·`no_op`)이 생기면 그것으로 바꾼다(`run-event-stream.md:482-499`, 소프트 의존). 보조: `Worker/runs/<run-id>/verify.md` `overall_status` — 호스트 로컬이라 같은 호스트에서만 |
| **완료** (delegate·quick 런) | 태스크 md `status: done` + 원장 `state: done` | `Todo/items/<slug>.md` · `Worker/assignments/<slug>.json` | 추적 | verify 스테이지가 없어 "검증"이 아니라 "완료"만 안다. 회차 체인은 회차마다 slug 가 달라 원장 행이 덮이지 않는다(장점). 엔진 `run_stats.json` 이 생기면 보강(`run-event-stream.md:498-499`) |
| **재시도 없음** | 원장 `refail_count == 0`(재디스패치에도 이월된다 `assignment.py:101-103`). `attempts`·`history` 는 **쓰지 않는다** — 재디스패치 때 리셋된다(§1.1 원장 행 실물). `capacity_limited`·`capacity_retry_count`·`job_running`·`checkout_busy` 는 **무시** — 태스크 결함이 아니다(`:110-133`) | 원장 | 추적 | 원장은 **현재 상태**다 — 행이 교체되면 이전 런의 세부가 사라지므로 런별 역사는 에피소드가 정본 |
| **사람 수정 없음** | ① 원장 `blocked_on` 공백(`:134`)·`parked_worker_mode` 공백(`:165`), 현재 행 `history[]` 에 `awaiting_human`·`needs_input` 전이 없음(`:207,227`) ② 태스크 md 에 `auto_rescoped`·`is_parent` 없음 — rescope 를 거친 부모 제외(`rescope.py:635-646`) ③ 그 slug 의 `Worker/proposals/<slug>/` 없음 — 프롬프트 제안이 필요했다면 절차가 자족적이지 않았다 ④ 머지된 PR 에 워커 밖 작성자의 커밋 없음 | ①② 원장·태스크 md, ③ 볼트 트리, ④ `gh` | ①②③ 추적, ④ 미기록 | ① 은 교체 전 런의 `awaiting_human` 경유를 못 본다. ④ 는 지금 어디에도 기록되지 않는다(미확인 — 구현 leaf 가 `gh` 조회 비용과 함께 정한다). `worker_mode_recommendation: review`(`worker-report.md:93-100`)는 호스트 로컬 report.md 에만 남아 보조 신호 |
| **머지** | 에피소드 `- pr:` 줄 + 태스크 `status: done` | 에피소드·태스크 md | 추적 | report.md 의 `merged:` 는 호스트 로컬 |

### 2.3 오탐을 줄이는 규칙

1. **최소 런·최소 slug**: 연속 성공 ≥ 3, 서로 다른 slug ≥ 2(§2.2). 한 번뿐인 일은 정의상 후보가 아니다.
2. **결정은 절차가 아니다**: `decision` 태그·`needs_decision: true`·`worker_mode: manual`(사람 담당) 태스크의 런은 세지
   않는다.
3. **재시도는 1회로**: 한 slug 의 재시도·재디스패치·`resumed-scheduled` 재큐잉(`assignment.py:45-61`)은 런 1회로 센다.
4. **리포를 건너는 반복은 더 엄격하게**: `repo` 가 다른 런을 한 클러스터로 묶으려면 `tags` 교집합 ≥ 2 **와**
   `touches_shape` 일치를 **함께** 요구한다 — 앱 리포들 사이의 공통 절차(같은 빌드·배포 순서)는 허용하고, 어휘만
   닮은 무관 작업은 막는다.
5. **안정 id 로 중복 제거**: 후보 id = 서명의 안정 해시. 같은 클러스터를 다음 틱에 다시 감지해도 같은 후보다
   (follow-up id 를 내용 해시로 만드는 선례 `followups.py:184`, discovery 리포트 stem 키 `discovery.py:418`).
6. **거절은 기억한다**: 플래너·사람이 거절한 후보는 rejected jsonl 로 억제하고, 아무도 처리하지 않은 후보는 N 회
   주입 뒤 만료한다(`discovery_rejected.jsonl`·`MAX_INJECTIONS = 3` 선례 `discovery.py:57,65,562-563`). 거절 뒤 서명이
   달라질 만큼 새 런이 쌓이면 새 id 로 다시 떠오른다.
7. **서명에 `allowed_tools` 는 넣지 않는다**: 도구가 같다고 절차가 같지 않고, 스킬은 도구 규칙을 나르지 않는다
   (§3.2 c, 상세는 §4).
8. **임계 미달은 보이지 않는다**: 후보 기록은 호스트 로컬에만 두고, 사람·플래너에게는 임계를 넘은 후보만 보인다
   — 노이즈가 사람의 주의를 먹지 않게.

### 2.4 감지·제안 주체 — 엔진 감지기(플래너 틱) + 플래너가 초안

**결론: 감지는 엔진 모듈이 플래너 틱에서 결정적으로 하고, 절차 초안은 플래너가 쓴다.** 근거(배제 순서):

- **Research·Plan 은 주체가 될 수 없다** — `STAGE_TOOLS` 가 `"Read Write"`(`stages.py:376-377`)이고
  `TASK_TOOLS_STAGES = ("implement",)`(`:446`)라 태스크가 넓힐 수도 없다. `related_context.py:3-9` 가 내린 결론과 같다.
- **report seam 은 한 런만 본다** — `followups.append_followups`(`followups.py:223`) 같은 "런 종료 시 파싱"은 그 런의
  다음 할 일을 뽑는 자리지 교차 런 집계가 아니다.
- **maintenance 틱**(`todo_system.worker maintenance`, 5분 `scripts/systemd/gen_units.py:213-216`)과 todo-sync 색인
  사이클(`graph-memory.md:72`)은 LLM 이 없어 절차 초안을 쓸 수 없다.
- **플래너 틱이 맞는 자리다** — 이미 엔진 `emit-context` 블록 다섯을 붙이고(`planner_tick.sh:142,248,259,277,298`),
  플래너는 `Read Write Edit Glob Grep Bash`(`:357,367`)로 파일을 쓰며, `from_followup`·`from_discovery` 로 "후보 →
  파일 또는 rejected jsonl"을 이미 한다(`worker-planner.md:299,351`).

그래서 두 조각:

- **(i) 감지 = 엔진 모듈**(가칭 `skill_candidates`, 신설) — `emit-context` 가족의 **여섯째**(related_context 가
  다섯째 `related_context.py:24-26`). 틱마다 §2.1 캐스케이드 + §2.2 임계를 돌려 host-local 후보 기록
  `Worker/_state/skill_candidates.json`(`discovery_state.json` 동형 `discovery.py:56`)을 갱신하고, 임계를 넘은 후보만
  프롬프트 블록으로 낸다. 블록은 **메타데이터만** — 후보 id·서명·근거 `run_id`·slug·에피소드 경로. 본문 발췌는 없다
  (R5, `related_context.py:28-32`). 언제나 exit 0, 실패하면 아무것도 출력하지 않는다(가족 규약 `:24-26`).
- **(ii) 초안 = 플래너** — 후보마다 `Worker/skills/<id>.md` 를 `status: proposed` 로 쓰고 `from_candidate: <id>` 를
  찍거나(§3.1), `Worker/_state/skill_candidates_rejected.jsonl` 에 `{"id": …, "reason": …}` 를 남긴다. reconcile 이
  처리된 후보를 닫는다(discovery 의 `from_discovery` reconcile 과 동형). 이것은 플래너의 "적재" 역할 안이다 —
  코드가 아니라 **기록**을 쓴다. 다만 스킬 본문이 플래너 정책 파일·플래너 가드레일·`auto_merge`/승인 게이트 완화
  (볼트 `Worker/planner_policy.yaml` 의 `self_guardrail_modification`)를 담을 수 없음은 §4 가 못박는다.

후보 기록 스키마(합성 예시):

```json
{"candidate_id": "sc-<sha1-12>",
 "signature": {"repo": "<owner>/<repo>", "tags": ["<tag-a>", "<tag-b>"],
               "touches_shape": ["docs/design/*.md"], "pipeline": "full", "lineage": null},
 "runs": [{"run_id": "<YYYYMMDDTHHMMSS>-<slug-1>", "slug": "<slug-1>",
           "episode": "Memory/episodes/<date>-worker-<slug-1>.md", "status": "pass"}],
 "first_seen": "<ISO>", "last_seen": "<ISO>", "injections": 0,
 "state": "pending | proposed | rejected | expired"}
```

---

## §3 사다리 세 단 — 제안 · 스킬 · 루틴

<!-- phase-3: §3 본문 -->

---

## §4 안전

> 다음 leaf에서 채움 — 형제 leaf `macp-skill-promotion-ladder-design-b-safety-metrics-split`(`depends_on` 이 leaf, `on_unblock: auto`)가 이 절을 제자리에서 채운다.

---

## §5 효과 측정

> 다음 leaf에서 채움 — 형제 leaf `macp-skill-promotion-ladder-design-b-safety-metrics-split`(`depends_on` 이 leaf, `on_unblock: auto`)가 이 절을 제자리에서 채운다.

---

## §6 단계별 구현 분할안

> 다음 leaf에서 채움 — 형제 leaf `macp-skill-promotion-ladder-design-b-safety-metrics-split`(`depends_on` 이 leaf, `on_unblock: auto`)가 이 절을 제자리에서 채운다.

---

## 부록: 이 노트가 확정한 설계 결정 요약

> 다음 leaf에서 채움 — 형제 leaf `macp-skill-promotion-ladder-design-b-safety-metrics-split`(`depends_on` 이 leaf, `on_unblock: auto`)가 이 절을 제자리에서 채운다.
