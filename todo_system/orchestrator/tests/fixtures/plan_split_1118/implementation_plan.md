---
stage: plan
run_id: 20261118T123354-example-batch-verify-light-tasks
slug: example-batch-verify-light-tasks
generated: 2026-11-18T12:33:54
declares_oversize: yes
---

# Implementation Plan — macp — 가벼운 태스크는 묶어서 한 번에 검증 (통합 게이트)

> 약칭: `W` = 작업 worktree(리포 macp), `V` = 볼트(읽기 전용). 줄 번호는 계획 시점의 `W` 기준이다.

## 크기 판정 — 한 Implement 스테이지에 들어가지 않는다

**판정: `declares_oversize: yes`.** 아래 8개 Phase를 하위 태스크 3개로 나눠 차례로 머지한다.

- **규모.** 운영 코드 약 900줄(신규 `batch_gate.py` 약 500줄 포함)과 테스트 약 1,000줄, 파일 14개 안팎이다. 러너의 Verify→머지 판정(`runner.py` L1581–1756)과 오케스트레이터 틱(`orchestrator.py` L3407–3490)을 함께 고치고, 실제 git 리포를 만드는 통합 테스트를 `Orchestrator.tick()`까지 통과시켜야 한다.
- **한도.** 이 태스크는 heavy 티어라 Implement 한도가 `stages.STAGE_TIMEOUTS["heavy"]` = 10,800초, `STAGE_MAX_TURNS["implement"]` = 200턴이다. 읽기 25~35턴, 편집 약 40턴, 테스트 작성·수정 60~90턴, 게이트·커밋 15~20턴으로 140~200턴이 든다. 턴이 먼저 떨어지면 Verify가 미완성 트리를 FAIL로 판정하고 research·plan부터 다시 돈다.
- **안전.** 가벼운 auto-merge 태스크 전체의 머지 경로를 바꾼다. 셋으로 나눠도 볼트 설정(`.macp/batch_gate.json`)이 생기기 전에는 기능이 꺼져 있어, 하위 PR 하나만 머지된 상태도 지금과 동작이 같다.

**엔진의 자동 분할은 이 태스크에서 되지 않는다.** `rescope.rescope_from_plan_phases`(`rescope.py` L1044–1056)는 부모 AC가 `§N` 키로 시작하거나 로컬 게이트 항목일 때만 분할안을 만든다. 이 태스크의 AC 1~5는 둘 다 아니라서 `rescope.json`이 만들어지지 않고 ⛔ 파킹으로 사람에게 간다. 아래 분할안은 손으로 적용한다.

### 분할안

| 하위 slug | Phase | 선행 | 티어 · 추정 | 한 줄 요약 |
|---|---|---|---|---|
| `example-batch-verify-1-quick-verify-state` | 1~3 | 없음 | heavy · p50 2h / p90 4h | `verify_batch` 배선, quick Verify, `batch_ready` 상태, 개별 Verify 복귀 |
| `example-batch-verify-2-integration-gate` | 4~6 | 1 | heavy · p50 3h / p90 5h | 통합 브랜치, 게이트 1회, 차례 머지, 실패·충돌 복귀, 알림, 틱 통합 테스트 |
| `example-batch-verify-3-dashboard` | 7~8 | 2 | standard · p50 1h / p90 2h | '묶음 검증 대기' 대시보드(웹·md·`/api/status`), 문서 |

태스크 본문은 ③을 "대시보드·경보"로 묶었지만, 게이트 실패 알림은 실패 처리와 한 코드 경로라 ②에 둔다. ③은 대시보드와 문서다.

**손 분할 절차** (`V/CLAUDE.md` §10.1 "손 분할한 부모"):

1. 하위 md 3개를 `uv run python -m todo_system.cli new <slug> --template auto-worker`로 만든다. 공통: `parent: "[[example-batch-verify-light-tasks]]"`, 부모와 같은 `repo`, `auto_merge: true`, `max_turns: 200`. 첫째는 `worker_mode: auto`, 둘째·셋째는 `worker_mode: blocked` + `on_unblock: auto` + `depends_on: [<앞 slug>]`.
2. 각 하위의 `next_action`: "`Worker/runs/20261118T123354-example-batch-verify-light-tasks/implementation_plan.md`의 Phase n~m과 같은 디렉터리의 `requirements.md`를 읽고 그 Phase만 수행한다(설계는 '확정한 설계' 절을 따르고 다시 도출하지 않는다)".
3. 부모 md에는 `auto_rescoped`·`is_parent`를 찍지 않는다. 하위가 모두 done이면 `reconcile_split_parents`가 부모 md와 파킹 행을 닫는다.
4. `uv run python -m todo_system.cli validate <slug>` 후 볼트에 커밋한다.

**하위별 `acceptance_criteria`** (부모 AC 1~6을 나눈 것. 마지막 줄은 셋 모두 같다):

- ① `example-batch-verify-1-quick-verify-state` — `touches`: `todo_system/orchestrator/{tasks,assignment,batch_gate,stages,agent_backend,runner,orchestrator}.py`, `docs/tasks.md`
  - "단위 테스트: 대상 판정(light/standard·verify_batch true/false·emulator 조건·auto_merge false·미등록 리포·원격 게이트 표시 리포 제외), RESULT→batch_ready 전이, 복귀 행은 refail 증가·poison 없이 재디스패치되고 다음 payload에 `verify_full`이 실린다"
  - "`verify_batch` frontmatter가 payload까지 실제로 전달된다(tasks.py 배선 테스트: md → load_dispatchable → batch_gate.eligible)"
  - "러너 경로 테스트(`StagedExecutor._execute`): 묶음 대상이면 verify·report 스테이지에 quick/batch 지시가 실리고 RESULT payload에 `batch_ready`·`review_required`가 실린다. 새 코드의 호출 위치를 verify.md에 적는다"
  - "기존 개별 Verify 경로(heavy 태스크·verify_batch: false·설정 없는 리포)는 바뀌지 않는다 — 기존 테스트 green"
- ② `example-batch-verify-2-integration-gate` — `touches`: `todo_system/orchestrator/{batch_gate,orchestrator,runner}.py`, `todo_system/i18n.py`
  - "단위 테스트: 통합 게이트 트리거(2개 이상 또는 30분), 충돌 브랜치 제외, 통과 시 순서대로 머지·md done, 실패 시 각 태스크 개별 Verify로 복귀 + 알림 1회, main이 움직이면 잡 재시작(상한을 넘으면 복귀)"
  - "통합 테스트(가짜 git 리포 + 가짜 게이트 명령): 브랜치 3개(그중 1개는 충돌)를 batch_ready로 두고 `Orchestrator.tick()`을 돌리면 통합 브랜치에 2개가 합쳐져 게이트 1회 실행 → 2개 PR 머지 호출 순서·인자 확인, 충돌 1개는 개별 Verify 대기열로. 새 코드의 호출 위치를 verify.md에 적는다"
  - "게이트 실행은 gradle 동시 빌드 상한 계산에 들어간다(상한이면 시작을 미루고, 실행 중이면 디스패치 쪽 계산에 +1) — 테스트"
  - "설정 없는 호스트의 틱은 잡 파일·로그 줄·git 호출을 만들지 않는다 — 기존 테스트 green"
