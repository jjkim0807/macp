# macp

The engine of MaestroACP: a markdown-first task system (task files → SQLite), an
orchestrator that dispatches tasks to headless Claude Code workers through a staged
pipeline (research → plan → implement → verify → report), a planner that refills the
queue, usage pacing against your Claude subscription limits, and the daemons that run
it all (systemd `--user` on Linux, launchd on macOS).

The engine holds no personal data. Tasks, memory, notes, decisions, run records,
secrets and host identity live in a separate **vault** directory that you own.

> **Maturity.** This is a personal-scale system published as-is. The core (task CLI,
> orchestrator, staged runner, usage guard, dashboard) is tested and runs standalone,
> but several integrations are still shaped around the author's own setup — see
> [Known limitations](#known-limitations). Read [SECURITY.md](SECURITY.md) before you
> let a worker run for real: workers execute code on your machine and can push to
> GitHub.

## Requirements

- Linux or macOS, Python ≥ 3.12, [`uv`](https://docs.astral.sh/uv/), `git`
- For real worker runs: the [Claude Code](https://code.claude.com) CLI on `PATH` and
  logged in (`claude` works in a terminal), and `gh` authenticated if tasks open PRs

## Quick start (no Claude calls)

```bash
git clone https://github.com/jjkim0807/macp ~/macp && cd ~/macp
uv sync

# 1. Make a vault from the skeleton and point the engine at it.
cp -r vault-template ~/my-vault
export MACP_VAULT_ROOT=~/my-vault

# 2. Scaffold a task, then edit ~/my-vault/Todo/items/hello-world.md:
#    fill outcome / next_action / if_then and change `worker_mode: blocked` to `auto`.
uv run python -m todo_system.cli new hello-world --template auto-worker
uv run python -m todo_system.cli import-md      # markdown → Todo/todo.db
uv run python -m todo_system.cli list

# 3. Dispatch it and run the worker once in shadow mode (no agent is spawned).
#    The orchestrator and runner resolve Worker/ relative to the current
#    directory, so run them from the vault.
cd ~/my-vault
uv run --project ~/macp python -m todo_system.orchestrator.orchestrator auto
uv run --project ~/macp python -m todo_system.orchestrator.runner \
    --agent worker-default --mode shadow --once
uv run --project ~/macp python -m todo_system.orchestrator.orchestrator status
```

`--mode live-staged` instead of `shadow` runs the real five-stage pipeline with
`claude -p`. A task with `repo:` set is cloned under `~/Worker/<repo>` and worked on in
a per-run git worktree; a task without it works on the vault itself.

## Vault

Resolution order:

1. `MACP_VAULT_ROOT`
2. the nearest `.macp/config.toml` — searched upward from the current directory when
   the engine is installed as a package, and upward from the engine checkout when you
   run from a source clone (so with a clone, set `MACP_VAULT_ROOT`)
3. otherwise a clear `VaultRootError`

`vault-template/` contains the skeleton:

| Path | What it is |
|---|---|
| `Todo/items/*.md` | one task per file — the source of truth (`Todo/todo.db` is a derived cache) |
| `Todo/templates/` | task templates for `cli new --template …` |
| `Worker/agents/registry.yaml` | who owns which tasks: a `human` agent and the `ai-local` worker |
| `Worker/runs/`, `Worker/assignments/` | per-run artifacts and the dispatch ledger (created on first use) |
| `Memory/`, `Notes/`, `Decisions/` | free-form knowledge the planner and workers read |
| `.macp/config.toml` | optional host identity: daemon label prefix, calendar id, repo slug |

Keep the vault in its own private git repository; the daemons commit task-state
changes to it.

## Task files

Markdown with YAML frontmatter. The fields the engine acts on:

| Field | Meaning |
|---|---|
| `status` | `open` · `done` · `cancelled` |
| `worker_mode` | `auto` (5-stage pipeline) · `quick` (one combined stage) · `delegate` (research/update, no code merge) · `manual` (a human does it) · `blocked` · `off` |
| `outcome`, `next_action`, `if_then` | done-criterion, first step, and the concrete trigger — required |
| `repo` | git URL of the project the task changes; omit for vault-only work |
| `auto_merge` | `true` lets the report stage squash-merge its PR once local checks pass |
| `allowed_tools` | a **quoted, comma-separated string** that replaces the stage's tool allowlist |
| `touches` | a **YAML list** of repo-relative paths; two tasks of the same repo run in parallel only when both declare disjoint lists |
| `acceptance_criteria` | list of checks the verify stage must see pass |
| `model_tier` | `heavy` · `standard` · `light` (otherwise inferred) |
| `depends_on`, `not_before` | ordering and earliest start (`YYYY-MM-DD` or `YYYY-MM-DD HH:MM`) |

Never put a `# comment` after a value — the parser keeps it as part of the value.
`uv run python -m todo_system.cli validate <slug>` checks a file against the worker
contract.

## Running it continuously

The pieces are a **runner** (long-lived, executes pipelines), an **orchestrator tick**
(every ~5 min: dispatch, collect results, reap stale runs), **todo-sync** (watches
`Todo/items` → `todo.db`), an optional **planner** tick, and the **dashboard**. A
minimal setup is just these, for example as your own systemd user units or tmux panes:

```bash
cd ~/my-vault
uv run --project ~/macp python -m todo_system.orchestrator.runner --agent worker-default --mode live-staged --daemon
uv run --project ~/macp python -u -m todo_system.cli daemon --interval 3
while sleep 300; do uv run --project ~/macp python -m todo_system.orchestrator.orchestrator auto; done
uv run --project ~/macp python ~/macp/scripts/macp_dashboard.py      # http://127.0.0.1:8787
```

`scripts/systemd/gen_units.py --print` renders the author's full unit set (Linux) and
`scripts/install_*_launchagent.sh` the macOS equivalents. They still assume the
author's layout (see below): review the `--print` output before writing anything, and
note that `--only` filters which units are *enabled*, not which files are written.

The dashboard binds to loopback. It refuses to start on any other address — or, when
`MACP_PUBLIC_EXPOSURE=1` declares that a tunnel fronts it, on loopback too — unless
`MACP_DASHBOARD_TOKEN` is set.

## Configuration

| Variable | Effect |
|---|---|
| `MACP_VAULT_ROOT` | the vault directory |
| `MACP_LABEL_PREFIX`, `MACP_CALENDAR_ID`, `MACP_REPO_SLUG` | override `.macp/config.toml` keys |
| `MACP_MAX_PARALLEL_PER_REPO`, `MACP_MAX_PARALLEL_GRADLE_BUILDS` | concurrency caps |
| `MACP_DASHBOARD_TOKEN`, `MACP_PUBLIC_EXPOSURE` | dashboard exposure (see above) |

Profiles and Claude accounts (`python -m todo_system.macp profile|account …`) live in
`~/.macp/`.

## Anthropic usage & cost

`todo_system.anthropic_usage` queries the Anthropic **Admin** API (no browser
console) for token counts and billed dollars:

```bash
# tokens per model/key + list-price cost, one-minute buckets
uv run python -m todo_system.anthropic_usage usage \
    --since 2026-10-16T00:30Z --until 2026-10-16T00:40Z --bucket 1m
# the amounts Anthropic billed, in USD (cents in the API)
uv run python -m todo_system.anthropic_usage cost --day 2026-10-16
```

Add `--json` for machine-readable output. The key is read from
`ANTHROPIC_ADMIN_KEY`, else `<vault>/.secrets/anthropic-admin-key`, and is never
printed. Report data lags live traffic by ~5 minutes. A **personal** account's
key gets 403 — the Admin API needs an organization (switch the account first);
the CLI then prints a one-line hint and exits 2.

Stdlib-only, so it also runs from a bare clone (`python3 -m
todo_system.anthropic_usage …`). For worker stages in other checkouts, install
the space-free wrapper and allow `Bash(macp-anthropic-usage:*)`:

```bash
cp scripts/macp-anthropic-usage ~/.local/bin/macp-anthropic-usage && chmod +x ~/.local/bin/macp-anthropic-usage
```

## Known limitations

- **Author-specific integrations ship in the tree**: KakaoTalk/Slack notifications
  (without credentials they are a silent no-op), stock-trading and `mailroom` units in
  `gen_units.py`, an app-beta publishing sweep, and an `advote` bridge. None run
  unless configured, but they add noise.
- `gen_units.py` defaults to `~/Projects/MaestroACP` (override with
  `MAESTRO_VAULT_DIR`) and renders units for scripts that are not in this repository.
- Parts of the worker prompts (`.claude/commands/`) and some docs are in Korean, and
  calendar sync uses the `Asia/Seoul` time zone.
- External repositories are cloned under `~/Worker/`.

These are being cleaned up; issues and PRs are welcome.

## Development

```bash
MACP_VAULT_ROOT=vault-template uv run --with pytest python -m pytest -q todo_system scripts/systemd
uv run --with ruff ruff check .
bash scripts/check_engine_isolation.sh
```

GitHub Actions is not used; run the checks locally. The full suite takes several
minutes.

## License

MIT — see [`LICENSE`](LICENSE).
