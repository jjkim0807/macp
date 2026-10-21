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

---

## §2 스테이지가 에이전트에 요구하는 능력 (8항목)

스테이지 하나를 돌리려면 백엔드가 아래 8가지를 제공해야 한다. 능력 이름은 태스크
AC 문구를 그대로 쓴다(Verify 체크리스트와 1:1). 각 항목은 **오늘의 Claude 기제**와
**중립 백엔드가 노출해야 할 것**을 짝지었고, 끝에 그 능력이 **오늘 claude 구독 전용**
인지 **이미 vendor 중립**인지를 표시한다.

1. **헤드리스 프롬프트 실행** — 비대화형으로 프롬프트 1개를 받아 끝까지 자율 실행.
   - *Claude 기제*: `claude -p <full_prompt>` (`stages.py:491`, 조립 `:490` `full = prompt + meta`). `-p`(= `--print`) 가 헤드리스 모드.
   - *중립 백엔드*: `run_stage(stage, prompt, …)` — 프롬프트를 받아 1회 비대화 실행하고 종료코드·stdout 를 돌려주는 진입점. **모든 백엔드의 필수 능력**(없으면 후보 탈락).
   - 상태: vendor 중립 가능(모든 후보 CLI 가 헤드리스 서브커맨드를 가짐 — §3).

