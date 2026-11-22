#!/usr/bin/env bash
#
# check_engine_isolation.sh — public split step 5: engine-tree isolation check (re-runnable)
#
# A script that pins step 5 of the parent [[macp-public-repo-engine-vault-split]]
# ("does it still run with only the engine split off?") so it can be re-checked in
# one line. It also runs the README "Try it in five minutes" loop, so the onboarding
# a newcomer follows is checked on every run. What it does:
#
#   1. Splits off only the engine tree (todo_system/ · scripts/ · .claude/commands/ ·
#      vault-template/ + pyproject.toml · uv.lock · README.md · LICENSE — pyproject's
#      readme/license metadata is needed for the build) into an isolated temp dir
#      (ENGINE_TMP) with `git archive HEAD`.
#   2. Makes two throwaway vaults, each a copy of the archived vault-template/ (the
#      same skeleton a newcomer copies):
#        VAULT_TMP — for the prime step and legs A/B.
#        TRY_VAULT — for leg T only.
#   3. With MACP_VAULT_ROOT pointed at VAULT_TMP, runs 2 read-only CLIs from
#      cwd=ENGINE_TMP (the engine archive) and judges each rc/observation.
#   4. Leg T runs the README "Try it in five minutes" commands in the same order
#      (cli new → set worker_mode: auto + outcome → cli validate → cli import-md →
#      orchestrator auto → runner --mode shadow --once → orchestrator auto →
#      orchestrator status). It runs with a fresh HOME, every MACP_* and
#      USAGE_GUARD_* variable unset, and only MACP_VAULT_ROOT=TRY_VAULT and
#      USAGE_GUARD_DISABLE=1 set. UV_CACHE_DIR / UV_PYTHON_INSTALL_DIR keep their
#      real (pre-HOME-swap) locations so uv still works offline. Each step must exit 0
#      and print the output the README promises.
#
# Why the engine (ENGINE_TMP) and the vault (VAULT_TMP) are separated (the key point):
#   - worker.py's ROOT / usage_guard.py (via PR #167) and orchestrator/human.py ·
#     orchestrator/stages.py (via [[macp-vault-root-human-stages-modules]]) were each
#     migrated to `paths.vault_root(...)` (for human·stages, `_VAULT_DATA_ROOT`), so
#     all three respect MACP_VAULT_ROOT for vault data.
#     All three modules anchor only the engine files (`scripts/kakao_send.sh`,
#     `scripts/health_report.py`; the stage prompts sit in the package,
#     `todo_system/prompts`) to `_ENGINE_ROOT` relative to
#     `__file__` (human·stages use `parents[2]`, worker.py uses `parents[1]` —
#     macp-public-split-11-worker-engine-root-split, inventory §11.8 c-1). This is
#     intended, so ignoring MACP_VAULT_ROOT there is correct.
#   - Thanks to this, (a) coupling gaps in the remaining submodules have a chance to
#     surface, and (b) the if_then safety condition (never touch the real vault's
#     Todo/·Memory/·Worker/) is met automatically by the layout alone. All temp is
#     under `mktemp -d`, cleaned up by an EXIT trap.
#
# Verdict: RESULT: PASS (exit 0) only when the 2 migrated legs (cli list ·
#       session_log list) are rc=0 AND leg T (the README five-minute loop) passes every
#       step and every expected output.
#
# Re-run:  bash scripts/check_engine_isolation.sh
#
set -euo pipefail

# All git access is read-only relative to the repo root (`git -C "$REPO"`).
REPO="$(git rev-parse --show-toplevel)"

# Isolated temp: the engine archive and the throwaway vaults are placed separately.
TMPROOT="$(mktemp -d)"
ENGINE_TMP="$TMPROOT/engine"
VAULT_TMP="$TMPROOT/vault"
TRY_ROOT="$TMPROOT/try"
TRY_VAULT="$TRY_ROOT/my-vault"
TRY_HOME="$TRY_ROOT/home"
mkdir -p "$ENGINE_TMP" "$VAULT_TMP" "$TRY_VAULT" "$TRY_HOME"
# Clean up only this specific path — no broad rm/glob.
trap 'rm -rf "$TMPROOT"' EXIT

echo "== check_engine_isolation =="
echo "repo:       $REPO"
echo "engine_tmp: $ENGINE_TMP"
echo "vault_tmp:  $VAULT_TMP"
echo "try_vault:  $TRY_VAULT"
echo

