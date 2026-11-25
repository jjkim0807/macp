# Running 24/7

[Getting started](getting-started.md) runs each piece once by hand. Running
continuously means keeping five processes alive:

| Process | Command | Cadence |
|---|---|---|
| **runner** | `orchestrator.runner --agent worker-default --mode live-staged --daemon` | long-lived; executes pipelines |
| **orchestrator tick** | `orchestrator.orchestrator auto` | every ~5 min: dispatch, collect, retry, reap |
| **todo-sync** | `cli daemon --interval 3` | long-lived; `Todo/items/*.md` → `todo.db` |
| **planner tick** (optional) | `scripts/planner_tick.sh` | periodic; refills an empty queue |
| **dashboard** (optional) | `scripts/macp_dashboard.py` | long-lived; http://127.0.0.1:8787 |

Other units cover housekeeping: maintenance (vault sync, engine self-update), the
morning brief, and the health report.

## The quickest way: four terminals

```bash
cd ~/my-vault      # only `cli daemon` still needs the vault as its cwd; the runner
                   # and the orchestrator tick resolve it from MACP_VAULT_ROOT
P="uv run --project $HOME/macp python"
$P -m todo_system.orchestrator.runner --agent worker-default --mode live-staged --daemon
$P -u -m todo_system.cli daemon --interval 3
while sleep 300; do $P -m todo_system.orchestrator.orchestrator auto; done
$P ~/macp/scripts/macp_dashboard.py
```

Run each line in its own tmux pane. This is enough to see the system work end to end
before you install anything.

## Linux: systemd user units

`scripts/systemd/gen_units.py` renders the engine-core units (runner, orchestrator,
planner, maintenance, todo-sync, dashboard, brief, health) for `systemctl --user`:

```bash
export MACP_VAULT_ROOT=~/my-vault
python3 ~/macp/scripts/systemd/gen_units.py --print     # read what it would write
python3 ~/macp/scripts/systemd/gen_units.py --enable    # write, daemon-reload, enable --now
systemctl --user list-units 'macp-*'
loginctl enable-linger "$USER"                           # keep them running after logout
```

Every `ExecStart` points at a script that ships in this repository, and data paths
point into your vault. Always read the `--print` output first. `--only` filters which
units are *enabled*, not which files are written.

Logs go to `<vault>/logs/<unit>.out.log` and `.err.log`. To check whether a daemon is
alive, look at the PID from `systemctl --user status`. The logs are append-only, so an
old error line says nothing about now.

## macOS: launchd agents

`scripts/install_*_launchagent.sh` install the same pieces as LaunchAgents
(`install_runner_launchagent.sh`, `install_orchestrator_launchagent.sh`,
`install_todo_sync_launchagent.sh`, …). Labels use `MACP_LABEL_PREFIX` (or
`label_prefix` in `.macp/config.toml`). Check them with
`launchctl list | grep <prefix>`. There is no installer for the maintenance tick:
schedule `python -m todo_system.worker maintenance` yourself (every ~5 minutes) if
this Mac should sync the vault and update the engine.

## Updating the engine

The maintenance tick fast-forwards the engine checkout (`git pull --ff-only`) and
restarts resident daemons whose code changed. You can also `git pull` yourself; the
daemons pick it up within one maintenance tick.

## Where a run works

A run never switches branches in a directory a person works in. Each one gets its own
git worktree under the **worker root** (`~/Worker` by default; set `MACP_WORKER_ROOT`
or `worker_root` in `.macp/config.toml` to move it), on the branch `worker/<slug>`:

| Task | Where its branch lives | Cut from | Removed |
|---|---|---|---|
| has `repo:` | `<worker root>/<repo>.wt/<slug>`, a worktree of the shared clone `<worker root>/<repo>` | `origin/<default>`; `origin/worker/<slug>` on a retry | after a run that succeeded. A failed or parked run's is kept for the retry and swept later |
| no `repo:` (a vault task: `auto`, `quick`, `delegate`) | `<worker root>/vault.wt/<slug>`, a linked worktree of the vault repository | the vault's **local** default branch, so commits that are not pushed yet are in it; `origin/worker/<slug>` when an earlier attempt pushed it | when the run ends, whatever the outcome |
| no `repo:`, `worker_mode: discover` | nowhere — it has no branch and no worktree | | |

**The live vault stays on its default branch for the whole run.** The stages still run
with the vault as their working directory (it is the trusted workspace), but a vault
task's edits, commits, push, PR and merge all happen in its worktree. A commit that you,
the planner or the sync daemon make in the vault while a run is in flight therefore
lands on the default branch, where you made it. After every stage the runner checks
that the vault's `HEAD` did not move. If a stage moved it, the runner puts it back and
records `live_head_moved` in the run's `stages.json`.

