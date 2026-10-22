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

---

## ④ git → external 1회 이관 + external → `Worker/assignments/` 스냅샷 export

### 4.1 git → external 1회 import (멱등·재실행 가능)

external 모드로 처음 전환할 때 트리/호스트의 현재 상태를 SQLite 로 끌어올린다:

1. **ledger** — `Worker/assignments/*.json` 전부를 `Ledger.all()`(assignment.py:264)
   모양으로 읽어 `ledger` 테이블에 upsert(`task_id` PK). 스칼라→열, `history`/`payload`/
   flags→JSON 열.
2. **mailbox** — `Worker/agents/<agent>/inbox/*.json` 전부를 `mailbox` 테이블에,
   `Worker/agents/_state/<agent>/cursor.json`(seen+acked)을 `mailbox_cursor` 로.
   커서의 ack 틈브스톤도 그대로 옮겨 이관 직후 재수신이 없게 한다.
3. **planner kv** — `planner_state.json`(worker.py:111)을 평탄화해 `planner_kv` 로.
4. **todo 캐시** — 건너뜀(파생; `todo.db` 유지).

멱등 요건: PK upsert 라 두 번 돌려도 같은 결과. 부분 실패 시 재실행으로 이어붙는다.
`meta.schema_version` 를 세워 이후 스키마 변경의 발판으로 삼는다.

### 4.2 external → `Worker/assignments/` 읽기 전용 스냅샷 export

클라우드 본부 인수·대시보드는 **git-추적 ledger**(`Worker/assignments/*.json`)를
읽도록 설계돼 있다([[Decisions/2026-09-10-macp-cloud-hq-vault-sot]]). external 모드에서
권위 사본이 SQLite 로 가더라도 이 소비자들을 깨지 않으려면, 주기적으로(또는 ledger
변경 틱마다) SQLite `ledger` 를 git-추적 `Worker/assignments/<task_id>.json` 으로
**단방향 덤프**한다.

- 스냅샷은 **읽기 전용 / 권위 사본은 SQLite** 라고 명시한다 — write-back 루프 방지.
  즉 export 는 SQLite→파일 한 방향만; 파일을 손으로 고쳐도 다음 export 가 덮어쓴다.
- **자가 치유**: §⑤의 git 조작이 스냅샷 파일을 되돌리거나 지워도, 다음 export 틱이
  SQLite(권위 사본)에서 다시 쓴다. 그래서 external 모드에서는 스냅샷조차 git-조작에
  견딘다(권위 상태가 트리 밖이므로).
- git 모드에서는 이 export 가 no-op(파일이 이미 권위 사본).

---

## ⑤ git 조작 회귀 테스트 시나리오 3종

공통 축: **external 모드**에서는 볼트 git 조작이 권위 상태(SQLite)를 **건드리지
못한다**(이 태스크의 동기이자 승리). **git 모드**에서는 기존 틈브스톤/at-least-once
방어(`mailbox.py:47-55`)가 **여전히 유효**해야 한다(회귀 가드). 세 시나리오는 그대로
leaf ⑥의 자동화 스위트(`test_state_store_git_ops.py`)가 된다.

> 정밀 주의(§①의 관찰): git 조작이 되살리거나 롤백할 수 있는 건 **추적** 상태뿐이다 —
> ledger(`.gitignore:122`)·mailbox 메시지(`:114`). queue/planner/커서는 비추적이라
> 두 모드 다 git 이 손대지 않는다. 그래서 아래 불변식은 추적 상태(ledger·mailbox)를
> 1차 대상으로 둔다.

### 5.1 옛 워커 브랜치 checkout → 복귀
- **Setup**: external 모드. `ledger` 에 과제 A(`state=in_progress`). orchestrator inbox 에
  방금 ack 로 삭제된 메시지 M 과 미처리 N. (M 은 옛 브랜치 HEAD 트리에는 아직 존재.)
- **Op**: `git checkout worker/<old>` → `git checkout main`.
- **기대(external)**: SQLite 의 ledger/mailbox **불변**. `receive("orchestrator")` 는 N 만
  반환, M 은 영구 소비(행 삭제)라 재수신 없음 — 틈브스톤 불필요.
