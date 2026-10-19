# Security

## What a worker can do

A worker is a headless `claude -p` process started by the runner, once per pipeline
stage, with your Claude Code login and your user account's permissions. Each stage gets
a tool allowlist (`todo_system/orchestrator/stages.py`, `STAGE_TOOLS`; a task's
`allowed_tools` replaces it). Treat that allowlist as a guard rail against mistakes,
**not as a sandbox**:

- Prefix rules such as `Bash(git:*)`, `Bash(uv:*)`, `Bash(npm:*)`, `Bash(npx:*)` or
  `Bash(./gradlew:*)` allow arbitrary code execution — `uv run`, `npx <pkg>`, a build
  script or a git hook all run whatever the repository says.
- `Bash(gh:*)` (verify, report and quick stages) is the full GitHub CLI: anything your
  `gh` token can do, including `gh api` writes.
- With `auto_merge: true` the report stage squash-merges its own PR once local checks
  pass. There is no human review unless you leave `auto_merge` false.
- Workers read the repositories they clone. Text in a cloned repository (README, issues,
  test fixtures) can try to steer the agent — prompt injection. Only point tasks at
  repositories you trust.

The runner removes a few obviously dangerous rules from any allowlist and sets
`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, but it does not isolate the process.

## Recommended setup

- Run workers on a **dedicated machine or VM** and a dedicated OS user, not on your
  daily laptop.
- Give `gh` a **fine-grained token** limited to the repositories you assign tasks to,
  with only the permissions PRs need (contents and pull requests read/write).
- Turn on **branch protection** for repositories where an unreviewed merge would hurt,
  and keep `auto_merge: false` on those tasks.
- Keep credentials in the vault's `.secrets/` (git-ignored, mode 0600) or in your
  service manager's environment files. Never commit them, and do not list them in a
  task file — task files are read by workers.
- Expose the dashboard only on loopback or a private network; set
  `MACP_DASHBOARD_TOKEN` (and `MACP_PUBLIC_EXPOSURE=1` behind a tunnel) otherwise. The
  dashboard shows vault contents.
- The SMS ingest endpoint (`POST /api/sms`) is disabled unless `MACP_SMS_TOKEN` is set;
  messages are stored outside the vault and never committed.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting on this repository
(**Security → Report a vulnerability**). Do not open a public issue with exploit
details.
