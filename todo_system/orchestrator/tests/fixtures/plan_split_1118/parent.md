---
tags:
  - todo
  - private
  - macp
  - worker
  - feature
status: open
due: "-"
updated: 2026-11-18
outcome: "macp 오케스트레이터가 같은 리포의 가벼운 태스크들을 각각 전체 게이트로 검증하지 않고, Verify에서는 빠른 검사(대상 테스트 + 컴파일)만 한 뒤 '묶음 대기'로 두었다가 최신 main 위에 여러 브랜치를 합친 통합 브랜치에서 전체 게이트를 한 번 돌려 통과하면 각 PR을 차례로 squash 머지한다. 실패·충돌이면 해당 태스크를 원래의 개별 Verify로 되돌린다. `uv run ruff check` + `uv run pytest` 로컬 green"
next_action: "macp `todo_system/orchestrator/stages.py`(Verify·Report 스테이지)·`orchestrator.py`(틱)·`merge_state.py`·`tasks.py`(frontmatter → payload, model_tier)를 읽고, '묶음 대기' 상태와 통합 게이트 잡이 들어갈 자리(틱 단계·잠금·상태 저장)를 implementation_plan에 적는다"
if_then: "워커가 이 태스크를 잡으면 → ① 대상: 같은 repo, worker_mode auto, auto_merge true, model_tier가 light/standard이거나 frontmatter `verify_batch: true`(명시적 `verify_batch: false`는 제외). 에뮬레이터 확인(emulator: true)이 필요한 태스크는 자기 Verify에서 그 확인까지 끝낸 경우만 ② Verify 모드 'quick': 태스크가 바꾼 파일과 관련된 대상 테스트 + 컴파일만(리포별 명령은 기존 레시피·.macp 설정에서), PASS면 state `batch_ready`로 두고 머지하지 않음 ③ 통합 게이트 잡(틱마다 확인, 리포 잠금): batch_ready가 2개 이상이거나 가장 오래된 것이 30분을 넘으면 최신 main에서 `integ/<repo>/<ts>` 브랜치를 만들어 각 브랜치를 차례로 머지 — 충돌 나는 브랜치는 빼고(그 태스크는 개별 Verify로 되돌림) 나머지로 진행. 리포의 전체 게이트(기존 Verify의 full 명령)를 한 번 실행 ④ 통과: 넣은 순서대로 각 PR을 squash 머지(머지 전 main이 통합 브랜치의 기준 sha 그대로인지 확인, 바뀌었으면 잡을 다시 시작), 태스크 md를 done, 통합 브랜치 삭제 ⑤ 실패: 묶음의 각 태스크를 개별 전체 Verify로 되돌림(어느 브랜치 탓인지 이분 탐색은 하지 않음 — 단순하게) ⑥ 이 호스트는 gradle 하나만 돌 수 있으니 통합 게이트도 기존 동시성 제한·remote gate 규칙을 그대로 따른다 ⑦ 대시보드·경보: batch_ready는 '묶음 검증 대기'로 보이고, 통합 게이트 실패는 한 번 알림 ⑧ 새 frontmatter 필드는 payload 배선까지(tasks.py) — 선언만 하고 안 읽는 필드 금지. 한 스테이지로 안 되면 rescope.json으로 나눈다(① 상태·quick Verify → ② 통합 게이트 잡 → ③ 대시보드·경보)"
estimate_p50: 6h
estimate_p90: 10h
review_cycle: "-"
priority: 75
worker_mode: auto
auto_merge: true
model_tier: heavy
max_turns: 200
repo: "https://github.com/example/macp.git"
acceptance_criteria:
  - "단위 테스트: 대상 판정(light/standard·verify_batch 필드·emulator 조건·auto_merge false 제외), batch_ready 전이, 통합 게이트 트리거(2개 이상 또는 30분), 충돌 브랜치 제외, 통과 시 순서대로 머지·md done, 실패 시 각 태스크 개별 Verify로 복귀, main이 움직이면 잡 재시작"
  - "통합 테스트(가짜 git 리포 + 가짜 게이트 명령): 브랜치 3개(그중 1개는 충돌)를 batch_ready로 두고 틱을 돌리면 통합 브랜치에 2개가 합쳐져 게이트 1회 실행 → 2개 PR 머지 호출 순서·인자 확인, 충돌 1개는 개별 Verify 대기열로 — 실제 틱 경로(orchestrator.py)를 거친다. 새 코드의 호출 위치를 verify.md에 적는다"
  - "`verify_batch` frontmatter가 payload까지 실제로 전달된다(tasks.py 배선 테스트)"
  - "기존 개별 Verify 경로(heavy 태스크·verify_batch: false)는 바뀌지 않는다 — 기존 테스트 green"
  - "대시보드에 '묶음 검증 대기' 상태가 웹·앱 같은 필드로 보인다(대시보드 동일성 규칙)"
  - "`check_public_identifiers.sh --export` + `check_engine_isolation.sh` + `uv run ruff check` + `uv run pytest` 로컬 green, run.log 인용"
touches:
  - "todo_system/orchestrator/stages.py"
  - "todo_system/orchestrator/orchestrator.py"
  - "todo_system/orchestrator/tasks.py"
  - "todo_system/orchestrator/runner.py"
  - "todo_system/orchestrator/merge_state.py"
  - "todo_system/orchestrator/statuses.py"
  - "todo_system/orchestrator/dashboard.py"
  - "todo_system/orchestrator/batch_gate.py"
---

# macp — 가벼운 태스크는 묶어서 한 번에 검증 (통합 게이트)

## Progress Log

- 2026-11-18: 생성
