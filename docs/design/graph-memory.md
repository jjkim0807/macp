---
title: 그래프 메모리 v2 — 설계 노트 (task-store external 축 위에 올라타는 메모리)
status: draft
created: 2026-11-04
related:
  - Todo/items/macp-graph-memory-v2-design.md            # 이 태스크
  - Todo/items/macp-task-store-external-backend-mode.md  # 부모 태스크(external 모드 = 기반)
  - docs/design/state-store.md                           # 자매 설계 leaf ①(external SQLite·⑦ 원격 상태 API)
  - Decisions/2026-10-13-macp-task-store.md              # md 정본·외부 상시 DB 배제
  - Decisions/2026-09-25-backend-unify-gcp-no-supabase   # Supabase·Neon 제외
---

# 그래프 메모리 v2 — 설계 노트

> **이 노트의 성격**: 분석·설계 전용. 이 태스크는 코드를 한 줄도 바꾸지 않는다 —
> `docs/design/graph-memory.md` 한 파일만 추가한다. 실제 구현은 §4 분할안의 후속
> leaf 가, 설계 머지 뒤 **사람이 게이트를 풀 때** 맡는다. 본문의 `파일:줄` 앵커는 전부
> 이 리포(`jjkim0807/macp`) HEAD 트리를 **읽어** 확인한 것이고, 확인하지 못한 지점은
> '미확인'으로 남겼다. §3 의 외부 저장소 공식 문서 URL 도 같은 규율 — WebFetch 로
> **열어 확인**한 것만 '확인', 못 연 것은 '미확인'.

## 왜 이 문서인가

사용자 의도(2026-11-04, 태스크 Spec): *"Task memory 같은 걸 그래프 DB 메모리 등 더
고차원적인 기술이 들어간 것으로 발전시키기 위함도 있다. 단순 git으로만 진행하면 계속
토이프로젝트 수준에 머문다."* 지금의 `todo_system/graph_memory.py`(M3-a)는 md 위키링크·
frontmatter 관계를 `Memory/relations.sqlite` 로 **매 틱 재빌드**하는 색인기다 — 의미 검색도,
시간축 질의도, 원격 접근도 없다. 이 노트는 그걸 넘어서는 **메모리 v2** — 엔티티·관계·사건을
bi-temporal(사건 시각 + 기록 시각)과 출처(어느 run·커밋)와 함께 담고, 의미 검색(임베딩)과
그래프 탐색을 결합하며, brief·planner·워커 스테이지·원격 러너의 질의 경로를 규정하는
그래프 메모리 — 를 설계한다.

**핵심 전제**: 메모리 v2 는 **별도의 병렬 저장소를 신설하지 않는다.** 자매 설계
[[docs/design/state-store|state-store.md]] 가 규정한 external 모드(볼트 밖 호스트 SQLite
`~/.local/share/macp/state.sqlite`)와 ⑦ 원격 상태 API 위에 **올라탄다**. §3 이 이 전제를
저장소·원격 질의 양면에서 명시적으로 답하고, §4 가 external 축(부모 체인)을 선행 의존으로
묶는다.

---

## §1 현황 전수표 — 무엇이 들어가나 · 누가 읽나 · 지금의 한계

### 1.1 무엇이 메모리에 들어가나

범례 — **SoT?**: 진실의 원천(편집·권위 소스)인가, 파생/재생성인가. **git 추적?**: 볼트
리포(`MaestroACP`)의 `.gitignore` 기준. **색인/노출 경로**: 엔진 리포(`jjkim0807/macp`)
HEAD 기준 `파일:줄`.

| 데이터 | 경로(볼트) | SoT? | 수명 | git 추적? | 색인/노출 경로 (`파일:줄`) |
|---|---|---|---|---|---|
| **엔티티 카드** | `Memory/entities/<name>.md` | **SoT** (사람·기관·개념·시스템 카드, 사실 누적·모순 보존) | 영속 | 추적 | SCAN_ROOTS 포함 `graph_memory.py:56-62` → wikilink/frontmatter edge(`:261-282`); index.md "핵심 entity top 10"(`:613-619`, `most_referenced_slugs`:359-370) |
| **에피소드** | `Memory/episodes/<YYYY-MM-DD>-<slug>.md` | **SoT** (brief·worker 가 매일 append; discovery 리포트도 여기 — `discovery.py:16`) | 영속(append-only) | 추적 | SCAN_ROOTS 포함; index.md "최근 14일 episodes"(`:631-637`, `recent_episodes`:373-390) |
| **수동 세션 저널** | `Memory/sessions/<YYYY-MM-DD>-<slug>.md` | **SoT** (`session_log` 체크포인트) | 영속 | 추적 | **SCAN_ROOTS 밖** — edge 색인 **안 됨**. index.md 리스트에만(`:639-651`, `recent_sessions`:393-410) |
| **결정(ADR)** | `Decisions/<date>-<slug>.md` | **SoT** | 영속 | 추적 | SCAN_ROOTS 포함; `active_decisions`(`decision_status: accepted`)만 index.md "활성 의사결정"(`:551-565`, `:621-629`) |
| **태스크 링크** | `Todo/items/<slug>.md` frontmatter+본문 | **SoT** (편집 소스) | 영속 | 추적 ([[Decisions/2026-09-10-macp-cloud-hq-vault-sot]]) | SCAN_ROOTS 포함; `depends_on`/`parent`/`supersedes`/`related_*`/`affects_tasks` + 본문 `[[wikilink]]` → typed edge(`:38-53`, `:261-282`) |
| **노트** | `Notes/*.md` | SoT | 영속 | 추적 | SCAN_ROOTS 포함; 본문 wikilink edge |
| **관계 색인** | `Memory/relations.sqlite` (edges 테이블) | **아니오** — md 에서 매 틱 재빌드되는 **파생 캐시** | 재생성 가능 | **비추적** | 스키마 `:150-160`; 재빌드 `scan_vault_and_index`:285-320 |

