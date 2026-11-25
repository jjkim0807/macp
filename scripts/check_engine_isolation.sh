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
#   2. Makes a throwaway vault, VAULT_TMP, a copy of the archived vault-template/
#      (the same skeleton the manual setup copies), for the prime step and legs A/B.
#   3. With MACP_VAULT_ROOT pointed at VAULT_TMP, runs 2 read-only CLIs from
#      cwd=ENGINE_TMP (the engine archive) and judges each rc/observation.
#   4. Leg T runs the README "Try it in five minutes" commands in the same order,
#      inside a throwaway git repository with no remote: `macp init` →
#      `macp add "…"` → `macp up --once`, then `macp status` and a look at the
#      branch the worker left. It runs with a fresh HOME and every MACP_*,
#      USAGE_GUARD_* and CLAUDE_* variable unset — nothing exported at all: lite
#      mode makes the vault (~/.macp/vault) itself. `claude` is the stand-in
#      todo_system/tests/fixtures/lite_fake_claude/claude (from the archive), first
#      on PATH, so the real orchestrator and runner run with no model call.
#      UV_CACHE_DIR / UV_PYTHON_INSTALL_DIR keep their real (pre-HOME-swap)
#      locations so uv still works offline. The README's command block must still be
#      exactly those three commands, each step must exit 0, and each must print the
#      output the README promises.
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
TRY_REPO="$TRY_ROOT/my-project"
TRY_HOME="$TRY_ROOT/home"
TRY_BIN="$TRY_ROOT/bin"
mkdir -p "$ENGINE_TMP" "$VAULT_TMP" "$TRY_REPO" "$TRY_HOME" "$TRY_BIN"
# Clean up only this specific path — no broad rm/glob.
trap 'rm -rf "$TMPROOT"' EXIT

echo "== check_engine_isolation =="
echo "repo:       $REPO"
echo "engine_tmp: $ENGINE_TMP"
echo "vault_tmp:  $VAULT_TMP"
echo "try_repo:   $TRY_REPO"
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

# --- Phase 2: throwaway vault = a copy of the archived vault-template ----------
# The same skeleton the manual setup copies. The real vault is never touched —
# everything is a throwaway under mktemp. (Leg T makes its own vault: `macp init`.)
echo "[phase2] copying vault-template -> $VAULT_TMP"
cp -R "$ENGINE_TMP/vault-template/." "$VAULT_TMP/"
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
#   MAESTRO_VAULT_DIR  → brief/tick-family alias
#   KAKAO_VAULT_DIR    → kakao-family alias
#   VIRTUAL_ENV        → keeps the activated real repo .venv from leaking in (uv makes
#                        a fresh ENGINE_TMP/.venv anyway, but this removes the
#                        warning and keeps it hermetic)
ISO_ENV=(env -u MAESTRO_VAULT_DIR -u KAKAO_VAULT_DIR -u VIRTUAL_ENV
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
# The README's three commands, in order, from inside a git repository of the
# user's (TRY_REPO: one commit, no remote). `macp` is the console script of the
# archived engine (`uv run --project <engine> macp …` stands in for the
# `uv tool install` line). The env is what a newcomer's fresh shell would have: a
# fresh HOME and no MACP_* / USAGE_GUARD_* / CLAUDE_* variable at all — the README
# exports nothing. The variable names are listed at run time (compgen -e), not from
# a fixed list, so a new knob in the caller's shell cannot leak in. `claude` on PATH
# is the archive's stand-in (fixtures/lite_fake_claude): it answers the model probe,
# `macp add`'s JSON request and the quick stage, so the real orchestrator and runner
# run end to end with no model call and nothing leaving the machine.
# uv keeps its cache and managed Pythons under HOME. Read their real locations before
# swapping HOME and pass them through, so the leg works offline (the pytest gate
# runs this script with UV_OFFLINE=1). If `uv python dir` fails, it is skipped.
REAL_UV_CACHE="$(uv cache dir 2>/dev/null || true)"
REAL_UV_PYTHON="$(uv python dir 2>/dev/null || true)"
TRY_ENV=(env)
while IFS= read -r name; do
  case "$name" in
    MACP_*|USAGE_GUARD_*|CLAUDE_*) TRY_ENV+=(-u "$name") ;;
  esac
