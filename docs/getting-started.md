# Getting started

This guide takes you from a fresh clone to a real task that a coding agent finishes
and turns into a pull request. Part 1 needs no accounts; part 2 does.

- [Part 1 — the loop, without an agent](#part-1--the-loop-without-an-agent)
- [Part 2 — a real run on one of your repositories](#part-2--a-real-run-on-one-of-your-repositories)
- [Where to go next](#where-to-go-next)

## Part 1 — the loop, without an agent

### 1. Install

You need Linux or macOS, Python ≥ 3.12, [`uv`](https://docs.astral.sh/uv/) and `git`.

```bash
git clone https://github.com/jjkim0807/macp ~/macp
cd ~/macp
uv sync
```

`~/macp` is the **engine**. It contains code and nothing about you.

### 2. Make a vault

The **vault** is the directory that holds everything that is yours: task files, the
dispatch ledger, run records, memory notes, secrets. Start from the skeleton:

```bash
cp -r ~/macp/vault-template ~/my-vault
export MACP_VAULT_ROOT=~/my-vault
```

Put `export MACP_VAULT_ROOT=…` in your shell profile so every command finds the vault.
Later, make `~/my-vault` its own **private** git repository. The daemons commit
task-state changes to it, and it is your backup.

What is inside:

| Path | What it is |
|---|---|
| `Todo/items/*.md` | one task per file — the source of truth |
| `Todo/templates/` | templates for `cli new --template …` |
| `Worker/agents/registry.yaml` | who owns which tasks: you (`kind: human`) and the AI worker (`kind: ai-local`) |
| `Memory/`, `Notes/`, `Decisions/` | free-form knowledge the planner and workers read |
| `.macp/config.toml` | optional host settings (all commented out) |

### 3. Turn off usage pacing for now

macp normally paces automation against your Claude subscription. It reads the
utilization of the 5-hour and weekly windows and holds dispatch when automation runs
ahead of pace. While you are only trying things out, switch it off:

```bash
export USAGE_GUARD_DISABLE=1
```

Unset it before you run workers for real (see [Configuration](configuration.md#usage-pacing)).

### 4. Write a task

```bash
cd ~/macp
uv run python -m todo_system.cli new hello-world --template auto-worker
```

This creates `~/my-vault/Todo/items/hello-world.md`. Open it. The top part is YAML
frontmatter, the fields the engine acts on. The body is free text the agent reads.
Fill in at least:

```yaml
outcome: "README.md has a one-line project description"
next_action: "Read README.md and add the description under the title"
if_then: "When the worker picks this up, edit README.md and run the tests"
worker_mode: auto
```

The scaffold starts with `worker_mode: blocked` on purpose. A daemon may import the
file while you are still writing it, and a half-written task must not be dispatched.
Change it to `auto` only when the task is ready. [Task files](tasks.md) explains every
field.

Check it and load it into the task database:

```bash
uv run python -m todo_system.cli validate hello-world   # worker contract OK (open tasks)
uv run python -m todo_system.cli import-md              # imported tasks: 1
uv run python -m todo_system.cli list
# - hello-world | slug=hello-world | ... | status=open | ... | owner=worker:auto
```

`Todo/todo.db` is a cache built from the markdown files. Always edit the markdown,
never the database.

### 5. Dispatch, run, collect

The orchestrator and runner find `Worker/` through `MACP_VAULT_ROOT`, so they work
from any directory:

```bash
uv run --project ~/macp python -m todo_system.orchestrator.orchestrator auto
# candidates=1 dispatched=1 ...
#   dispatched hello-world -> worker-default
```

One **tick** of the orchestrator does the following:

1. It found one dispatchable task.
2. It routed the task with `registry.yaml`. `worker-default` matches `"*"`.
3. It wrote an assignment to the ledger (`Worker/assignments/hello-world.json`).
4. It put a `TASK_ASSIGN` message in the worker's mailbox.

Now run the worker once in **shadow** mode, which goes through the motions without
starting an agent:

```bash
uv run --project ~/macp python -m todo_system.orchestrator.runner \
    --agent worker-default --mode shadow --once
# recv TASK_ASSIGN task=hello-world from=orchestrator
#   RESULT: shadow run for 'hello-world' (no agent spawned)
```

Tick again so the orchestrator collects the result, then look at the ledger:

```bash
uv run --project ~/macp python -m todo_system.orchestrator.orchestrator auto
uv run --project ~/macp python -m todo_system.orchestrator.orchestrator status
# done           hello-world                              worker-default
```

That is the complete control loop. Running 24/7 is these same steps on a timer:
[Running 24/7](running.md).

## Part 2 — a real run on one of your repositories

### Before you start

- Read **[SECURITY.md](../SECURITY.md)**. A worker is a headless agent with your user
  account's permissions. Use a machine or VM where that is acceptable.
- Install the [Claude Code](https://code.claude.com) CLI and log in (`claude` works in
  a terminal).
- Install and authenticate [`gh`](https://cli.github.com) (`gh auth status`). The
  report stage opens a pull request with it.
- Pick a repository you own that has tests the worker can run locally.
- `unset USAGE_GUARD_DISABLE`, so pacing protects your subscription from here on.

### 1. A task that changes a repository

```bash
cd ~/macp
uv run python -m todo_system.cli new add-version-flag --template auto-worker
```

Edit `~/my-vault/Todo/items/add-version-flag.md`:

```yaml
outcome: "`mytool --version` prints the version from pyproject.toml; `uv run pytest` passes"
next_action: "Find the CLI entry point and add a --version flag with a test"
if_then: "When dispatched, implement the flag, add a test, run the test suite"
worker_mode: auto
repo: "https://github.com/you/mytool.git"
auto_merge: false
acceptance_criteria:
  - "uv run pytest passes"
  - "mytool --version prints the version"
```

Describe the work in the body under `## Spec`: the goal, what is in and out of scope,
and pointers. The research stage reads the body first.

Keep `auto_merge: false` for the first runs. The report stage then opens the PR and
stops, and you merge it yourself.

### 2. Run the real pipeline once

```bash
cd ~/macp && uv run python -m todo_system.cli import-md
uv run --project ~/macp python -m todo_system.orchestrator.orchestrator auto
uv run --project ~/macp python -m todo_system.orchestrator.runner \
    --agent worker-default --mode live-staged --once
```

`live-staged` runs the five stages, each as its own `claude -p` call. The repository
is cloned under `~/Worker/<repo>` (change the root with `MACP_WORKER_ROOT`), and the run works in a fresh git worktree on the
branch `worker/add-version-flag`. A run can take from a few minutes to over an hour.

### 3. Read what happened

Every run leaves its artifacts in `~/my-vault/Worker/runs/<run_id>/`:

| Stage | Artifact | What to look for |
|---|---|---|
| research | `requirements.md` | how the agent understood the task |
| plan | `implementation_plan.md` | the steps it chose |
| implement | `run.log` | the stage's captured output |
| verify | `verify.md` | the local checks it ran and their verdict (PASS / FAIL) |
| report | `report.md` | the summary, the branch and the PR link |

Then tick the orchestrator once more to collect the result:

```bash
uv run --project ~/macp python -m todo_system.orchestrator.orchestrator auto
uv run --project ~/macp python -m todo_system.orchestrator.orchestrator trace add-version-flag
```

`trace` shows one task's current state: the latest run, which artifacts belong to it,
the ledger state, and the branch/PR.

If verify failed, the orchestrator retries the task within limits. If the agent needs
a decision, the task is **parked** for you (`awaiting_human`) with the question. See
[How it works — when a person is needed](concepts.md#when-a-person-is-needed).

## Where to go next

- [How it works](concepts.md) — the pieces, the life of a task, the planner.
- [Task files](tasks.md) — every frontmatter field, worker modes, templates.
- [Running 24/7](running.md) — daemons, systemd/launchd units, the dashboard.
- [Configuration](configuration.md) — environment variables, accounts, usage pacing.
- [Troubleshooting](troubleshooting.md) — the mistakes everyone makes once.
