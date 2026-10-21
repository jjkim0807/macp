---
title: 다른 코딩 에이전트 지원 — 스테이지 백엔드 추상화 설계 노트 (leaf ①)
status: draft
created: 2026-10-22
related:
  - Todo/items/macp-agent-backends-1-design.md    # 이 설계 태스크
  - Todo/items/macp-agent-backends-2-implement.md  # 사람 검토 게이트 후속 구현
  - docs/design/state-store.md                     # 동일 "분석→설계→leaf 분할" 패턴의 선행 노트
---

# 다른 코딩 에이전트 지원 — 스테이지 백엔드 추상화 설계 노트

> **이 노트의 성격**: 분석·설계 전용. 이 leaf(①)는 코드·테스트·프롬프트를 바꾸지
> 않는다 — `docs/design/agent-backends.md` 한 파일만 추가한다. 실제 추상화 구현은
> 사람 검토 게이트(`human_gate: true`) 뒤의 후속 태스크
> [[macp-agent-backends-2-implement]]가 §5 의 분할안대로 맡는다. 모든 `파일:줄`
> 앵커는 이 리포(`jjkim0807/macp`)의 HEAD 트리를 **직접 읽어** 확인했고(줄 번호는
> 드리프트하므로 Verify 는 `grep -n <symbol> <file>` 로 재확인한다), 확인하지 못한
> 사실은 '미확인'으로 남겼다(§3 후보 에이전트). CLI 플래그·권한 모델은 버전마다
> 바뀌므로 §3 각 사실에 **조회 날짜**를 적는다.

## 왜 이 문서인가

macp 는 스스로를 **"코딩 에이전트를 위한 control plane"** 이라 부르지만
(`docs/concepts.md:3`), 오늘 스테이지를 실제로 돌리는 경로는 **Claude Code 하나에
하드코딩**돼 있다. `docs/concepts.md:28–29` 와 `README.md:31–32` 는 "executors for
other coding agents are **planned** / 다른 코딩 에이전트 지원은 **계획 중**" 이라고
적어 두었는데, 이 문서가 바로 그 "계획"의 설계다.

결합의 핵심은 세 겹이다: (1) `runner.py` 의 `Executor` 프로토콜(`:70`)은 이미
백엔드 경계에 가장 가깝지만, 실제 5-스테이지 파이프라인은 (2) `stages.build_stage_argv`
(`:402`)가 `["claude","-p", …]` argv 를 통째로 조립해 넘긴다. 반대로 (3)
`config.build_invocation`(`:273`)은 **이미 vendor 중립** `(argv, env)` 을 만들고
(claude / upstage / 일반 CLI 3갈래), `solar_stage.run`(`:257`)은 **이미 비-Claude·
비-CLI(HTTP) 백엔드 한 종을 스테이지 루프에 꽂은 선례**다 — `route_to_solar` 게이트
(`stages.py:181`)로 quick/delegate 스테이지만 `claude -p` 대신 Solar 로 돌린다.
즉 추상화의 뼈대는 이미 두 군데(프로토콜·vendor 중립 invocation)에 있고, 남은 일은
`build_stage_argv` 가 조립하는 Claude 전용 argv 를 **에이전트 중립 인터페이스 뒤로**
옮기되 Claude 경로의 argv/env 는 **바이트 동일**하게 유지하는 것이다(§4).

---

## §1 Claude 전용 결합 지점 전수표

범례 — **파일:줄** 은 HEAD 기준(Verify 가 `grep -n` 재확인). **무엇이 Claude 전용인가**
= 이 지점이 claude CLI/구독/OAuth 에 묶인 부분. **추상화 방향** = 백엔드 중립화 시 이
지점이 어디로 가야 하는가. 이미 vendor 중립인 지점은 그렇게 표시한다(재사용 대상).

### (A) argv·플래그 조립 — `todo_system/orchestrator/stages.py` (중심 결합점)