- **기대(git, 현행 유지)**: 옛 브랜치 체크아웃이 `M.json` 을 트리에 부활시키지만 커서
  틈브스톤(`mailbox.py:47-55`)이 M 을 억제해 파이프라인 재실행을 막는다. 이 방어가
  깨지지 않아야 함(핵심 회귀 가드).

### 5.2 `git reset --hard`
- **Setup**: external 모드. `ledger` A(`in_progress`) + 방금 ack 로 지운 M.
- **Op**: stage 에이전트의 사고성 `git reset --hard HEAD~1`.
- **기대(external)**: reset 은 트리만 되돌린다 → SQLite ledger/mailbox **불변**, A 의
  진행 상태 유지. (스냅샷 파일이 롤백돼도 §4.2 export 가 복원.)
- **기대(git, 현행)**: reset 이 커밋된 ledger/inbox 를 HEAD 커밋 버전으로 되돌려 **트리내
  상태가 손실·되살림될 수 있음** — 바로 external 이 제거하는 취약성. 삭제 메시지 부활은
  틈브스톤이 방어하나 ledger 롤백 자체는 git 모드의 알려진 한계.

### 5.3 `git stash` / autostash
- **Setup**: external 모드. dirty tree(stage 작업 중), ledger A 갱신 진행.
- **Op**: `git stash push -u -m <tag>` → `git stash apply`(또는 `pull --rebase` 의
  autostash 실패 경로 — `mailbox.py:50` 주석이 명시한 사례).
- **기대(external)**: stash 는 트리 변경만 치운다 → SQLite **불변**.
- **기대(git, 현행)**: autostash 실패가 inbox 메시지를 부활시킬 수 있고, 틈브스톤이
  재실행을 막는다. (주의: 이 리포는 공유 stash 스택이므로 테스트는 bare `stash pop`
  대신 태그 기반 `push -u`/`apply` 로 격리한다.)

---

## ⑥ 후속 leaf 지도 (②~⑥, +⑦ 선택)

각 leaf = 범위 한 줄 + touches 후보. ②~⑥은 **필수**, ⑦은 **선택**(다중 호스트 공유가
필요해질 때). 순서는 직렬 의존(앞이 머지돼야 뒤가 안전):

- **② `StateStore` ABC + `GitStateStore` 현상유지 어댑터** — 네 영역을 하위 표면으로 묶고
  git 백엔드가 현행 `Ledger`/`open_mailbox`/planner_state/todo.db 를 **그대로** 감싼다.
  동작 변화 0(기본 경로). *touches*: `todo_system/orchestrator/state_store.py`(신규) +
  `todo_system/orchestrator/tests/test_state_store.py`(신규).
- **③ 설정 배선** — `MACP_STATE_BACKEND`(기본 git) + `paths.py` 리졸버(`repo_slug` 패턴
  복제) + `state_sqlite_path`. *touches*: `todo_system/paths.py` + `todo_system/tests/test_paths.py`.
- **④ `ExternalStateStore` SQLite 백엔드** — §③ 스키마를 세우고 ledger/mailbox/planner_kv 를
  SQLite 로. SQLite 기반 `Mailbox` 하위클래스(전송 훅 오버라이드). *touches*:
  `todo_system/orchestrator/state_store_sqlite.py`(신규) + 테스트(신규).
- **⑤ 1회 이관 + 스냅샷 export** — §④의 git→external import 와 external→assignments
  읽기 전용 export. *touches*: `todo_system/orchestrator/state_migrate.py`(신규) 또는
  `cli`/`worker` 하위명령 + 테스트(신규).
- **⑥ git-조작 회귀 스위트 + 호출부 라우팅** — §⑤ 3종을 자동화하고, 현행 직접 접근부
  (`worker.py`의 QUEUE/PLANNER·`metrics.py`의 LEDGER/DB·orchestrator)를 `StateStore`
  경유로 돌린다. *touches*: `todo_system/tests/test_state_store_git_ops.py`(신규) +
  `worker.py`·`metrics.py`·orchestrator 호출부.
