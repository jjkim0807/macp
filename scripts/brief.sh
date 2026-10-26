#!/bin/zsh
set -euo pipefail

# Two anchors, same as notify_kakao.sh. ENGINE_DIR is this script's own
# checkout: .claude/commands/brief.md, scripts/export_db_snapshot.py,
# scripts/notify_kakao.sh and the todo_system package live there. VAULT_DIR is
# where the data lives (logs/, Todo/, Worker/_state/, .secrets/):
# MAESTRO_VAULT_DIR wins, then MACP_VAULT_ROOT (split install, engine checkout
# != vault), then the engine checkout itself (combined layout: unchanged
# behaviour).
ENGINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VAULT_DIR="${MAESTRO_VAULT_DIR:-${MACP_VAULT_ROOT:-$ENGINE_DIR}}"

# The children find the vault through MACP_VAULT_ROOT or their cwd, and
# `uv run --directory "$ENGINE_DIR"` moves the cwd to the engine. So a split
# install hands the vault down explicitly; the combined layout leaves env alone.
if [[ "$VAULT_DIR" != "$ENGINE_DIR" ]]; then
  export MACP_VAULT_ROOT="$VAULT_DIR"
fi

# Installed wheel (public split 13b): ENGINE_DIR is site-packages/todo_system/
# _engine, which ships no pyproject.toml, so `uv run --directory "$ENGINE_DIR"`
# cannot work there. engine_script hands the caller's interpreter down as
# MACP_PYTHON; its site-packages already holds todo_system, so the three engine
# calls below run under it with -P, the cwd staying the vault (MACP_VAULT_ROOT
# is exported above in a split install). Only when BOTH hold — no pyproject AND
# MACP_PYTHON set; a checkout keeps every uv line below byte for byte.
INSTALLED=0
if [[ ! -f "$ENGINE_DIR/pyproject.toml" && -n "${MACP_PYTHON:-}" ]]; then
  INSTALLED=1
fi

PROMPT_FILE="$ENGINE_DIR/.claude/commands/brief.md"
LOG_DIR="$VAULT_DIR/logs"
BRIEF_DIR="$VAULT_DIR/Todo/Briefings"
TODAY="$(date +%F)"
NOW_TS="$(date +%FT%T)"

mkdir -p "$LOG_DIR" "$BRIEF_DIR"

# Ensure Claude Code CLI is on PATH (Homebrew location for this machine).
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

cd "$VAULT_DIR"

if ! command -v claude >/dev/null 2>&1; then
  echo "[$NOW_TS] brief.sh: claude CLI not found on PATH" >&2
  exit 1
fi

# Worker queue self-heal (doctor) — cleans up stuck in_progress entries, failed entries
# of cancelled tasks, and auto-retryable failed entries. Guarantees brief reads a clean
# queue. Result messages go to stdout one per line (`doctor: ...`).
if [[ "$INSTALLED" -eq 1 ]]; then
  "$MACP_PYTHON" -P -m todo_system.worker scan-queue 2>&1 \
    | tee -a "$LOG_DIR/brief.out.log" \
    | grep '^doctor:' || true
elif command -v uv >/dev/null 2>&1; then
  uv run --directory "$ENGINE_DIR" python -m todo_system.worker scan-queue 2>&1 \
    | tee -a "$LOG_DIR/brief.out.log" \
    | grep '^doctor:' || true
fi

# (The email_to_todo pipeline was retired on 2026-08-05 — Decisions/2026-08-05-email-to-todo-retire.md)

# Task snapshot export — the local SQLite (Todo/todo.db) is the SoT, and
# Todo/Snapshots/latest.json is rebuilt from it. The brief agent reads this snapshot, so
# the export has to run here for the briefing to see today's data.
# (A comment from the days of exporting from hosted Supabase was left over, but that is
# a retired path.)
# Even if it fails, brief generation goes on (|| true).
if [[ "$INSTALLED" -eq 1 ]]; then
  echo "[$NOW_TS] brief.sh: running export_db_snapshot.py"
  "$MACP_PYTHON" -P "$ENGINE_DIR/scripts/export_db_snapshot.py" 2>&1 \
    | tee -a "$LOG_DIR/brief.out.log" \
    | grep '^export_db_snapshot:' || true
elif command -v uv >/dev/null 2>&1; then
  echo "[$NOW_TS] brief.sh: running export_db_snapshot.py"
  uv run --directory "$ENGINE_DIR" python "$ENGINE_DIR/scripts/export_db_snapshot.py" 2>&1 \
    | tee -a "$LOG_DIR/brief.out.log" \
    | grep '^export_db_snapshot:' || true
fi

# mailroom mail summary — the separate 21:30 KakaoTalk digest was retired on 2026-09-11.
# Instead, the last-24h mail section (Todo/Snapshots/mailroom.md) is built here and the
# brief agent reads it and merges it into the morning briefing. Even if the mailroom
# checkout is missing or it fails, brief goes on (|| true).
MAILROOM_DIR="${MAILROOM_DIR:-$HOME/Projects/mailroom}"
MAILROOM_SNAPSHOT="$VAULT_DIR/Todo/Snapshots/mailroom.md"
if [[ -d "$MAILROOM_DIR" ]] && command -v uv >/dev/null 2>&1; then
  echo "[$NOW_TS] brief.sh: running mailroom digest --out $MAILROOM_SNAPSHOT"
  uv run --directory "$MAILROOM_DIR" mailroom digest --out "$MAILROOM_SNAPSHOT" 2>&1 \
    | tee -a "$LOG_DIR/brief.out.log" \
    | grep '^mailroom digest:' || echo "[$NOW_TS] brief.sh: WARNING mailroom digest failed (non-fatal)"
