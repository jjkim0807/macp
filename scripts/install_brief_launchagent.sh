#!/bin/zsh
set -euo pipefail

# Two anchors, as in
# install_orchestrator_launchagent.sh (11k). ENGINE_DIR is this script's own
# checkout (where brief.sh lives). VAULT_DIR is where the data lives (logs/,
# Todo/Briefings/, and the plist WorkingDirectory): MACP_VAULT_ROOT (split
# install) or the engine checkout itself (combined layout). The installer is
# run from inside its checkout, so the combined default resolves exactly as the
# old hardcoded fallback did.
# The plist also hands MACP_VAULT_ROOT=$VAULT_DIR to brief.sh (11l), so the
# brief resolves the vault at run time instead of falling back to its own
# checkout. In the combined layout the value equals brief.sh's own ENGINE_DIR,
# so its split-only branch stays off (no-op).
ENGINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VAULT_DIR="${MACP_VAULT_ROOT:-$ENGINE_DIR}"
LABEL="${MACP_LABEL_PREFIX:-com.macp.}secondbrain.brief"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$VAULT_DIR/logs"
SCRIPT_PATH="$ENGINE_DIR/scripts/brief.sh"

# Installed mode (public split 13d, as gen_units does in 13c): with
# MACP_ENGINE_PYTHON set, launchd runs `<python> -P -m todo_system.engine_script
# brief.sh` so the script resolves from the installed wheel. Unset or empty, the
# plist is byte-identical to the pre-13d one. -P because WorkingDirectory is the
# vault, which may still carry a checkout's todo_system/.
ENGINE_PYTHON="${MACP_ENGINE_PYTHON:-}"
if [[ -n "$ENGINE_PYTHON" && "$ENGINE_PYTHON" != /* ]]; then
  echo "ERROR MACP_ENGINE_PYTHON must be an absolute path (launchd ProgramArguments): $ENGINE_PYTHON" >&2
  exit 2
fi
if [[ -n "$ENGINE_PYTHON" ]]; then
  PROGRAM_ARGUMENTS="    <string>${ENGINE_PYTHON}</string>
    <string>-P</string>
    <string>-m</string>
    <string>todo_system.engine_script</string>
    <string>brief.sh</string>"
else
  PROGRAM_ARGUMENTS="    <string>/bin/zsh</string>
    <string>${SCRIPT_PATH}</string>"
fi

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$LOG_DIR"
mkdir -p "$VAULT_DIR/Todo/Briefings"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <key>ProgramArguments</key>
  <array>
${PROGRAM_ARGUMENTS}
  </array>

  <key>WorkingDirectory</key>
  <string>${VAULT_DIR}</string>

  <!-- 08:30, not 06:30: the Claude weekly quota resets at 08:00 KST. On the last
       morning of a heavy week a 06:30 run dies at the "claude -p" step
       ("hit your weekly limit · resets 8am") and the briefing silently goes
       missing — the earlier steps still run, so only the document is absent.
       Keep this after 08:00. (2026-08-04/05 both lost their brief this way.) -->
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>8</integer>
    <key>Minute</key>
    <integer>30</integer>
  </dict>

  <key>RunAtLoad</key>
  <false/>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>MACP_VAULT_ROOT</key>
    <string>${VAULT_DIR}</string>
  </dict>

  <key>StandardOutPath</key>
  <string>${LOG_DIR}/brief.out.log</string>

  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/brief.err.log</string>
</dict>
</plist>
PLIST

if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
  launchctl bootout "gui/$(id -u)" "$PLIST_PATH" || true
fi

launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/${LABEL}"

echo "Installed: ${LABEL}"
echo "Plist: ${PLIST_PATH}"
echo "Schedule: every day at 08:30"
echo "Run manually now with: launchctl kickstart gui/\$(id -u)/${LABEL}"
echo "Or directly with: bash ${SCRIPT_PATH}"
