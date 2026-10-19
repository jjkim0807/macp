---
title: 상태 저장소 외부 백엔드 — 설계 노트 (leaf ①)
status: draft
created: 2026-10-20
related:
  - Todo/items/macp-task-store-external-backend-mode.md  # 부모 태스크
  - Decisions/2026-10-13-macp-task-store.md              # md 정본·외부 상시 DB 배제
  - Decisions/2026-09-25-backend-unify-gcp-no-supabase   # Supabase·Neon 제외
---

# 상태 저장소 외부 백엔드 — 설계 노트

> **이 노트의 성격**: 분석·설계 전용. 이 leaf(①)는 코드를 바꾸지 않는다 —
> `docs/design/state-store.md` 한 파일만 추가한다. 실제 추상화 구현은 후속
> leaf ②~⑥(§6)이 맡는다. `파일:줄` 앵커는 전부 이 리포(`jjkim0807/macp`)의
> HEAD 트리를 **읽어** 확인한 것이고, 확인하지 못한 지점은 '미확인'으로 남겼다.

## 왜 이 문서인가

오케스트레이터의 지속 상태는 오늘 **전부 볼트 git 트리 안이나 그 옆(호스트
로컬 파일)** 에 산다. 둘로 갈린다:

1. **git-추적 상태** — ledger(`Worker/assignments/*.json`), mailbox 메시지
   (`Worker/agents/*/inbox/*.json`), 태스크 소스(`Todo/items/*.md`). 이들은
   커밋돼 클라우드 본부가 상속하지만([[Decisions/2026-09-10-macp-cloud-hq-vault-sot]]),
   바로 그 때문에 **트리를 되돌리는 아무 git 조작**(stage 에이전트의 `git restore`·
   `stash`·`reset --hard`, autostash 실패, 옛 워커 브랜치 체크아웃)이 상태를
   되살리거나 잃게 만든다. `mailbox.py:47-55`의 **ack 틈브스톤 핵**이 바로 그
   흉터다 — ack 가 지운 메시지 파일을 git 이 복원하면 클레임도 `seen` 항목도 없는
   동일 봉투가 "새 메시지"로 보여 파이프라인 전체가 재실행됐다. 이 태스크가
   은퇴시키려는 실패 유형이 이것이다.
2. **git-비추적 호스트 로컬 상태** — `queue.json`·`planner_state.json`·
   `Worker/runs/`·reader 커서(`_state/*/cursor.json`). git 이 안 건드리므로 git
   조작에는 안전하지만, 리포 동기화로 호스트 간 이동하지 않고 클라우드 인수가
   상속하지도 않는다.

목표는 상태를 트리 **밖**으로 옮길 수 있는 선택적 외부 백엔드다: 기본은 지금의
git 모드(무변경), 외부 모드는 볼트 밖 호스트 SQLite(`~/.local/share/macp/state.sqlite`).
external 모드에서는 (1)의 git-조작 취약성이 통째로 사라지고 — 상태가 트리에
없으니 checkout/reset/stash 가 건드릴 게 없다 — 틈브스톤 핵도 필요 없어진다.

---

## ① 상태 데이터 전수 목록

범례 — **SoT?**: 이 데이터가 진실의 원천인가(편집·권위 소스), 아니면 파생/전송인가.
**git 추적?**: 볼트 리포(`MaestroACP`)의 `.gitignore` 기준(이 엔진 리포가 아니라 —
상태 파일은 런타임에 볼트에 산다). **접근 코드 위치**는 엔진 리포(`jjkim0807/macp`)
HEAD 기준 `파일:줄`.