A vault run still writes these **in the live vault**, as before: `Worker/runs/<run_id>/`
(the run's artifacts), the ledger (`Worker/assignments/`), the mailbox, `Worker/_state/`
and `Memory/episodes/`. The worktree does **not** have the vault's gitignored files
(`Todo/todo.db`, `Worker/runs/`, …) or changes that are not committed yet. The stage is
told both, and to copy the current version of a file into the worktree before editing
it when the committed one is older.

What follows from this:

- A vault task needs the vault to be **its own git repository with at least one
  commit**. Otherwise the run fails at the start with
  `could not prepare vault worktree: …` and the reason.
- Before the worktree is removed, whatever a stage left uncommitted in it is committed
  onto `worker/<slug>` (`chore(worker): <slug> WIP rescue …`). A failed run's work is on
  the branch, not gone. The runner never deletes the branch.
- Only a clean worktree is removed. If its leftovers could not be committed (a refused
  commit, a detached `HEAD`), it stays and the run says `vault worktree kept`. The next
  run of that task then fails with `a leftover vault worktree is in the way` until you
  have looked: `git -C <vault> worktree list` shows it, and once you have saved what you
  need, `git -C <vault> worktree remove --force <path>` removes it.
- A run that was killed outright (power loss, `kill -9`) leaves its worktree behind as
  well. The next run of the same task commits what was in it, removes it and starts
  from a fresh one. The worktree sweep (run by todo-sync) does not look at `vault.wt/`.
- Vault runs still run one at a time.

## Disk space

Android and Gradle work fills a root disk quickly. Two guards keep it usable. Both
stay silent while there is enough free space.

| | Run-start disk guard | Maintenance cache prune |
|---|---|---|
| Code | `orchestrator/disk_guard.py` | `maintenance_disk.py` |
| When | once per run, before branch prep | every maintenance tick (~5 min) |
| Trigger → target | < 3 GiB → 5 GiB free | < 8 GiB → 10 GiB free |
| What it frees | `./gradlew clean --no-daemon` in idle `~/Worker` clones | caches that Gradle and uv download again on demand |

### What the cache prune removes

Below the trigger, the prune runs one pass, at most once every 30 minutes:

1. `uv cache prune` (uv's own command, which takes uv's own lock).
2. `<gradle home>/caches/<version>/` directories for a Gradle version that is in
   use by neither a `~/Worker` wrapper (`gradle/wrapper/gradle-wrapper.properties`,
   linked worktrees included) nor a live Gradle daemon, or that is but nothing below
   has touched for 30 days. Active runs no longer block this — only the version set
   actually in use is protected. If the protected set can't be read (an unparseable
   wrapper, a Gradle daemon whose version can't be parsed, or no `/proc`), every
   version directory is kept.
3. Regular hex-named files directly in `<gradle home>/caches/build-cache-1/` that
   haven't changed for 30 days, oldest first. These prune even while runs are active:
   each is deleted as one atomic file.

It stops once free space reaches the target. Every deletion goes through one function
that checks the real path against this allowlist and refuses symlinks. These are never
touched: `caches/modules-2`, every distribution under `<gradle home>/wrapper`, other
`caches/*` entries, the `~/Worker` clones and worktrees, the vault, `~/.config`, and
the browser caches under `~/.cache`.

**Protected versions.** The versions kept are the ones the `~/Worker` wrappers name
unioned with the versions live Gradle daemons are running (parsed from each daemon's
distribution/launcher path in `/proc`). The set is recomputed before every deletion,
so a daemon that starts mid-pass protects its version from that point on; the line then
says `<version>=now protected`. It is fail-closed: an unparseable wrapper, a daemon
whose version can't be read, or no `/proc` keeps every version directory (the line says
`versions=protected set unknown (…)`) — build-cache entries still prune. An active
ledger run by itself no longer protects anything.

When `uv cache prune` times out or exits non-zero, the skip reason names the live
processes holding uv's cache lock (any process whose `argv[0]` is `uv` — `uv run`,
`uv sync`, `uv cache`), e.g. `uv=timed out (90s); lock held by: uv run python trading_loop.py`.

A pass prints exactly one `disk-prune:` line to the maintenance log, for example:

```
disk-prune: free 6.4G → 11.2G of 96.0G (93% → 88% used, trigger <8.0G, target 10.0G) — uv cache prune ok; removed versions 8.7; removed 1234 build-cache entries
```

