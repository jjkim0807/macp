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

Other units cover housekeeping: maintenance (worktree sweep, engine self-update), the
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
`launchctl list | grep <prefix>`.

## Updating the engine

The maintenance tick fast-forwards the engine checkout (`git pull --ff-only`) and
restarts resident daemons whose code changed. You can also `git pull` yourself; the
daemons pick it up within one maintenance tick.

## Disk space

Android and Gradle work fills a root disk quickly. Two guards keep it usable. Both
stay silent while there is enough free space.

| | Run-start disk guard | Maintenance cache prune |
|---|---|---|
| Code | `orchestrator/disk_guard.py` | `maintenance_disk.py` |
| When | once per run, before branch prep | every maintenance tick (~5 min) |
| Trigger → target | < 3 GiB → 5 GiB free | < 8 GiB → 10 GiB free |
| What it frees | `./gradlew clean --no-daemon` in idle `~/Worker` clones | caches that Gradle and uv download again on demand |
| Kill switch | `MACP_DISK_GUARD=0` | `MACP_DISK_PRUNE=0` |

### What the cache prune removes

Below the trigger, the prune runs one pass, at most once every 30 minutes:

1. `uv cache prune` (uv's own command, which takes uv's own lock).
2. If Gradle is idle, `<gradle home>/caches/<version>/` directories for a Gradle
   version that no `~/Worker` wrapper (`gradle/wrapper/gradle-wrapper.properties`,
   linked worktrees included) uses, or that nothing below has touched for N days. If
   any wrapper file can't be parsed, every version directory is kept.
3. If Gradle is idle, regular hex-named files directly in
   `<gradle home>/caches/build-cache-1/` that haven't changed for N days, oldest first.

It stops once free space reaches the target. Every deletion goes through one function
that checks the real path against this allowlist and refuses symlinks. These are never
touched: `caches/modules-2`, every distribution under `<gradle home>/wrapper`, other
`caches/*` entries, the `~/Worker` clones and worktrees, the vault, `~/.config`, and
the browser caches under `~/.cache`.

**Gradle is busy** when a ledger row holding a repo is dispatched, accepted or
in progress, when a long_job is running or has a state file (the run-start guard's
rule), or when a Gradle daemon, wrapper or worker process is alive. An unreadable
ledger or a host without `/proc` also counts as busy. While Gradle is busy, only uv is
pruned, and the line says why the Gradle part was skipped.

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
| `MACP_DISK_PRUNE` | on | `0` switches the step off |
| `MACP_DISK_PRUNE_MIN_FREE_GIB` | 8 | trigger and alert threshold |
| `MACP_DISK_PRUNE_TARGET_GIB` | trigger + 2 | stop deleting at this much free space |
| `MACP_DISK_PRUNE_MAX_AGE_DAYS` | 30 | N in "unchanged for N days" |
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