| 상태 데이터 | 현재 경로(볼트) | SoT? | 수명 | git 추적? | 접근 코드 위치 (`파일:줄`) |
|---|---|---|---|---|---|
| 태스크 소스 `Todo/items/*.md` | `Todo/items/<slug>.md` | **SoT** (편집 소스) | 영속(닫혀도 `status: done` 보관) | **추적** (`.gitignore:91` "Todo/items/ IS tracked"; 동기화 경로 `worker.py:2070` `VAULT_SYNC_PATHS`) | `cli.py:26` `ITEMS_DIR`; `worker.py:65` `ITEMS_DIR`(워커가 frontmatter 재파싱, `worker.py:15-17`) |
| todo 캐시 `Todo/todo.db` | `Todo/todo.db` | 아니오 (md의 파생 SQLite 캐시) | 재생성 가능(`cli import-md`) | **비추적** (`.gitignore:61`) | `cli.py:27` `DB_PATH`; `metrics.py:31` `DB` + sqlite 읽기 `metrics.py:45-55` |
| 과제 원장(ledger) `Worker/assignments/<task_id>.json` | `Worker/assignments/` | **SoT** (in-flight 오케스트레이션 상태머신) | terminal까지 활성, 이후 보관 | **추적** (`.gitignore:122` "Worker/assignments/ IS tracked: durable ledger a cloud takeover inherits") | `assignment.py:245` `Ledger`(`save`:252 / `load`:255 / `all`:264 / `active`:275); 데이터클래스 `Assignment` `assignment.py:93`(필드 95–193); 상태 상수 `assignment.py:18-27`; 소비자 `metrics.py:29` `LEDGER` |
| mailbox 메시지 `Worker/agents/<agent>/inbox/<msg>.json` | `Worker/agents/` | 전송(transport) — durable, 멱등 | ack까지(소비=파일 삭제, 커밋은 안 함) | **추적** (`.gitignore:114` "message files ARE tracked (git = the transport)") | `mailbox.py:81` `Mailbox`(`_inbox`:88, `send`:145, `receive`:154, `ack`:172); 팩토리 `open_mailbox`:341; `GitMailbox`:226 / `FileMailbox`:217 |
| reader 커서 + ack 틈브스톤 `Worker/agents/_state/<agent>/cursor.json` | `Worker/agents/_state/` | reader-local 소비집합(`seen`+`acked`) | 런타임(틈브스톤 TTL 14d, cap 20000 — `mailbox.py:54-55`) | **비추적** (`.gitignore:116`) | `mailbox.py:91-92` `_cursor_path`; 틈브스톤 근거·핵 `mailbox.py:47-55`(이 태스크가 은퇴시키려는 git-restore 부활 방어) |
| 실행 큐 `Worker/queue.json` | `Worker/queue.json`(호스트 스코프 `_STATE_DIR`) | 아니오 — **파생 뷰**, ledger가 SoT ([[macp-queue-json-is-derived-view]]) | 런타임(매 틱 변경) | **비추적** (`.gitignore:109` "mutated every tick — tracking breaks branch checkout") | `worker.py:110` `QUEUE_PATH` |
| planner kv `Worker/planner_state.json` | `Worker/planner_state.json`(호스트 스코프) | planner scratch(`verdict`+`todo_state_hash`[+queue]) | 런타임 | **비추적** (`.gitignore:110`) | 경로 `worker.py:111` `PLANNER_STATE_PATH`; 읽기 `load_planner_state` `worker.py:364`; 쓰기 `save_planner_state` `worker.py:377`(write 393–394). **추가 리더**(kv 표면이 흡수해야 함): `worker.py:2332` `PLANNER_STATE_FILE` + `_read_planner_state` `worker.py:2409`; `discovery.py:56` `PLANNER_STATE_PATH`·`planner_verdict` `discovery.py:271`; `project_stall.py:355-360` |
| per-run 산출물 `Worker/runs/<run-id>/` | `Worker/runs/` | run 감사 추적(requirements/plan/verify/run.log 등) | ephemeral | **비추적** (`.gitignore:75` — 디렉토리 전체; `*.log`는 전역 규칙 `.gitignore:25`로도 제외) | `worker.py:109` `RUNS_DIR`; `metrics.py:30` `RUNS` |

**8행**(요구 최소 7행 충족). 추가로 확인한 git-비추적 호스트 로컬 상태(참고, 표에는 생략):
`Worker/sweep_state.json`(`worker.py:112`, `.gitignore:111`),
`Worker/remote_ack.json`(`worker.py:104`, `.gitignore:150`),
틱 하트비트 `Worker/todo_sync_heartbeat`(`worker.py:120`)·runner heartbeat(`worker.py:157`, `.gitignore:162`·`:154`).

