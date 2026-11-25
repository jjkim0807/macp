---
title: 다른 코딩 에이전트 지원 — 스테이지 백엔드 추상화 설계 노트 (철회됨)
status: superseded
created: 2026-10-22
superseded: 2026-11-25
related:
  - Decisions/2026-11-20-human-gate-irreversible-only.md
---

# 다른 코딩 에이전트 지원 — 스테이지 백엔드 추상화 설계 노트 (철회됨)

> **철회(2026-11-25).** 이 노트가 설계한 **비-Claude 에이전트 백엔드**(Upstage Solar
> HTTP 루프 `solar_stage` + Gemini CLI 백엔드 + vendor 라우팅)는 **사용 0회**로
> 측정되어 엔진에서 삭제됐다. `agent:` 태스크 필드로 스테이지를 다른 vendor에 보내던
> 경로와 그 계정·키 배선도 함께 지웠다. 근거·측정은
> `Notes/macp-deletion-audit/2026-11-20.md`의 D10, 결정은
> `Decisions/2026-11-20-human-gate-irreversible-only.md`.
>
> 남는 것: 스테이지 실행은 여전히 `AgentBackend` 심(`todo_system/orchestrator/agent_backend.py`)
> 뒤에 있고, 유일한 백엔드는 `ClaudeAgentBackend`(`claude -p`)다. 디스패처의 능력
> 게이트(`missing_capabilities`)는 그대로 남아, 스테이지가 요구하는 능력을 못 채우는
> 백엔드를 디스패치 전에 거절한다. 다시 다른 코딩 에이전트를 붙일 필요가 생기면 git
> 히스토리에서 설계를 복구한다.

이 파일은 다른 노트가 걸어 둔 링크가 깨지지 않도록 남겨 둔 **철회 기록**이다.
상세 설계 내용은 보관하지 않는다 — 필요하면 git 히스토리를 본다.
