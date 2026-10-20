#!/usr/bin/env bash
# Planner tick — feed the v2 autonomous loop.
#
# When the dispatchable auto-queue is below the policy depth limit, invoke the
# planner agent (.claude/commands/worker-planner.md) to CREATE a few SAFE auto
# tasks for active projects. The orchestrator `auto` tick (every 5 min) then
# dispatches whatever the planner wrote; the runners execute + auto-merge.
#
# Safety comes from the planner prompt + Worker/planner_policy.yaml, which block
# personal/decision/external/paid/deferred work and cap max_new_tasks (3) and
# queue depth (5). Cancelled projects are excluded because the planner only
# extends approved/open work and load_dispatchable filters status='open'.
#
# Cost control (two gates, both must pass to invoke):
#   1) queue depth < DEPTH_LIMIT  — don't pile on when work is already queued
#   2) backoff — skip re-invoke while the open-task state is unchanged AND the
#      last invocation was < EXHAUST_HOURS ago (an exhausted backlog shouldn't
#      burn a planner run every tick; legacy PLANNER_EXHAUSTED_THRESHOLD_HOURS).
#
# Aggressive mode (todo_system/orchestrator/planner_mode.py): when usage has
# fresh slack in BOTH windows, the queue is empty and nothing is running, the
# tick shortens the backoff (24h -> 2h), injects a "탐색 모드" block raising the
# per-call cap (3 -> 6, clamped under DEPTH_LIMIT) and shortens the discovery
# interval (14d -> 3d). Knobs: MAESTRO_PLANNER_AGGRESSIVE_* ;
# MAESTRO_PLANNER_AGGRESSIVE=0 turns it off. Normal mode runs the exact same
# commands and prompt as before.
#
# Two anchors, same as orchestrator_tick.sh. ENGINE_DIR is this script's own
# checkout (the todo_system package). VAULT_DIR is where the data lives
# (Todo/, Worker/, the git refs): MAESTRO_VAULT_DIR wins, then MACP_VAULT_ROOT,
# then the engine checkout itself (combined layout: unchanged behaviour).
#
# Installed wheel (public split 13b residue): ENGINE_DIR is site-packages/
# todo_system/_engine, with no pyproject.toml for `uv run`. engine_script hands
# the caller's interpreter down as MACP_PYTHON, so every engine module runs as
# "$MACP_PYTHON" -P -m todo_system... and the `command -v uv` guards are
# skipped. Taken only when BOTH hold — no pyproject AND MACP_PYTHON set — so a
# checkout keeps its command lines byte for byte. The stdlib-only python3 calls
# (queue depth, backoff state, session id) stay as they are.
set -uo pipefail

ENGINE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VAULT_DIR="${MAESTRO_VAULT_DIR:-${MACP_VAULT_ROOT:-$ENGINE_DIR}}"

INSTALLED=0
if [[ ! -f "$ENGINE_DIR/pyproject.toml" && -n "${MACP_PYTHON:-}" ]]; then
  INSTALLED=1
fi

# ENGINE_PY runs an engine module. A scalar, word-split like $PY below (which
# tests pin as a string), so paths with spaces are not supported. A split
# checkout (engine != vault) puts the engine on PYTHONPATH and runs python -P,
# as the 13b scripts do; the vault has no scripts/, so the alert names the
# engine's kakao_send.sh.
ENGINE_PY="uv run python"
KAKAO_SEND="scripts/kakao_send.sh"
if [[ "$VAULT_DIR" != "$ENGINE_DIR" ]]; then
  export MACP_VAULT_ROOT="$VAULT_DIR"
  KAKAO_SEND="$ENGINE_DIR/scripts/kakao_send.sh"
  if [[ "$INSTALLED" -eq 0 ]]; then
    export PYTHONPATH="$ENGINE_DIR${PYTHONPATH:+:$PYTHONPATH}"
    ENGINE_PY="uv run --project $ENGINE_DIR python -P"
  fi
fi
if [[ "$INSTALLED" -eq 1 ]]; then
  ENGINE_PY="$MACP_PYTHON -P"
fi

cd "$VAULT_DIR" || exit 1

DB="Todo/todo.db"
STATE="Worker/_state/planner_tick_state.json"
DEPTH_LIMIT=5
EXHAUST_HOURS=24
mkdir -p "$(dirname "$STATE")"

