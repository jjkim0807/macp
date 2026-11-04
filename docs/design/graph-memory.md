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

메모리 v2 가 충족해야 할 다섯 축. 각 요구는 §1 의 한계를 번호로 되짚고(해소 대상),
§4 권장안·§5 지표가 이 번호를 참조한다.

### R1 — 엔티티·관계·사건 + bi-temporal (사건 시각 · 기록 시각)

지금은 **관계(edge)만 1급**이고 엔티티는 md 파일, 사건은 edge 의 부산물이다. v2 는 셋을
명시적 레코드로 승격한다:

- **entity** — 사람·기관·개념·시스템(현 `Memory/entities/` 카드에 대응) + 임베딩(R3).
- **relation(edge)** — typed, bi-temporal, provenance(R2) 를 단 관계.
- **event/episode** — 시점이 있는 사건(episode·session·워커 run 결과) + 임베딩(R3).

**bi-temporal 이 핵심 요구**다. 현 edges 테이블은 `event_date`·`recorded_at`·`valid_until`
**열을 이미 가진다**(`graph_memory.py:156-159`) — 문제는 그것이 "재빌드 부산물"이라
질의에 못 쓰인다는 것(§1 한계 ①③). v2 는 이 두 시간축을 **질의 가능한 1급 축**으로 승격한다:

- **valid time = `event_date`**: 사건이 실제로 일어난 시각(결정이 내려진 날, 실패가 난 run 날짜).
- **transaction time = `recorded_at`**: 그 사실이 메모리에 적힌 시각.
- **`valid_until`**(현재 항상 NULL — `:256`): 사실이 번복·만료된 시각. bi-temporal 의
  "언제까지 유효했나".

따라서 v2 는 **append + tombstone** 모델이다 — 사실은 insert 되고, 번복은 행을 DELETE 하는 게
아니라 `valid_until` 을 채운다(§1 한계 ① "역사 없음"의 직접 해소). "2026-09 시점에 유효했던
결정", "이후 번복된 사실" 같은 시간축 질의가 가능해야 한다.

### R2 — 출처(provenance): 어느 run · 커밋에서 왔나

각 사실(entity 속성·edge·event)은 **어디서 왔는지**를 달고 저장된다. 현 `source_file`
(`graph_memory.py:155`, md 파일 경로)을 넘어 **`run_id` + commit sha** 를 함께 보존한다 —
[[docs/design/state-store|state-store.md]] 의 ledger run/커밋 메타(`Worker/assignments/`,
`Worker/runs/<run-id>/`)와 정합. 효용: "이 사실은 믿을 만한가"를 출처로 판정(어느 워커 run 의
측정인지, 어느 커밋에서 확정됐는지), R1 의 `valid_until` 과 결합하면 "무엇이 언제 어느 run 에서
뒤집혔나"가 나온다. **출처는 식별자가 아닌 run/commit 참조로만** 적는다(R5 개인정보 경계와 정합).

### R3 — 의미 검색(임베딩) + 그래프 탐색 결합 (하이브리드)

§1 한계 ②(문자열 매칭뿐)의 해소. v2 질의는 **두 단계 하이브리드**다:

1. **의미(semantic)**: 질의 텍스트를 임베딩해 최근접 entity/episode `k`개를 찾는다 — 정확한
   슬러그·wikilink 가 없어도 "의미가 비슷한 과거 실패"에 닿는다.
2. **그래프(structural)**: 1단계가 찾은 노드에서 현 `get_related` BFS(`:328-356`) 류의 탐색으로
   연결된 결정·실패·의존 태스크를 확장한다.

임베딩만으로는 "왜 연결됐나"(의존·번복·부모)를 잃고, 그래프만으로는 §1 한계 ②에 걸린다 —
**둘의 결합**이 요구다. 구체 임베딩 모델·벡터 저장소·그래프 탐색 엔진은 §3 이 공식 문서 URL 과
함께 결론 낸다.

### R4 — 워커 스테이지의 "관련 과거 실패·결정" 질의

