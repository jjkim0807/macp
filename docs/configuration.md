# Configuration

Everything has a working default. You only need `MACP_VAULT_ROOT`, and even that can
be replaced by a `.macp/config.toml` in the vault.

- [Finding the vault](#finding-the-vault)
- [Environment variables](#environment-variables)
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
| `MACP_BROWSER_MCP_SERVER` | macp-browser MCP server script for the browser stages (key `browser_mcp_server`; default unset = no MCP wiring; `MACP_BROWSER_NODE` picks the node binary, default `node`). See [Browser MCP server](#browser-mcp-server) |
| `MACP_DASHBOARD_TOKEN`, `MACP_PUBLIC_EXPOSURE` | dashboard exposure (see [Running 24/7](running.md#the-dashboard)) |
| `USAGE_GUARD_DISABLE` | `1` turns usage pacing off |
| `USAGE_GUARD_*` | pacing thresholds (below) |
| `ANTHROPIC_ADMIN_KEY` | Anthropic Admin API key for the usage/cost CLI |

Concurrency per repository and across Gradle builds are the profile fields
`max_parallel_per_repo` (default 2; `1` forces serial) and `max_parallel_gradle_builds`
(default 0 = no cap).

### Heavy tasks

A remote runner with `capabilities: [heavy]` in the registry receives heavy tasks while it
is online; when it goes quiet they come back to their owner. Task frontmatter decides what
is heavy:

| field | effect |
| --- | --- |
| `heavy: false` | never heavy — beats every other signal |
| `heavy: true` | always heavy |
| `emulator: true`, `screen_check: true`, `long_job: …` | heavy |
| (none of the above) | heavy when the task's repo clone has a `gradlew` at its root |

A heavy task that may need a **device check** goes only to a remote that also has
`capabilities: [heavy, device]`; otherwise it stays with its owner. "May need" means:
`emulator` / `screen_check` was asked for, the clone is not on disk yet, or the clone has a
`gradlew` and one of the repo's screen-check app paths (`app/` by default, widened by the
repo's `screen_check.app_paths` in the vault's `beta_publish_config.json`). The reason is
the post-verify screen check, which only a remote that can reach the emulator node can
run. A Gradle server repo (no `app/`) and a non-app long job still go to a plain `heavy`
remote. `screen_check: false` is
not an exemption — the runner still checks an app diff.

A task that goes to a remote is not held by this host's per-repo checkout cap or its
global gradle cap (`max_parallel_gradle_builds`), and a remote's in-flight run does not
count toward them: the remote builds in its own clone and memory. Its own `concurrency`
bounds it instead.

A `git://` remote is also skipped while its mailbox checkout (the vault) is on a branch
other than `main` — a worker run on the vault itself — because a message committed there
would never be pushed to the remote; the task stays with its owner that tick.

A remote earns `device` by running its own emulator: set `APP_TEST_NODE_MODE=local` in the
runner's environment and `scripts/app-test-node` (screen check, `macp-emulator`, beta smoke)
drives an AVD named `api30` on that host instead of the GCE node. The host needs the SDK's
`emulator`, `platform-tools`, a build-tools with `aapt2` and the AVD; `app-test-node start`
checks the first three.

### The emulator node's lifecycle (`.macp/remote_gate.json`)

Without `APP_TEST_NODE_MODE=local`, `scripts/app-test-node start|stop` (and so screen check,
`macp-emulator` and beta smoke) run the engine helper
`python -m todo_system.orchestrator.remote_gate node-up|node-down`, which follows
`node_mode` in the vault's `.macp/remote_gate.json` — the same config and code the remote
test gate uses:

| `node_mode` | `start` | `stop` | between runs |
|---|---|---|---|
| `start-stop` (default) | `instances start` | `instances stop` | the stopped VM and its disk (billed) |
| `create-delete` | reuse a running VM, start a stopped one, else **create** from the image | **delete** (an absent VM is ok) | only the image |

`create-delete` always passes `--max-run-duration=<per_run_vm_ttl_min>` with termination
action `DELETE`, so a leaked VM deletes itself, and a spot preemption deletes the VM too
(`start` then creates a fresh one). Its create spec comes only from the config; each key is
optional and its flag is omitted when absent:

| key | gcloud flag |
|---|---|
| `image_family` (preferred) / `image` | `--image-family` / `--image` |
| `image_project` | `--image-project` |
| `spot: true` | `--provisioning-model=SPOT` |
| `nested_virtualization: true` | `--enable-nested-virtualization` (the emulator needs KVM) |
| `boot_disk_gb`, `boot_disk_type` | `--boot-disk-size=<n>GB`, `--boot-disk-type` |
| `network`, `subnet`, `tags`, `labels` | the same-named flags |
| `no_service_account: true` / `service_account`, `scopes` | `--no-service-account --no-scopes` / `--service-account`, `--scopes` |
| `no_address: true` | `--no-address` |

The helper runs on `APP_TEST_PYTHON` (default `python3`; the engine passes its own
interpreter) and needs the vault root (`MACP_VAULT_ROOT`). If it cannot run at all — no
`remote_gate.json`, or the engine fails to import — the script warns and falls back to a
direct `gcloud instances start|stop`. `APP_TEST_SSH_WAIT` (default 300 s) bounds the wait
for ssh after `start`; a create restores the disk from the image and boots slower than a
start. A fresh VM also starts with the image's Gradle cache and AVD `/data`, so the first
build on it is slower and no device state carries over between runs.

**Refreshing the image** after changing the SDK, AVD or system packages:

1. `scripts/app-test-node start`, make the change over `scripts/app-test-node ssh`, then
   `ssh 'sudo sync'`. Finish within `per_run_vm_ttl_min` — the VM deletes itself after it.
2. `gcloud compute instances stop <node> --zone <zone>` — stop, not delete.
3. `gcloud compute images create <family>-$(date +%Y%m%d) --source-disk <node>
   --source-disk-zone <zone> --family <family> --storage-location <region>`
4. `gcloud compute instances delete <node> --zone <zone> --quiet` (the boot disk goes with it).
5. Optionally delete the previous image (`gcloud compute images list --filter=family=<family>`).

New creates take the newest image in the family, so the config does not change.

The rules, the reroute reasons (`accept-timeout`, `agent-stale`, `rejected`) and what a
remote does on `CANCEL` are in [Running 24/7 → Heavy tasks on a stronger
machine](running.md#heavy-tasks-on-a-stronger-machine).

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

KakaoTalk/Slack notifications and an app-beta publishing sweep
ship in the tree, but each turns on only when its config file or token exists in the
vault. With none present they stay silent: no network calls, at most one warning per
process. [scripts/NOTIFICATIONS.md](../scripts/NOTIFICATIONS.md) lists the exact
switches. Without notifications, the daily brief and the dashboard are where parked
tasks show up.