- ③ `example-batch-verify-3-dashboard` — `touches`: `todo_system/orchestrator/dashboard.py`, `scripts/macp_dashboard.py`, `todo_system/i18n.py`, `docs/batch-verify.md`
  - "대시보드에 '묶음 검증 대기' 상태가 웹·앱 같은 필드로 보인다: `/api/status`의 `batch_ready[]`, 행의 `state: batch_ready`·`batch_ready: true`, 웹 카드, Orchestrator.md 섹션(테스트)"
  - "묶음 행이 없으면 기존 렌더 출력과 같고, i18n ko·en 키가 같다"
- 공통 마지막 줄: "`uv run --with ruff ruff check .` + `uv run --with pytest python -m pytest -q` + `check_engine_isolation.sh` + 볼트 `ops/check_public_identifiers.sh --export`(AUDIT_ROOT=worktree) 로컬 green, run.log 인용"

## 확정한 설계 (requirements §5 열린 질문의 답)

1. **전체 게이트 명령의 출처 = 리포별 opt-in 설정.** 볼트 `<vault_root>/.macp/batch_gate.json`(`beta_publish_config.json`·`remote_gate.json` 선례). 파일이 없거나 `enabled`가 false이거나 리포 항목이 없으면 그 리포는 지금과 같다. 볼트 파일은 이 PR로 넣을 수 없으니 실제 활성화는 후속 볼트 태스크다.
   ```json
   {"enabled": true,
    "repos": {"<repo_name>": {"full_gate": [["./gradlew", ":app:testDebugUnitTest"]],
                              "quick_hint": "선택", "max_wait_min": 30, "max_batch": 5,
                              "gate_timeout_min": 120}}}
   ```
   `full_gate`는 argv 목록의 목록이다(셸 문자열 아님, 순서대로 전부 rc 0). 키는 `repos.repo_name(url)`.
2. **"light/standard"의 뜻 = 러너가 이미 계산하는 `classify_tier(payload)` 결과.** `verify_batch: true`는 heavy도 넣고 `false`는 항상 뺀다.
3. **대상 경계.** 외부 리포(`repo` 있음) + 5단계 파이프라인 + `worker_mode: auto` + 러너의 `auto_merge` 판정이 True인 경우만. 볼트 태스크, quick·delegate·discover, review, `auto_merge: false`는 지금과 같다.
4. **emulator 조건의 증거 = 이 런의 `screen_check.has_device_pass(run_dir)`.** `emulator: true`인데 증거가 없거나 UI 확신도 가드(`_ui_merge_hold_fires`)가 걸리면 묶지 않고 바로 개별 전체 Verify로 되돌린다. quick만 통과한 트리는 Report가 머지하지 않고, 머지 보류 기록(`merge_hold.schedule`)도 만들지 않는다.
5. **ledger 상태는 `assignment.py`에 둔다**(`statuses.py`는 태스크 md 상태 전용). `batch_ready`는 TERMINAL·in-flight·AWAITING_HUMAN 어디에도 넣지 않는다. 묶음 정보는 행의 `payload["batch"]`에 둔다(`last_run_dir` 선례 — dataclass 필드를 늘리지 않아 다른 행의 JSON이 그대로다).
6. **개별 Verify 복귀 = FAILED + history 마지막 항목 `batch-returned`.** `_ledger_gate`가 이 행을 `checkout_busy`·`job_running`과 같은 deferral로 본다(poison 없음, 5분 재확인, `dispatch()`의 refail 증가 없음). 복귀 표식 `verify_full: true`와 `batch_return_reason`은 `dispatch()`가 기존 행 payload에서 새 payload로 넘긴다. 그 태스크는 done까지 다시 묶이지 않는다.
7. **통합 브랜치는 로컬 전용.** 공유 클론의 별도 worktree(`<repo>.wt/integ-<ts>`)에 `integ/<repo_name>/<ts>`를 만들고 push하지 않는다. 워커 클론의 pre-push 가드(`stages._PUSH_GUARD_HOOK`)가 원격 ref 삭제를 막기 때문이다.
8. **"main이 움직이면 재시작".** 첫 머지 전에는 원격 기본 브랜치 sha가 `base_sha`인지, 그 뒤에는 직전 우리 머지의 squash sha(`beta_publish.resolve_merge_sha`, 못 구하면 머지 직후 원격 sha)인지 본다. 다르면 남은 태스크를 `batch_ready`로 둔 채 통합 브랜치를 버리고 다음 틱에 새로 만든다. 재시작 3회를 넘으면 남은 태스크를 개별 Verify로 되돌리고 한 번 알린다.
9. **상수** (`batch_gate.py`, 리포 설정으로 덮어씀): `MIN_BATCH = 2`, `MAX_WAIT_S = 1800`, `MAX_BATCH = 5`, `EXPIRE_S = 86400`, `MAX_RESTARTS = 3`, `GATE_TIMEOUT_S = 7200`. 나이는 `payload["batch"]["ready_ts"]` 기준이고, 순서는 `ready_ts` 오름차순이다.
10. **원격 게이트 규칙.** `full_gate` 명령 중 하나라도 `remote_gate.decide(cmd, checkout)`가 `remote`를 내면 그 리포는 v1 묶음 대상에서 뺀다. 원격으로 가야 하는 명령은 지금처럼 각 태스크의 Verify가 처리한다. 통합 게이트의 원격 실행은 후속이다.
11. **동시성.** 게이트는 `jobs.start`로 분리 실행하고 틱은 `jobs.poll`만 한다. 실행 중인 게이트는 `max_parallel_gradle_builds` 계산에 양방향으로 들어간다. PAUSED면 새 잡을 시작하지 않고, 이미 도는 잡의 판정·머지는 계속한다.
12. **wire 문자열** (git 추적 JSON에 들어가므로 바꾸지 않는다): 상태 `batch_ready` · history `batch-ready`, `batch-returned`, `batch-merged` · alert kind `batch-gate-failed` · RESULT payload `batch_ready`, `batch`, `batch_return` · payload `verify_batch`, `verify_full`, `batch_return_reason` · 복귀 사유 `gate-failed`, `gate-timeout`, `conflict`, `branch-moved`, `branch-missing`, `no-checkout`, `expired`, `restarts`, `merge-failed`, `not-enabled`, `no-device-evidence`, `no-pr`, `no-head-sha`.
13. **범위 밖(report "다음 단계"에 적을 후속):** 볼트 `.macp/batch_gate.json` 추가(리포별 전체 게이트 명령 결정 포함), `V/CLAUDE.md`·`V/Todo/templates/`의 `verify_batch` 안내, `V/docs/dashboard-parity.md`의 `batch_ready` 계약, Android 앱 렌더, 통합 게이트 원격 실행, 게이트 진행 상황의 대시보드 표시.