If free space is still below the trigger after a pass, the line ends with
`— STILL below 8.0G` and one alert goes out through the notifier, at most once every
24 hours. The throttle state lives outside git, in
`~/.local/share/macp/disk/prune_state.json`.

Environment (read at call time):

| Variable | Default | Meaning |
|---|---|---|
| `MACP_DISK_PRUNE_STATE_DIR` | `~/.local/share/macp/disk` | throttle state |
| `MACP_UV_BIN` | `$UV`, then `PATH`, then `~/.local/bin/uv` | uv binary |
| `GRADLE_USER_HOME` | `~/.gradle` | Gradle home |

To see what a pass would do, without deleting anything or running uv:

```bash
uv run --project $HOME/macp python -m todo_system.maintenance_disk --dry-run
uv run --project $HOME/macp python -m todo_system.orchestrator.disk_guard --dry-run
```

## The dashboard

`scripts/macp_dashboard.py` serves the queue, running stages, parked tasks and recent
runs on `127.0.0.1:8787`. It refuses to bind anywhere but loopback unless
`MACP_DASHBOARD_TOKEN` is set. When a tunnel fronts it, set `MACP_PUBLIC_EXPOSURE=1`,
and the token is then required on loopback too. The dashboard shows vault contents,
so treat it as private.

`orchestrator dashboard` writes the same overview to `Todo/Orchestrator.md`, if you
prefer reading it in your editor.

## Several machines

The vault is a git repository, so several hosts can share it. One host **dispatches**:
it runs the orchestrator tick, declared in a git-tracked line
(`orchestrator/hq_host.py`). Other hosts run runners for the tasks they can do, for
example a Windows machine for a Windows-only project. Each host needs its own engine
checkout and `MACP_VAULT_ROOT`.

State travels between hosts through the **git model**: messages and the ledger move
through the vault repo (the default `git://Worker/agents` mailbox), which is
asynchronous and offline-tolerant — a host that cannot reach the remote simply catches
up on the next tick.

### Heavy tasks on a stronger machine

A remote runner can offer to take **heavy** tasks — Gradle builds, emulator runs, long
jobs — off the dispatching host. Give it the `heavy` capability in the registry:

```yaml
agents:
  - id: worker-default
    kind: ai-local
    projects: ["*"]
  - id: desktop-runner
    kind: ai-remote
    capabilities: [heavy]
    concurrency: 1
```

A task is heavy when, in this order:

1. `heavy: false` in its frontmatter — **never** heavy (the opt-out beats every rule below);
2. `heavy: true` — always heavy;
3. `emulator: true`, `screen_check: true` or a `long_job:` is set;
4. its external repo's shared clone has a `gradlew` at the root.

A vault task (no `repo`) is heavy only by rules 1–3. Only a task whose `for_project` owner
is an **AI-local** agent is moved; a human-owned task, or one a remote owns by project
(say a Windows-only project), keeps its owner.

The dispatcher sends a heavy task to the first `heavy` remote, in registry order, that is
**online** — it has sent any message within 900 seconds
(`orchestrator agents` shows the same liveness) — and has fewer runs in flight than its
`concurrency`. The envelope carries `requires: [heavy]`; the ledger payload does not, so a
retry on the default agent is never refused for the capability. With no such remote the
task goes to its owner exactly as before. `orchestrator status` marks a task sent this way
`(heavy; fallback worker-default)`.

**Coming back.** At the start of every tick the dispatcher moves a heavy task back to its
owner when its remote:

| reason | when |
| --- | --- |
| `accept-timeout` | no ACCEPT within 600 seconds — the remote is off |
| `agent-stale` | it ACCEPTed, but is no longer online and sent no RESULT/FAIL |
| `rejected` | it REJECTed the task; that remote is not picked for this task again |

