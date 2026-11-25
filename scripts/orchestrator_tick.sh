#!/bin/zsh
# CUTOVER tick — v2 owns execution. Dispatch open auto tasks to agents (the
# worker-default runner executes the staged pipeline), collect replies,
# regenerate the dashboard. Activate by copying this over
# orchestrator_tick.sh (see scripts/CUTOVER.md).

set -uo pipefail

# Two anchors, same as brief.sh. ENGINE_DIR is this script's own checkout (the
# todo_system package). VAULT_DIR is where the data lives (Worker/, Todo/,
# logs/): MAESTRO_VAULT_DIR wins, then MACP_VAULT_ROOT (split install, engine
# checkout != vault), then the engine checkout itself (combined layout:
# unchanged behaviour).
ENGINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VAULT_DIR="${MAESTRO_VAULT_DIR:-${MACP_VAULT_ROOT:-$ENGINE_DIR}}"

# The orchestrator resolves its defaults (ledger, registry, dashboard paths,
# hq-host marker, the git pull) against its cwd, so the cwd stays the vault and
# `uv run --project` picks the engine without moving it. todo_system is not
# installed (package = false): a split install puts the engine on PYTHONPATH
# and runs python -P so a stale copy in the vault cannot shadow it. The
# combined layout leaves the env alone.
#
# Installed wheel (public split 13b): ENGINE_DIR is site-packages/todo_system/
# _engine, which ships no pyproject.toml (so `uv run --project` cannot work) and
# is not a package (so PYTHONPATH must not point at it). engine_script hands the
# caller's interpreter down as MACP_PYTHON; its site-packages already holds
# todo_system, so the module runs under that interpreter with -P (a stale copy
# in the vault cwd cannot shadow it). Taken only when BOTH hold — no pyproject
# AND MACP_PYTHON set — so a checkout (pyproject present) and a bare path run
# (MACP_PYTHON unset) keep the uv command line below byte for byte. A bare path
# run of the wheel with no MACP_VAULT_ROOT would resolve the vault to
# site-packages; the units that pass the env are leaf 13c's.
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

cd "$VAULT_DIR" || exit 1

if [[ "$INSTALLED" -eq 1 ]]; then
  M=("$MACP_PYTHON" -P -m todo_system.orchestrator.orchestrator
     --uri "file://$VAULT_DIR/Worker/agents")
else
  M=(uv run --project "$ENGINE_DIR" "${PY[@]}" -m todo_system.orchestrator.orchestrator
     --uri "file://$VAULT_DIR/Worker/agents")
fi
# Both legs always run; the dashboard is how a failed tick gets seen. $? right
# after each leg is the orchestrator's status; an auto failure outranks the
# dashboard's. Output goes to stdout only — the service manager writes the log
# (systemd: logs/macp-orchestrator.out.log, launchd: logs/orchestrator.out.log).
"${M[@]}" --lease "$VAULT_DIR/Worker/orchestrator.lock" auto --db "$VAULT_DIR/Todo/todo.db" 2>&1
rc_auto=$?
"${M[@]}" dashboard 2>&1
rc_dash=$?
exit $(( rc_auto != 0 ? rc_auto : rc_dash ))
