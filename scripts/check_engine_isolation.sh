#!/usr/bin/env bash
#
# check_engine_isolation.sh — 공개 분리 5단계: 엔진 트리 격리 검증 (재실행 가능)
#
# 부모 [[macp-public-repo-engine-vault-split]] 실행 단계 5("엔진만 떼어내도
# 도는가")를 한 줄로 재검증할 수 있게 고정한 스크립트다. 하는 일:
#
#   1. 엔진 트리(todo_system/ · scripts/ · .claude/commands/ + pyproject.toml
#      · uv.lock · README.md · LICENSE — pyproject 의 readme/license 메타데이터가 빌드에 필요)만
#      `git archive HEAD`로 격리 temp dir(ENGINE_TMP)에 떼어낸다.
#   2. 별도 throwaway 볼트(VAULT_TMP)를 만들어 Todo/items/에 샘플 태스크,
#      Memory/·Notes/·Decisions/·Worker/ 빈 골격을 둔다.
#   3. MACP_VAULT_ROOT=VAULT_TMP 로 가리킨 채, cwd=ENGINE_TMP(엔진 archive) 에서
#      읽기 전용 CLI 3종을 돌려 각 rc/관측을 판정한다.
#
# 왜 엔진(ENGINE_TMP)과 볼트(VAULT_TMP)를 분리하나 (핵심):
#   - worker.py 의 ROOT / usage_guard.py 는 PR #167 로, orchestrator/human.py ·
#     orchestrator/stages.py 는 [[macp-vault-root-human-stages-modules]] 로
#     각각 `paths.vault_root(...)` 로 마이그레이션돼(human·stages 는
#     `_VAULT_DATA_ROOT`) 셋 다 볼트 데이터에 MACP_VAULT_ROOT 를 존중한다.
#     세 모듈 모두 엔진 파일(`.claude/commands`, `scripts/kakao_send.sh`,
#     `scripts/health_report.py`)만 `__file__` 기준 `_ENGINE_ROOT` 에 앵커한다
#     (human·stages 는 `parents[2]`, worker.py 는 `parents[1]` —
#     macp-public-split-11-worker-engine-root-split, 인벤토리 §11.8 c-1). 이건
#     의도된 것이라 MACP_VAULT_ROOT 를 무시하는 게 맞다.
#   - 덕분에 (a) 남은 하위 모듈의 결합 갭이 드러날 여지가 있고, (b) if_then
#     안전조건(실제 볼트 Todo/·Memory/·Worker/ 를 절대 안 건드림)이 레이아웃만으로
#     자동 충족된다. temp 는 전부 `mktemp -d` 아래, EXIT trap 정리.
#
# 판정: 마이그레이션된 2종(cli list · session_log list) rc=0 → RESULT: PASS,
#       exit 0. worker 레그(run-next --dry-run)도 이제 세 모듈 모두
#       paths.vault_root() 를 존중해 rc=0 이 정상이지만, 회귀를 잡기 위해
#       rc≠0 이어도 스크립트를 실패시키지는 않는다 (기록만).
#
# 재실행:  bash scripts/check_engine_isolation.sh
#
set -euo pipefail

# 모든 git 접근은 리포 루트 기준 읽기 전용(`git -C "$REPO"`).
REPO="$(git rev-parse --show-toplevel)"

# 격리 temp: 엔진 archive 와 throwaway 볼트를 분리 배치.
TMPROOT="$(mktemp -d)"
ENGINE_TMP="$TMPROOT/engine"
VAULT_TMP="$TMPROOT/vault"
mkdir -p "$ENGINE_TMP" "$VAULT_TMP"
# 특정 경로만 정리 — 광역 rm/glob 금지.
trap 'rm -rf "$TMPROOT"' EXIT

echo "== check_engine_isolation =="
echo "repo:       $REPO"
echo "engine_tmp: $ENGINE_TMP"
echo "vault_tmp:  $VAULT_TMP"
echo

# --- Phase 1: 엔진 트리만 archive (HEAD 커밋 트리 기준) ----------------------
# git archive 는 커밋된 트리만 반영하므로 이 스크립트 자신의 최신 편집은
# 검증 대상이 아니다(검증 대상 = HEAD 의 엔진 모듈). 대상 경로는 전부 tracked.
echo "[phase1] archiving engine tree -> $ENGINE_TMP"
git -C "$REPO" archive HEAD todo_system scripts .claude/commands pyproject.toml uv.lock README.md LICENSE \
  | tar -x -C "$ENGINE_TMP"
echo "[phase1] engine files extracted: $(find "$ENGINE_TMP" -type f | wc -l)"
echo

# --- Phase 2: throwaway 볼트 픽스처 (VAULT_TMP) ------------------------------
# 마이그레이션 CLI(cli/session_log)가 MACP_VAULT_ROOT 로 읽을 최소 redirectable
# 볼트. 실제 볼트는 절대 건드리지 않는다 — 전부 mktemp 아래의 throwaway.
echo "[phase2] building throwaway vault fixture -> $VAULT_TMP"
mkdir -p \
  "$VAULT_TMP/Todo/items" \
  "$VAULT_TMP/Memory/sessions" \
  "$VAULT_TMP/Memory/episodes" \
  "$VAULT_TMP/Notes" \
  "$VAULT_TMP/Decisions" \
  "$VAULT_TMP/Worker"