## Phases

### Phase 1: `verify_batch` 배선, ledger 상태, 대상 판정 (하위 ①)

- **Goal**: frontmatter 필드가 payload까지 가고, 대상 판정과 상태 전이의 순수 함수가 테스트와 함께 생긴다.
- **Files**:
  - `W/todo_system/orchestrator/tasks.py` (수정)
  - `W/todo_system/orchestrator/assignment.py` (수정)
  - `W/todo_system/orchestrator/batch_gate.py` (신규)
  - `W/todo_system/orchestrator/tests/test_tasks.py` (수정)
  - `W/todo_system/orchestrator/tests/test_batch_gate.py` (신규)
  - `W/docs/tasks.md` (수정 — Field reference에 `verify_batch` 한 줄)
- **Steps**:
  1. `tasks.py` `load_dispatchable`의 `heavy` 블록(L198–200) 바로 아래에 `verify_batch = _fm_bool(fm.get("verify_batch"))`를 넣고 `None`이 아닐 때만 `payload["verify_batch"]`에 싣는다(true·false 모두 실음).
  2. `assignment.py`: `BATCH_READY = "batch_ready"`(TERMINAL에 넣지 않는 이유를 주석으로), `BATCH_RETURNED = "batch-returned"`, `is_batch_return_pending(row)`(state FAILED이고 history 마지막 항목 type이 `BATCH_RETURNED` — `is_resume_pending` L48–61과 같은 모양)를 추가하고 `is_deferred`(L64–71)의 조건에 더한다.
  3. `batch_gate.py` 신규(stdlib만, 형제 모듈은 함수 안에서 지연 import, `vault_root`는 인자, `parents[N]`으로 루트를 계산하지 않음):
     - 설계 12번의 wire 상수와 9번의 수치 상수.
     - `load_config(vault_root) -> dict`(없음·깨짐·`enabled` false → `{}`), `repo_gate(cfg, repo_url) -> dict | None`(`full_gate`를 `list[list[str]]`로 정규화, 비었거나 형식이 틀리면 None).
     - `remote_marked(gate, checkout) -> bool`(`remote_gate.decide(shlex.join(argv), checkout) == "remote"`가 하나라도 있으면 True, 예외는 True — 보수적으로 제외).
     - `needs_device(payload) -> bool`(`heavy_signals`에 `emulator`가 있거나 `emulator`가 참).
     - `eligible(payload, *, tier, gate, five_stage, auto_merge, remote_marked=False) -> tuple[bool, str]`. 첫 일치 우선: ① `gate is None` → 제외 ② `repo` 없음 → 제외 ③ `verify_full` 참 → 제외 ④ `five_stage`가 아니거나 `worker_mode != "auto"` → 제외 ⑤ `auto_merge` 거짓 → 제외 ⑥ `remote_marked` → 제외 ⑦ `verify_batch`가 False → 제외, True → 포함 ⑧ `tier in ("light", "standard")` → 포함, 그 밖 → 제외.
     - `device_ok(payload, *, device_pass, ui_hold) -> bool`(설계 4번).
     - `mark_ready(a, result_payload, *, now_iso) -> bool`: `pr_url`·`repo`·`batch.head_sha` 중 하나라도 없으면 아무것도 바꾸지 않고 False. 있으면 `a.state = BATCH_READY`, `a.payload["batch"] = {pr_url, head_sha, branch, ready_ts, run}`(`run`은 run_dir의 basename만), `last_note`, history `batch-ready`.
     - `mark_returned(a, reason, *, now_iso)`: `state = FAILED`, `payload.pop("batch")`, `payload["verify_full"] = True`, `payload["batch_return_reason"] = reason`, `updated_ts = now_iso`, `capacity_limited`·`job_running`·`checkout_busy`·`timed_out` False, `blocked_on = ""`, `last_note`, history `{"type": BATCH_RETURNED, "reason": reason}`. `refail_count`는 건드리지 않는다.
     - `verify_hint(gate, default_branch="") -> str`(quick 지시 줄에 붙일 문구: 미룬 전체 게이트 명령 표시 + `quick_hint`).
  4. `test_batch_gate.py`: `eligible` 표 전체(티어 3종 × verify_batch 3값, auto_merge false, worker_mode quick, repo 없음, gate None, verify_full, remote_marked), `device_ok`·`needs_device`, 설정 로더(없는 파일, `enabled: false`, 단일 argv 정규화, 형식 오류), `mark_ready`의 False 경로와 정상 경로, `mark_returned` 뒤 `is_batch_return_pending`·`is_deferred`가 True.
  5. `test_tasks.py`: md에 `verify_batch: true` / `false` / 없음 세 경우로 `load_dispatchable` payload를 확인하고, 같은 테스트에서 그 payload를 `batch_gate.eligible`에 넘겨 소비까지 잇는다(heavy + true → 포함, standard + false → 제외).
  6. `docs/tasks.md` Field reference에 `verify_batch`(true/false, 기본 없음 = 티어로 판정) 한 줄.
  7. 커밋(`git -C W add` → `git -C W commit`을 각각 따로 호출).
- **Risk**: `is_deferred`가 넓어져 기존 대시보드 테스트가 흔들릴 수 있다 → history 마지막 항목이 `batch-returned`일 때만 참이라 기존 행에는 영향이 없다. 그 사실을 테스트로 고정한다.

### Phase 2: quick Verify 지시와 러너의 머지 보류·복귀 신호 (하위 ①)

- **Goal**: 묶음 대상 런은 Verify를 quick 모드로 돌리고, PASS면 Report가 ready PR만 열며 RESULT에 묶음 표식을 싣는다. 대상이 아닌 런은 argv·payload·진행 줄이 그대로다.
- **Files**:
  - `W/todo_system/orchestrator/stages.py` (수정)
  - `W/todo_system/orchestrator/agent_backend.py` (수정)
  - `W/todo_system/orchestrator/runner.py` (수정)
  - `W/todo_system/tests/test_agent_backend_golden.py` (수정)
  - `W/todo_system/tests/test_agent_backend.py` (수정)
  - `W/todo_system/orchestrator/tests/test_runner_batch.py` (신규)
