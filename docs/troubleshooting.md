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
7. **No free slot.** "N task(s) wait in the queue — `worker` 빈 슬롯 없음 (2/2 사용 중)"
   means the worker is full; the task goes out when a run ends. "슬롯 하트비트 없음"
   means that runner stopped announcing: check that its process is up
   (`orchestrator agents`). The dashboard's waiting list shows the same line per task.

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

## A vault task's PR conflicts on files the run never touched

A vault task works inside the vault repository, so while `worker/<slug>` is checked
out the vault's own daemons can commit onto it (`chore: sync vault state`,
`chore: planner`, `chore: beta watermark`, `chore(mailbox):`, …). `main` gets the
same state under other commits, and the PR reads CONFLICTING on
`Worker/assignments/*.json`, `Memory/index.md` and the like.

For an `auto_merge: true` **delegate** task the engine handles this itself
(`orchestrator/vault_pr_rebuild.py`). When the squash merge is refused for a conflict
it replays only the run's own commits onto `origin/main` (the engine's commits are
dropped, and a commit `main` already has is skipped), pushes the result to the same
`worker/<slug>` as a fast-forward, and merges again. It never uses `--admin`,
`--auto`, a force push or a push to `main`. If one of the run's own commits really
conflicts, nothing is pushed: the task parks in `awaiting_human` and the note names
the conflicting files. Resolve those by hand.

The engine list of commit subjects is `ENGINE_COMMIT_PREFIXES` in that module. A code
repository's PR never takes this path. Its conflicts are real ones.

## Builds fail with "No space left on device"

Look for `disk-prune:` and `disk guard:` lines in the maintenance and runner logs.
`python -m todo_system.maintenance_disk --dry-run` shows free space, the protected
Gradle version set (wrapper ∪ live daemon versions, or UNKNOWN), and which caches a
pass would remove. A line ending in `— STILL below`
means the prunable caches are already gone, and what fills the disk is something
neither guard may touch (clones, distributions, data). `du -sh ~/.gradle ~/.cache
~/Worker` usually finds it. See [Running 24/7](running.md#disk-space).

## The dashboard will not start

It refuses non-loopback addresses without `MACP_DASHBOARD_TOKEN`. With
`MACP_PUBLIC_EXPOSURE=1` it requires the token on loopback as well. That is
intentional, because the dashboard shows vault contents.

## Something else

Open an issue with the tick line, the relevant `Worker/runs/<run_id>/` artifacts (check
them for private data first) and `orchestrator trace <slug>` output.
