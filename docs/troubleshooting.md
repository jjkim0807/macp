# Troubleshooting

The problems below have all happened on a real installation. Most come from the task
file or the environment, not the engine.

## A task is never dispatched

Check these in order:

1. **`worker_mode`** must be `auto`, `quick`, `delegate` or `discover`. A new scaffold
   says `blocked` on purpose.
2. **An inline comment.** `worker_mode: auto  # ready` is read as the value
   `auto  # ready`. Move comments to their own line.
3. **The database is stale.** Run `cli import-md`, or keep the todo-sync daemon
   running.
4. **`not_before` / `depends_on`.** The task is deferred or waiting on another task.
   Run `cli validate <slug>`.
5. **`USAGE-HOLD (…)`** at the end of the tick line means pacing held dispatch on
   purpose. See [Configuration — usage pacing](configuration.md#usage-pacing).
6. **Same-repo serialization.** "deferred — checkout … is busy" means another run holds
   the repository. Declare disjoint `touches` lists on both tasks to let them run side
   by side.

`orchestrator trace <slug>` shows what the system currently believes about one task.

## `VaultRootError: cannot resolve the MaestroACP vault root`

Set `MACP_VAULT_ROOT` to your vault, or create `<vault>/.macp/config.toml`. When you
run from a source clone, the config-file search starts at the clone, not at your
current directory, so set the variable.

## A stage fails with "Not logged in" or exits immediately

- Run `claude` in a terminal as the **same OS user** the runner runs as, and log in.
- Under systemd, the unit's environment may differ from your shell. Check `HOME` and
  any `CLAUDE_CONFIG_DIR` in the unit or its `EnvironmentFile`.
- A stage that ends with exit code 0 but has no artifact can mean the subscription
  quota ran out mid-stage. The CLI does not always signal that with a failing exit
  code. The usage guard exists to prevent this.

## The worker cannot run a command it needs

- The command is missing from the stage's allowlist. Add it to the task's
  `allowed_tools` string, for example
  `"Read,Edit,Write,Bash(git:*),Bash(./gradlew:*)"`.
- **A rule with a space never matches.** `Bash(uv run:*)` allows nothing, because the
  allowlist is split on spaces. Write `Bash(uv:*)`.
- `allowed_tools` written as a YAML list is ignored, and the stage falls back to its
  default tools. It must be one quoted string.

## A long command is killed

A stage has a time budget (1–3 h by tier), and a background process started inside a
stage dies when the stage ends. Put long preliminary work (data collection, a long
benchmark) in the task's `long_job:` field. The runner starts it detached and resumes
the task when it finishes.

## Verify fails, but the code looks right

Verify runs the repository's own checks **locally** in the worktree. GitHub Actions is
not consulted. Read `Worker/runs/<run_id>/verify.md` for the exact commands and
output. Missing toolchains on the worker host (a JDK, Node, an Android SDK) are the
usual cause. Name the exact local command in `acceptance_criteria`.

## The queue is stuck and nothing is running

- An uncommitted file in the vault can stop the next run's branch preparation. Commit
  or remove it (`git -C ~/my-vault status`).
- `orchestrator status` shows tasks in `awaiting_human`. Those are waiting for **you**.
  Answer with `orchestrator.human reply` (see
  [How it works](concepts.md#when-a-person-is-needed)).
- Never edit `Worker/assignments/*.json` by hand. The orchestrator owns the ledger, and
  a hand edit races the next tick.

## The dashboard will not start

It refuses non-loopback addresses without `MACP_DASHBOARD_TOKEN`. With
`MACP_PUBLIC_EXPOSURE=1` it requires the token on loopback as well. That is
intentional, because the dashboard shows vault contents.

## Something else

Open an issue with the tick line, the relevant `Worker/runs/<run_id>/` artifacts (check
them for private data first) and `orchestrator trace <slug>` output.