- **Steps**:
  1. `stages.build_stage_argv`(L570–717)에 `batch_mode: str | None = None, batch_hint: str = ""`를 추가하고 `BATCH_QUICK = "quick"` 상수를 둔다. `if project_context:`(L688) 바로 앞에서, `batch_mode == BATCH_QUICK`일 때만 메타 줄을 붙인다.
     - `stage == "verify"`: "**Verify mode: QUICK — batch gate (overrides Step 2 and the pass rule in the prompt above)**". 내용: 전체 게이트는 이 스테이지 뒤에 통합 브랜치에서 한 번 돈다 / 이 스테이지는 (1) 바꾼 모듈의 컴파일·빌드와 (2) `git diff --name-only origin/<default>...<branch>` 파일을 덮는 대상 테스트만 돌린다 / 리포 전체 게이트는 돌리지 않고 verify.md `## Deferred to the integration gate`에 명령을 적는다 / `overall_status: pass`의 "목록의 모든 검사" 규칙은 quick 집합에만 적용한다 / 전체 게이트로만 보일 AC는 `DEFERRED (integration gate)`로 적고 pass를 막지 않는다 / 기기 확인(`emulator: true`)은 미루지 않는다 / `confidence`는 `medium` 이하 / 끝에 `batch_hint`.
     - `stage == "report"`: "**Batch gate**: 이 PR은 통합 게이트가 통과한 뒤 오케스트레이터가 머지한다. ready PR을 열고 머지하지 않는다(`auto_merge: false`는 사람 검토 때문이 아니다). 다른 사유가 없으면 `사람이 검토해야 할 부분`은 없음으로 적는다."
  2. `agent_backend.ClaudeAgentBackend.run_stage`(L138–164)에 같은 두 인자를 추가하고, 값이 있을 때만 `build_stage_argv`에 넘긴다(`**extra`). Gemini·Solar는 고치지 않는다.
  3. `runner.py`에 `StagedExecutor._batch_plan(self, payload, tier, repo, repo_url, auto_merge, progress) -> tuple[bool, str]`를 추가한다. `repo_url`이 없거나 `batch_gate.repo_gate(batch_gate.load_config(stages._VAULT_DATA_ROOT), repo_url)`이 None이면 진행 줄 없이 `(False, "")`. 등록된 리포일 때만 `eligible(...)`을 부르고 `progress("batch verify: quick|off (<이유>)")` 한 줄을 남긴다. `five_stage`는 `tuple(self._pipeline_for(payload)) == tuple(stages.STAGES_AUTO)`. 전체를 try/except로 싸서 어떤 오류도 `(False, "")`가 되게 한다.
  4. `_execute` 배선(대상이 아닌 런의 kwargs·흐름은 그대로 둔다):
     - 티어 기록 뒤(L1165): `batch_quick, batch_hint = self._batch_plan(...)`.
     - `ui_merge_hold` 변수 옆(L1217–1219): `batch_hold = False`, `batch_return = ""`, `batch_head_sha = ""`.
     - 스테이지 호출(L1316–1324): `batch_quick and is_claude and stage in ("verify", "report")`일 때만 `{"batch_mode": stages.BATCH_QUICK, "batch_hint": batch_hint}`를 `**`로 더한다.
     - verdict 블록(L1614): 기존 `if stage == "verify" and auto_merge and not ui_merge_hold and self._ui_merge_hold_fires(...)`를 `elif`로 바꾸고 그 앞에 새 분기를 둔다.
       ```python
       if stage == "verify" and batch_quick:
           fires = self._ui_merge_hold_fires(repo, repo_url, payload, run_dir, branch, text, progress)
           sha = self._head_sha(repo)
           if not batch_gate.device_ok(payload, ui_hold=fires,
                                       device_pass=screen_check.has_device_pass(run_dir)):
               batch_return = batch_gate.RETURN_NO_DEVICE
           elif not sha:
               batch_return = batch_gate.RETURN_NO_HEAD_SHA
           else:
               batch_hold, batch_head_sha = True, sha
           auto_merge = False      # quick만 통과한 트리는 Report가 머지하지 않는다
       ```
     - 루프 뒤 `pr_url` 계산(L1712)과 `if ui_merge_hold:`(L1721) 사이: `batch_hold`인데 `pr_url`이 없으면 `stages.no_op_run(...)`이 참일 때 기존 no-op 종결(`review = False`, `no_op_close = True`, 같은 merge_note)로, 아니면 `batch_return = RETURN_NO_PR`로 바꾼다. `batch_hold or batch_return`이면 `review = True`(구 오케스트레이터에서는 기존 검토 파킹으로 떨어짐)와 `merge_note`를 정한다.
     - `extra`(L1733–1750): `batch_hold` → `extra["batch_ready"] = True`, `extra["batch"] = {"head_sha": batch_head_sha}`. `batch_return` → `extra["batch_return"] = batch_return`.
  5. 테스트:
     - `test_agent_backend_golden.py`: 기본 argv 프롬프트에 "Verify mode: QUICK"·"Batch gate"가 없다(전 스테이지). `batch_mode="quick"`이면 verify·report 프롬프트에만 각 줄과 `batch_hint`가 있고 플래그 뼈대는 같다.
     - `test_agent_backend.py`: 새 인자를 주지 않으면 `build_stage_argv`가 받는 kwargs에 `batch_mode`가 없고, 주면 그대로 전달된다.
     - `test_runner_batch.py`: 기존 `_execute` 수준 테스트의 준비 방식을 `git -C W grep -ln "_ui_merge_hold_fires\|ui_merge_hold" -- todo_system`으로 찾아 그대로 따른다. 경우: (a) 등록 리포 + standard → verify·report 호출에 `batch_mode`, RESULT payload에 `batch_ready`·`batch.head_sha`·`review_required`, Report의 `auto_merge`는 False (b) 미등록 리포 → 호출 kwargs와 payload에 batch 키가 없고 `batch verify:` 진행 줄도 없다 (c) `verify_batch: false`·heavy → 기존 경로 (d) emulator 필요 + 기기 증거 없음 → `batch_return == "no-device-evidence"`이고 `merge_hold.schedule`이 불리지 않는다 (e) `_batch_plan`이 예외를 내도 기존 경로로 간다.
  6. 커밋.
- **Risk**: 러너의 머지 판정부를 건드린다 → 새 분기는 `batch_quick`이 참일 때만 타고 기존 블록은 `elif`로 조건이 같다. (b)·(c)와 golden 테스트로 고정한다.

### Phase 3: 오케스트레이터 — `batch_ready` 전이, 개별 Verify 복귀, 안전망 스윕 (하위 ①)

- **Goal**: 묶음 RESULT는 사람 알림 없이 `batch_ready`가 되고, 복귀 행은 벌점 없이 전체 Verify로 다시 나간다. 통합 게이트(하위 ②)가 없어도 `batch_ready` 행이 남지 않는다.
- **Files**:
  - `W/todo_system/orchestrator/orchestrator.py` (수정)
  - `W/todo_system/orchestrator/tests/test_batch_ready_state.py` (신규)
