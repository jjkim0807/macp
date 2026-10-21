# How it works

macp is a **control plane for coding agents**. A coding agent can finish the task in
front of it. macp decides which task is in front of it, with how much model and which
tools, whether the result is really done, and when a person has to decide something.

- [Two planes](#two-planes)
- [The life of a task](#the-life-of-a-task)
- [The staged pipeline](#the-staged-pipeline)
- [Choosing resources](#choosing-resources)
- [Keeping the queue full](#keeping-the-queue-full)
- [When a person is needed](#when-a-person-is-needed)
- [Engine and vault](#engine-and-vault)
- [Repository map](#repository-map)

## Two planes

| | Control plane — decides | Data plane — does |
|---|---|---|
| **Who** | orchestrator, planner, assignment ledger, usage guard | runners driving coding agents; people |
| **What** | what to run next, where, with which model tier and tools, when to retry, when to ask a person | research, code, run tests, open PRs, answer questions |
| **State** | `Worker/assignments/*.json` (ledger), `Worker/agents/*/inbox` (mailboxes) | `Worker/runs/<run_id>/` artifacts, git branches and PRs |

The planes talk only through **messages** (`TASK_ASSIGN`, `PROGRESS`, `RESULT`,
`NEEDS_INPUT`, …) in file mailboxes, and through the ledger. A runner does not know how
tasks are chosen, and the orchestrator does not know how code gets written. That
separation is what keeps macp independent of any particular agent. The runner calls an
`Executor` (`todo_system/orchestrator/runner.py`). Today's executors drive Claude Code
(`claude -p`), and for quick/delegate stages they can drive Upstage Solar
(`orchestrator/solar_stage.py`).

A person is a data-plane agent too: `registry.yaml` lists you as `kind: human`. Tasks
routed to you are parked in your inbox, not executed.

## The life of a task

```
 Todo/items/x.md ──todo-sync──► todo.db ──orchestrator tick──► ledger: dispatched
                                                                  │
                         runner picks up TASK_ASSIGN ◄────────────┘
                                   │
                    research → plan → implement → verify → report
                                   │
                  RESULT ──► orchestrator collects ──► ledger: done ──► task md: done
                     │
                     ├─ verify FAIL ─────────► retried (bounded), then failed
                     ├─ too big ─────────────► split into 2–5 subtasks (rescope)
                     └─ needs a decision ────► awaiting_human (you are asked)
```

1. **You write** `Todo/items/<slug>.md`. The **todo-sync** daemon imports it into
   `Todo/todo.db`, a cache. The markdown stays the source of truth.
2. Every ~5 minutes an **orchestrator tick** (`orchestrator auto`) runs. It loads every
   task whose `worker_mode` is dispatchable (`auto`, `quick`, `delegate`, `discover`) and
   checks five gates:
   - `not_before`
   - `depends_on`
   - the usage guard
   - per-repository concurrency
   - the `touches` overlap
   Then it routes each remaining task through `registry.yaml` and writes an assignment
   to the ledger.
3. The **runner** receives the `TASK_ASSIGN`, prepares a git worktree, and runs the
   pipeline. Each stage writes its artifact into `Worker/runs/<run_id>/`.
4. The **report** stage pushes the branch and opens a PR. With `auto_merge: true` it
   also squash-merges the PR once verify has passed.
5. The next tick **collects** the result. On success the ledger goes to `done` and the
   task file is closed. On failure the orchestrator decides whether to retry, park the
   task, or give up. Stale runs are reaped.

Ledger states: `dispatched`, `accepted`, `in_progress`, `done`, `failed`, `rejected`,
`awaiting_human`, `needs_input`, `deferred`. Never edit the ledger by hand: the
orchestrator owns it. To change what happens to a task, edit its markdown or reply
through the human CLI.

## The staged pipeline

| Stage | Reads | Writes | Job |
|---|---|---|---|
| research | task file, vault notes | `requirements.md` | restate the task as checkable requirements |
| plan | requirements | `implementation_plan.md` | choose the steps and files |
| implement | plan | code on `worker/<slug>`, `run.log` | make the change, commit |
| verify | the diff | `verify.md` | run the project's own checks **locally**, judge against `acceptance_criteria` |
| report | everything | `report.md`, PR | summarize, push, open the PR, merge if allowed |

Shorter pipelines exist for smaller work, all set by `worker_mode`:

- **`quick`**: one combined stage for a trivial change.
- **`delegate`**: research or update work that edits the task file or notes and merges
  no code.
- **`discover`**: clone a project, run its tests and build, and report candidate tasks.

Each stage is a separate agent call with its own time limit, turn budget and tool
allowlist (`orchestrator/stages.py`). The stages share nothing but the artifacts on
disk. A stage that overruns loses only its own work, and the next stage can always
see what the previous one produced.

Verification is local by design. macp does not use GitHub Actions. The verify stage
runs the repository's own commands (`pytest`, `ruff`, `./gradlew test`, …) in the
worktree.

## Choosing resources

**Model tier.** `model_select.classify_tier` picks heavy, standard or light. The first
matching rule wins:

1. an explicit `model_tier:` field
2. tags: `hard`/`complex`/`architecture` mean heavy; `trivial`/`quick`/`chore` mean light
3. `worker_mode`: `quick` means light, `delegate` means standard
4. `estimate_p90`: ≤ 45 min means light, ≥ 4 h means heavy
5. otherwise standard

Each tier is a **ladder** of models tried top-down (the first one available on your
account is used), with an effort level (heavy max, standard high, light medium) and a
stage timeout (3 h / 2 h / 1 h).

**Tools.** Each stage has a default allowlist. A task's `allowed_tools` string replaces
it, for example to allow `Bash(./gradlew:*)`. The runner always strips a few dangerous
rules. Treat the allowlist as a guard rail, not a sandbox (see [SECURITY.md](../SECURITY.md)).

**Isolation and concurrency.** Every run gets its own git worktree on
`worker/<slug>`. Two tasks on the same repository run at the same time only when both
declare `touches` lists that do not overlap. Global caps are
`MACP_MAX_PARALLEL_PER_REPO` (default 2) and `MACP_MAX_PARALLEL_GRADLE_BUILDS`.

**Usage pacing.** The usage guard reads your Claude subscription's 5-hour and weekly
utilization. It holds new dispatch (`USAGE-HOLD`) when automation runs ahead of the
elapsed share of the window, and above a hard cap. Slack below the line buys extra
parallel slots. A single task can pass a pacing hold with `bypass_guard: true`.

**Splitting.** When implement finds a task too big for one stage, the worker parks it
with a split proposal. `rescope` then writes 2–5 dependent subtasks
(`depends_on` chains, each with `parent:`) without a person. When every subtask is
done, the parent closes.

## Keeping the queue full

When there is nothing dispatchable, the **planner** tick (`scripts/planner_tick.sh`,
prompt `.claude/commands/worker-planner.md`) reads the vault and the projects and files
new tasks that are safe to run without confirmation. It works harder while
subscription tokens are going unused and the queue is idle (`orchestrator/planner_mode.py`).
**Discovery** goes further: it clones a project, runs its tests, build and a smoke
launch, sweeps the tree, and turns what it finds into candidate tasks
(`orchestrator/discovery.py`).

Follow-ups that a report stage names ("next steps") are picked up durably as well
(`orchestrator/followups.py`), so finished work keeps leading to the next piece of work.

## When a person is needed

macp asks only when a decision is really yours, and it says exactly what it needs:

- A run that hits a question it cannot answer moves to **`awaiting_human`**. The
  question and the blocker (`blocked_on`) are recorded and shown in the daily brief and
  on the dashboard.
- Tasks that only a person can do (`worker_mode: manual`, or routed to your `human`
  agent) are tracked in the same ledger. Re-nudges back off over time.
- A project that quietly stalls gets **one** decision task instead of silence
  (`orchestrator/project_stall.py`).

Answer from the CLI:

```bash
cd ~/my-vault
uv run --project ~/macp python -m todo_system.orchestrator.human reply \
    --agent worker-default --task <slug> --status reject --note "Re-run with the new spec"
```

Statuses: `done`, `progress`, `needs-input`, `accept`, `reject`. To change a parked
task's instructions, edit its markdown, commit, and run
`cli resume <slug> --after-commit <sha>`. The task is dispatched again once the vault
contains that commit.

## Engine and vault

| | Engine (this repository) | Vault (yours, private) |
|---|---|---|
| Contains | code, prompts (`.claude/commands/`), unit generators, the vault skeleton | tasks, ledger, runs, memory, notes, decisions, secrets, host identity |
| Updated by | `git pull` / your PRs | the daemons and you |
| Found via | the install location | `MACP_VAULT_ROOT`, else the nearest `.macp/config.toml` |

The engine never writes personal data into its own tree, and
`scripts/check_engine_isolation.sh` checks that it runs from a bare clone. Several
machines can share one vault through git: one host dispatches, and the others run
workers.

## Repository map

| Path | What lives there |
|---|---|
| `todo_system/cli.py` | the task CLI: `new`, `validate`, `import-md`, `list`, `set`, `complete`, `daemon`, `resume`, `session-end` |
| `todo_system/orchestrator/orchestrator.py` | the tick: dispatch, collect, retry, reap, reconcile |
| `todo_system/orchestrator/runner.py`, `stages.py` | the runner, executors and stage definitions (prompts, tools, timeouts) |
| `todo_system/orchestrator/model_select.py` | model tiers and ladders |
| `todo_system/orchestrator/assignment.py`, `mailbox.py`, `message.py`, `registry.py` | the ledger, mailboxes, message types and agent registry |
| `todo_system/orchestrator/rescope.py`, `followups.py`, `discovery.py`, `planner_mode.py` | splitting, follow-ups, discovery, planner intensity |
| `todo_system/orchestrator/human.py`, `resume.py`, `project_stall.py`, `decisions.py` | the human side: replies, conditional resume, stall and decision surfacing |
| `todo_system/orchestrator/trace.py`, `metrics.py`, `dashboard.py` | read-only views of the system |
| `todo_system/usage_guard.py` | subscription usage pacing |
| `todo_system/macp/` | profiles and accounts (`python -m todo_system.macp`), including Upstage |
| `todo_system/paths.py` | how the engine finds the vault and itself |
| `.claude/commands/` | the stage prompts (`worker-research.md`, `worker-implement.md`, …) and interactive skills |
| `scripts/` | daemons, the dashboard, unit installers and integrations — see [scripts/README.md](../scripts/README.md) |
| `vault-template/` | the vault skeleton |
| `docs/design/` | design notes |