command -v claude >/dev/null 2>&1 || { echo "[planner-tick] claude not on PATH"; exit 0; }
command -v sqlite3 >/dev/null 2>&1 || { echo "[planner-tick] sqlite3 not on PATH"; exit 0; }

# Gate -2: HQ host guard — only the marker-elected host plans. The planner
# writes Todo/items/*.md that spread through git, so two hosts planning means
# duplicate tasks. Exit 3 = not the HQ; anything else (no marker, uv missing,
# module error) fails open — see todo_system/orchestrator/hq_host.py.
if [[ "$INSTALLED" -eq 1 ]] || command -v uv >/dev/null 2>&1; then
  $ENGINE_PY -m todo_system.orchestrator.hq_host --check
  if [ $? -eq 3 ]; then
    echo "[planner-tick] not the HQ host (Worker/hq_host); skip planning"
    exit 0
  fi
fi

# Gate -1: manual pause. Worker/_state/PAUSED quiesces the control plane (drain to
# zero before an upgrade). Remove the file to resume.
if [ -f "Worker/_state/PAUSED" ]; then
  echo "[planner-tick] control plane PAUSED (Worker/_state/PAUSED); skip planning"
  exit 0
fi

# Task-level dependency chaining (issue #21): flip `blocked` tasks whose every
# `depends_on` task is done → their on_unblock mode, then sync to todo.db. Runs
# before the budget/queue gates (which may exit early) so a completed upstream
# promptly activates the next pure-code task. It also runs before the usage-guard gate:
# it is a cheap markdown rewrite with no LLM call, and a pacing hold must not freeze
# dependency chains — a downstream task may carry `bypass_guard: true` and be meant to
# run during the hold (2026-10-20: a bypass task sat `blocked` for over an hour after
# its upstream finished because this step sat behind Gate -0.5).
if [[ "$INSTALLED" -eq 1 ]] || command -v uv >/dev/null 2>&1; then PY="$ENGINE_PY"; else PY="python3"; fi
UNBLOCKED=$($PY -m todo_system.task_deps 2>/dev/null | grep -c '^unblocked:')
if [ "${UNBLOCKED:-0}" -gt 0 ]; then
  echo "[planner-tick] auto-unblocked ${UNBLOCKED} task(s) whose deps completed; syncing db"
  $PY -m todo_system.cli import-md >/dev/null 2>&1 || true
fi

# Gate -0.5: Claude usage guard — don't spend a planner run while the
# subscription quota is pacing-blocked (todo_system/usage_guard.py).
# Exit 3 = blocked; any other outcome (uv missing, guard error) fails open.
if [[ "$INSTALLED" -eq 1 ]] || command -v uv >/dev/null 2>&1; then
  $ENGINE_PY -m todo_system.usage_guard check
  if [ $? -eq 3 ]; then
    echo "[planner-tick] usage guard blocked; skip planning"
    exit 0
  fi
fi

# Gate 0: daily budget (cost governance). Stop CREATING new work once today's task
# runs hit the cap — the orchestrator enforces the same cap on dispatch.
BUDGET="${MAESTRO_DAILY_RUN_BUDGET:-40}"
RUNS_TODAY=$(ls -d "Worker/runs/$(date +%Y%m%d)"* 2>/dev/null | wc -l | tr -d ' ')
if [ "${BUDGET:-0}" -gt 0 ] && [ "${RUNS_TODAY:-0}" -ge "$BUDGET" ]; then
  echo "[planner-tick] daily budget reached (${RUNS_TODAY}/${BUDGET} runs today); skip planning"
  exit 0
fi

