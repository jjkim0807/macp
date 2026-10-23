# Configuration

Everything has a working default. You only need `MACP_VAULT_ROOT`, and even that can
be replaced by a `.macp/config.toml` in the vault.

- [Finding the vault](#finding-the-vault)
- [Environment variables](#environment-variables)
- [Switching to the external state backend](#switching-to-the-external-state-backend)
- [Usage pacing](#usage-pacing)
- [Profiles and accounts](#profiles-and-accounts)
- [Anthropic usage and cost](#anthropic-usage-and-cost)
- [Notifications and other integrations](#notifications-and-other-integrations)

## Finding the vault

The engine looks for the vault in this order:

1. `MACP_VAULT_ROOT`
2. the nearest `.macp/config.toml`. It is searched upward from the current directory
   when the engine is installed as a package, and upward from the engine checkout when
   you run from a source clone. So with a clone, set `MACP_VAULT_ROOT`.
3. otherwise it stops with a `VaultRootError` that says what to set.

`<vault>/.macp/config.toml` holds optional host identity. Every key is commented out
in the skeleton, and the matching environment variable wins:

```toml
# label_prefix = "com.example."   # launchd/systemd label prefix (MACP_LABEL_PREFIX)
# calendar_id  = "primary"        # Google Calendar for `cli calendar-sync` (MACP_CALENDAR_ID)
# repo_slug    = "you/my-vault"   # this vault's GitHub remote, for links (MACP_REPO_SLUG)
# timezone     = "Asia/Seoul"     # IANA zone for calendar events (MACP_TIMEZONE)
# worker_root  = "~/Worker"       # where `repo:` tasks are cloned (MACP_WORKER_ROOT)
# language     = "en"             # UI language for log/CLI text: en | ko (MACP_LANGUAGE)
# state_backend      = "git"      # orchestrator state backend: git | external (MACP_STATE_BACKEND)
# state_sqlite_path  = "~/.local/share/macp/state.sqlite"  # external backend's db path (MACP_STATE_SQLITE_PATH)
```

Without `timezone`, calendar events use the host's own IANA zone. If the host does not
report one, the event carries no zone and Google applies the calendar's own.

## Environment variables

| Variable | Effect |
|---|---|
| `MACP_VAULT_ROOT` | the vault directory |
| `MACP_LABEL_PREFIX`, `MACP_CALENDAR_ID`, `MACP_REPO_SLUG` | override the `.macp/config.toml` keys |
| `MACP_TIMEZONE` | IANA timezone for calendar events (key `timezone`; default: the host's zone) |
| `MACP_WORKER_ROOT` | where external repositories are cloned (key `worker_root`; default `~/Worker`) |
| `MACP_LANGUAGE` | UI language for engine log/CLI text, `en` or `ko` (key `language`; default `en`; other values read as `en`) |
| `MACP_STATE_BACKEND` | orchestrator state backend: `git` (default) or `external` (key `state_backend`) |
| `MACP_STATE_SQLITE_PATH` | external backend's SQLite path (key `state_sqlite_path`; default `$XDG_DATA_HOME/macp/state.sqlite`, else `~/.local/share/macp/state.sqlite`) |
| `MACP_MAX_PARALLEL_PER_REPO` | concurrent runs per repository (default 2; `1` forces serial) |
| `MACP_MAX_PARALLEL_GRADLE_BUILDS` | global cap on concurrent Gradle builds (default 0 = no cap) |
| `MACP_DASHBOARD_TOKEN`, `MACP_PUBLIC_EXPOSURE` | dashboard exposure (see [Running 24/7](running.md#the-dashboard)) |
| `USAGE_GUARD_DISABLE` | `1` turns usage pacing off |
| `USAGE_GUARD_*` | pacing thresholds (below) |
| `ANTHROPIC_ADMIN_KEY` | Anthropic Admin API key for the usage/cost CLI |

## Switching to the external state backend

By default the orchestrator keeps its state as files in the vault (`git` backend). The
`external` backend keeps the assignment ledger, the mailbox and the planner state in one
SQLite file outside the vault instead. Moving a host over is a one-off copy with
`todo_system.orchestrator.state_migrate`:

1. **Stop** the orchestrator and runner, so nothing writes state during the copy.
2. **Back up.** `<vault>/Worker/assignments/` is git-tracked, so a clean `git status` is
   its backup. Copy `<vault>/Worker/agents/_state/` and `<vault>/Worker/planner_state.json`
   (both untracked), and an existing `~/.local/share/macp/state.sqlite` if there is one.
3. **Dry run.** This reads only the vault, prints the per-area counts and writes nothing:

   ```bash
   uv run python -m todo_system.orchestrator.state_migrate import --dry-run
   ```

4. **Import.** It copies `Worker/assignments/`, the inboxes and cursors under
   `Worker/agents/`, and `Worker/planner_state.json`. It is safe to re-run: a second run
   over the same vault changes nothing, a ledger row the database already has newer is
   kept, and a message it already acknowledged is not brought back. On a remote host,
   point `--planner-state` at that host's state file. `Todo/todo.db` is not copied (it
   is rebuilt from the task files).

   ```bash
   uv run python -m todo_system.orchestrator.state_migrate import
   ```

5. **Switch.** Set `MACP_STATE_BACKEND=external` in the environment of every engine
   process, or `state_backend = "external"` in `.macp/config.toml`. Set
   `MACP_STATE_SQLITE_PATH` (key `state_sqlite_path`) as well if you do not use the
   default path. Both commands above take `--sqlite-path` and `--vault-root` to match.
6. **Check.** `sqlite3 ~/.local/share/macp/state.sqlite 'SELECT COUNT(*) FROM ledger'`
   should equal `ledger` + `ledger_unchanged` + `ledger_kept_newer` from the import.
   Then export and confirm the snapshot matches the files you started from:

   ```bash
   uv run python -m todo_system.orchestrator.state_migrate export
   git status Worker/assignments    # no changes on a first switch
   ```

In external mode the database is authoritative and `Worker/assignments/` is a read-only
snapshot written by `export` in the same format as before, so the dashboard keeps
working. A hand edit there is overwritten by the next export. In git mode `export` does
nothing unless you pass `--sqlite-path`. To roll back, unset `MACP_STATE_BACKEND` (git is
the default) and the vault files are authoritative again.

## Usage pacing

Automation and your own interactive use share one Claude subscription. The usage
guard (`todo_system/usage_guard.py`) reads the account's utilization for the 5-hour
session window and the weekly window before every dispatch:

- **Pace.** If utilization runs more than an *allowance* ahead of the elapsed share of
  the window, new dispatch is held. The tick prints `USAGE-HOLD (weekly 57% ahead of
  pace (44% elapsed + 12pp allowance))`. Running stages are never interrupted.
- **Hard cap.** Above the cap, dispatch waits for the window to reset.
- **Slots.** Slack below the line buys extra parallel runs.

| Variable | Default | Meaning |
|---|---|---|
| `USAGE_GUARD_DISABLE` | unset | `1` = never hold (for trying macp out, or an API-billed setup) |
| `USAGE_GUARD_WEEKLY_ALLOWANCE` | 12 | percentage points the weekly window may run ahead of pace |
| `USAGE_GUARD_SESSION_ALLOWANCE` | 30 | the same for the 5-hour window |
| `USAGE_GUARD_WEEKLY_HARD_CAP`, `USAGE_GUARD_SESSION_HARD_CAP` | 90 | block outright above this % |
| `USAGE_GUARD_FAIL_CLOSED` | unset | `1` = hold when usage cannot be read (the default lets work through) |

A single task can pass a pacing hold with `bypass_guard: true` in its frontmatter.

## Profiles and accounts

Profiles and accounts live in `~/.macp/` and are managed with
`python -m todo_system.macp`:

```bash
uv run python -m todo_system.macp profile list
uv run python -m todo_system.macp account list
uv run python -m todo_system.macp account add <name> --agent claude --config-dir ~/.claude-accounts/<name>
```

A profile holds one or more agent accounts. Selection takes the project's pin from its
`.macp/config.toml` if there is one, then the profile's `active_account`, then the
lowest `priority` number (a Claude account defaults to 100). Several Claude accounts
let the runner spread parallel runs across them.

## Anthropic usage and cost

`todo_system.anthropic_usage` queries the Anthropic **Admin** API for token counts and
billed dollars, with no browser console needed:

```bash
uv run python -m todo_system.anthropic_usage usage \
    --since 2026-10-16T00:30Z --until 2026-10-16T00:40Z --bucket 1m
uv run python -m todo_system.anthropic_usage cost --day 2026-10-16
```

- Add `--json` for machine-readable output.
- The key is read from `ANTHROPIC_ADMIN_KEY`, or else from
  `<vault>/.secrets/anthropic-admin-key`, and is never printed.
- Data lags live traffic by about five minutes.
- A personal account's key gets 403. The Admin API needs an organization, and the CLI
  says so and exits 2.

The module uses only the standard library, so `python3 -m todo_system.anthropic_usage …`
works from a bare clone. Worker stages in other checkouts call it through a wrapper
without spaces in its name:

```bash
cp scripts/macp-anthropic-usage ~/.local/bin/ && chmod +x ~/.local/bin/macp-anthropic-usage
# allow Bash(macp-anthropic-usage:*) in the task's allowed_tools
```

## Notifications and other integrations

KakaoTalk/Slack notifications, an app-beta publishing sweep and an `advote` bridge
ship in the tree, but each turns on only when its config file or token exists in the
vault. With none present they stay silent: no network calls, at most one warning per
process. [scripts/NOTIFICATIONS.md](../scripts/NOTIFICATIONS.md) lists the exact
switches. Without notifications, the daily brief and the dashboard are where parked
tasks show up.