§1.2 에서 보았듯 지금 "이 태스크와 관련된 과거 기록"을 실제로 질의하는 스테이지는 **Research
하나뿐**이고, 그나마 `Memory/index.md` 라는 **집계 스냅샷**(top-10·최근 14일 목록)을 읽는
수준이다. v2 요구: Research/Plan 스테이지가 **태스크 slug·제목·`touches`** 로 질의하면 관련
과거 **episode(실패 회고)** 와 **decision(결정)** 을 적중률 높게 반환한다. 근거: episodes 에 이미
`worker-*` 회고가 쌓이고 있다(index.md:71-90 최근 14일만도 워커 episode 다수). 효용 = 워커가 같은
실패를 반복하지 않는 것. **이 질의의 적중률이 §5 평가 지표의 1차 대상**이다.

### R5 — 개인정보 경계 (SMS · 메일 본문 · 건강 원자료는 넣지 않는다)

메모리 v2 는 **사실·관계·결정·실패 요약만** 담는다. 다음은 경계 밖:

- **SMS/메일 본문·인증번호**: CLAUDE.md §11·§11b — "볼트나 외부로 옮겨 적지 않는다". 메모리
  v2 도 저장·임베딩 대상이 아니다.
- **건강 원자료**: 진료·수치 원본은 넣지 않는다.
- **임베딩 입력에도 동일 경계**: 임베딩 벡터는 원문을 부분 복원할 여지가 있으므로, 경계 밖
  데이터는 **임베딩 입력에서도 제외**한다(요약·메타만 벡터화).
- 공개 리포(`jjkim0807/macp`) 설계라 스키마 예시·테스트 픽스처에 **실명·토큰·엔드포인트 host·
  절대 홈경로**를 넣지 않는다(§6 식별자 자가감사의 대상).

---

## §3 저장소·접근 경로 비교표 (공식 문서 URL 포함)

메모리 v2 는 (a) **벡터/임베딩**(R3), (b) **그래프 탐색**(R3), (c) **bi-temporal + provenance**
(R1·R2) 를 한 저장소에서 지원해야 하고, (d) [[docs/design/state-store|state-store.md]] 의
external SQLite·⑦ 원격 상태 API 와 **같은 서버로 질의**를 낼 수 있어야 한다(열림질문 해소,
§3.4). 후보를 임베디드/서버형으로 나눠 비교한다.

### 3.1 공식 문서 URL (WebFetch 2026-11-04 확인; 못 연 것은 '미확인')

| 후보 / 참조 | 종류 | 공식 문서 URL | 확인 |
|---|---|---|---|
| SQLite 재귀 CTE (현행) | 임베디드 질의 | `https://www.sqlite.org/lang_with.html` | 확인 (stable; 재귀 CTE 로 트리/그래프 질의, 복합 재귀 3.34.0+) |
| sqlite-vec | 임베디드 벡터 확장 | `https://github.com/asg017/sqlite-vec` | 확인 (**pre-v1 — breaking changes 주의**; `vec0` 가상테이블, float/int8/binary, sqlite-vss 후속, Apache-2.0/MIT) |
| DuckDB VSS | 임베디드 벡터 확장 | `https://duckdb.org/docs/stable/core_extensions/vss` (정본 `…/docs/2.0/core_extensions/vss.html`) | 확인 (**experimental**; HNSW on ARRAY, 지속성은 experimental 플래그 뒤 — 미커밋 종료 시 손상 경고) |
| Kùzu | 임베디드 그래프 DB | `https://docs.kuzudb.com` | **미확인** (DNS ENOTFOUND — 호스트 미해결; §3.2 기각 사유) |
| pgvector | 서버(Postgres) 벡터 | `https://github.com/pgvector/pgvector` | 확인 (v0.8.x, Postgres 13+, HNSW·IVFFlat, 벡터는 일반 PG 테이블) |
| Apache AGE | 서버(Postgres) 그래프 | `https://age.apache.org/` | 확인 (활성 ASF 프로젝트, PG16 호환, Cypher on Postgres) |
| Neo4j | 서버 그래프 DB | `https://neo4j.com/docs` | 확인 (활성 상용, Cypher·GDS·네이티브 벡터) |
| **Graphiti/Zep** (선행연구) | 시간축 KG 프레임워크 | `https://github.com/getzep/graphiti` · 논문 `https://arxiv.org/abs/2501.13956` | 확인 (Apache-2.0; validity window+provenance = R1·R2 와 동형; 백엔드 Neo4j/FalkorDB/Neptune·**Kuzu deprecated**; 논문 DMR 94.8% vs MemGPT 93.4%) |
| **Letta** (구 MemGPT, 선행연구) | 에이전트 메모리 프레임워크 | `https://docs.letta.com/` · MemGPT 논문 `arXiv:2310.08560` | docs.letta.com 확인 (core/archival/recall 계층; pip 기본 SQLite+Chroma, Docker 기본 Postgres+pgvector) · 논문 arXiv id **미확인(미열람)** |

