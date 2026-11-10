# Configuration

Everything has a working default. You only need `MACP_VAULT_ROOT`, and even that can
be replaced by a `.macp/config.toml` in the vault.

- [Finding the vault](#finding-the-vault)
- [Environment variables](#environment-variables)
- [Switching to the external state backend](#switching-to-the-external-state-backend)
- [Remote state backend](#remote-state-backend)
- [Usage pacing](#usage-pacing)
- [Profiles and accounts](#profiles-and-accounts)
- [Anthropic usage and cost](#anthropic-usage-and-cost)
- [Browser MCP server](#browser-mcp-server)
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
# state_backend      = "git"      # orchestrator state backend: git | external | remote (MACP_STATE_BACKEND)
# state_sqlite_path  = "~/.local/share/macp/state.sqlite"  # external backend's db path (MACP_STATE_SQLITE_PATH)
# state_remote_url   = "http://HQ-HOST:PORT"  # remote backend: HQ state API base URL (MACP_STATE_REMOTE_URL)
# (the remote bearer token has NO config key — it is a secret; use MACP_STATE_REMOTE_TOKEN
#  or MACP_STATE_REMOTE_TOKEN_FILE, below)
# browser_mcp_server = "/path/to/macp-browser/mcp/server.mjs"  # macp-browser MCP server for browser stages (MACP_BROWSER_MCP_SERVER)
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
| `MACP_STATE_BACKEND` | orchestrator state backend: `git` (default), `external`, or `remote` (key `state_backend`) |
| `MACP_STATE_SQLITE_PATH` | external backend's SQLite path (key `state_sqlite_path`; default `$XDG_DATA_HOME/macp/state.sqlite`, else `~/.local/share/macp/state.sqlite`) |
| `MACP_STATE_REMOTE_URL` | remote backend: base URL of the HQ state API (key `state_remote_url`; required when the backend is `remote`) |
| `MACP_STATE_REMOTE_TOKEN` / `MACP_STATE_REMOTE_TOKEN_FILE` | remote backend bearer token, or a file to read it from. Env-only — **no config key** (it is a secret). Never logged |
| `MACP_BROWSER_MCP_SERVER` | macp-browser MCP server script for the browser stages (key `browser_mcp_server`; default unset = no MCP wiring; `MACP_BROWSER_NODE` picks the node binary, default `node`). See [Browser MCP server](#browser-mcp-server) |
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

## Remote state backend

The `external` backend keeps the SQLite file on one host. A runner on a **different**
machine cannot open that file, so the `remote` backend reaches the same state over an
authenticated HTTP hop (design `docs/design/state-store.md` §⑧ option (a), $0 — no managed
database). The authoritative SQLite stays in one place:

- **HQ (the primary host)** runs `external` (the SQLite source of truth) **and** serves the
  state API:

  ```bash
  MACP_STATE_REMOTE_TOKEN_FILE=~/.secrets/macp-state-token \
    uv run python -m todo_system.orchestrator.state_store_remote serve --bind 127.0.0.1:8787
  ```

- **A remote host** sets `MACP_STATE_BACKEND=remote`, points `MACP_STATE_REMOTE_URL` at the
  HQ API, and supplies the same bearer token with `MACP_STATE_REMOTE_TOKEN` (or
  `MACP_STATE_REMOTE_TOKEN_FILE`). `scripts/orchestrator_runner_remote.sh` reads
  `STATE_BACKEND` / `STATE_REMOTE_URL` / `STATE_REMOTE_TOKEN[_FILE]` and wires them for you.

How it behaves:

- **Auth.** Every request carries `Authorization: Bearer <token>`; a missing or wrong token
  is rejected with `401`. Keep the token out of the repo and the vault — use the env var or a
  file (there is deliberately **no** `state_remote_url`-style config key for it). It is never
  logged. Behind a tailnet you can also restrict the listener to the mesh (a `tailscale`
  ACL), so the token alone is not the only barrier.
- **HQ down.** A call that cannot reach HQ retries with backoff, then a failed *write* is
  appended to a host-local append-only outbox and the runner stops rather than losing the
  task; when HQ returns, the outbox is flushed in order (idempotent upserts). Claiming new
  work needs a live HQ, so a down HQ means "finish what you have, pick up nothing new".
- **Single execution.** Two runners that receive the same message both call `claim`; the HQ
  grants the lease to exactly one (a `BEGIN IMMEDIATE` conditional insert), so the task runs
  once. A crashed owner's lease expires by TTL and can be re-acquired.

**Not done here (task 7b, human):** actually exposing the HQ port on the network, installing
a unit, or running `tailscale serve` is a separate human step. The code above runs the server
on loopback only. Provisioning a managed database (Cloud SQL) is a separate human-gated
escalation (§⑧ option (b)); `Supabase`/`Neon` are not used.

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

## Browser MCP server

The macp-browser folder ships a stdio MCP server (`mcp/server.mjs`) that attaches to the
logged-in Chrome once and takes the browser lease itself on the first tool call. It is
off by default. To turn it on for worker stages, point the engine at the script:

```toml
browser_mcp_server = "/path/to/macp-browser/mcp/server.mjs"
```

(or set `MACP_BROWSER_MCP_SERVER`). When the path names an existing file, the
implement, verify, quick and delegate stages get:

- `--mcp-config` with an inline server definition whose env sets
  `MACP_BROWSER_OWNER` to the run id, so the stage's lease owner is the run;
- `--strict-mcp-config`, so a user-scope registration of the same server (below) is
  not loaded into the stage as a second copy without the run's owner;
- `mcp__macp-browser__*` at the end of the allowed tools, also when a task supplies
  its own `allowed_tools`;
- one run-metadata line telling the stage to use the MCP tools first and keep the
  `macp-browser` wrapper for compatibility.

The `Bash(macp-browser:*)` wrapper rule and its lease/scratch lines stay as they are.
research, plan, report and discover never get the server. With the key unset, or the
path pointing at a missing file, stage argv is byte-identical to a host without this
feature. `MACP_BROWSER_NODE` names the node binary when `node` is not on the
runner's `PATH`.

Interactive Claude Code sessions register the same server themselves, with their
own session id as the owner:

```bash
claude mcp add --scope user macp-browser \
  --env MACP_BROWSER_OWNER=<your-session-id> \
  -- node /path/to/macp-browser/mcp/server.mjs
```

That registration does not reach worker stages, which run with `--strict-mcp-config`.

## App screen check and launch extras

After the verify stage of a task on an **app repo**, the runner can build the debug
APK, launch it on the shared emulator node and pull its first screen into the run
dir. This is **on by default** when the run's diff touches the app module, so an app
change cannot merge with nobody having seen a screen. Turn it off for one task with
`screen_check: false` (see [Task fields](tasks.md)). A non-app repo, an opted-out
task, or a diff that misses the app module is a complete no-op — no emulator node is
started and nothing is appended to `verify.md`.

A bare launcher launch stops a **sign-in-gated** app on its login screen, so every
screenshot shows the gate instead of the change. A repo can therefore declare launch
intent extras, which both this check and the beta smoke gate pass to `am start`.
Both read the repo's entry in the vault's `.macp/beta_publish_config.json`, under a
`screen_check` or a `smoke` block (`screen_check` wins when both carry the key):

```json
{
  "repos": {
    "demo-app": {
      "screen_check": {
        "app_paths": ["app/"],
        "activity": ".MainActivity",
        "launch_extras": [
          {"type": "string", "key": "session_token",
           "value_from_secret": ".secrets/demo-app.json#session_token"},
          {"type": "bool", "key": "skip_onboarding", "value": true},
          {"type": "int", "key": "start_tab", "value": 2}
        ]
      }
    }
  }
}
```

| Key | Meaning |
|---|---|
| `app_paths` | path prefixes that count as "the app module". Default `["app/"]` — set it for a repo whose app lives elsewhere (e.g. `["composeApp/"]`) |
| `activity` | the component `am start -n <pkg>/<activity>` launches. **Required for extras**: without it the launch stays on the launcher intent |
| `launch_extras` | a list of `{type, key, value \| value_from_secret}`. `type` is `string` (`--es`), `bool` (`--ez`) or `int` (`--ei`); it defaults to `string` |

**Secret values never go in this file.** As with `build_props`, the config names the
file and the field and the value is read at launch time:
`value_from_secret: "<vault-relative json path>#<field>"`, pointing at a gitignored
JSON object in the vault. A path that escapes the vault is refused.

A declaration that cannot be honoured — missing secret file, absent/blank field,
unreadable JSON, a bad type, no `activity` — is a **clean skip**, never a crash: the
app launches with the original launcher intent and one line naming the *file and
field* (never a value) is appended to the run's `verify.md` note. Resolved secret
values are also redacted out of `install.log`, since a remote shell can echo its own
command line; they appear in no run log, no note, no result JSON and no exception.

## Notifications and other integrations

KakaoTalk/Slack notifications, an app-beta publishing sweep and an `advote` bridge
ship in the tree, but each turns on only when its config file or token exists in the
vault. With none present they stay silent: no network calls, at most one warning per
process. [scripts/NOTIFICATIONS.md](../scripts/NOTIFICATIONS.md) lists the exact
switches. Without notifications, the daily brief and the dashboard are where parked
tasks show up.