# Gate 1: ACTIONABLE queue depth. Count open auto/delegate tasks MINUS those
# whose ledger assignment is FAILED — a stalled (failed+cooldown) task is not
# actionable work, and must NOT block the planner from running: otherwise a task
# that failed on a red CI would keep the planner from creating the CI-fix that
# would unblock it (a compounding stall). Self-healing depends on this.
N=$(python3 - <<'PY' 2>/dev/null || echo 0
import sqlite3, json, glob
con = sqlite3.connect("Todo/todo.db")
open_auto = {r[0] for r in con.execute(
    "SELECT slug FROM tasks WHERE status='open' AND is_parent=0 "
    "AND worker_mode IN ('auto','delegate','discover') "
    # A task deferred by not_before cannot run yet, so it is not queue depth:
    # two tasks held to a future date blocked aggressive mode (queue 2 > 0)
    # and kept the planner from refilling a dry queue (2026-09-27).
    # Date-only = 00:00 that day, a time host-local (mirrors
    # todo_system/not_before.py): `YYYY-MM-DD[ |T]HH:MM[:SS]` vs local now.
    "AND NOT (COALESCE(not_before,'') GLOB '[0-9][0-9][0-9][0-9]-*' "
    "AND replace(trim(not_before),'T',' ') > "
    "strftime('%Y-%m-%d %H:%M:%S','now','localtime'))")}
failed = set()
for f in glob.glob("Worker/assignments/*.json"):
    try:
        d = json.load(open(f))
        if d.get("state") == "failed":
            failed.add(d.get("task_id"))
    except Exception:
        pass
print(len(open_auto - failed))
PY
)
if [ "${N:-0}" -ge "$DEPTH_LIMIT" ]; then
  echo "[planner-tick] actionable auto-queue ${N} >= ${DEPTH_LIMIT}; skip"
  exit 0
fi

# Planner mode (todo_system/orchestrator/planner_mode.py). Decided only here,
# after every gate above (HQ, PAUSED, usage guard block, daily budget, queue
# depth) has passed, so aggressive mode can never bypass one of them. Fail
# closed: MODE stays normal unless the CLI printed an aggressive verdict with
# well-formed knobs — a CLI error, missing uv or a parse failure is a normal tick.
MODE=normal
MODE_REASON="mode check unavailable"
MODE_OUT=$($PY -m todo_system.orchestrator.planner_mode decide --queue-depth "${N:-0}" --depth-limit "$DEPTH_LIMIT" 2>/dev/null)
_mode_kv() { printf '%s\n' "$MODE_OUT" | sed -n "s/^$1=//p" | head -1; }
_reason="$(_mode_kv REASON)"
[ -n "$_reason" ] && MODE_REASON="$_reason"
if [ "$(_mode_kv MODE)" = "aggressive" ]; then
  AGG_BACKOFF="$(_mode_kv BACKOFF_HOURS)"
  AGG_MAX_NEW="$(_mode_kv MAX_NEW_TASKS)"
  AGG_DISC_DAYS="$(_mode_kv DISCOVERY_INTERVAL_DAYS)"
  MODE=aggressive
  case "$AGG_BACKOFF" in ''|0|*[!0-9]*) MODE=normal ;; esac
  case "$AGG_MAX_NEW" in ''|0|*[!0-9]*) MODE=normal ;; esac
  case "$AGG_DISC_DAYS" in ''|*[!0-9.]*) MODE=normal ;; esac
  [ "$MODE" = normal ] && MODE_REASON="malformed aggressive knobs; ${MODE_REASON}"
fi
echo "[planner-tick] mode=${MODE} (${MODE_REASON})"
if [ "$MODE" = "aggressive" ]; then
  EXHAUST_HOURS="$AGG_BACKOFF"
fi

# Gate 2: backoff on unchanged state.
HASH=$(sqlite3 "$DB" "SELECT slug,status FROM tasks WHERE status='open' ORDER BY slug;" 2>/dev/null | shasum | awk '{print $1}')
NOW=$(date +%s)
if [ -f "$STATE" ]; then
  LAST_HASH=$(python3 -c "import json,sys;print(json.load(open('$STATE')).get('hash',''))" 2>/dev/null || echo "")
  LAST_TS=$(python3 -c "import json,sys;print(int(json.load(open('$STATE')).get('ts',0)))" 2>/dev/null || echo 0)
  if [ "$HASH" = "$LAST_HASH" ] && [ $((NOW - ${LAST_TS:-0})) -lt $((EXHAUST_HOURS * 3600)) ]; then
    echo "[planner-tick] open-task state unchanged and last run < ${EXHAUST_HOURS}h ago; skip (exhausted)"
    exit 0
  fi
fi

echo "[planner-tick] auto-queue=${N} (< ${DEPTH_LIMIT}); invoking planner agent…"

# Walk model_select's heavy ladder (Fable 5.1 first); claude-opus-4-8 if it prints nothing.
if [[ "$INSTALLED" -eq 1 ]] || command -v uv >/dev/null 2>&1; then
  MODEL=$($ENGINE_PY -m todo_system.orchestrator.model_select 2>/dev/null)
else
  MODEL=$(python3 -m todo_system.orchestrator.model_select 2>/dev/null)
fi
[ -n "$MODEL" ] || MODEL="claude-opus-4-8"
echo "[planner-tick] model: $MODEL"