- **Steps**:
  1. `.assignment` import(L29–32)에 `BATCH_READY`, `is_batch_return_pending`을 더한다. ALERT 상수 옆(L131–154)에 `ALERT_BATCH_GATE_FAILED = "batch-gate-failed"`를 두고 `FAILURE_ALERT_KINDS`에는 넣지 않는다.
  2. `collect()`(L1287): 기존 `elif msg.type == m.RESULT:` 앞에 `elif msg.type == m.RESULT and self._batch_result(a, msg): pass`를 넣는다. `_batch_result`는 payload에 `batch_ready`가 있으면 `batch_gate.mark_ready(...)`의 결과를, `batch_return`이 있으면 `mark_returned` 후 True를, 그 밖과 모든 예외에서는 False를 돌려준다(False면 기존 검토 파킹 경로).
  3. `_ledger_gate`(L869): `deferred = job_running or checkout_busy or is_batch_return_pending(existing)`.
  4. `dispatch()`(`_route_decision` 호출 L762 앞): 기존 행 payload에 `batch_gate.CARRY_KEYS`(`verify_full`, `batch_return_reason`)가 있으면 `task = Task(task.slug, {**task.payload, **carry})`로 새 payload에 넘긴다. 호출자의 Task 객체는 바꾸지 않는다.
  5. `reconcile_batch_gate(self, now=None) -> dict`(안전망 판) 추가. `{"started": [], "merged": [], "returned": [], "closed": []}`를 돌려주고, `BATCH_READY` 행이 없으면 I/O 없이 끝난다. 행마다 try/except + `_SkipTally`: 태스크 md가 닫혔으면 `_close_split_parent_row`와 같은 모양으로 DONE 정리(`closed`), 리포가 설정에서 빠졌으면 `mark_returned(..., "not-enabled")`, `ready_ts`가 `EXPIRE_S`를 넘었으면 `mark_returned(..., "expired")` + 알림 1회(`_notify_routed`, event_key `batch-expired:<slug>`). 바깥에도 try/except를 두고 `[orchestrator] batch gate sweep skipped (fail-open): …` 한 줄만 남긴다.
  6. `tick()`: `reconcile_split_parents`(L3456) 뒤, `reconcile_merge_holds`(L3460) 앞에서 호출하고 결과 dict에 `"batch_gate"` 키를 더한다.
  7. 상태 소비자 점검: `git -C W grep -n "AWAITING_HUMAN\|_IN_FLIGHT\|is_terminal\|awaiting_human\|in_progress" -- todo_system scripts ':!*tests*'` 결과를 훑어 `batch_ready` 행이 잘못 분류되는 곳이 있는지 run.log에 적는다. 특히 `worker._runner_idle`이 이 상태를 "바쁨"으로 읽지 않는지 확인한다. `todo_system/cli.py`는 수정 금지이므로 문제가 있으면 후속으로만 기록한다.
  8. `test_batch_ready_state.py`: RESULT(`review_required` + `batch_ready` + `pr_url`) → state `batch_ready`, notify 호출 없음 / `pr_url` 없음 → 기존 AWAITING_HUMAN 경로 / `review_required`만 있는 RESULT → 기존과 같음 / `batch_return` → FAILED, `display_state == "deferred"` / 복귀 행 재디스패치: `refail_count` 그대로, 새 행 payload와 envelope에 `verify_full`, refail이 poison 문턱이어도 격리되지 않음 / `batch_ready` 행은 `dispatch()`가 None / 안전망: 만료·설정 제거·md 닫힘 / `tick([])`이 `reconcile_batch_gate`를 실제로 거친다(결과 dict의 `batch_gate` 키).
  9. 하위 ① 마감: Verification strategy의 AC 6 명령 4개를 커밋 뒤에 돌리고 출력을 run.log에 인용한다.
- **Risk**: 틱 결과 dict의 키 집합을 고정한 기존 테스트가 있으면 깨진다 → 키는 추가만 하고, 깨지는 테스트는 기대값에 새 키를 더한다(동작을 바꾸지 않는다).

### Phase 4: 통합 게이트 엔진 — 통합 브랜치, 게이트, 머지 순서 (하위 ②)

- **Goal**: 한 리포의 잡을 틱마다 한 걸음씩 진행시키는 상태 기계가 `batch_gate.py`에 생긴다. git·게이트·머지·시계는 모두 주입한다.
- **Files**:
  - `W/todo_system/orchestrator/batch_gate.py` (수정)
  - `W/todo_system/orchestrator/tests/test_batch_gate_job.py` (신규)