### 3.2 비교표 (비용 · 운영 · 원격질의)

**비용 열** = 본부 VM(macp-hq) **디스크·메모리** 증가와 **새 유료 인프라** 여부. 본부 VM 자체는
어느 후보든 상주하므로 공통 비용에서 뺀다. 제약: 상시 외부 SaaS DB(Supabase·Neon) **금지**
([[Decisions/2026-09-25-backend-unify-gcp-no-supabase]]), 유료는 GCP Cloud SQL 만·사람 게이트
(월 ~$10~25, [[Decisions/2026-10-13-macp-task-store]]).

| 후보 | 임베/서버 | 비용(디스크·메모리 / 새 유료 인프라) | 운영 부담 | 임베딩(벡터) | 그래프 탐색 | **external SQLite·⑦ API 와 같은 서버 질의?** |
|---|---|---|---|---|---|---|
| SQLite + 재귀 CTE (현행) | 임베디드 | 디스크 微·메모리 微 / **$0** | 최저(이미 운영) | **없음**(§1 한계②) | 재귀 CTE(느림) | **예** — `state.sqlite` 와 같은 파일/프로세스 |
| **SQLite + sqlite-vec** ★ | 임베디드 | 디스크 小(벡터)·메모리 小 / **$0** | 낮음(확장 1개 load) | `vec0` KNN | 재귀 CTE | **예** — `state.sqlite` 와 동일 파일(ATTACH/동일 커넥션), ⑦ (a) HTTP API 가 그대로 노출 |
| DuckDB + VSS | 임베디드 | 디스크 中·메모리 中(OLAP) / **$0** | 중(VSS 지속성 experimental — 손상 경고) | HNSW(ARRAY) | 재귀 CTE | 부분 — 별 엔진·별 파일, ⑦ API 로 노출하려면 별도 배선 |
| ~~Kùzu~~ (기각) | 임베디드 | — / **$0** | — | 네이티브 | Cypher | — (아래 기각 사유) |
| Postgres + pgvector (+AGE) | 서버 | 본부 self-host $0·메모리 中 / Cloud SQL **~$10~25** | 높음(DB 상주·IAM·백업·마이그레이션) | HNSW·IVFFlat | AGE Cypher | ⑦ (b) Cloud SQL 경로와 정합 — 단 **상시 DB**(배제 방침·사람 게이트) |
| Neo4j | 서버 | 메모리 大(JVM) / self-host $0·관리형 유료 | 높음(별 서버 프로세스·힙 튜닝) | 네이티브 벡터 | Cypher(네이티브, 강력) | **아니오** — external SQLite 와 다른 서버 |

★ = §4 권장 방향. **Kùzu 기각 사유**: 공식 docs 호스트가 DNS 로 해결되지 않고(미확인),
Graphiti 요구사항 목록이 **"Kuzu 0.11.2 deprecated"** 로 명시한다. 임베디드 그래프 DB 로 기능은
매력적이나 유지보수 신호가 끊긴 의존성을 메모리 SoT 로 삼을 수 없다.

### 3.3 선행 연구 대조 (저장소가 아니라 참조 아키텍처)