else
  echo "[$NOW_TS] brief.sh: mailroom checkout not found at $MAILROOM_DIR — skipping mail section"
fi

if [[ ! -f "$PROMPT_FILE" ]]; then
  echo "[$NOW_TS] brief.sh: prompt file missing: $PROMPT_FILE" >&2
  exit 1
fi

# Strip the YAML frontmatter from the slash command file so the body becomes the headless prompt.
# Frontmatter is the block between the first '---' line and the second '---' line.
PROMPT_BODY="$(awk '
  BEGIN { in_fm = 0; seen = 0 }
  /^---$/ {
    if (seen == 0) { in_fm = 1; seen = 1; next }
    else if (in_fm == 1) { in_fm = 0; next }
  }
  { if (in_fm == 0 && seen == 1) print }
' "$PROMPT_FILE")"

# Append run metadata so the agent knows today and the output path
FULL_PROMPT="$PROMPT_BODY

---

# Run metadata

- Today's date (use this exactly for the filename and frontmatter): $TODAY
- Output path: Todo/Briefings/${TODAY}.md
- Working directory: $VAULT_DIR
"

echo "[$NOW_TS] brief.sh: invoking claude -p (max-turns=30)"

# Call Claude Code in headless mode. Allow only the tools needed.
# - Read: required to read vault context
# - Write/Edit: required to write the brief file
# - Bash(git log:*): for "yesterday → today" diffing
# Token usage instrumentation (Design B, best-effort — same scheme the stage loop
# uses). Fresh --session-id per call ONLY when the kill switch is not "0"; after
# the CLI exits, usage_probe reads that session's usage back (reusing
# stages.stage_usage, no new parser) into an untracked JSONL (Worker/_state/).
# The else branch is the pre-instrumentation call byte-for-byte. This script runs
# under `set -euo pipefail`, whose pre-existing semantic is: a claude crash aborts
# with claude's rc BEFORE the artifact check below. `if PIPELINE` stops set -e from
# firing on the pipe so we can read claude's rc from $pipestatus[1] (a trailing
# `|| true` would reset pipestatus to the rc of `true`), record it best-effort,
# then reproduce that abort with `exit "$rc"` — so exit codes never change.
_t0=$(date +%s)
SID=""
[[ "${MACP_STAGE_USAGE:-}" != "0" ]] && SID=$(python3 -c 'import uuid;print(uuid.uuid4())' 2>/dev/null || true)
if [[ -n "$SID" ]]; then
  if claude -p "$FULL_PROMPT" \
       --max-turns 30 \
       --session-id "$SID" \
       --allowed-tools "Read" "Write" "Edit" "Bash(git log:*)" "Bash(git diff:*)" "Bash(git status:*)" \
       2>&1 | tee -a "$LOG_DIR/brief.out.log"; then
    rc=0
  else
    rc=$pipestatus[1]
  fi
  if [[ "$INSTALLED" -eq 1 ]]; then
    "$MACP_PYTHON" -P -m todo_system.orchestrator.usage_probe record \
      --source brief --dest "$VAULT_DIR/Worker/_state/brief_usage.jsonl" \
      --session-id "$SID" --rc "$rc" \
      --duration-ms $(( ($(date +%s) - _t0) * 1000 )) \
      >/dev/null 2>&1 || true
  else
    uv run --directory "$ENGINE_DIR" python -m todo_system.orchestrator.usage_probe record \
      --source brief --dest "$VAULT_DIR/Worker/_state/brief_usage.jsonl" \
      --session-id "$SID" --rc "$rc" \
      --duration-ms $(( ($(date +%s) - _t0) * 1000 )) \
      >/dev/null 2>&1 || true
  fi
  [[ "$rc" -ne 0 ]] && exit "$rc"
else
  claude -p "$FULL_PROMPT" \
    --max-turns 30 \
    --allowed-tools "Read" "Write" "Edit" "Bash(git log:*)" "Bash(git diff:*)" "Bash(git status:*)" \
    2>&1 | tee -a "$LOG_DIR/brief.out.log"
fi

if [[ ! -f "$BRIEF_DIR/${TODAY}.md" ]]; then
  echo "[$NOW_TS] brief.sh: WARNING brief file not found after run" >&2
  exit 2
fi

echo "[$NOW_TS] brief.sh: brief generated at Todo/Briefings/${TODAY}.md"

# Push to KakaoTalk if configured. Non-fatal if token missing or push fails.
if [[ -f "$VAULT_DIR/.secrets/kakao-token.json" ]]; then
  if bash "$ENGINE_DIR/scripts/notify_kakao.sh" 2>>"$LOG_DIR/brief.out.log"; then
    echo "[$NOW_TS] brief.sh: kakao push OK"
  else
    echo "[$NOW_TS] brief.sh: kakao push failed (non-fatal, see logs/notify_kakao.out.log)"
  fi
else
  echo "[$NOW_TS] brief.sh: kakao token not configured — skipping push"
fi

exit 0