- **Steps**:
  1. 순수 함수: `trigger_due(rows, now, *, min_batch, max_wait_s, restart_due=False) -> bool`, `pick(rows, max_batch)`(`ready_ts` 오름차순).
  2. 잡 기록(`Worker/_state/batch_gate/<repo_name>.json`, `atomic_io.atomic_write_text`): `state_dir(vault_root)`, `load_job`, `save_job`, `clear_job`, `running_jobs(vault_root)`, `has_jobs(vault_root)`. 필드: `repo`, `state`(`running`|`passed`|`idle`), `branch`, `worktree`, `default_branch`, `base_sha`, `head_sha`, `expect_base`, `members`(slug·branch·head_sha·pr_url, 머지 순서), `merged`, `gate`, `started_ts`, `restarts`, `restart_due`, `merge_attempts`.
  3. `repo_lock(vault_root, repo_name)`: `fcntl`을 함수 안에서 import하는 non-blocking flock 컨텍스트 매니저. 못 잡으면 그 리포는 이번 틱에 건너뛴다.
  4. `build_integration(clone, default, members, *, branch, worktree, git) -> dict`: `fetch origin --prune` → `rev-parse origin/<default>`로 `base_sha` → `worktree add -B <branch> <worktree> origin/<default>` → 멤버마다 `rev-parse --verify origin/<member.branch>`(없으면 `branch-missing`), 기록된 `head_sha`와 다르면 `branch-moved`, `merge --no-ff --no-edit`(중립 `-c user.name`·`-c user.email`)가 rc 0이 아니면 `merge --abort` 후 `conflict`. `{"base_sha", "head_sha", "included", "excluded": [(slug, reason)]}`를 돌려준다. `merge-tree`는 쓰지 않는다(없는 ref에도 rc 1을 낸다).
  5. 게이트 러너: `start(job) -> dict`, `poll(job) -> "running" | "passed" | "failed"`, `stop(job)`, `cleanup(job)`을 가진 객체. 기본 구현 `LocalDetachedGate`는 `jobs.clear` → `jobs.start(f"batch-gate-{repo_name}", cmd, cwd=worktree)`. `cmd`는 `export GRADLE_OPTS=<shlex.quote(기존 값 + stages._worker_gradle_opts(worktree))>; ` 뒤에 argv별 `shlex.join`을 ` && `로 이은 것이다. `poll`은 `jobs.poll` 결과를 옮긴다(done → passed, failed·absent → failed). `cleanup`은 `stages.cleanup_build_daemons(worktree)` + `jobs.clear`. `default_gate()` 팩토리를 둬 테스트가 바꿔 끼운다.
  6. `Env` dataclass(주입점): `git`, `gate`, `merge_pr`, `pr_state`, `pr_merged`, `merge_sha`, `remote_head`, `now`, `log`, `clone_for(repo_url)`, `worktree_for(repo_url, name)`. 기본값은 함수 안에서 `stages`·`repos`·`beta_publish`를 지연 import해 만든다.
  7. `step(repo_url, rows, gate_cfg, env, hooks, *, vault_root, paused, gradle_busy) -> dict`. `hooks`는 오케스트레이터가 주는 콜백 `close(a, note)`, `returned(a, reason)`, `park(a, note)`, `alert(kind, repo_label, slugs, detail)`.
     - 잡 없음(또는 `idle`): 후보별 `pr_state`가 `MERGED`면 `close`, `CLOSED`면 `park`(AWAITING_HUMAN — 닫힌 PR 알림은 기존 `reconcile_merged`가 한 번 낸다). `trigger_due`가 아니면 끝. `paused`나 `gradle_busy()`면 시작하지 않는다. 클론이 없으면 전원 `no-checkout` 복귀. `build_integration` → 제외된 멤버는 사유대로 `returned`. 포함이 0이면 worktree·브랜치를 지우고 끝. 아니면 `gate.start` 후 `state: running`으로 기록.
     - `running`: `gate.poll`이 `running`이면 시간 초과(`gate_timeout_min`)만 확인한다. 초과 시 `gate.stop` 후 실패 처리(사유 `gate-timeout`). `failed` → 멤버 전원 `returned(..., "gate-failed")`, `alert("failed", …)` 1회, 정리, 기록 삭제. `passed` → 머지 단계로.
     - 머지 단계(틱을 넘어 이어질 수 있음): `merged`에 없는 멤버를 순서대로. `remote_head != expect_base`면 재시작(정리, `restarts += 1`, `state: idle` + `restart_due`. `MAX_RESTARTS` 초과면 남은 멤버 `returned(..., "restarts")` + `alert("restarts", …)`). `merge_pr(clone, branch, repo=url)`이 실패하면 `pr_merged`가 True일 때만 머지된 것으로 보고, 아니면 이번 틱은 멈춘다(3회 연속이면 그 멤버를 `merge-failed`로 복귀). 성공하면 `close(a, note)`, `merged`에 추가, `expect_base = merge_sha(...) or remote_head(...)`, 기록 저장. 모두 끝나면 정리 + `alert("passed", …)`.
     - 정리 = `gate.cleanup` → `worktree remove --force` → `branch -D <integ>` → `worktree prune` → `worktree_sweep.remove_registry(<worktree 이름>)`. 모두 fail-open.
  8. `test_batch_gate_job.py`(같은 파일의 모듈 수준 헬퍼가 tmp_path에 bare origin + 클론 + `worker/a`, `worker/b`, 충돌하는 `worker/c`를 만든다. 원격·`gh`·네트워크·`jobs.start`는 쓰지 않는다): 트리거 3경우, 충돌 제외(통합 브랜치에 a·b만), 통과 → `merge_pr` 호출 순서와 인자·`close` 순서, 실패 → 전원 `returned` + alert 1회, 시작 전에 main이 움직임 → 머지 호출 없이 재시작하고 다음 호출에서 새 base로 다시 만듦, 재시작 상한, `merge_pr` 실패 → 닫지 않고 다음 호출에서 이어감, 게이트 시간 초과, 잡이 없을 때 `step`이 git을 부르지 않음.
  9. 커밋.
- **Risk**: 실제 git을 쓰는 테스트가 호스트의 git 전역 설정에 기대면 깨진다 → 헬퍼가 리포마다 `user.name`·`user.email`·`init.defaultBranch`를 로컬로 설정하고 `GIT_CONFIG_GLOBAL`을 빈 파일로 돌린다.

### Phase 5: 틱 배선, 동시성, 알림, 통합 테스트 (하위 ②)

- **Goal**: `Orchestrator.tick()`이 리포 잠금 아래에서 통합 게이트 잡을 진행시키고, 실패는 한 번 알린다.
- **Files**:
  - `W/todo_system/orchestrator/orchestrator.py` (수정)
  - `W/todo_system/i18n.py` (수정)
  - `W/todo_system/orchestrator/tests/test_batch_gate_job.py` (수정)
- **Steps**:
  1. `reconcile_batch_gate`를 넓힌다. `BATCH_READY` 행을 `payload["repo"]`로 묶고, `batch_gate.running_jobs(vault_root)`에만 있는 리포도 포함한다. 리포마다 try/except + `_SkipTally`, `repo_lock` 안에서 Phase 3의 안전망 검사 뒤 `batch_gate.step(...)`. hooks: `close` = `_close_with_evidence(a)` + DONE + `job_running`·`checkout_busy` 해제 + history `batch-merged` + save(`reconcile_merge_holds` L3371–3380과 같은 모양) / `returned` = `mark_returned` + save / `park` = AWAITING_HUMAN + note + save / `alert` = `_notify_routed`.
  2. gradle 상한: `_batch_gradle_jobs()`(실행 중 잡 가운데 gradle 체크아웃인 것의 수)를 추가해 `_checkout_gate`의 `gradle_runs`(L1083–1087)에 더한다. `step`에 넘기는 `gradle_busy`는 `max_parallel_gradle_builds > 0`이고 gradle 체크아웃일 때 "in-flight gradle 런 + 실행 중 잡 ≥ 상한". 상한이 0이면 둘 다 기존과 같다.
  3. 알림(`i18n.MESSAGES` ko·en 동시): `alert.batch_gate_failed`("❌ 묶음 통합 게이트 실패 — {repo} {n}건을 개별 전체 Verify로 되돌렸어요", 즉시, event_key `batch-gate-failed:<repo>:<integ 브랜치>`, 멤버 행에 `last_alert_kind = ALERT_BATCH_GATE_FAILED`), `alert.batch_gate_passed`(digest), `alert.batch_restarts`, Phase 3의 만료 문구를 `alert.batch_expired`로 옮긴다. 게이트 로그는 경로만 싣는다.
  4. 통합 테스트(AC 2): tmp 볼트 배치(ledger `Worker/assignments`, `.macp/batch_gate.json`)와 Phase 4 헬퍼의 리포로 a·b·c를 `batch_ready`로 두고 **`orch.tick([])`을 실제로 호출**한다. 가짜 게이트(`batch_gate.default_gate` 패치), 가짜 `stages.merge_pr`·`gh_pr_state`·`gh_pr_merged`, `tasks.mark_done`·`md_fingerprint` 기록기, 클론 루트는 `repos.local_path` 패치. 관계없는 스윕은 `mock.patch.object`로 막는다. 틱 1: 통합 브랜치에 a·b, c는 FAILED + `batch-returned` + `verify_full`, 게이트 시작 1회. 틱 2: `merge_pr`가 `worker/a`, `worker/b` 순서로 `(클론 경로, 브랜치, repo=url)` 호출, a·b는 DONE, `mark_done`이 a → b 순서. 별도 테스트로 상한이 찼을 때 시작 보류와 `_checkout_gate`의 +1, 설정이 없는 호출에서 잡 파일·출력·git 호출이 없음을 확인한다.
  5. 커밋.