done < <(compgen -e)
TRY_ENV+=(-u MAESTRO_VAULT_DIR -u KAKAO_VAULT_DIR -u VIRTUAL_ENV -u PYTHONPATH
          HOME="$TRY_HOME" PATH="$TRY_BIN:$PATH")
if [ -n "$REAL_UV_CACHE" ]; then TRY_ENV+=(UV_CACHE_DIR="$REAL_UV_CACHE"); fi
if [ -n "$REAL_UV_PYTHON" ]; then TRY_ENV+=(UV_PYTHON_INSTALL_DIR="$REAL_UV_PYTHON"); fi

# The stand-in `claude`, and the user's repository: one commit, no remote. The
# identity is passed inline (the fresh HOME has none); nothing global is written.
cp "$ENGINE_TMP/todo_system/tests/fixtures/lite_fake_claude/claude" "$TRY_BIN/claude"
chmod 0755 "$TRY_BIN/claude"
git -C "$TRY_REPO" init -q -b main
printf '# my project\n' > "$TRY_REPO/README.md"
git -C "$TRY_REPO" add README.md
git -C "$TRY_REPO" -c user.name=newcomer -c user.email=newcomer@example.invalid \
  commit -q -m "first commit"

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
TRY_STEP_TOTAL=6
TRY_EXPECT_TOTAL=6

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

# Step 0: the README's command block is still exactly the three commands run below
# (comments dropped, first two words compared). One more command in the block — the
# number a newcomer has to type — fails the leg instead of drifting past it.
README_CMDS_EXPECTED=$'macp init\nmacp add\nmacp up'
try_readme_block() {
  local got
  echo "[phase3] leg T step 0: README 'Try it in five minutes' command block"
  got="$(awk '/^## Try it in five minutes/ {s = 1} s && /^```bash/ {b = 1; next}
              b && /^```/ {exit} b {print}' "$ENGINE_TMP/README.md" \
         | sed -e 's/[[:space:]]#.*$//' -e '/^[[:space:]]*$/d' \
         | awk '{print $1, $2}' || true)"
  printf '%s\n' "$got" | sed 's/^/    T| /'
  if [ "$got" = "$README_CMDS_EXPECTED" ]; then
    TRY_STEPS=$((TRY_STEPS + 1))
    TRY_EXPECTS=$((TRY_EXPECTS + 1))
  else
    TRY_OK=0
    TRY_FAIL="step 0: README command block is not 'macp init / macp add / macp up'"
  fi
}

echo "[phase3] leg T: README five-minute try (clean HOME, nothing exported, stand-in claude)"
try_readme_block
pushd "$TRY_REPO" >/dev/null
try_step 1 fixed "vault created: $TRY_HOME/.macp/vault" \
  uv run --project "$ENGINE_TMP" macp init
try_step 2 fixed "task added: readme-install-line (quick)" \
  uv run --project "$ENGINE_TMP" macp add "Add an install section to the README"
try_step 3 fixed "dispatched readme-install-line -> worker-default" \
  uv run --project "$ENGINE_TMP" macp up --once
try_step 4 regex 'awaiting_human[[:space:]]+readme-install-line[[:space:]]+worker-default' \
  uv run --project "$ENGINE_TMP" macp status
try_step 5 fixed "chore(worker): readme-install-line" \
  git -C "$TRY_REPO" log --format=%s main..worker/readme-install-line
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
  "README five-minute try (clean HOME)"             "$T_RC" "env 없음 (macp init → ~/.macp/vault, HOME=mktemp)" "$T_OBS"
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
