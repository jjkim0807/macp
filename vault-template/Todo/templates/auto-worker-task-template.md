---
tags:
  - todo
  - work
  - worker
status: open
due: YYYY-MM-DD
updated: 2026-05-19
outcome: "One line: what must be true to close this task"
next_action: "The very next concrete step"
if_then: "When the worker picks this up, do X and check Y"
estimate_p50: 30m
estimate_p90: 90m
review_cycle: weekly
priority: 70

# --- Worker fields ---
# The parser does NOT strip inline comments: `worker_mode: auto  # note` makes the
# value "auto  # note", which is not "auto". Keep every comment on its own line.
#
# worker_mode: auto | quick | delegate | discover | blocked | review | manual | off
#
# A new scaffold starts as `blocked` on purpose. The todo-sync daemon imports files
# every few seconds, so a half-written task with `auto` could be dispatched before
# you finish it. Switch to `auto` only when the task is ready.
worker_mode: blocked
# repo: git URL of the project this task changes. Omit for work on the vault itself.
# repo: "https://github.com/you/project.git"
# auto_merge: true lets the report stage squash-merge its PR once verify passed.
auto_merge: false
max_turns: 80
# max_budget_usd is informational; nothing enforces it.
max_budget_usd: 2.0
# allowed_tools MUST be one quoted, comma-separated string. A YAML list is ignored
# and the stage falls back to its default tools. Rules may not contain spaces:
# `Bash(uv run:*)` matches nothing, write `Bash(uv:*)`.
allowed_tools: "Read,Edit,Write,Bash(git:*),Bash(uv:*),Bash(pytest:*)"
# model_tier: heavy | standard | light (optional). If omitted it is inferred:
# tags (hard/complex → heavy, trivial/quick/chore → light), then worker_mode
# (quick → light, delegate → standard), then estimate_p90 (≤45m light, ≥4h heavy),
# else standard.
# model_tier: standard
# touches: a YAML list of repo-relative paths/globs this task will change. Two tasks
# of the same repo run in parallel only when both declare lists that do not overlap;
# without it the task is serialized with every other task of that repo. Unlike
# allowed_tools this MUST be a list. Globs are compared by their static prefix
# (before the first wildcard), so `dir/Foo*Test.kt` counts as all of `dir` — use
# `dir/**` or list the files.
# touches:
#   - "src/feature/**"
#   - "tests/test_feature.py"
acceptance_criteria:
  - "tests pass"
  - "lint clean"
worker_branch: "worker/<slug>"
# long_job: a shell command that takes longer than a stage (stages are capped at
# 1–3 h). The runner starts it detached and resumes the task when it finishes; a
# background process started inside a stage dies with the stage.
# long_job: "uv run python collect.py --years 10"
---

# Task title

> A single task for the worker. The `## Spec` section below is the first input the
> research and plan stages read.

## Spec

- **Goal**: one line — what must be finished
- **Background**: why this is needed (1–3 sentences)
- **Scope**:
  - In: which files or modules may change
  - Out: what must not be touched
- **References**:
  - Notes: [[Memory/...]]
  - Decisions: [[Decisions/...]]
  - Links: ...

## Acceptance

- Matches the frontmatter `acceptance_criteria`, written out in prose.
- Example: `uv run pytest` passes.
- Example: `uv run ruff check .` is clean.

## Steps (optional — the plan stage writes its own)

- [ ] Step 1
- [ ] Step 2

## Progress Log

- 2026-05-19: created