# Invoke the planner as a SLASH COMMAND. Feeding the raw command markdown as a
# `-p` prompt just makes claude echo the document; `/worker-planner` loads the
# command with its execution semantics. --allowed-tools are space-separated
# (each its own arg), matching stages.build_stage_argv. The planner reads
# todo.db + policy + memory and WRITES Todo/items/*.md; the todo-sync daemon
# syncs those into the DB and the orchestrator dispatches them.
# max-turns 30, raised from 15 on 2026-08-18. model_select's Fable 5 probe began
# failing and the first fallback (claude-opus-5) exceeded a 15-turn budget on
# EVERY run — 5 consecutive `Error: Reached max turns (15)`, each burning a full
# high-effort run and producing NO verdict and NO tasks. claude-opus-4-8 had
# completed 5/5 within the same budget, so this is a per-model turn-cost
# difference, not a workload change. Failing at the cap is strictly worse than
# a larger cap: the turns are spent either way, only the output is lost.
#
# NOTE the backoff below throttles on the open-task state HASH, so a day of
# active task churn (as on 08-18) defeats it and every tick re-invokes. If these
# failures recur, that gate is the thing to fix — not this number.
# Durable report follow-ups: pull any UNRESOLVED follow-ups (recorded to
# Worker/_state/followups.jsonl by the runner's report seam) so a follow-up that
# missed the run-completed scan window still reaches the planner as extra context.
# Empty when there are none, so PROMPT stays exactly "/worker-planner" —
# byte-identical to before on the common path (no planner behaviour change unless
# something is actually pending). A separate `if` rather than
# "/worker-planner${CTX:+$'\n\n'$CTX}" because ANSI-C $'...' quoting is NOT
# re-evaluated inside a ${:+} expansion — that form would inject the literal
# characters $'\n\n' instead of two newlines.
CTX=$($PY -m todo_system.orchestrator.followups emit-context 2>/dev/null)
PROMPT="/worker-planner"
if [ -n "$CTX" ]; then
  echo "[planner-tick] injecting $(printf '%s' "$CTX" | grep -c '^- \[') unresolved follow-up(s) into planner context"
  PROMPT="$PROMPT"$'\n\n'"$CTX"
fi
# Discovery reports (todo_system.orchestrator.discovery): candidate-task lists a
# `discover` worker run wrote after actually running + sweeping a project. Same
# contract as the follow-ups above — empty when there are none, so the common
# path is untouched; reconciled after the run so a handled report stops
# re-appearing and an ignored one expires.
DCTX=$($PY -m todo_system.orchestrator.discovery emit-context 2>/dev/null)
if [ -n "$DCTX" ]; then
  echo "[planner-tick] injecting $(printf '%s' "$DCTX" | grep -c '^- \[') unconsumed discovery report(s) into planner context"
  PROMPT="$PROMPT"$'\n\n'"$DCTX"
fi
# Tasks already created from each recent discovery report (open AND done,
# resolved or not): the planner's dedup otherwise only sees open tasks, so a
# candidate finished last week was re-created under a new slug (10-18). Always
# injected, not just in 탐색 모드; empty when no recent report produced tasks.
CCTX=$($PY -m todo_system.orchestrator.discovery emit-consumed 2>/dev/null)
if [ -n "$CCTX" ]; then
  echo "[planner-tick] injecting $(printf '%s' "$CCTX" | grep -c '^- \[') discovery report(s) with already-created tasks into planner context"
  PROMPT="$PROMPT"$'\n\n'"$CCTX"
fi
# 탐색 모드 (aggressive only): tell the planner to scan past its own trigger
# skip, raise its per-call cap to AGG_MAX_NEW (already clamped to the room under
# DEPTH_LIMIT), and keep every blocked pattern. Normal mode never reaches this.
if [ "$MODE" = "aggressive" ]; then
  ACTX=$($PY -m todo_system.orchestrator.planner_mode emit-context --max-new-tasks "$AGG_MAX_NEW" --depth-limit "$DEPTH_LIMIT" 2>/dev/null)
  if [ -n "$ACTX" ]; then
    echo "[planner-tick] injecting 탐색 모드 context (max_new_tasks=${AGG_MAX_NEW})"
    PROMPT="$PROMPT"$'\n\n'"$ACTX"
  fi
fi