### ①의 핵심 관찰 (설계를 이끄는 사실)

- **git-조작 취약성은 '추적' 행에만 있다.** checkout/reset/stash 가 되살리거나
  덮어쓸 수 있는 건 ledger·mailbox 메시지·`Todo/items` md 뿐이다. 비추적 런타임
  파일(queue·planner·runs·커서)은 git 이 손대지 않는다 — 그래서 §⑤ 회귀 테스트의
  "state unchanged" 주장은 **추적 행**(ledger·mailbox)에서만 의미 있는 승리다.
- **틈브스톤 핵(`mailbox.py:47-55`)의 존재 이유 = mailbox 메시지가 추적되는데
  ack-삭제는 커밋되지 않음.** external 백엔드로 메시지를 트리 밖으로 옮기면 삭제가
  곧 영구 소비가 되어 틈브스톤 보정 자체가 불필요해진다.
- **`planner_state.json`은 리더가 최소 넷**(worker 두 상수 + discovery + project_stall).
  kv 표면은 이들을 전부 거쳐가게 하거나, 최소한 전부가 같은 백엔드를 읽도록 배선해야
  한다(§6 leaf ③의 범위).

---

## ② `StateStore` 인터페이스 초안

네 영역(ledger CRUD · mailbox put/take/ack · todo 캐시 · planner kv)을 하나의
`StateStore` 아래 **하위 표면(sub-surface)으로 노출**한다. 시그니처는 전부 현행
구현을 거울상으로 베낀 **초안**이다 — 확정 API 가 아니라 leaf ②가 실제로 쓸 때의
출발점. 각 표면 옆에 거울상이 된 현행 코드 앵커를 달았다.

```python
# docs/design: draft signatures only — leaf ②가 구현.
from abc import ABC, abstractmethod

class StateStore(ABC):
    """오케스트레이터 지속 상태의 단일 진입점. 네 영역을 하위 표면으로 돌려준다."""

    @abstractmethod
    def ledger(self) -> "Ledger": ...          # assignment.py:245 모양 그대로
    @abstractmethod
    def mailbox(self) -> "Mailbox": ...         # mailbox.py:81 ABC — git 모드는 그대로 재사용
    @abstractmethod
    def planner_kv(self) -> "PlannerKV": ...    # worker.py planner_state 모양
    @abstractmethod
    def todo_cache(self) -> "TodoCacheReader": ...  # 파생 — 소유는 선택

# --- 영역 1: ledger CRUD (거울상: assignment.py:245 Ledger) ---
#   save(a: Assignment) -> None        # assignment.py:252
#   load(task_id: str) -> Assignment | None   # :255
#   all() -> list[Assignment]          # :264
#   active(task_id: str) -> Assignment | None # :275
# Assignment 데이터클래스(assignment.py:93, 필드 95–193)와 상태머신(:18-27)은
# 백엔드와 무관한 값 타입이라 그대로 재사용. git 백엔드는 현행 Ledger 를 감싸기만 한다.

# --- 영역 2: mailbox put/take/ack (거울상: mailbox.py:81 Mailbox ABC) ---
#   init_agent(agent_id: str) -> None  # mailbox.py:94
#   send(msg: Envelope) -> Path        # :145   (put)
#   receive(agent_id: str) -> list[Envelope]   # :154   (take, at-least-once)
#   ack(agent_id, msgs) -> None        # :172   (소비)
# ↑ 이미 Mailbox ABC 가 정확히 이 네 메서드 + 틈브스톤/커서 로직을 담고 있고,
#   전송 훅(_before_receive:138 / _after_send:141 / _after_ack:193)이 하위클래스
#   확장점으로 열려 있다. → 아래 '재사용/흡수 결정' 참조.

class PlannerKV(ABC):   # 거울상: worker.py load/save_planner_state (:364/:377)
    @abstractmethod
    def get(self, key: str) -> "Any | None": ...
    @abstractmethod
    def put(self, key: str, value: "Any") -> None: ...
    @abstractmethod
    def all(self) -> dict: ...
# 주의: 현행 save_planner_state(:377)는 dict 전체를 통째로 쓰고(verdict[+queue]),
#   리더가 넷(§①의 관찰)이다. kv 표면은 이 coarse write 를 get/put 로 감싸되,
#   네 리더가 전부 같은 PlannerKV 인스턴스를 거치게 배선하는 게 leaf ③의 일.

class TodoCacheReader(ABC):   # 거울상: metrics._db_counts(:45-55) + cli.DB_PATH(:27)
    @abstractmethod
    def counts(self) -> dict: ...          # done/auto_done/cancelled 등 파생 집계
    # (확장) query(sql, params) 같은 읽기 전용 표면
# todo.db 는 md 의 **파생 캐시**(SoT 아님)다. 따라서 external 백엔드도 todo.db 를
# 그대로 두고(= cli import-md 가 계속 생성) 읽기만 위임할 수 있다 — SQLite state.sqlite
# 로 복사할 필요 없음. 소유 이전은 비목표.
```

