# Running 24/7

[Getting started](getting-started.md) runs each piece once by hand. Running
continuously means keeping five processes alive:

| Process | Command (run from the vault) | Cadence |
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
cd ~/my-vault
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
cd ~/my-vault
P="uv run --project $HOME/macp python"
$P -m todo_system.orchestrator.orchestrator status     # the ledger
$P -m todo_system.orchestrator.orchestrator agents     # runner liveness
$P -m todo_system.orchestrator.orchestrator trace <slug>
```

A tick prints a one-line summary such as
`candidates=3 dispatched=1 updated=2 … reaped=0`. A trailing `USAGE-HOLD (…)` means
pacing held new dispatch on purpose (see [Configuration](configuration.md#usage-pacing)).