# --- checkout lease: serialize THIS claude call against the orchestrator's vault
# branch prep. The planner commits what it writes (CLAUDE.md §10.1); with no lock,
# prepare_vault_branch's `checkout -B worker/<slug>` can move HEAD mid-call and
# that self-commit lands on the worker branch, to be orphaned by the next
# sibling's prep (observed ed395e1b). Dedicated lease (NOT Worker/orchestrator.lock
# — grabbing that would starve collect/report/dashboard for the whole up-to-10-min
# call, a merge-blocking regression). Reuses lease.py's Lease, never flock. TTL
# 1200s > the 600s planner timeout; a crashed holder is cleared by TTL + the
# same-host dead-PID reclaim in lease.py.
LEASE_PATH="Worker/checkout.lock"
LEASE_HOLDER="$(hostname):$$"
LEASE_HELD=0
_release_checkout_lease() {
  [ "$LEASE_HELD" = 1 ] && $PY -m todo_system.orchestrator.lease release \
    --path "$LEASE_PATH" --holder "$LEASE_HOLDER" >/dev/null 2>&1
  LEASE_HELD=0
  return 0
}
trap _release_checkout_lease EXIT
_lease_ok=0
for _try in 1 2 3; do
  $PY -m todo_system.orchestrator.lease acquire --path "$LEASE_PATH" \
    --ttl 1200 --holder "$LEASE_HOLDER" >/dev/null 2>&1
  _rc=$?
  if [ "$_rc" -eq 0 ]; then LEASE_HELD=1; _lease_ok=1; break; fi
  if [ "$_rc" -ne 1 ]; then _lease_ok=1; break; fi   # CLI error → fail-open, proceed unguarded
  sleep 2
done
if [ "$_lease_ok" -eq 0 ]; then
  echo "[planner-tick] checkout lease held (orchestrator vault branch prep) — skip this tick"
  exit 0
fi

# Snapshot worker/* tips (sorted) so the post-verify below can tell whether the
# planner committed onto a worker branch despite the lease + branch pin, and alert
# if so — a backstop, never an auto-repair ([[macp-orphaned-commit-escalation]]).
_worker_tips() { git for-each-ref --format='%(objectname) %(refname:short)' 'refs/heads/worker/*' 2>/dev/null | sort; }
WORKER_TIPS_BEFORE="$(_worker_tips)"

# Token usage instrumentation (Design B, best-effort — same scheme the stage loop
# uses, todo_system/orchestrator/stages.py "Stage token usage"). Attach a fresh
# --session-id per call ONLY when the kill switch is not "0"; after the CLI exits,
# read that session's usage back via usage_probe (which reuses stages.stage_usage,
# no new parser) into an untracked JSONL (Worker/_state/, git-ignored). Kill switch
# off ⇒ the else branch is the pre-instrumentation call byte-for-byte and no probe
# runs. Recording is best-effort (|| true): a token count never fails the tick.
_t0=$(date +%s)
SID=""
[ "${MACP_STAGE_USAGE:-}" != "0" ] && SID=$(python3 -c 'import uuid;print(uuid.uuid4())' 2>/dev/null || true)
if [ -n "$SID" ]; then
  claude -p "$PROMPT" \
    --model "$MODEL" --effort high --max-turns 30 \
    --session-id "$SID" \
    --allowed-tools Read Write Edit Glob Grep Bash 2>&1 | tail -40
  RC=${PIPESTATUS[0]}
  $PY -m todo_system.orchestrator.usage_probe record \
    --source planner_tick --dest Worker/_state/planner_usage.jsonl \
    --session-id "$SID" --rc "$RC" \
    --duration-ms $(( ($(date +%s) - _t0) * 1000 )) --model "$MODEL" \
    >/dev/null 2>&1 || true
else
  claude -p "$PROMPT" \
    --model "$MODEL" --effort high --max-turns 30 \
    --allowed-tools Read Write Edit Glob Grep Bash 2>&1 | tail -40
fi

# claude is done — the window where the planner could self-commit is closed, so
# release the checkout lease NOW (before the post-run bookkeeping below) to unblock
# the orchestrator's vault branch prep. The EXIT trap is the backstop.
_release_checkout_lease

