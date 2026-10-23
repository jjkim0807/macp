# macp — Maestro Agent Control Plane

**Queue tasks; macp keeps coding agents working on them around the clock.**

[한국어 README](README.ko.md) · [Getting started](docs/getting-started.md) ·
[How it works](docs/concepts.md) · [Task files](docs/tasks.md) ·
[Running 24/7](docs/running.md) · [Configuration](docs/configuration.md) ·
[Troubleshooting](docs/troubleshooting.md) · [Security](SECURITY.md)

A coding agent is good at the one task in front of it. Keeping many projects moving
takes more than that. Something has to decide what to work on next, how much model to
spend on it, which tools it may use, when it is really finished, and when a person has
to step in. macp is that layer.

You write tasks as small markdown files. macp then:

- **Analyzes and splits.** Every task goes through research → plan → implement →
  verify → report. A task that turns out too big for one implement stage is split
  into 2–5 dependent subtasks automatically.
- **Allocates resources.** Each task gets a model tier (heavy / standard / light)
  chosen from its difficulty, a tool allowlist, its own git worktree, and a slot under
  per-repository concurrency limits. Usage pacing keeps automation inside your
  subscription limits.
- **Runs 24/7.** Daemons dispatch work, collect results, retry, and clean up stale runs
  without anyone watching. You are asked only when a decision is yours.
- **Refills the queue.** When the task pool runs dry, a planner runs and re-reads your
  projects (tests, build, docs) to find needed work and files it as new tasks.

macp aims to be **orthogonal to the coding agent**. The agent writes the code; macp
decides what gets worked on, with which resources, and when. Agents sit behind an
`Executor` interface. Today's executor drives [Claude Code](https://code.claude.com)
(`claude -p`); support for other coding agents is planned.

**In use.** From 2026-07-20 to 2026-10-20 the author's instance completed **1,080
tasks** on its own across **22 independent repositories**. In October 2026 that was
about 25 a day (counted from its assignment ledger).

## How it fits together

```
          you ──────────────► Todo/items/*.md  (one task per file)
                                     │
                       ┌─────────────┴───────────────┐
  control plane        │ orchestrator (every ~5 min) │◄── planner (queue empty?
                       │ route · pace · dispatch ·   │     find work, file tasks)
                       │ collect · retry · reap      │
                       └─────────────┬───────────────┘
                                     │  assignment ledger + mailbox
                       ┌─────────────┴───────────────┐
  data plane           │ runner                      │──► coding agent per stage
                       │ research → plan → implement │     (own worktree, model tier,
                       │ → verify → report (PR)      │      tool allowlist)
                       └─────────────┬───────────────┘
                                     │  questions it cannot answer
                                     ▼
                                    you
```

The **control plane** (orchestrator, planner, ledger) decides; the **data plane**
(agent runners and people) does the work. Everything personal lives in a **vault**
directory you own: tasks, memory, run records, secrets. This repository is the engine
and holds no personal data. [How it works](docs/concepts.md) has the full picture.

Orchestration state — the assignment ledger, the mailboxes and the planner's notes —
is **git-backed by default**, living in the vault tree (`Worker/assignments/`,
`Worker/agents/`) so a second host can take over from git alone. Setting
`MACP_STATE_BACKEND=external` (or `state_backend = "external"` in `.macp/config.toml`)
instead keeps it in a host-local SQLite file outside the vault tree
(`~/.local/share/macp/state.sqlite`, overridable with `MACP_STATE_SQLITE_PATH`), so a
stray `git checkout`/`reset`/`stash` can never resurrect an acked message or rewind
the ledger; the derived `todo.db` task cache is unaffected either way.

## Try it in five minutes

No agent is called and nothing leaves your machine. You need Linux or macOS,
Python ≥ 3.12, [`uv`](https://docs.astral.sh/uv/) and `git`.

```bash
git clone https://github.com/jjkim0807/macp ~/macp && cd ~/macp
uv sync

cp -r vault-template ~/my-vault            # your vault: tasks, runs, memory
export MACP_VAULT_ROOT=~/my-vault
export USAGE_GUARD_DISABLE=1               # nothing to pace yet (see below)

uv run python -m todo_system.cli new hello-world --template auto-worker
#   edit ~/my-vault/Todo/items/hello-world.md:
#   write an outcome, and change `worker_mode: blocked` to `worker_mode: auto`
uv run python -m todo_system.cli validate hello-world   # → worker contract OK
uv run python -m todo_system.cli import-md              # → imported tasks: 1

uv run --project ~/macp python -m todo_system.orchestrator.orchestrator auto
#   → dispatched hello-world -> worker-default
uv run --project ~/macp python -m todo_system.orchestrator.runner \
    --agent worker-default --mode shadow --once
#   → RESULT: shadow run for 'hello-world' (no agent spawned)
uv run --project ~/macp python -m todo_system.orchestrator.orchestrator auto
uv run --project ~/macp python -m todo_system.orchestrator.orchestrator status
#   → done   hello-world   worker-default
```

That is the whole loop: a task file was imported, routed, dispatched, executed (in
shadow mode) and collected. `USAGE_GUARD_DISABLE=1` skips usage pacing. Without it, a
machine where Claude Code is logged in is paced against that account's real usage,
and dispatch can be held. [Getting started](docs/getting-started.md) walks through the
same steps with explanations, then a first **real** run against one of your
repositories.

## Status

macp runs the author's projects every day. The core (task CLI, orchestrator, staged
runner, usage guard, dashboard) is tested and runs standalone. It is still a
personal-scale system, published as-is:

- Workers execute code on your machine and can push to GitHub. **Read
  [SECURITY.md](SECURITY.md) before the first live run.**
- Some integrations are shaped around the author's setup: KakaoTalk/Slack
  notifications, an app-beta publishing sweep, and an `advote` bridge. They stay
  silent unless their config file exists; see
  [scripts/NOTIFICATIONS.md](scripts/NOTIFICATIONS.md).
- Parts of the worker prompts (`.claude/commands/`), code comments and log messages
  are in Korean. Translation is in progress.

Issues and pull requests are welcome.

## Development

```bash
MACP_VAULT_ROOT=vault-template uv run --with pytest python -m pytest -q todo_system scripts/systemd
uv run --with ruff ruff check .
bash scripts/check_engine_isolation.sh
```

GitHub Actions is not used; run the checks locally. The full suite takes several
minutes. [docs/concepts.md](docs/concepts.md#repository-map) has a map of the
repository.

## License

MIT — see [`LICENSE`](LICENSE).