| 결합 지점 | 파일:줄 | 무엇이 Claude 전용인가 | 추상화 방향 |
|---|---|---|---|
| `build_stage_argv` (argv 조립) | `stages.py:402`; 조립부 `:491–503` | `argv[0]="claude"`, `-p <prompt>`, `--model`/`--effort`/`--max-turns`/`--session-id`/`--allowed-tools`/`--add-dir` 가 전부 claude CLI 플래그 문법 | 이 함수가 선택된 백엔드의 `build_invocation(stage, meta) → (argv|호출, env)` 로 **위임**. Claude 백엔드는 지금 argv 를 바이트 동일 재현(§4 불변식) |
| `STAGE_TOOLS` (스테이지별 허용툴) | `stages.py:259` | `Bash(git:*)` 같은 claude `--allowed-tools` 문법(툴명+글롭) | 능력 "툴 허용목록"을 백엔드가 자기 표기로 번역, **미지원이면 조용히 무시 말고 디스패치 전 거절/경고**(구현 AC #3) |
| `STAGE_MAX_TURNS` (턴 예산) | `stages.py:284` | "turn" 은 claude 개념(`--max-turns`) | 능력 "턴/시간 상한". 턴 개념 없는 백엔드는 시간/반복 상한으로 매핑하거나 미지원 표기 |
| `stage_model` / `stage_effort` | `stages.py:85` / `:95` | `model_select` 로 claude 모델 id·effort 레벨 반환 | vendor별 모델 선택은 백엔드가(`Account.models`); `--effort` 는 claude 전용 능력이라 없으면 생략 |
| `route_to_solar` / `SOLAR_STAGES` | `stages.py:181` / `:178` | `agent=="upstage"` && `stage∈{quick,delegate}` 단일 vendor 하드 게이트 | "이 백엔드가 이 스테이지를 지원하는가" 일반 라우팅으로 대체 — 이미 존재하는 유일한 vendor 분기이자 확장 템플릿 |
| `_safe_tools` / `FORBIDDEN_TOOL_SUBSTRINGS` / `TASK_TOOLS_STAGES` | `stages.py:329` / `:303` / `:326` | 필터 자체는 vendor 중립(문자열 검사)이나 결과를 claude `--allowed-tools` 로 흘려보냄 | 필터는 **재사용**, 출력 소비처만 백엔드 번역 뒤로 |
| 사용량 read-back: `USAGE_ENV`·`SESSION_ID_FLAG`·`usage_enabled`·`argv_session_id` | `stages.py:372`·`:379`·`:382`·`:390` | `--session-id` 로 claude transcript 를 만들고 `record_stage_usage` 가 그 transcript 에서 토큰을 되읽음 | 능력 "사용량 보고". 백엔드가 자기 토큰 카운트를 공통 `USAGE_KEYS` dict 로 반환(solar 는 `usage_rec=None` 로 이미 비활성) |
| `load_stage_prompt` / `COMMANDS_DIR` | `stages.py:309` / `:50` | Claude 전용은 아님(`.claude/commands/worker-<stage>.md` 읽기)이나 **전체 프롬프트를 claude 에 전달**하는 전제 | 능력 "프롬프트 전달": 전체 `.claude/commands` 프롬프트 vs 축약 payload(§2-(8), solar 선례) |

### (B) 실행기(executor) — `todo_system/orchestrator/runner.py`

| 결합 지점 | 파일:줄 | 무엇이 Claude 전용인가 | 추상화 방향 |
|---|---|---|---|
| `Executor` 프로토콜 (`mode`+`execute`) | `runner.py:70` | 중립 — 이미 백엔드 경계에 가장 가까운 추상 | **유지·확장**; 새 백엔드도 이 프로토콜을 만족 |
| `ClaudeCodeExecutor.build_argv` | `runner.py:141` | 레거시 비-staged 경로의 `claude -p … --max-turns --allowed-tools` | StagedExecutor 가 실제 파이프라인이라 영향 적음 — 참고 결합점 |
| `ClaudeCodeExecutor._run` | `runner.py:148` | `subprocess.run(..., timeout=1800, env=claude_env())` — `claude_env()` OAuth 주입 + 1800s | 서브프로세스 실행+env 주입을 백엔드가; 타임아웃은 공통 래퍼로 |
| `BackgroundClaudeExecutor` | `runner.py:186` | `claude --bg … --worktree` 백그라운드 에이전트(claude.ai/code 관측) | `live-remote` 모드는 claude 고유 — 당분간 **claude 전용 유지**(이식 대응물 없음) |
| `StagedExecutor._account_env` | `runner.py:301` | `CLAUDE_CONFIG_DIR` 로 claude 계정 격리 | 계정→env 는 `build_invocation` 이 이미 vendor별 처리(claude=CLAUDE_CONFIG_DIR, upstage=key). **재사용** |
| `_maybe_rotate_account` | `runner.py:371` | claude 계정 용량 분류+회전(`accounts.classify_capacity`/`rotate_active`) | 능력 "종료 의미(쿼터 소진)". claude 구독 모델 특화 — 비-claude 는 자기 레이트리밋 신호로 분류 |
| `use_solar` 분기 + `_solar_task_prompt` | `runner.py:1046–1066` / `:238` | **비-claude 경로의 정본**: `route_to_solar` 면 `solar_stage.run` 이 `claude -p` 대신 돌고 `CompletedProcess`-shape 반환, `usage_rec=None`, 자기 artifact 작성 | 이 2갈래(claude argv ↔ solar)를 **N-백엔드 디스패치로 일반화 — AgentBackend 인터페이스가 맞물릴 바로 그 지점**(§4) |

### (C) 계정·인증 — `todo_system/macp/config.py`, `todo_system/claude_auth.py`

| 결합 지점 | 파일:줄 | 무엇이 Claude 전용인가 | 추상화 방향 |
|---|---|---|---|
| `config.Account` | `config.py:51` | **이미 vendor 중립** — `agent`/`command`/`env`/`key_file`/`models`; docstring 이 claude/codex/cursor/gemini/aider 명시 열거 | 백엔드 선택의 선언적 소스 — **그대로 소비** |
| `config.build_invocation` | `config.py:273` | **이미 vendor 중립 `(argv, env)`** — claude / upstage(CLI 없으면 `ValueError`) / 일반 CLI(`command` 템플릿 `{prompt}` 치환) 3갈래 | **저수준 seam 의 자연 정착점**; AgentBackend 의 CLI 변형이 이걸 감싼다(§4) |
| `solar_model_for` / `account_for` | `config.py:208` / `:218` | vendor별 모델 매핑(예), vendor 필터 라운드로빈 — 이미 중립 | **재사용** |
| `claude_auth.claude_env` | `claude_auth.py:104` | `CLAUDE_CODE_OAUTH_TOKEN` 주입(없으면 상속분 제거) — claude OAuth/keychain 전용 | claude 백엔드의 env-builder; 다른 vendor 는 `Account.env` + `accounts.read_api_key` |

### (D) 모델·effort 사다리 — `todo_system/orchestrator/model_select.py`

| 결합 지점 | 파일:줄 | 무엇이 Claude 전용인가 | 추상화 방향 |
|---|---|---|---|
| `LADDERS` / `TIER_EFFORT` | `model_select.py:54` / `:64` | claude 모델 id 튜플 + claude effort 레벨(max/high/medium) | claude 백엔드 내부로; vendor별 사다리는 `Account.models` |
| `_probe` / `_UNAVAILABLE_MARKERS` / `resolve_model` | `model_select.py:116` / `:91` / `:225` | `claude -p ok --model M --effort E --max-turns 1` 프로브 + claude 쿼터 문구 매칭 | **claude 전용 유지**(설계 결정 Q2: 초기엔 claude 만 프로브·페이싱) |
| `classify_tier` | `model_select.py:271` | **vendor 중립**(태그·estimate 기반 tier 판정) | **공유** — 백엔드와 무관 |

### (E) 사용량 페이싱 — `todo_system/usage_guard.py`

| 결합 지점 | 파일:줄 | 무엇이 Claude 전용인가 | 추상화 방향 |
|---|---|---|---|
| `usage_guard` (전 모듈) | `usage_guard.py:94` (`USAGE_URL`) | `https://api.anthropic.com/api/oauth/usage` 를 읽어 5h/7d 윈도우로 자동화 페이싱 — claude **구독 전용** | **claude 전용 유지**; 비-claude 백엔드는 페이싱 없이 돌리거나(설계 결정 Q2) 자기 레이트리밋 신호 사용 |

### (F) 프롬프트 소스 · (G) planner

| 결합 지점 | 파일:줄 | 무엇이 Claude 전용인가 | 추상화 방향 |
|---|---|---|---|
| 스테이지 프롬프트 소스 `.claude/commands/worker-<stage>.md` | `stages.py:50` `COMMANDS_DIR` | 디렉토리 이름이 claude 규약(`.claude/`)이나 내용은 일반 지침 | 능력 "프롬프트 전달"로 노출(§2-(8)) — CLI 백엔드는 전체 프롬프트, 축약 payload 백엔드(solar)는 summary/outcome/next_action |
| planner 호출 `scripts/planner_tick.sh` | `planner_tick.sh:330`/`:341` (`claude -p "/worker-planner" --model …`); checkout 리스 `:289` `Worker/checkout.lock` | planner 도 `claude -p` 슬래시 커맨드로 돈다 | **범위 외** — planner 는 스테이지 밖 레인. 이 설계는 결합점으로 기록만 하고, 구현은 스테이지 백엔드부터(planner 백엔드화는 별도 후속) |

### 서술 앵커(이 태스크가 바꾸지 않음, 구현 태스크가 갱신)

- `docs/concepts.md:28–29`, `README.md:31–32` — "executors for other coding agents are **planned** / 계획 중". 구현 태스크([[macp-agent-backends-2-implement]] AC #4)가 실제 지원 범위로 갱신한다. 이 설계 노트는 참조만.

<!-- end -->