# Post-verify: worker/* tips must be UNCHANGED by the run. Lease + branch pin
# should guarantee it; if a worker branch gained or moved a commit anyway, alert
# via the existing kakao plumbing and let a human decide (no auto cherry-pick).
# The runner legitimately creates, pushes and deletes worker/* branches while the
# planner runs (7 of 7 alerts up to 2026-10-06 were that: pure deletions after
# merge, or the runner's own stage commits), so only a commit whose subject is the
# planner's own (`chore: planner …` / `chore(planner): …`) on a new or moved tip pages.
WORKER_TIPS_AFTER="$(_worker_tips)"
if [ "$WORKER_TIPS_BEFORE" != "$WORKER_TIPS_AFTER" ]; then
  PLANNER_HITS=""
  while read -r _new _ref; do
    [ -n "$_ref" ] || continue
    _old="$(printf '%s\n' "$WORKER_TIPS_BEFORE" | awk -v r="$_ref" '$2 == r {print $1}')"
    if [ -n "$_old" ]; then _range="$_old..$_new"; else _range="$_new --not refs/heads/main"; fi
    # shellcheck disable=SC2086  # _range is deliberately word-split into rev args
    if git log --format=%s $_range 2>/dev/null | grep -qE '^chore(\(planner\))?: planner|^chore\(planner\)'; then
      PLANNER_HITS="$PLANNER_HITS$_new $_ref "
    fi
  done < <(comm -13 <(printf '%s\n' "$WORKER_TIPS_BEFORE") <(printf '%s\n' "$WORKER_TIPS_AFTER"))
  if [ -n "$PLANNER_HITS" ]; then
    echo "[planner-tick] WARN planner commit landed on worker/*: $PLANNER_HITS"
    bash "$KAKAO_SEND" "[Planner] 플래너 커밋이 worker/* 브랜치에 들어감 — 사람이 옮겨야 함: $PLANNER_HITS" >/dev/null 2>&1 || true
  else
    echo "[planner-tick] worker/* moved during planner run (runner activity, no planner commit) — not alerting"
  fi
fi

# The todo-sync daemon can persist a freshly-written task WITHOUT its worker_mode
# (observed race: status synced, worker_mode empty), leaving the new auto task
# un-dispatchable. Force a full markdown->DB import so planner-created tasks are
# immediately picked up by the orchestrator auto tick.
if [[ "$INSTALLED" -eq 1 ]] || command -v uv >/dev/null 2>&1; then
  $ENGINE_PY -m todo_system.cli import-md >/dev/null 2>&1 || true
else
  python3 -m todo_system.cli import-md >/dev/null 2>&1 || true
fi

# Consume follow-ups the planner just acted on: a task it wrote stamped
# `from_followup: <id>` in frontmatter, or an entry it appended to
# Worker/_state/followups_rejected.jsonl, marks that follow-up done so it stops
# re-appearing in the injected context above. Runs AFTER import-md so the new
# task .md files are fully on disk. Best-effort; never fails the tick. A
# follow-up the planner did NOT act on stays open by design — durable, not lost.
$PY -m todo_system.orchestrator.followups reconcile 2>/dev/null || true
$PY -m todo_system.orchestrator.discovery reconcile 2>/dev/null || true

# Discovery cadence: when the planner just answered `exhausted` (it read
# Worker/planner_state.json), hand the idle capacity to a `discover` run — one
# project at a time, one per repo per MAESTRO_DISCOVERY_INTERVAL_DAYS (14; in
# aggressive mode the shorter AGG_DISC_DAYS, 3 by default, for this call only). The
# worker runs + sweeps the project and its report feeds the NEXT planner run via
# the injection above. MAESTRO_DISCOVERY=0 disables. Every gate above (pause,
# usage guard, daily budget, queue depth) has already passed by this line.
if [ "$MODE" = "aggressive" ]; then
  DISC=$(MAESTRO_DISCOVERY_INTERVAL_DAYS="$AGG_DISC_DAYS" $PY -m todo_system.orchestrator.discovery maybe-create 2>/dev/null)
else
  DISC=$($PY -m todo_system.orchestrator.discovery maybe-create 2>/dev/null)
fi
case "$DISC" in
  created*)
    echo "[planner-tick] discovery: $DISC"
    $PY -m todo_system.cli import-md >/dev/null 2>&1 || true
    ;;
  *) echo "[planner-tick] discovery: ${DISC:-skip}" ;;
esac

# Record state even on a no-op verdict so the backoff gate can throttle.
python3 -c "import json;json.dump({'hash':'$HASH','ts':$NOW}, open('$STATE','w'))" 2>/dev/null || true
echo "[planner-tick] done"