# --- Phase 1: archive only the engine tree (from the HEAD commit tree) ---------
# git archive reflects only the committed tree, so this script's own latest edits
# are not what is checked (what is checked = the engine modules at HEAD). Every
# target path is tracked.
echo "[phase1] archiving engine tree -> $ENGINE_TMP"
git -C "$REPO" archive HEAD todo_system scripts .claude/commands vault-template \
    pyproject.toml uv.lock README.md LICENSE \
  | tar -x -C "$ENGINE_TMP"
echo "[phase1] engine files extracted: $(find "$ENGINE_TMP" -type f | wc -l)"
echo

# --- Phase 2: throwaway vaults = copies of the archived vault-template ---------
# The same skeleton the README tells a newcomer to `cp -r`. The real vault is never
# touched — everything is a throwaway under mktemp.
echo "[phase2] copying vault-template -> $VAULT_TMP and $TRY_VAULT"
cp -R "$ENGINE_TMP/vault-template/." "$VAULT_TMP/"
cp -R "$ENGINE_TMP/vault-template/." "$TRY_VAULT/"
echo "[phase2] vault fixture ready ($(find "$VAULT_TMP" -type f | wc -l) file(s))"
echo

# --- Phase 3: run the isolation legs + rc/observation table + exit policy ---
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
# creation) are fine. The template's Todo/items/ is empty, so it imports 0 tasks.
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

# --- Leg T (required): the README "Try it in five minutes" loop ----------------
# Same commands, same order, same shape as the README: the cli commands run with
# cwd = the engine checkout, the orchestrator/runner with `--project <engine>`.
# The env is what a newcomer's fresh shell would have: a fresh HOME, no MACP_* /
# USAGE_GUARD_* variable except the two the README exports. The variable names are
# listed at run time (compgen -e), not from a fixed list, so a new MACP_* knob in
# the caller's shell cannot leak in.
# uv keeps its cache and managed Pythons under HOME. Read their real locations before
# swapping HOME and pass them through, so the leg works offline (the pytest gate
# runs this script with UV_OFFLINE=1). If `uv python dir` fails, it is skipped.
REAL_UV_CACHE="$(uv cache dir 2>/dev/null || true)"
REAL_UV_PYTHON="$(uv python dir 2>/dev/null || true)"
TRY_ENV=(env)
while IFS= read -r name; do
  case "$name" in
    MACP_*|USAGE_GUARD_*) TRY_ENV+=(-u "$name") ;;
  esac
done < <(compgen -e)
TRY_ENV+=(-u WORKER_HOST -u MAESTRO_VAULT_DIR -u KAKAO_VAULT_DIR -u VIRTUAL_ENV
          HOME="$TRY_HOME" MACP_VAULT_ROOT="$TRY_VAULT" USAGE_GUARD_DISABLE=1)
if [ -n "$REAL_UV_CACHE" ]; then TRY_ENV+=(UV_CACHE_DIR="$REAL_UV_CACHE"); fi
if [ -n "$REAL_UV_PYTHON" ]; then TRY_ENV+=(UV_PYTHON_INSTALL_DIR="$REAL_UV_PYTHON"); fi

capture_try() {
  set +e
  CAP_OUT="$("${TRY_ENV[@]}" "$@" 2>&1)"
  CAP_RC=$?
  set -e
}

# Stop at the first failure. A failure is rc≠0 or a missing expected string.
# Expected strings are matched from a here-string, not a pipe, so `grep -q` exiting
# early cannot SIGPIPE the producer under pipefail and fake a failure.
TRY_OK=1
TRY_FAIL=""
TRY_STEPS=0
TRY_EXPECTS=0
TRY_STEP_TOTAL=8
TRY_EXPECT_TOTAL=5

# try_step <n> <fixed|regex|none> <needle> <cmd...>
try_step() {
  local n="$1" kind="$2" needle="$3"
  shift 3
  if [ "$TRY_OK" -ne 1 ]; then return 0; fi
  echo "[phase3] leg T step $n: $*"
  capture_try "$@"
  printf '%s\n' "$CAP_OUT" | sed 's/^/    T| /'
  echo "[phase3] leg T step $n rc=$CAP_RC"
  if [ "$CAP_RC" -ne 0 ]; then
    TRY_OK=0
    TRY_FAIL="step $n: rc=$CAP_RC"
    return 0
  fi
  TRY_STEPS=$((TRY_STEPS + 1))
  case "$kind" in
    fixed)
      if grep -F -q -- "$needle" <<<"$CAP_OUT"; then
        TRY_EXPECTS=$((TRY_EXPECTS + 1))
      else
        TRY_OK=0
        TRY_FAIL="step $n: missing '$needle'"
      fi
      ;;
    regex)
      if grep -E -q -- "$needle" <<<"$CAP_OUT"; then
        TRY_EXPECTS=$((TRY_EXPECTS + 1))
      else
        TRY_OK=0
        TRY_FAIL="step $n: missing /$needle/"
      fi
      ;;
  esac
}

