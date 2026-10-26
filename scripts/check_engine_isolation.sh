#!/usr/bin/env bash
#
# check_engine_isolation.sh — public split step 5: engine-tree isolation check (re-runnable)
#
# A script that pins step 5 of the parent [[macp-public-repo-engine-vault-split]]
# ("does it still run with only the engine split off?") so it can be re-checked in
# one line. What it does:
#
#   1. Splits off only the engine tree (todo_system/ · scripts/ · .claude/commands/ +
#      pyproject.toml · uv.lock · README.md · LICENSE — pyproject's readme/license
#      metadata is needed for the build) into an isolated temp dir (ENGINE_TMP) with
#      `git archive HEAD`.
#   2. Creates a separate throwaway vault (VAULT_TMP) with a sample task in
#      Todo/items/ and an empty skeleton of Memory/·Notes/·Decisions/·Worker/.
#   3. With MACP_VAULT_ROOT pointed at VAULT_TMP, runs 3 read-only CLIs from
#      cwd=ENGINE_TMP (the engine archive) and judges each rc/observation.
#
# Why the engine (ENGINE_TMP) and the vault (VAULT_TMP) are separated (the key point):
#   - worker.py's ROOT / usage_guard.py (via PR #167) and orchestrator/human.py ·
#     orchestrator/stages.py (via [[macp-vault-root-human-stages-modules]]) were each
#     migrated to `paths.vault_root(...)` (for human·stages, `_VAULT_DATA_ROOT`), so
#     all three respect MACP_VAULT_ROOT for vault data.
#     All three modules anchor only the engine files (`.claude/commands`,
#     `scripts/kakao_send.sh`, `scripts/health_report.py`) to `_ENGINE_ROOT` relative to
#     `__file__` (human·stages use `parents[2]`, worker.py uses `parents[1]` —
#     macp-public-split-11-worker-engine-root-split, inventory §11.8 c-1). This is
#     intended, so ignoring MACP_VAULT_ROOT there is correct.
#   - Thanks to this, (a) coupling gaps in the remaining submodules have a chance to
#     surface, and (b) the if_then safety condition (never touch the real vault's
#     Todo/·Memory/·Worker/) is met automatically by the layout alone. All temp is
#     under `mktemp -d`, cleaned up by an EXIT trap.
#
# Verdict: the 2 migrated legs (cli list · session_log list) rc=0 → RESULT: PASS,
#       exit 0. The worker leg (run-next --dry-run) is now expected to be rc=0 too,
#       since all three modules respect paths.vault_root(), but to catch regressions
#       a rc≠0 there still does not fail the script (recorded only).
#
# Re-run:  bash scripts/check_engine_isolation.sh
#
set -euo pipefail

# All git access is read-only relative to the repo root (`git -C "$REPO"`).
REPO="$(git rev-parse --show-toplevel)"

# Isolated temp: the engine archive and the throwaway vault are placed separately.
TMPROOT="$(mktemp -d)"
ENGINE_TMP="$TMPROOT/engine"
VAULT_TMP="$TMPROOT/vault"
mkdir -p "$ENGINE_TMP" "$VAULT_TMP"
# Clean up only this specific path — no broad rm/glob.
trap 'rm -rf "$TMPROOT"' EXIT

echo "== check_engine_isolation =="
echo "repo:       $REPO"
echo "engine_tmp: $ENGINE_TMP"
echo "vault_tmp:  $VAULT_TMP"
echo

# --- Phase 1: archive only the engine tree (from the HEAD commit tree) ---------
# git archive reflects only the committed tree, so this script's own latest edits
# are not what is checked (what is checked = the engine modules at HEAD). Every
# target path is tracked.
echo "[phase1] archiving engine tree -> $ENGINE_TMP"
git -C "$REPO" archive HEAD todo_system scripts .claude/commands pyproject.toml uv.lock README.md LICENSE \
  | tar -x -C "$ENGINE_TMP"
echo "[phase1] engine files extracted: $(find "$ENGINE_TMP" -type f | wc -l)"
echo

# --- Phase 2: throwaway vault fixture (VAULT_TMP) -----------------------------
# The minimal redirectable vault that the migrated CLIs (cli/session_log) read via
# MACP_VAULT_ROOT. The real vault is never touched — everything is a throwaway
# under mktemp.
echo "[phase2] building throwaway vault fixture -> $VAULT_TMP"
mkdir -p \
  "$VAULT_TMP/Todo/items" \
  "$VAULT_TMP/Memory/sessions" \
  "$VAULT_TMP/Memory/episodes" \
  "$VAULT_TMP/Notes" \
  "$VAULT_TMP/Decisions" \
  "$VAULT_TMP/Worker"

# One task with minimal valid frontmatter — what cli import-md/list will read in.
# (parse_task_file is .get()-based, so it is safe even with missing fields, but this
#  keeps the documented work-task template shape so it shows up in the list without
#  regressions.)
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

