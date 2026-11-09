# 프롬프트 배선 제안 — memory v2 · G4b (사람이 적용)

헤드리스 워커는 `.claude/**` 를 쓸 수 없다(하드 가드). 이 가드는 리포 루트의 `.claude/` 뿐
아니라 **중첩된** `Worker/proposals/<slug>/.claude/commands/` 경로의 Write 도 "sensitive
file" 로 거부한다. 그래서 제안본을 `.claude` 없는 `commands/` 아래 두고, 사람이 아래 한 줄로
반영한다([[claude-dir-gate]], PR #64 이후 늘 쓰는 프로토콜).

> **이 제안은 "추가만" 했다.** 세 프롬프트의 기존 문구는 한 글자도 바꾸지 않고 한 줄씩만
> 끼워 넣었다. 확인:
> `git diff --no-index .claude/commands/<name>.md Worker/proposals/macp-graph-memory-v2-g4b-engine-injected-wiring/commands/<name>.md`
> 가 세 파일 모두 `+` 줄 하나만 보여준다(수정·삭제 줄 없음).

## 매핑 (제안본 → 실제 목적지)

| 제안본 (`.claude` 없음) | 실제 목적지 |
|---|---|
| `commands/worker-research.md` | `.claude/commands/worker-research.md` (Required reading 에 step **3b**) |
| `commands/worker-planner.md` | `.claude/commands/worker-planner.md` (Execution steps 에 step **6c**) |
| `commands/brief.md` | `.claude/commands/brief.md` (Required context 에 step **5b**) |

## 적용 (사람이, 엔진 체크아웃 `~/Projects/macp` 에서)

```bash
cp Worker/proposals/macp-graph-memory-v2-g4b-engine-injected-wiring/commands/*.md .claude/commands/
```

파일명이 목적지와 같아서 한 번에 된다. 개별로 하려면:

```bash
cp Worker/proposals/macp-graph-memory-v2-g4b-engine-injected-wiring/commands/worker-research.md .claude/commands/worker-research.md
cp Worker/proposals/macp-graph-memory-v2-g4b-engine-injected-wiring/commands/worker-planner.md  .claude/commands/worker-planner.md
cp Worker/proposals/macp-graph-memory-v2-g4b-engine-injected-wiring/commands/brief.md           .claude/commands/brief.md
```

## 이 `cp` 없이도 엔진은 안전하다 (그러나 절반만 쓸모 있다)

반영을 미뤄도 **아무것도 깨지지 않는다** — 이번 리프의 엔진 쪽 변경은 전부 독립적이다:

- `Worker/runs/<run-id>/related.md` 는 러너가 이미 쓴다. 프롬프트가 그 파일을 모르면
  research 에이전트가 그냥 **읽지 않는다**(무해).
- planner·brief 의 "## 관련 과거 기록" 섹션은 **프롬프트 자체에 들어간다**(`planner_tick.sh` ·
  `brief.sh`). 그래서 이 `cp` 없이도 지금 바로 동작한다 — 이 제안은 에이전트에게 그 섹션을
  **어떻게 쓰라**고 말해 주는 것뿐이다.

즉 `cp` 가 실제로 바꾸는 건 **research 경로 하나**다. 그것 없이 머지되면 엔진이 매 run 마다
파일을 만들지만 아무도 열지 않는다 — 전례가 있다([[macp-prompt-proposals-need-human-cp]]:
제안이 커밋만 되고 `cp` 가 안 되어 엔진이 절반만 동작한 채 no_op 로 돌았다).

## 옛 제안(`macp-graph-memory-v2-g4-wiring-eval`)을 왜 지웠나

그 디렉터리의 세 줄은 모두 에이전트에게 **직접 질의 명령을 실행하라**고 했다:

```
python -m todo_system.orchestrator.eval_memory query --slug <slug> --title "<title>"
```

그 문장은 **실행될 수 없었다**. 세 소비자 중 둘은 Bash 자체가 없다:

- `STAGE_TOOLS["research"] = "Read Write"` (`todo_system/orchestrator/stages.py`) — 게다가
  `TASK_TOOLS_STAGES = ("implement",)` 라서 태스크의 `allowed_tools` 로도 못 넓힌다.
- `brief.sh` 의 `--allowed-tools` 는 `Read Write Edit` + `git log|diff|status` 뿐이다.
- `planner_tick.sh` 만 bare `Bash` 를 준다 — 거기서는 실행 *가능*했지만, 틱이 계산해 주는 쪽이
  결정적이고 비용이 틱에 묶인다.

그래서 이 리프가 배선을 **엔진 주입**으로 바꾸고(러너가 `related.md` 를 쓰고, 두 틱이 섹션을
프롬프트에 붙인다) 옛 제안 디렉터리를 `git rm -r` 했다. 평가 하니스의 `eval_memory query`
서브커맨드 자체는 **그대로 남아 있다** — 사람과 대화형 세션이 쓴다. 지운 것은 "에이전트가 그걸
실행하라"는 죽은 지시뿐이다.

## 세 줄이 공통으로 지키는 것 (R5)

주입되는 목록은 **슬러그·제목·출처 경로·점수·엣지 종류만** 담는다. 본문 발췌도, 태스크의
`outcome`·`next_action` 텍스트도 들어가지 않는다(그 둘은 질의 *입력*으로만 쓰인다).
더 필요한 에이전트는 `source` 경로를 직접 Read 한다 — 경계가 한 곳
(`todo_system/orchestrator/related_context.py`)에 있고 회귀 테스트가 지킨다.

## 되돌리기

`cp` 를 반영한 뒤 시끄러우면 프롬프트를 되돌리지 않아도 된다. 배선 전체가 env 두 개 뒤에 있다:

- `MACP_RELATED_CONTEXT=0` — `related.md` + planner·brief 섹션 전부 끈다.
- `MACP_EMBED_TICK=0` — maintenance 틱의 증분 임베딩을 끈다.
