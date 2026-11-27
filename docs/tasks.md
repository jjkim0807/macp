# Task files

A task is one markdown file in `<vault>/Todo/items/<slug>.md`. The YAML frontmatter
tells the engine what to do. The body is what the agent reads. The file is the
source of truth: `Todo/todo.db` is rebuilt from it, and the engine writes state back
into it, for example `status: done` and Progress Log lines.

- [Create one](#create-one)
- [A complete example](#a-complete-example)
- [Worker modes](#worker-modes)
- [Field reference](#field-reference)
- [Ordering and gating](#ordering-and-gating)
- [Repeating tasks](#repeating-tasks)
- [Writing a good task](#writing-a-good-task)
- [Rules the parser enforces](#rules-the-parser-enforces)

## Create one

```bash
uv run python -m todo_system.cli new <slug> --template auto-worker   # for the AI worker
uv run python -m todo_system.cli new <slug> --template work          # a task for you
uv run python -m todo_system.cli validate <slug>                     # check before enabling
```

The slug is kebab-case English and becomes the file name, the branch
(`worker/<slug>`) and the run id. Templates live in `<vault>/Todo/templates/`, so edit
them to match your habits.

## A complete example

```markdown
---
tags: [todo, work, mytool]
status: open
due: 2026-11-30
outcome: "`mytool --version` prints the version from pyproject.toml; `uv run pytest` passes"
next_action: "Find the CLI entry point and add a --version flag with a test"
if_then: "When dispatched, implement the flag, add a test, run the suite"
estimate_p50: 30m
estimate_p90: 90m
priority: 60
worker_mode: auto
repo: "https://github.com/you/mytool.git"
auto_merge: true
allowed_tools: "Read,Edit,Write,Bash(git:*),Bash(uv:*),Bash(pytest:*)"
touches:
  - "src/mytool/cli.py"
  - "tests/test_cli.py"
acceptance_criteria:
  - "uv run pytest passes"
  - "uv run ruff check . is clean"
---

# Add a --version flag

## Spec

- **Goal**: `mytool --version` prints the package version.
- **Background**: users report bugs without saying which version they run.
- **Scope**: `src/mytool/cli.py` and its tests. Do not touch packaging.
- **References**: the version lives in `pyproject.toml` (`[project] version`).

## Acceptance

- `uv run pytest` passes, including a new test for `--version`.

## Progress Log

- 2026-11-01: created
```

## Worker modes

`worker_mode` decides whether the worker picks the task up and how much pipeline it
gets.

| Mode | Dispatched? | Pipeline | Use it for |
|---|---|---|---|
| `auto` | yes | implement → verify → report | normal code changes |
| `quick` | yes | one combined stage | a trivial, safe change (a typo, a config value) |
| `delegate` | yes | one stage that researches and updates the task file or notes | investigation, drafting, "find out and write it down" |
| `discover` | yes | clone, run tests/build/smoke launch, sweep, report candidates | finding work in a project |
| `blocked` | no | — | not ready yet; flips automatically when `depends_on` is done |
| `review` | no | — | must be looked at by a person first |
| `manual` | no | — | a person does it; tracked in the brief and dashboard |
| `off` | no | — | ignored |

A task with no `worker_mode` belongs to you.

## Field reference

**Required for every task**

| Field | Meaning |
|---|---|
| `status` | `open` · `done` · `cancelled` |
| `outcome` | one line: what must be true to close the task |
| `next_action` | the very next concrete step |
| `if_then` | "when/where X happens, do Y". The trigger a worker or person acts on |
| `tags` | starts with `todo`, then your own categories |

**Planning**

| Field | Meaning |
|---|---|
| `due` | absolute date `YYYY-MM-DD`, or `-` |
| `estimate_p50`, `estimate_p90` | optimistic and conservative effort (`30m`, `2h`). p90 feeds the model tier |
| `priority` | relative importance (0–100); higher sorts first in views and stale-task reminders |
| `review_cycle` | `daily` · `weekly` · `-` |

**Worker behaviour**

| Field | Meaning |
|---|---|
| `worker_mode` | see [Worker modes](#worker-modes) |
| `repo` | git URL of the project to change. Omit it for work on the vault itself |
| `auto_merge` | `true` lets the report stage squash-merge its PR once verify passed. Default `false` |
| `allowed_tools` | a **quoted, comma-separated string** added to the implement, quick and delegate stages' default allowlist. verify, report and discover ignore it |
| `touches` | a **YAML list** of repo-relative paths or globs. Same-repo tasks run in parallel only when their lists do not overlap |
| `acceptance_criteria` | list of checks verify must see pass |
| `model_tier` | `heavy` · `standard` · `light`. Otherwise inferred (see [How it works](concepts.md#choosing-resources)) |
| `long_job` | a shell command that outlives a stage (data collection, a long benchmark). The runner starts it detached and resumes the task when it ends. Starting and checking the job continue through a usage hold |
| `bypass_guard` | `true` lets this one task through a usage-pacing hold |
| `screen_check` | post-verify first-screen capture on the emulator node. **Runs by default** on an app repo whose diff touches `app/` (or the repo's configured `app_paths`); set `false` to opt out, `true` to force it regardless of the diff. A non-app repo, or a diff that misses the app module, is a no-op. See [Configuration](configuration.md#app-screen-check-and-launch-extras) |
| `screen_check_cap_min` | node-uptime cap for that check in minutes (default 20) |
| `screen_check_gradle_task` | the gradle task that builds the debug APK (default `assembleDebug`) |

## Ordering and gating

| Field | Meaning |
|---|---|
| `depends_on` | `[slug, …]`. Upstream tasks that must be `done` first. Pair with `worker_mode: blocked` |
| `on_unblock` | the mode to flip to when every dependency is done (default `auto`) |
| `auto_activate` | `false` opts out of the automatic flip |
| `human_gate` | `true` means a person must look first, even with every dependency done. It also stops the automatic split of a task that is too big for one stage |
| `not_before` | earliest start, `YYYY-MM-DD` or `YYYY-MM-DD HH:MM` |
| `recur` | `daily` · `weekly` · `every Nd` · `weekly <day> HH:MM` · `off`. The task repeats: when a round's `not_before` comes, the next round is written as `<series>-<YYYYMMDD>.md`. See [Repeating tasks](#repeating-tasks) |
| `parent` | `"[[parent-slug]]"`. When every child is done, the parent closes |

To chain work, write the children as `worker_mode: blocked` with `depends_on` pointing
at the previous child. Each one starts as soon as its predecessor merges.

## Repeating tasks

Add `recur` to a task and it becomes the first round of a series. It works for a
worker task and for a task of your own alike.

```yaml
not_before: "2026-11-27 10:00"
recur: weekly
```

| Value | The next round is |
|---|---|
| `daily` | one day after this one |
| `weekly` | seven days after this one |
| `every Nd` | N days after this one, for example `every 3d` |
| `weekly <day> HH:MM` | the next `<day>` at that time, for example `weekly fri 09:00` (`mon`…`sun`, the full names and 월…일 all work) |
| `off` | not written: the series stops here |

- **Rounds are files.** A series is `<series>.md` (a first round written without a
  date) plus `<series>-<YYYYMMDD>.md`. The slug is all that ties them together, so do
  not give an unrelated task the same name with a date on the end. An undated
  `<series>.md` with no `recur` of its own, such as an umbrella task the rounds name
  as their `parent`, is not part of the series.
- **When.** On the first tick after the newest round's `not_before` has passed (at
  once, if it has none) the orchestrator writes the next round. Nothing is written
  while another round of the series is still open; close it and the series picks up
  again.
- **What.** The next round is a copy of the newest one: `not_before` and the date in
  the slug become the next time, the dates in `due` and `scheduled` move by the same
  number of days (a `HH:MM~HH:MM` block keeps its times), `status` is `open`, every
  checkbox is cleared and the Progress Log starts over. Everything else is copied as
  written, so edit a round that has not started yet and the rounds after it inherit
  the change. Prose is not rewritten either: word the title, `outcome` and steps
  without the round's own date ("this week's report", not "the 2026-11-27 report").
- **From when.** The next time is counted from this round's `not_before`. Without
  one it is counted from its `scheduled` date, then the date in its slug, then today.
  Periods that went by unnoticed are skipped: you get one round, at the next time
  still ahead.
- **Stopping.** Closing or cancelling a round does not stop the series; the next
  round still appears when the closed one's time comes. To stop, delete `recur` from
  the newest round or set `recur: off`.

## Writing a good task

- **`outcome` is a test, not a wish.** "`uv run pytest` passes and `--version` prints
  1.2.0" can be verified; "improve the CLI" cannot.
- **Name the local check.** Verify runs the repository's own commands, so say which
  ones (`./gradlew :app:testDebugUnitTest`, `uv run pytest -q`).
- **Scope it to one implement stage.** Roughly 20–60 minutes of focused work. Bigger
  tasks are split automatically, but a clear scope splits better.
- **Say what not to touch.** A "do not modify" line in the spec is cheaper than a
  rejected PR.
- **Put secrets elsewhere.** Workers read task files, so never paste a token into one.

## Rules the parser enforces

- **No inline comments after a value.** `worker_mode: auto  # note` makes the value
  `auto  # note`, which is not `auto`. Put comments on their own line above.
- **`allowed_tools` is a string, `touches` is a list.** A YAML list for `allowed_tools`
  is ignored (the stage falls back to its default tools), and a string for `touches`
  is an error.
- **Tool rules contain no spaces.** `Bash(uv run:*)` matches nothing, because the
  allowlist is split on spaces. Use `Bash(uv:*)`.
- **Dates are absolute.** "tomorrow" is not parsed.
- **`recur` takes exactly the values in [Repeating tasks](#repeating-tasks).** Anything
  else (`monthly`, `weekly fri` without a time) is an error, and the task does not
  repeat.

`cli validate <slug>` reports all of these before the worker ever sees the file.