# The README's hand edit: write an outcome and switch worker_mode blocked → auto.
# Portable sed (no -i: GNU and BSD differ). If the scaffold no longer has the lines
# the README tells the user to change, that is template drift and the leg fails.
try_edit_task() {
  if [ "$TRY_OK" -ne 1 ]; then return 0; fi
  local f="$TRY_VAULT/Todo/items/hello-world.md"
  echo "[phase3] leg T step 2: edit Todo/items/hello-world.md (outcome, worker_mode: auto)"
  if [ -f "$f" ] \
    && sed -e 's/^worker_mode: blocked$/worker_mode: auto/' \
           -e 's/^outcome: .*/outcome: "hello-world runs through the shadow loop"/' \
           "$f" > "$f.tmp" \
    && mv "$f.tmp" "$f" \
    && grep -q '^worker_mode: auto$' "$f" \
    && grep -q '^outcome: "hello-world runs through the shadow loop"$' "$f"; then
    TRY_STEPS=$((TRY_STEPS + 1))
    echo "[phase3] leg T step 2 ok"
  else
    TRY_OK=0
    TRY_FAIL="step 2: template drift (worker_mode/outcome lines not found)"
    echo "[phase3] leg T step 2 FAILED: template drift"
  fi
}

echo "[phase3] leg T: README five-minute try (clean HOME, MACP_VAULT_ROOT=TRY_VAULT)"
pushd "$ENGINE_TMP" >/dev/null
try_step 1 none "" \
  uv run python -m todo_system.cli new hello-world --template auto-worker
try_edit_task
try_step 3 fixed "worker contract OK" \
  uv run python -m todo_system.cli validate hello-world
try_step 4 fixed "imported tasks: 1" \
  uv run python -m todo_system.cli import-md
try_step 5 fixed "dispatched hello-world -> worker-default" \
  uv run --project "$ENGINE_TMP" python -m todo_system.orchestrator.orchestrator auto
try_step 6 fixed "shadow run for 'hello-world' (no agent spawned)" \
  uv run --project "$ENGINE_TMP" python -m todo_system.orchestrator.runner \
    --agent worker-default --mode shadow --once
try_step 7 none "" \
  uv run --project "$ENGINE_TMP" python -m todo_system.orchestrator.orchestrator auto
try_step 8 regex 'done[[:space:]]+hello-world[[:space:]]+worker-default' \
  uv run --project "$ENGINE_TMP" python -m todo_system.orchestrator.orchestrator status
popd >/dev/null
if [ "$TRY_OK" -eq 1 ]; then
  T_RC=0
  T_OBS="$TRY_STEPS/$TRY_STEP_TOTAL steps, $TRY_EXPECTS/$TRY_EXPECT_TOTAL expectations"
else
  T_RC=1
  T_OBS="$(printf '%s' "$TRY_FAIL" | tr '|' '/')"
fi
echo "[phase3] leg T rc=$T_RC ($T_OBS)"
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
  "README five-minute try (clean HOME)"             "$T_RC" "예 (MACP_VAULT_ROOT=TRY_VAULT, HOME=mktemp)"      "$T_OBS"
printf '%s\n' "(prime: cli import-md rc=$PRIME_RC)"
echo "======================================================="
echo

# --- exit policy: PASS if legs A/B are rc=0 and leg T passed -----------------
if [ "$A_RC" -eq 0 ] && [ "$B_RC" -eq 0 ] && [ "$T_RC" -eq 0 ]; then
  echo "RESULT: PASS"
  exit 0
else
  echo "RESULT: FAIL (cli list rc=$A_RC, session_log list rc=$B_RC — 마이그레이션 레그는 rc=0 이어야 함;"
  echo "  README five-minute try rc=$T_RC: ${T_OBS})"
  exit 1
fi