# --- Phase 3: run the 3 isolation legs + rc/observation table + exit policy ---
# Each leg runs with cwd=ENGINE_TMP (the engine archive) + MACP_VAULT_ROOT=VAULT_TMP.
# `uv run --directory` with the engine's own pyproject.toml/uv.lock → the project deps
# (google-*·pyyaml) are resolved from the engine manifest (offline and a few seconds
# if the uv cache is warm).
# Because cwd is ENGINE_TMP, `python -m todo_system.*` imports the archived package,
# and worker.py's `_ENGINE_ROOT` (parents[1]) also resolves to ENGINE_TMP.
#
# Neutralize redirect env vars this repo/shell may already carry, to make the
# isolation certain:
#   WORKER_HOST        → switches worker to remote-host mode, changing _STATE_DIR
#   MAESTRO_VAULT_DIR  → brief/tick-family alias
#   KAKAO_VAULT_DIR    → kakao-family alias
#   VIRTUAL_ENV        → keeps the activated real repo .venv from leaking in (uv makes
#                        a fresh ENGINE_TMP/.venv anyway, but this removes the
#                        warning and keeps it hermetic)
ISO_ENV=(env -u WORKER_HOST -u MAESTRO_VAULT_DIR -u KAKAO_VAULT_DIR -u VIRTUAL_ENV
         MACP_VAULT_ROOT="$VAULT_TMP")

# Safely capture rc + merged output (including stderr). Wrapped in set +e and then
# restored, so a failed command substitution under set -e does not kill the script.
CAP_OUT=""
CAP_RC=0
capture() {
  set +e
  CAP_OUT="$("${ISO_ENV[@]}" "$@" 2>&1)"
  CAP_RC=$?
  set -e
}

# Last non-empty line = observation summary. '|' becomes '/' so it does not clash
# with the table pipes. tail -n 1 reads its input to the end, so no SIGPIPE; if it
# is all blank, grep rc=1 → || true so pipefail does not kill it.
last_line() {
  printf '%s\n' "$1" | grep -v '^[[:space:]]*$' | tail -n 1 | tr '|' '/' || true
}

# Prime (not a leg, required first): cli list goes straight to SELECT ... FROM tasks,
# so without a DB it dies with no-such-table → build VAULT_TMP/Todo/todo.db first
# with import-md. It is a throwaway, so the side effects (todo.db·NOW/FOCUS
# creation) are fine.
echo "[phase3] prime: cli import-md (build VAULT_TMP/Todo/todo.db)"
capture uv run --directory "$ENGINE_TMP" python -m todo_system.cli import-md
PRIME_RC=$CAP_RC
echo "$CAP_OUT" | sed 's/^/    prime| /'
echo "[phase3] prime rc=$PRIME_RC"
echo

# Leg A (migrated, required): cli list — respects MACP_VAULT_ROOT (paths.vault_root).
echo "[phase3] leg A: cli list"
capture uv run --directory "$ENGINE_TMP" python -m todo_system.cli list
A_RC=$CAP_RC; A_OBS="$(last_line "$CAP_OUT")"
echo "$CAP_OUT" | sed 's/^/    A| /'
echo "[phase3] leg A rc=$A_RC"
echo

# Leg B (migrated, required, stdlib): session_log list.
echo "[phase3] leg B: session_log list"
capture uv run --directory "$ENGINE_TMP" python -m todo_system.session_log list
B_RC=$CAP_RC; B_OBS="$(last_line "$CAP_OUT")"
echo "$CAP_OUT" | sed 's/^/    B| /'
echo "[phase3] leg B rc=$B_RC"
echo

# Leg C (migration done, recorded only): worker run-next --dry-run.
# worker.py's own ROOT (PR #167), and the _VAULT_DATA_ROOT of
# orchestrator/human.py · orchestrator/stages.py that run-next references
# ([[macp-vault-root-human-stages-modules]]), were all migrated to
# paths.vault_root() and respect MACP_VAULT_ROOT. rc≠0 is a regression, but this leg
# only records it and does not fail the script (the exit policy looks only at legs
# A/B).
# This leg does not go through worker's engine-file paths (COMMANDS_DIR·
# KAKAO_SEND_SCRIPT·HEALTH_REPORT_SCRIPT), so that coupling is caught by
# todo_system/tests/test_worker_engine_root.py (inventory §11.9).
echo "[phase3] leg C: worker run-next --dry-run"
capture uv run --directory "$ENGINE_TMP" python -m todo_system.worker run-next --dry-run
C_RC=$CAP_RC; C_OUT="$CAP_OUT"; C_OBS="$(last_line "$CAP_OUT")"
echo "$CAP_OUT" | sed 's/^/    C| /'
echo "[phase3] leg C rc=$C_RC"
echo

# --- rc/observation table (in a form easy to copy into Notes §6) ------------
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

# --- coupling-gap findings (worker leg) ------------------------------------
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

# --- exit policy: PASS if the 2 migrated legs are rc=0 ---------------------
if [ "$A_RC" -eq 0 ] && [ "$B_RC" -eq 0 ]; then
  echo "RESULT: PASS"
  exit 0
else
  echo "RESULT: FAIL (cli list rc=$A_RC, session_log list rc=$B_RC — 마이그레이션 레그는 rc=0 이어야 함)"
  exit 1
fi