### 재사용/흡수 결정 — **구성(composition)을 택한다**

| 선택지 | 내용 | 트레이드오프 |
|---|---|---|
| **구성 (채택)** | `StateStore.mailbox()`가 `Mailbox`(mailbox.py:81)를 돌려준다. git 모드는 `open_mailbox("git://Worker/agents")`(:341)로 **현행 `GitMailbox`(:226)·`FileMailbox`(:217)를 그대로** 쓴다. external 모드는 SQLite 기반 `Mailbox` 하위클래스가 전송 훅(`_before_receive`:138 / `_after_send`:141 / `_after_ack`:193)과 커서 영속만 오버라이드한다. | 검증된 커서/틈브스톤 정합성(`mailbox.py:47-55`, 99–209)을 **다시 안 짠다**. 표면이 하나 더 생기지만(`StateStore.mailbox()`) 이미 `Mailbox` ABC 가 하위클래스 확장점을 갖고 있어 자연스럽다. |
| 흡수 | 평평한 `StateStore` 하나가 `send`/`receive`/`ack`를 직접 노출(별도 Mailbox 없음). | 표면은 작아지지만 틈브스톤/at-least-once 로직을 **모든 백엔드가 재구현**해야 하고, leaf ②~⑥의 폭발 반경이 커진다. GitMailbox 의 pull/push·rebase 재시도(`mailbox.py:295-333`)를 StateStore 로 끌어올려야 해 레이어가 뒤섞인다. |

**결정 근거**: `Mailbox` ABC 는 이미 (a) put/take/ack 대칭 네 메서드, (b) 전송
훅 세 개(`_before_receive`/`_after_send`/`_after_ack`)라는 백엔드 확장 seam 을
갖고 있다. 새 백엔드는 "재작성"이 아니라 "하위클래스"로 끝난다 — 구성을 가리키는
결정적 증거다. 흡수의 유일한 장점(표면 축소)은 틈브스톤 재구현 비용을 넘지 못한다.
따라서 **ledger·planner_kv·todo_cache 도 같은 원칙**으로 간다: `StateStore` 는
각 영역의 기존 구현(또는 그 하위클래스)을 **보유(has-a)**하고, 호출부는
`StateStore`를 통해 그 영역에 닿는다.

---

## ③ 모드 전환 설정 + external SQLite 스키마 초안

### 모드 전환 키

- **환경변수** `MACP_STATE_BACKEND` ∈ `{git, external}`, **기본 `git`**.
- **설정 파일** `.macp/config.toml` 최상위 문자열 키 `state_backend`.
- **리졸버**는 `paths.py`의 기존 3종 패턴을 그대로 복제한다:
  `label_prefix`(paths.py:238) · `primary_calendar_id`(:277) · `repo_slug`(:312)
  — 셋 다 `env MACP_* → 상향 탐색한 가장 가까운 .macp/config.toml 의 키(`_read_config_key`:83,
  tomllib 없으면 minimal 폴백 :103) → 중립 기본값` 순서다. 신규 `state_backend` 리졸버도
  동일하게:
  1. `os.environ.get("MACP_STATE_BACKEND")` 가 있으면 그 값(검증: `{git, external}` 외는 거부).
  2. 없으면 상향 탐색한 `.macp/config.toml` 의 `state_backend` 키.
  3. 없으면 중립 기본 `"git"` — 기존 설치는 아무 설정 없이 **현행 동작 그대로**.
  > 이 리졸버 함수의 **정확한 이름/시그니처는 여기서 확정하지 않는다**(leaf ③의 일).
  > 본 노트는 "`repo_slug` 를 닮은 리졸버 하나"라고만 규정한다 — 지어낸 시그니처가
  > leaf ③과 어긋나지 않게.
