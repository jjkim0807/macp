#!/usr/bin/env bash
# Remote v2 runner loop (P4) — runs on a secondary host (WSL/Linux; a Windows
# equivalent mirrors claude-remote-control-windows). Runs the orchestrator
# Runner as a daemon; the git:// mailbox syncs messages with the Mac
# orchestrator through the vault repo.
#
# Identity is passed inline (--agent-id), so NO registry/PyYAML is needed here —
# only the stdlib transport + git (+ claude for live modes). Runs under a bare
# python3.
#
# Usage (on the remote host, in a clone of the vault):
#   AGENT_ID=sc1-homeland PROJECT_REPO="$HOME/sc1-homeland" \
#     scripts/orchestrator_runner_remote.sh --mode shadow
#   # flip --mode live-remote once the host is trusted and you want real runs.

set -uo pipefail

VAULT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_ID="${AGENT_ID:-}"
PROJECT_REPO="${PROJECT_REPO:-}"
CAPABILITIES="${CAPABILITIES:-}"   # comma-separated; needed so ACCEPT passes the
                                   # dispatch's `requires` check (id-only runs load no registry)
MODE="shadow"
INTERVAL=30

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$AGENT_ID" ]]; then
  echo "error: AGENT_ID must be set (e.g. AGENT_ID=sc1-homeland)" >&2
  exit 2
fi

# claude lives in ~/.local/bin (native installer) or nvm (fallback below); the old
# per-user claude bin dir is dangling after the config-dir migration. $CLAUDE_BIN_DIR
# optionally overrides for a host that keeps the binary elsewhere.
export PATH="${CLAUDE_BIN_DIR:+$CLAUDE_BIN_DIR:}$HOME/.local/bin:/usr/local/bin:$PATH"
if ! command -v claude >/dev/null 2>&1 && [[ -s "$HOME/.nvm/nvm.sh" ]]; then
  # shellcheck disable=SC1091
  source "$HOME/.nvm/nvm.sh" >/dev/null 2>&1 || true
fi

for tool in python3 git; do
  command -v "$tool" >/dev/null 2>&1 || { echo "error: '$tool' not found" >&2; exit 2; }
done

cd "$VAULT_DIR" || exit 1

echo "[$(date +%FT%T)] remote runner: agent=$AGENT_ID mode=$MODE interval=${INTERVAL}s"
exec python3 -m todo_system.orchestrator.runner \
  --agent-id "$AGENT_ID" --kind ai-remote ${PROJECT_REPO:+--repo "$PROJECT_REPO"} \
  ${CAPABILITIES:+--capabilities "$CAPABILITIES"} \
  --uri "git://Worker/agents" --mode "$MODE" --daemon --interval "$INTERVAL"
