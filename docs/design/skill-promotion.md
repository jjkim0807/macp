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

<!-- phase-2: §2 본문 -->

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