2. **도구 허용 목록** — 스테이지가 쓸 수 있는 셸/파일 도구를 화이트리스트로 제한.
   - *Claude 기제*: `--allowed-tools Read Edit Write "Bash(git:*)" …` (`stages.py:499`); 목록은 `STAGE_TOOLS`(`:259`)에서 오고 `_safe_tools`(`:329`)가 `FORBIDDEN_TOOL_SUBSTRINGS`(`:303`)로 거른다. `Bash(git:*)` 는 claude 고유의 "툴명(글롭)" 문법.
   - *중립 백엔드*: 능력 `supports("tool_allowlist")`. 지원하면 자기 표기로 번역, **미지원이면 조용히 무시하지 말고 디스패치 전에 거절/경고**(구현 AC #3 — 읽기전용이어야 할 research/verify 의 안전 보장이 조용히 증발하면 안 된다).
   - 상태: claude 는 세밀(per-tool glob), 다수 후보는 **거친 승인 모드**(allow-all / per-command)뿐 — 능력 격차가 가장 큰 항목.

3. **턴/시간 상한** — 한 스테이지가 무한정 돌지 않도록 바운드.
   - *Claude 기제*: `--max-turns`(`stages.py:493`, `STAGE_MAX_TURNS` `:284` — implement 200, verify 50…) + 바깥의 `run_stage_subprocess(timeout=stage_timeout)`(`runner.py:1095`, 벽시계).
   - *중립 백엔드*: 능력 "턴 상한"(있으면) + 공통 "시간 상한"(런너가 서브프로세스/호출을 벽시계로 reap — 백엔드 독립). 턴 개념 없는 백엔드는 시간 상한만.
   - 상태: 시간 상한은 vendor 중립(런너가 강제). 턴 상한은 claude 고유 — 능력으로 노출하되 선택적.

4. **모델·effort 선택** — 태스크 tier 에 맞는 모델과 추론 강도 지정.
   - *Claude 기제*: `--model <id> --effort <max|high|medium>` (`stages.py:492`); id·effort 는 `stage_model`/`stage_effort`(`:85`/`:95`) → `model_select.LADDERS`/`TIER_EFFORT`(`:54`/`:64`)에서, tier 는 `classify_tier`(`:271`, **중립**)에서.
   - *중립 백엔드*: tier→모델은 `Account.models`(`config.py:69`, 이미 중립, 예 `{"light":"solar-mini4-260922"}`) 또는 백엔드 기본. `effort` 는 **claude 전용 개념** — 능력 없으면 생략(미지원을 거절까진 안 함; 품질만 영향).
   - 상태: tier 판정(classify_tier)은 중립. claude 모델 사다리·effort·가용성 프로브(`_probe` `:116`)는 **구독 전용**.

5. **작업 디렉터리·추가 디렉터리** — 코드를 수정할 checkout(cwd)과 읽기 허용 추가 경로.
   - *Claude 기제*: 외부 리포는 cwd=볼트 루트에서 프롬프트 메타의 `working_dir`(`stages.py:464`)로 안내 + `--add-dir <worker_root>`(`:502`)로 볼트 run_dir 아티팩트를 읽게 함.
   - *중립 백엔드*: 능력 "cwd 설정"(모든 CLI 필수) + "추가 읽기 경로"(선택). `--add-dir` 대응물이 없으면 프롬프트에 경로를 적어 안내하거나 미지원 표기.
   - 상태: cwd 는 vendor 중립. add-dir(샌드박스 밖 경로 허용)은 에이전트별 상이.

6. **종료 의미 (성공 / 쿼터 소진 / 오류)** — 종료코드+출력을 세 갈래로 해석.
   - *Claude 기제*: `rc==0` = 성공; 단 claude 는 **쿼터 소진에도 rc=0** 을 내므로(`model_select.py:86–90` 사건 주석) 출력을 `_UNAVAILABLE_MARKERS`(`:91`)/`stages.is_capacity_limit`로 함께 본다. 소진이면 `_maybe_rotate_account`(`runner.py:371`)가 계정 회전. 그 외 rc≠0 = 오류.
   - *중립 백엔드*: 능력 `classify_exit(rc, stdout) → success|exhausted|error`. "쿼터 소진"의 신호는 vendor마다 다르다(claude=문구+rc0, upstage=`rate_limit` 토큰, OpenAI=HTTP 429…). **백엔드가 자기 신호를 공통 3갈래로 환원**.
   - 상태: "성공/오류" 2갈래는 중립. "쿼터 소진" 감지와 계정 회전은 claude 구독 모델 특화 — Solar 는 `usage_rec=None` 으로 이 arm 을 아예 비활성(`runner.py:1051–1052`).

7. **사용량 보고** — 스테이지가 쓴 토큰을 `stages.json` 에 기록.
   - *Claude 기제*: `--session-id <uuid>`(`stages.py:498`, `MACP_STAGE_USAGE!=0` 일 때) 로 transcript 를 만들고 `record_stage_usage` 가 그 transcript 를 되읽어 `USAGE_KEYS`(`:375` input/output/cache_* 토큰) dict 로 저장.
   - *중립 백엔드*: 능력 "사용량 보고". transcript read-back 은 claude 고유이므로, 백엔드는 자기 응답의 토큰 카운트를 **같은 `USAGE_KEYS` 모양 dict** 로 반환하면 된다. 없으면 `None`(비활성, 경보 아님).
   - 상태: claude transcript 경로는 구독·CLI 전용. "토큰 dict 반환" 계약은 중립 — Solar 가 이미 집계하되 `usage_rec=None` 로 넘긴다.

8. **프롬프트(.claude/commands) 전달** — 스테이지 지침을 에이전트에 어떻게 넣는가.
   - *Claude 기제*: `load_stage_prompt`(`stages.py:309`)가 `.claude/commands/worker-<stage>.md` 전체를 읽어 `strip_frontmatter` 후 run 메타와 합쳐 `-p` 로 통째 전달.
   - *중립 백엔드*: 두 모양이 이미 공존한다 — (a) **전체 프롬프트** 전달(CLI 백엔드: claude/codex/… 가 긴 지시문을 그대로 받음), (b) **축약 payload**(`_solar_task_prompt` `runner.py:238` = summary/outcome/next_action 만, HTTP 모델이 도구루프 시스템 프롬프트로 감쌈). 백엔드가 자기에 맞는 모양을 고르되, **디렉토리 이름 `.claude/`에 묶이지 않도록** 프롬프트 소스를 백엔드와 분리.
   - 상태: `.md` 읽기는 중립. 어떤 모양으로 넣느냐는 백엔드 특성(전체 vs 축약).

### 요약: 오늘 claude 구독 전용 vs 이미 vendor 중립

| 능력 | claude 구독/CLI 전용 | 이미 vendor 중립(재사용) |
|---|---|---|
| (1) 헤드리스 실행 | `-p` 플래그 | 개념 자체(모든 CLI 지원) |
| (2) 툴 허용목록 | `Bash(git:*)` 세밀 문법 | `_safe_tools`/`FORBIDDEN_TOOL_SUBSTRINGS` 필터 로직 |
| (3) 턴/시간 상한 | `--max-turns` (턴) | 벽시계 timeout(런너가 강제) |
| (4) 모델·effort | `LADDERS`·`TIER_EFFORT`·`_probe`·`--effort` | `classify_tier`·`Account.models` |
| (5) cwd·add-dir | `--add-dir` | cwd 지정 |
| (6) 종료 의미 | 쿼터 소진 rc0+문구, `_maybe_rotate_account` | success/error 2갈래, `classify_exit` 계약 |
| (7) 사용량 보고 | `--session-id` transcript read-back | `USAGE_KEYS` dict 반환 계약 |
| (8) 프롬프트 전달 | — | `.md` 읽기 + 전체/축약 선택(`_solar_task_prompt` 선례) |

→ **설계 함의**: 구현 1차 범위를 "claude 구독 전용" 열(모델 사다리·`_probe`·usage_guard 페이싱·쿼터회전)은 **claude 백엔드 안에 그대로 두고**, "중립" 열만 인터페이스로 끌어올리면 비-Claude 백엔드 하나를 **페이싱·effort 없이** 최소로 꽂을 수 있다(§4 결정 Q2).

---

## §3 후보 에이전트 대조표 (능력 vs 공식 문서)

각 칸은 **그 능력의 지원 여부 + 공식 플래그**이고, 이번 세션(**조회 2026-10-22**)에
공식 문서로 확인한 것만 적는다. 공식 페이지로 확인하지 못한 칸은 **`미확인`**(추측
금지). 출처 URL 은 표 아래에 에이전트별로 모았다. **CLI 플래그·권한 모델은 버전마다
바뀌므로** 구현 태스크는 착수 시점에 각 공식 레퍼런스를 재확인해야 한다.

범례: ✅ 공식 확인 · ⚠️ 부분/제약 있음(셀 안에 명시) · 미확인 = 이번 세션 공식 문서로 확인 못 함.

| 에이전트 | (1) 헤드리스 호출 | (2) 권한·툴 허용목록 | (3) 턴/시간 상한 | (4) 모델 선택 | (5) 사용량 보고 | (6) 프롬프트·지시 전달 |
|---|---|---|---|---|---|---|
| **OpenAI Codex CLI** | ✅ `codex exec "<prompt>"` (stderr=진행, stdout=최종); `codex exec -`=stdin | ⚠️ `--sandbox` {read-only(기본)·workspace-write·danger-full-access}, `--approve-for-me`; `--full-auto` 폐지. **per-tool 허용목록(Bash(git:*) 대응) 미확인**(non-interactive 페이지 미기재) | 미확인 (non-interactive 페이지 미기재) | 미확인 (`-m/--model` 이 non-interactive 공식 페이지엔 미기재) | ✅ `--json` JSONL 의 `turn.completed.usage` = {input_tokens, cached_input_tokens, output_tokens, reasoning_output_tokens} | ✅ positional arg 또는 stdin(`-`); `--output-schema <file>`(JSON Schema 강제); `-o/--output-last-message`; `--ignore-user-config`=`$CODEX_HOME/config.toml` 스킵 |
| **Gemini CLI** | ✅ `-p`/`--prompt` (non-TTY 도 헤드리스 트리거) | ✅ `--approval-mode` {default·auto_edit·plan} + `--yolo`(=`--approval-mode=yolo`); **툴 허용목록 있음**: `tools.core`(allowlist)·`tools.exclude`·`tools.allowed`(확인창 bypass) 설정 | ✅ `model.maxSessionTurns`(기본 -1=무제한) + 초과 시 **exit 53 "Turn limit exceeded"** | ⚠️ `model.name` 설정 / `GEMINI_MODEL` env (공식); **`-m/--model` CLI 플래그는 공식 config 레퍼런스엔 미확인** | ✅ `--output-format json` → `stats` 토큰 사용량 + per-model 토큰 분해 | ✅ `-p`/`--prompt` 로 쿼리; `GEMINI.md`/stdin 은 헤드리스 페이지 미기재(미확인) |
| **Cursor CLI** (`cursor-agent`) | ✅ `-p`/`--print` (non-TTY/파이프도 추론) | ⚠️ `-f`/`--force`(=`--yolo`) "명시적 deny 외 전부 허용"; `--sandbox` {enabled·disabled}; `--mode` {plan·ask}; `--trust`(헤드리스 전용); `--approve-mcps`. **`--allowedTools`/`--deniedTools` 미확인**(parameters 페이지 미기재) | 미확인 (agent 턴/시간 바운드 미기재; `--idle-release-timeout` 은 worker 전용) | ✅ `--model <model>`(`-m` 단축형은 미확인); `agent models`·`--list-models` 로 목록 | 미확인 (`--output-format json` 은 `result` 필드; **토큰 사용량 필드 미기재**) | ✅ positional arg; `--output-format` {text(기본)·json·stream-json}(`--print` 와만) |
| **aider** | ✅ `--message`/`--msg`/`-m COMMAND`(응답 처리 후 종료, chat 비활성); `--message-file`/`-f FILE` | ⚠️ `--yes-always`(모든 확인에 yes) — **per-tool 허용목록 없음**(all-or-nothing); `--auto-commits`(기본 true) | 미확인 (턴/반복 상한 플래그 미기재) | ✅ `--model`(env `AIDER_MODEL`) | 미확인 (스크립트용 토큰 보고 플래그 없음; `/tokens` 은 in-chat, `--max-chat-history-tokens` 은 report 아님) | ✅ `--message` 인라인 / `--message-file`/`-f` / env `AIDER_MESSAGE`·`AIDER_MESSAGE_FILE` |
| **OpenCode** | ✅ `opencode run [message..]` (스크립팅/자동화 목적) | ⚠️ `run --auto`(명시적 deny 외 자동 승인); `agent create --permissions`(allowlist, 생략=deny) — **허용목록은 agent 설정 경유** | 미확인 (`run` 턴/시간 상한 미기재; `OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS` 는 bash 전용) | ✅ `--model`/`-m` (`provider/model` 형식); `models` 로 목록 | ⚠️ `opencode stats`(세션 토큰/비용, **별도 커맨드**); `run` 자체 인라인 토큰 출력은 미확인 | ✅ positional `[message..]`; `--agent`; `--command`; `--file`/`-f`; `--format` {default·json} |

### 출처 (조회 2026-10-22; 전부 공식 문서)

- **OpenAI Codex CLI**: [learn.chatgpt.com/docs/non-interactive-mode](https://learn.chatgpt.com/docs/non-interactive-mode) (= `developers.openai.com/codex/noninteractive` 가 308 리다이렉트하는 공식 non-interactive 가이드).
- **Gemini CLI**: 헤드리스 [github.com/google-gemini/gemini-cli · docs/cli/headless.md](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/headless.md) · 설정 [docs/reference/configuration.md](https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/configuration.md).
- **Cursor CLI**: 헤드리스 [cursor.com/docs/cli/headless](https://cursor.com/docs/cli/headless) · 파라미터 [cursor.com/docs/cli/reference/parameters](https://cursor.com/docs/cli/reference/parameters) · 출력 포맷 [cursor.com/docs/cli/reference/output-format](https://cursor.com/docs/cli/reference/output-format).
- **aider**: 스크립팅 [aider.chat/docs/scripting.html](https://aider.chat/docs/scripting.html) · 옵션 [aider.chat/docs/config/options.html](https://aider.chat/docs/config/options.html).
- **OpenCode**: CLI 레퍼런스 [opencode.ai/docs/cli](https://opencode.ai/docs/cli).

### 읽는 법 (구현 태스크용 요약)

- **헤드리스 실행(§2-1)·프롬프트 전달(§2-8)·모델 선택(§2-4)** 은 다섯 후보 모두 공식 지원(Gemini 의 CLI `--model` 플래그만 공식 config 페이지 미확인 — 설정/ env 로는 확인). 즉 **최소 백엔드(effort·페이싱 없이)는 어느 후보로도 가능**.
- **per-tool 허용목록(§2-2, Claude `Bash(git:*)` 대응)** 이 능력 격차가 가장 크다. **Gemini(`tools.core`)·OpenCode(`agent --permissions`)** 만 허용목록형을 공식 제공. Codex·Cursor 는 **sandbox/force 수준의 거친 승인**이고 per-tool allowlist 는 미확인, aider 는 **없음(`--yes-always` all-or-nothing)**. → 구현 AC #3("미지원 능력은 조용히 무시 말고 거절/경고")이 바로 이 격차를 방어한다.
- **사용량 보고(§2-7)** 는 **Codex(`--json` usage)·Gemini(`--output-format json` stats)** 가 인라인 토큰을 공식 제공. OpenCode 는 별도 `stats` 커맨드, Cursor·aider 는 미확인 → 이 둘은 `usage_rec=None`(Solar 와 동일) 로 꽂으면 된다.
- **턴 상한(§2-3)** 은 **Gemini(`maxSessionTurns`+exit 53)** 만 공식. 나머지는 미확인이나, **벽시계 timeout 은 런너가 백엔드 밖에서 강제**하므로(§2-3) 치명적이지 않다.

---

## §4 `AgentBackend` 인터페이스 초안

### 결정: altitude (Open Q1)

인터페이스는 **저수준 `(argv, env)` 빌더가 아니라 고수준 `run_stage(...) →
CompletedProcess`** 로 둔다. 이유: 추상화가 수용해야 할 **두 실제 모양**이 이미
그 높이에서 만난다 —

- **CLI 서브프로세스**: `config.build_invocation`(`:273`)이 `(argv, env)` 를 만들고
  런너가 `run_stage_subprocess(timeout)`(`runner.py:1095`)로 돌려 `CompletedProcess` 를 얻는다.
- **엔진 내 HTTP**: `solar_stage.run`(`:257`)은 서브프로세스 없이 도구루프를 돌고
  **`CompletedProcess`-shape 를 직접 반환**(`:294`/`:390`)하며 자기 artifact 를 쓴다.

`(argv, env)` 만 감싸면 HTTP 모양이 안 들어온다. 공통 분모는 **"스테이지 하나를
돌리고 `returncode`/`stdout` 를 주고 artifact 를 남긴다"** — 그 높이가
`runner.py:1046–1066` 의 `use_solar` 분기가 이미 두 경로를 합류시키는 바로 그 지점이다.

### 인터페이스 (메서드 시그니처 초안)

```python
# todo_system/orchestrator/agent_backend.py (신규, 구현 태스크가 생성)
from typing import Protocol
from pathlib import Path
import subprocess

class AgentBackend(Protocol):
    name: str  # vendor id: "claude" | "upstage" | "codex" | "gemini" | ...

    def supports(self, capability: str) -> bool:
        """이 백엔드가 명명된 스테이지 능력(§2)을 이행할 수 있는가.
        capability ∈ {"headless","tool_allowlist","turn_cap","model_select",
        "effort","add_dir","usage_report","exit_quota"}.
        디스패처가 스테이지 실행 '전에' 호출한다. 태스크가 요구하는 능력을
        백엔드가 지원하지 않으면 조용히 버리지 말고 거절/경고한다(구현 AC #3)."""

    def run_stage(self, stage: str, *, task_id: str, run_id: str, run_dir: Path,
                  branch: str, repo: str | None, working_dir: str | None,
                  auto_merge: bool, account, tier: str | None,
                  allowed_tools: str | None, project_context: str,
                  retry_context: str, env: dict, timeout: float,
                  progress) -> subprocess.CompletedProcess:
        """스테이지 1개를 헤드리스 실행하고 CompletedProcess-shape(returncode +
        stdout)를 반환하며, 스테이지 artifact(stages.STAGE_ARTIFACT[stage])를
        run_dir 아래에 쓴다. 이 모양은 solar_stage.run 과 `claude -p` 서브프로세스가
        '이미' 둘 다 내는 것이라, 런너의 공유 tail(r.returncode/r.stdout →
        run.log·용량 체크·artifact 체크)이 무수정으로 읽는다.
        시그니처 키워드는 stages.build_stage_argv 의 인자 집합과 1:1 (drop-in)."""

    def classify_exit(self, rc: int, stdout: str) -> str:
        """'success' | 'exhausted' | 'error' (§2-6). Claude: rc==0 且
        model_select._UNAVAILABLE_MARKERS 부재 = success; 마커 있으면 exhausted
        (→ 계정 회전). 쿼터 개념 없는 백엔드는 success/error 만."""

    def report_usage(self, stage: str, run_dir: Path, handle) -> dict | None:
        """stages.USAGE_KEYS 모양의 토큰 dict, 또는 사용량 채널이 없으면 None
        (Solar 가 usage_rec=None 을 넘기듯 — None 이면 rc==0 용량 arm 이 inert)."""
```

CLI 계열(claude/codex/cursor/gemini/aider/opencode)은 이 Protocol 을 만족하는 **하나의
`CliAgentBackend`** 로 묶는다: `run_stage` 가 (1) `stages.load_stage_prompt`+메타로 full
프롬프트를 만들고, (2) `argv, env = config.build_invocation(account, full, extra_args)`
— 여기서 `extra_args` 는 **vendor 별로 번역된 플래그**(claude 는 아래 바이트 동일
세트, 다른 vendor 는 `supports()` 가 true 인 것만) — (3) `run_stage_subprocess` 로
실행. HTTP 계열(upstage)은 `solar_stage.run` 을 감싸는 `SolarAgentBackend`.

### Claude 백엔드 불변식: argv/env **바이트 동일** (필수 조건)

Claude 백엔드의 `run_stage` 는 오늘 `build_stage_argv`+`_run` 이 만드는 argv·env 를
**바이트 동일**하게 재현해야 한다. 구체 고정점:

- **argv (순서 포함)** — `stages.py:491–503`:
  `["claude","-p", full, "--model", <m>, "--effort", <e>, "--max-turns", <N>]`
  `(+ ["--session-id", <uuid>] if usage_enabled())` `+ ["--allowed-tools", *tools]`
  `(+ ["--add-dir", <worker_root>] if exists)`. **`--session-id` 는 가변인자
  `--allowed-tools` 앞**(`:496–499` 주석), `--add-dir` 는 맨 끝. 이 순서가 깨지면 바이트
  동일이 아니다.
- **env** — `runner.py:1100` `stages.with_browser_owner(stage, stage_env, run_id)`
  위에 claude 경로는 `claude_auth.claude_env()`(`:148`, `CLAUDE_CODE_OAUTH_TOKEN` 주입)
  와 per-account `CLAUDE_CONFIG_DIR`(`_account_env` `:301`)·bash 타임아웃이 얹힌다.
- **프롬프트 본문** — `strip_frontmatter(load_stage_prompt(stage))` + 런 메타 블록
  (`stages.py:412–490`). 한 글자도 달라지면 안 된다.

**오늘 이 argv 전체를 핀하는 테스트는 없다** — `build_stage_argv` 는
`test_claude_accounts_switch.py:964` 에서 **mock(`return_value=["true"]`)으로만** 쓰이고
출력을 검증하는 골든 테스트가 없다. 따라서 **구현 첫 leaf 는 리팩터 전에 먼저
"현재 claude 스테이지 argv 를 스테이지별로 캡처하는 골든/스냅샷 테스트"를 추가**해야
한다(그게 바이트 동일의 안전망이다 — §5 leaf ②-a).

반면 **이미 존재해 무수정 green 으로 유지해야 할** 바이트 동일 테스트(= Verify 가
확인할 것) — 전부 `todo_system/tests/test_macp_upstage_account.py`:
- `test_build_invocation_claude_path_unchanged`(`:245`) — `build_invocation(Account(name="x"),"hi")` == `["claude","-p","hi"]`, env 에 `CLAUDE_CONFIG_DIR` 없음.
- `test_build_invocation_never_launches_claude_for_an_upstage_account`(`:224`) — upstage 계정은 claude 로 안 샌다(ValueError).
- `test_claude_only_profile_output_is_byte_identical`(`:171`) — upstage 계정을 더해도 claude-only 프로필 TOML 바이트 동일.
- `test_account_for_round_robin_unchanged_by_an_upstage_account`(`:203`) — 계정 라운드로빈 불변.

### 나머지 Open Q 결정 (구현 태스크가 "설계대로" 따름)

- **Q2 — 모델 사다리·사용량 페이싱**: **초기엔 claude 전용으로 둔다.** `model_select`
  의 `LADDERS`/`_probe`/`resolve_model`(claude 모델·`claude -p` 프로브)과 `usage_guard`
  (`/api/oauth/usage` 구독 페이싱)는 claude 백엔드 안에 그대로 남기고, 비-Claude 백엔드는
  **effort 없이·페이싱 없이** 돌린다(vendor별 모델은 `Account.models`). `classify_tier`
  는 중립이라 공유. 이유: §2 요약표의 "claude 구독 전용" 열을 1차 범위에서 제외해야
  비-Claude 백엔드 1종을 최소 변경으로 꽂는다.
- **Q3 — 미지원 능력 처리**: **디스패치 시점에 거절/경고**(백엔드 `supports()`
  반환을 디스패처가 검사). 조용한 무시 금지 — 특히 `tool_allowlist` 미지원 백엔드가
  read-only 여야 할 research/verify 를 받으면 안 된다. 구현 AC #3 과 일치.
- **Q4 — 첫 레인**: **단일 스테이지 delegate/quick 부터**(Solar 선례와 동일 —
  `route_to_solar` 가 이미 그 두 스테이지만 비-claude 로 보낸다). 5-스테이지 전체
  파이프라인은 이후. 이유: `use_solar` 분기가 이미 quick/delegate 에서 비-claude 를
  수용하므로 가장 작은 안전한 시작점이고, §5 leaf 순서를 좌우한다.

<!-- end -->