- **⑦ (선택, 다중 호스트 공유 필요 시) Cloud SQL(Postgres) 백엔드** — external 축을
  네트워크 공유 DB 로 확장. **Supabase·Neon 금지**([[Decisions/2026-09-25-backend-unify-gcp-no-supabase]]);
  **GCP Cloud SQL 만** 각주 확장. *touches*: 신규 백엔드 모듈 + 테스트.

> 열림 질문 해소: 부모 분해는 ②~⑦이지만 이 leaf 의 수용 기준은 ②~⑥을 필수로 본다.
> 위처럼 ②~⑥을 필수, ⑦을 선택으로 적어 양쪽을 만족한다.

---

## ⑦ 네트워크 공유 백엔드(Cloud SQL) — 예약 슬롯

external 축(본부 볼트 밖 SQLite)을 **다중 호스트가 네트워크로 공유**할 때의 자리. §①~⑥의
SQLite 는 본부(primary host) 로컬 파일이라 다른 머신이 직접 열 수 없다 — 네트워크 공유가
필요해지면 상세 후보 비교는 **§⑧(원격 워커 접근)**, 구현 leaf 는 **§⑥ 지도의 leaf ⑦** 이다.
**상시 외부 SaaS DB(Supabase·Neon)는 금지**하고 유료 인프라는 **GCP Cloud SQL 만**
([[Decisions/2026-09-25-backend-unify-gcp-no-supabase]], [[Decisions/2026-10-13-macp-task-store]]),
월 비용을 숫자로 밝히며(§⑧ (b) ~$10~25/월) 프로비저닝은 사람 결정이다. 이 절은 §⑥→§⑧ 번호
점프를 막는 **예약 슬롯**으로, 새 설계 결정을 지어내지 않고 §⑧로 포워딩만 한다.

---

## ⑧ 원격 워커 접근 (external 모드에서 다른 머신의 러너가 같은 상태에 읽고/쓰기)

§①~⑥은 external 모드의 상태를 **본부(primary host) 한 대의 볼트 밖 SQLite**
(`~/.local/share/macp/state.sqlite`)에 두는 것으로 설계했다. 그런데 원격 호스트(Windows/WSL 등)의
러너도 같은 상태를 읽고 써야 한다(사용자 요구: "외부 머신에서 동작하는 워커도 DB에 접근 가능해야").
SQLite 파일은 로컬 파일 핸들이라 다른 머신이 직접 열 수 없으므로 네트워크 경로가 필요하다.

**현행 원격 경로(baseline)** 는 **순수 git** 이다: `scripts/worker_remote_loop.sh` 가 볼트를 pull →
`Worker/remote/<host>/inbox/*.md` 의 과제를 실행 → 결과(queue/run 산출물/episodes)를 push 하고,
오케스트레이터 Runner 는 `scripts/orchestrator_runner_remote.sh:54-57` 로 `--uri "git://Worker/agents"`
mailbox 를 쓴다(transport 는 전부 볼트 git). external 모드는 이 git-추적 상태를 볼트 밖으로 빼는 게
목적이므로, 원격 접근도 "git 말고 무엇으로"를 정해야 한다.

### 후보 비교표