# 최소 유효 frontmatter 태스크 1개 — cli import-md/list 가 읽어들일 대상.
# (parse_task_file 은 .get() 기반이라 필드 누락에도 안전하지만, 문서화된
#  work-task 템플릿 형태를 지켜 회귀 없이 리스트에 뜨게 한다.)
cat > "$VAULT_TMP/Todo/items/sample-isolation-check.md" <<'TASK_EOF'
---
tags:
  - todo
  - infra
status: open
due: "-"
updated: 2026-09-21
outcome: "격리 검증용 throwaway 태스크 — 엔진이 MACP_VAULT_ROOT 를 읽는지 확인"
next_action: "cli list 출력에 이 태스크가 나타나면 엔진이 볼트를 존중하는 것"
if_then: "throwaway 볼트에서만 쓰인다"
estimate_p50: 5m
estimate_p90: 10m
review_cycle: "-"
---

# 격리 검증용 샘플 태스크

- [ ] cli list 에 이 태스크가 나타나는지 확인 (5m)

## Progress Log

- 2026-09-21: check_engine_isolation.sh 픽스처로 생성
TASK_EOF

echo "[phase2] vault fixture ready ($(find "$VAULT_TMP" -type f | wc -l) file(s))"
echo

# --- Phase 3: 격리 3레그 실행 + rc/관측 표 + exit 정책 -----------------------
# 각 레그는 cwd=ENGINE_TMP(엔진 archive) + MACP_VAULT_ROOT=VAULT_TMP 로 실행한다.
# 엔진 자신의 pyproject.toml/uv.lock 로 `uv run --directory` → 프로젝트 deps
# (google-*·pyyaml)를 엔진 매니페스트로 해석(uv 캐시 warm 이면 offline·수초).
# cwd 가 ENGINE_TMP 라 `python -m todo_system.*` 가 archive 패키지를 import 하고,
# worker.py 의 `_ENGINE_ROOT`(parents[1]) 도 ENGINE_TMP 로 잡힌다.
#
# 이 리포/셸이 이미 갖고 있을 수 있는 리다이렉트 env 를 중화해 격리를 확정한다:
#   WORKER_HOST        → worker 를 remote-host 모드로 바꿔 _STATE_DIR 이 달라짐
#   MAESTRO_VAULT_DIR  → brief/tick 계열 별칭
#   KAKAO_VAULT_DIR    → kakao 계열 별칭
#   VIRTUAL_ENV        → 활성화된 실제 리포 .venv 가 새어들지 않게(uv 는 어차피
#                        ENGINE_TMP/.venv 를 새로 만들지만 경고를 없애고 hermetic)
ISO_ENV=(env -u WORKER_HOST -u MAESTRO_VAULT_DIR -u KAKAO_VAULT_DIR -u VIRTUAL_ENV
         MACP_VAULT_ROOT="$VAULT_TMP")

# rc + 병합 출력(stderr 포함)을 안전하게 캡처. set -e 하에서 실패한 명령치환이
# 스크립트를 죽이지 않도록 set +e 로 감쌌다가 복원한다.
CAP_OUT=""
CAP_RC=0
capture() {
  set +e
  CAP_OUT="$("${ISO_ENV[@]}" "$@" 2>&1)"
  CAP_RC=$?
  set -e
}

# 마지막 비어있지 않은 줄 = 관측 요약. 표 파이프와 충돌하지 않게 '|' 는 '/' 로.
# tail -n 1 은 입력을 끝까지 읽으므로 SIGPIPE 없음; 전부 공백이면 grep rc=1 →
# pipefail 로 죽지 않게 || true.
last_line() {
  printf '%s\n' "$1" | grep -v '^[[:space:]]*$' | tail -n 1 | tr '|' '/' || true
}

# Prime (비레그, 필수 선행): cli list 는 SELECT ... FROM tasks 직행이라 DB 가
# 없으면 no-such-table 로 죽는다 → import-md 로 VAULT_TMP/Todo/todo.db 를 먼저
# 만든다. throwaway 라 부수효과(todo.db·NOW/FOCUS 생성) 무방.
echo "[phase3] prime: cli import-md (build VAULT_TMP/Todo/todo.db)"
capture uv run --directory "$ENGINE_TMP" python -m todo_system.cli import-md
PRIME_RC=$CAP_RC
echo "$CAP_OUT" | sed 's/^/    prime| /'
echo "[phase3] prime rc=$PRIME_RC"
echo

# Leg A (마이그레이션, 필수): cli list — MACP_VAULT_ROOT 존중(paths.vault_root).
echo "[phase3] leg A: cli list"
capture uv run --directory "$ENGINE_TMP" python -m todo_system.cli list
A_RC=$CAP_RC; A_OBS="$(last_line "$CAP_OUT")"
echo "$CAP_OUT" | sed 's/^/    A| /'
echo "[phase3] leg A rc=$A_RC"
echo

