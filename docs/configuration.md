# Configuration

Everything has a working default. You only need `MACP_VAULT_ROOT`, and even that can
be replaced by a `.macp/config.toml` in the vault.

- [Finding the vault](#finding-the-vault)
- [Environment variables](#environment-variables)
- [Usage pacing](#usage-pacing)
- [Profiles and accounts](#profiles-and-accounts)
- [Upstage Solar](#upstage-solar)
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
```

## Environment variables

| Variable | Effect |
|---|---|
| `MACP_VAULT_ROOT` | the vault directory |
| `MACP_LABEL_PREFIX`, `MACP_CALENDAR_ID`, `MACP_REPO_SLUG` | override the `.macp/config.toml` keys |
| `MACP_MAX_PARALLEL_PER_REPO` | concurrent runs per repository (default 2; `1` forces serial) |
| `MACP_MAX_PARALLEL_GRADLE_BUILDS` | global cap on concurrent Gradle builds (default 0 = no cap) |
| `MACP_DASHBOARD_TOKEN`, `MACP_PUBLIC_EXPOSURE` | dashboard exposure (see [Running 24/7](running.md#the-dashboard)) |
| `USAGE_GUARD_DISABLE` | `1` turns usage pacing off |
| `USAGE_GUARD_*` | pacing thresholds (below) |
| `UPSTAGE_API_KEY`, `MACP_UPSTAGE_KEY_FILE` | the Upstage key, or where to read it |
| `ANTHROPIC_ADMIN_KEY` | Anthropic Admin API key for the usage/cost CLI |

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

## Upstage Solar

An account can come from another vendor. `upstage` uses an API key instead of a
config directory:

```bash
uv run python -m todo_system.macp account add solar --agent upstage --priority 150
uv run python -m todo_system.macp account probe solar --tier light   # one real call → one line of JSON
```

`--priority 150` keeps it *behind* the default Claude account.

**The key is never stored.** It is read at call time from `UPSTAGE_API_KEY`, or else
from a key file holding one `UPSTAGE_API_KEY=…` line. The default file is
`~/.config/macp/upstage.env`; keep it at mode 0600, because a looser mode prints a
warning. The profile keeps only the *path*, so no key reaches a config file, log line,
commit or PR. `account list` reports where the key comes from
(`key=env|file|missing`), never its value. Point elsewhere with `--key-file`,
`MACP_UPSTAGE_KEY_FILE`, or `upstage_key_file` in a project's `.macp/config.toml`.

**Running stages on Solar.** A task with `agent: upstage` in its frontmatter runs its
`quick` or `delegate` stage on Solar (`orchestrator/solar_stage.py`). That is an
in-engine tool-calling loop with read-file, write-file and finish tools and **no
shell**, bounded by an iteration cap and a deadline. Every other task, and every
stage of the five-stage pipeline, still runs on Claude Code.

**Models.** The tier → model mapping defaults to date-pinned snapshots:
`solar-pro4-260806` for heavy/standard and `solar-mini4-260922` for light. Pins age,
so replace them per account:

```bash
uv run python -m todo_system.macp account add solar --agent upstage \
    --models light=solar-mini4-260922,heavy=solar-pro4-260806
```

You can also edit `models = { … }` in the profile's `[[account]]` block.

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