| 후보 | 비용(월 $) | 운영 부담 | 지연 | 오프라인 시 동작 | 보안 |
|---|---|---|---|---|---|
| **(a) 본부 HTTP 상태 API + 토큰** — SQLite 는 본부에만, 원격 러너가 HTTP(S)로 CRUD/claim 호출 | **$0**(본부 VM 상주, 추가 관리형 자원 없음) | 중 — 상태 API 서버(읽기/쓰기/claim)·인증·버전 관리 신규 | 낮음(네트워크 1홉) | **약함** — 본부 다운 시 못 닿음 → 로컬 큐로 degrade(아래) | 토큰(Secret Manager) + TLS, 선택적으로 Tailscale 테일넷 안에서만 리슨 |
| (b) GCP Cloud SQL(Postgres) 각 호스트 직접 접속 | **~$10~25**([[Decisions/2026-10-13-macp-task-store]] C행; db-f1-micro~소형) | 높음 — 인스턴스·IAM·Auth Proxy·백업·스키마 마이그레이션 상시 운영 | 낮음~중 | 약함 — 외부 관리형 DB 의존(2026-09 Supabase 참사와 같은 SPOF 구조) | Cloud SQL IAM + Auth Proxy / 승인 네트워크 |
| (c) 현행 git 동기화 유지 — external 은 **본부 전용**, 원격은 지금처럼 git inbox | **$0** | 낮음 — 이미 구축·운영 중(`worker_remote_loop.sh`) | 높음(pull/push 주기 = 수초~수분) | **강함** — git 은 비동기·오프라인 내성(못 닿으면 다음 틱에 이어붙음) | 기존 git 인증(토큰/SSH) 그대로 |
| (a′) (a)를 Tailscale 등 메시 VPN 으로만 노출 | **$0**(무료 티어) | 중(+테일넷 ACL) | 낮음 | (a)와 동일 | 공개 IP 없음 — 테일넷 ACL 로 디바이스 단위 제한 |

*비용 열은 관리형 자원의 월 과금만; 본부 VM 자체는 어느 후보든 상주하므로 공통 비용에서 뺐다.*

### 권장안 + 이유

**권장: (a) 본부 HTTP 상태 API + 토큰.** 단 **(c) git baseline 을 "상태 공유가 꼭 필요하지 않은 원격
호스트"의 기본 fallback 으로 유지**하고, **(b) Cloud SQL 은 본부 단일 장애점이 실제 병목이 될 때만 가는
사람-게이트 에스컬레이션**으로 둔다. 노출면을 줄이려면 (a)를 (a′)(Tailscale 변형)로 배치한다.

근거(5열 + 프로젝트 prior):