- **Risk**: 틱에서 `gh`·git이 막히면 디스패치가 멈춘다 → 잡이 있거나 트리거가 찼을 때만 외부 호출을 하고, 각 호출은 기존 헬퍼의 타임아웃(`merge_pr` 120초, `gh_pr_state` 30초)을 쓴다. 전체 게이트는 분리 프로세스다.

### Phase 6: 충돌로 복귀한 런은 Implement가 기본 브랜치를 머지한다 (하위 ②)

- **Goal**: `conflict`로 되돌아온 태스크가 사람 파킹(`rebase_conflict`)으로 직행하지 않고, 재실행의 Implement가 기본 브랜치를 머지해 푼 뒤 전체 Verify를 받는다.
- **Files**:
  - `W/todo_system/orchestrator/runner.py` (수정)
  - `W/todo_system/orchestrator/tests/test_runner_batch.py` (수정)
- **Steps**:
  1. `_execute`의 재시도 맥락(L1129–1144): `payload.get("batch_return_reason") == "conflict"`이면 `retry_ctx`를 고정 문구로 바꾼다. 내용: 통합 게이트에서 기본 브랜치와 충돌이 났다 / `git merge origin/<default>`로 일반 머지 커밋을 만든다(rebase·force-push 금지) / 두 변경의 의도를 모두 살려 충돌을 푼다 / 영향받은 테스트를 다시 돌리고 커밋한다. 직전 quick PASS verify.md가 "실패 맥락"으로 붙지 않게 한다.
  2. 상속 계산(L1150–1158): 같은 조건이면 `candidates`에서 `implement`를 빼 Implement가 다시 돈다. 그 밖의 복귀 사유는 research·plan·implement를 상속하고 전체 Verify만 다시 돈다(코드 변경 없음, 테스트로 고정).
  3. 테스트: `conflict` 복귀 → implement가 상속되지 않고 retry 문구가 implement 호출에 실림 / `gate-failed` 복귀 → 세 스테이지 상속, verify 호출에 `batch_mode` 없음 / 두 경우 모두 다시 `batch_ready`가 되지 않음.
  4. 하위 ② 마감: AC 6 명령 4개를 커밋 뒤에 돌리고 run.log에 인용한다.
- **Risk**: Implement의 충돌 해결이 틀릴 수 있다 → 뒤따르는 전체 Verify가 판정하고, 여전히 충돌이면 Report §4b가 지금처럼 사람에게 넘긴다.

### Phase 7: 대시보드 — Orchestrator.md 섹션, `/api/status` 필드, 웹 카드 (하위 ③)

- **Goal**: `batch_ready` 행이 '묶음 검증 대기'로 보이고, 웹과 앱이 같은 서버 필드를 읽는다.
- **Files**:
  - `W/todo_system/orchestrator/dashboard.py` (수정)
  - `W/scripts/macp_dashboard.py` (수정)
  - `W/todo_system/i18n.py` (수정)
  - `W/todo_system/orchestrator/tests/test_batch_dashboard.py` (신규)
  - `W/todo_system/tests/test_macp_dashboard_batch.py` (신규)
- **Steps**:
  1. `dashboard.render`(L194–240): `batch = by(A.BATCH_READY)`. 행이 있을 때만 "## 🏃 진행 중" 다음에 "## 📦 묶음 검증 대기 (통합 게이트 후 머지)" 섹션을 넣는다(행마다 slug, agent, `last_note`). 머리 집계 줄은 그대로 둔다. `_deferred_line`(L41–51)에 `is_batch_return_pending`이면 "묶음 복귀 — 개별 Verify 대기" 라벨을 더한다.
  2. `scripts/macp_dashboard.py`(`orchestrator.orchestrator`는 import하지 않는다):
     - `collect_assignments`(L315–348): 모든 행에 `"batch_ready": d.get("state") == "batch_ready"`(`deferred` 선례 — 새 상태값을 모르는 클라이언트용).
     - `collect_status`(L1789–1899): `batch_ready = [r for r in a if r["state"] == "batch_ready" and not is_closed(r.get("todo_status"))]`, 각 행에 `group`, 응답에 `"batch_ready": batch_ready`. `active`·`attention`에는 섞지 않는다. 필드 계약을 함수 주석에 적는다.
     - 웹: `c-active`와 `c-queue` 사이에 카드 `c-batch`(행이 없으면 숨김, 제목 `@@dash.html.batch_ready@@`), `update(d)`에서 `d.batch_ready || []`를 채운다. `badge()`(L2192)에 `batch_ready: 'b-def'`와 표시 라벨 `@@dash.js.st_batch_ready@@`를 더한다. 새 색 토큰은 만들지 않는다.
  3. `i18n.DASHBOARD` ko·en에 `dash.html.batch_ready`("묶음 검증 대기" / "Batch verify queue"), `dash.js.st_batch_ready`("묶음 대기" / "batch wait"), `dash.cmt.batch_ready`를 같이 넣는다. en 값에 ko에 없는 따옴표·괄호·중괄호를 넣지 않는다.
  4. 테스트: `test_batch_dashboard.py` — 묶음 행이 없으면 `render` 출력이 변경 전과 같고(섹션 제목 없음), 있으면 섹션과 행이 나온다. 복귀 행은 deferred 섹션에 새 라벨로 나온다. `test_macp_dashboard_batch.py`(`test_macp_dashboard.py`처럼 스크립트를 경로로 로드, `ASSIGN` 등은 tmp로 패치) — `collect_assignments` 행의 `state`·`batch_ready`, `collect_status()["batch_ready"]`의 행 키, 그 행이 `active`·`attention`에 없음, 렌더된 HTML에 `c-batch`와 ko·en 문구가 있고 `@@` 마커가 남지 않음.
  5. 커밋.
- **Risk**: 페이지 템플릿의 `@@key@@` 치환 규칙을 어기면 i18n 테스트가 깨진다 → 기존 키 형식을 그대로 따르고 `uv run --directory W --with pytest python -m pytest -q todo_system/tests -k i18n`으로 먼저 확인한다.