**edges 스키마**(`graph_memory.py:150-160`): `id · from_slug · to_slug · edge_type ·
source_file · event_date · recorded_at · valid_until · confidence`. 현 edge 분포(index.md:14-23,
2026-11-04 기준 total 8055): `wikilink` 6266 · `parent` 1039 · `depends_on` 325 ·
`related_tasks` 310 · `affects_tasks` 86 · `related_entities` 21 · `supersedes` 5 ·
`related_decisions` 3.

### 1.2 누가 읽나

| 주체 | 무엇을 읽나 | 근거 (`파일:줄`) |
|---|---|---|
| **brief** (매일 아침) | `Memory/index.md` = "매 brief 시작 시 가장 먼저 읽는 진입점"; `Memory/episodes/` 최근 7일 | `.claude/commands/brief.md:17`(step 4)·`:18`(step 5); index.md 머리말 `graph_memory.py:600` |
| **planner** | `Memory/index.md` = active project context; 미반영 discovery 리포트 후보 | `.claude/commands/worker-planner.md:63`(필수 reading 4)·`:346`(step 6); `orchestrator/planner_mode.py:221` |
| **worker Research 스테이지** | `Memory/index.md` (있으면; 없으면 skip) | `.claude/commands/worker-research.md:16`(step 3) |
| **producer**(reader 아님) | todo-sync 데몬이 매 사이클 `scan_vault_and_index`+`render_index_markdown` 로 relations.sqlite·index.md **재생성** | `cli.py:1489-1501` |

> **reader 오분류 방지**: `worker.py:2557` 의 `Memory/index.md` 는 reader 가 **아니다** —
> `ALLOWED_DIRTY_EXACT` 더티체크 면제 항목으로, index.md 를 "데몬이 계속 덮어쓰는 churn"으로
> **무시**하라는 뜻이다. `session_end.py:45,82-132` 도 index.md 를 diff 로만 분류(`last_synced:`
> 한 줄 변경이면 ignorable). 커밋 경로는 `VAULT_SYNC_PATHS`(`worker.py:2579`, `Memory` 포함 →
> episodes/sessions 추적분 push). plan·implement·verify·report 스테이지 프롬프트는 index.md 를
> required-reading 으로 두지 **않는다**(Research 만 둔다) — 즉 "이 태스크와 관련된 과거 실패·결정"을
> 실제로 질의하는 스테이지는 지금 **Research 하나뿐**이고, 그나마 index.md 라는 **집계 스냅샷**을
> 읽는 수준이다(§2 요구 4번의 출발점).

### 1.3 지금의 한계 (메모리 v2 가 넘어서려는 기준선)

① **재빌드형 색인 (역사 없음)**: `scan_vault_and_index`(`:285-320`)가 `clear_edges`
(`:171-173`, `DELETE FROM edges`) 후 매 틱 전체 재스캔한다. 증분이 없고, 8055개 edge 를
통째로 다시 만든다. edge 의 "언제 생겼다 언제 사라졌다"는 재빌드 시점 스냅샷에 지워진다 —
사실의 **역사가 보존되지 않는다**.

② **의미 검색 없음**: edge 는 wikilink/frontmatter **문자열 매칭**뿐이다(`WIKILINK_RE`:64,
typed edge `:261-282`). 임베딩·벡터 최근접이 없어 "의미가 비슷한 과거 실패"를 **못 찾는다** —
정확한 슬러그/링크가 이미 본문에 있어야만 연결된다. 새 태스크 제목이 과거 episode 와 다른
단어를 쓰면 관련성이 0으로 보인다.

③ **시간축 질의 약함**: edges 에 `event_date`/`recorded_at`/`valid_until` **열은 있으나**
(`:156-159`) 질의로 쓰이지 못한다 — 재빌드마다 `recorded_at` 이 "지금"으로 덮이고(`:306`,
`dt.datetime.now()`), `valid_until` 은 항상 NULL 로 삽입되며(`:256`), bi-temporal 질의 API 가
없다. 질의 표면은 `get_related` BFS(`:328-356`)·`most_referenced_slugs`(`:359-370`)뿐이고
둘 다 **시간 필터가 없다**. "2026-09 시점에 유효했던 결정"이나 "이후 번복된 사실" 같은 질의가
불가능하다.

④ **원격 러너 접근 불가**: `relations.sqlite`(`graph_memory.py:34`,
`DB_PATH = MEMORY_DIR/"relations.sqlite"`)·`index.md` 는 볼트 로컬 파일이라 다른 머신의
러너가 열 수 없다. 원격 호스트(Windows/WSL)는 볼트를 git 으로 pull 해 **스냅샷**만 보고, 질의를
낼 수 없다. 이는 [[docs/design/state-store|state-store.md]] §⑧ 이 다룬 "SQLite 파일 핸들은
로컬 — 네트워크 경로가 필요"와 **동형 문제**다.

---

## §2 요구사항

*(Phase 2 에서 채움)*

---

## §3 저장소·접근 경로 비교표 (공식 문서 URL 포함)

*(Phase 3 에서 채움)*

---

## §4 권장안 + 단계별 구현 분할안

*(Phase 4 에서 채움)*

---

## §5 평가 방법 (지표)

*(Phase 5 에서 채움)*