- **Graphiti/Zep** (확인): 시간축 지식그래프 기반 에이전트 메모리. edge 에 **validity window
  (`valid_at`/`invalid_at`) + provenance** 를 다는 모델이 본 설계의 **R1(bi-temporal) + R2(출처)
  와 정확히 같다** — 즉 v2 의 데이터 모델은 이미 검증된 방향이다. 논문(arXiv 2501.13956)은
  Deep Memory Retrieval 벤치에서 MemGPT 대비 우위(94.8% vs 93.4%)를 보고하며, 이는 §5 가
  적중률을 1급 지표로 삼는 근거다. **단 Graphiti 의 기본 백엔드는 Neo4j/FalkorDB(서버형)** 라
  "본부 단일 SoT·$0·오프라인 내성" 우선순위와는 운영 트레이드오프가 있다 → 데이터 모델은 차용,
  저장소는 external SQLite 로 간다(§4).
- **Letta/MemGPT** (docs 확인): core/archival/recall **계층형 메모리** + 임베딩 retrieval.
  기본 저장소가 pip=SQLite+Chroma, Docker=Postgres+pgvector 라는 점은 후보(SQLite+벡터 /
  Postgres+pgvector)와 정합 — "임베디드 SQLite+벡터가 에이전트 메모리의 실무 기본값"이라는
  방증이다.

### 3.4 열림질문 해소 — 별도 저장소 신설하지 않고 external 축에 올라탄다

요구(태스크 §5·Research 열림질문)대로 **메모리 v2 는 병렬 저장소를 신설하지 않는다**:

1. **같은 백엔드**: 메모리 v2 는 [[docs/design/state-store|state-store.md]] §③ external 모드의
   볼트 밖 호스트 SQLite(`~/.local/share/macp/state.sqlite`)와 **같은 파일/엔진**에 산다 —
   sqlite-vec 확장을 load 하고 벡터·메모리 테이블을 그 DB 에 둔다(또는 `memory.sqlite` 를 같은
   디렉토리에 두고 `ATTACH`). 관계 그래프는 **현 edges 테이블을 그대로 승격**(재귀 CTE 탐색),
   벡터만 `vec0` 가상테이블로 추가한다. git 모드에서는 현 `Memory/relations.sqlite` 가 베이스라인.
2. **같은 서버 원격 질의**: 원격 러너의 질의는 state-store.md §⑧ 의 권장 **⑦ (a) 본부 HTTP 상태
   API + 토큰** 을 통해 낸다 — 상태 CRUD 엔드포인트에 **메모리 질의 엔드포인트**(hybrid search·
   get_related)를 더하면 끝이고, **새 네트워크 서비스·새 유료 인프라가 필요 없다**. SQLite SoT 는
   본부 한 곳(단일 SoT), 원격은 HTTP 로 읽는다. (상시 외부 DB 가 필요해지면 ⑦ (b) Cloud SQL —
   사람 게이트.)
3. **임베딩 생성 경로**(열림질문): **본부 VM 로컬 임베딩 모델**을 1차 권장한다 — 새 유료 API
   호출 **$0**, 외부 전송 없음(R5 PII 경계와 정합: 민감 원문은 애초에 경계 밖이고, 요약/메타만
   로컬에서 벡터화), 오프라인 내성. 관리형 임베딩 API(예: Vertex text-embedding)는 로컬 모델의
   적중률이 §5 지표에서 부족할 때 가는 **사람 게이트 에스컬레이션**으로 둔다. 정확한 로컬 모델·
   차원·양자화는 **구현 leaf 가 §5 벤치로 선택**(미확인 — 지금 단정하지 않는다). 디스크·메모리
   증가는 "작은 문장 임베딩 모델 1개 + 벡터 인덱스" 수준으로, 본부 VM 상주 자원 안에서 흡수.

---

## §4 권장안 + 단계별 구현 분할안

*(Phase 4 에서 채움)*

---

## §5 평가 방법 (지표)

*(Phase 5 에서 채움)*