### Phase 8: 문서, 후속 항목, 마감 게이트 (하위 ③)

- **Goal**: 동작과 설정이 엔진 문서에 남고, 이 PR로 못 하는 일이 후속으로 넘어간다.
- **Files**:
  - `W/docs/batch-verify.md` (신규)
  - `W/docs/tasks.md` (수정 — 새 문서 링크)
- **Steps**:
  1. `docs/batch-verify.md`: 흐름(대상 판정 → quick Verify → `batch_ready` → 통합 게이트 → 차례 머지 / 복귀), `.macp/batch_gate.json` 스키마와 기본 상수, 상태·history·복귀 사유 표, `/api/status` 필드, 한계(원격 표시 게이트 제외, 통합 브랜치 로컬 전용, 이분 탐색 없음, 조합 실패는 개별 Verify가 각자 통과할 수 있음, 묶음 머지 뒤 베타 발행은 `_beta_sweep_tick`이 따라잡음). 개인 식별자(소유자명·URL)는 쓰지 않는다.
  2. `docs/tasks.md`의 `verify_batch` 줄에서 새 문서로 링크한다.
  3. report "다음 단계"에 설계 13번의 후속 6건을 적는다.
  4. 하위 ③ 마감: AC 6 명령 4개를 커밋 뒤에 돌리고 run.log에 인용한다.
- **Risk**: 문서의 예시 URL이 식별자 게이트에 걸릴 수 있다 → `https://example.test/acme/app.git` 같은 중립 값만 쓴다.

## Long-running work

없음

## Parallelizable subtasks

없음 — 하위 ①→②→③은 `orchestrator.py`·`runner.py`·`i18n.py`를 차례로 고쳐서 직렬이다. Phase 7은 내용상 Phase 4~6과 독립이지만 `i18n.py`가 겹친다.

## Verification strategy

모든 명령은 커밋된 트리에서 돌린다(`check_engine_isolation.sh`는 `git archive HEAD`만 본다). Verify 스테이지에는 `Bash(bash:*)`가 없으므로 스크립트는 `uv run … python -c`로 감싼다.

- **AC 1** (단위 테스트: 대상 판정, batch_ready 전이, 트리거, 충돌 제외, 순서 머지·md done, 실패 복귀, main 이동 재시작) →
  `uv run --directory W --with pytest python -m pytest -q todo_system/orchestrator/tests/test_batch_gate.py todo_system/orchestrator/tests/test_batch_ready_state.py todo_system/orchestrator/tests/test_runner_batch.py todo_system/orchestrator/tests/test_batch_gate_job.py`
  (하위 ①은 앞 세 파일, 하위 ②는 네 파일 모두)
- **AC 2** (틱을 거치는 통합 테스트 + 호출 위치) →
  `uv run --directory W --with pytest python -m pytest -q todo_system/orchestrator/tests/test_batch_gate_job.py -k tick`
  와 `git -C W grep -n "reconcile_batch_gate\|_batch_result\|batch_gate\.\|_batch_plan\|batch_mode" -- todo_system scripts ':!*tests*'`. verify.md `## Wiring`에 적을 호출 사슬: `tasks.load_dispatchable` → payload → `runner.StagedExecutor._batch_plan` → `batch_gate.eligible` / `runner._execute` → `ClaudeAgentBackend.run_stage` → `stages.build_stage_argv(batch_mode=…)` / `Orchestrator.collect` → `_batch_result` / `Orchestrator.tick` → `reconcile_batch_gate` → `batch_gate.step` / `Orchestrator._checkout_gate` → `_batch_gradle_jobs`.
- **AC 3** (`verify_batch` 배선) →
  `uv run --directory W --with pytest python -m pytest -q todo_system/orchestrator/tests/test_tasks.py -k verify_batch`
- **AC 4** (기존 경로 불변) →
  `uv run --directory W --with pytest python -m pytest -q todo_system/tests/test_agent_backend_golden.py todo_system/tests/test_agent_backend.py todo_system/orchestrator/tests/test_runner.py todo_system/orchestrator/tests/test_orchestrator.py`
  그리고 아래 전체 pytest. `git -C W diff origin/main...HEAD --stat`으로 기존 테스트 파일의 수정이 기대값 추가뿐인지 읽어 확인한다.
- **AC 5** (대시보드) →
  `uv run --directory W --with pytest python -m pytest -q todo_system/orchestrator/tests/test_batch_dashboard.py todo_system/tests/test_macp_dashboard_batch.py`
  와 `uv run --directory W --with pytest python -m pytest -q todo_system/tests -k i18n`
- **AC 6** (로컬 게이트 4종, 각 rc 0, 스크립트 둘은 마지막 줄 `RESULT: PASS`, 출력을 run.log에 인용) →
  1. `uv run --directory W --with ruff ruff check .`
  2. `uv run --directory W --with pytest python -m pytest -q`
  3. `uv run --directory W python -c "import subprocess,sys; sys.exit(subprocess.run(['bash','scripts/check_engine_isolation.sh'], cwd='W').returncode)"`
  4. `uv run --directory W python -c "import os,subprocess,sys; sys.exit(subprocess.run(['bash','V/ops/check_public_identifiers.sh','--export'], cwd='W', env={**os.environ,'AUDIT_ROOT':'W'}).returncode)"`
  (`W`·`V`는 실제 절대 경로로 바꾼다. 3·4번이 허용 목록에 막히면 막힌 명령을 그대로 기록하고, 2번 안의 `todo_system/tests/test_public_gates_real_tree.py` 결과를 함께 인용한다.)

## Rollback plan

이 런은 계획이 초과를 선언해 Implement가 돌지 않으므로 `worker/example-batch-verify-light-tasks` 브랜치에는 커밋이 없고, 지울 것은 그 브랜치뿐이다. 하위 태스크의 Implement가 중간에 실패하면 Phase 단위 커밋이 `worker/<하위 slug>`에 남으니 그 브랜치를 지우거나(로컬·origin) 재시도가 이어서 쓴다. 이미 머지된 하위 PR은 볼트에 `.macp/batch_gate.json`이 없는 한 런타임 동작을 바꾸지 않으므로 급히 되돌릴 필요가 없다. 기능을 켠 뒤 끄려면 그 파일을 지우거나 `"enabled": false`로 둔다. 다음 틱에 대기 중인 `batch_ready` 행이 모두 개별 전체 Verify로 돌아간다(`not-enabled`). 코드를 되돌릴 때는 squash 커밋을 ③ → ② → ① 순서로 `git revert`한다. `batch_ready` 행이 남은 채 ①을 되돌리면 구 엔진이 그 상태를 모르므로, 되돌리기 전에 설정을 꺼서 행을 먼저 비운다.
