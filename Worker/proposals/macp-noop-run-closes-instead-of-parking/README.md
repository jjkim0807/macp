# 프롬프트 배선 제안 — '바꿀 것 없음' 실행은 파킹 대신 닫기 (사람이 적용)

헤드리스 워커는 `.claude/**` 를 쓸 수 없다(하드 가드). 이 가드는 리포 루트의 `.claude/`
뿐 아니라 **중첩된** `Worker/proposals/<slug>/.claude/commands/` 경로의 Write 도
"sensitive file" 로 거부한다([[claude-dir-gate]]). 그래서 제안본을 `.claude` 없는
`commands/` 아래 두고, 사람이 아래 한 줄로 반영한다.

> **이 제안은 "추가만" 했다.** 두 프롬프트의 기존 문구는 한 글자도 바꾸지 않고, 출력
> frontmatter 에 `no_op: true | false` 한 줄씩과 그 필드를 언제 쓰는지 설명하는 문단
> 하나씩만 끼워 넣었다. 확인:
> `git diff --no-index .claude/commands/<name>.md Worker/proposals/macp-noop-run-closes-instead-of-parking/commands/<name>.md`
> 가 두 파일 모두 `+` 줄만 보여준다(수정·삭제 줄 없음 — 이 run 에서 실측 확인).

## 매핑 (제안본 → 실제 목적지)

| 제안본 (`.claude` 없음) | 실제 목적지 | 추가된 것 |
|---|---|---|
| `commands/worker-quick.md` | `.claude/commands/worker-quick.md` | frontmatter `no_op:` 줄 + `overall_status` 설명 뒤 규칙 문단 |
| `commands/worker-report.md` | `.claude/commands/worker-report.md` | frontmatter `no_op:` 줄 + `merged: no` 섹션 뒤 `#### no_op: true` 소절 |

## 적용 (사람이, 엔진 체크아웃 `~/Projects/macp` 에서)

```bash
cp Worker/proposals/macp-noop-run-closes-instead-of-parking/commands/worker-quick.md  .claude/commands/worker-quick.md
cp Worker/proposals/macp-noop-run-closes-instead-of-parking/commands/worker-report.md .claude/commands/worker-report.md
```

파일명이 목적지와 같으므로 한 번에:
`cp Worker/proposals/macp-noop-run-closes-instead-of-parking/commands/*.md .claude/commands/`.

> 선례: `.claude/**` 는 설정으로 열 수 없는 built-in 가드라 늘 이 proposals → 사람 `cp`
> 프로토콜을 쓴다([[claude-dir-gate]], PR #64; 엔진 프롬프트 제안은 macp 리포 PR 에 포함 — 선례 PR #109).

## 왜 — 엔진 변경만으로는 발화하지 않는다

이 태스크의 엔진 변경(`stages.no_op_run` 탐지 + runner 머지 암 + orchestrator 닫기 노트)은
마지막 스테이지 산출물의 `no_op: true` 신호를 읽는다. **에이전트가 그 신호를 쓰지 않으면
동작은 지금과 똑같다(여전히 파킹).** 위 두 프롬프트가 quick·report 에이전트에게 "검증된
0-커밋 no-op 일 때만 `no_op: true` 를 찍어라" 를 가르쳐야 엔진 경로가 켜진다.

안전장치(런너가 이미 강제):
- `no_op_run` 은 **신호 + `origin/<default>..<branch>` 0커밋** 둘 다여야 True 다 — 신호만으로는 안 닫는다.
- verify FAIL / `blocked_on` 은 머지 암에 **도달하기 전에** return 하므로, 실패·블록 런에
  no-op 를 찍어도 닫히지 않는다. 프롬프트 규칙은 그 위의 예의일 뿐, 런너가 구조적으로 막는다.
