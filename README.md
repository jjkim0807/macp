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

- **Analyzes and splits.** Every task goes through implement (which plans first) →
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
`Executor` interface, and stage execution behind an `AgentBackend` interface.
[Claude Code](https://code.claude.com) (`claude -p`) runs every stage; a dispatch-time
capability gate refuses a backend that can't meet a stage's needs (e.g. the tool allowlist
a read-only stage relies on) instead of silently weakening it.

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
                       │ implement → verify          │     (own worktree, model tier,
                       │ → report (PR)               │      tool allowlist)
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
is **git-backed**, living in the vault tree (`Worker/assignments/`, `Worker/agents/`)
so a second host can take over from git alone; the derived `todo.db` task cache is
rebuilt from the task markdown.

## Try it in five minutes

A real Claude Code worker finishes one small task in a repository of yours and leaves
you a pull request — or a local branch, if the repository has no GitHub remote.
Nothing is merged for you.

You need Linux or macOS, Python ≥ 3.12, [`uv`](https://docs.astral.sh/uv/), `git`, and
the [Claude Code](https://code.claude.com) CLI, logged in. Install macp once:

    uv tool install git+https://github.com/jjkim0807/macp

Then, inside a git repository you own:

```bash
macp init                                   # makes ~/.macp/vault, registers this repository
macp add "Add an install section to the README"
macp up --once                              # dispatch → worker → result, then exit
```

- `macp init` creates your vault (tasks, run records, memory) under `~/.macp/` and
  marks this repository as managed. There are no environment variables to set and no
  vault repository to create. It changes `.macp/` (and `CLAUDE.md` / `AGENTS.md`) in
  your repository and commits nothing.
- `macp add` turns the sentence into a validated task file. You never edit frontmatter.
  The worker sees committed work only — pushed work, when the repository has a remote.
- `macp up --once` runs one pass of the same orchestrator and runner the 24/7 setup
  uses. The worker works in its own clone under `~/.macp/work`, on the branch
  `worker/<slug>`. Your checkout is not touched.

When it finishes, `macp status` shows the task waiting for your review, with the pull
request link. Without a GitHub remote the result is the branch `worker/<slug>` in your
repository: look at it with `git diff ...worker/<slug>`, merge it, then run
`macp complete <slug>`.

`macp up` keeps the loop running in the foreground and paces itself against your
Claude subscription usage; `macp up --once` runs what you queued right away.
`macp up --service` installs the loop as one systemd user unit.

The worker is a headless agent with your user account's permissions — read
[SECURITY.md](SECURITY.md) first. [Getting started](docs/getting-started.md) explains
each step and still documents the manual loop. `bash scripts/check_engine_isolation.sh`
runs these three commands in a clean HOME, with a stand-in for `claude`, and fails if
any of them or the result above changes.

`macp` from the install line above is the command these docs mean. A development
checkout's `scripts/macp` wrapper is a different program (the project/profile CLI).

### Already using Claude Code?

With macp installed by the line above, add it to Claude Code once, inside any session:

    /plugin marketplace add jjkim0807/macp
    /plugin install macp@macp

Then the same commands are slash commands in every session: `/macp init`,
`/macp add "<one sentence>"`, `/macp up --once`, `/macp status`. The plugin has no code
of its own: each slash command runs the `macp` command and shows what it prints, and
`/macp up` runs in the background of the session. Where another `/macp` command
already exists, as in this repository, type `/macp:macp` instead.
[plugins/macp](plugins/macp/README.md) has the details.

## Who it's for

macp fits an individual or a small operator who wants coding agents working on several
repositories all the time. You need a machine or VM where an agent may act with your
user account's permissions.

It does not fit if you want a hosted SaaS for a team. It also does not fit if you
expect it to trade real money or deploy to production with nobody watching.

## Status

macp runs the author's projects every day. The core (task CLI, orchestrator, staged
runner, usage guard, dashboard) is tested and runs standalone. It is still a
personal-scale system, published as-is:

- Workers execute code on your machine and can push to GitHub. **Read
  [SECURITY.md](SECURITY.md) before the first live run.**
- Some integrations are shaped around the author's setup: KakaoTalk/Slack
  notifications and an app-beta publishing sweep. They stay
  silent unless their config file exists; see
  [scripts/NOTIFICATIONS.md](scripts/NOTIFICATIONS.md).
- Parts of the worker prompts (`todo_system/prompts/`), code comments and log messages
  are in Korean. Translation is in progress.

## Contributing

Open an issue first, or send a pull request directly. Before you open a pull request,
run the three commands under [Development](#development) locally. All three must exit
0. This repository is public: do not commit personal identifiers such as emails,
hostnames or private paths.

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
