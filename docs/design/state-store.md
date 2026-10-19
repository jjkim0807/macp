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
