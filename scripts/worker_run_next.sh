#!/bin/zsh
# Vault upkeep entry. Invoked by LaunchAgent every 5 minutes (or manually).
#
# This used to be the v1 executor: `scan-queue` (register auto tasks in
# Worker/queue.json) + `run-next` (execute one). The v2 cutover moved execution
# to the orchestrator + runner daemons (ledger is the SoT, queue.json is a
# derived view), but this script was never switched over — so BOTH executors
# stayed live, each unaware of the other's runs.
#
# The result was that every auto task ran TWICE. Mostly that only wasted quota,
# because the v2 stages detect the collision ("blocked on duplicate dispatch")
# and bail. On 2026-08-14 it did real damage: leaf 16 and leaf 17 were each
# double-run against the single ~/Worker/ai-stock-trading clone, and the four
# concurrent pipelines' branch-preps reset leaf 17's finished implementation off
# its branch and `git clean -fd`'d leaf 16's long_job artifact.
#
# `maintenance` is the post-cutover subcommand built for exactly this slot: vault
# upkeep only (remote-run sweep, vault sync and the other upkeep steps), no dispatch, no
# execution. Planning is NOT lost — the planner has its own LaunchAgent
# (<prefix>secondbrain.planner -> scripts/planner_tick.sh, every 30m).

set -euo pipefail

# Two anchors, as in orchestrator_tick.sh (11g). ENGINE_DIR is this script's
# own checkout (the todo_system package). VAULT_DIR is where the data lives
# (logs/, and the cwd todo_system.worker resolves its vault from):
# MACP_VAULT_ROOT (split install) or the engine checkout itself (combined
# layout). The LaunchAgent runs this script from inside its checkout, so the
# combined default resolves exactly as the old hardcoded fallback did.
# Unlike brief.sh / orchestrator_tick.sh this script never read
# MAESTRO_VAULT_DIR, and still does not.
ENGINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VAULT_DIR="${MACP_VAULT_ROOT:-$ENGINE_DIR}"
LOG_DIR="$VAULT_DIR/logs"
NOW_TS="$(date +%FT%T)"

export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# todo_system is not installed (package = false), so the import comes from
# the cwd, which stays the vault. `uv run --project` picks the engine venv
# without moving it; a split install also puts the engine on PYTHONPATH and
# runs python -P so a stale copy in the vault cannot shadow it. The
# combined layout leaves env and argv alone.
#
# Installed wheel (public split 13b): ENGINE_DIR is site-packages/todo_system/
# _engine — no pyproject.toml for `uv run --project`, not a package for
# PYTHONPATH. engine_script hands the caller's interpreter down as MACP_PYTHON
# and the module runs under it with -P; uv is not needed at all, so the uv
# guard below is skipped. Only when BOTH hold — no pyproject AND MACP_PYTHON
# set; a checkout (pyproject present) keeps the uv command line byte for byte.
# A bare path run of the wheel with no MACP_VAULT_ROOT would resolve the vault
# to site-packages; the units that pass the env are leaf 13c's.
INSTALLED=0
if [[ ! -f "$ENGINE_DIR/pyproject.toml" && -n "${MACP_PYTHON:-}" ]]; then
  INSTALLED=1
fi

PY=(python)
if [[ "$VAULT_DIR" != "$ENGINE_DIR" ]]; then
  export MACP_VAULT_ROOT="$VAULT_DIR"
  if [[ "$INSTALLED" -eq 0 ]]; then
    export PYTHONPATH="$ENGINE_DIR${PYTHONPATH:+:$PYTHONPATH}"
  fi
  PY=(python -P)
fi

mkdir -p "$LOG_DIR"
cd "$VAULT_DIR"

log() {
  echo "[$NOW_TS] worker_run_next: $*" | tee -a "$LOG_DIR/worker.out.log"
}

if [[ "$INSTALLED" -eq 0 ]] && ! command -v uv >/dev/null 2>&1; then
  log "ERROR uv not on PATH"
  exit 1
fi

log "maintenance"
if [[ "$INSTALLED" -eq 1 ]]; then
  "$MACP_PYTHON" -P -m todo_system.worker maintenance 2>&1 | tee -a "$LOG_DIR/worker.out.log"
else
  uv run --project "$ENGINE_DIR" "${PY[@]}" -m todo_system.worker maintenance 2>&1 | tee -a "$LOG_DIR/worker.out.log"
fi
