# scripts/

Daemons, installers, wrappers and integrations. The engine's Python code is in
`todo_system/`. These are the entry points around it.

## Core — what a normal installation runs

| Script | What it does |
|---|---|
| `orchestrator_tick.sh` | one orchestrator tick: dispatch, collect, retry, reap (every ~5 min) |
| `planner_tick.sh` | planner tick: when the queue is idle, have the planner file new tasks |
| `macp_dashboard.py` | local live dashboard on `127.0.0.1:8787` |
| `brief.sh` | write the daily brief to `<vault>/Todo/Briefings/YYYY-MM-DD.md` |
| `health_report.py` | weekly health report of the automation stack |
| `export_db_snapshot.py` | JSON snapshot of task data for the brief agent |
| `macp_monitor.py` | sensor that turns anomalies (stranded branches, stuck runs, silent logs) into findings |
| `systemd/gen_units.py` | render the Linux systemd `--user` units for all of the above ([Running 24/7](../docs/running.md)) |

## macOS LaunchAgent installers

`install_runner_launchagent.sh`, `install_orchestrator_launchagent.sh`,
`install_todo_sync_launchagent.sh`, `install_brief_launchagent.sh`,
`install_health_launchagent.sh` and `install_macp_monitor_launchagent.sh` install the
macOS equivalents of the systemd units. `install_worker_launchagent.sh` and
`worker_run_next.sh` drive the vault-upkeep loop.

## Wrappers for worker stages

Worker tool allowlists are split on spaces, so `uv run …` cannot be allowed as one
rule. These wrappers have names without spaces. Copy one to `~/.local/bin/` and allow
`Bash(<name>:*)`.

| Wrapper | Runs |
|---|---|
| `macp` | the profile/account CLI (`python -m todo_system.macp`) |
| `macp-anthropic-usage` | Anthropic Admin API usage and cost |
| `macp-sms` | the host's phone-SMS inbox (verification codes) |
| `macp-browser` | a logged-in Chrome over CDP, with a lease so one user drives it at a time |
| `macp-remote-gate` | a heavy verification command on a separate high-memory node |

## Several hosts

| Script | What it does |
|---|---|
| `orchestrator_runner_remote.sh` | runner loop on a secondary host (WSL/Linux) |
| `worker_remote_loop.sh`, `setup_remote_host_wsl.sh` | the remote-host worker loop and its one-time setup |
| `app-test-node/` | an on-demand Android emulator node for app screen checks |

## Account and auth upkeep

| Script | What it does |
|---|---|
| `claude_auth_watch.py` | alert when the Claude Code CLI credential dies |
| `claude_model_autoswitch.py` (+ installer) | keep the legacy config dir pointed at a model that can run |
| `claude_accounts_migrate.py` | one-shot migration of a single Claude config dir into the multi-account layout (with `--rollback`) |
| `devtunnel_watchdog.py` | restart a dev tunnel host that is alive but no longer hosting |

## Author integrations (opt-in)

These stay silent unless their config file exists in the vault; see
[NOTIFICATIONS.md](NOTIFICATIONS.md).

- **Notifications:** `kakao_send.sh` (dispatcher), `slack_send.py` (Slack backend),
  `kakao_route.py` (noise tiers), `kakao_digest_send.py` (daily digest),
  `notify_kakao.sh` and `_build_brief_message.py` (brief summary),
  `kakao_token_refresh.sh`, `kakao_oauth_helper.sh`, and
  `install_kakao_refresh_launchagent.sh`. The Korean guides `KAKAO_SETUP.md` and
  `KAKAO_ALERTS.md` cover setup and alert tiers.

## Engine repository upkeep

| Script | What it does |
|---|---|
| `check_engine_isolation.sh` | prove the engine runs from a bare clone with no vault data (part of the local checks) |
| `export_engine_tree.sh`, `public_export_exclude.txt` | reproduce the public engine tree from a combined checkout |