# Leg B (마이그레이션, 필수, stdlib): session_log list.
echo "[phase3] leg B: session_log list"
capture uv run --directory "$ENGINE_TMP" python -m todo_system.session_log list
B_RC=$CAP_RC; B_OBS="$(last_line "$CAP_OUT")"
echo "$CAP_OUT" | sed 's/^/    B| /'
echo "[phase3] leg B rc=$B_RC"
echo

# Leg C (마이그레이션 완료, 기록만): worker run-next --dry-run.
# worker.py 자신의 ROOT(PR #167), 그리고 run-next 가 참조하는
# orchestrator/human.py · orchestrator/stages.py 의 _VAULT_DATA_ROOT
# ([[macp-vault-root-human-stages-modules]])는 모두 paths.vault_root() 로
# 마이그레이션돼 MACP_VAULT_ROOT 를 존중한다. rc≠0 이면 회귀이지만, 이 레그는
# 기록만 하고 스크립트를 실패시키지 않는다(exit 정책은 레그 A/B 만 본다).
# 이 레그는 worker 의 엔진 파일 경로(COMMANDS_DIR·KAKAO_SEND_SCRIPT·
# HEALTH_REPORT_SCRIPT)를 타지 않으므로 그 결합은
# todo_system/tests/test_worker_engine_root.py 가 잡는다(인벤토리 §11.9).
echo "[phase3] leg C: worker run-next --dry-run"
capture uv run --directory "$ENGINE_TMP" python -m todo_system.worker run-next --dry-run
C_RC=$CAP_RC; C_OUT="$CAP_OUT"; C_OBS="$(last_line "$CAP_OUT")"
echo "$CAP_OUT" | sed 's/^/    C| /'
echo "[phase3] leg C rc=$C_RC"
echo

# --- rc/관측 표 (그대로 Notes §6 로 옮겨 적기 좋은 형태) --------------------
echo "================ ISOLATION RESULT TABLE ================"
printf '%s\n' "| Command | rc | MACP_VAULT_ROOT 존중? | 관측 |"
printf '%s\n' "|---|---:|---|---|"
printf '| %s | %s | %s | %s |\n' \
  "python -m todo_system.cli list"                  "$A_RC" "예 (paths.vault_root→env)"                        "$A_OBS"
printf '| %s | %s | %s | %s |\n' \
  "python -m todo_system.session_log list"          "$B_RC" "예 (paths.vault_root→env, stdlib)"                 "$B_OBS"
printf '| %s | %s | %s | %s |\n' \
  "python -m todo_system.worker run-next --dry-run" "$C_RC" "예 (worker.py·human.py·stages.py 모두 paths.vault_root→env)"  "$C_OBS"
printf '%s\n' "(prime: cli import-md rc=$PRIME_RC)"
echo "======================================================="
echo

# --- 결합 갭 소견 (worker 레그) --------------------------------------------
if [ "$C_RC" -ne 0 ]; then
  echo "REGRESSION: worker run-next --dry-run rc=$C_RC — worker.py · orchestrator/human.py ·"
  echo "  orchestrator/stages.py 는 모두 paths.vault_root() 로 마이그레이션되어"
  echo "  MACP_VAULT_ROOT 를 존중해야 정상이다(PR #167,"
  echo "  [[macp-vault-root-human-stages-modules]]). rc≠0 은 회귀일 가능성이 높다 —"
  echo "  스크립트 exit 자체는 영향 없음(레그 A/B 만 판정에 반영)."
  echo
else
  case "$C_OUT" in
    *"no pending runs"*|*"entering planner path"*|*"[planner]"*)
      echo "NOTE: worker 레그 rc=0 — worker.py · orchestrator/human.py ·"
      echo "  orchestrator/stages.py 모두 paths.vault_root() 마이그레이션되어"
      echo "  MACP_VAULT_ROOT(VAULT_TMP)를 존중한다(PR #167,"
      echo "  [[macp-vault-root-human-stages-modules]]). 'no pending runs' 는 VAULT_TMP"
      echo "  픽스처에 Worker/queue.json 이 없어 나오는 정상 결과다(레그 A/B처럼"
      echo "  VAULT_TMP 를 읽었다는 뜻). 세 모듈 모두 엔진 파일(.claude/commands,"
      echo "  scripts/)만 __file__ 기준 _ENGINE_ROOT 에 앵커하며(worker.py 포함),"
      echo "  이는 의도된 것이라 MACP_VAULT_ROOT 를 무시하는 게 맞다."
      echo
      ;;
  esac
fi

# --- exit 정책: 마이그레이션 2종 rc=0 이면 PASS -----------------------------
if [ "$A_RC" -eq 0 ] && [ "$B_RC" -eq 0 ]; then
  echo "RESULT: PASS"
  exit 0
else
  echo "RESULT: FAIL (cli list rc=$A_RC, session_log list rc=$B_RC — 마이그레이션 레그는 rc=0 이어야 함)"
  exit 1
fi
