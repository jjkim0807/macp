# 프롬프트 배선 제안 — memory v2 · G4 (사람이 적용)

헤드리스 워커는 `.claude/**` 를 쓸 수 없다(하드 가드). 이 가드는 리포 루트의 `.claude/`
뿐 아니라 **중첩된** `Worker/proposals/<slug>/.claude/commands/` 경로의 Write 도
"sensitive file" 로 거부한다(이 run 에서 실측 확인 — R1 열림질문 O.Q.1). 그래서 제안본을
`.claude` 없는 `commands/` 아래 두고, 사람이 아래 한 줄로 반영한다.

> **이 제안은 "추가만" 했다.** 세 프롬프트의 기존 문구는 한 글자도 바꾸지 않고, "관련 과거
> 기록 질의 (memory v2)" 단계 **한 줄**씩만 끼워 넣었다. 확인:
> `git diff --no-index .claude/commands/<name>.md Worker/proposals/macp-graph-memory-v2-g4-wiring-eval/commands/<name>.md`
> 가 세 파일 모두 `+` 줄 하나만 보여준다(수정·삭제 줄 없음).

## 매핑 (제안본 → 실제 목적지)

| 제안본 (`.claude` 없음) | 실제 목적지 |
|---|---|
| `commands/worker-research.md` | `.claude/commands/worker-research.md` (Required reading 에 step **3b** 추가) |
| `commands/worker-planner.md` | `.claude/commands/worker-planner.md` (Execution steps 에 step **6c** 추가) |
| `commands/brief.md` | `.claude/commands/brief.md` (Required context 에 step **5b** 추가) |

## 적용 (사람이, 엔진 체크아웃 `~/Projects/macp` 에서)

```bash
cp Worker/proposals/macp-graph-memory-v2-g4-wiring-eval/commands/worker-research.md .claude/commands/worker-research.md
cp Worker/proposals/macp-graph-memory-v2-g4-wiring-eval/commands/worker-planner.md  .claude/commands/worker-planner.md
cp Worker/proposals/macp-graph-memory-v2-g4-wiring-eval/commands/brief.md           .claude/commands/brief.md
```

파일명이 목적지와 같으므로 한 번에: `cp Worker/proposals/macp-graph-memory-v2-g4-wiring-eval/commands/*.md .claude/commands/`.

> 선례: `.claude/**` 는 설정으로 열 수 없는 built-in 가드라 늘 이 proposals → 사람 `cp`
> 프로토콜을 쓴다([[claude-dir-gate]], PR #64).

## 추가된 단계가 가리키는 질의 표면

세 단계 모두 **이 leaf 가 새로 만든 in-scope CLI** 하나를 가리킨다(신규 `graph_memory.py`
질의 CLI 를 만들지 않는다 — G4 `touches` 밖):

```bash
python -m todo_system.orchestrator.eval_memory query --slug <slug> --title "<title>" [--touches ...]
```

sqlite-vec 가 없으면 자동으로 BFS-only 로 강등되고(절대 raise 안 함), 볼트에 읽기 전용이다.

## AC#2 — "권장" 은 조건부다 (측정이 가설을 반박할 수 있음)

**이 배선을 "권장" 으로 적용할지는 적중률 평가 결과에 달렸다.** 사람 골든셋을 확정한 뒤
평가 표를 돌려:

```bash
python -m todo_system.orchestrator.eval_memory table --golden <golden.json> --k 5,10
```

- 표 하단 판정이 **`배선 제안: 권장`** (v2 가 recall@10 을 올리고 precision@5 를 내리지
  않음)일 때만 이 세 `cp` 를 **권장**으로 반영한다.
- **`미권장`** 이면(= v2 가 v1 을 유의하게 넘지 못함) 숫자를 그대로 보고하고 반영을 보류한다 —
  G3/G4 가 임베딩 모델·`k`·blend 가중치를 재조정하거나 설계를 되돌린다(설계 §5.2).

골든셋 확정 전에는 `emit-candidates` 가 뽑아 준 **약한 라벨(참고)** 스캐폴드로만 돌릴 수 있고,
그 결과 표는 "약한 라벨(참고)" 캡션을 달고 나온다(v1 에 유리한 편향이 있으니 참고용):

```bash
python -m todo_system.orchestrator.eval_memory emit-candidates --out candidates.json --limit 50
# → candidates.json 의 각 task 에서 relevant 를 사람이 검토·가지치기 → status 를 golden 으로
```
