---
title: 상태 저장소 외부 백엔드 — 설계 노트 (철회됨)
status: superseded
created: 2026-10-20
superseded: 2026-11-25
related:
  - Decisions/2026-10-13-macp-task-store.md              # md 정본·외부 상시 DB 배제
---

# 상태 저장소 외부 백엔드 — 설계 노트 (철회됨)

> **철회(2026-11-25).** 이 노트가 설계한 **외부 상태 저장소**(호스트 로컬 SQLite
> `external` 모드 + 본부 HTTP `remote` 모드 + git→external 이관 도구)와 **외부 트래커
> 호환**(§⑨ Jira·GitHub Issues·Linear)은 **사용 0회**로 측정되어 엔진에서 삭제됐다.
> 근거·측정은 `Notes/macp-deletion-audit/2026-11-20.md`의 D7·D8, 결정은
> `Decisions/2026-11-20-human-gate-irreversible-only.md`.
>
> 남는 것은 **git 백엔드 하나뿐**이다: 오케스트레이터의 지속 상태(assignment ledger,
> mailbox, planner state)는 볼트 git 트리 안의 파일로 유지된다. `open_state_store()`는
> 언제나 `GitStateStore`를 돌려준다(`todo_system/orchestrator/state_store.py`).
> 다시 다중 호스트가 한 상태를 공유해야 할 필요가 생기면 git 히스토리에서 복구한다.

이 파일은 다른 설계 노트가 걸어 둔 링크가 깨지지 않도록 남겨 둔 **철회 기록**이다.
상세 설계 내용은 보관하지 않는다 — 필요하면 git 히스토리를 본다.