- **단일 SoT 유지**: (a)는 SQLite 권위 사본을 본부 한 곳에 그대로 둔다(§④의 "권위 사본은 SQLite,
  `Worker/assignments/` 는 읽기전용 스냅샷"과 정합). (b)는 상태 SoT 를 또 하나의 상시 DB 로 옮겨 2026-09
  Supabase 마비(큐 22일 정지)의 구조를 재현할 위험이 있다 — 상시 외부 DB 배제 방침
  ([[Decisions/2026-09-25-backend-unify-gcp-no-supabase]], [[Decisions/2026-10-13-macp-task-store]])과 충돌.
- **유료 인프라는 사람 게이트**: (a)/(c)는 $0(본부 상주 자원만). (b)만 월 ~$10~25 과금 + Cloud SQL 상시
  운영 — 부모 체인의 ⑦(원격 접근)이 human_gate 인 이유다. 실제 네트워크 노출·유료 프로비저닝은 **사람 결정**.
- **오프라인 내성**: (c)는 강하고 (a)는 약하지만, (a)의 약점은 아래 "본부 장애 시 동작"의 로컬 큐 degrade 로
  상쇄된다 — 쓰기를 로컬에 적재했다가 복구 시 flush 하면 (c)의 비동기 내성을 쓰기 경로에서 회복한다.
- **운영 최소**: (a)는 본부 상주 VM 에 엔드포인트 하나를 더하는 수준. (b)의 Cloud SQL 상시 운영 대비 가볍다.
- **md=SoT 불변**: 태스크 소스는 external 모드에서도 md(§①)이고, **상태 저장소만** 네트워크화한다 — (a)는
  상태 CRUD 만 API 로 노출하고 태스크 편집 인터페이스(파일)는 안 건드린다.

### 인증 방식

권장안 (a)/(a′)에 정합하는 레이어:

- **토큰**: 러너→본부 API 요청에 bearer 토큰, 토큰은 본부 **Secret Manager** 에 보관(리포·볼트에 평문 금지).
  공개 리포 설계라 실제 토큰 값·엔드포인트 host 는 여기 안 적는다.
- **Google ID 토큰 서버 검증**(대안/강화): 2026-09-25 결정의 표준 인증(Cloud Run + ID 토큰 서버 검증)과
  동형 — API 가 호출자의 ID 토큰을 서버에서 검증.
- **Tailscale ACL**(네트워크 레이어): API 를 테일넷에만 리슨시키고 디바이스 단위 ACL 로 제한 → 토큰이
  유출돼도 공개 네트워크에서 도달 불가. (a′) 권장 배치의 핵심.

### 본부 장애 시 원격 러너 동작

(a)의 오프라인 약점을 메우는 핵심 규칙:

1. **재시도(backoff)**: 상태 API 호출 실패 시 지수 backoff 로 재시도(현행 git push 가 3회 재시도하는 것과
   동형 — `scripts/worker_remote_loop.sh:79-87`).
2. **로컬 큐 적재 → 복구 시 flush**: 재시도 한계를 넘으면 원격 러너는 쓰기(상태 변경·claim 해제·결과)를
   **로컬 append-only 아웃박스**(호스트 로컬 파일, 볼트 밖)에 적재하고 과제 진행을 멈춘다. 본부 복구가
   감지되면 아웃박스를 순서대로 flush(멱등 upsert 라 중복 적용 안전). 이로써 쓰기 경로에서 (c)의 비동기
   내성을 회복한다.
3. **읽기 degrade**: 새 과제 claim 은 본부가 살아 있어야만 가능(claim 은 공유 상태 쓰기) → 본부 다운 중
   원격은 **진행 중 과제만 마치고 신규는 안 집는다**(중복 실행 방지와 정합).

### at-least-once · 중복 실행 방지 (lease/claim)

- **claim = lease**: 러너가 과제를 집을 때 공유 상태(ledger 행 또는 전용 claims 테이블)에 **lease**
  (소유자 host/run_id · 획득 ts · TTL 만료 ts)를 원자적으로 기록한다. 이미 유효 lease 가 있으면 다른
  러너는 그 과제를 집지 않는다 → 동시 두 호스트의 중복 실행 방지.
- **TTL 로 크래시 복구**: 러너가 과제 도중 죽으면 lease 가 TTL 만료되어 다른 러너가 재획득 가능. 이때
  과제는 다시 실행될 수 있으나(at-least-once), 스테이지 파이프라인은 재시도=전체 재실행으로 재실행
  안전하게 설계돼 있어 허용된다.
- **mailbox 틈브스톤과의 관계**(`mailbox.py:47-55`): git 모드의 틈브스톤 핵은 "ack 삭제가 커밋 안 돼
  git-restore 가 메시지를 부활시킨다"는 **git-추적 전송** 고유 문제다. 네트워크 백엔드에선 claim/ack 이
  **DB 행 변이**(네트워크로 커밋)라 git 이 되돌릴 트리 상태가 없다 → claim 이 곧 영구 소비, 틈브스톤 보정
  **불필요**(§①·§⑤가 external 모드에 대해 이미 내린 결론과 동일). at-least-once 전달(mailbox receive 가
  메시지를 ≥1회 반환)은 유지되고, claim 의 멱등성(재집음 시 기존 lease 소유자 확인)이 중복 실행을 막는다.

---

### 부록: 이 노트가 확정한 설계 결정 요약

1. Mailbox 관계 = **구성(composition)**, 흡수 아님(§②).
2. 기본 백엔드 = **git(무변경)**, external = 볼트 밖 호스트 SQLite(§③).
3. ledger 는 external 모드에서도 git-추적 `Worker/assignments/` 로 **읽기 전용 스냅샷**
   export(권위 사본은 SQLite) — 본부/대시보드 호환 유지(§④).
4. todo.db 는 **비이관**(md 의 파생 캐시)(§②§③).
5. 상시 외부 SaaS DB(Supabase/Neon) **배제**; Cloud SQL 은 선택 각주(§⑥⑦)
   ([[Decisions/2026-10-13-macp-task-store]], [[Decisions/2026-09-25-backend-unify-gcp-no-supabase]]).