The ledger row keeps its `attempts` and `refail_count` (it was not the task's fault), gets
a new `corr_id`, and records `rerouted_from: {agent, reason, ts, corr_id}` — shown by
`status`, `trace <slug>` and `/api/status` (`recent[].rerouted_from`). The remote gets a
`CANCEL` for the old run. Replies the remote still sends about that run are logged as
`ignoring late …` and change nothing. A task moved for `accept-timeout` or `agent-stale`
can go to the remote again on a later dispatch, once it is online.

**What CANCEL does on the remote.** A queued assignment with a CANCEL is skipped without a
reply. A run in progress stops at its next stage boundary: no checkpoint push, no report
stage (so no PR and no merge), no delegate publish, no reply. A report stage that was
already running when the CANCEL arrived cannot be stopped, so its PR may still open.

**Heartbeats carry slots.** Every runner sends a `HEARTBEAT` with
`slots: {max, used}` when either number changes (at most once a minute) and at least
every 300 seconds, idle or busy. The orchestrator assigns only to a free slot (see
[Concepts — free slots](concepts.md#choosing-resources)), so this message is what brings work to a
runner. A remote runner also sends `HEARTBEAT {status: busy}` every
300 seconds from a separate thread while it is
running something, because its serial loop is blocked for the whole run and a single
stage can run for an hour without a progress message.

With the git mailbox, messages and liveness arrive at the vault sync cadence (a few
minutes). The 600 s / 900 s bounds leave room for that.
`max_parallel_gradle_builds` (off by default) still counts
heavy runs that went to a remote.

#### Game mode (pause a heavy remote)

A heavy remote is often someone's own desktop. When its owner wants the machine back (say,
to play a game), switch the agent to **game** mode. Game and the worker never run at the
same time: the switch stops the remote's work and hands it back, it does not merely lower
its priority. Switch it back to **work** to make it a candidate again.

Each agent has a mode, `work` (default) or `game`, kept in one state file in the vault,
`Worker/agents/_state/agent_mode.json` (gitignored, replaced atomically). A missing file
means every agent is `work`. A broken file also means `work` — the router fails open and
prints one `[agent_mode] … treating as work` line to stderr.

**Command line** — one line in, one line out:

```bash
P="uv run --project $HOME/macp python"
$P -m todo_system.orchestrator.orchestrator agent-mode desktop-runner game --reason "playing"
# agent-mode desktop-runner: work -> game since 2026-01-01T20:00:00+09:00 (reason: playing)
$P -m todo_system.orchestrator.orchestrator agent-mode desktop-runner        # show
$P -m todo_system.orchestrator.orchestrator agent-mode desktop-runner work   # resume
```

Only a non-human agent in the registry has a mode; any other id exits with code 2. Setting
the mode an agent already has keeps its `since`. `orchestrator agents` appends
`mode: game since <ts>` to an agent in game mode and leaves every other line as it was.

**HTTP API** — on the dashboard, behind the same token as every other `/api` route (so a
token is required whenever `MACP_PUBLIC_EXPOSURE` is set or the bind is not loopback):

| request | response |
| --- | --- |
| `GET /api/agent-mode?agent=<id>` | `{agent, mode, since, reason}`; 404 for an unknown or human id |
| `GET /api/agent-mode` | `{agents: [{agent, mode, since, reason}, …]}` for every non-human agent |
| `POST /api/agent-mode` with body `{agent, mode, reason}` | `{agent, mode, since, reason, changed}` |

The POST needs `Content-Type: application/json` (otherwise 415) and a body of at most
4096 bytes. `mode` must be `work` or `game` (400), `agent` a registered non-human agent
(404); `reason` is optional, kept to 200 printable characters. A watcher on the remote
machine can poll the GET and POST `game` when it sees a game start, e.g.
`curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d
'{"agent":"desktop-runner","mode":"game","reason":"game started"}' <dashboard>/api/agent-mode`.

**Dashboard.** Each remote's card in the agents section has a **Game mode** / **Resume
work** button that sends that POST, and shows the mode and its start time while in game.
`/api/status` carries the same data on every agent row — `agents[].mode`,
`agents[].mode_since`, `agents[].mode_reason` — so other clients read what the page shows.

**Routing effect.** A game agent counts as offline for heavy routing: new heavy tasks go
to the next usable remote or stay with their owner. On the next tick, every heavy task
that agent holds in flight (dispatched, accepted or in progress) is moved back to its owner
with reason `paused:game`, through the same path as the table above: the remote gets a
`CANCEL`, the row gets a new `corr_id` and `rerouted_from`, and `refail_count`, `attempts`,
`timeout_streak` and `capacity_retry_count` stay as they were. Unlike `rejected`,
`paused:game` does not bar that remote: once it is back to `work`, it is picked for new
heavy tasks and for later re-dispatches of the moved one. A task a remote owns by project
is never moved, and nothing moves while the system is `PAUSED`.

## A healthy system, at a glance

```bash
P="uv run --project $HOME/macp python"
$P -m todo_system.orchestrator.orchestrator status     # the ledger
$P -m todo_system.orchestrator.orchestrator agents     # runner liveness
$P -m todo_system.orchestrator.orchestrator trace <slug>
```

A tick prints a one-line summary such as
`candidates=3 dispatched=1 updated=2 … reaped=0`. A trailing `USAGE-HOLD (…)` means
pacing held new dispatch on purpose (see [Configuration](configuration.md#usage-pacing)).