- **external SQLite 경로**: 기본 `~/.local/share/macp/state.sqlite`(XDG `$XDG_DATA_HOME`
  존중, 볼트 **밖** — git 트리에 없음이 핵심). 재정의: `MACP_STATE_SQLITE_PATH` env /
  `state_sqlite_path` config 키(같은 리졸버 패턴).

### external SQLite 스키마 초안

①의 전수 목록을 그대로 테이블로 투영한다(초안 — leaf ④가 확정·마이그레이션):

```sql
-- 메타/버전 (마이그레이션 발판)
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);          -- schema_version 등

-- ledger: Assignment(assignment.py:93, 필드 95–193) 1행=1과제
CREATE TABLE ledger (
  task_id      TEXT PRIMARY KEY,
  agent_id     TEXT NOT NULL,
  corr_id      TEXT NOT NULL,
  state        TEXT NOT NULL,          -- assignment.py:18-27 상수
  kind         TEXT,
  attempts     INTEGER,
  refail_count INTEGER,
  created_ts   TEXT,
  updated_ts   TEXT,
  -- 스칼라 필드는 열로; 가변/중첩은 JSON 텍스트로(SQLite JSON1):
  flags_json   TEXT,                   -- capacity_limited/job_running/checkout_busy/… 묶음
  history_json TEXT,                   -- history: list[dict] (assignment.py:192)
  payload_json TEXT                    -- payload: dict (:193)
);
CREATE INDEX ledger_state ON ledger(state);

-- mailbox: inbox 메시지 (Envelope)
CREATE TABLE mailbox (
  msg_id      TEXT PRIMARY KEY,        -- ULID (시간순)
  to_agent    TEXT NOT NULL,
  frm         TEXT,
  type        TEXT NOT NULL,
  task_id     TEXT,
  corr_id     TEXT,
  payload_json TEXT,
  created_ts  TEXT NOT NULL
);
CREATE INDEX mailbox_to ON mailbox(to_agent, msg_id);

-- mailbox 커서 + ack 틈브스톤 (mailbox.py:47-55 의 at-least-once/틈브스톤을 열로 보존)
CREATE TABLE mailbox_cursor (
  agent_id TEXT NOT NULL,
  msg_id   TEXT NOT NULL,
  status   TEXT NOT NULL,              -- 'seen' | 'acked'
  acked_at REAL,                       -- 틈브스톤 타임스탬프(TTL/캡 적용 대상)
  PRIMARY KEY (agent_id, msg_id)
);
-- external 모드에서는 소비=행 삭제가 곧 영구 소비라 git-restore 부활이 원천적으로
-- 없음 — 틈브스톤은 git 모드 호환/이관 보존용으로만 유지(§⑤ 참조).

-- planner kv (worker.py:364/:377) — 통짜 dict 를 key/value 로 평탄화
CREATE TABLE planner_kv (
  key        TEXT PRIMARY KEY,         -- verdict / todo_state_hash / …
  value_json TEXT,
  updated_ts TEXT
);
```

- **todo 캐시는 SQLite 로 복사하지 않는다** — md 의 파생이라 `todo.db` 를 그대로 둔다(§②).
- `queue.json`·`runs/`·`sweep_state`·heartbeat 는 **호스트 로컬 런타임**이고 SoT 가
  아니므로 1차 external 백엔드의 이관 대상이 아니다(ledger 에서 재생성되는 파생 뷰거나
  순수 로컬 스크래치). 공유가 필요해지면 leaf ⑦(Cloud SQL)에서 재검토.
